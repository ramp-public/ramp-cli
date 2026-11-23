//! Windows harnesses state and edit paths as `C:\dir` (Claude Code and Codex), `C:/dir`, or Git
//! Bash's `/c/dir`; attribution resolves each to the repository on disk.
#![cfg(windows)]

use rrh::attr::{attribute_bytes, Attribution, ConvCache, Ctx, SessionMemory};
use rrh::git::{RepoResolver, Resolved};

struct T<'a> {
    resolver: &'a RepoResolver,
    sessions: &'a SessionMemory,
    /// x-codex-turn-metadata, which Codex sends with every request.
    codex: Option<String>,
}

impl Ctx for T<'_> {
    fn header(&self, name: &str) -> Option<String> {
        self.codex.clone().filter(|_| name.eq_ignore_ascii_case("x-codex-turn-metadata"))
    }
    fn calling_process(&self, _a: &mut Attribution) -> Option<String> {
        None
    }
    fn resolver(&self) -> &RepoResolver {
        self.resolver
    }
    fn session_get(&self, id: &str) -> Option<(Resolved, String)> {
        self.sessions.get(id)
    }
    fn session_put(&self, id: &str, value: (Resolved, String)) {
        self.sessions.put(id, value.0, value.1)
    }
}

/// A repository with an origin remote; returns its native path (`C:\...`).
fn repo(root: &std::path::Path, name: &str) -> String {
    let dir = root.join(name);
    std::fs::create_dir_all(dir.join(".git")).unwrap();
    std::fs::create_dir_all(dir.join("src")).unwrap();
    std::fs::write(dir.join(".git").join("config"), format!("[remote \"origin\"]\n\turl = https://github.com/ramp/{name}.git\n")).unwrap();
    std::fs::write(dir.join("src").join("main.rs"), "fn main() {}\n").unwrap();
    dir.to_str().unwrap().to_string()
}

fn root(test: &str) -> std::path::PathBuf {
    let tmp = std::env::temp_dir().join(format!("rrh-windows-paths-{test}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&tmp);
    std::fs::create_dir_all(&tmp).unwrap();
    tmp
}

/// A Claude Code Messages request stating `cwd`, then one tool call.
fn claude(cwd: &str, tool: &str, input: serde_json::Value) -> Vec<u8> {
    serde_json::to_vec(&serde_json::json!({
        "model": "m",
        "system": [{ "type": "text", "text": format!("<env>\nWorking directory: {cwd}\nIs directory a git repo: Yes\nPlatform: win32\n</env>") }],
        "messages": [
            { "role": "user", "content": "go" },
            { "role": "assistant", "content": [{ "type": "tool_use", "id": "t1", "name": tool, "input": input }] },
            { "role": "user", "content": [{ "type": "tool_result", "tool_use_id": "t1", "content": "ok" }] }
        ]
    }))
    .unwrap()
}

fn attribute(body: &[u8]) -> Attribution {
    attribute_with(body, None)
}

fn attribute_with(body: &[u8], codex: Option<String>) -> Attribution {
    let (resolver, sessions) = (RepoResolver::default(), SessionMemory::default());
    let mut ctx = T { resolver: &resolver, sessions: &sessions, codex };
    attribute_bytes(body, &mut ctx, &ConvCache::default(), false).unwrap().attribution
}

#[test]
fn a_stated_drive_path_is_the_repository() {
    let tmp = root("stated");
    let alpha = repo(&tmp, "alpha");
    let a = attribute(&claude(&alpha, "Read", serde_json::json!({ "file_path": format!("{alpha}\\src\\main.rs") })));
    assert_eq!(a.repository.as_deref(), Some("ramp/alpha"));
    assert_eq!(a.repository_host.as_deref(), Some("github.com"));
    assert_eq!(a.directory_basename.as_deref(), Some("alpha"));
    std::fs::remove_dir_all(&tmp).unwrap();
}

#[test]
fn an_edit_with_backslashes_moves_the_tag() {
    let tmp = root("edit");
    let alpha = repo(&tmp, "alpha");
    let beta = repo(&tmp, "beta");
    let a = attribute(&claude(&alpha, "Edit", serde_json::json!({ "file_path": format!("{beta}\\src\\main.rs"), "old_string": "a", "new_string": "b" })));
    assert_eq!(a.repository.as_deref(), Some("ramp/beta"));
    // Forward slashes, as some tools write them.
    let a = attribute(&claude(&alpha, "Write", serde_json::json!({ "file_path": format!("{}/src/main.rs", beta.replace('\\', "/")), "content": "x" })));
    assert_eq!(a.repository.as_deref(), Some("ramp/beta"));
    std::fs::remove_dir_all(&tmp).unwrap();
}

#[test]
fn a_git_bash_cd_moves_the_tag() {
    let tmp = root("bash");
    let alpha = repo(&tmp, "alpha");
    let beta = repo(&tmp, "beta");
    // C:\Users\x\beta as Git Bash writes it: /c/Users/x/beta
    let drive = beta[..1].to_ascii_lowercase();
    let bash = format!("/{drive}{}", beta[2..].replace('\\', "/"));
    let a = attribute(&claude(&alpha, "Bash", serde_json::json!({ "command": format!("cd {bash} && cargo build") })));
    assert_eq!(a.repository.as_deref(), Some("ramp/beta"));
    std::fs::remove_dir_all(&tmp).unwrap();
}

#[test]
fn a_powershell_cd_with_backslashes_moves_the_tag() {
    // Codex and Cursor run PowerShell on Windows, where `\` is a plain separator.
    let tmp = root("powershell");
    let alpha = repo(&tmp, "alpha");
    let beta = repo(&tmp, "beta");
    let a = attribute(&claude(&alpha, "Bash", serde_json::json!({ "command": format!("cd {beta}; cargo build") })));
    assert_eq!(a.repository.as_deref(), Some("ramp/beta"));
    let a = attribute(&claude(&alpha, "Bash", serde_json::json!({ "command": format!("git -C {beta}\\src status") })));
    assert_eq!(a.repository.as_deref(), Some("ramp/beta"));
    std::fs::remove_dir_all(&tmp).unwrap();
}

#[test]
fn the_resolver_takes_native_paths() {
    let tmp = root("resolver");
    let alpha = repo(&tmp, "alpha");
    let resolver = RepoResolver::default();
    assert_eq!(resolver.resolve(&alpha).map(|r| r.slug).as_deref(), Some("ramp/alpha"));
    assert_eq!(resolver.resolve(&format!("{alpha}\\src")).map(|r| r.slug).as_deref(), Some("ramp/alpha"));
    std::fs::remove_dir_all(&tmp).unwrap();
}

#[test]
fn a_codex_workdir_with_backslashes_is_used() {
    let tmp = root("codex");
    let alpha = repo(&tmp, "alpha");
    let body = serde_json::to_vec(&serde_json::json!({
        "model": "m",
        "input": [
            { "type": "message", "role": "user", "content": [{ "type": "input_text",
              "text": format!("<environment_context>\n<cwd>{alpha}</cwd>\n<shell>powershell</shell>\n</environment_context>") }] },
            { "type": "message", "role": "user", "content": [{ "type": "input_text", "text": "build" }] }
        ]
    }))
    .unwrap();
    let metadata = serde_json::json!({ "thread_id": "thread", "turn_id": "turn" }).to_string();
    assert_eq!(attribute_with(&body, Some(metadata)).repository.as_deref(), Some("ramp/alpha"));
    std::fs::remove_dir_all(&tmp).unwrap();
}
