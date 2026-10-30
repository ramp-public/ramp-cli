//! Exercise the actual HTTP forwarding path, including chunked uploads and live settings changes.

use bytes::Bytes;
use http_body_util::{BodyExt, Full};
use hyper::{Request, Response};
use hyper_util::rt::TokioIo;
use rrh::proxy::{Conn, Server, MAX_BUFFERED_BODY_BYTES};
use std::convert::Infallible;
use std::path::PathBuf;
use std::sync::Arc;
use std::os::unix::fs::PermissionsExt;
use sha2::{Digest, Sha256};

const TOKEN: &str = "ramp-local-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

#[tokio::test]
async fn local_token_is_replaced_and_cannot_select_another_upstream() {
    let (url, task, upstream) = proxy(None).await;
    let client = hyper_util::client::legacy::Client::builder(hyper_util::rt::TokioExecutor::new()).build_http::<Full<Bytes>>();
    for (host, key, expected) in [
        ("api.router.com", TOKEN, 200),
        ("internal-api.router.com", TOKEN, 401),
        ("api.router.com", "real-upstream-key", 401),
        ("api.router.com", "ramp-local-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", 401),
        ("api.router.com", "", 401),
    ] {
        let request = Request::get(format!("{url}/u/{host}/v1/models"))
            .header("authorization", format!("Bearer {key}"))
            .header("x-api-key", "must-not-reach-upstream")
            .body(Full::new(Bytes::new())).unwrap();
        let response = client.request(request).await.unwrap();
        assert_eq!(response.status(), expected);
        if expected == 200 {
            assert_eq!(response.headers().get("x-observed-authorization").unwrap(), "Bearer real-upstream-key");
        }
        let _ = response.into_body().collect().await.unwrap();
    }
    task.abort();
    upstream.abort();
}

struct Cleanup(Option<PathBuf>);
impl Drop for Cleanup {
    fn drop(&mut self) {
        if let Some(path) = &self.0 { let _ = std::fs::remove_dir_all(path); }
    }
}

async fn proxy(settings: Option<PathBuf>) -> (String, tokio::task::JoinHandle<()>, tokio::task::JoinHandle<()>) {
    proxy_peer(settings, true).await
}

