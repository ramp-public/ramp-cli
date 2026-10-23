//! Attribution log: one JSON object per request, written off the request path by a dedicated
//! thread, rotated at 5 MB, mode 0600. Never contains request or response content.

use crate::attr::Attribution;
use serde_json::{json, Map, Value};
use std::fs::OpenOptions;
use std::io::Write;
use std::path::PathBuf;
use std::collections::BTreeMap;
use std::sync::Mutex;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::mpsc;

pub const MAX_LOG_BYTES: u64 = 5 * 1024 * 1024;

pub fn attribution_fields(a: &Attribution, out: &mut Map<String, Value>) {
    let fields = json!({
        "repository": a.repository, "repository_host": a.repository_host, "repository_source": a.repository_source,
        "turn_id": a.turn_id, "turn_id_source": a.turn_id_source, "harness": a.harness,
        "directory_source": a.directory_source, "directory_basename": a.directory_basename, "cwd_repository": a.cwd_repository,
        "client_pid": a.client_pid, "client_command": a.client_command, "error": a.error,
    });
    if let Value::Object(m) = fields {
        out.extend(m);
    }
}

/// Local time as `%Y-%m-%dT%H:%M:%S%z`.
pub fn timestamp() -> String {
    unsafe {
        let t = libc::time(std::ptr::null_mut());
        let mut tm: libc::tm = std::mem::zeroed();
        libc::localtime_r(&t, &mut tm);
        let mut buf = [0u8; 64];
        let n = libc::strftime(buf.as_mut_ptr() as *mut libc::c_char, buf.len(), c"%Y-%m-%dT%H:%M:%S%z".as_ptr(), &tm);
        String::from_utf8_lossy(&buf[..n]).into_owned()
    }
}

pub struct Logger {
    tx: Option<mpsc::Sender<String>>,
    pub requests_seen: AtomicU64,
    pub attribution_sources: Mutex<BTreeMap<String, u64>>,
    pub harnesses: Mutex<BTreeMap<String, u64>>,
}

impl Logger {
    pub fn new(path: Option<PathBuf>) -> Logger {
        let tx = path.map(|path| {
            let (tx, rx) = mpsc::channel::<String>();
            std::thread::Builder::new()
                .name("rrh-log".into())
                .spawn(move || {
                    for line in rx {
                        let _ = (|| -> std::io::Result<()> {
                            if std::fs::metadata(&path).map(|m| m.len() > MAX_LOG_BYTES).unwrap_or(false) {
                                let mut rotated = path.clone().into_os_string();
                                rotated.push(".1");
                                std::fs::rename(&path, rotated)?;
                            }
                            let mut f = OpenOptions::new().create(true).append(true).open(&path)?;
                            use std::os::unix::fs::PermissionsExt;
                            let _ = f.set_permissions(std::fs::Permissions::from_mode(0o600));
                            f.write_all(line.as_bytes())
                        })();
                    }
                })
                .expect("log thread");
            tx
        });
        Logger {
            tx,
            requests_seen: AtomicU64::new(0),
            attribution_sources: Mutex::new(BTreeMap::new()),
            harnesses: Mutex::new(BTreeMap::new()),
        }
    }

    pub fn event(&self, fields: Map<String, Value>) {
        self.requests_seen.fetch_add(1, Ordering::Relaxed);
        if let Some(source) = fields.get("repository_source").and_then(Value::as_str) {
            *self.attribution_sources.lock().unwrap().entry(source.to_string()).or_default() += 1;
        }
        if let Some(harness) = fields.get("harness").and_then(Value::as_str) {
            *self.harnesses.lock().unwrap().entry(harness.to_string()).or_default() += 1;
        }
        if let Some(tx) = &self.tx {
            let mut line = Value::Object(fields).to_string();
            line.push('\n');
            let _ = tx.send(line);
        }
    }
}
