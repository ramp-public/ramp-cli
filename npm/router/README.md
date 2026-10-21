# @ramp/router

Install the Ramp CLI and open the Router workspace to connect coding agents and desktop apps, manage API keys, edit routing strategies, and choose subagent models.

## Install

Run in one command without a global install:

```bash
npx @ramp/router
```

Or install the global `router` command:

```bash
npm install -g @ramp/router
router
```

Requires Node.js 22+ and npm 10+ (Python is not required). Supported platforms: macOS (Apple Silicon & Intel), Linux (glibc x64 & arm64), and Windows (x64).

## Router Workspace & Setup

Open the interactive Router workspace or configure harnesses directly:

```bash
router                               # or: ramp router ui
router ui --inline --height 18       # beneath your prompt (macOS/Linux)
router ui --theme light              # dark, light, terminal, or auto (default)
router configure                     # Start directly at harness setup
router configure codex               # Configure a specific harness
router status                        # View current Router connection and agent status
```

Interactive `router configure`, `keys`, `strategies`, `account`, and `subagents` open their workspace screens.

For scripts and AI agents:

```bash
router --agent keys list
router --agent --no-input configure codex  # key from RAMP_ROUTER_CONFIGURE_API_KEY or --setup-file
```

## Manage Router API Keys & Strategies

Run `router login`, then `router keys` to select a key, inspect its usage, rename it, choose a routing strategy, or lock/unlock it:

```bash
router keys rename KEY_ID --name "Coding"
router keys set-strategy KEY_ID --routing-profile "Cheap"
router keys lock KEY_ID
router keys unlock KEY_ID
```

For routing strategies, `router strategies` opens the account editor in interactive mode, or `router --agent strategies list --account` in script mode.

## Codex Model Picker

Codex CLI and Desktop use Router's recommended model shortlist by default:

```bash
router configure codex --codex-models compact --reuse-existing-key
router configure codex --codex-models all --reuse-existing-key
```

## License

MIT — powered by [`@ramp/cli`](https://www.npmjs.com/package/@ramp/cli). See [https://github.com/ramp-public/ramp-cli](https://github.com/ramp-public/ramp-cli) for full documentation and source.
