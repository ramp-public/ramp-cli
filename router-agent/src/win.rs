//! Windows counterparts of the agent's Unix-only pieces: who owns the client end of a loopback
//! connection, owner and DACL checks for private files, process liveness, local time, and child
//! processes that never open a console window.

use crate::winacl::{self, Kind};
use std::ffi::c_void;
use std::os::windows::fs::OpenOptionsExt;
use std::os::windows::io::{AsRawHandle, IntoRawHandle};
use std::path::Path;
use std::sync::OnceLock;
use windows_sys::Win32::Foundation::{CloseHandle, GetLastError, LocalFree, FILETIME, HANDLE, SYSTEMTIME};
use windows_sys::Win32::Security::Authorization::GetSecurityInfo;
use windows_sys::Win32::Security::{GetLengthSid, GetSidSubAuthority, GetSidSubAuthorityCount, GetTokenInformation, IsValidSid, ACE_HEADER, ACL, PSID};
use windows_sys::Win32::Storage::FileSystem::{FileTimeToLocalFileTime, GetFileInformationByHandle, BY_HANDLE_FILE_INFORMATION};
use windows_sys::Win32::System::Threading::{GetCurrentProcess, OpenProcess, OpenProcessToken, QueryFullProcessImageNameW, WaitForSingleObject};

const ERROR_INSUFFICIENT_BUFFER: u32 = 122;
const ERROR_INVALID_PARAMETER: u32 = 87;
const PROCESS_QUERY_INFORMATION: u32 = 0x400;
const PROCESS_QUERY_LIMITED_INFORMATION: u32 = 0x1000;
const PROCESS_VM_READ: u32 = 0x10;
const SYNCHRONIZE: u32 = 0x0010_0000;
const TOKEN_QUERY: u32 = 0x8;
const TOKEN_USER_CLASS: i32 = 1;
const TOKEN_INTEGRITY_LEVEL_CLASS: i32 = 25;
const TOKEN_IS_APP_CONTAINER_CLASS: i32 = 29;
const SECURITY_MANDATORY_MEDIUM_RID: u32 = 0x2000;
const SE_FILE_OBJECT: i32 = 1;
const OWNER_SECURITY_INFORMATION: u32 = 1;
const DACL_SECURITY_INFORMATION: u32 = 4;
const FILE_ATTRIBUTE_DIRECTORY: u32 = 0x10;
const FILE_ATTRIBUTE_REPARSE_POINT: u32 = 0x400;
const FILE_FLAG_BACKUP_SEMANTICS: u32 = 0x0200_0000;
const FILE_FLAG_OPEN_REPARSE_POINT: u32 = 0x0020_0000;
const READ_CONTROL: u32 = 0x0002_0000;
const FILE_READ_ATTRIBUTES: u32 = 0x80;
const WAIT_OBJECT_0: u32 = 0;

/// No console of its own: a console program started from this windowless agent would otherwise get
/// a new console window. Its own children inherit that hidden console, so they open none either.
pub const CREATE_NO_WINDOW: u32 = 0x0800_0000;
pub const CREATE_NEW_PROCESS_GROUP: u32 = 0x200;
pub const CREATE_BREAKAWAY_FROM_JOB: u32 = 0x0100_0000;
pub const BELOW_NORMAL_PRIORITY_CLASS: u32 = 0x4000;

struct Handle(HANDLE);

impl Drop for Handle {
    fn drop(&mut self) {
        unsafe { CloseHandle(self.0) };
    }
}

fn open_process(pid: u32, access: u32) -> Option<Handle> {
    let h = unsafe { OpenProcess(access, 0, pid) };
    (!h.is_null()).then_some(Handle(h))
}

fn process_token(process: HANDLE) -> Option<Handle> {
    let mut token: HANDLE = std::ptr::null_mut();
    (unsafe { OpenProcessToken(process, TOKEN_QUERY, &mut token) } != 0).then_some(Handle(token))
}

