//! tokio + hyper 1.x proxy: `/u/<router host>/<path>` is forwarded to that Router host (allowlist
//! only), model requests get `metadata.repository`, and responses are relayed frame by frame.

use crate::attr::{self, Attribution, SessionMemory};
use crate::git::{RepoResolver, Resolved};
use crate::log::{self, Logger};
use crate::process;
use bytes::{Bytes, BytesMut};
use http_body_util::{combinators::BoxBody, BodyExt, Full};
use hyper::body::{Body, Frame, Incoming};
use hyper::header::{HeaderMap, HeaderName, HeaderValue};
use hyper::{Method, Request, Response, StatusCode};
use hyper_util::client::legacy::{connect::HttpConnector, Client};
use serde_json::json;
use std::collections::HashSet;
use std::path::PathBuf;
use std::pin::Pin;
use std::sync::atomic::Ordering;
use std::sync::{Arc, Mutex};
use std::task::{Context, Poll};
use std::time::{Duration, Instant, SystemTime};

pub const SERVICE_NAME: &str = "ramp-router-agent";
pub const HEALTH_PATH: &str = "/__ramp_router_agent/health";
pub const UPSTREAM_PREFIX: &str = "/u/";
/// Requests up to this size are buffered and tagged; larger ones are streamed through untouched.
pub const MAX_BUFFERED_BODY_BYTES: u64 = 64 * 1024 * 1024;
pub const ROUTER_HOSTS: [&str; 5] = ["api.router.com", "internal-api.router.com", "qa-api.router.com", "qa-internal-api.router.com", "router-api.ramp.com"];
pub const ROUTER_HOST_SUFFIX: &str = ".router.com";
pub const MODEL_PATH_SUFFIXES: [&str; 5] = ["/responses", "/responses/compact", "/messages", "/chat/completions", "/completions"];
const HOP_BY_HOP: [&str; 10] = ["connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length"];
const UPSTREAM_READ_IDLE: Duration = Duration::from_secs(900);
const UPSTREAM_HEADER_TIMEOUT: Duration = Duration::from_secs(900);

type Body2 = BoxBody<Bytes, Box<dyn std::error::Error + Send + Sync>>;
type HttpsClient = Client<hyper_rustls::HttpsConnector<HttpConnector>, Body2>;

/// Once the buffering limit is crossed, replay every frame already read and stream the rest.
/// Retain frames rather than copying a potentially large prefix (including any trailers).
struct Prefixed<B> {
    prefix: std::collections::VecDeque<Frame<Bytes>>,
    rest: B,
}

impl<B: Body<Data = Bytes> + Unpin> Body for Prefixed<B> {
    type Data = Bytes;
    type Error = B::Error;

