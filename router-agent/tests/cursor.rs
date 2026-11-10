//! A Cursor transcript is tagged per tool-call count with the same rules as a proxied request.
use rrh::cursor::{records, slug};
use rrh::git::RepoResolver;
use serde_json::json;

fn repo(root: &std::path::Path, name: &str) -> String {
    let dir = root.join(name);
    std::fs::create_dir_all(dir.join(".git")).unwrap();
    std::fs::write(dir.join(".git/config"), format!("[remote \"origin\"]\n\turl = git@github.com:ramp/{name}.git\n")).unwrap();
    dir.to_str().unwrap().to_string()
}

fn tool(calls: serde_json::Value) -> String {
    json!({ "role": "assistant", "message": { "content": calls } }).to_string()
}

#[test]
fn transcript_records_follow_tool_calls() {
    let tmp = std::env::temp_dir().join(format!("rrh-cursor-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&tmp);
    let (alpha, beta) = (repo(&tmp, "alpha"), repo(&tmp, "beta"));
    let query = "<timestamp>Thursday</timestamp>\n<user_query>\ngo\n</user_query>";
    let transcript = [
        json!({ "role": "user", "message": { "content": [{ "type": "text", "text": query }] } }).to_string(),
        tool(json!([{ "type": "tool_use", "name": "Shell", "input": { "command": format!("cd {beta} && git status") } }])),
        tool(json!([
            { "type": "tool_use", "name": "Read", "input": { "path": format!("{alpha}/a") } },
            { "type": "tool_use", "name": "Read", "input": { "path": format!("{beta}/b") } }
        ])),
        tool(json!([{ "type": "tool_use", "name": "Shell", "input": { "command": "git status", "working_directory": alpha } }])),
        json!({ "role": "assistant", "message": { "content": [{ "type": "text", "text": "done" }] } }).to_string(),
    ]
    .join("\n");
    let got: Vec<(u64, String)> = records(&transcript, &alpha, "chat", &RepoResolver::default(), 0)
        .iter()
        .map(|r| (r["tool_call_count"].as_u64().unwrap(), r["repository"].as_str().unwrap().to_string()))
        .collect();
    let resumed: Vec<u64> = records(&transcript, &alpha, "chat", &RepoResolver::default(), 3).iter().map(|r| r["tool_call_count"].as_u64().unwrap()).collect();
    let _ = std::fs::remove_dir_all(&tmp);
    assert_eq!(resumed, [3, 4]);
    let want = [(0, "ramp/alpha"), (1, "ramp/beta"), (3, "ramp/beta"), (4, "ramp/alpha")];
    assert_eq!(got, want.map(|(n, r)| (n, r.to_string())));
}

#[test]
fn slug_matches_cursor_project_folders() {
    assert_eq!(slug("/Users/a.b/dev/x_y/repo-a"), "Users-a-b-dev-x-y-repo-a");
}
