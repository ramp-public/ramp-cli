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
`providerID`, `name`, `apiKeyEnv`, `contextWindow`, and `maxOutputTokens`:

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