    fn poll_frame(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Option<Result<Frame<Bytes>, Self::Error>>> {
        if let Some(frame) = self.prefix.pop_front() {
            Poll::Ready(Some(Ok(frame)))
        } else {
            Pin::new(&mut self.rest).poll_frame(cx)
        }
    }
}

enum Buffered<B> {
    Complete(Bytes),
    Streaming(Prefixed<B>, u64),
}

async fn buffer_or_stream<B: Body<Data = Bytes> + Unpin>(mut body: B, limit: u64) -> Result<Buffered<B>, B::Error> {
    let mut prefix = std::collections::VecDeque::new();
    let mut len = 0;
    while let Some(frame) = body.frame().await {
        let frame = frame?;
        len += frame.data_ref().map_or(0, |b| b.len() as u64);
        prefix.push_back(frame);
        if len > limit {
            return Ok(Buffered::Streaming(Prefixed { prefix, rest: body }, len));
        }
    }
    // Keep the common, single-frame request zero-copy.
    if prefix.len() == 1 {
        return Ok(Buffered::Complete(prefix.pop_front().unwrap().into_data().unwrap_or_default()));
    }
    let mut bytes = BytesMut::with_capacity(len as usize);
    for frame in prefix {
        if let Ok(data) = frame.into_data() {
            bytes.extend_from_slice(&data);
        }
    }
    Ok(Buffered::Complete(bytes.freeze()))
}

async fn response_headers<T, E: std::fmt::Debug>(request: impl Future<Output = Result<T, E>>, deadline: Duration) -> Result<T, String> {
    tokio::time::timeout(deadline, request).await.map_err(|_| "upstream response headers timed out".to_string())?.map_err(|e| format!("{e:?}"))
}

/// Request body sent upstream as slices of the client's buffer plus the inserted metadata bytes,
/// so an injected body is never copied a second time.
pub struct Chunks {
    parts: std::collections::VecDeque<Bytes>,
    len: u64,
}

impl Chunks {
    pub fn whole(b: Bytes) -> Self {
        let len = b.len() as u64;
        let mut parts = std::collections::VecDeque::new();
        if len > 0 {
            parts.push_back(b);
        }
        Self { parts, len }
    }
    pub fn spliced(src: &Bytes, plan: &crate::json::Plan) -> Self {
        let mut parts = std::collections::VecDeque::with_capacity(plan.len() * 2 + 1);
        let mut pos = 0;
        for (s, e, rep) in plan {
            if *s > pos {
                parts.push_back(src.slice(pos..*s));
            }
            if !rep.is_empty() {
                parts.push_back(Bytes::copy_from_slice(rep));
            }
            pos = *e;
        }
        if pos < src.len() {
            parts.push_back(src.slice(pos..));
        }
        let len = parts.iter().map(|b| b.len() as u64).sum();
        Self { parts, len }
    }
    pub fn len(&self) -> u64 {
        self.len
    }
}

impl Body for Chunks {
    type Data = Bytes;
    type Error = std::convert::Infallible;
    fn poll_frame(mut self: Pin<&mut Self>, _cx: &mut Context<'_>) -> Poll<Option<Result<Frame<Bytes>, Self::Error>>> {
        match self.parts.pop_front() {
            Some(b) => {
                self.len -= b.len() as u64;
                Poll::Ready(Some(Ok(Frame::data(b))))
            }
            None => Poll::Ready(None),
        }
    }
    fn is_end_stream(&self) -> bool {
        self.parts.is_empty()
    }
    fn size_hint(&self) -> http_body::SizeHint {
        http_body::SizeHint::with_exact(self.len)
    }
}

pub struct Server {
    /// Shadow mode: attribute and log, but forward the original body unchanged.
    pub shadow: bool,
    pub port: u16,
    pub version: String,
    pub logger: Logger,
    pub settings_path: Option<PathBuf>,
    pub resolver: RepoResolver,
    pub sessions: SessionMemory,
    /// Per-conversation state so a request only parses the messages added since the last one.
    pub conversations: attr::ConvCache,
    /// RRH_VERIFY_INCREMENTAL: also attribute every incremental request in full and report differences.
    pub verify_incremental: bool,
    pub started_at: SystemTime,
    /// Test hook (RRH_TEST_UPSTREAM): allowed hosts are sent here instead.
    pub upstream_override: Option<String>,
    /// Background sync, told about agent activity so a new session gets fresh setups soon.
    pub scheduler: Option<Arc<crate::sync::Scheduler>>,
    client: HttpsClient,
    extra_hosts: Mutex<(Option<SystemTime>, HashSet<String>)>,
}

impl Server {
    pub fn new(port: u16, log_path: Option<PathBuf>, settings_path: Option<PathBuf>, version: String, scheduler: Option<Arc<crate::sync::Scheduler>>) -> Arc<Server> {
        let _ = rustls::crypto::ring::default_provider().install_default();
        let mut http = HttpConnector::new();
        http.enforce_http(false);
        http.set_connect_timeout(Some(Duration::from_secs(15)));
        http.set_nodelay(true);
        // HTTP/2 with Router when it offers it (ALPN): one warm multiplexed connection, and Router
        // answers ~80 ms sooner over h2 than h1.
        let builder = hyper_rustls::HttpsConnectorBuilder::new().with_tls_config(crate::tls::client_config()).https_or_http();
        let https = if std::env::var("RRH_UPSTREAM_HTTP1").is_err() {
            builder.enable_all_versions().wrap_connector(http)
        } else {
            builder.enable_http1().wrap_connector(http)
        };
        let client = Client::builder(hyper_util::rt::TokioExecutor::new())
            .pool_idle_timeout(Duration::from_secs(30))
            .pool_max_idle_per_host(32)
            .pool_timer(hyper_util::rt::TokioTimer::new())
            .build(https);
        Arc::new(Server {
            shadow: std::env::var("RRH_SHADOW").map(|v| v == "1").unwrap_or(false),
            port,
            version,
            logger: Logger::new(log_path),
            settings_path,
            resolver: RepoResolver::default(),
            sessions: SessionMemory::default(),
            conversations: attr::ConvCache::default(),
            verify_incremental: std::env::var("RRH_VERIFY_INCREMENTAL").is_ok_and(|v| v == "1"),
            started_at: SystemTime::now(),
            upstream_override: std::env::var("RRH_TEST_UPSTREAM").ok().filter(|s| !s.is_empty()),
            scheduler,
            client,
            extra_hosts: Mutex::new((None, HashSet::new())),
        })
    }

