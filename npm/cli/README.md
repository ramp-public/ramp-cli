# @ramp/cli

Install and launch the standalone Ramp CLI. Requires Node.js 22+ and npm 10+;
Python is not required.

```sh
npm install -g @ramp/cli
ramp --help
```

Or run it without a global npm install:

```sh
npx @ramp/cli --help
```

Installation does not sign in, open a browser, or configure Ramp Router. Use
`ramp auth login` or `ramp router ui` explicitly when needed.

The shared installer reuses a working native Ramp CLI if available. Otherwise
it downloads the public release selected by `nativeCliVersion` in this package,
verifies SHA-256, and installs the entire standalone runtime atomically. Each npm
version targets the native CLI release of the same version.

Local CLI reuse does not distinguish public and private builds. An existing
internal build can be reused, but this package does not preserve an internal
release channel when updating it. The native `ramp update` command currently
uses the public installer. Fresh npm-managed downloads always use public assets.

An npm-managed CLI is updated only through npm. `ramp update` automatically
updates supported global installs in their recorded npm prefix, or updates an
npx-owned runtime through the owning package at `latest` without opening Router.
Project dependencies and older/unknown receipts receive manual instructions;
their lockfiles are never changed automatically. The launcher and persistent
shortcuts turn off native update notices and never run the curl updater. Direct
native invocation on Windows prints instructions because a running executable
cannot be replaced. A reused CLI from another installer keeps its own updater.

After a native upgrade, existing Router setups are fully refreshed with the new
CLI, including upgrades initiated directly through npm or deferred with
`--ignore-scripts`. No agent skills are installed or updated. A failed refresh
keeps the upgraded CLI and is retried on the next launcher run; it can also be
retried with `ramp router refresh`.

The npm-managed distribution lives in `~/.local/lib/ramp-cli-npm` on macOS/Linux
or `%LOCALAPPDATA%\Ramp\CLI\npm` on Windows. It is separate from curl-installed
distributions. A recoverable installation lock serializes both npm packages.
Existing unrelated commands and installations are never overwritten.

Global npm installs use npm's normal command shims. `npx` can create a persistent
native `ramp` shortcut in `~/.local/bin`, but does not edit shell startup files.
The native installation survives npm cache cleanup and npm uninstallation.

Supported targets: macOS and glibc Linux, x64/arm64; Windows x64. Alpine/musl,
Windows arm64, and other architectures are not supported.

Environment overrides shared by both packages:

- `RAMP_CLI_VERSION`: explicit native tag such as `v0.2.55`, or `latest`.
- `RAMP_CLI_FORCE_UPDATE=1`: refresh the npm-managed installation.
- `RAMP_CLI_INSTALL_DIR`: choose the native installation directory.
- `RAMP_CLI_BIN_DIR`: choose the persistent command directory.

With `--ignore-scripts`, installation is deferred until the first invocation.
Downloads require HTTPS; do not disable certificate validation to work around
corporate proxy or certificate errors.

Source and issues: https://github.com/ramp-public/ramp-cli
