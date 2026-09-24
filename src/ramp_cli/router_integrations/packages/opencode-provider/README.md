# Ramp Router provider for OpenCode

This local npm package registers Ramp Router as a native OpenCode provider on
both OpenCode v1 (`>= 1.18.3`) and OpenCode v2. When OpenCode starts, the
plugin authenticates to `GET /v1/models` and exposes exactly the models
available to the configured Router API key, with each model's own context and
output limits, pricing (including cache rates), reasoning-effort variants, and
deprecation state taken from Router's metadata. Model requests use the OpenAI
Responses API. Rows Router marks as unable to serve Responses traffic (for
example TypeSafe, which answers only on `/v1/systemone`) are left out.

The recommended installer is:

```bash
ramp router configure opencode
```

It asks the installed `opencode` binary which generation it is and writes the
matching layout (below). Set `RAMP_OPENCODE_MAJOR=1` or `=2` to override the
detection when the binary is not reachable from the shell running `ramp`.

## One package, both generations

The package uses the dual shape OpenCode documents for the v1→v2 transition:
its default export carries both a v1 `server()` and a v2 `setup(ctx)`. v1 calls
`server()` and mutates `config.provider[providerID]` from the `config` hook; v2
calls `setup()`, which runs discovery once and registers the provider through
`ctx.provider.transform`. Neither generation runs the other's implementation,
and both offer the same model catalog and effort menu.

| | OpenCode v1 | OpenCode v2 |
| --- | --- | --- |
| Server config | `~/.config/opencode/opencode.json`, `"plugin": [[package, options]]` | `~/.config/opencode/opencode.json`, `"plugins": [{ "package", "options" }]` |
| Sidebar config | `~/.config/opencode/tui.json`, same tuple | `~/.config/opencode/cli.json`, same object |
| Runtime package | `@ai-sdk/openai` | `@opencode/ai/providers/openai/responses` |
| Reasoning variants | object keyed by effort | array with an `id` per effort |
| Deprecated model | `status: "deprecated"` | `enabled: false` (kept in the catalog, out of the picker) |

On v2 the plugin also states each model's default reasoning effort and the
exact `include` list Router publishes for it, because v2's Responses runtime
would otherwise guess both from the model's name and ask every upstream for
`reasoning.encrypted_content`.

For development, install this package by adding its directory to OpenCode's
global plugin array. Both forms accept `apiKey`, `baseURL`, `usageBaseURL`,
`providerID`, `name`, `apiKeyEnv`, `rampCliVersion`, and `rampExecutable`:

```jsonc
// OpenCode v1
{
  "plugin": [
    [
      "file:///absolute/path/to/opencode-provider",
      { "apiKey": "...", "baseURL": "https://router-api.ramp.com/v1" }
    ]
  ]
}

// OpenCode v2
{
  "plugins": [
    {
      "package": "file:///absolute/path/to/opencode-provider",
      "options": { "apiKey": "...", "baseURL": "https://router-api.ramp.com/v1" }
    }
  ]
}
```

The API key is resolved from the `apiKey` option, then `RAMP_ROUTER_API_KEY`
(or the `apiKeyEnv` option), then the legacy `LLM_GATEWAY_API_KEY`. On v2 the
plugin runs inside OpenCode's shared background service, whose environment is
not the shell that launched the terminal client, so prefer the `apiKey` option
(which `ramp router configure opencode` writes) over an environment variable.

Reasoning-capable models expose one OpenCode effort variant per level Router
reports the model actually accepts; the variant carries a reasoning summary
request and an `include` list only where Router says the upstream reads them.

## Session cost sidebar

The package's `./tui` entrypoint registers a section in OpenCode's session
sidebar, below the built-in Context/MCP/LSP/Todo sections. OpenCode's terminal
client loads sidebar plugins only from its own config, never from
`opencode.json` alone: v1 reads `tui.json`, and v2 reads `cli.json` (v2 does
auto-load the TUI half of a server plugin, but without its options, so the
credential and dashboard origin never reach it). `ramp router configure
opencode` therefore writes the same plugin entry to both files. The section
renders the Claude Code status line's exact layout:

```
Switchyard enabled

Routed to: GPT-5.4 via OpenAI  -63% vs Claude Opus 5
Ramp          █████████░░░░░░░░░░░░░░░ $0.42
Claude Opus 5 ████████████████████████ $1.13
```

"Switchyard enabled" leads in NVIDIA green when Router reports it. The header
bolds the model the session last routed to, with the percent delta beside it
in Ramp yellow. Both bars span the same fixed width, filled `█` scaled to the
larger figure and padded with gray `░`; the Ramp fill is Ramp yellow and the
reference fill is Anthropic terracotta. The bar rows are sized to fit the
sidebar's fixed 36-column content area, capped at Claude Code's 24 cells and
never below 10.

The figures come from Router's session-usage endpoint, refreshed when a turn
ends (`session.idle`) and queried with the OpenCode session id. Both OpenCode
generations already send that id to Router as `X-Session-Id` on every model
request (v2 also sends `x-opencode-session`), so no request hook is needed.
"Routed to" names the model Router last recorded for the session (via
usage-event ingestion, so it can trail the live turn by a few seconds), and the
section is hidden entirely for sessions Router has not billed.

The endpoint lives on the Router dashboard origin, not the data plane. It is
resolved from the `usageBaseURL` option (written by `ramp router configure
opencode`), then the `RAMP_ROUTER_USAGE_BASE_URL` environment variable, and
otherwise derived from `baseURL`. Every failure is silent: the display never
interrupts a working chat.

## Layout

`index.ts` and `tui.ts` at the package root forward to `src/`. OpenCode v2
loads a local plugin directory by resolving `<dir>/index` and `<dir>/tui`
directly rather than through `package.json` exports, so the entrypoints must
live at the root; v1 follows the `exports` map, which points at the same files.
