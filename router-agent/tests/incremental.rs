//! Incremental (per-conversation) attribution must equal a full parse of every request, and the
//! zero-copy upstream body must equal the spliced body.

use http_body_util::BodyExt;
use rrh::attr::{attribute_bytes, Attribution, ConvCache, Ctx, SessionMemory};
use rrh::git::{RepoResolver, Resolved};
use rrh::proxy::{attribute_body, Chunks};

struct T<'a> {
    headers: Vec<(String, String)>,
    resolver: &'a RepoResolver,
    sessions: &'a SessionMemory,
}

impl Ctx for T<'_> {
    fn header(&self, name: &str) -> Option<String> {
        self.headers.iter().find(|(k, _)| k.eq_ignore_ascii_case(name)).map(|(_, v)| v.clone())
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
    fn session_put(&self, id: &str, v: (Resolved, String)) {
        self.sessions.put(id, v.0, v.1)
    }
}

fn repo(root: &std::path::Path, name: &str) -> String {
    let d = root.join(name);
    std::fs::create_dir_all(d.join(".git")).unwrap();
    std::fs::write(d.join(".git/config"), format!("[remote \"origin\"]\n\turl = git@github.com:ramp/{name}.git\n")).unwrap();
    d.to_str().unwrap().to_string()
}

