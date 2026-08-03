# Ramp Router provider for Pi

This local npm package registers Ramp Router as a native dynamic Pi provider.
Pi refreshes authenticated `GET /v1/models` discovery through its provider
credential store and caches exactly the models available to the configured
Router API key. Model requests use the OpenAI Responses API.

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