    fn extra_hosts(&self) -> HashSet<String> {
        let Some(p) = &self.settings_path else { return HashSet::new() };
        let Ok(mtime) = std::fs::metadata(p).and_then(|m| m.modified()) else { return HashSet::new() };
        let mut g = self.extra_hosts.lock().unwrap();
        if g.0 != Some(mtime) {
            g.1 = read_extra_hosts(p);
            g.0 = Some(mtime);
        }
        g.1.clone()
    }

    fn upstream_for(&self, netloc: &str) -> Option<String> {
        let up = default_upstream(netloc, &self.extra_hosts())?;
        Some(self.upstream_override.clone().unwrap_or(up))
    }

    fn tagging_enabled(&self) -> bool {
        // With a settings file, fail closed: a retained listener must forward without
        // attribution as soon as tagging is disabled, even on existing connections.
        self.settings_path.as_ref().map_or(true, |p| {
            std::fs::read(p).ok().and_then(|b| serde_json::from_slice::<serde_json::Value>(&b).ok())
                .is_some_and(|v| v.get("enabled").and_then(|v| v.as_bool()) == Some(true))
        })
    }
}

pub fn read_extra_hosts(p: &std::path::Path) -> HashSet<String> {
    let Ok(text) = std::fs::read(p) else { return HashSet::new() };
    let Ok(v) = serde_json::from_slice::<serde_json::Value>(&text) else { return HashSet::new() };
    v.get("hosts")
        .and_then(|h| h.as_array())
        .map(|a| a.iter().filter_map(|h| h.as_str()).filter(|h| !h.is_empty()).map(|h| h.to_lowercase()).collect())
        .unwrap_or_default()
}

pub fn router_host_allowed(host: &str, extra: &HashSet<String>) -> bool {
    let host = host.to_lowercase();
    let host = host.trim_end_matches('.');
    ROUTER_HOSTS.contains(&host) || host.ends_with(ROUTER_HOST_SUFFIX) || extra.contains(host)
}

/// `/u/<host>/<rest>?q` -> (host, `/<rest>?q`)
pub fn split_upstream_path(path: &str, query: Option<&str>) -> Option<(String, String)> {
    let rest = path.strip_prefix(UPSTREAM_PREFIX)?;
    let (netloc, tail) = rest.split_once('/').unwrap_or((rest, ""));
    if netloc.is_empty() {
        return None;
    }
    let mut up = format!("/{tail}");
    if let Some(q) = query.filter(|q| !q.is_empty()) {
        up.push('?');
        up.push_str(q);
    }
    Some((netloc.to_string(), up))
}

/// urlsplit("https://" + netloc) -> hostname/port, then the allowlist.
pub fn default_upstream(netloc: &str, extra: &HashSet<String>) -> Option<String> {
    let hostport = netloc.rsplit('@').next().unwrap_or(netloc);
    let (host, port) = if let Some(rest) = hostport.strip_prefix('[') {
        let (h, after) = rest.split_once(']')?;
        let port = after.strip_prefix(':');
        (h.to_string(), port)
    } else {
        match hostport.rsplit_once(':') {
            Some((h, p)) => (h.to_string(), Some(p)),
            None => (hostport.to_string(), None),
        }
    };
    let port: Option<u16> = match port {
        None | Some("") => None,
        Some(p) => {
            if !p.bytes().all(|b| b.is_ascii_digit()) {
                return None;
            }
            Some(p.parse::<u32>().ok().filter(|&v| v <= 65535)? as u16)
        }
    };
    let host = host.to_lowercase();
    if host.is_empty() || !router_host_allowed(&host, extra) {
        return None;
    }
    Some(match port {
        Some(p) if p != 0 => format!("https://{host}:{p}"),
        _ => format!("https://{host}"),
    })
}

fn full(b: impl Into<Bytes>) -> Body2 {
    Full::new(b.into()).map_err(|never| match never {}).boxed()
}

fn json_response(status: StatusCode, body: String, close: bool) -> Response<Body2> {
    let mut r = Response::new(full(body.clone()));
    *r.status_mut() = status;
    let h = r.headers_mut();
    h.insert("content-type", HeaderValue::from_static("application/json"));
    h.insert("content-length", HeaderValue::from(body.len()));
    if close {
        h.insert("connection", HeaderValue::from_static("close"));
    }
    r
}

fn error_json(message: &str) -> String {
    json!({"error": {"message": message}}).to_string()
}

/// Per-connection state: the calling process is looked up once per connection.
pub struct Conn {
    pub peer: std::net::SocketAddr,
    client: Mutex<Option<(i64, String)>>,
}

impl Conn {
    pub fn new(peer: std::net::SocketAddr) -> Arc<Conn> {
        Arc::new(Conn { peer, client: Mutex::new(None) })
    }
}

struct AttrCtx<'a> {
    server: &'a Server,
    conn: &'a Conn,
    headers: &'a HeaderMap,
}

