//! Keeps Router setups fresh: every few minutes (and when agent activity suggests a new
//! session), ask Router whether anything changed, and run `ramp router sync --scheduled` only
//! when it did or a refresh is due.
//!
//! `ramp` owns everything about what a refresh writes. After each scheduled sync it writes a
//! manifest (mode 0600) naming the version requests to make, their headers, the parts of the
//! version it last applied, and when a refresh is due regardless. This module only makes those
//! requests and compares; any doubt resolves toward running `ramp`, which re-checks itself.

use bytes::Bytes;
use http_body_util::{BodyExt, Empty};
use hyper::{Request, StatusCode};
use hyper_util::client::legacy::{connect::HttpConnector, Client};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::collections::{HashMap, HashSet};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

const REQUEST_TIMEOUT: Duration = Duration::from_secs(5);
const CHANGE_RETRY_SECONDS: f64 = 60.0 * 60.0;

#[derive(Default)]
struct ChangeRetries {
    /// Per-check answers and attempt times, not manifest write times. Only a
    /// matching expected value acknowledges a change; a failed Python recheck
    /// may rewrite the manifest without applying anything.
    attempts: HashMap<String, (String, f64)>,
}

impl ChangeRetries {
    fn should_refresh(&mut self, key: String, value: String, t: f64) -> bool {
        if let Some((previous, at)) = self.attempts.get(&key) {
            if previous == &value && t - at < CHANGE_RETRY_SECONDS {
                return false;
            }
        }
        self.attempts.insert(key, (value, t));
        true
    }

    fn acknowledge(&mut self, key: &str) {
        self.attempts.remove(key);
    }
}
/// Activity checks: a new session at most this often, any model request at most every ACTIVE.
const NEW_SESSION_MIN_GAP: f64 = 30.0;
const ACTIVE_MIN_GAP: f64 = 120.0;

#[derive(Clone, Debug)]
pub struct Config {
    /// The `ramp` executable that runs the refresh.
    pub ramp: PathBuf,
    pub env_file: PathBuf,
    pub manifest: PathBuf,
    /// Present when the user opted out of background sync: no checks, no refresh.
    pub opt_out: PathBuf,
    pub interval: Duration,
    /// Where the refresh's output goes.
    pub log: Option<PathBuf>,
}

