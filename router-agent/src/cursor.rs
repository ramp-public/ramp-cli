//! Cursor sends model requests from its own servers, so they never pass through this agent.
//! Cursor also writes each chat's transcript locally. This tags those transcripts with the same
//! attribution rules and uploads the results; Router joins each to its request by chat id, first
//! user query, and tool-call count.

use crate::attr::{attribute_bytes, Attribution, ConvCache, Ctx, SessionMemory};
use crate::git::{RepoResolver, Resolved};
use bytes::Bytes;
use http_body_util::Full;
use hyper_util::client::legacy::{connect::HttpConnector, Client};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, SystemTime};

const POLL: Duration = Duration::from_secs(5);
const RECENT: Duration = Duration::from_secs(24 * 3600);
/// Records per upload. Router rejects a request with more than 5000 (cursorattribution.MaxRecords).
const MAX_BATCH: usize = 1000;
/// Wait after Router refuses an upload (a revoked key, a rejected record): retrying sooner won't help.
const REJECTED_BACKOFF: Duration = Duration::from_secs(10 * 60);
/// Written by `ramp router configure cursor` and `ramp router cursor`, next to the settings file.
const BINDING: &str = "router-agent-cursor.json";
/// The last upload's time, size and outcome, which `ramp router agent status` shows. No key, no records.
const UPLOAD_STATUS: &str = "router-agent-cursor-status.json";

struct Tagger<'a> {
    session: String,
    resolver: &'a RepoResolver,
    sessions: &'a SessionMemory,
}

