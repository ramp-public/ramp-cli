# Ramp Router provider for OpenCode

This local npm package registers Ramp Router as a native OpenCode provider.
When OpenCode starts, the plugin authenticates to `GET /v1/models` and exposes
exactly the models available to the configured Router API key. Model requests
use the OpenAI Responses API.

The recommended installer is:

```bash
ramp router configure opencode
```

For development, install this package by adding its directory to OpenCode's
global `plugin` array. The tuple form accepts `apiKey`, `baseURL`,
`usageBaseURL`, `providerID`, `name`, `apiKeyEnv`, `contextWindow`, and
`maxOutputTokens`:

```json
{
  "plugin": [
    [
      "file:///absolute/path/to/opencode-provider",
      {
        "apiKey": "...",
        "baseURL": "https://router-api.ramp.com/v1"
      }
    ]
  ]
}
```

Reasoning-capable models expose OpenCode effort variants. OpenAI model
families use OpenCode's model-specific effort levels; other Router reasoning
providers currently expose the portable `low`, `medium`, and `high` levels.

## Session cost sidebar

The package's `./tui` entrypoint registers a section in OpenCode's session
sidebar, below the built-in Context/MCP/LSP/Todo sections. OpenCode's TUI
loads plugins only from `tui.json` (never from `opencode.json`), so
`ramp router configure opencode` writes the same plugin tuple to both files:
`opencode.json` for the server-side provider and `tui.json` for this sidebar.
The section renders the Claude Code status line's exact layout:

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
ends (`session.idle`) and queried with the OpenCode session id, which
OpenCode `>= 1.18.3` already sends to Router on every request. "Routed to"
names the model Router last recorded for the session (via usage-event
ingestion, so it can trail the live turn by a few seconds), and the section
is hidden entirely for sessions Router has not billed.

The endpoint lives on the Router dashboard origin, not the data plane. It is
resolved from the `usageBaseURL` option (written by `ramp router configure
opencode`), then the `RAMP_ROUTER_USAGE_BASE_URL` environment variable, and
otherwise derived from `baseURL`. Every failure is silent: the display never
interrupts a working chat.
