# Ramp Router provider for Pi

This local npm package registers Ramp Router as a native dynamic Pi provider.
Pi refreshes authenticated `GET /v1/models` discovery through its provider
credential store and caches exactly the models available to the configured
Router API key. Anthropic-owned models use Router's Anthropic Messages
compatibility endpoint; other models use the OpenAI Responses API. Rows Router
marks as unable to serve their selected API are left out.

For GPT-class chat models with `router.pricing.long_context` and a valid
`long_context_token_threshold`, the discovered context window is capped at
that threshold so Pi compacts before entering the higher-priced band. Non-GPT
models always advertise Router's full window; Pi handles compaction independently.
GPT models without that pricing band also keep their stated window. Published
output limits and standard rates are unchanged. This is a cost-aware default,
not a hard spending limit: manual window overrides or a large prompt/tool result
can still cross it.

As in native Pi, the advertised window controls both compaction and per-request
output budgeting. Available output shrinks as the context fills. Caller output
limits and model tuning still follow Pi's normal adapter behavior.

The cost object also includes Pi's native input-token pricing tiers. Long-context
rates apply to the whole request strictly above Router's threshold, counting
cached input as well as uncached input. These tiers persist in the offline cache.

On Pi 0.99 and later, Router's System One models (TypeSafe's Jev) are
registered as Pi classifier models and answered through `/v1/systemone`. Like
other classifiers they do not appear in `/model`; codemode scripts reach them
with `models.classify()` and extensions with `ctx.modelRegistry.classify()`.
Calls carry the active Pi session's Router attribution. Older Pi versions do
not list them.

On Pi 1.1 and later, OpenAI's Decisions models (today `gpt-6-luna`) are also
classifier models, answered through `/v1/decisions` with image input. They stay
available as chat models too. A model qualifies when Router lists a
`decisions` surface for it or Pi's built-in `openai` provider lists it as a
classifier. Router bills these calls on input tokens only.
Anthropic and OpenAI models are shown only when their IDs also appear in the
installed Pi version's built-in catalog for the corresponding provider. Pi
supplies model-specific Messages and Responses compatibility settings, so
update Pi to see newly released models. Other Router-owned models still use
the Responses adapter without requiring a native Pi catalog entry.

Router sessions also show a native widget above Pi's editor after settled
turns. It reports Switchyard routing, Router's last routed model and provider,
and Ramp session cost against the Claude Opus reference cost. The built-in Pi
footer and local cost display remain unchanged.

The recommended installer is:

```bash
ramp router configure pi
```

To also use Router connector tools inside Pi (0.99 or later):

```bash
ramp router configure pi --connectors
```

This signs in with connector access and registers `ramp-connectors` in Pi's
native `mcp.json`. Run `/reload` in an existing session, then `/mcp` to inspect
the connection and tools. Tools are available through Pi's default codemode
exposure. To remove the registration, run
`ramp router connectors disable --harness pi`.

For development, verify this workspace and install the TypeScript package directly:

```bash
npm install
npm run verify
pi install ./packages/pi-provider
```

Run Pi and use `/login` to replace the stored Ramp Router API key:

```bash
pi --list-models ramp-router
pi --provider ramp-router --model <model-id> --thinking high --print \
  "Reply with exactly: ROUTER_OK"
```

A Router workspace admin can choose the model new Pi sessions start on
(Router's coding-agent settings). The extension applies it through Pi's
`setModel` on a fresh launch and on `/new`, when the key's catalog serves it,
without touching Pi's configured default model. `--continue`, `--resume`,
forks, reloads, and a launch with an explicit `--model` keep their model, and
`/model` switches within a session stand, including one made before Router
has answered. The setting is read from the dashboard origin at most once a
minute per process with a short timeout, in the background interactively
and before the first prompt in `--print`/`--mode json`, and never in
`PI_OFFLINE` mode; any failure leaves Pi's own model in place. Pi 0.84.1 and
0.84.2 are skipped: their `setModel` also rewrote the configured default.

Production Router is the default endpoint. Set `RAMP_ROUTER_BASE_URL` only to
override it. Reasoning-capable models expose their supported Pi thinking
levels; non-OpenAI reasoning providers currently use the portable `off`,
`low`, `medium`, and `high` levels.
