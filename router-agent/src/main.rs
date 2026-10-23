//! ramp-router-agent: the Ramp Router background agent, run by launchd or systemd.
//!
//!   ramp-router-agent [serve]
//!       [--sync-ramp <ramp> --sync-env-file <file> --sync-manifest <file> --sync-opt-out <file>
//!        --sync-interval <seconds> --sync-log <file>]
//!       [--port 47317 --settings <file> --log <file> [--shadow]]
//!
//! The --sync-* options keep Router setups fresh; --port also serves repository tagging.

use rrh::proxy::{self, Conn, Server};
use rrh::sync::{self, Scheduler};
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

fn arg(args: &[String], name: &str) -> Option<String> {
    args.iter().position(|a| a == name).and_then(|i| args.get(i + 1)).cloned()
}

/// A listening socket handed over by the service manager, so restarts never refuse connections.
fn inherited_listener() -> Option<std::net::TcpListener> {
    use std::os::fd::FromRawFd;
    // systemd socket activation
    if std::env::var("LISTEN_PID").ok().and_then(|p| p.parse::<u32>().ok()) == Some(std::process::id())
        && std::env::var("LISTEN_FDS").ok().and_then(|n| n.parse::<i32>().ok()).unwrap_or(0) >= 1
    {
        return Some(unsafe { std::net::TcpListener::from_raw_fd(3) });
    }
    #[cfg(target_os = "macos")]
    {
        extern "C" {
            fn launch_activate_socket(name: *const libc::c_char, fds: *mut *mut libc::c_int, cnt: *mut libc::size_t) -> libc::c_int;
        }
        if std::env::var("XPC_SERVICE_NAME").map(|n| n == "com.ramp.router").unwrap_or(false) {
            let mut fds: *mut libc::c_int = std::ptr::null_mut();
            let mut cnt: libc::size_t = 0;
            let rc = unsafe { launch_activate_socket(c"Listeners".as_ptr(), &mut fds, &mut cnt) };
            if rc == 0 && cnt >= 1 && !fds.is_null() {
                let fd = unsafe { *fds };
                unsafe { libc::free(fds as *mut libc::c_void) };
                return Some(unsafe { std::net::TcpListener::from_raw_fd(fd) });
            }
        }
    }
    None
}

/// 127.0.0.1:port with SO_REUSEADDR and a 1024-connection backlog (std's default is 128).
fn bind(port: u16) -> std::io::Result<std::net::TcpListener> {
    use std::os::fd::FromRawFd;
    unsafe {
        let fd = libc::socket(libc::AF_INET, libc::SOCK_STREAM, 0);
        if fd < 0 {
            return Err(std::io::Error::last_os_error());
        }
        let listener = std::net::TcpListener::from_raw_fd(fd); // closes fd on error paths
        let one: libc::c_int = 1;
        libc::setsockopt(fd, libc::SOL_SOCKET, libc::SO_REUSEADDR, &one as *const _ as *const libc::c_void, std::mem::size_of_val(&one) as libc::socklen_t);
        let mut addr: libc::sockaddr_in = std::mem::zeroed();
        addr.sin_family = libc::AF_INET as libc::sa_family_t;
        addr.sin_port = port.to_be();
        addr.sin_addr.s_addr = u32::from_be_bytes([127, 0, 0, 1]).to_be();
        #[cfg(target_os = "macos")]
        {
            addr.sin_len = std::mem::size_of::<libc::sockaddr_in>() as u8;
        }
        if libc::bind(fd, &addr as *const _ as *const libc::sockaddr, std::mem::size_of_val(&addr) as libc::socklen_t) != 0 || libc::listen(fd, 1024) != 0 {
            return Err(std::io::Error::last_os_error());
        }
        Ok(listener)
    }
}

fn scheduler(args: &[String]) -> Option<Arc<Scheduler>> {
    let manifest = arg(args, "--sync-manifest")?;
    let need = |name: &str| {
        arg(args, name).map(PathBuf::from).unwrap_or_else(|| {
            eprintln!("ramp-router-agent: --sync-manifest needs {name}");
            std::process::exit(2);
        })
    };
    Some(Scheduler::new(sync::Config {
        ramp: need("--sync-ramp"),
        env_file: need("--sync-env-file"),
        manifest: PathBuf::from(manifest),
        opt_out: need("--sync-opt-out"),
        interval: Duration::from_secs(arg(args, "--sync-interval").and_then(|s| s.parse().ok()).filter(|&s: &u64| s >= 60).unwrap_or(300)),
        log: arg(args, "--sync-log").map(PathBuf::from),
    }))
}

