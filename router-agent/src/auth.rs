//! Local bearer tokens select an owner-private credential for exactly one upstream.

use hyper::http::HeaderValue;
use sha2::{Digest, Sha256};
use std::io::Read;
#[cfg(unix)]
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
use std::path::Path;

#[cfg(unix)]
fn private_directory(path: &Path) -> Option<()> {
    let info = std::fs::symlink_metadata(path).ok()?;
    (info.is_dir() && info.uid() == unsafe { libc::getuid() } && info.mode() & 0o022 == 0).then_some(())
}

/// Owned by this user (or SYSTEM/Administrators), not a symlink or junction, and no one else may
/// add, remove or rename entries in it (see `winacl`).
#[cfg(windows)]
fn private_directory(path: &Path) -> Option<()> {
    crate::win::private_directory(path).then_some(())
}

#[cfg(unix)]
fn open_private(path: &Path) -> Option<std::fs::File> {
    let file = std::fs::OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK).open(path).ok()?;
    let info = file.metadata().ok()?;
    if !info.is_file() || info.uid() != unsafe { libc::getuid() } || info.mode() & 0o077 != 0 {
        return None;
    }
    Some(file)
}

/// A regular file owned by this user that no one else may read (see `winacl`).
#[cfg(windows)]
use crate::win::open_private_file as open_private;

pub fn read_private(path: &Path) -> Option<Vec<u8>> {
    private_directory(path.parent()?)?;
    let file = open_private(path)?;
    if file.metadata().ok()?.len() > 65536 {
        return None;
    }
    let mut bytes = Vec::new();
    file.take(65537).read_to_end(&mut bytes).ok()?;
    (bytes.len() <= 65536).then_some(bytes)
}

pub fn upstream_authorization(settings: &Path, token: &str, upstream: &str) -> Option<HeaderValue> {
    let secret = token.strip_prefix("ramp-local-")?;
    if secret.len() != 64 || !secret.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)) {
        return None;
    }
    let parent = settings.parent()?;
    private_directory(parent)?;
    let digest = format!("{:x}", Sha256::digest(token.as_bytes()));
    let path = parent.join("router-agent-credentials").join(format!("{digest}.json"));
    let record: serde_json::Value = serde_json::from_slice(&read_private(&path)?).ok()?;
    if record.get("upstream")?.as_str()? != upstream.to_ascii_lowercase() {
        return None;
    }
    let key = record.get("api_key")?.as_str()?;
    if key.is_empty() || key.starts_with("ramp-local-") {
        return None;
    }
    HeaderValue::from_str(&format!("Bearer {key}")).ok()
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use std::os::unix::fs::{symlink, PermissionsExt};

    #[test]
    fn credential_files_must_be_private_regular_files() {
        let root = std::env::temp_dir().join(format!("rrh-private-auth-{}", std::process::id()));
        std::fs::create_dir_all(&root).unwrap();
        let path = root.join("key");
        std::fs::write(&path, b"private").unwrap();
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600)).unwrap();
        assert_eq!(read_private(&path).unwrap(), b"private");
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o644)).unwrap();
        assert!(read_private(&path).is_none());
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600)).unwrap();
        let link = root.join("link");
        symlink(&path, &link).unwrap();
        assert!(read_private(&link).is_none());
        assert!(read_private(&root).is_none());
        std::fs::set_permissions(&root, std::fs::Permissions::from_mode(0o777)).unwrap();
        assert!(read_private(&path).is_none());
        std::fs::remove_dir_all(&root).unwrap();
    }
}

#[cfg(all(test, windows))]
mod windows_tests {
    use super::*;

    fn icacls(path: &Path, args: &[&str]) {
        let status = std::process::Command::new("icacls").arg(path).args(args).stdout(std::process::Stdio::null()).status().unwrap();
        assert!(status.success(), "icacls {args:?}");
    }

    #[test]
    fn credential_files_must_be_private_regular_files() {
        let root = std::env::temp_dir().join(format!("rrh-private-auth-{}", std::process::id()));
        std::fs::create_dir_all(&root).unwrap();
        // This user's alone: a %TEMP% can inherit entries for others (Codex's sandbox users).
        let user = std::env::var("USERNAME").unwrap();
        icacls(&root, &["/inheritance:r", "/grant:r", &format!("{user}:(OI)(CI)F")]);
        // Created as ramp's own writes are: owned by this user, or by Administrators when the
        // process is elevated (as on CI runners), which both count as private.
        let path = root.join("key");
        std::fs::write(&path, b"private").unwrap();
        assert_eq!(read_private(&path).unwrap(), b"private");
        // Everyone may read the file: refused.
        icacls(&path, &["/grant", "*S-1-1-0:R"]);
        assert!(read_private(&path).is_none());
        icacls(&path, &["/remove:g", "*S-1-1-0"]);
        assert_eq!(read_private(&path).unwrap(), b"private");
        // Everyone may read the folder: fine; may add files to it: refused.
        icacls(&root, &["/grant", "*S-1-1-0:(RX)"]);
        assert_eq!(read_private(&path).unwrap(), b"private");
        icacls(&root, &["/grant", "*S-1-1-0:(M)"]);
        assert!(read_private(&path).is_none());
        icacls(&root, &["/remove:g", "*S-1-1-0"]);
        assert!(read_private(&root).is_none());
        // A symlink or junction to the file or folder is never followed (creating one needs
        // Developer Mode; skipped without it).
        let link = root.join("link");
        if std::os::windows::fs::symlink_file(&path, &link).is_ok() {
            assert!(read_private(&link).is_none());
        }
        std::fs::remove_dir_all(&root).unwrap();
    }
}