/// A Claude Code conversation that cds between repositories; `prefix_metadata` puts `metadata`
/// before `messages` (the layout the tail walk cannot see without parsing the prefix).
fn bodies(alpha: &str, beta: &str, prefix_metadata: bool) -> Vec<Vec<u8>> {
    let mut msgs: Vec<serde_json::Value> = vec![serde_json::json!({"role": "user", "content": "go"})];
    let mut out = Vec::new();
    let image = "A".repeat(200_000);
    for turn in 0..12 {
        let dir = if turn % 3 == 2 { beta } else { alpha };
        let id = format!("toolu_{turn:04}");
        msgs.push(serde_json::json!({"role": "assistant", "content": [
            {"type": "text", "text": format!("step {turn}")},
            {"type": "tool_use", "id": id, "name": "Bash", "input": {"command": format!("cd {dir} && git status")}}]}));
        let mut result = vec![serde_json::json!({"type": "tool_result", "tool_use_id": id, "content": "ok"})];
        if turn == 4 {
            result.push(serde_json::json!({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": image}}));
        }
        msgs.push(serde_json::json!({"role": "user", "content": result}));
        let system = format!("You are Claude Code.\n<env>\nWorking directory: {alpha}\n</env>");
        let tools = serde_json::json!([{"name": "Bash", "description": "Run a command", "input_schema": {"type": "object"}}]);
        let body = if prefix_metadata {
            serde_json::json!({"model": "m", "metadata": {"user_id": "u"}, "messages": msgs, "system": system, "tools": tools})
        } else {
            serde_json::json!({"model": "m", "messages": msgs, "system": system, "tools": tools, "metadata": {"user_id": "u"}})
        };
        out.push(serde_json::to_vec(&body).unwrap());
    }
    out
}

#[test]
fn incremental_equals_full() {
    let tmp = std::env::temp_dir().join(format!("rrh-incr-{}", std::process::id()));
    let alpha = repo(&tmp, "alpha");
    let beta = repo(&tmp, "beta");
    for prefix_metadata in [false, true] {
        let resolver = RepoResolver::default();
        let (s1, s2) = (SessionMemory::default(), SessionMemory::default());
        let cache = ConvCache::default();
        let headers = vec![("X-Claude-Code-Session-Id".to_string(), format!("s{prefix_metadata}"))];
        let mut incremental = 0;
        for (i, b) in bodies(&alpha, &beta, prefix_metadata).iter().enumerate() {
            let mut a = T { headers: headers.clone(), resolver: &resolver, sessions: &s1 };
            let r = attribute_bytes(b, &mut a, &cache, true).unwrap();
            assert_eq!(r.mismatch, None, "request {i}");
            let mut f = T { headers: headers.clone(), resolver: &resolver, sessions: &s2 };
            let full = attribute_bytes(b, &mut f, &ConvCache::default(), false).unwrap();
            assert_eq!(r.attribution, full.attribution, "request {i}");
            assert_eq!(r.plan, full.plan, "request {i}");
            let want = if i % 3 == 2 { "ramp/beta" } else { "ramp/alpha" };
            assert_eq!(r.attribution.repository.as_deref(), Some(want), "request {i}");
            // the spliced body is valid JSON with exactly one metadata member
            let spliced = rrh::json::apply_plan(b, r.plan.as_ref().unwrap());
            let v: serde_json::Value = serde_json::from_slice(&spliced).unwrap();
            assert_eq!(v["metadata"]["repository"], want);
            assert_eq!(v["metadata"]["user_id"], "u");
            assert_eq!(String::from_utf8_lossy(&spliced).matches("\"metadata\"").count(), 1);
            incremental += r.incremental as usize;
        }
        assert!(incremental >= 9, "only {incremental} incremental requests");
    }
    let _ = std::fs::remove_dir_all(&tmp);
}

#[test]
fn derived_turn_id_survives_incremental_tool_loops() {
    let tmp = std::env::temp_dir().join(format!("rrh-turn-{}", std::process::id()));
    let alpha = repo(&tmp, "alpha");
    let resolver = RepoResolver::default();
    let sessions = SessionMemory::default();
    let cache = ConvCache::default();
    let headers = vec![("X-Claude-Code-Session-Id".to_string(), "turn-session".to_string())];
    let mut messages = vec![serde_json::json!({"role": "user", "content": [
        {"type": "text", "text": "<system-reminder>ignore</system-reminder>"},
        {"type": "text", "text": "fix alpha"}
    ]})];
    let mut first_turn = None;
    for step in 0..3 {
        let id = format!("tool_{step}");
        messages.push(serde_json::json!({"role": "assistant", "content": [
            {"type": "tool_use", "id": id, "name": "Bash", "input": {"command": format!("cd {alpha} && git status")}}
        ]}));
        messages.push(serde_json::json!({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": id, "content": "ok"},
            {"type": "text", "text": "<system-reminder>tool output was truncated</system-reminder>"}
        ]}));
        let body = serde_json::to_vec(&serde_json::json!({"model": "m", "messages": messages})).unwrap();
        let mut ctx = T { headers: headers.clone(), resolver: &resolver, sessions: &sessions };
        let result = attribute_bytes(&body, &mut ctx, &cache, true).unwrap();
        assert_eq!(result.mismatch, None);
        assert_eq!(result.attribution.turn_id_source.as_deref(), Some("derived_user_message"));
        let turn = result.attribution.turn_id.clone().unwrap();
        assert_eq!(first_turn.get_or_insert(turn.clone()), &turn);
        let spliced = rrh::json::apply_plan(&body, result.plan.as_ref().unwrap());
        let value: serde_json::Value = serde_json::from_slice(&spliced).unwrap();
        assert_eq!(value["metadata"]["turn_id"], turn);
        assert_eq!(value["metadata"]["turn_id_source"], "derived_user_message");
    }
    messages.push(serde_json::json!({"role": "user", "content": "fix alpha"}));
    let body = serde_json::to_vec(&serde_json::json!({"model": "m", "messages": messages})).unwrap();
    let mut ctx = T { headers, resolver: &resolver, sessions: &sessions };
    let next = attribute_bytes(&body, &mut ctx, &cache, true).unwrap();
    assert_ne!(next.attribution.turn_id, first_turn, "the same prompt text later is a new turn");
    std::fs::remove_dir_all(tmp).unwrap();
}

#[test]
fn non_text_user_turns_have_distinct_ids_and_survive_tool_loops() {
    for (list, block) in [
        ("messages", serde_json::json!({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}})),
        ("input", serde_json::json!({"type": "input_image", "image_url": "https://example.com/a.png"})),
        ("messages", serde_json::json!({"type": "document", "source": {"type": "text", "data": "document"}})),
    ] {
        let resolver = RepoResolver::default();
        let sessions = SessionMemory::default();
        let headers = vec![("x-session-id".into(), "non-text-session".into())];
        let mut previous = None;
        let mut first = None;
        // The first non-text message needs an ID; later identical messages are new turns.
        let mut messages = Vec::new();
        let cache = ConvCache::default();
        for turn in 0..3 {
            messages.push(serde_json::json!({"role": "user", "content": [block]}));
            let body = serde_json::to_vec(&serde_json::json!({"model": "m", list: messages})).unwrap();
            let mut ctx = T { headers: headers.clone(), resolver: &resolver, sessions: &sessions };
            let opening = attribute_bytes(&body, &mut ctx, &cache, true).unwrap();
            assert_eq!(opening.mismatch, None);
            let id = opening.attribution.turn_id.unwrap();
            assert_ne!(Some(&id), previous.as_ref());
            first.get_or_insert(id.clone());
            for step in 0..2 {
                let tool = format!("tool_{turn}_{step}");
                messages.push(serde_json::json!({"role": "assistant", "content": [
                    {"type": "tool_use", "id": tool, "name": "Bash", "input": {"command": "true"}}
                ]}));
                messages.push(serde_json::json!({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": tool, "content": "ok"},
                    {"type": "image", "source": {"data": "tool image"}}
                ]}));
                let body = serde_json::to_vec(&serde_json::json!({"model": "m", list: messages})).unwrap();
                let result = attribute_bytes(&body, &mut ctx, &cache, true).unwrap();
                assert_eq!(result.mismatch, None);
                assert_eq!(result.attribution.turn_id.as_ref(), Some(&id));
            }
            previous = Some(id);
        }
        // Different non-text content must also change the hash, not just the turn count.
        let body = serde_json::to_vec(&serde_json::json!({"model": "m", list: [
            {"role": "user", "content": [{"type": "image", "source": {"data": "different"}}]}
        ]})).unwrap();
        let mut ctx = T { headers, resolver: &resolver, sessions: &sessions };
        let changed = attribute_bytes(&body, &mut ctx, &ConvCache::default(), false).unwrap();
        assert_ne!(changed.attribution.turn_id, first);
    }
}

