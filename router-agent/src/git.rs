//! Directory -> repository: walk up to `.git` (directory, or a worktree/submodule `gitdir:` file),
//! read the common dir's remote, normalize it to `owner/repo`.

use crate::paths;
use regex::Regex;
use std::collections::HashMap;
use std::process::{Command, Stdio};
use std::sync::{LazyLock, Mutex};
use std::time::{Duration, Instant};

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Resolved {
    pub slug: String,
    pub host: Option<String>,
    /// "git_remote" | "git_no_remote"
    pub source: &'static str,
}

static REMOTE_PATTERNS: LazyLock<[Regex; 3]> = LazyLock::new(|| {
    [
        Regex::new(r"^git@(?P<host>[^:/]+):(?P<path>.+?)(?:\.git)?/?$").unwrap(),
        Regex::new(r"^https?://(?:[^@/]+@)?(?P<host>[^/]+)/(?P<path>.+?)(?:\.git)?/?$").unwrap(),
        Regex::new(r"^ssh://(?:[^@/]+@)?(?P<host>[^/]+)/(?P<path>.+?)(?:\.git)?/?$").unwrap(),
    ]
});

/// (host, owner/repo) for a Git remote URL.
pub fn normalize_remote(url: &str) -> Option<(String, String)> {
    let url = paths::strip(url);
    for pat in REMOTE_PATTERNS.iter() {
        if let Some(m) = pat.captures(url) {
            let path = paths::strip_chars(&m["path"], "/");
            let path = path.strip_suffix(".git").unwrap_or(path);
            if !path.contains('/') {
                return None;
            }
            return Some((m["host"].to_lowercase(), path.to_lowercase()));
        }
    }
    None
}

/// File contents; None if unreadable or not UTF-8 (a garbled remote must not become a slug).
fn read_text(path: &str) -> Option<String> {
    String::from_utf8(std::fs::read(path).ok()?).ok()
}

/// (worktree_root, git_dir) for the checkout containing `start`.
pub fn find_git_dir(start: &str) -> Option<(String, String)> {
    let mut current = paths::abspath(start);
    loop {
        let dotgit = paths::join(&current, ".git");
        if paths::isdir(&dotgit) {
            return Some((current, dotgit));
        }
        if paths::isfile(&dotgit) {
            let content = read_text(&dotgit)?;
            if let Some(rest) = paths::strip(&content).strip_prefix("gitdir:") {
                let mut git_dir = paths::strip(rest).to_string();
                if !git_dir.starts_with('/') {
                    git_dir = paths::normpath(&paths::join(&current, &git_dir));
                }
                return Some((current, git_dir));
            }
        }
        let parent = paths::dirname(&current);
        if parent == current {
            return None;
        }
        current = parent;
    }
}

fn common_git_dir(git_dir: &str) -> String {
    match read_text(&paths::join(git_dir, "commondir")) {
        Some(s) => paths::normpath(&paths::join(git_dir, paths::strip(&s))),
        None => git_dir.to_string(),
    }
}

/// Git config values concatenate quoted/unquoted segments, decode escapes, and ignore unquoted
/// comments and trailing whitespace. Unsupported/invalid values are left to Git itself.
fn config_value(text: &str) -> Option<String> {
    let mut value = String::new();
    let mut quoted = false;
    let mut end = 0;
    let mut chars = text.trim_start().chars();
    while let Some(c) = chars.next() {
        match c {
            '"' => quoted = !quoted,
            '#' | ';' if !quoted => break,
            '\\' => {
                value.push(match chars.next()? {
                    'n' => '\n', 't' => '\t', 'b' => '\x08', '\\' => '\\', '"' => '"',
                    _ => return None,
                });
                end = value.len();
            }
            _ => {
                value.push(c);
                if quoted || !c.is_whitespace() {
                    end = value.len();
                }
            }
        }
    }
    if quoted { return None; }
    value.truncate(end);
    Some(value)
}