/// GetTokenInformation into a pointer-aligned buffer.
fn token_info(token: HANDLE, class: i32) -> Option<Vec<u64>> {
    let mut len = 0u32;
    unsafe { GetTokenInformation(token, class, std::ptr::null_mut(), 0, &mut len) };
    if len == 0 || unsafe { GetLastError() } != ERROR_INSUFFICIENT_BUFFER {
        return None;
    }
    let mut buf = vec![0u64; (len as usize).div_ceil(8)];
    (unsafe { GetTokenInformation(token, class, buf.as_mut_ptr() as *mut c_void, len, &mut len) } != 0).then_some(buf)
}

fn sid_bytes(sid: PSID) -> Option<Vec<u8>> {
    if sid.is_null() || unsafe { IsValidSid(sid) } == 0 {
        return None;
    }
    let len = unsafe { GetLengthSid(sid) } as usize;
    Some(unsafe { std::slice::from_raw_parts(sid as *const u8, len) }.to_vec())
}

/// The first field of TOKEN_USER and TOKEN_MANDATORY_LABEL is a SID_AND_ATTRIBUTES, whose first
/// field is the SID pointer.
fn first_sid(buf: &[u64]) -> PSID {
    buf[0] as usize as PSID
}

fn token_user(token: HANDLE) -> Option<Vec<u8>> {
    sid_bytes(first_sid(&token_info(token, TOKEN_USER_CLASS)?))
}

/// This process's user SID.
pub fn current_user_sid() -> Option<&'static [u8]> {
    static SID: OnceLock<Option<Vec<u8>>> = OnceLock::new();
    SID.get_or_init(|| token_user(process_token(unsafe { GetCurrentProcess() })?.0)).as_deref()
}

/// Whether `pid` runs as this user at medium integrity or above, outside an AppContainer. The
/// integrity floor keeps sandboxed (low integrity) processes of the same user, such as browser
/// renderers, from using the agent to read files they could not read themselves.
pub fn trusted_client(pid: u32) -> bool {
    let Some(me) = current_user_sid() else { return false };
    let Some(process) = open_process(pid, PROCESS_QUERY_LIMITED_INFORMATION) else { return false };
    let Some(token) = process_token(process.0) else { return false };
    if token_user(token.0).as_deref() != Some(me) {
        return false;
    }
    let app_container = token_info(token.0, TOKEN_IS_APP_CONTAINER_CLASS).is_none_or(|b| b[0] as u32 != 0);
    if app_container {
        return false;
    }
    let Some(label) = token_info(token.0, TOKEN_INTEGRITY_LEVEL_CLASS) else { return false };
    let sid = first_sid(&label);
    if sid.is_null() || unsafe { IsValidSid(sid) } == 0 {
        return false;
    }
    let count = unsafe { *GetSidSubAuthorityCount(sid) };
    count > 0 && unsafe { *GetSidSubAuthority(sid, count as u32 - 1) } >= SECURITY_MANDATORY_MEDIUM_RID
}

/// The process owning the established IPv4 connection 127.0.0.1:`peer_port` -> 127.0.0.1:`helper_port`.
pub fn tcp_peer_pid(helper_port: u16, peer_port: u16) -> Option<u32> {
    use windows_sys::Win32::NetworkManagement::IpHelper::{GetExtendedTcpTable, MIB_TCPROW_OWNER_PID, TCP_TABLE_OWNER_PID_ALL};
    const AF_INET: u32 = 2;
    const ESTABLISHED: u32 = 5;
    let loopback = u32::from_ne_bytes([127, 0, 0, 1]);
    let port = |p: u32| u16::from_be(p as u16);
    let mut size = 0u32;
    for _ in 0..4 {
        let mut buf = vec![0u32; (size as usize).div_ceil(4).max(1)];
        let rc = unsafe { GetExtendedTcpTable(buf.as_mut_ptr() as *mut c_void, &mut size, 0, AF_INET, TCP_TABLE_OWNER_PID_ALL, 0) };
        if rc == ERROR_INSUFFICIENT_BUFFER {
            size += 4096; // connections opened since the size query
            continue;
        }
        if rc != 0 {
            return None;
        }
        // MIB_TCPTABLE_OWNER_PID: a u32 count, then the rows
        let count = buf[0] as usize;
        let row_words = std::mem::size_of::<MIB_TCPROW_OWNER_PID>() / 4;
        if 1 + count * row_words > buf.len() {
            return None;
        }
        let rows = unsafe { std::slice::from_raw_parts(buf.as_ptr().add(1) as *const MIB_TCPROW_OWNER_PID, count) };
        return rows
            .iter()
            .find(|r| r.dwState == ESTABLISHED && r.dwLocalAddr == loopback && r.dwRemoteAddr == loopback && port(r.dwLocalPort) == peer_port && port(r.dwRemotePort) == helper_port)
            .map(|r| r.dwOwningPid);
    }
    None
}

