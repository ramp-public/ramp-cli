//! A Codex turn that starts in one repository and writes another through the shell is attributed
//! to the repository it wrote. Codex runs `apply_patch` through `exec_command`, so these writes
//! must count as edits, not as weak path mentions.
use rrh::attr::{attribute_bytes, Attribution, ConvCache, Ctx, SessionMemory};
use rrh::git::{RepoResolver, Resolved};

struct T<'a> {
    metadata: String,
    resolver: &'a RepoResolver,
    sessions: &'a SessionMemory,
}

impl Ctx for T<'_> {
    fn header(&self, name: &str) -> Option<String> {
        name.eq_ignore_ascii_case("x-codex-turn-metadata")
            .then(|| self.metadata.clone())
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

fn repo(root: &std::path::Path, name: &str) -> String {
    let dir = root.join(name);
    std::fs::create_dir_all(dir.join(".git")).unwrap();
    std::fs::create_dir_all(dir.join("lib")).unwrap();
    std::fs::write(
        dir.join(".git/config"),
        format!("[remote \"origin\"]\n\turl = git@github.com:ramp/{name}.git\n"),
    )
    .unwrap();
    std::fs::write(dir.join("lib/b.py"), "x = 1\n").unwrap();
    dir.to_str().unwrap().to_string()
}

/// A Codex Responses request: started in `alpha`, then one `exec_command` call with `cmd`.
fn codex_body(alpha: &str, cmd: &str) -> Vec<u8> {
    let args = serde_json::json!({ "cmd": cmd, "workdir": alpha }).to_string();
    serde_json::to_vec(&serde_json::json!({
        "model": "m",
        "input": [
            { "type": "message", "role": "user", "content": [{ "type": "input_text",
              "text": format!("<environment_context>\n<cwd>{alpha}</cwd>\n</environment_context>") }] },
            { "type": "message", "role": "user", "content": [{ "type": "input_text", "text": "edit beta" }] },
            { "type": "function_call", "name": "exec_command", "call_id": "c1", "arguments": args },
            { "type": "function_call_output", "call_id": "c1", "output": "Success." }
        ]
    }))
    .unwrap()
}

fn attributed(cmd_for: impl Fn(&str) -> String) -> (Option<String>, Option<String>) {
    let tmp = std::env::temp_dir().join(format!(
        "rrh-shell-writes-{}-{}",
        std::process::id(),
        rand_suffix()
    ));
    let _ = std::fs::remove_dir_all(&tmp);
    let alpha = repo(&tmp, "alpha");
    let beta = repo(&tmp, "beta");
    let resolver = RepoResolver::default();
    let sessions = SessionMemory::default();
    // Codex states its working directory in turn metadata; with it, the stated repository is known,
    // so weak path mentions cannot move the tag. This is the case that failed in practice.
    let metadata = serde_json::json!({ "thread_id": "thread", "turn_id": "turn", "workspaces": { alpha.clone(): {} } }).to_string();
    let mut ctx = T {
        metadata,
        resolver: &resolver,
        sessions: &sessions,
    };
    let body = codex_body(&alpha, &cmd_for(&beta));
    let a = attribute_bytes(&body, &mut ctx, &ConvCache::default(), false)
        .unwrap()
        .attribution;
    let _ = std::fs::remove_dir_all(&tmp);
    (a.repository, a.repository_source)
}

fn rand_suffix() -> u128 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_nanos()
}

#[test]
fn codex_apply_patch_through_exec_command_moves_to_the_patched_repository() {
    let (repo, _) = attributed(|beta| {
        format!("apply_patch <<'PATCH'\n*** Begin Patch\n*** Update File: {beta}/lib/b.py\n@@\n+y = 2\n*** End Patch\nPATCH")
    });
    assert_eq!(repo.as_deref(), Some("ramp/beta"));
}

#[test]
fn non_apply_patch_shell_writes_stay_weak() {
    for make in [
        (|beta: &str| format!("printf '%s\\n' '# touched' >> {beta}/lib/b.py"))
            as fn(&str) -> String,
        |beta: &str| format!("echo x | tee {beta}/lib/b.py"),
        |beta: &str| format!("sed -i '' 's/x/y/' {beta}/lib/b.py"),
    ] {
        let (repo, _) = attributed(make);
        assert_eq!(repo.as_deref(), Some("ramp/alpha"), "{}", make("<beta>"));
    }
}

#[test]
fn reading_another_repository_from_a_repository_still_stays_put() {
    let (repo, _) = attributed(|beta| format!("cat {beta}/lib/b.py"));
    assert_eq!(repo.as_deref(), Some("ramp/alpha"));
}