/// `url` of `[remote "origin"]`, else of the first remote that has one, and whether the config
/// includes other files (where a remote could still be defined). Unreadable config: (None, true).
fn remote_from_config(git_dir: &str) -> (Option<String>, bool) {
    let Some(text) = read_text(&paths::join(&common_git_dir(git_dir), "config")) else { return (None, true) };
    let (mut section, mut includes) = (String::new(), false);
    let mut urls: Vec<(String, String)> = Vec::new();
    for line in text.lines().map(paths::strip) {
        if line.starts_with(['#', ';']) {
            continue;
        }
        if let Some((name, _)) = line.strip_prefix('[').and_then(|l| l.rsplit_once(']')) {
            section = name.to_string();
            includes |= section.to_ascii_lowercase().starts_with("include");
            continue;
        }
        if !section.starts_with("remote \"") {
            continue;
        }
        if let Some((key, value)) = line.split_once(['=', ':']) {
            if paths::strip(key).eq_ignore_ascii_case("url") {
                // Git treats URLs as a list: get-url (without --all) returns the first.
                if urls.iter().any(|(s, _)| *s == section) {
                    continue;
                }
                let Some(value) = config_value(value) else {
                    return (None, true);
                };
                urls.push((section.clone(), value));
            }
        }
    }
    let url = urls.iter().find(|(s, _)| s == "remote \"origin\"").or(urls.first()).map(|(_, u)| u.clone());
    (url, includes)
}

/// `git -C root remote get-url origin`, two-second limit.
fn git_remote_get_url(root: &str) -> Option<String> {
    let mut child = Command::new("git")
        .args(["-C", root, "remote", "get-url", "origin"])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .ok()?;
    let deadline = Instant::now() + Duration::from_secs(2);
    loop {
        match child.try_wait() {
            Ok(Some(status)) => {
                if !status.success() {
                    return None;
                }
                let mut out = String::new();
                use std::io::Read;
                child.stdout.take()?.read_to_string(&mut out).ok()?;
                let s = paths::strip(&out);
                return (!s.is_empty()).then(|| s.to_string());
            }
            Ok(None) if Instant::now() < deadline => std::thread::sleep(Duration::from_millis(2)),
            _ => {
                let _ = child.kill();
                let _ = child.wait();
                return None;
            }
        }
    }
}

fn resolve_root(root: &str, git_dir: &str) -> Resolved {
    let (mut url, includes) = remote_from_config(git_dir);
    // git itself (a ~45 ms process spawn on macOS) only when the config could define a remote
    // somewhere this reader does not look
    if includes {
        url = git_remote_get_url(root).or(url);
    }
    if let Some((host, slug)) = url.as_deref().and_then(normalize_remote) {
        return Resolved { slug, host: Some(host), source: "git_remote" };
    }
    Resolved { slug: format!("local/{}", paths::basename(root).to_lowercase()), host: None, source: "git_no_remote" }
}

type Cache = Mutex<HashMap<String, (Instant, Option<Resolved>)>>;

/// Directory -> repository, cached for ten minutes (negative results too), and repository root ->
/// result so each new directory of a known repository costs only the walk up to its `.git`.
#[derive(Default)]
pub struct RepoResolver {
    dirs: Cache,
    roots: Cache,
}

const TTL: Duration = Duration::from_secs(600);

fn cached(cache: &Cache, key: &str, limit: usize, compute: impl FnOnce() -> Option<Resolved>) -> Option<Resolved> {
    if let Some((t, r)) = cache.lock().unwrap().get(key) {
        if t.elapsed() < TTL {
            return r.clone();
        }
    }
    let r = compute();
    let mut c = cache.lock().unwrap();
    if c.len() > limit {
        c.clear();
    }
    c.insert(key.to_string(), (Instant::now(), r.clone()));
    r
}

