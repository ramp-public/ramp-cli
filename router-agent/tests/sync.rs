//! The background sync's decisions against a mock Router: run `ramp` only when something changed,
//! a key was rejected, or a refresh is due; decide nothing during an outage.

use http_body_util::Full;
use hyper::{Request, Response};
use rrh::sync::{defaults_digest, Config, Decision, Scheduler};
use serde_json::json;
use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::Duration;

type Routes = Arc<Mutex<HashMap<String, (u16, String)>>>;
type Requests = Arc<Mutex<Vec<String>>>;

async fn mock_router() -> (String, Routes, Requests) {
    let routes: Routes = Arc::new(Mutex::new(HashMap::new()));
    let requests: Requests = Arc::new(Mutex::new(Vec::new()));
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let base = format!("http://{}", listener.local_addr().unwrap());
    let r = routes.clone();
    let calls = requests.clone();
    tokio::spawn(async move {
        loop {
            let (stream, _) = listener.accept().await.unwrap();
            let r = r.clone();
            let calls = calls.clone();
            tokio::spawn(async move {
                let svc = hyper::service::service_fn(move |req: Request<hyper::body::Incoming>| {
                    let r = r.clone();
                    let calls = calls.clone();
                    async move {
                        assert_eq!(req.headers().get("authorization").unwrap(), "Bearer k");
                        calls.lock().unwrap().push(req.uri().path().to_string());
                        let (status, body) = r.lock().unwrap().get(req.uri().path()).cloned().unwrap_or((404, String::new()));
                        Ok::<_, std::convert::Infallible>(Response::builder().status(status).body(Full::new(bytes::Bytes::from(body))).unwrap())
                    }
                });
                let _ = hyper::server::conn::http1::Builder::new().serve_connection(hyper_util::rt::TokioIo::new(stream), svc).await;
            });
        }
    });
    (base, routes, requests)
}

const BODY: &str = r#"{"harnesses": {"codex": {"model": "gpt-5.5", "effort": null, "ratio": 0.25}, "claude-code": {"model": "claude-opus-5-5", "label": "Opus — fast"}}}"#;

#[test]
fn defaults_digest_matches_ramp() {
    // computed by ramp's _fetch_defaults_version digest on the same bodies
    let mut body: serde_json::Value = serde_json::from_str(BODY).unwrap();
    assert_eq!(defaults_digest(&body).as_deref(), Some("f4c0b80c11a20266"));
    body["switchyard_offered"] = json!(true);
    assert_eq!(defaults_digest(&body).as_deref(), Some("ebb824be7222c8c4"));
}

