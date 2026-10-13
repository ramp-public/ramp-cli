import assert from "node:assert/strict"
import { afterEach, describe, it, mock } from "node:test"

import { parseRouterMetadata } from "../src/discovery.ts"
import plugin from "../src/index.ts"

function routerMetadata() {
  return {
    schema_version: 1,
    limits: { context_window: 1_000_000, max_output_tokens: 128_000 },
    pricing: {
      input: "2",
      output: "10",
      cache_read_input: "1/5",
      cache_write_input: "0",
      long_context_token_threshold: 272_000,
      long_context: { input: "4", output: "15", cache_read_input: "2/5" },
    },
  }
}

describe("standard-price context windows", () => {
  const cases = [
    ["caps at the inclusive standard-price threshold", {}, 272_000],
    ["keeps an unpriced window", { pricing: undefined }, 1_000_000],
    ["keeps a window without a long-context band", { long_context: undefined }, 1_000_000],
    ["keeps a window without a threshold", { long_context_token_threshold: undefined }, 1_000_000],
    ["ignores an incomplete price band", { long_context: { input: "4" } }, 1_000_000],
    ["ignores a malformed price band", { long_context: { input: "bad", output: "15" } }, 1_000_000],
    ["accepts fractional price strings", { long_context: { input: "9/2", output: "31/2" } }, 272_000],
    ["never enlarges a smaller window", { context_window: 128_000 }, 128_000],
    ["keeps a window equal to the threshold", { context_window: 272_000 }, 272_000],
    ["caps a window one token above the threshold", { context_window: 272_001 }, 272_000],
    ["never enlarges a window below the threshold", { long_context_token_threshold: 2_000_000 }, 1_000_000],
    ...[null, 0, -1, 1.5, "272000", Number.MAX_SAFE_INTEGER + 1, NaN, Infinity].map(
      (threshold) => [
        `ignores an invalid threshold: ${threshold}`,
        { long_context_token_threshold: threshold },
        1_000_000,
      ],
    ),
  ]

  for (const [name, overrides, expected] of cases) {
    it(name, () => {
      const metadata = routerMetadata()
      const { context_window, ...prices } = overrides
      if ("pricing" in prices) metadata.pricing = prices.pricing
      else Object.assign(metadata.pricing, prices)
      if (context_window !== undefined) metadata.limits.context_window = context_window
      const parsed = parseRouterMetadata(metadata, undefined, "test-model")
      assert.equal(parsed.contextWindow, expected)
      assert.equal(parsed.maxOutputTokens, 128_000)
      if (metadata.pricing) {
        assert.deepEqual(parsed.pricing, { input: 2, output: 10, cacheRead: 0.2, cacheWrite: 0 })
      }
      assert.equal(metadata.limits.context_window, overrides.context_window ?? 1_000_000)
    })
  }
})

afterEach(() => mock.restoreAll())

it("registers the capped window in OpenCode v1 and v2", async () => {
  mock.method(globalThis, "fetch", async () => new Response(JSON.stringify({
    data: [{ id: "test-model", router: routerMetadata() }],
  })))
  const options = { apiKey: "test-secret", baseURL: "http://localhost:8002" }
  const hooks = await plugin.server({}, options)
  const config = {}
  await hooks.config(config)
  assert.deepEqual(config.provider["ramp-router"].models["test-model"].limit, {
    context: 272_000,
    output: 128_000,
  })

  const add = mock.fn()
  await plugin.setup({
    options,
    provider: {
      transform: async (callback) => callback({ get: () => undefined, add }),
    },
    agent: {
      transform: async () => ({ dispose: async () => {} }),
      reload: async () => {},
      list: async () => ({ data: [] }),
    },
    model: { transform: async () => {}, reload: async () => {} },
    event: { subscribe: () => (async function* () {})() },
  })
  assert.deepEqual(add.mock.calls[0].arguments[0].models[0].limit, {
    context: 272_000,
    output: 128_000,
  })
})