impl attr::Ctx for AttrCtx<'_> {
    fn header(&self, name: &str) -> Option<String> {
        self.headers.get(name).map(|v| String::from_utf8_lossy(v.as_bytes()).into_owned())
    }
    fn calling_process(&self, att: &mut Attribution) -> Option<String> {
        let mut c = self.conn.client.lock().unwrap();
        if c.is_none() {
            *c = process::find(self.server.port, self.conn.peer.port());
        }
        let (pid, cmd) = c.clone()?;
        att.client_pid = Some(pid);
        att.client_command = Some(cmd);
        drop(c);
        process::cwd(pid)
    }
    fn resolver(&self) -> &RepoResolver {
        &self.server.resolver
    }
    fn session_get(&self, id: &str) -> Option<(Resolved, String)> {
        self.server.sessions.get(id)
    }
    fn session_put(&self, id: &str, v: (Resolved, String)) {
        self.server.sessions.put(id, v.0, v.1)
    }
}

#[derive(Default)]
pub struct AttrOut {
    pub attribution: Attribution,
    pub plan: Option<crate::json::Plan>,
    pub incremental: bool,
    pub mismatch: Option<String>,
}

/// Attribution + injection plan for one model request; never fails the request. `conversations`
/// None disables the incremental path (every request parsed in full).
pub fn attribute_body(body: &[u8], ctx: &mut dyn attr::Ctx, conversations: Option<&attr::ConvCache>, verify: bool) -> AttrOut {
    let empty = attr::ConvCache::default();
    let failed = |name: &str| AttrOut { attribution: Attribution { error: Some(format!("attribution_failed:{name}")), ..Default::default() }, ..Default::default() };
    match std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| attr::attribute_bytes(body, ctx, conversations.unwrap_or(&empty), verify))) {
        Ok(Ok(r)) => AttrOut { attribution: r.attribution, plan: r.plan, incremental: r.incremental, mismatch: r.mismatch },
        Ok(Err(name)) => failed(name),
        Err(_) => failed("panic"),
    }
}

struct LogCtx {
    server: Arc<Server>,
    started: Instant,
    method: String,
    path: String,
    model_call: bool,
    status: Option<u16>,
    attribution_ms: f64,
    gateway_client: Option<String>,
    user_agent: String,
    body_bytes: usize,
    injected: bool,
    skipped: Option<String>,
    attribution: Attribution,
}

impl LogCtx {
    fn emit(self, upstream_error: Option<String>) {
        let latency = (self.started.elapsed().as_secs_f64() * 1000.0).round() as i64;
        let serde_json::Value::Object(mut f) = json!({
            "ts": log::timestamp(), "method": self.method, "path": self.path, "model_call": self.model_call,
            "status": self.status, "latency_ms": latency, "attribution_ms": self.attribution_ms,
            "gateway_client": self.gateway_client, "user_agent": self.user_agent, "body_bytes": self.body_bytes,
            "injected": self.injected, "skipped": self.skipped, "upstream_error": upstream_error,
        }) else { unreachable!() };
        log::attribution_fields(&self.attribution, &mut f);
        self.server.logger.event(f);
    }
}

/// Upstream body relayed frame by frame; logs the request when the stream ends, fails, or the
/// client goes away. Enforces the upstream read-idle timeout.
struct Relay {
    inner: Incoming,
    log: Option<LogCtx>,
    idle: Pin<Box<tokio::time::Sleep>>,
    /// frames since the idle timer was last armed; the clock is only read when the timer fires
    frames: u64,
}