#[test]
fn native_turn_ids_win_and_untagged_requests_keep_turn_metadata() {
    let resolver = RepoResolver::default();
    let sessions = SessionMemory::default();
    let body = br#"{"model":"m","messages":[{"role":"user","content":"hello"}]}"#;
    let mut ctx = T {
        headers: vec![("x-session-id".into(), "pi-session".into()), ("X-Turn-Id".into(), "entry-42".into())],
        resolver: &resolver,
        sessions: &sessions,
    };
    let result = attribute_bytes(body, &mut ctx, &ConvCache::default(), false).unwrap();
    assert_eq!(result.attribution.repository, None);
    assert_eq!(result.attribution.turn_id.as_deref(), Some("entry-42"));
    assert_eq!(result.attribution.turn_id_source.as_deref(), Some("harness_header"));
    let spliced = rrh::json::apply_plan(body, result.plan.as_ref().unwrap());
    let value: serde_json::Value = serde_json::from_slice(&spliced).unwrap();
    assert_eq!(value["metadata"], serde_json::json!({
        "turn_id": "entry-42",
        "turn_id_source": "harness_header"
    }));
}

#[test]
fn read_only_evidence_has_its_own_source_label() {
    let tmp = std::env::temp_dir().join(format!("rrh-read-{}", std::process::id()));
    let alpha = repo(&tmp, "alpha");
    std::fs::write(format!("{alpha}/README.md"), "alpha").unwrap();
    let resolver = RepoResolver::default();
    let sessions = SessionMemory::default();
    let body = serde_json::to_vec(&serde_json::json!({"model": "m", "messages": [
        {"role": "user", "content": "read it"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "read", "name": "Read", "input": {"file_path": format!("{alpha}/README.md")}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "read", "content": "alpha"}]}
    ]})).unwrap();
    let mut ctx = T { headers: vec![("x-session-id".into(), "read-session".into())], resolver: &resolver, sessions: &sessions };
    let result = attribute_bytes(&body, &mut ctx, &ConvCache::default(), false).unwrap();
    assert_eq!(result.attribution.repository.as_deref(), Some("ramp/alpha"));
    assert_eq!(result.attribution.repository_source.as_deref(), Some("tool_read_path"));
    std::fs::remove_dir_all(tmp).unwrap();
}

