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

fn repo(root: &std::path::Path, name: &str) -> String {
    let dir = root.join(name);
    std::fs::create_dir_all(dir.join(".git")).unwrap();
    std::fs::write(dir.join(".git/config"), format!("[remote \"origin\"]\n\turl = git@github.com:ramp/{name}.git\n")).unwrap();
    dir.to_str().unwrap().to_string()
}

#[test]
fn codex_workspace_root_does_not_include_a_sibling_with_the_same_prefix() {
    let tmp = std::env::temp_dir().join(format!("rrh-codex-workspace-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&tmp);
    let root = repo(&tmp, "repo");
    let sibling = repo(&tmp, "repo-old");
    let body = serde_json::json!({
        "model": "m",
        "input": [{
            "type": "message",
            "role": "user",
            "content": [{
                "type": "input_text",
                "text": format!("<environment_context>\n<cwd>{sibling}</cwd>\n</environment_context>")
            }]
        }]
    });
    let metadata = serde_json::json!({
        "thread_id": "thread",
        "turn_id": "turn",
        "workspaces": { root.clone(): {} }
    })
    .to_string();
    let resolver = RepoResolver::default();
    let sessions = SessionMemory::default();
    let mut ctx = T { metadata, resolver: &resolver, sessions: &sessions };

    let result = attribute_bytes(&serde_json::to_vec(&body).unwrap(), &mut ctx, &ConvCache::default(), false).unwrap();

    assert_eq!(result.attribution.repository.as_deref(), Some("ramp/repo"));
    let _ = std::fs::remove_dir_all(&tmp);
}