impl Ctx for Tagger<'_> {
    fn header(&self, name: &str) -> Option<String> {
        name.eq_ignore_ascii_case("x-session-id").then(|| self.session.clone())
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

/// Cursor's folder name for a workspace: every character other than [A-Za-z0-9-] becomes `-`.
pub fn slug(workspace: &str) -> String {
    workspace.trim_start_matches('/').chars().map(|c| if c.is_ascii_alphanumeric() || c == '-' { c } else { '-' }).collect()
}

fn percent_decode(s: &str) -> String {
    let b = s.as_bytes();
    let (mut out, mut i) = (Vec::with_capacity(b.len()), 0);
    while i < b.len() {
        match (b[i], b.get(i + 1..i + 3).and_then(|h| u8::from_str_radix(std::str::from_utf8(h).ok()?, 16).ok())) {
            (b'%', Some(v)) => (out.push(v), i += 3),
            (c, _) => (out.push(c), i += 1),
        };
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// Cursor's user-data directory, found the way the CLI's `cursor.user_dir()` finds it.
fn user_dir(home: &Path, env: impl Fn(&str) -> Option<std::ffi::OsString>) -> PathBuf {
    let var = |name: &str| env(name).filter(|v| !v.is_empty()).map(PathBuf::from);
    if let Some(dir) = var("RAMP_CURSOR_USER_DIR") {
        return dir.strip_prefix("~").map_or_else(|_| dir.clone(), |rest| home.join(rest));
    }
    #[cfg(target_os = "macos")]
    let base = home.join("Library/Application Support");
    #[cfg(not(target_os = "macos"))]
    let base = var("XDG_CONFIG_HOME").unwrap_or_else(|| home.join(".config"));
    base.join("Cursor/User")
}

/// Workspace folders Cursor has opened, by slug. Different folders can share a slug (`a_b` and
/// `a.b`), and then one project folder holds both folders' transcripts, so the project is left out
/// rather than tagged from a guessed folder. Cursor never prunes workspaceStorage, so a folder that
/// no longer exists and whose workspace state is unchanged for RECENT is dropped first: it cannot
/// own a transcript changed within RECENT, the only ones scanned. Only shared slugs are checked, as
/// a stat on a hung network mount can block for minutes.
fn workspaces(storage: &Path) -> HashMap<String, String> {
    let mut by_slug: HashMap<String, Vec<(String, PathBuf)>> = HashMap::new();
    for e in std::fs::read_dir(storage).into_iter().flatten().flatten() {
        let Some(v) = std::fs::read(e.path().join("workspace.json")).ok().and_then(|b| serde_json::from_slice::<Value>(&b).ok()) else { continue };
        let Some(dir) = v["folder"].as_str().and_then(|f| f.strip_prefix("file://")).map(percent_decode) else { continue };
        by_slug.entry(slug(&dir)).or_default().push((dir, e.path()));
    }
    // When Cursor last touched a workspace: its storage folder or the state file inside it.
    let last_used = |entry: &Path| {
        [entry.to_path_buf(), entry.join("state.vscdb")].iter().filter_map(|p| std::fs::metadata(p).and_then(|m| m.modified()).ok()).max()
    };
    by_slug
        .into_iter()
        .filter_map(|(slug, mut entries)| {
            if entries.iter().any(|(d, _)| *d != entries[0].0) {
                entries.retain(|(d, entry)| Path::new(d).is_dir() || last_used(entry).is_some_and(|t| t.elapsed().unwrap_or_default() <= RECENT));
            }
            let first = entries.first()?.0.clone();
            entries.iter().all(|(d, _)| *d == first).then_some((slug, first))
        })
        .collect()
}

/// One upload record per tool-call count from `from` on, tagged as the agent would tag the request
/// with that many calls. Earlier counts were already uploaded and never change, so they are skipped.
pub fn records(transcript: &str, workspace: &str, chat_id: &str, resolver: &RepoResolver, from: u64) -> Vec<Value> {
    let (sessions, cache) = (SessionMemory::default(), ConvCache::default());
    let mut ctx = Tagger { session: format!("cu:{chat_id}:{workspace}"), resolver, sessions: &sessions };
    let (mut out, mut calls, mut first) = (Vec::<Value>::new(), 0u64, None);
    // The request body for each step is this buffer plus a closing "]}". Each message is
    // serialized once as it arrives, so a step costs only its new messages, not the whole history.
    let mut body = serde_json::to_vec(&json!({ "system": format!("<env>\nWorking directory: {workspace}\n</env>") })).unwrap_or_default();
    body.pop();
    body.extend_from_slice(b",\"messages\":[");
    let first_message = body.len();
    let push = |body: &mut Vec<u8>, message: Value| {
        if body.len() > first_message {
            body.push(b',');
        }
        let _ = serde_json::to_writer(&mut *body, &message);
    };
    for line in transcript.lines() {
        let Ok(v) = serde_json::from_str::<Value>(line) else { continue };
        let content = v["message"]["content"].as_array().cloned().unwrap_or_default();
        match v["role"].as_str() {
            Some("user") => {
                if first.is_none() {
                    first = content.iter().filter_map(|p| p["text"].as_str()).find(|t| t.contains("<user_query>")).map(|t| format!("{:x}", Sha256::digest(t)));
                }
                push(&mut body, json!({ "role": "user", "content": content }));
            }
            Some("assistant") => {
                let mut results = Vec::new();
                let blocks: Vec<Value> = content
                    .into_iter()
                    .map(|mut p| {
                        if p["type"] == "tool_use" {
                            calls += 1;
                            p["id"] = json!(format!("t{calls}"));
                            results.push(json!({ "type": "tool_result", "tool_use_id": format!("t{calls}"), "content": "" }));
                        }
                        p
                    })
                    .collect();
                push(&mut body, json!({ "role": "assistant", "content": blocks }));
                if results.is_empty() {
                    continue;
                }
                push(&mut body, json!({ "role": "user", "content": results }));
            }
            _ => continue,
        }
        if calls < from {
            continue;
        }
        body.extend_from_slice(b"]}");
        let attributed = attribute_bytes(&body, &mut ctx, &cache, false);
        body.truncate(body.len() - 2);
        let Ok(r) = attributed else { continue };
        let (Some(repo), Some(q)) = (r.attribution.repository, &first) else { continue };
        let record = json!({ "chat_id": chat_id, "first_query_sha256": q, "tool_call_count": calls, "repository": repo, "repository_host": r.attribution.repository_host });
        if out.last().is_some_and(|l| l["tool_call_count"] == calls) {
            out.pop();
        }
        out.push(record);
    }
    out
}

/// Last upload per transcript: (mtime, next tool-call count to send, whether that mtime is fully sent).
type Sent = HashMap<PathBuf, (SystemTime, u64, bool)>;

/// Transcripts changed since the last upload, with at most `budget` records in total:
/// (path, mtime, new records, next tool-call count to send, all records for this mtime included).
fn scan(home: &Path, cursor_user: &Path, sent: &Sent, resolver: &RepoResolver, mut budget: usize) -> Vec<(PathBuf, SystemTime, Vec<Value>, u64, bool)> {
    let mut changed = Vec::new();
    for project in std::fs::read_dir(home.join(".cursor/projects")).into_iter().flatten().flatten() {
        for chat in std::fs::read_dir(project.path().join("agent-transcripts")).into_iter().flatten().flatten() {
            let Some(chat_id) = chat.file_name().to_str().map(String::from) else { continue };
            let subagents = std::fs::read_dir(chat.path().join("subagents")).into_iter().flatten().flatten().map(|e| e.path());
            for path in std::iter::once(chat.path().join(format!("{chat_id}.jsonl"))).chain(subagents) {
                let Some(mtime) = std::fs::metadata(&path).and_then(|m| m.modified()).ok() else { continue };
                if sent.get(&path).is_some_and(|p| p.0 == mtime && p.2) || mtime.elapsed().unwrap_or_default() > RECENT {
                    continue;
                }
                changed.push((project.file_name(), chat_id.clone(), path, mtime));
            }
        }
    }
    if changed.is_empty() {
        return Vec::new();
    }
    let spaces = workspaces(&cursor_user.join("workspaceStorage"));
    let mut out = Vec::new();
    for (project, chat_id, path, mtime) in changed {
        if budget == 0 {
            break;
        }
        let Some(workspace) = project.to_str().and_then(|s| spaces.get(s)) else { continue };
        // A file that cannot be read is retried next poll rather than recorded as sent. Bad UTF-8
        // only spoils its own line, which then fails to parse like any other partial line.
        let Ok(bytes) = std::fs::read(&path) else { continue };
        let from = sent.get(&path).map_or(0, |p| p.1);
        let mut new = records(&String::from_utf8_lossy(&bytes), workspace, &chat_id, resolver, from);
        let done = new.len() <= budget;
        new.truncate(budget);
        budget -= new.len();
        let next = new.last().and_then(|r| r["tool_call_count"].as_u64()).map_or(from, |c| c + 1);
        // One record Router rejects fails the whole upload, so those are never sent.
        let before = new.len();
        new.retain(acceptable);
        if new.len() < before {
            eprintln!("ramp-router-agent: skipped {} Cursor records Router would reject in {}", before - new.len(), path.display());
        }
        out.push((path, mtime, new, next, done));
    }
    out
}

/// Router's rules for an upload record (cursorattribution.validate).
fn acceptable(r: &Value) -> bool {
    let hex = |s: &str, upper: bool| s.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b) || (upper && (b'A'..=b'F').contains(&b)));
    let uuid = r["chat_id"].as_str().is_some_and(|s| s.split('-').map(str::len).eq([8, 4, 4, 4, 12]) && hex(&s.replace('-', ""), true));
    let sha = r["first_query_sha256"].as_str().is_some_and(|s| s.len() == 64 && hex(s, false));
    let repo = r["repository"].as_str().is_some_and(|s| !s.trim().is_empty() && s.len() <= 256);
    let host = r["repository_host"].as_str().map_or(r["repository_host"].is_null(), |s| s.len() <= 256);
    uuid && sha && repo && host && r["tool_call_count"].is_u64()
}

/// Cursor's binding: (Router base URL, API key, Cursor's user-data directory). Cursor's servers call
/// Router with the key Cursor was given, so uploads use that same key, which files them under the
/// same Router user. No binding, no uploads.
fn credential(settings: &Path) -> Option<(String, String, Option<PathBuf>)> {
    let parent = settings.parent()?;
    let binding: Value = serde_json::from_slice(&crate::auth::read_private(&parent.join(BINDING))?).ok()?;
    let upstream = binding["upstream"].as_str()?;
    // Never send the key to a host the proxy would refuse to forward to.
    let base = crate::proxy::default_upstream(upstream, &crate::proxy::read_extra_hosts(settings))?;
    let digest = binding["credential"].as_str().filter(|d| d.len() == 64 && d.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)))?;
    let stored: Value = serde_json::from_slice(&crate::auth::read_private(&parent.join("router-agent-credentials").join(format!("{digest}.json")))?).ok()?;
    // A key that cannot go in a header would fail every request after a full scan.
    let key = stored["api_key"].as_str().filter(|k| !k.is_empty() && !k.starts_with("ramp-local-") && hyper::header::HeaderValue::from_str(&format!("Bearer {k}")).is_ok())?;
    let user_dir = binding["user_dir"].as_str().map(PathBuf::from).filter(|d| d.is_absolute());
    (stored["upstream"].as_str()? == upstream).then(|| (base, key.to_string(), user_dir))
}