#[tokio::test]
async fn zero_copy_body_equals_spliced() {
    let tmp = std::env::temp_dir().join(format!("rrh-zc-{}", std::process::id()));
    let alpha = repo(&tmp, "alpha");
    let resolver = RepoResolver::default();
    let sessions = SessionMemory::default();
    for b in bodies(&alpha, &alpha, true) {
        let b = bytes::Bytes::from(b);
        let mut ctx = T { headers: vec![], resolver: &resolver, sessions: &sessions };
        let out = attribute_body(&b, &mut ctx, None, false);
        let Some(p) = out.plan else { panic!("not injected") };
        let chunks = Chunks::spliced(&b, &p);
        let len = chunks.len();
        let sent = chunks.collect().await.unwrap().to_bytes();
        assert_eq!(sent.len() as u64, len);
        assert_eq!(&sent[..], &rrh::json::apply_plan(&b, &p)[..]);
    }
    let _ = std::fs::remove_dir_all(&tmp);
}

#[test]
fn conversation_last_without_metadata_keeps_separator() {
    let tmp = std::env::temp_dir().join(format!("rrh-last-{}", std::process::id()));
    let alpha = repo(&tmp, "alpha");
    for conv in ["messages", "input"] {
        for prefix in [false, true] {
            let resolver = RepoResolver::default();
            let sessions = SessionMemory::default();
            let cache = ConvCache::default();
            let mut ctx = T {
                headers: vec![("X-Claude-Code-Session-Id".into(), format!("{conv}-{prefix}"))],
                resolver: &resolver, sessions: &sessions,
            };
            let mut messages = vec![];
            for turn in 0..3 {
                messages.push(serde_json::json!({"role": "assistant", "content": [
                    {"type": "tool_use", "id": format!("tool_{turn}"), "name": "Bash", "input": {"command": format!("cd {alpha}")}}
                ]}));
                messages.push(serde_json::json!({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": format!("tool_{turn}"), "content": "ok"}
                ]}));
                let mut value = serde_json::Map::new();
                if prefix { value.insert("model".into(), "test".into()); }
                value.insert(conv.into(), serde_json::json!(messages));
                let body = serde_json::to_vec(&value).unwrap();
                let r = attribute_bytes(&body, &mut ctx, &cache, true).unwrap();
                assert_eq!(r.incremental, turn > 0);
                assert_eq!(r.mismatch, None);
                let spliced = rrh::json::apply_plan(&body, &r.plan.unwrap());
                let tagged: serde_json::Value = serde_json::from_slice(&spliced).unwrap();
                assert_eq!(tagged["metadata"]["repository"], "ramp/alpha");
                assert_eq!(tagged[conv], value[conv]);
            }
        }
    }
    std::fs::remove_dir_all(tmp).unwrap();
}

#[test]
fn expansion_before_a_path_does_not_keep_the_previous_repository() {
    let tmp = std::env::temp_dir().join(format!("rrh-expansion-{}", std::process::id()));
    let alpha = repo(&tmp, "alpha");
    let beta = repo(&tmp, "beta");
    let resolver = RepoResolver::default();
    let sessions = SessionMemory::default();
    let mut ctx = T {
        headers: vec![("x-session-id".into(), "expansion".into())],
        resolver: &resolver, sessions: &sessions,
    };
    for (command, expected) in [
        (format!("cd {alpha}"), "ramp/alpha"),
        (format!("echo \"$HOME\"; cat {beta}/README.md"), "ramp/beta"),
    ] {
        let body = serde_json::to_vec(&serde_json::json!({"messages": [{"role": "assistant", "content": [
            {"type": "tool_use", "id": "tool", "name": "Bash", "input": {"command": command}}
        ]}]})).unwrap();
        let result = attribute_body(&body, &mut ctx, None, false);
        assert_eq!(result.attribution.repository.as_deref(), Some(expected));
        let tagged = rrh::json::apply_plan(&body, &result.plan.unwrap());
        let value: serde_json::Value = serde_json::from_slice(&tagged).unwrap();
        assert_eq!(value["metadata"]["repository"], expected);
    }
    std::fs::remove_dir_all(tmp).unwrap();
}