impl Body for Relay {
    type Data = Bytes;
    type Error = Box<dyn std::error::Error + Send + Sync>;
    fn poll_frame(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Option<Result<Frame<Bytes>, Self::Error>>> {
        let this = &mut *self;
        match Pin::new(&mut this.inner).poll_frame(cx) {
            Poll::Ready(Some(Ok(f))) => {
                this.frames += 1;
                // A body with a known length can end on its last data frame; hyper then drops the
                // body without polling for `None`, which must not be logged as a disconnect.
                if this.inner.is_end_stream() {
                    if let Some(l) = this.log.take() {
                        l.emit(None);
                    }
                }
                Poll::Ready(Some(Ok(f)))
            }
            Poll::Ready(Some(Err(e))) => {
                if let Some(l) = this.log.take() {
                    l.emit(Some(format!("RemoteProtocolError: {e}").chars().take(300).collect()));
                }
                Poll::Ready(Some(Err(Box::new(e))))
            }
            Poll::Ready(None) => {
                if let Some(l) = this.log.take() {
                    l.emit(None);
                }
                Poll::Ready(None)
            }
            Poll::Pending => {
                while this.idle.as_mut().poll(cx).is_ready() {
                    if this.frames > 0 {
                        // data arrived since the timer was armed: not idle, re-arm and keep waiting
                        this.frames = 0;
                        this.idle.as_mut().reset(tokio::time::Instant::now() + UPSTREAM_READ_IDLE);
                        continue;
                    }
                    if let Some(l) = this.log.take() {
                        l.emit(Some("ReadTimeout: upstream read timed out".into()));
                    }
                    return Poll::Ready(Some(Err("upstream read timed out".into())));
                }
                Poll::Pending
            }
        }
    }
    fn is_end_stream(&self) -> bool {
        self.inner.is_end_stream()
    }
    fn size_hint(&self) -> http_body::SizeHint {
        self.inner.size_hint()
    }
}

impl Drop for Relay {
    fn drop(&mut self) {
        if let Some(l) = self.log.take() {
            if self.inner.is_end_stream() {
                l.emit(None);
            } else {
                l.emit(Some("BrokenPipeError: client went away".into()));
            }
        }
    }
}

use std::future::Future;

fn local_host_header(h: &HeaderMap) -> bool {
    let host = h.get("host").map(|v| String::from_utf8_lossy(v.as_bytes()).trim().to_lowercase()).unwrap_or_default();
    let name = host.split(':').next().unwrap_or("");
    name == "127.0.0.1" || name == "localhost"
}

/// The harness session a request belongs to, from the headers each harness sends.
fn session_of(h: &HeaderMap) -> Option<String> {
    let get = |n: &str| h.get(n).and_then(|v| v.to_str().ok()).filter(|v| !v.is_empty()).map(String::from);
    get("x-claude-code-session-id")
        .or_else(|| {
            let md: serde_json::Value = serde_json::from_str(&get("x-codex-turn-metadata")?).ok()?;
            md.get("session_id").and_then(|s| s.as_str()).map(String::from)
        })
        .or_else(|| get("x-opencode-session-id"))
        .or_else(|| get("x-session-id"))
}

/// HMAC-SHA256, using the fixed-size, user-private health key. Only the proof is sent over HTTP.
fn health_proof(token: &[u8], message: &[u8]) -> String {
    use sha2::{Digest, Sha256};
    let mut inner = [0x36; 64];
    let mut outer = [0x5c; 64];
    for (i, &b) in token.iter().enumerate() {
        inner[i] ^= b;
        outer[i] ^= b;
    }
    let mut hash = Sha256::new();
    hash.update(inner);
    hash.update(message);
    let digest = hash.finalize();
    let mut hash = Sha256::new();
    hash.update(outer);
    hash.update(digest);
    format!("{:x}", hash.finalize())
}

fn health(server: &Server, headers: &HeaderMap) -> Response<Body2> {
    let up = server.started_at.elapsed().map(|d| d.as_secs()).unwrap_or(0);
    let proof = (|| {
        let nonce = headers.get("x-ramp-health-challenge")?.to_str().ok()?;
        if nonce.len() != 64 || !nonce.bytes().all(|b| b.is_ascii_hexdigit()) {
            return None;
        }
        let path = server.settings_path.as_ref()?.with_file_name("router-agent-health-token");
        let token = crate::auth::read_private(&path)?;
        if token.len() != 64 || !token.iter().all(|b| b.is_ascii_hexdigit()) {
            return None;
        }
        // Bind the proof to this listener, so a different port cannot relay the challenge.
        Some(health_proof(&token, format!("{SERVICE_NAME}:{}:{nonce}", server.port).as_bytes()))
    })();
    let body = json!({
        "ok": true, "service": SERVICE_NAME, "version": server.version, "pid": std::process::id(), "uptime_seconds": up,
        "requests_seen": server.logger.requests_seen.load(Ordering::Relaxed), "implementation": "rust", "tls_trust": crate::tls::trust_source(), "sync": server.scheduler.is_some(),
        "attribution_sources": server.logger.attribution_sources.lock().unwrap().clone(),
        "harnesses": server.logger.harnesses.lock().unwrap().clone(),
        "health_proof": proof, "local_auth": true,
    });
    json_response(StatusCode::OK, body.to_string(), false)
}

fn not_implemented(method: &Method) -> Response<Body2> {
    let body = error_json(&format!("unsupported method {}", method.as_str()));
    let mut r = json_response(StatusCode::NOT_IMPLEMENTED, body, true);
    if method == Method::HEAD {
        *r.body_mut() = full(Bytes::new());
    }
    r
}

pub async fn handle(server: Arc<Server>, conn: Arc<Conn>, req: Request<Incoming>) -> Result<Response<Body2>, std::convert::Infallible> {
    // The owner lookup matches 127.0.0.1 socket tuples. Other loopback aliases
    // can reuse a same-user connection's source port and must never reach it.
    if conn.peer.ip() != std::net::IpAddr::V4(std::net::Ipv4Addr::LOCALHOST) {
        return Ok(json_response(StatusCode::FORBIDDEN, error_json("the Ramp Router agent only accepts 127.0.0.1 peers"), true));
    }
    let started = Instant::now();
    let method = req.method().clone();
    let path_only = req.uri().path().to_string();
    if method == Method::GET && path_only == HEALTH_PATH {
        return Ok(health(&server, req.headers()));
    }
    // HEAD (Claude Code's connectivity probe) is answered locally: Router returns 404 for it anyway,
    // and forwarding only adds a round trip to startup.
    let proxied = matches!(method, Method::GET | Method::POST | Method::PUT | Method::PATCH | Method::DELETE);
    if !proxied {
        return Ok(not_implemented(&method));
    }
    let target = split_upstream_path(&path_only, req.uri().query());
    let upstream = target.as_ref().and_then(|t| server.upstream_for(&t.0));
    let browser = req.headers().contains_key("origin"); // never serve web pages (CSRF, DNS rebinding)
    if !local_host_header(req.headers()) || target.is_none() || upstream.is_none() || browser {
        let status = if target.is_some() { StatusCode::FORBIDDEN } else { StatusCode::NOT_FOUND };
        return Ok(json_response(status, error_json("the Ramp Router agent only forwards to Router"), true));
    }
    let (upstream_host, upstream_path) = target.unwrap();
    let upstream = upstream.unwrap();
    // Authenticate before reading bodies, resolving paths, or forwarding any
    // request. An intercepted local token is not usable by another OS user.
    let token = req.headers().get("authorization").and_then(|h| h.to_str().ok())
        .and_then(|h| h.strip_prefix("Bearer ")).unwrap_or("").to_owned();
    let auth_server = server.clone();
    let peer_port = conn.peer.port();
    let authorization = tokio::task::spawn_blocking(move || {
        if !process::same_user(auth_server.port, peer_port) {
            return None;
        }
        crate::auth::upstream_authorization(auth_server.settings_path.as_deref()?, &token, &upstream_host)
    }).await.ok().flatten();
    let Some(authorization) = authorization else {
        return Ok(json_response(StatusCode::UNAUTHORIZED, error_json("invalid local Router credential or unverified peer; run ramp router configure"), true));
    };
    let (parts, incoming) = req.into_parts();
    let headers = parts.headers;
    let up_path_only = upstream_path.split('?').next().unwrap_or("").to_string();
    let model_call = method == Method::POST && MODEL_PATH_SUFFIXES.iter().any(|s| up_path_only.ends_with(s));
    if let (true, Some(s)) = (model_call, &server.scheduler) {
        s.activity(session_of(&headers).as_deref());
    }

    let declared_len: Option<u64> = headers.get("content-length").and_then(|v| v.to_str().ok()).and_then(|v| v.trim().parse().ok());
    let tagging_enabled = server.tagging_enabled();
    let stream_through = declared_len.is_some_and(|l| l > MAX_BUFFERED_BODY_BYTES) || !tagging_enabled;

    let mut log_ctx = LogCtx {
        server: server.clone(),
        started,
        method: method.as_str().to_string(),
        path: up_path_only.clone(),
        model_call,
        status: None,
        attribution_ms: 0.0,
        gateway_client: headers.get("x-gateway-client").map(|v| String::from_utf8_lossy(v.as_bytes()).into_owned()),
        user_agent: headers.get("user-agent").map(|v| String::from_utf8_lossy(v.as_bytes()).chars().take(120).collect()).unwrap_or_default(),
        body_bytes: 0,
        injected: false,
        skipped: None,
        attribution: Attribution::default(),
    };

    // upstream request: client headers minus hop-by-hop; identity encoding unless the client asked
    let mut up = Request::builder().method(method.clone()).uri(format!("{upstream}{upstream_path}"));
    for (name, value) in headers.iter() {
        let n = name.as_str();
        if HOP_BY_HOP.contains(&n) || matches!(n, "accept-encoding" | "authorization" | "x-api-key") {
            continue;
        }
        up = up.header(name, value);
    }
    up = up.header("authorization", authorization);
    let ae = headers.get("accept-encoding").cloned().unwrap_or(HeaderValue::from_static("identity"));
    up = up.header("accept-encoding", ae);

    let buffered = if stream_through {
        Buffered::Streaming(Prefixed { prefix: Default::default(), rest: incoming }, 0)
    } else {
        match buffer_or_stream(incoming, MAX_BUFFERED_BODY_BYTES).await {
            Ok(b) => b,
            Err(e) => {
                log_ctx.emit(Some(format!("ClientDisconnected: {e}")));
                return Ok(json_response(StatusCode::BAD_REQUEST, error_json("could not read the request body"), true));
            }
        }
    };
    let upstream_body = match buffered {
        Buffered::Streaming(body, received) => {
            log_ctx.skipped = Some(if tagging_enabled { "body_too_large" } else { "tagging_disabled" }.into());
            log_ctx.body_bytes = declared_len.unwrap_or(received) as usize;
            if let Some(len) = declared_len {
                up = up.header("content-length", len);
            }
            body.map_err(|e| -> Box<dyn std::error::Error + Send + Sync> { Box::new(e) }).boxed()
        }
        Buffered::Complete(body) => {
            let mut upstream_body: Option<Chunks> = None;
            if model_call {
                let ct = headers.get("content-type").map(|v| String::from_utf8_lossy(v.as_bytes()).into_owned()).unwrap_or_default();
                if !ct.contains("json") {
                    log_ctx.skipped = Some("not_json".into());
                } else {
                    let t0 = Instant::now();
                    let srv = server.clone();
                    let cn = conn.clone();
                    let hdrs = headers.clone();
                    let b2 = body.clone();
                    let r = tokio::task::spawn_blocking(move || {
                        // Peer ownership was checked before credential lookup and forwarding.
                        let mut ctx = AttrCtx { server: &srv, conn: &cn, headers: &hdrs };
                        attribute_body(&b2, &mut ctx, Some(&srv.conversations), srv.verify_incremental)
                    })
                    .await
                    .unwrap_or_else(|_| AttrOut { attribution: Attribution { error: Some("attribution_failed:panic".into()), ..Default::default() }, ..Default::default() });
                    if let Some(m) = &r.mismatch {
                        eprintln!("rrh-verify mismatch path={up_path_only}: {m}");
                    }
                    if let Some(plan) = r.plan {
                        if server.shadow {
                            log_ctx.skipped = Some("shadow".into());
                        } else {
                            upstream_body = Some(Chunks::spliced(&body, &plan));
                            log_ctx.injected = true;
                        }
                    }
                    log_ctx.attribution = r.attribution;
                    log_ctx.attribution_ms = (t0.elapsed().as_secs_f64() * 10_000.0).round() / 10.0;
                }
            }
            let upstream_body = upstream_body.unwrap_or_else(|| Chunks::whole(body));
            log_ctx.body_bytes = upstream_body.len() as usize;
            up = up.header("content-length", upstream_body.len());
            upstream_body.map_err(|never| match never {}).boxed()
        }
    };
    let response = match up.body(upstream_body) {
        Ok(r) => response_headers(server.client.request(r), UPSTREAM_HEADER_TIMEOUT).await,
        Err(e) => Err(e.to_string()),
    };

    let t_upstream_headers = started.elapsed();
    let resp = match response {
        Ok(r) => {
            if std::env::var("RRH_DEBUG").is_ok() {
                let arrival_unix_ms = SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_millis()).unwrap_or(0) - started.elapsed().as_millis();
                eprintln!("rrh-debug arrival_unix_ms={arrival_unix_ms} run={} path={} version={:?} status={} upstream_headers_ms={:.1} attribution_ms={:.2} body_bytes={}",
                    headers.get("x-bench-run").and_then(|v| v.to_str().ok()).unwrap_or("-"), up_path_only, r.version(), r.status().as_u16(), t_upstream_headers.as_secs_f64() * 1000.0, log_ctx.attribution_ms, log_ctx.body_bytes);
            }
            r
        }
        Err(e) => {
            log_ctx.emit(Some(format!("ConnectError: {e}").chars().take(300).collect()));
            return Ok(json_response(StatusCode::BAD_GATEWAY, error_json("Router could not be reached"), true));
        }
    };
    let (rparts, rbody) = resp.into_parts();
    let status = rparts.status;
    log_ctx.status = Some(status.as_u16());
    let mut out = Response::builder().status(status);
    for (name, value) in rparts.headers.iter() {
        if HOP_BY_HOP.contains(&name.as_str()) {
            continue;
        }
        out = out.header(name, value);
    }
    let no_body = status == StatusCode::NO_CONTENT || status == StatusCode::NOT_MODIFIED || status.is_informational();
    if let (false, Some(len)) = (no_body, rparts.headers.get("content-length")) {
        out = out.header(HeaderName::from_static("content-length"), len);
    }
    let relay = Relay { inner: rbody, log: Some(log_ctx), idle: Box::pin(tokio::time::sleep(UPSTREAM_READ_IDLE)), frames: 0 };
    Ok(out.body(relay.boxed()).unwrap_or_else(|_| json_response(StatusCode::BAD_GATEWAY, error_json("Router could not be reached"), true)))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::VecDeque;
    use std::sync::atomic::AtomicUsize;

