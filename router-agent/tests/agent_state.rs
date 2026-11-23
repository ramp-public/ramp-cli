//! A turn that reads a coding agent's own state, such as Codex memories, keeps the repository
//! of its project work. Without this, the newest weak read names the memory repository.
use rrh::attr::{attribute_bytes, Attribution, ConvCache, Ctx, SessionMemory};
use rrh::git::{RepoResolver, Resolved};

struct T<'a> {
    metadata: String,
    resolver: &'a RepoResolver,
    sessions: &'a SessionMemory,
}

impl Ctx for T<'_> {
    fn header(&self, name: &str) -> Option<String> {
        name.eq_ignore_ascii_case("x-codex-turn-metadata").then(|| self.metadata.clone())
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

fn repo(dir: &std::path::Path, remote: Option<&str>) {
    std::fs::create_dir_all(dir.join(".git")).unwrap();
    std::fs::create_dir_all(dir.join("lib")).unwrap();
    let config = remote.map_or(String::new(), |r| format!("[remote \"origin\"]\n\turl = {r}\n"));
    std::fs::write(dir.join(".git/config"), config).unwrap();
    std::fs::write(dir.join("lib/notes.md"), "x\n").unwrap();
}

fn read(call: &str, path: &str) -> [serde_json::Value; 2] {
    let args = serde_json::json!({ "cmd": format!("cat {path}") }).to_string();
    [
        serde_json::json!({ "type": "function_call", "name": "exec_command", "call_id": call, "arguments": args }),
        serde_json::json!({ "type": "function_call_output", "call_id": call, "output": "x" }),
    ]
}

#[test]
fn codex_memory_reads_do_not_replace_the_project_repository() {
    let tmp = std::env::temp_dir().join(format!("rrh-agent-state-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&tmp);
    let code = tmp.join("code");
    let project = code.join("project");
    let state = tmp.join("home/.codex");
    let memories = state.join("memories");
    repo(&project, Some("git@github.com:ramp/project.git"));
    repo(&memories, None);
    // Separated like PATH, so a Windows drive path's colon is not a separator.
    std::env::set_var("RRH_AGENT_STATE_DIRS", std::env::join_paths([state.as_path(), tmp.join("other").as_path()]).unwrap());
    let [project_call, project_output] = read("c1", project.join("lib/notes.md").to_str().unwrap());
    let [memory_call, memory_output] = read("c2", memories.join("lib/notes.md").to_str().unwrap());
    let body = serde_json::json!({
        "model": "m",
        "input": [
            { "type": "message", "role": "user", "content": [{ "type": "input_text",
              "text": format!("<environment_context>\n<cwd>{}</cwd>\n</environment_context>", code.display()) }] },
            { "type": "message", "role": "user", "content": [{ "type": "input_text", "text": "fix project" }] },
            project_call, project_output, memory_call, memory_output
        ]
    });
    let metadata = serde_json::json!({ "thread_id": "thread", "turn_id": "turn" }).to_string();
    let resolver = RepoResolver::default();
    let sessions = SessionMemory::default();
    let mut ctx = T { metadata, resolver: &resolver, sessions: &sessions };

    let result = attribute_bytes(&serde_json::to_vec(&body).unwrap(), &mut ctx, &ConvCache::default(), false).unwrap();

    assert_eq!(result.attribution.repository.as_deref(), Some("ramp/project"));
    let _ = std::fs::remove_dir_all(&tmp);
}
