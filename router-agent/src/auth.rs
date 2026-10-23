//! Local bearer tokens select an owner-private credential for exactly one upstream.

use hyper::http::HeaderValue;
use sha2::{Digest, Sha256};
use std::io::Read;
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
use std::path::Path;

fn private_directory(path: &Path) -> Option<()> {
    let info = std::fs::symlink_metadata(path).ok()?;
    (info.is_dir() && info.uid() == unsafe { libc::getuid() } && info.mode() & 0o022 == 0).then_some(())
}

pub fn read_private(path: &Path) -> Option<Vec<u8>> {
    private_directory(path.parent()?)?;
    let file = std::fs::OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK).open(path).ok()?;
    let info = file.metadata().ok()?;
    if !info.is_file() || info.uid() != unsafe { libc::getuid() } || info.mode() & 0o077 != 0 || info.len() > 65536 {
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

#[cfg(test)]
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
