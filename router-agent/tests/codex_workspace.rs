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

#[test]
fn codex_child_inherits_exact_parent_turn_repository() {
    let tmp = std::env::temp_dir().join(format!("rrh-codex-parent-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&tmp);
    let root = repo(&tmp, "parent-repo");
    let resolver = RepoResolver::default();
    let sessions = SessionMemory::default();
    let parent = resolver.resolve(&root).unwrap();
    sessions.put("thread:parent-thread", parent.clone(), root.clone());
    sessions.put("turn:parent-turn", parent, root.clone());
    let body = serde_json::json!({"model":"m","input":[{"type":"message","role":"user","content":[{"type":"input_text","text":"review"}]}]});
    let metadata = serde_json::json!({"session_id":"child-session","thread_id":"child-thread","parent_thread_id":"parent-thread","parent_turn_id":"parent-turn","turn_id":"child-turn"}).to_string();
    let mut ctx = T { metadata, resolver: &resolver, sessions: &sessions };
    let result = attribute_bytes(&serde_json::to_vec(&body).unwrap(), &mut ctx, &ConvCache::default(), false).unwrap();
    assert_eq!(result.attribution.repository.as_deref(), Some("ramp/parent-repo"));
    assert_eq!(result.attribution.repository_source.as_deref(), Some("parent_turn_memory"));
    let _ = std::fs::remove_dir_all(&tmp);
}

fn attribute_child(resolver: &RepoResolver, sessions: &SessionMemory, metadata: serde_json::Value) -> Attribution {
    let body = serde_json::json!({"model":"m","input":[{"type":"message","role":"user","content":[{"type":"input_text","text":"review"}]}]});
    let mut ctx = T { metadata: metadata.to_string(), resolver, sessions };
    attribute_bytes(&serde_json::to_vec(&body).unwrap(), &mut ctx, &ConvCache::default(), false).unwrap().attribution
}

#[test]
fn codex_child_finds_parent_thread_whose_session_id_differs() {
    let tmp = std::env::temp_dir().join(format!("rrh-codex-parent-thread-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&tmp);
    let root = repo(&tmp, "parent-repo");
    let resolver = RepoResolver::default();
    let sessions = SessionMemory::default();
    let parent_body = serde_json::json!({"model":"m","input":[{"type":"message","role":"user","content":[{"type":"input_text","text":"go"}]}]});
    let parent_metadata = serde_json::json!({"session_id":"root-session","thread_id":"parent-thread","turn_id":"parent-turn","workspaces":{root.clone():{}}}).to_string();
    let mut ctx = T { metadata: parent_metadata, resolver: &resolver, sessions: &sessions };
    let parent = attribute_bytes(&serde_json::to_vec(&parent_body).unwrap(), &mut ctx, &ConvCache::default(), false).unwrap().attribution;
    assert_eq!(parent.repository.as_deref(), Some("ramp/parent-repo"));

    let child = attribute_child(&resolver, &sessions, serde_json::json!({"session_id":"child-session","thread_id":"child-thread","parent_thread_id":"parent-thread","turn_id":"child-turn"}));
    assert_eq!(child.repository.as_deref(), Some("ramp/parent-repo"));
    assert_eq!(child.repository_source.as_deref(), Some("parent_session_memory"));
    let _ = std::fs::remove_dir_all(&tmp);
}

#[test]
fn nested_codex_child_inherits_through_intermediate_child() {
    let tmp = std::env::temp_dir().join(format!("rrh-codex-nested-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&tmp);
    let root = repo(&tmp, "parent-repo");
    let resolver = RepoResolver::default();
    let sessions = SessionMemory::default();
    sessions.put("turn:parent-turn", resolver.resolve(&root).unwrap(), root.clone());

    let child = attribute_child(&resolver, &sessions, serde_json::json!({"session_id":"child-session","thread_id":"child-thread","parent_thread_id":"parent-thread","parent_turn_id":"parent-turn","turn_id":"child-turn"}));
    assert_eq!(child.repository.as_deref(), Some("ramp/parent-repo"));
    let by_turn = attribute_child(&resolver, &sessions, serde_json::json!({"session_id":"g1","thread_id":"g1","parent_thread_id":"child-thread","parent_turn_id":"child-turn","turn_id":"g1-turn"}));
    assert_eq!(by_turn.repository.as_deref(), Some("ramp/parent-repo"));
    assert_eq!(by_turn.repository_source.as_deref(), Some("parent_turn_memory"));
    let by_thread = attribute_child(&resolver, &sessions, serde_json::json!({"session_id":"g2","thread_id":"g2","parent_thread_id":"child-thread","turn_id":"g2-turn"}));
    assert_eq!(by_thread.repository.as_deref(), Some("ramp/parent-repo"));
    assert_eq!(by_thread.repository_source.as_deref(), Some("parent_session_memory"));
    let _ = std::fs::remove_dir_all(&tmp);
}

#[test]
fn reused_codex_child_prefers_current_parent_turn_over_its_session_memory() {
    let tmp = std::env::temp_dir().join(format!("rrh-codex-reused-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&tmp);
    let old = repo(&tmp, "old-repo");
    let new = repo(&tmp, "new-repo");
    let resolver = RepoResolver::default();
    let sessions = SessionMemory::default();
    sessions.put("cx:child-session", resolver.resolve(&old).unwrap(), old.clone());
    sessions.put("turn:parent-turn-2", resolver.resolve(&new).unwrap(), new.clone());

    let child = attribute_child(&resolver, &sessions, serde_json::json!({"session_id":"child-session","thread_id":"child-thread","parent_thread_id":"parent-thread","parent_turn_id":"parent-turn-2","turn_id":"child-turn-2"}));
    assert_eq!(child.repository.as_deref(), Some("ramp/new-repo"));
    assert_eq!(child.repository_source.as_deref(), Some("parent_turn_memory"));
    let _ = std::fs::remove_dir_all(&tmp);
}
