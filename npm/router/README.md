# @ramp/router

Install the Ramp CLI and open the Router terminal workspace in one command:

```sh
npx @ramp/router
```

Or install a global Router command:

```sh
npm install -g @ramp/router
router
```

Requires Node.js 22+ and npm 10+. This package depends on an exact version of
`@ramp/cli` for its shared native installer. It owns only the `router` npm
command; `@ramp/cli` owns `ramp`, so installing both does not replace either shim.

Bare `router` opens the interactive Router workspace; its Connect tab handles
sign-in and configuration. It does not open a web dashboard. Explicit arguments
are forwarded to the native CLI's `router` group, so `router status` and
`router --version` continue to work. Help/version commands do not launch the UI
or edit shell startup files.

A global install opens the workspace once when it has an interactive terminal;
otherwise it installs successfully and tells you to run `router` from one.
Dependency installs and npx lifecycle hooks only install; running
`npx @ramp/router` opens the workspace. `CI=1` or
`RAMP_ROUTER_SKIP_ONBOARDING=1` suppresses install-time UI and optional wrapper
prompts, but an explicit `router` command still opens the UI. An unsuccessful UI
launch leaves the CLI installed for retry.

The workspace needs a human interactive terminal. Without one, install-time UI
is deferred and an explicit `router` command reports a usage error. Sign-in may
open a browser from within the terminal workspace.

Interactive npx launch offers persistent PATH setup with consent. Global
installation uses npm's normal shims. Shared installation overrides and
platform limitations are documented in `@ramp/cli`'s README. With
`--ignore-scripts`, the first command installs the native CLI on demand.

Source and issues: https://github.com/ramp-public/ramp-cli