async fn proxy_peer(settings: Option<PathBuf>, verified: bool) -> (String, tokio::task::JoinHandle<()>, tokio::task::JoinHandle<()>) {
    let upstream = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let upstream_addr = upstream.local_addr().unwrap();
    let upstream_task = tokio::spawn(async move {
        loop {
            let (stream, _) = upstream.accept().await.unwrap();
            tokio::spawn(async move {
                let echo = hyper::service::service_fn(|req: Request<hyper::body::Incoming>| async move {
                    let authorization = req.headers().get("authorization").cloned().unwrap();
                    assert!(!req.headers().contains_key("x-api-key"));
                    let body = req.into_body().collect().await.unwrap().to_bytes();
                    Ok::<_, Infallible>(Response::builder().header("x-observed-authorization", authorization).body(Full::new(body)).unwrap())
                });
                let _ = hyper::server::conn::http1::Builder::new().serve_connection(TokioIo::new(stream), echo).await;
            });
        }
    });
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let cleanup = Cleanup(settings.is_none().then(|| std::env::temp_dir().join(format!("rrh-auth-{}-{}", std::process::id(), addr.port()))));
    let settings = settings.unwrap_or_else(|| cleanup.0.as_ref().unwrap().join("settings.json"));
    let credentials = settings.parent().unwrap().join("router-agent-credentials");
    std::fs::create_dir_all(&credentials).unwrap();
    if !settings.exists() { std::fs::write(&settings, br#"{"enabled":true}"#).unwrap(); }
    let record = credentials.join(format!("{:x}.json", Sha256::digest(TOKEN.as_bytes())));
    std::fs::write(&record, br#"{"upstream":"api.router.com","api_key":"real-upstream-key"}"#).unwrap();
    std::fs::set_permissions(&record, std::fs::Permissions::from_mode(0o600)).unwrap();
    let mut server = Server::new(addr.port(), None, Some(settings), "test".into(), None);
    Arc::get_mut(&mut server).unwrap().upstream_override = Some(format!("http://{upstream_addr}"));
    let task = tokio::spawn(async move {
        let _cleanup = cleanup;
        loop {
            let (stream, peer) = listener.accept().await.unwrap();
            let server = server.clone();
            tokio::spawn(async move {
                let conn = Conn::new(if verified { peer } else { std::net::SocketAddr::from(([127, 0, 0, 1], 0)) });
                let handler = hyper::service::service_fn(move |req| rrh::proxy::handle(server.clone(), conn.clone(), req));
                let _ = hyper::server::conn::http1::Builder::new().serve_connection(TokioIo::new(stream), handler).await;
            });
        }
    });
    (format!("http://{addr}"), task, upstream_task)
}

#[tokio::test]
async fn unverified_peer_cannot_attribute_request_supplied_paths() {
    let root = std::env::temp_dir().join(format!("rrh-unverified-{}", std::process::id()));
    std::fs::create_dir_all(root.join(".git")).unwrap();
    std::fs::write(root.join(".git/config"), "[remote \"origin\"]\nurl = https://github.com/private/repo\n").unwrap();
    let (url, task, upstream) = proxy_peer(None, false).await;
    let client = hyper_util::client::legacy::Client::builder(hyper_util::rt::TokioExecutor::new()).build_http::<Full<Bytes>>();
    let original = serde_json::to_vec(&serde_json::json!({"metadata":{"cwd":root}, "messages":[{"role":"user", "content":format!("<cwd>{}</cwd>", root.display())}]})).unwrap();
    let request = Request::post(format!("{url}/u/api.router.com/v1/messages")).header("content-type", "application/json").header("authorization", format!("Bearer {TOKEN}"))
        .body(Full::new(Bytes::copy_from_slice(&original))).unwrap();
    let response = client.request(request).await.unwrap();
    assert_eq!(response.status(), 401);
    task.abort();
    upstream.abort();
    std::fs::remove_dir_all(root).unwrap();
}

#[tokio::test]
async fn guardian_request_inherits_parent_turn_repository_over_proxy() {
    let root = std::env::temp_dir().join(format!("rrh-guardian-{}", std::process::id()));
    let repo = root.join("repo");
    let shared = root.join("code");
    std::fs::create_dir_all(repo.join(".git")).unwrap();
    std::fs::create_dir_all(&shared).unwrap();
    std::fs::write(repo.join(".git/config"), "[remote \"origin\"]\nurl = git@github.com:ramp/repo.git\n").unwrap();
    let (url, task, upstream) = proxy(None).await;
    let client = hyper_util::client::legacy::Client::builder(hyper_util::rt::TokioExecutor::new()).build_http::<Full<Bytes>>();
    let parent_metadata = serde_json::json!({"session_id":"root-session","thread_id":"root-thread","turn_id":"parent-turn","workspaces":{repo.to_str().unwrap():{}}}).to_string();
    let parent_body = serde_json::json!({"messages":[{"role":"user","content":format!("<environment_context>\n<cwd>{}</cwd>\n</environment_context>", repo.display())}]});
    let parent = Request::post(format!("{url}/u/api.router.com/v1/messages"))
        .header("content-type", "application/json").header("authorization", format!("Bearer {TOKEN}"))
        .header("x-codex-turn-metadata", parent_metadata)
        .body(Full::new(Bytes::from(serde_json::to_vec(&parent_body).unwrap()))).unwrap();
    assert_eq!(client.request(parent).await.unwrap().status(), 200);

    let guardian_metadata = serde_json::json!({"session_id":"guardian-session","thread_id":"guardian-thread","parent_thread_id":"root-thread","parent_turn_id":"parent-turn","turn_id":"guardian-turn"}).to_string();
    let guardian_body = serde_json::json!({"messages":[{"role":"user","content":"Review the parent action."}],"input":[{"type":"function_call","call_id":"call_ps","name":"exec_command","arguments":serde_json::json!({"cmd":"ps -p 62196 -o pid,command","workdir":shared.to_str().unwrap()}).to_string()},{"type":"function_call_output","call_id":"call_ps","output":"operation not permitted"}]});
    let guardian = Request::post(format!("{url}/u/api.router.com/v1/messages"))
        .header("content-type", "application/json").header("authorization", format!("Bearer {TOKEN}"))
        .header("x-codex-turn-metadata", guardian_metadata)
        .body(Full::new(Bytes::from(serde_json::to_vec(&guardian_body).unwrap()))).unwrap();
    let echoed = client.request(guardian).await.unwrap().into_body().collect().await.unwrap().to_bytes();
    let value: serde_json::Value = serde_json::from_slice(&echoed).unwrap();
    assert_eq!(value["metadata"]["repository"], "ramp/repo");
    assert_eq!(value["metadata"]["repository_source"], "parent_turn_memory");
    task.abort();
    upstream.abort();
    std::fs::remove_dir_all(root).unwrap();
}

#[cfg(target_os = "linux")]
#[tokio::test]
async fn loopback_alias_cannot_borrow_a_trusted_connections_source_port() {
    let (url, task, upstream) = proxy(None).await;
    let addr: std::net::SocketAddr = url.strip_prefix("http://").unwrap().parse().unwrap();
    let trusted = tokio::net::TcpStream::connect(addr).await.unwrap();
    let source_port = trusted.local_addr().unwrap().port();
    // Different local addresses can share a source port. A port-only owner
    // lookup would find the trusted socket for requests on this second socket.
    let alias = tokio::net::TcpSocket::new_v4().unwrap();
    alias.bind(std::net::SocketAddr::from(([127, 0, 0, 2], source_port))).unwrap();
    let stream = alias.connect(addr).await.unwrap();
    let (mut sender, connection) = hyper::client::conn::http1::handshake(TokioIo::new(stream)).await.unwrap();
    let connection = tokio::spawn(connection);
    let request = Request::post("/u/api.router.com/v1/messages")
        .header("host", addr.to_string()).header("content-type", "application/json")
        .body(Full::new(Bytes::from_static(br#"{"messages":[{"role":"user","content":"<cwd>/private/repo</cwd>"}]}"#))).unwrap();
    let response = sender.send_request(request).await.unwrap();
    assert_eq!(response.status(), 403);
    drop(trusted);
    connection.abort();
    task.abort();
    upstream.abort();
}

#[tokio::test]
async fn disabling_a_retained_listener_forwards_unchanged_on_existing_connection() {
    let root = std::env::temp_dir().join(format!("rrh-proxy-{}", std::process::id()));
    let repo = root.join("repo with spaces");
    std::fs::create_dir_all(repo.join(".git")).unwrap();
    std::fs::write(repo.join(".git/config"), "[remote \"origin\"]\nurl = \"git@github.com:o/repo.git\" # comment\n").unwrap();
    // A truncated /repo token must not override /repo with spaces, even as a weak event.
    std::fs::create_dir_all(root.join("repo/.git")).unwrap();
    std::fs::write(root.join("repo/.git/config"), "[remote \"origin\"]\nurl = git@github.com:o/wrong.git\n").unwrap();
    let settings = root.join("settings.json");
    std::fs::write(&settings, br#"{"enabled":true}"#).unwrap();
    let (url, task, upstream) = proxy(Some(settings.clone())).await;
    let client = hyper_util::client::legacy::Client::builder(hyper_util::rt::TokioExecutor::new()).build_http::<Full<Bytes>>();
    let original = serde_json::to_vec(&serde_json::json!({"messages":[{"role":"assistant", "content":[
        {"type":"tool_use", "id":"call", "name":"Bash", "input":{"command":format!("cd \"{}\" && git status", repo.display())}}
    ]}]})).unwrap();
    for enabled in [true, false, true] {
        std::fs::write(&settings, format!("{{\"enabled\":{enabled},\"keep_listening\":true}}")).unwrap();
        let request = Request::post(format!("{url}/u/api.router.com/v1/messages")).header("content-type", "application/json").header("authorization", format!("Bearer {TOKEN}"))
            .body(Full::new(Bytes::copy_from_slice(&original))).unwrap();
        let response = client.request(request).await.unwrap();
        assert_eq!(response.status(), 200);
        let echoed = response.into_body().collect().await.unwrap().to_bytes();
        if enabled {
            let v: serde_json::Value = serde_json::from_slice(&echoed).unwrap();
            assert_eq!(v["metadata"]["repository"], "o/repo");
        } else {
            assert_eq!(echoed.as_ref(), original);
        }
    }
    task.abort();
    upstream.abort();
    std::fs::remove_dir_all(root).unwrap();
}

/// An unknown-length body is sent chunked, rather than given a Content-Length by hyper.
struct Upload {
    remaining: usize,
}

impl hyper::body::Body for Upload {
    type Data = Bytes;
    type Error = Infallible;
    fn poll_frame(mut self: std::pin::Pin<&mut Self>, _: &mut std::task::Context<'_>) -> std::task::Poll<Option<Result<hyper::body::Frame<Bytes>, Self::Error>>> {
        if self.remaining == 0 {
            return std::task::Poll::Ready(None);
        }
        let n = self.remaining.min(1024 * 1024);
        self.remaining -= n;
        std::task::Poll::Ready(Some(Ok(hyper::body::Frame::data(Bytes::from(vec![b'x'; n])))))
    }
}

#[tokio::test]
async fn oversized_chunked_upload_is_forwarded_without_loss() {
    let (url, task, upstream) = proxy(None).await;
    let client = hyper_util::client::legacy::Client::builder(hyper_util::rt::TokioExecutor::new()).build_http::<Upload>();
    let len = MAX_BUFFERED_BODY_BYTES as usize + 12345;
    let request = Request::post(format!("{url}/u/api.router.com/v1/messages")).header("content-type", "application/json").header("authorization", format!("Bearer {TOKEN}"))
        .body(Upload { remaining: len }).unwrap();
    let response = client.request(request).await.unwrap();
    assert_eq!(response.status(), 200);
    let echoed = response.into_body().collect().await.unwrap().to_bytes();
    assert_eq!(echoed.len(), len);
    assert!(echoed.iter().all(|&b| b == b'x'));
    task.abort();
    upstream.abort();
}