fn tagging_enabled(settings: &Path) -> bool {
    std::fs::read(settings).ok().and_then(|b| serde_json::from_slice::<Value>(&b).ok()).is_some_and(|v| v["enabled"] == true)
}

/// `status` is None when Router could not be reached or did not answer in time.
fn upload_status(records: usize, status: Option<hyper::StatusCode>, now: SystemTime) -> Value {
    let at = now.duration_since(std::time::UNIX_EPOCH).map_or(0, |d| d.as_secs());
    let (outcome, http_status) = match status {
        Some(s) if s.is_success() => ("ok", None),
        Some(s) => ("rejected", Some(s.as_u16())),
        None => ("unreachable", None),
    };
    json!({ "at": at, "records": records, "outcome": outcome, "http_status": http_status })
}

/// Written beside the settings file, replaced whole so status never reads half a file.
fn record_upload(settings: &Path, status: &Value) {
    let Some(parent) = settings.parent() else { return };
    let (path, temp) = (parent.join(UPLOAD_STATUS), parent.join(format!(".{UPLOAD_STATUS}.tmp")));
    if std::fs::write(&temp, status.to_string()).is_err() || std::fs::rename(&temp, &path).is_err() {
        let _ = std::fs::remove_file(&temp);
    }
}

pub async fn run(settings: PathBuf) {
    let Some(home) = std::env::var_os("HOME").map(PathBuf::from) else { return };
    let _ = rustls::crypto::ring::default_provider().install_default();
    let mut http = HttpConnector::new();
    http.enforce_http(false);
    let https = hyper_rustls::HttpsConnectorBuilder::new().with_tls_config(crate::tls::client_config()).https_only().enable_all_versions().wrap_connector(http);
    let client: Client<_, Full<Bytes>> = Client::builder(hyper_util::rt::TokioExecutor::new()).build(https);
    let resolver = Arc::new(RepoResolver::default());
    // The service gets no XDG_CONFIG_HOME or RAMP_CURSOR_USER_DIR, so the binding carries the folder
    // the CLI found; this is for a binding without one.
    let default_user = user_dir(&home, |n| std::env::var_os(n));
    // Shared with the scan task by reference count, so polls don't copy it. Entries for transcripts
    // untouched for RECENT are dropped; if such a chat resumes, it is uploaded again from step 0,
    // which is harmless: Router keeps every upload and reads the ones near each request.
    let mut sent: Arc<Sent> = Arc::default();
    loop {
        tokio::time::sleep(POLL).await;
        Arc::make_mut(&mut sent).retain(|_, (mtime, _, _)| mtime.elapsed().unwrap_or_default() <= RECENT);
        let Some((base, key, cursor_user)) = tagging_enabled(&settings).then(|| credential(&settings)).flatten() else { continue };
        let (h, u, s, r) = (home.clone(), cursor_user.unwrap_or_else(|| default_user.clone()), sent.clone(), resolver.clone());
        let Ok(changed) = tokio::task::spawn_blocking(move || scan(&h, &u, &s, &r, MAX_BATCH)).await else { continue };
        let batch: Vec<&Value> = changed.iter().flat_map(|(_, _, new, _, _)| new).collect();
        let status = if batch.is_empty() {
            None
        } else {
            let req = hyper::Request::post(format!("{base}/v1/attribution/cursor"))
                .header("authorization", format!("Bearer {key}"))
                .header("content-type", "application/json")
                .body(Full::new(Bytes::from(json!({ "records": batch }).to_string())));
            let status = match req {
                Ok(req) => match tokio::time::timeout(Duration::from_secs(10), client.request(req)).await {
                    Ok(Ok(r)) => Some(r.status()),
                    _ => None,
                },
                // Retrying at the next poll would rescan every transcript for the same failure.
                Err(_) => Some(hyper::StatusCode::BAD_REQUEST),
            };
            record_upload(&settings, &upload_status(batch.len(), status, SystemTime::now()));
            Some(status.unwrap_or(hyper::StatusCode::SERVICE_UNAVAILABLE))
        };
        if let Some(status) = status.filter(|s| !s.is_success()) {
            // Not marked sent either way. A refused key or record stays refused, so wait longer;
            // throttling, outages and timeouts retry after a minute.
            let retry = status == hyper::StatusCode::TOO_MANY_REQUESTS || status.is_server_error();
            tokio::time::sleep(if retry { Duration::from_secs(60) } else { REJECTED_BACKOFF }).await;
            continue;
        }
        let sent = Arc::make_mut(&mut sent);
        for (path, mtime, _, next, done) in changed {
            sent.insert(path, (mtime, next, done));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn root(name: &str) -> PathBuf {
        let root = std::env::temp_dir().join(format!("rrh-cursor-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).unwrap();
        // read_private refuses a group-writable parent, which a umask of 002 would give.
        std::fs::set_permissions(&root, std::os::unix::fs::PermissionsExt::from_mode(0o700)).unwrap();
        root
    }

    /// workspaceStorage entries for `folders` (relative to `root`, percent-encoded); `real` ones exist.
    fn storage(root: &Path, folders: &[(&str, bool)]) -> PathBuf {
        let storage = root.join("workspaceStorage");
        for (i, (folder, real)) in folders.iter().enumerate() {
            if *real {
                std::fs::create_dir_all(root.join(percent_decode(folder))).unwrap();
            }
            let dir = storage.join(format!("ws{i}"));
            std::fs::create_dir_all(&dir).unwrap();
            std::fs::write(dir.join("workspace.json"), json!({ "folder": format!("file://{}/{folder}", root.display()) }).to_string()).unwrap();
        }
        storage
    }

    fn repo(root: &Path, name: &str) -> String {
        let dir = root.join(name);
        std::fs::create_dir_all(dir.join(".git")).unwrap();
        std::fs::write(dir.join(".git/config"), format!("[remote \"origin\"]\n\turl = git@github.com:ramp/{name}.git\n")).unwrap();
        dir.to_str().unwrap().to_string()
    }

    #[test]
    fn colliding_workspace_slugs_are_left_out() {
        let root = root("slugs");
        let storage = storage(&root, &[("a_b", true), ("a.b", true), ("c", true), ("c", true), ("d%20e", true), ("f_g", true), ("f.g", false), ("h_i", true), ("h.i", false)]);
        // f.g was deleted long ago; h.i only just now, so its chats may still be in the project.
        let old = SystemTime::now() - RECENT - Duration::from_secs(3600);
        std::fs::File::open(storage.join("ws6")).unwrap().set_modified(old).unwrap();
        let spaces = workspaces(&storage);
        let at = |p: &str| spaces.get(&slug(root.join(p).to_str().unwrap())).cloned();
        assert_eq!(at("a_b"), None);
        assert_eq!(at("c").as_deref(), root.join("c").to_str());
        assert_eq!(at("d e").as_deref(), root.join("d e").to_str());
        // A folder deleted long ago does not hide the live one sharing its slug...
        assert_eq!(at("f_g").as_deref(), root.join("f_g").to_str());
        // ...but one used within RECENT may own recent transcripts, so the slug stays ambiguous.
        assert_eq!(at("h_i"), None);
        std::fs::remove_dir_all(&root).unwrap();
    }

    /// HOME laid out as scan() reads it, with one chat in `workspace`. The transcript is not written.
    fn home(root: &Path, workspace: &str, chat: &str) -> (PathBuf, PathBuf) {
        let home = root.join("home");
        let ws = home.join("cursor-user/workspaceStorage/ws0");
        std::fs::create_dir_all(&ws).unwrap();
        std::fs::write(ws.join("workspace.json"), json!({ "folder": format!("file://{workspace}") }).to_string()).unwrap();
        let dir = home.join(".cursor/projects").join(slug(workspace)).join("agent-transcripts").join(chat);
        std::fs::create_dir_all(&dir).unwrap();
        (home, dir.join(format!("{chat}.jsonl")))
    }

    fn transcript(dirs: &[&str]) -> String {
        let query = json!({ "role": "user", "message": { "content": [{ "type": "text", "text": "<user_query>\ngo\n</user_query>" }] } });
        let steps = dirs.iter().map(|d| json!({ "role": "assistant", "message": { "content": [{ "type": "tool_use", "name": "Shell", "input": { "command": format!("cd {d} && ls") } }] } }));
        std::iter::once(query).chain(steps).map(|v| v.to_string()).collect::<Vec<_>>().join("\n")
    }

    fn counts(new: &[Value]) -> Vec<u64> {
        new.iter().map(|r| r["tool_call_count"].as_u64().unwrap()).collect()
    }

    #[test]
    fn unreadable_transcripts_are_retried_and_bad_utf8_is_not_fatal() {
        let root = root("retry");
        let alpha = repo(&root, "alpha");
        let (home, path) = home(&root, &alpha, "11111111-1111-1111-1111-111111111111");
        let resolver = RepoResolver::default();
        // A directory where the transcript belongs: metadata succeeds, reading fails.
        std::fs::create_dir_all(&path).unwrap();
        assert!(scan(&home, &home.join("cursor-user"), &Sent::new(), &resolver, MAX_BATCH).is_empty());
        std::fs::remove_dir_all(&path).unwrap();
        let mut bytes = transcript(&[&alpha, &alpha]).into_bytes();
        bytes.extend_from_slice(b"\n{\"role\":\"user\",\"message\":{\"content\":[{\"type\":\"text\",\"text\":\"\xff\"}]}}");
        std::fs::write(&path, bytes).unwrap();
        let got = scan(&home, &home.join("cursor-user"), &Sent::new(), &resolver, MAX_BATCH);
        assert_eq!((counts(&got[0].2), got[0].3, got[0].4), (vec![0, 1, 2], 3, true));
        std::fs::remove_dir_all(&root).unwrap();
    }

    #[test]
    fn uploads_are_capped_and_resume_where_the_cap_stopped() {
        let root = root("cap");
        let alpha = repo(&root, "alpha");
        let (home, path) = home(&root, &alpha, "22222222-2222-2222-2222-222222222222");
        std::fs::write(&path, transcript(&[alpha.as_str(); 4])).unwrap();
        let resolver = RepoResolver::default();
        let mut sent = Sent::new();
        let mut uploaded = Vec::new();
        for _ in 0..4 {
            for (path, mtime, new, next, done) in scan(&home, &home.join("cursor-user"), &sent, &resolver, 2) {
                uploaded.extend(counts(&new));
                sent.insert(path, (mtime, next, done));
            }
        }
        assert_eq!(uploaded, [0, 1, 2, 3, 4]);
        assert!(sent[&path].2);
        std::fs::remove_dir_all(&root).unwrap();
    }

    /// A config dir with Cursor's binding and the credential it names, both private.
    fn binding(root: &Path, upstream: &str, stored_upstream: &str) -> PathBuf {
        use std::os::unix::fs::PermissionsExt;
        let digest = "a".repeat(64);
        let creds = root.join("router-agent-credentials");
        std::fs::create_dir_all(&creds).unwrap();
        std::fs::set_permissions(&creds, std::fs::Permissions::from_mode(0o700)).unwrap();
        let private = |path: PathBuf, v: Value| {
            std::fs::write(&path, v.to_string()).unwrap();
            std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600)).unwrap();
        };
        private(creds.join(format!("{digest}.json")), json!({ "upstream": stored_upstream, "api_key": "sk-cursor" }));
        private(root.join(BINDING), json!({ "upstream": upstream, "credential": digest, "user_dir": "/cursor/User" }));
        let settings = root.join("router-repo-helper.json");
        std::fs::write(&settings, json!({ "enabled": true, "hosts": ["router.example.test"] }).to_string()).unwrap();
        settings
    }

    #[test]
    fn uploads_use_only_cursors_binding_on_an_allowed_host() {
        let root = root("binding");
        let settings = binding(&root, "api.router.com", "api.router.com");
        assert_eq!(credential(&settings), Some(("https://api.router.com".into(), "sk-cursor".into(), Some(PathBuf::from("/cursor/User")))));
        // A host the settings allow, with a port.
        let settings = binding(&root, "router.example.test:8443", "router.example.test:8443");
        assert_eq!(credential(&settings).map(|c| c.0), Some("https://router.example.test:8443".into()));
        // The key is never sent to a host the proxy would refuse.
        assert_eq!(credential(&binding(&root, "evil.example.com", "evil.example.com")), None);
        // The binding must name a credential stored for the same upstream.
        assert_eq!(credential(&binding(&root, "api.router.com", "qa-api.router.com")), None);
        // A binding others can read is ignored, like any agent credential.
        use std::os::unix::fs::PermissionsExt;
        let settings = binding(&root, "api.router.com", "api.router.com");
        std::fs::set_permissions(root.join(BINDING), std::fs::Permissions::from_mode(0o644)).unwrap();
        assert_eq!(credential(&settings), None);
        std::fs::remove_file(root.join(BINDING)).unwrap();
        assert_eq!(credential(&settings), None);
        // A key that cannot be a header value, and a relative user_dir.
        let settings = binding(&root, "api.router.com", "api.router.com");
        let private = |path: PathBuf, v: Value| {
            std::fs::write(&path, v.to_string()).unwrap();
            std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600)).unwrap();
        };
        let digest = "a".repeat(64);
        private(root.join(BINDING), json!({ "upstream": "api.router.com", "credential": digest, "user_dir": "cursor/User" }));
        assert_eq!(credential(&settings).map(|c| c.2), Some(None));
        private(root.join(format!("router-agent-credentials/{digest}.json")), json!({ "upstream": "api.router.com", "api_key": "sk-\ncursor" }));
        assert_eq!(credential(&settings), None);
        std::fs::remove_dir_all(&root).unwrap();
    }

    #[test]
    fn records_router_would_reject_are_not_sent() {
        let ok = json!({ "chat_id": "0b9c1e2a-5f3d-4c6b-8a7e-1d2c3b4a5f60", "first_query_sha256": "f".repeat(64), "tool_call_count": 3, "repository": "ramp/router", "repository_host": "github.com" });
        assert!(acceptable(&ok));
        let with = |k: &str, v: Value| {
            let mut r = ok.clone();
            r[k] = v;
            acceptable(&r)
        };
        assert!(with("repository_host", Value::Null));
        assert!(with("chat_id", json!("0B9C1E2A-5F3D-4C6B-8A7E-1D2C3B4A5F60")));
        assert!(!with("chat_id", json!("not-a-uuid")));
        assert!(!with("chat_id", json!("0b9c1e2a5f3d4c6b8a7e1d2c3b4a5f60")));
        assert!(!with("first_query_sha256", json!("F".repeat(64))));
        assert!(!with("repository", json!("")));
        assert!(!with("repository", json!(" \t ")));
        assert!(!with("repository", json!("r".repeat(257))));
        assert!(with("repository", json!("r".repeat(256))));
        assert!(!with("repository_host", json!("h".repeat(257))));
        assert!(!with("tool_call_count", json!(-1)));
    }

    #[test]
    fn user_dir_follows_the_cli_overrides() {
        // A fake environment: set_var in a test races getenv in the tests running beside it.
        let home = Path::new("/home/u");
        let env = |vars: &'static [(&'static str, &'static str)]| move |n: &str| vars.iter().find(|v| v.0 == n).map(|v| v.1.into());
        assert_eq!(user_dir(home, env(&[("RAMP_CURSOR_USER_DIR", "~/cursor-user")])), Path::new("/home/u/cursor-user"));
        assert_eq!(user_dir(home, env(&[("RAMP_CURSOR_USER_DIR", "/abs/user"), ("XDG_CONFIG_HOME", "/xdg")])), Path::new("/abs/user"));
        assert_eq!(user_dir(home, env(&[("RAMP_CURSOR_USER_DIR", "")])), user_dir(home, env(&[])));
        #[cfg(not(target_os = "macos"))]
        {
            assert_eq!(user_dir(home, env(&[("XDG_CONFIG_HOME", "/xdg")])), Path::new("/xdg/Cursor/User"));
            assert_eq!(user_dir(home, env(&[])), Path::new("/home/u/.config/Cursor/User"));
        }
        #[cfg(target_os = "macos")]
        assert_eq!(user_dir(home, env(&[("XDG_CONFIG_HOME", "/xdg")])), Path::new("/home/u/Library/Application Support/Cursor/User"));
    }
    #[test]
    fn upload_status_is_what_cli_status_reads() {
        let at = std::time::UNIX_EPOCH + Duration::from_secs(1_700_000_000);
        assert_eq!(upload_status(3, Some(hyper::StatusCode::OK), at), json!({ "at": 1_700_000_000u64, "records": 3, "outcome": "ok", "http_status": null }));
        assert_eq!(upload_status(1, Some(hyper::StatusCode::UNAUTHORIZED), at)["http_status"], 401);
        assert_eq!(upload_status(1, Some(hyper::StatusCode::UNAUTHORIZED), at)["outcome"], "rejected");
        assert_eq!(upload_status(1, None, at)["outcome"], "unreachable");
        let root = root("upload-status");
        let settings = root.join("router-repo-helper.json");
        for status in [None, Some(hyper::StatusCode::OK)] {
            record_upload(&settings, &upload_status(2, status, at));
        }
        let written: Value = serde_json::from_slice(&std::fs::read(root.join(UPLOAD_STATUS)).unwrap()).unwrap();
        assert_eq!(written["outcome"], "ok");
        assert_eq!(std::fs::read_dir(&root).unwrap().count(), 1, "no temporary file is left behind");
        std::fs::remove_dir_all(&root).unwrap();
    }
}
