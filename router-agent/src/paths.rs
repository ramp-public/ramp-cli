//! Path helpers (POSIX normalization, `~` expansion) and cached `stat` that never touches autofs.

use std::collections::HashMap;
use std::sync::{LazyLock, Mutex};
use std::time::{Duration, Instant};

/// Unicode whitespace plus the ASCII separators U+001C..U+001F.
#[inline]
pub fn is_space(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

pub fn strip(s: &str) -> &str {
    s.trim_matches(is_space)
}

pub fn strip_chars<'a>(s: &'a str, chars: &str) -> &'a str {
    s.trim_matches(|c| chars.contains(c))
}

pub fn rstrip_chars<'a>(s: &'a str, chars: &str) -> &'a str {
    s.trim_end_matches(|c| chars.contains(c))
}

/// The first `n` characters of `s`.
pub fn char_prefix(s: &str, n: usize) -> &str {
    if s.len() <= n {
        return s;
    }
    match s.char_indices().nth(n) {
        Some((i, _)) => &s[..i],
        None => s,
    }
}

pub fn join(a: &str, b: &str) -> String {
    if b.starts_with('/') {
        return b.to_string();
    }
    if a.is_empty() || a.ends_with('/') {
        format!("{a}{b}")
    } else {
        format!("{a}/{b}")
    }
}

/// POSIX normalization: drops `.` and empty components and resolves `..` lexically.
pub fn normpath(path: &str) -> String {
    if path.is_empty() {
        return ".".into();
    }
    let initial = if path.starts_with('/') {
        if path.starts_with("//") && !path.starts_with("///") { 2 } else { 1 }
    } else {
        0
    };
    let mut comps: Vec<&str> = Vec::new();
    for comp in path.split('/') {
        if comp.is_empty() || comp == "." {
            continue;
        }
        if comp != ".." || (initial == 0 && comps.is_empty()) || comps.last() == Some(&"..") {
            comps.push(comp);
        } else if !comps.is_empty() {
            comps.pop();
        }
    }
    let mut out = "/".repeat(initial);
    out.push_str(&comps.join("/"));
    if out.is_empty() { ".".into() } else { out }
}

pub fn dirname(p: &str) -> String {
    let i = p.rfind('/').map(|i| i + 1).unwrap_or(0);
    let head = &p[..i];
    if !head.is_empty() && head.bytes().any(|b| b != b'/') {
        head.trim_end_matches('/').to_string()
    } else {
        head.to_string()
    }
}

pub fn basename(p: &str) -> &str {
    let i = p.rfind('/').map(|i| i + 1).unwrap_or(0);
    &p[i..]
}

pub fn abspath(p: &str) -> String {
    if p.starts_with('/') {
        normpath(p)
    } else {
        let cwd = std::env::current_dir().map(|c| c.to_string_lossy().into_owned()).unwrap_or_else(|_| "/".into());
        normpath(&join(&cwd, p))
    }
}

pub fn expanduser(p: &str) -> String {
    if !p.starts_with('~') {
        return p.to_string();
    }
    let i = p.find('/').unwrap_or(p.len());
    let user = &p[1..i];
    let home = if user.is_empty() { std::env::var("HOME").ok().or_else(|| home_of(None)) } else { home_of(Some(user)) };
    match home {
        Some(h) => {
            let h = h.trim_end_matches('/');
            let h = if h.is_empty() { "/" } else { h };
            let rest = &p[i..];
            if h == "/" && rest.starts_with('/') { rest.to_string() } else { format!("{h}{rest}") }
        }
        None => p.to_string(),
    }
}

fn home_of(user: Option<&str>) -> Option<String> {
    unsafe {
        let pw = match user {
            None => libc::getpwuid(libc::getuid()),
            Some(u) => {
                let c = std::ffi::CString::new(u).ok()?;
                libc::getpwnam(c.as_ptr())
            }
        };
        if pw.is_null() || (*pw).pw_dir.is_null() {
            return None;
        }
        Some(std::ffi::CStr::from_ptr((*pw).pw_dir).to_string_lossy().into_owned())
    }
}