#[tokio::test]
async fn decisions() {
    let dir = std::env::temp_dir().join(format!("rra-sync-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let manifest = dir.join("router-agent-sync.json");
    let opt_out = dir.join("opted-out");
    let s = Scheduler::new(Config {
        ramp: "/bin/false".into(),
        env_file: dir.join("env.json"),
        manifest: manifest.clone(),
        opt_out: opt_out.clone(),
        interval: Duration::from_secs(300),
        log: None,
    });
    let (base, routes, requests) = mock_router().await;
    let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_secs_f64();
    let write = |expected: serde_json::Value, extra: serde_json::Value| {
        let mut m = json!({
            "version": 1, "due_at": now + 3600.0, "unsupported_due_at": now + 3600.0,
            "connections": [{
                "checks": [{"kind": "sync", "url": format!("{base}/v1/client/sync-version")},
                           {"kind": "defaults", "url": format!("{base}/self-service/coding-agent-settings")}],
                "headers": {"Authorization": "Bearer k"}, "expected": expected, "skip_until": null,
            }],
        });
        for (k, v) in extra.as_object().unwrap() {
            m[k] = v.clone();
        }
        std::fs::write(&manifest, m.to_string()).unwrap();
    };
    let route = |path: &str, status: u16, body: &str| {
        routes.lock().unwrap().insert(path.to_string(), (status, body.to_string()));
    };
    route("/v1/client/sync-version", 200, r#"{"version": "v7"}"#);
    route("/self-service/coding-agent-settings", 200, BODY);

    assert_eq!(s.decide().await, Decision::Refresh("no_manifest"));
    write(json!(["v7", "f4c0b80c11a20266"]), json!({}));
    assert_eq!(s.decide().await, Decision::Idle);
    // Existing manifests work without migration, even when the capability
    // changes without changing the opaque version. Only literal true opts in.
    for capability in [json!(true), json!(false), serde_json::Value::Null, json!("true")] {
        route("/v1/client/sync-version", 200, &json!({"version": "v7", "includes_coding_agent_settings": capability}).to_string());
        requests.lock().unwrap().clear();
        assert_eq!(s.decide().await, Decision::Idle);
        assert_eq!(requests.lock().unwrap().len(), if capability == json!(true) { 1 } else { 2 });
    }
    route("/v1/client/sync-version", 200, r#"{"version":"v7","includes_coding_agent_settings":true}"#);
    // A failed Python settings read must still recover without a version change.
    write(json!(["v7", "?"]), json!({}));
    route("/self-service/coding-agent-settings", 503, "");
    assert_eq!(s.decide().await, Decision::Idle);
    route("/self-service/coding-agent-settings", 200, BODY);
    assert_eq!(s.decide().await, Decision::Refresh("changed"));
    write(json!(["v7", "f4c0b80c11a20266"]), json!({}));
    assert_eq!(s.decide().await, Decision::Idle);
    // Coverage never suppresses a changed combined version.
    route("/v1/client/sync-version", 200, r#"{"version":"covered-v8","includes_coding_agent_settings":true}"#);
    assert_eq!(s.decide().await, Decision::Refresh("changed"));
    route("/v1/client/sync-version", 200, r#"{"version":"v7"}"#);
    std::fs::write(&opt_out, "").unwrap();
    assert_eq!(s.decide().await, Decision::Skipped("opted_out"));
    std::fs::remove_file(&opt_out).unwrap();

    // a new model list, or a changed workspace default
    route("/v1/client/sync-version", 200, r#"{"version": "v8"}"#);
    assert_eq!(s.decide().await, Decision::Refresh("changed"));
    // A failed Python recheck may rewrite the old expected value. Rewrites
    // never acknowledge the change; retries are paced independently (unit-tested
    // with an injected time), including if Python failed to write anything.
    assert_eq!(s.decide().await, Decision::Idle);
    write(json!(["v7", "f4c0b80c11a20266"]), json!({"written_at": now + 1.0}));
    assert_eq!(s.decide().await, Decision::Idle);
    route("/v1/client/sync-version", 200, r#"{"version": "v7"}"#);
    route("/self-service/coding-agent-settings", 200, r#"{"harnesses": {}}"#);
    assert_eq!(s.decide().await, Decision::Refresh("changed"));

    // a failed defaults check keeps what ramp recorded; an outage decides nothing
    route("/self-service/coding-agent-settings", 503, "");
    assert_eq!(s.decide().await, Decision::Idle);
    route("/v1/client/sync-version", 502, "");
    assert_eq!(s.decide().await, Decision::Idle);
    route("/v1/client/sync-version", 200, "not json");
    assert_eq!(s.decide().await, Decision::Idle);

    // a control plane without defaults records "none"
    route("/v1/client/sync-version", 200, r#"{"version": "v7"}"#);
    route("/self-service/coding-agent-settings", 404, "");
    write(json!(["v7", "none"]), json!({}));
    assert_eq!(s.decide().await, Decision::Idle);

    // a rejected key: ramp records it; one rejected recently is not asked again
    route("/v1/client/sync-version", 401, "");
    assert_eq!(s.decide().await, Decision::Refresh("key_rejected"));
    let mut m: serde_json::Value = serde_json::from_slice(&std::fs::read(&manifest).unwrap()).unwrap();
    m["connections"][0]["skip_until"] = json!(now + 600.0);
    std::fs::write(&manifest, m.to_string()).unwrap();
    assert_eq!(s.decide().await, Decision::Idle);

    // an older Router without the endpoint follows the session cooldown
    route("/v1/client/sync-version", 404, "");
    write(json!(["v7", "none"]), json!({}));
    assert_eq!(s.decide().await, Decision::Idle);
    write(json!(["v7", "none"]), json!({"unsupported_due_at": now - 1.0}));
    assert_eq!(s.decide().await, Decision::Refresh("unsupported_router"));

    // Python clamps the unsupported deadline to its pending retry. The Rust
    // poll must remain idle throughout backoff, then launch when the retry is due.
    write(json!(["v7", "none"]), json!({"due_at": now + 3600.0, "unsupported_due_at": now + 3600.0}));
    for _ in 0..3 {
        assert_eq!(s.decide().await, Decision::Idle);
    }
    write(json!(["v7", "none"]), json!({"due_at": now - 1.0, "unsupported_due_at": now - 1.0}));
    assert_eq!(s.decide().await, Decision::Refresh("due"));

    // the daily refresh, a due retry, or a pending Cowork pass
    route("/v1/client/sync-version", 200, r#"{"version": "v7"}"#);
    write(json!(["v7", "none"]), json!({"due_at": now - 1.0}));
    assert_eq!(s.decide().await, Decision::Refresh("due"));

    // a Cowork pass runs once Claude Desktop's processes are gone
    write(json!(["v7", "none"]), json!({"cowork_wait_pids": [std::process::id()]}));
    assert_eq!(s.decide().await, Decision::Idle);
    let mut gone = std::process::Command::new("true").spawn().unwrap();
    let pid = gone.id();
    gone.wait().unwrap();
    write(json!(["v7", "none"]), json!({"cowork_wait_pids": [pid]}));
    assert_eq!(s.decide().await, Decision::Refresh("cowork"));

    // never applied: any answer is a change
    write(serde_json::Value::Null, json!({}));
    assert_eq!(s.decide().await, Decision::Refresh("changed"));
    let _ = std::fs::remove_dir_all(&dir);
}
