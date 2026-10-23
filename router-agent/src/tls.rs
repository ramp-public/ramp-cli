//! Upstream TLS trust: whatever the machine trusts, plus Mozilla's roots as a floor.
//!
//! Managed laptops often inspect TLS with an IT-installed root CA. Harnesses reach Router through
//! those roots (Codex and curl via the OS store, Node-based Claude Code / OpenCode / Pi often via
//! NODE_EXTRA_CA_CERTS), so the helper must accept every root the harness could, or a request that
//! works direct would fail through the helper. Verification is never disabled.

use rustls::client::danger::ServerCertVerifier;
use rustls::ClientConfig;
use rustls_pki_types::pem::PemObject;
use rustls_pki_types::CertificateDer;
use std::sync::{Arc, OnceLock};

/// CA bundles IT commonly points tools at; the service definition passes them through.
pub const CA_ENV_VARS: [&str; 4] = ["NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"];

/// Set by `ramp router agent enable` in the service definition: the CA bundles it has seen in
/// `CA_ENV_VARS`, separated like PATH (a service does not inherit the user's shell).
pub const EXTRA_CA_ENV: &str = "RAMP_ROUTER_EXTRA_CA_FILES";

/// Certificates from the CA bundle files named by `EXTRA_CA_ENV` and `CA_ENV_VARS` (unreadable
/// files and non-certificate entries are skipped; a bad bundle never stops the helper).
pub fn env_bundle_roots() -> Vec<CertificateDer<'static>> {
    let mut paths: Vec<std::ffi::OsString> = std::env::var_os(EXTRA_CA_ENV).map(|v| std::env::split_paths(&v).map(Into::into).collect()).unwrap_or_default();
    paths.extend(CA_ENV_VARS.iter().filter_map(std::env::var_os));
    let mut out = Vec::new();
    for path in paths.into_iter().filter(|p| !p.is_empty()) {
        let Ok(iter) = CertificateDer::pem_file_iter(&path) else { continue };
        out.extend(iter.filter_map(Result::ok));
    }
    out
}

fn verifier(provider: Arc<rustls::crypto::CryptoProvider>) -> (Arc<dyn ServerCertVerifier>, &'static str) {
    let mut extra: Vec<CertificateDer<'static>> = webpki_root_certs::TLS_SERVER_ROOT_CERTS.to_vec();
    extra.extend(env_bundle_roots());
    // the OS verifier (macOS Security.framework incl. MDM/keychain trust; system bundle on Linux),
    // with Mozilla's roots and the env bundles added
    match rustls_platform_verifier::Verifier::new_with_extra_roots(extra.clone(), provider.clone()) {
        Ok(v) => (Arc::new(v), "platform"),
        Err(_) => {
            // the platform could not be queried: Mozilla roots plus the env bundles
            let mut roots = rustls::RootCertStore::empty();
            for c in extra {
                let _ = roots.add(c);
            }
            match rustls::client::WebPkiServerVerifier::builder_with_provider(Arc::new(roots), provider).build() {
                Ok(v) => (v, "bundled"),
                Err(_) => unreachable!("Mozilla root set is never empty"),
            }
        }
    }
}

fn built() -> &'static (ClientConfig, &'static str) {
    static CFG: OnceLock<(ClientConfig, &'static str)> = OnceLock::new();
    CFG.get_or_init(|| {
        let provider = Arc::new(rustls::crypto::ring::default_provider());
        let (v, kind) = verifier(provider.clone());
        let cfg = ClientConfig::builder_with_provider(provider)
            .with_safe_default_protocol_versions()
            .expect("TLS 1.2/1.3")
            .dangerous() // a custom verifier, not disabled verification
            .with_custom_certificate_verifier(v)
            .with_no_client_auth();
        (cfg, kind)
    })
}

/// The client TLS configuration (built once; ALPN is set by the connector).
pub fn client_config() -> ClientConfig {
    built().0.clone()
}

/// Which trust source is active: "platform" (OS store + Mozilla + env bundles) or "bundled".
pub fn trust_source() -> &'static str {
    built().1
}