/// The executable's file name without `.exe`, like a Unix command name.
pub fn image_name(pid: u32) -> String {
    let Some(process) = open_process(pid, PROCESS_QUERY_LIMITED_INFORMATION) else { return String::new() };
    let mut buf = vec![0u16; 32768];
    let mut len = buf.len() as u32;
    if unsafe { QueryFullProcessImageNameW(process.0, 0, buf.as_mut_ptr(), &mut len) } == 0 {
        return String::new();
    }
    let path = String::from_utf16_lossy(&buf[..len as usize]);
    let name = path.rsplit(['\\', '/']).next().unwrap_or("");
    match name.len().checked_sub(4) {
        Some(i) if name.is_char_boundary(i) && name[i..].eq_ignore_ascii_case(".exe") => name[..i].to_string(),
        _ => name.to_string(),
    }
}

/// A 64-bit process's working directory, read from its process parameters (what Process Explorer
/// shows). None for 32-bit processes and for processes this user cannot read.
#[cfg(target_pointer_width = "64")]
pub fn process_cwd(pid: u32) -> Option<String> {
    use windows_sys::Wdk::System::Threading::NtQueryInformationProcess;
    use windows_sys::Win32::System::Diagnostics::Debug::ReadProcessMemory;
    use windows_sys::Win32::System::Threading::IsWow64Process;
    #[repr(C)]
    struct BasicInformation {
        exit_status: i32,
        peb: usize,
        affinity_mask: usize,
        base_priority: i32,
        unique_process_id: usize,
        inherited_from: usize,
    }
    let process = open_process(pid, PROCESS_QUERY_INFORMATION | PROCESS_VM_READ)?;
    let mut wow64 = 0;
    if unsafe { IsWow64Process(process.0, &mut wow64) } == 0 || wow64 != 0 {
        return None;
    }
    let mut info: BasicInformation = unsafe { std::mem::zeroed() };
    let mut len = 0u32;
    let status = unsafe { NtQueryInformationProcess(process.0, 0, &mut info as *mut _ as *mut c_void, std::mem::size_of::<BasicInformation>() as u32, &mut len) };
    if status != 0 || info.peb == 0 {
        return None;
    }
    let read = |address: usize, out: &mut [u8]| -> Option<()> {
        let mut done = 0usize;
        let ok = unsafe { ReadProcessMemory(process.0, address as *const c_void, out.as_mut_ptr() as *mut c_void, out.len(), &mut done) };
        (ok != 0 && done == out.len()).then_some(())
    };
    // PEB.ProcessParameters at 0x20; RTL_USER_PROCESS_PARAMETERS.CurrentDirectory.DosPath (a
    // UNICODE_STRING: u16 length in bytes, u16 capacity, padding, buffer pointer) at 0x38.
    let mut word = [0u8; 8];
    read(info.peb + 0x20, &mut word)?;
    let params = usize::from_ne_bytes(word);
    let mut string = [0u8; 16];
    read(params.checked_add(0x38)?, &mut string)?;
    let bytes = u16::from_ne_bytes([string[0], string[1]]) as usize;
    let buffer = usize::from_ne_bytes(string[8..16].try_into().ok()?);
    if bytes == 0 || bytes % 2 != 0 || buffer == 0 {
        return None;
    }
    let mut raw = vec![0u8; bytes];
    read(buffer, &mut raw)?;
    let wide: Vec<u16> = raw.chunks_exact(2).map(|c| u16::from_ne_bytes([c[0], c[1]])).collect();
    let dir = String::from_utf16(&wide).ok()?;
    let trimmed = dir.trim_end_matches('\\');
    // Keep the root's separator: "C:\" stays "C:\".
    Some(if trimmed.ends_with(':') { dir } else { trimmed.to_string() })
}