#[test]
fn case_substitution_does_not_poison_session_repository() {
    let tmp = std::env::temp_dir().join(format!("rrh-case-{}", std::process::id()));
    let alpha = repo(&tmp, "alpha");
    let beta = repo(&tmp, "beta");
    let resolver = RepoResolver::default();
    let sessions = SessionMemory::default();
    let mut ctx = T {
        headers: vec![("x-session-id".into(), "case-substitution".into())],
        resolver: &resolver, sessions: &sessions,
    };
    let command = format!("echo $(case alpha in beta) cat {beta}/README.md;; esac)");
    let body_for = |command: &str| serde_json::to_vec(&serde_json::json!({"messages": [{"role": "assistant", "content": [
        {"type": "tool_use", "id": "tool", "name": "Bash", "input": {"command": command}}
    ]}]})).unwrap();
    // With no previous repository, the unreachable path must not create an attribution.
    let result = attribute_body(&body_for(&command), &mut ctx, None, false);
    assert_eq!(result.attribution.repository, None);
    for command in [
        format!("cd {alpha}"),
        command,
        format!("echo $(case alpha in beta) cd {beta};; esac)"),
        format!("echo $(case alpha in beta) git -C {beta} status;; esac)"),
        "echo no-path".into(),
    ] {
        let body = body_for(&command);
        let result = attribute_body(&body, &mut ctx, None, false);
        assert_eq!(result.attribution.repository.as_deref(), Some("ramp/alpha"));
        let tagged = rrh::json::apply_plan(&body, &result.plan.unwrap());
        let value: serde_json::Value = serde_json::from_slice(&tagged).unwrap();
        assert_eq!(value["metadata"]["repository"], "ramp/alpha");
    }
    std::fs::remove_dir_all(tmp).unwrap();
}

#[test]
fn comments_and_case_arguments_preserve_claude_shell_tracking() {
    let tmp = std::env::temp_dir().join(format!("rrh-shell-tracking-{}", std::process::id()));
    let alpha = repo(&tmp, "alpha");
    let beta = repo(&tmp, "beta");
    repo(std::path::Path::new(&beta), "child");
    for switch in [
        format!("# switch repos\ncd {beta} && git status"),
        format!("echo $(printf '%s' case); cd {beta}"),
        format!("time -p -- cd {beta}"),
        format!("command -p cd {beta}"),
        format!("command -- cd {beta}"),
        format!("command -p -- cd {beta}"),
        format!("builtin -- cd {beta}"),
        format!("builtin -- command -p -- cd {beta}"),
        format!("[[ -d {beta} ]] && cd {beta}"),
        format!("for x in a; do echo done; done; cd {beta}"),
        format!("printf x > >(cat); cd {beta}"),
    ] {
        let resolver = RepoResolver::default();
        let sessions = SessionMemory::default();
        let cache = ConvCache::default();
        let mut ctx = T {
            headers: vec![("X-Claude-Code-Session-Id".into(), "shell-tracking".into())],
            resolver: &resolver, sessions: &sessions,
        };
        let mut messages = vec![];
        for (turn, (command, expected)) in [
            (format!("cd {alpha}"), "ramp/alpha"),
            (switch, "ramp/beta"),
            (format!("echo \"example: cd {alpha} && make test\""), "ramp/beta"),
            (format!("echo cd {alpha}"), "ramp/beta"),
            (format!("echo 'git -C {alpha} status'"), "ramp/beta"),
            (format!("command -v cd {alpha}"), "ramp/beta"),
            (format!("command -V cd {alpha}"), "ramp/beta"),
            (format!("echo $(for x do case alpha in beta) cd {alpha};; esac; done)"), "ramp/beta"),
            (format!("echo $(select x do case alpha in beta) cd {alpha};; esac; done)"), "ramp/beta"),
            (format!("for x in; do command done; cd {alpha}; done"), "ramp/beta"),
            (format!("for x in; do builtin done; cd {alpha}; done"), "ramp/beta"),
            (format!("cd {alpha} | cat; pwd"), "ramp/beta"),
            (format!("cat |\n cd {alpha}; pwd"), "ramp/beta"),
            (format!("cd {alpha} && pwd & pwd"), "ramp/beta"),
            (format!("while false; do cd {alpha}; done; pwd"), "ramp/beta"),
            (format!("until true; do cd {alpha}; done; pwd"), "ramp/beta"),
            (format!("if false; then cd {alpha}; fi; pwd"), "ramp/beta"),
            (format!("if true; then while false; do cd {alpha}; done; fi; pwd"), "ramp/beta"),
            (format!("{{ cd {alpha}; }} | cat; pwd"), "ramp/beta"),
            (format!("{{ {{ cd {alpha}; }}; pwd; }} & pwd"), "ramp/beta"),
            (format!("cd {beta} &>/dev/null; pwd"), "ramp/beta"),
            (format!("cd {beta} &>>/dev/null; pwd"), "ramp/beta"),
            ("cd child".into(), "ramp/child"),
            ("pwd".into(), "ramp/child"),
        ].into_iter().enumerate() {
            messages.push(serde_json::json!({"role": "assistant", "content": [
                {"type": "tool_use", "id": format!("tool_{turn}"), "name": "Bash", "input": {"command": command}}
            ]}));
            messages.push(serde_json::json!({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": format!("tool_{turn}"), "content": "ok"}
            ]}));
            let body = serde_json::to_vec(&serde_json::json!({
                "system": format!("You are Claude Code.\n<env>\nWorking directory: {alpha}\n</env>"),
                "messages": messages,
            })).unwrap();
            let result = attribute_bytes(&body, &mut ctx, &cache, true).unwrap();
            assert_eq!(result.mismatch, None);
            // The longer replay also crosses the scheduled full scan every 16 requests.
            assert_eq!(result.incremental, turn % 16 != 0);
            assert_eq!(result.attribution.repository.as_deref(), Some(expected));
            let tagged = rrh::json::apply_plan(&body, &result.plan.unwrap());
            let value: serde_json::Value = serde_json::from_slice(&tagged).unwrap();
            assert_eq!(value["metadata"]["repository"], expected);
        }
    }
    std::fs::remove_dir_all(tmp).unwrap();
}