pub fn isdir(p: &str) -> bool {
    stat(p) == Some(true)
}

pub fn isfile(p: &str) -> bool {
    stat(p) == Some(false)
}

static STAT_CACHE: LazyLock<Mutex<HashMap<String, (Instant, Option<bool>)>>> = LazyLock::new(|| Mutex::new(HashMap::new()));
static AUTOFS: LazyLock<Vec<String>> = LazyLock::new(autofs_mount_points);

/// Some(true) = directory, Some(false) = other existing file, None = missing / error. Cached
/// briefly; paths under an autofs trigger directory (macOS /home, /net) are never statted, since
/// each lookup would go through automountd.
fn stat(p: &str) -> Option<bool> {
    if p.contains('\0') || AUTOFS.iter().any(|m| p.len() > m.len() && p.starts_with(m.as_str()) && p.as_bytes()[m.len()] == b'/') {
        return None;
    }
    if let Some((t, v)) = STAT_CACHE.lock().unwrap().get(p) {
        let ttl = if v.is_some() { Duration::from_secs(5) } else { Duration::from_secs(30) };
        if t.elapsed() < ttl {
            return *v;
        }
    }
    let v = std::fs::metadata(p).ok().and_then(|m| if m.is_dir() { Some(true) } else if m.is_file() { Some(false) } else { None });
    let mut c = STAT_CACHE.lock().unwrap();
    if c.len() > 100_000 {
        c.clear();
    }
    c.insert(p.to_string(), (Instant::now(), v));
    v
}

#[cfg(target_os = "macos")]
fn autofs_mount_points() -> Vec<String> {
    let mut out = Vec::new();
    unsafe {
        let n = libc::getfsstat(std::ptr::null_mut(), 0, libc::MNT_NOWAIT);
        if n <= 0 {
            return out;
        }
        let mut buf: Vec<libc::statfs> = vec![std::mem::zeroed(); n as usize];
        let n = libc::getfsstat(buf.as_mut_ptr(), (buf.len() * std::mem::size_of::<libc::statfs>()) as i32, libc::MNT_NOWAIT);
        for fs in buf.iter().take(n.max(0) as usize) {
            if std::ffi::CStr::from_ptr(fs.f_fstypename.as_ptr()).to_bytes() == b"autofs" {
                let m = std::ffi::CStr::from_ptr(fs.f_mntonname.as_ptr()).to_string_lossy().into_owned();
                if let Some(stripped) = m.strip_prefix("/System/Volumes/Data").filter(|s| !s.is_empty()) {
                    out.push(stripped.to_string());
                }
                out.push(m);
            }
        }
    }
    out
}

#[cfg(not(target_os = "macos"))]
fn autofs_mount_points() -> Vec<String> {
    std::fs::read_to_string("/proc/mounts")
        .map(|t| {
            t.lines()
                .filter_map(|l| {
                    let f: Vec<&str> = l.split_whitespace().collect();
                    (f.len() > 2 && f[2] == "autofs").then(|| f[1].to_string())
                })
                .collect()
        })
        .unwrap_or_default()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn posix_paths() {
        assert_eq!(normpath("/a/./b/../c//d/"), "/a/c/d");
        assert_eq!(normpath("//a/b"), "//a/b");
        assert_eq!(normpath("///a"), "/a");
        assert_eq!(normpath("a/../../b"), "../b");
        assert_eq!(normpath("/.."), "/");
        assert_eq!(dirname("/a/b/"), "/a/b");
        assert_eq!(dirname("/a"), "/");
        assert_eq!(dirname("a"), "");
        assert_eq!(basename("/a/b"), "b");
        assert_eq!(join("/a", "b"), "/a/b");
        assert_eq!(join("/a/", "b"), "/a/b");
        assert_eq!(char_prefix("héllo", 2), "hé");
        assert_eq!(strip("\u{1c} x \u{a0}"), "x");
    }
}