#[cfg(not(target_pointer_width = "64"))]
pub fn process_cwd(_pid: u32) -> Option<String> {
    None
}

/// Whether the process is known to have exited (Unix: kill(pid, 0) failing with ESRCH). A process
/// that exists but cannot be opened counts as running.
pub fn process_gone(pid: u32) -> bool {
    match open_process(pid, SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION) {
        Some(h) => (unsafe { WaitForSingleObject(h.0, 0) }) == WAIT_OBJECT_0,
        None => (unsafe { GetLastError() }) == ERROR_INVALID_PARAMETER,
    }
}

/// The open object's owner and DACL as plain data for `winacl::private`.
fn private_handle(handle: HANDLE, kind: Kind) -> bool {
    let mut info: BY_HANDLE_FILE_INFORMATION = unsafe { std::mem::zeroed() };
    if unsafe { GetFileInformationByHandle(handle, &mut info) } == 0 {
        return false;
    }
    let is_dir = info.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY != 0;
    if info.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT != 0 || is_dir != (kind == Kind::Directory) {
        return false; // a symlink or junction, or the wrong kind of object
    }
    let Some(user) = current_user_sid() else { return false };
    let (mut owner, mut dacl, mut descriptor): (PSID, *mut ACL, *mut c_void) = (std::ptr::null_mut(), std::ptr::null_mut(), std::ptr::null_mut());
    let rc = unsafe {
        GetSecurityInfo(handle, SE_FILE_OBJECT, OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION, &mut owner, std::ptr::null_mut(), &mut dacl, std::ptr::null_mut(), &mut descriptor)
    };
    if rc != 0 {
        return false;
    }
    let result = (|| {
        let owner = sid_bytes(owner)?;
        if dacl.is_null() {
            return Some(winacl::private(kind, &owner, None, user));
        }
        let count = unsafe { (*dacl).AceCount };
        let mut aces: Vec<(u8, u8, u32, Vec<u8>)> = Vec::with_capacity(count as usize);
        for i in 0..count as u32 {
            let mut ace: *mut c_void = std::ptr::null_mut();
            if unsafe { windows_sys::Win32::Security::GetAce(dacl, i, &mut ace) } == 0 || ace.is_null() {
                return Some(false);
            }
            let header = unsafe { &*(ace as *const ACE_HEADER) };
            if header.AceType == winacl::ACCESS_ALLOWED_ACE_TYPE {
                // ACCESS_ALLOWED_ACE: header, u32 mask, then the SID
                let mask = unsafe { *((ace as *const u8).add(4) as *const u32) };
                let sid = sid_bytes(unsafe { (ace as *mut u8).add(8) } as PSID)?;
                aces.push((header.AceType, header.AceFlags, mask, sid));
            } else {
                aces.push((header.AceType, header.AceFlags, 0, Vec::new()));
            }
        }
        let view: Vec<winacl::Ace> = aces.iter().map(|(t, f, m, s)| (*t, *f, *m, s.as_slice())).collect();
        Some(winacl::private(kind, &owner, Some(&view), user))
    })();
    unsafe { LocalFree(descriptor) };
    result.unwrap_or(false)
}

/// Opens `path` without following a final symlink or junction (O_NOFOLLOW).
fn open_nofollow(path: &Path, kind: Kind) -> Option<std::fs::File> {
    let mut options = std::fs::OpenOptions::new();
    match kind {
        Kind::File => options.read(true).custom_flags(FILE_FLAG_OPEN_REPARSE_POINT),
        Kind::Directory => options.access_mode(READ_CONTROL | FILE_READ_ATTRIBUTES).custom_flags(FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_BACKUP_SEMANTICS),
    };
    options.open(path).ok()
}

/// A directory owned by this user (or SYSTEM/Administrators) that nobody else can write.
pub fn private_directory(path: &Path) -> bool {
    open_nofollow(path, Kind::Directory).is_some_and(|d| private_handle(d.as_raw_handle(), Kind::Directory))
}