#[test]
fn command_local_directories_do_not_poison_incremental_repository_tags() {
    let tmp = std::env::temp_dir().join(format!("rrh-command-boundaries-{}", std::process::id()));
    let alpha = repo(&tmp, "alpha");
    let beta = repo(&tmp, "beta");
    repo(std::path::Path::new(&beta), "child");
    for separator in [";", "&&", "||", "|", "&", "\n"] {
        let resolver = RepoResolver::default();
        let sessions = SessionMemory::default();
        let cache = ConvCache::default();
        let mut ctx = T {
            headers: vec![("X-Claude-Code-Session-Id".into(), "command-boundaries".into())],
            resolver: &resolver, sessions: &sessions,
        };
        let mut messages = Vec::new();
        for (turn, (command, expected)) in [
            (format!("make -C {beta} test {separator} make -C . test"), "ramp/alpha"),
            (format!("make -C {beta} -C child test; # done\n"), "ramp/child"),
            ("pwd".to_string(), "ramp/alpha"),
            (format!("cd {beta} && make -C {alpha} test && make -C . test"), "ramp/beta"),
            ("pwd".to_string(), "ramp/beta"),
        ].into_iter().enumerate() {
            messages.push(serde_json::json!({"role":"assistant", "content":[
                {"type":"tool_use", "id":format!("tool_{turn}"), "name":"Bash", "input":{"command":command}}
            ]}));
            messages.push(serde_json::json!({"role":"user", "content":[
                {"type":"tool_result", "tool_use_id":format!("tool_{turn}"), "content":"ok"}
            ]}));
            let body = serde_json::to_vec(&serde_json::json!({
                "system":format!("You are Claude Code.\n<env>\nWorking directory: {alpha}\n</env>"),
                "messages":messages,
            })).unwrap();
            let result = attribute_bytes(&body, &mut ctx, &cache, true).unwrap();
            assert_eq!(result.mismatch, None);
            assert_eq!(result.incremental, turn > 0);
            assert_eq!(result.attribution.repository.as_deref(), Some(expected), "{command}");
            let tagged = rrh::json::apply_plan(&body, &result.plan.unwrap());
            let value: serde_json::Value = serde_json::from_slice(&tagged).unwrap();
            assert_eq!(value["metadata"]["repository"], expected);
        }
    }
    std::fs::remove_dir_all(tmp).unwrap();
}