/// What one check decided.
#[derive(Debug, PartialEq, Eq)]
pub enum Decision {
    /// Nothing changed and nothing is due.
    Idle,
    /// Run `ramp router sync --scheduled`, for this reason.
    Refresh(&'static str),
    Skipped(&'static str),
}

type HttpsClient = Client<hyper_rustls::HttpsConnector<HttpConnector>, Empty<Bytes>>;

pub struct Scheduler {
    cfg: Config,
    client: HttpsClient,
    last_check: Mutex<f64>,
    running: AtomicBool,
    checking: AtomicBool,
    changes: Mutex<ChangeRetries>,
    sessions: Mutex<HashSet<String>>,
    wake: tokio::sync::Notify,
}

fn now() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

impl Scheduler {
    pub fn new(cfg: Config) -> Arc<Scheduler> {
        let _ = rustls::crypto::ring::default_provider().install_default();
        let mut http = HttpConnector::new();
        http.enforce_http(false);
        http.set_connect_timeout(Some(REQUEST_TIMEOUT));
        let https = hyper_rustls::HttpsConnectorBuilder::new().with_tls_config(crate::tls::client_config()).https_or_http().enable_all_versions().wrap_connector(http);
        let client = Client::builder(hyper_util::rt::TokioExecutor::new())
            .pool_idle_timeout(Duration::from_secs(90))
            .pool_timer(hyper_util::rt::TokioTimer::new())
            .build(https);
        Arc::new(Scheduler {
            cfg,
            client,
            last_check: Mutex::new(0.0),
            running: AtomicBool::new(false),
            checking: AtomicBool::new(false),
            changes: Mutex::new(ChangeRetries::default()),
            sessions: Mutex::new(HashSet::new()),
            wake: tokio::sync::Notify::new(),
        })
    }

    /// A model request went through the proxy. A session not seen before (a harness just
    /// started) asks for a check soon; ongoing activity at most every couple of minutes.
    pub fn activity(&self, session: Option<&str>) {
        let new_session = session.is_some_and(|s| {
            let mut seen = self.sessions.lock().unwrap();
            if seen.len() > 4096 {
                seen.clear();
            }
            seen.insert(s.to_string())
        });
        let since = now() - *self.last_check.lock().unwrap();
        if (new_session && since >= NEW_SESSION_MIN_GAP) || since >= ACTIVE_MIN_GAP {
            self.wake.notify_one();
        }
    }

    /// Runs forever: a check at start, then whenever the interval has passed on the wall clock
    /// (so a machine that slept checks as soon as it wakes), or activity asks for one.
    pub async fn run(self: Arc<Self>) {
        loop {
            let due = now() - *self.last_check.lock().unwrap() >= self.cfg.interval.as_secs_f64();
            let woken = tokio::select! {
                _ = self.wake.notified() => true,
                _ = tokio::time::sleep(Duration::from_secs(if due { 0 } else { 60 })) => false,
            };
            if due || woken {
                self.clone().check_and_refresh().await;
            }
        }
    }

    pub async fn check_and_refresh(self: Arc<Self>) {
        if self.checking.swap(true, Ordering::AcqRel) {
            return;
        }
        *self.last_check.lock().unwrap() = now();
        let decision = self.decide().await;
        if let Decision::Refresh(reason) = decision {
            self.spawn_refresh(reason);
        }
        self.checking.store(false, Ordering::Release);
    }

    pub async fn decide(&self) -> Decision {
        if self.cfg.opt_out.exists() {
            return Decision::Skipped("opted_out");
        }
        if self.running.load(Ordering::Acquire) {
            return Decision::Skipped("refresh_running");
        }
        let manifest = match std::fs::read(&self.cfg.manifest).ok().and_then(|b| serde_json::from_slice::<Value>(&b).ok()) {
            Some(m) => m,
            // never written (fresh install, or a setup just changed): ramp writes it
            None => return Decision::Refresh("no_manifest"),
        };
        let t = now();
        // A Cowork pass waits for Claude Desktop to close: run it once every
        // process ramp recorded is gone (signal 0 only checks it exists).
        let wait: Vec<i32> = manifest.get("cowork_wait_pids").and_then(Value::as_array).into_iter().flatten().filter_map(Value::as_i64).filter_map(|p| i32::try_from(p).ok()).filter(|&p| p > 0).collect();
        if !wait.is_empty() && wait.iter().all(|&p| unsafe { libc::kill(p, 0) } != 0 && std::io::Error::last_os_error().raw_os_error() == Some(libc::ESRCH)) {
            return Decision::Refresh("cowork");
        }
        if manifest.get("due_at").and_then(Value::as_f64).is_none_or(|d| t >= d) {
            return Decision::Refresh("due");
        }
        let unsupported_due = manifest.get("unsupported_due_at").and_then(Value::as_f64).is_none_or(|d| t >= d);
        for conn in manifest.get("connections").and_then(Value::as_array).into_iter().flatten() {
            if conn.get("skip_until").and_then(Value::as_f64).is_some_and(|s| t < s) {
                continue; // a key Router rejected recently
            }
            let expected: Option<Vec<&str>> = conn.get("expected").and_then(Value::as_array).map(|a| a.iter().filter_map(Value::as_str).collect());
            let headers = conn.get("headers").and_then(Value::as_object);
            let mut includes_settings = false;
            for (i, check) in conn.get("checks").and_then(Value::as_array).into_iter().flatten().enumerate() {
                let (Some(url), Some(kind)) = (check.get("url").and_then(Value::as_str), check.get("kind").and_then(Value::as_str)) else { continue };
                // Keep legacy checks in the manifest for older Routers and for
                // settings reads Python could not complete during a refresh.
                if kind == "defaults" && includes_settings && expected.as_ref().and_then(|e| e.get(i)).is_some_and(|v| *v != "?" && *v != "none") {
                    continue;
                }
                let outcome = self.fetch(url, headers).await;
                let value = match (kind, outcome) {
                    (_, Outcome::Revoked) if i == 0 => return Decision::Refresh("key_rejected"),
                    ("sync", Outcome::Unsupported) => {
                        if unsupported_due {
                            return Decision::Refresh("unsupported_router");
                        }
                        break;
                    }
                    ("sync", Outcome::Ok(body)) => {
                        includes_settings = body.get("includes_coding_agent_settings") == Some(&Value::Bool(true));
                        match body.get("version").and_then(Value::as_str) {
                            Some(v) => v.to_string(),
                            None => break, // an unreadable answer is an outage: decide nothing
                        }
                    },
                    ("defaults", Outcome::Unsupported) => "none".to_string(),
                    ("defaults", Outcome::Ok(body)) => match defaults_digest(&body) {
                        Some(d) => d,
                        None => continue,
                    },
                    // the sync check failing decides nothing for this key; a failed defaults
                    // check keeps the part ramp recorded
                    (_, _) if i == 0 => break,
                    _ => continue,
                };
                // Include the connection's headers: two keys at one URL can
                // expose different model catalogs and must retry independently.
                let key = format!("{url}\n{}", conn.get("headers").unwrap_or(&Value::Null));
                match &expected {
                    Some(e) if e.get(i) == Some(&value.as_str()) => {
                        self.changes.lock().unwrap().acknowledge(&key);
                    }
                    _ => {
                        if self.changes.lock().unwrap().should_refresh(key, value, t) {
                            return Decision::Refresh("changed");
                        }
                    }
                }
            }
        }
        Decision::Idle
    }

    async fn fetch(&self, url: &str, headers: Option<&serde_json::Map<String, Value>>) -> Outcome {
        let mut req = Request::get(url);
        for (k, v) in headers.into_iter().flatten() {
            if let Some(v) = v.as_str() {
                req = req.header(k.as_str(), v);
            }
        }
        let Ok(req) = req.body(Empty::<Bytes>::new()) else { return Outcome::Unavailable };
        let fut = async {
            let resp = self.client.request(req).await.ok()?;
            let status = resp.status();
            let body = resp.into_body().collect().await.ok()?.to_bytes();
            Some((status, body))
        };
        match tokio::time::timeout(REQUEST_TIMEOUT, fut).await {
            Ok(Some((status, body))) => classify(status, &body),
            _ => Outcome::Unavailable,
        }
    }

    fn spawn_refresh(self: &Arc<Self>, reason: &'static str) {
        if self.running.swap(true, Ordering::AcqRel) {
            return;
        }
        let this = self.clone();
        std::thread::spawn(move || {
            let status = this.run_ramp();
            this.log(&format!("scheduled refresh ({reason}) exited: {status}"));
            this.running.store(false, Ordering::Release);
        });
    }

    fn run_ramp(&self) -> String {
        use std::os::unix::process::CommandExt;
        let log = self.cfg.log.as_ref().and_then(|p| std::fs::OpenOptions::new().create(true).append(true).open(p).ok());
        let mut cmd = std::process::Command::new(&self.cfg.ramp);
        cmd.args(["router", "sync", "--scheduled", "--env-file"]).arg(&self.cfg.env_file);
        cmd.stdin(std::process::Stdio::null());
        match log.and_then(|l| Some((l.try_clone().ok()?, l))) {
            Some((out, err)) => {
                cmd.stdout(out).stderr(err);
            }
            None => {
                cmd.stdout(std::process::Stdio::null()).stderr(std::process::Stdio::null());
            }
        }
        // Its own session, so restarting the agent never stops a refresh mid-write; and low
        // priority, since the agent itself may be serving requests at normal priority.
        unsafe {
            cmd.pre_exec(|| {
                libc::setsid();
                libc::setpriority(libc::PRIO_PROCESS, 0, 10);
                low_io_priority();
                Ok(())
            });
        }
        match cmd.status() {
            Ok(s) => s.to_string(),
            Err(e) => format!("could not start {}: {e}", self.cfg.ramp.display()),
        }
    }

    fn log(&self, line: &str) {
        if let Some(p) = &self.cfg.log {
            if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(p) {
                use std::io::Write;
                let _ = writeln!(f, "{} ramp-router-agent: {line}", crate::log::timestamp());
            }
        }
    }
}

#[cfg(target_os = "macos")]
fn low_io_priority() {
    extern "C" {
        fn setiopolicy_np(iotype: libc::c_int, scope: libc::c_int, policy: libc::c_int) -> libc::c_int;
    }
    // IOPOL_TYPE_DISK, IOPOL_SCOPE_PROCESS, IOPOL_THROTTLE
    unsafe { setiopolicy_np(0, 0, 3) };
}

#[cfg(target_os = "linux")]
fn low_io_priority() {
    // ioprio_set(IOPRIO_WHO_PROCESS, 0, IOPRIO_CLASS_IDLE << 13)
    unsafe { libc::syscall(libc::SYS_ioprio_set, 1, 0, 3 << 13) };
}

#[cfg(not(any(target_os = "macos", target_os = "linux")))]
fn low_io_priority() {}

#[derive(Debug)]
enum Outcome {
    Ok(Value),
    Revoked,
    Unsupported,
    Unavailable,
}

/// The same classes as ramp's `_sync_check`.
fn classify(status: StatusCode, body: &[u8]) -> Outcome {
    match status.as_u16() {
        401 | 403 => Outcome::Revoked,
        404 | 405 | 501 => Outcome::Unsupported,
        200 => serde_json::from_slice(body).map(Outcome::Ok).unwrap_or(Outcome::Unavailable),
        _ => Outcome::Unavailable,
    }
}

/// ramp's `_fetch_defaults_version` digest: sha256 of `json.dumps(covered, sort_keys=True)`,
/// first 16 hex digits.
pub fn defaults_digest(body: &Value) -> Option<String> {
    // ramp reads only an object here; anything else is no usable answer
    let harnesses = body.get("harnesses").filter(|h| h.is_object())?;
    let covered = if body.get("switchyard_offered") == Some(&Value::Bool(true)) {
        serde_json::json!({"harnesses": harnesses, "switchyard_offered": true})
    } else {
        harnesses.clone()
    };
    let mut text = String::new();
    py_dumps(&covered, &mut text);
    let hex = format!("{:x}", Sha256::digest(text.as_bytes()));
    Some(hex[..16].to_string())
}

/// Python's `json.dumps(value, sort_keys=True)`: ", " and ": " separators, ASCII-only output.
fn py_dumps(v: &Value, out: &mut String) {
    match v {
        Value::Null => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Number(n) => match (n.as_i64(), n.as_u64(), n.as_f64()) {
            (Some(i), _, _) => out.push_str(&i.to_string()),
            (_, Some(u), _) => out.push_str(&u.to_string()),
            (_, _, Some(f)) => out.push_str(&py_float(f)),
            _ => out.push_str(&n.to_string()),
        },
        Value::String(s) => py_str(s, out),
        Value::Array(a) => {
            out.push('[');
            for (i, x) in a.iter().enumerate() {
                if i > 0 {
                    out.push_str(", ");
                }
                py_dumps(x, out);
            }
            out.push(']');
        }
        Value::Object(m) => {
            // UTF-8 byte order is code point order, which is how Python sorts str keys
            let mut keys: Vec<&String> = m.keys().collect();
            keys.sort();
            out.push('{');
            for (i, k) in keys.iter().enumerate() {
                if i > 0 {
                    out.push_str(", ");
                }
                py_str(k, out);
                out.push_str(": ");
                py_dumps(&m[k.as_str()], out);
            }
            out.push('}');
        }
    }
}

fn py_str(s: &str, out: &mut String) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 || (c as u32) > 0x7e => {
                let mut buf = [0u16; 2];
                for u in c.encode_utf16(&mut buf) {
                    out.push_str(&format!("\\u{u:04x}"));
                }
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

/// Python's `repr(float)`: shortest round trip, exponent outside [1e-4, 1e16).
fn py_float(f: f64) -> String {
    if f.is_nan() {
        return "NaN".into();
    }
    if f.is_infinite() {
        return if f > 0.0 { "Infinity".into() } else { "-Infinity".into() };
    }
    let a = f.abs();
    if a != 0.0 && !(1e-4..1e16).contains(&a) {
        // Rust's {:e} is shortest round trip too: "1.5e-5" -> Python "1.5e-05"
        let s = format!("{f:e}");
        let (m, e) = s.split_once('e').unwrap();
        let e: i32 = e.parse().unwrap();
        return format!("{m}e{}{:02}", if e < 0 { '-' } else { '+' }, e.abs());
    }
    let s = format!("{f}");
    if s.contains('.') { s } else { format!("{s}.0") }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unacknowledged_changes_retry_after_an_hour() {
        let mut changes = ChangeRetries::default();
        let t = 100.0;
        assert!(changes.should_refresh("sync".into(), "v2".into(), t));
        assert!(!changes.should_refresh("sync".into(), "v2".into(), t + 300.0));
        assert!(changes.should_refresh("sync".into(), "v2".into(), t + CHANGE_RETRY_SECONDS));
        // Suppressed checks must not slide the next retry forward forever.
        assert!(!changes.should_refresh("sync".into(), "v2".into(), t + 2.0 * CHANGE_RETRY_SECONDS - 1.0));
        assert!(changes.should_refresh("sync".into(), "v2".into(), t + 2.0 * CHANGE_RETRY_SECONDS));
    }

    #[test]
    fn changed_answers_and_connections_retry_independently() {
        let mut changes = ChangeRetries::default();
        assert!(changes.should_refresh("key-a/sync".into(), "v2".into(), 100.0));
        assert!(changes.should_refresh("key-b/sync".into(), "v2".into(), 100.0));
        assert!(changes.should_refresh("key-a/defaults".into(), "d2".into(), 100.0));
        assert!(!changes.should_refresh("key-a/sync".into(), "v2".into(), 101.0));
        assert!(changes.should_refresh("key-a/sync".into(), "v3".into(), 101.0));
        changes.acknowledge("key-a/sync");
        assert!(changes.should_refresh("key-a/sync".into(), "v3".into(), 102.0));
        assert!(!changes.should_refresh("key-b/sync".into(), "v2".into(), 102.0));
    }

    #[test]
    fn python_dumps() {
        let v = serde_json::json!({"b": [1, 2.5, true, null], "a": {"é": "x\"y\n", "z": 1e-5, "y": 1e16, "w": 3.0}, "c": "😀"});
        let mut s = String::new();
        py_dumps(&v, &mut s);
        // python3 -c 'import json; print(json.dumps({...}, sort_keys=True))'
        assert_eq!(s, r#"{"a": {"w": 3.0, "y": 1e+16, "z": 1e-05, "\u00e9": "x\"y\n"}, "b": [1, 2.5, true, null], "c": "\ud83d\ude00"}"#);
    }
}
