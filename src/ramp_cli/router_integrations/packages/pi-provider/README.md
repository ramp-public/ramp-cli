# Ramp Router provider for Pi

This local npm package registers Ramp Router as a native dynamic Pi provider.
Pi refreshes authenticated `GET /v1/models` discovery through its provider
credential store and caches exactly the models available to the configured
Router API key. Anthropic-owned models use Router's Anthropic Messages
compatibility endpoint; other models use the OpenAI Responses API. Rows Router
marks as unable to serve their selected API are left out.

On Pi 0.99 and later, Router's System One models (TypeSafe's Jev) are
registered as Pi classifier models and answered through `/v1/systemone`. Like
other classifiers they do not appear in `/model`; codemode scripts reach them
with `models.classify()` and extensions with `ctx.modelRegistry.classify()`.
Calls carry the active Pi session's Router attribution. Older Pi versions do
not list them.
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

Production Router is the default endpoint. Set `RAMP_ROUTER_BASE_URL` only to
override it. Reasoning-capable models expose their supported Pi thinking
levels; non-OpenAI reasoning providers currently use the portable `off`,
`low`, `medium`, and `high` levels.
