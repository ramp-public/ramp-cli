//! Calling-process lookup without lsof: which local process owns the client end of a connection
//! to the helper, and that process's working directory.

use std::sync::Mutex;

/// Recently seen client pids, checked before a full scan (agents keep reconnecting from the same pid).
static RECENT: Mutex<Vec<i32>> = Mutex::new(Vec::new());

fn remember(pid: i32) {
    let mut r = RECENT.lock().unwrap();
    r.retain(|&p| p != pid);
    r.insert(0, pid);
    r.truncate(64);
}

fn recent() -> Vec<i32> {
    RECENT.lock().unwrap().clone()
}

/// (pid, command) of the process whose socket has local port `peer_port` and remote port `helper_port`.
pub fn find(helper_port: u16, peer_port: u16) -> Option<(i64, String)> {
    let me = std::process::id() as i32;
    imp::find(helper_port, peer_port, me).map(|(pid, cmd)| {
        remember(pid);
        (pid as i64, cmd)
    })
}

pub fn cwd(pid: i64) -> Option<String> {
    imp::cwd(pid as i32)
}

/// Fail closed before using request-supplied paths with the daemon's permissions.
pub fn same_user(helper_port: u16, peer_port: u16) -> bool {
    imp::same_user(helper_port, peer_port)
}

#[cfg(target_os = "linux")]
mod imp {
    use super::recent;
    use std::collections::HashSet;
    use std::fs;

    fn socket_owner(helper_port: u16, peer_port: u16) -> Option<(u64, u32)> {
        for table in ["/proc/net/tcp", "/proc/net/tcp6"] {
            let Ok(text) = fs::read_to_string(table) else { continue };
            for line in text.lines().skip(1) {
                let f: Vec<&str> = line.split_whitespace().collect();
                if f.len() < 10 || f[3] != "01" {
                    continue; // ESTABLISHED only
                }
                let port = |addr: &str| addr.rsplit(':').next().and_then(|p| u16::from_str_radix(p, 16).ok());
                let loopback = |addr: &str| matches!(addr.split(':').next(), Some("0100007F" | "0000000000000000FFFF00000100007F"));
                if loopback(f[1]) && loopback(f[2]) && port(f[1]) == Some(peer_port) && port(f[2]) == Some(helper_port) {
                    return Some((f[9].parse().ok()?, f[7].parse().ok()?));
                }
            }
        }
        None
    }

    pub fn same_user(helper_port: u16, peer_port: u16) -> bool {
        owned_by(helper_port, peer_port, unsafe { libc::geteuid() })
    }

    fn owned_by(helper_port: u16, peer_port: u16, expected_uid: u32) -> bool {
        socket_owner(helper_port, peer_port).is_some_and(|(_, uid)| uid == expected_uid)
    }

    fn owns(pid: i32, needle: &str) -> bool {
        let Ok(dir) = fs::read_dir(format!("/proc/{pid}/fd")) else { return false };
        dir.flatten().any(|e| fs::read_link(e.path()).map(|l| l.as_os_str() == needle).unwrap_or(false))
    }

    pub fn find(helper_port: u16, peer_port: u16, me: i32) -> Option<(i32, String)> {
        let (inode, owner) = socket_owner(helper_port, peer_port)?;
        let uid = unsafe { libc::geteuid() };
        if owner != uid { return None; }
        let needle = format!("socket:[{inode}]");
        let mut seen = HashSet::new();
        let check = |pid| {
            use std::os::unix::fs::MetadataExt;
            if pid != me && fs::metadata(format!("/proc/{pid}")).is_ok_and(|m| m.uid() == uid) && owns(pid, &needle) {
                let comm = fs::read_to_string(format!("/proc/{pid}/comm")).unwrap_or_default();
                Some((pid, comm.trim_end_matches('\n').to_string()))
            } else {
                None
            }
        };
        for pid in recent() {
            seen.insert(pid);
            if let Some(client) = check(pid) { return Some(client); }
        }
        if let Ok(d) = fs::read_dir("/proc") {
            for e in d.flatten() {
                if let Some(pid) = e.file_name().to_str().and_then(|s| s.parse::<i32>().ok()) {
                    if seen.insert(pid) {
                        if let Some(client) = check(pid) { return Some(client); }
                    }
                }
            }
        }
        None
    }

    pub fn cwd(pid: i32) -> Option<String> {
        fs::read_link(format!("/proc/{pid}/cwd")).ok().map(|p| p.to_string_lossy().into_owned())
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        #[test]
        fn socket_identity_requires_matching_uid_and_live_peer() {
            let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
            let helper = listener.local_addr().unwrap().port();
            let client = std::net::TcpStream::connect(listener.local_addr().unwrap()).unwrap();
            let (_accepted, peer) = listener.accept().unwrap();
            let uid = unsafe { libc::geteuid() };
            assert!(same_user(helper, peer.port()));
            assert!(!owned_by(helper, peer.port(), uid + 1));
            assert!(!same_user(helper, 0));
            drop(client);
        }
    }
}

