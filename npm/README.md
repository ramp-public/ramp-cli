# Ramp npm packages

Two public packages distribute the same native CLI from this repository:

- `cli/` (`@ramp/cli`) owns the installer, argument forwarding, and `ramp` command.
  Its lifecycle never opens a browser or configures Router.
- `router/` (`@ramp/router`) depends on the exact CLI package version and owns
  only `router`. Bare invocation opens the Router terminal workspace, where
  users can configure it. Global installation opens that UI when interactive;
  dependency/npx lifecycle hooks do not open it.

Both packages use the same lock-protected native distribution. The source lives
under the internal repository's `public/npm/` and is exported as `npm/` in
`ramp-public/ramp-cli`; no new repository or private HTTP credentials are needed.

## Scope and release boundaries

This change adds public npm distribution only. New managed downloads come from
`ramp-public/ramp-cli`; neither package contains the private CLI extension or
private binaries. Packing stamps the Python CLI version into both npm package
versions and `nativeCliVersion`, so each npm release installs the native release
it shipped with.

Private npm publication, GitHub Packages, npm-proxy routing, managed release
channels, MDM deployment, and independent internal preview releases are follow-up
work.

Discovery may reuse a compatible local CLI, including an internal build. It
checks version and Router UI availability, not distribution flavor. Thus a
fresh managed download is public, but an existing binary is not guaranteed to
be public. An accepted update prompt delegates to that executable's `update`
command. The current native updater uses the public installer and can replace
an older private build with a newer public one. This package does not provide
a channel-preserving internal update flow.

## Updating

`ramp update` automatically updates supported npm-owned installations: global
packages are updated in their recorded npm prefix, and npx-owned runtimes run
the owning package at `latest` without opening the Router workspace or creating
a global npm install. The helper is kept beside the native binary, not in npx's
temporary cache. Project dependencies retain their package-manager/lockfile
policy and receive instructions instead. Missing npm, a removed owning package,
or an unrecognized receipt never falls back to the curl installer.

Project ownership is determined from the launcher's installation directory,
even when invoked by `npx ramp` or `npm exec -- ramp`. Automatic updates use the
native CLI's parser-only validation mode when supported. Older explicit native
pins remain updatable: only an unsupported-validation-option error enables a
fallback that checks the argument shape and validates root options through the
old CLI's help parser, without invoking its updater. Invalid options, arguments,
environments or profiles fail with exit 2 before npm runs or a pending Router
refresh is retried. Other validation failures never bypass these checks. Help
remains native help.

The Node launcher and persistent shortcuts handle updates on supported platforms.
Directly invoking the native executable on Windows prints instructions instead,
because that running executable cannot be replaced. Older receipts without a
known install context also receive manual instructions.

After a native version changes, the installer runs a noninteractive full
`ramp router refresh` with the new binary. This repairs existing Router harness
setups and installs/updates the scheduled sync job in releases that support it;
it never installs or updates agent skills. This also applies to direct npm
upgrades and the first launch after an upgrade with `--ignore-scripts`. Fresh
installs do not refresh or configure anything automatically.

A failed refresh does not roll back or remove the upgraded CLI. It is recorded
for retry on the next launcher run, and `ramp update` reports the partial failure.
Users can also retry `ramp router refresh` explicitly. Scheduled models-only
sync is complementary; it is not a replacement for full post-upgrade repair.

## Local development

From this directory (or `public/npm` in the internal repository):

```sh
npm ci --ignore-scripts
npm test
npm run check
npm run pack
```

Always use `--ignore-scripts` while developing: installing the workspace without
it runs the production native download hooks. The test suite uses isolated
homes, fake standalone releases, and a loopback-only registry for the actual
packed packages. It does not publish, sign in, or change your shell configuration.
The packed-install tests must reach the loopback registry; external transitive
dependencies are served from the already-installed workspace, not the network.