/// An open, regular, non-reparse-point file owned by this user that nobody else can read.
pub fn open_private_file(path: &Path) -> Option<std::fs::File> {
    let file = open_nofollow(path, Kind::File)?;
    private_handle(file.as_raw_handle(), Kind::File).then_some(file)
}

/// Local time as `%Y-%m-%dT%H:%M:%S%z`.
pub fn timestamp() -> String {
    use windows_sys::Win32::System::SystemInformation::GetSystemTimeAsFileTime;
    use windows_sys::Win32::System::Time::FileTimeToSystemTime;
    let mut utc: FILETIME = unsafe { std::mem::zeroed() };
    let mut local: FILETIME = unsafe { std::mem::zeroed() };
    let mut t: SYSTEMTIME = unsafe { std::mem::zeroed() };
    unsafe {
        GetSystemTimeAsFileTime(&mut utc);
        if FileTimeToLocalFileTime(&utc, &mut local) == 0 || FileTimeToSystemTime(&local, &mut t) == 0 {
            return String::new();
        }
    }
    let ticks = |f: &FILETIME| ((f.dwHighDateTime as i64) << 32) | f.dwLowDateTime as i64;
    // 100 ns ticks per minute
    let offset = ((ticks(&local) - ticks(&utc)) as f64 / 600_000_000.0).round() as i64;
    let (sign, offset) = if offset < 0 { ('-', -offset) } else { ('+', offset) };
    format!("{:04}-{:02}-{:02}T{:02}:{:02}:{:02}{sign}{:02}{:02}", t.wYear, t.wMonth, t.wDay, t.wHour, t.wMinute, t.wSecond, offset / 60, offset % 60)
}

/// Sends this process's stdout and stderr to `path` (appending). Task Scheduler gives a windowless
/// program no output handles, so this is where launchd's StandardErrorPath goes on Windows.
pub fn redirect_output(path: &Path) {
    use windows_sys::Win32::System::Console::{SetStdHandle, STD_ERROR_HANDLE, STD_OUTPUT_HANDLE};
    if let Some(parent) = path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    let Ok(file) = std::fs::OpenOptions::new().create(true).append(true).open(path) else { return };
    let Ok(copy) = file.try_clone() else { return };
    unsafe {
        SetStdHandle(STD_OUTPUT_HANDLE, file.into_raw_handle() as HANDLE);
        SetStdHandle(STD_ERROR_HANDLE, copy.into_raw_handle() as HANDLE);
    }
}

/// `--version` and `--help` from a console: a windowless program has no output handles unless its
/// caller redirected them, so print to the caller's console instead. Never creates a console.
pub fn attach_parent_console() {
    use windows_sys::Win32::System::Console::{AttachConsole, GetStdHandle, ATTACH_PARENT_PROCESS, STD_OUTPUT_HANDLE};
    unsafe {
        let out = GetStdHandle(STD_OUTPUT_HANDLE);
        if out.is_null() || out as isize == -1 {
            AttachConsole(ATTACH_PARENT_PROCESS);
        }
    }
}

/// 127.0.0.1:port with SO_EXCLUSIVEADDRUSE (so no other program, of this user or another, can bind
/// the same port and take connections meant for the agent) and a 1024-connection backlog.
pub fn bind(port: u16) -> std::io::Result<std::net::TcpListener> {
    use socket2::{Domain, Protocol, Socket, Type};
    use std::os::windows::io::AsRawSocket;
    use windows_sys::Win32::Networking::WinSock::{setsockopt, SOCKET, SOL_SOCKET, SO_EXCLUSIVEADDRUSE};
    let socket = Socket::new(Domain::IPV4, Type::STREAM, Some(Protocol::TCP))?;
    let one: i32 = 1;
    if unsafe { setsockopt(socket.as_raw_socket() as SOCKET, SOL_SOCKET, SO_EXCLUSIVEADDRUSE, &one as *const i32 as *const u8, 4) } != 0 {
        return Err(std::io::Error::last_os_error());
    }
    socket.bind(&std::net::SocketAddr::from(([127, 0, 0, 1], port)).into())?;
    socket.listen(1024)?;
    Ok(socket.into())
}