#[cfg(target_os = "macos")]
mod imp {
    use super::recent;
    use libproc::libproc::bsd_info::BSDInfo;
    use libproc::libproc::file_info::{pidfdinfo, ListFDs, ProcFDType};
    use libproc::libproc::net_info::{SocketFDInfo, SocketInfoKind};
    use libproc::libproc::proc_pid::{listpidinfo, pidinfo};
    use libproc::processes::{pids_by_type, ProcFilter};

    fn owns(pid: i32, helper_port: u16, peer_port: u16) -> bool {
        let Ok(info) = pidinfo::<BSDInfo>(pid, 0) else { return false };
        if info.pbi_uid != unsafe { libc::geteuid() } { return false; }
        let Ok(fds) = listpidinfo::<ListFDs>(pid, info.pbi_nfiles as usize) else { return false };
        for fd in fds {
            if !matches!(ProcFDType::from(fd.proc_fdtype), ProcFDType::Socket) {
                continue;
            }
            let Ok(s) = pidfdinfo::<SocketFDInfo>(pid, fd.proc_fd) else { continue };
            if !matches!(SocketInfoKind::from(s.psi.soi_kind), SocketInfoKind::Tcp) {
                continue;
            }
            let ini = unsafe { s.psi.soi_proto.pri_tcp.tcpsi_ini };
            let lport = u16::from_be(ini.insi_lport as u16);
            let fport = u16::from_be(ini.insi_fport as u16);
            let loopback = |addr: libproc::libproc::net_info::InSIAddr| unsafe {
                match s.psi.soi_family {
                    libc::AF_INET => u32::from_be(addr.ina_46.i46a_addr4.s_addr) == u32::from(std::net::Ipv4Addr::LOCALHOST),
                    libc::AF_INET6 => addr.ina_6.s6_addr == std::net::Ipv4Addr::LOCALHOST.to_ipv6_mapped().octets(),
                    _ => false,
                }
            };
            if lport == peer_port && fport == helper_port && loopback(ini.insi_laddr) && loopback(ini.insi_faddr) {
                return true;
            }
        }
        false
    }

    pub fn same_user(helper_port: u16, peer_port: u16) -> bool {
        // Unlike attribution fallback, identity verification also permits this
        // process (e.g. an embedded client), but still requires a matching UID.
        let me = std::process::id() as i32;
        owns(me, helper_port, peer_port) || super::find(helper_port, peer_port).is_some()
    }

    fn comm(pid: i32) -> String {
        pidinfo::<BSDInfo>(pid, 0)
            .map(|i| {
                let bytes: Vec<u8> = i.pbi_comm.iter().take_while(|&&c| c != 0).map(|&c| c as u8).collect();
                String::from_utf8_lossy(&bytes).into_owned()
            })
            .unwrap_or_default()
    }

    pub fn find(helper_port: u16, peer_port: u16, me: i32) -> Option<(i32, String)> {
        for pid in recent() {
            if pid != me && owns(pid, helper_port, peer_port) {
                return Some((pid, comm(pid)));
            }
        }
        let uid = unsafe { libc::geteuid() };
        let pids = pids_by_type(ProcFilter::ByUID { uid }).ok()?;
        for pid in pids {
            let pid = pid as i32;
            if pid != me && pid > 0 && owns(pid, helper_port, peer_port) {
                return Some((pid, comm(pid)));
            }
        }
        None
    }

    /// proc_pidinfo(PROC_PIDVNODEPATHINFO): struct proc_vnodepathinfo { vnode_info_path pvi_cdir; ... },
    /// vnode_info_path = { struct vnode_info (152 bytes); char vip_path[MAXPATHLEN] }. Called directly
    /// because libproc's pidcwd is unimplemented on macOS.
    pub fn cwd(pid: i32) -> Option<String> {
        const PROC_PIDVNODEPATHINFO: libc::c_int = 9;
        const VNODE_INFO_SIZE: usize = 152;
        const MAXPATHLEN: usize = 1024;
        let size = 2 * (VNODE_INFO_SIZE + MAXPATHLEN);
        let mut buf = vec![0u8; size];
        let n = unsafe { libc::proc_pidinfo(pid, PROC_PIDVNODEPATHINFO, 0, buf.as_mut_ptr() as *mut libc::c_void, size as libc::c_int) };
        if n <= 0 {
            return None;
        }
        let path = &buf[VNODE_INFO_SIZE..VNODE_INFO_SIZE + MAXPATHLEN];
        let end = path.iter().position(|&b| b == 0).unwrap_or(path.len());
        let s = String::from_utf8_lossy(&path[..end]).into_owned();
        (!s.is_empty()).then_some(s)
    }
}