    struct Frames {
        frames: VecDeque<Result<Frame<Bytes>, std::io::Error>>,
        polls: Arc<AtomicUsize>,
    }

    impl Body for Frames {
        type Data = Bytes;
        type Error = std::io::Error;
        fn poll_frame(mut self: Pin<&mut Self>, _: &mut Context<'_>) -> Poll<Option<Result<Frame<Bytes>, Self::Error>>> {
            self.polls.fetch_add(1, Ordering::Relaxed);
            Poll::Ready(self.frames.pop_front())
        }
    }

    fn frames() -> Frames {
        let mut trailers = HeaderMap::new();
        trailers.insert("x-trailer", HeaderValue::from_static("kept"));
        Frames {
            frames: VecDeque::from([
                Ok(Frame::data(Bytes::from_static(b"abcd"))),
                Ok(Frame::data(Bytes::from_static(b"efgh"))),
                Ok(Frame::data(Bytes::from_static(b"ijkl"))),
                Ok(Frame::trailers(trailers)),
            ]),
            polls: Arc::new(AtomicUsize::new(0)),
        }
    }

    #[tokio::test]
    async fn chunked_body_crossing_limit_streams_prefix_and_unread_frames() {
        let body = frames();
        let polls = body.polls.clone();
        let Buffered::Streaming(body, received) = buffer_or_stream(body, 4).await.unwrap() else { panic!("buffer limit not enforced") };
        assert_eq!(received, 8);
        assert_eq!(polls.load(Ordering::Relaxed), 2); // remainder has not been read
        let collected = body.collect().await.unwrap();
        assert_eq!(collected.trailers().unwrap()["x-trailer"], "kept");
        assert_eq!(collected.to_bytes(), "abcdefghijkl");
    }