Tarballs are written to ignored `artifacts/`. Tests and release tooling are
outside both published packages. CI tests Linux/macOS; Windows command/PATH
generation is covered by unit tests, but native Windows acceptance remains a
release prerequisite. Test real interactive workspace sign-in separately as well.

## Versioning

The Python CLI version (`src/ramp_cli/__init__.py`) is the only version. The
checked-in manifests keep a `0.0.0` placeholder; packing stamps the Python
version into both npm package versions, Router's exact `@ramp/cli` dependency,
and `nativeCliVersion`. Never bump an npm version by hand; a test fails if the
placeholder changes. An npm-only fix ships with the next native release
(dispatch the daily release workflow for an off-cycle one).

## One-time npm setup (maintainer action)

1. Obtain `@ramp` permissions to create both public packages and configure their
   trusted publishers. Confirm npm name availability; an unauthenticated 404
   does not prove a name is unclaimed. Use your npm account with 2FA.
2. Run local checks and native-platform acceptance, then pack both packages.
3. Authenticate to the public npm registry. Bootstrap `@ramp/cli` first, then
   `@ramp/router`, using their reviewed tarballs, `--ignore-scripts`,
   `--access public`, `--tag next`, and `--registry https://registry.npmjs.org/`.
   These are real public publications; review all artifacts first. Do not use
   an npm prerelease version merely to test the `next` tag.
4. For **each** package, configure npm trusted publishing for organization
   `ramp-public`, repository `ramp-cli`, workflow filename `npm-packages.yml`,
   environment `npm`. Allow direct `npm publish` where the npm UI requires it.
5. Create the public repository's `npm` GitHub environment and restrict its
   deployments to `main`. npm trusted publishing does not check the branch, so
   this rule is what keeps other branches from publishing. Do not require
   reviewers, or the daily job times out waiting. No npm write token belongs in
   GitHub secrets, `.npmrc`, or either tarball.
6. Verify the OIDC release, then require 2FA and disallow traditional publishing
   tokens in npm package settings. Public repo + public package OIDC releases
   receive npm provenance. A manual bootstrap publish does not establish that.

## Subsequent releases

npm releases follow the daily native release automatically. Because packing
stamps the Python version, every native release has a matching npm version
without a separate bump. After `verify-public` confirms the
curl installation, its `publish-npm` job dispatches this public **npm packages**
workflow on main with `latest` and succeeds only once npmjs.org lists both
packages at that version. The npm tests also run in internal CI, so a failing
npm test blocks the daily release. A run that finds the current version already
published does nothing, and a later run publishes a version an earlier one
missed.

The workflow can still be dispatched manually with `next` for acceptance
testing. The workflow uses Node 22 and npm 11.6.2 with GitHub OIDC.

The release script validates package relationships, public release status, all
five platform archives/checksums, and tarball contents. It preflights immutable
npm versions, publishes CLI before Router, and reconciles an identical version
if a response was lost or only CLI succeeded. Conflicting tarball contents fail
closed; release a new CLI version instead of trying to overwrite an existing one.

The publish script's asset checks are not a native-binary smoke test; in the
daily release, `verify-public` runs that smoke test before npm publication.
Fixture tests are not evidence of native Windows or authenticated onboarding
acceptance.

To promote an **already-published** acceptance version from `next` to `latest`,
use authenticated `npm dist-tag add @ramp/cli@VERSION latest` and then
`npm dist-tag add @ramp/router@VERSION latest`, explicitly selecting npmjs.org.
The workflow deliberately does not mutate an existing version's tags or require
extra OIDC dist-tag privileges. A tag mismatch requires a maintainer decision.

Before rollout, test both `npx` and global installation from npm on fresh hosts,
co-installation, `--ignore-scripts`, existing curl/npm installs, proxy settings,
and cancellation. Request first-party package allowlisting from Security if
Ramp's internal npm proxy blocks new versions; this is separate from publishing.

Follow-up: update the CLI installation guide in `ramp/ramp-api-docs` after npm
publication is approved and verified. Do not advertise unpublished commands in
customer setup instructions.