async fn serve(args: Vec<String>) {
    let scheduler = scheduler(&args);
    if let Some(s) = &scheduler {
        tokio::spawn(s.clone().run());
    }
    let Some(port) = arg(&args, "--port").and_then(|p| p.parse::<u16>().ok()) else {
        if scheduler.is_none() {
            eprintln!("ramp-router-agent: nothing to do: pass --sync-manifest and/or --port (see --help)");
            std::process::exit(2);
        }
        // background sync only: no listener
        std::future::pending::<()>().await;
        return;
    };
    let log = arg(&args, "--log").map(PathBuf::from);
    if let Some(l) = &log {
        if let Some(parent) = l.parent() {
            let _ = std::fs::create_dir_all(parent);
        }
    }
    let settings = arg(&args, "--settings").map(PathBuf::from);
    let version = arg(&args, "--reported-version").or_else(|| std::env::var("RAMP_CLI_VERSION").ok()).unwrap_or_else(|| env!("CARGO_PKG_VERSION").to_string());
    let std_listener = match inherited_listener() {
        Some(l) => l,
        None => match bind(port) {
            Ok(l) => l,
            Err(e) => {
                eprintln!("ramp-router-agent: cannot listen on 127.0.0.1:{port}: {e}");
                std::process::exit(98);
            }
        },
    };
    std_listener.set_nonblocking(true).expect("nonblocking");
    let local_port = std_listener.local_addr().map(|a| a.port()).unwrap_or(port);
    let listener = tokio::net::TcpListener::from_std(std_listener).expect("listener");
    if args.iter().any(|a| a == "--shadow") {
        std::env::set_var("RRH_SHADOW", "1");
    }
    let server = Server::new(local_port, log, settings, version, scheduler);
    println!("ready");
    loop {
        let (stream, peer) = match listener.accept().await {
            Ok(v) => v,
            Err(e) => {
                eprintln!("ramp-router-agent: accept failed: {e}");
                tokio::time::sleep(Duration::from_millis(100)).await;
                continue;
            }
        };
        let _ = stream.set_nodelay(true);
        let server = server.clone();
        tokio::spawn(async move {
            let conn = Conn::new(peer);
            let io = hyper_util::rt::TokioIo::new(stream);
            let svc = hyper::service::service_fn(move |req| proxy::handle(server.clone(), conn.clone(), req));
            let _ = hyper::server::conn::http1::Builder::new().keep_alive(true).serve_connection(io, svc).await;
        });
    }
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.iter().any(|a| a == "--help" || a == "-h") {
        println!("usage: ramp-router-agent [serve] [--sync-ramp <ramp> --sync-env-file <file> --sync-manifest <file> --sync-opt-out <file> [--sync-interval <seconds>] [--sync-log <file>]] [--port <port> [--settings <file>] [--log <file>] [--shadow]]\n       ramp-router-agent --version");
        return;
    }
    if args.iter().any(|a| a == "--version" || a == "-V") {
        println!("ramp-router-agent {}", env!("CARGO_PKG_VERSION"));
        return;
    }
    // --build identifies the binary in the service definition (so updates reload it); unused here.
    const OPTIONS: [&str; 12] = ["--build", "--port", "--settings", "--log", "--reported-version", "--shadow", "--sync-ramp", "--sync-env-file", "--sync-manifest", "--sync-opt-out", "--sync-interval", "--sync-log"];
    if let Some(bad) = args.iter().find(|a| a.starts_with("--") && !OPTIONS.contains(&a.as_str())) {
        eprintln!("ramp-router-agent: unknown option {bad} (see --help)");
        std::process::exit(2);
    }
    let threads: usize = std::env::var("RRH_THREADS").ok().and_then(|t| t.parse().ok()).unwrap_or(2);
    tokio::runtime::Builder::new_multi_thread().worker_threads(threads).max_blocking_threads(4).enable_all().build().expect("runtime").block_on(serve(args));
}