impl RepoResolver {
    pub fn resolve(&self, directory: &str) -> Option<Resolved> {
        cached(&self.dirs, directory, 50_000, || {
            if !paths::isdir(directory) {
                return None;
            }
            let (root, git_dir) = find_git_dir(directory)?;
            cached(&self.roots, &format!("{root}\0{git_dir}"), 10_000, || Some(resolve_root(&root, &git_dir)))
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn included_origin_precedes_direct_fallback() {
        let dir = std::env::temp_dir().join(format!("rrh-include-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        assert!(Command::new("git").args(["init", "--quiet"]).arg(&dir).status().unwrap().success());
        let git = dir.join(".git");
        std::fs::write(git.join("config"), "[remote \"upstream\"]\nurl = https://github.com/o/upstream\n[include]\npath = included\n").unwrap();
        std::fs::write(git.join("included"), "[remote \"origin\"]\nurl = https://github.com/o/origin\n").unwrap();
        assert_eq!(resolve_root(dir.to_str().unwrap(), git.to_str().unwrap()).slug, "o/origin");
        std::fs::write(git.join("included"), "[core]\nbare = false\n").unwrap();
        assert_eq!(resolve_root(dir.to_str().unwrap(), git.to_str().unwrap()).slug, "o/upstream");
        std::fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn remotes() {
        let cases = [
            ("git@github.com:Ramp/Router.git", Some(("github.com", "ramp/router"))),
            ("https://user@github.com/ramp/x/", Some(("github.com", "ramp/x"))),
            ("ssh://git@gitlab.com/a/b/c.git", Some(("gitlab.com", "a/b/c"))),
            ("ssh://git@gitlab.com:2222/a/b/c.git", Some(("gitlab.com:2222", "a/b/c"))),
            ("ssh://user@gitlab.com:2222/a/b.git", Some(("gitlab.com:2222", "a/b"))),
            ("https://github.com/solo", None),
            ("file:///tmp/x", None),
        ];
        for (u, want) in cases {
            assert_eq!(normalize_remote(u), want.map(|(h, s)| (h.to_string(), s.to_string())), "{u}");
        }
    }

    #[test]
    fn config_remotes() {
        let dir = std::env::temp_dir().join(format!("rrh-cfg-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let g = dir.to_str().unwrap();
        let write = |s: &str| std::fs::write(dir.join("config"), s).unwrap();
        write("[core]\n\tbare\n[remote \"upstream\"]\n\turl = git@github.com:o/u.git\n[remote \"origin\"]\n\tURL = git@github.com:o/r.git \n\tfetch = +refs/heads/*:refs/remotes/origin/*\n");
        assert_eq!(remote_from_config(g), (Some("git@github.com:o/r.git".into()), false));
        write("# c\n[remote \"fork\"]\n\turl = https://github.com/o/f\n[include]\n\tpath = x\n");
        assert_eq!(remote_from_config(g), (Some("https://github.com/o/f".into()), true));
        for config in [
            "[remote \"origin\"]\nurl = git@github.com:o/main.git\nurl = git@github.com:o/mirror.git\n",
            "[remote \"origin\"]\nurl = git@github.com:o/main.git\n[remote \"origin\"]\nurl = git@github.com:o/mirror.git\n",
            "[remote \"upstream\"]\nurl = git@github.com:o/main.git\nurl = git@github.com:o/mirror.git\n",
        ] {
            write(config);
            assert_eq!(remote_from_config(g), (Some("git@github.com:o/main.git".into()), false));
        }
        for value in [
            "\"git@github.com:o/repo.git\"",
            "git@github.com:o/repo.git # inline comment",
            "git@github.com:o/repo.git; inline comment",
            "\"git@github.com:\"o/repo.git # comment",
        ] {
            write(&format!("[remote \"origin\"]\n url = {value}\n"));
            let (url, includes) = remote_from_config(g);
            assert!(!includes);
            assert_eq!(url.as_deref().and_then(normalize_remote), Some(("github.com".into(), "o/repo".into())));
        }
        assert_eq!(config_value(r#" "a\t\"b\\c#d; " # ignored"#), Some("a\t\"b\\c#d; ".into()));
        assert_eq!(config_value("\"unfinished"), None);
        assert_eq!(config_value(r"bad\escape"), None);
        std::fs::remove_dir_all(&dir).unwrap();
    }
}
