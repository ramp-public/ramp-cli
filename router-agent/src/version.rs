//! The Ramp CLI release this agent ships in.
//!
//! The agent ships beside `ramp` in every release, so its version is the CLI's. Cargo's own
//! package version never changes, so it can't tell an old agent from a new one.

fn cli_release() -> Option<&'static str> {
    include_str!("../../src/ramp_cli/__init__.py")
        .lines()
        .find_map(|line| line.strip_prefix("__version__ = \"")?.strip_suffix('"'))
}

/// For example "0.2.112".
pub fn release() -> &'static str {
    cli_release().unwrap_or(env!("CARGO_PKG_VERSION"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reports_the_cli_release_version() {
        // Falling back to Cargo's fixed 0.1.0 would make every agent look the same.
        assert!(
            cli_release().is_some(),
            "ramp_cli/__init__.py no longer has a __version__ line"
        );
        assert_ne!(release(), env!("CARGO_PKG_VERSION"));
    }
}
