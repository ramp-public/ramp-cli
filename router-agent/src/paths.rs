//! Path helpers (POSIX normalization, `~` expansion) and cached `stat` that never touches autofs.
//!
//! Attribution works on POSIX paths. On Windows, absolute paths enter it through `canon` as
//! `/C:/dir/file` (so every "absolute path starts with /" rule holds) and leave it through `native`
//! for file system calls. Elsewhere both are the identity.

use std::borrow::Cow;
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
        let cwd = std::env::current_dir().map(|c| canon(&c.to_string_lossy()).into_owned()).unwrap_or_else(|_| "/".into());
        normpath(&join(&cwd, p))
    }
}

/// Identity: Unix paths are already in attribution's form.
#[cfg(not(windows))]
#[inline]
pub fn canon(p: &str) -> Cow<'_, str> {
    Cow::Borrowed(p)
}

/// A Windows absolute path in attribution's form, `/C:/dir/file`: `C:\dir`, `C:/dir`,
/// `\\?\C:\dir`, a file-URL path `/c:/dir`, and Git Bash's `/c/dir`. Anything else (relative
/// paths, UNC shares, which can hang on a dead server) is returned unchanged.
#[cfg(windows)]
pub fn canon(p: &str) -> Cow<'_, str> {
    let rest = p.strip_prefix(r"\\?\").unwrap_or(p);
    let b = rest.as_bytes();
    let drive = |i: usize| b.get(i).is_some_and(u8::is_ascii_alphabetic);
    let sep = |i: usize| matches!(b.get(i), Some(b'/' | b'\\'));
    let (letter, tail) = if drive(0) && b.get(1) == Some(&b':') && (sep(2) || b.len() == 2) {
        (b[0], &rest[2..])
    } else if b.first() == Some(&b'/') && drive(1) && b.get(2) == Some(&b':') && (sep(3) || b.len() == 3) {
        (b[1], &rest[3..])
    } else if b.first() == Some(&b'/') && drive(1) && (b.get(2) == Some(&b'/') || b.len() == 2) {
        (b[1], &rest[2..])
    } else {
        return Cow::Borrowed(p);
    };
    let tail = tail.replace('\\', "/");
    Cow::Owned(format!("/{}:{}", letter.to_ascii_uppercase() as char, if tail.is_empty() { "/" } else { &tail }))
}

/// Identity: attribution's paths are the file system's.
#[cfg(not(windows))]
#[inline]
pub fn native(p: &str) -> Cow<'_, str> {
    Cow::Borrowed(p)
}

/// `/C:/dir` back to `C:/dir` (Windows accepts `/` separators) for file system calls.
#[cfg(windows)]
pub fn native(p: &str) -> Cow<'_, str> {
    let b = p.as_bytes();
    if b.first() == Some(&b'/') && b.get(1).is_some_and(u8::is_ascii_alphabetic) && b.get(2) == Some(&b':') {
        return Cow::Owned(if b.len() == 3 { format!("{}/", &p[1..]) } else { p[1..].to_string() });
    }
    Cow::Borrowed(p)
}

pub fn expanduser(p: &str) -> String {
    if !p.starts_with('~') {
        return p.to_string();
    }
    let i = p.find('/').unwrap_or(p.len());
    let user = &p[1..i];
    let home = if user.is_empty() { home_var().or_else(|| home_of(None)) } else { home_of(Some(user)) };
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

/// The profile folder for `~`; other users' (`~name`) are not looked up.
#[cfg(windows)]
fn home_of(user: Option<&str>) -> Option<String> {
    if user.is_some() {
        return None;
    }
    std::env::var("USERPROFILE").ok().filter(|h| !h.is_empty()).map(|h| canon(&h).into_owned())
}

#[cfg(unix)]
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

/// `$HOME`. Windows sets no HOME for a scheduled task, and a HOME set by Git Bash or MSYS tools
/// is not the profile folder Windows programs use, so `~` is always the profile there.
fn home_var() -> Option<String> {
    if cfg!(windows) { None } else { std::env::var("HOME").ok() }
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
    let v = std::fs::metadata(&*native(p)).ok().and_then(|m| if m.is_dir() { Some(true) } else if m.is_file() { Some(false) } else { None });
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

/// Windows has no autofs.
#[cfg(windows)]
fn autofs_mount_points() -> Vec<String> {
    Vec::new()
}

#[cfg(all(unix, not(target_os = "macos")))]
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

    #[cfg(not(windows))]
    #[test]
    fn unix_paths_pass_through() {
        for p in ["/a/b", "C:\\x", "/c/x", "rel"] {
            assert!(matches!(canon(p), Cow::Borrowed(q) if q == p));
            assert!(matches!(native(p), Cow::Borrowed(q) if q == p));
        }
    }

    #[cfg(windows)]
    #[test]
    fn windows_paths() {
        assert_eq!(canon(r"C:\Users\me\repo"), "/C:/Users/me/repo");
        assert_eq!(canon("c:/Users/me/repo/"), "/C:/Users/me/repo/");
        assert_eq!(canon(r"\\?\D:\work"), "/D:/work");
        assert_eq!(canon("/c:/Users/me"), "/C:/Users/me");
        assert_eq!(canon("/c/Users/me"), "/C:/Users/me");
        assert_eq!(canon("C:"), "/C:/");
        assert_eq!(canon(r"C:\"), "/C:/");
        assert_eq!(canon("/c"), "/C:/");
        // not absolute drive paths: unchanged
        for p in ["C:rel", "src\\main.rs", r"\\server\share\x", "/usr/bin", "/cd/x", "rel/x"] {
            assert_eq!(canon(p), p);
        }
        assert_eq!(native("/C:/Users/me"), "C:/Users/me");
        assert_eq!(native("/C:"), "C:/");
        assert_eq!(native("/usr"), "/usr");
        assert_eq!(normpath(&canon(r"C:\a\..\b")), "/C:/b");
        assert_eq!(dirname("/C:/a"), "/C:");
        assert_eq!(dirname("/C:"), "/");
        let home = std::env::var("USERPROFILE").unwrap();
        assert_eq!(expanduser("~/x"), format!("{}/x", canon(&home).trim_end_matches('/')));
        assert!(isdir(&canon(&home)));
    }
}