    #[tokio::test]
    async fn body_at_limit_is_buffered_and_errors_propagate() {
        let Buffered::Complete(bytes) = buffer_or_stream(frames(), 12).await.unwrap() else { panic!("exact limit should fit") };
        assert_eq!(bytes, "abcdefghijkl");
        let mut body = frames();
        body.frames.push_front(Err(std::io::Error::other("client disconnected")));
        assert!(buffer_or_stream(body, 12).await.is_err());
        let mut body = frames();
        body.frames.push_back(Err(std::io::Error::other("client disconnected")));
        let Buffered::Streaming(body, _) = buffer_or_stream(body, 4).await.unwrap() else { panic!() };
        assert!(body.collect().await.is_err());
    }

    #[tokio::test]
    async fn response_headers_have_a_deadline() {
        let deadline = Duration::from_millis(5);
        let stalled = std::future::pending::<Result<(), std::io::Error>>();
        assert_eq!(response_headers(stalled, deadline).await.unwrap_err(), "upstream response headers timed out");
        assert_eq!(response_headers(async { Ok::<_, std::io::Error>(42) }, deadline).await.unwrap(), 42);
        assert!(response_headers(async { Err::<(), _>("connect failed") }, deadline).await.unwrap_err().contains("connect failed"));
    }

    #[test]
    fn health_hmac_matches_rfc4231() {
        assert_eq!(health_proof(&[0x0b; 20], b"Hi There"), "b0344c61d8db38535ca8afceaf0bf12b881dc200c9833da726e9376c2e32cff7");
    }

    #[test]
    fn retained_listener_observes_tagging_disabled_without_restart() {
        let path = std::env::temp_dir().join(format!("rrh-settings-{}.json", std::process::id()));
        std::fs::write(&path, br#"{"enabled":true}"#).unwrap();
        let server = Server::new(0, None, Some(path.clone()), "test".into(), None);
        assert!(server.tagging_enabled());
        std::fs::write(&path, br#"{"enabled":false,"keep_listening":true}"#).unwrap();
        assert!(!server.tagging_enabled());
        std::fs::write(&path, b"bad json").unwrap();
        assert!(!server.tagging_enabled());
        std::fs::remove_file(path).unwrap();
        assert!(!server.tagging_enabled());
    }
}
