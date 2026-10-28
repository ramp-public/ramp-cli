import assert from "node:assert/strict"
import { afterEach, describe, it, mock } from "node:test"

import { parseRouterMetadata } from "../src/discovery.ts"
import plugin from "../src/index.ts"

function routerMetadata() {
  return {
    schema_version: 1,
    provider_model: "gpt-5.4",
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

describe("provider identity context policy", () => {
  const cases = [
    ["provider GPT outranks a readable id", "GPT 6.1 Sol", { provider_model: "gpt-6.1-sol" }, 272_000],
    ...["openai/gpt-6.1-sol", "openai:gpt-6.1-sol", "openai.gpt-6.1-sol"].map(
      (provider_model) => [provider_model, "Sol", { provider_model }, 272_000],
    ),
    ["non-GPT provider outranks GPT alias", "gpt-route", { provider_model: "claude-haiku-5-5", request_name: "gpt-route" }, 1_000_000],
    ["non-GPT provider outranks GPT label", "grok-4.7", { provider_model: "grok-4.7", display_name: "gpt-6.1-sol" }, 1_000_000],
    ["missing provider falls back to request name", "Readable", { provider_model: undefined, request_name: "openai/gpt-6.1-sol" }, 272_000],
    ["empty provider falls back to request name", "Readable", { provider_model: "", request_name: "gpt-6.1-sol" }, 272_000],
    ["request identity outranks GPT id", "gpt-route", { provider_model: undefined, request_name: "grok-4.7" }, 1_000_000],
    ["missing identities fall back to id", "openai:gpt-6.1-sol", { provider_model: undefined }, 272_000],
    ["readable label is not identity", "Sol", { provider_model: undefined, display_name: "gpt-6.1-sol" }, 1_000_000],
    ["GPT substring is not GPT identity", "not-gpt-model", { provider_model: "not-gpt-model" }, 1_000_000],
    ["full window without pricing", "haiku", { provider_model: "claude-haiku-5-5", pricing: undefined }, 1_000_000],
  ]
  for (const [name, id, overrides, expected] of cases) {
    it(name, () => {
      const original = { ...routerMetadata(), ...overrides }
      const gpt = parseRouterMetadata({ ...original, provider_model: "gpt-6.1-sol" }, undefined, id)
      const parsed = parseRouterMetadata(original, undefined, id)
      assert.equal(parsed.contextWindow, expected)
      assert.deepEqual({ ...parsed, contextWindow: gpt.contextWindow }, gpt)
      assert.equal(original.limits.context_window, 1_000_000)
    })
  }
})

it("registers GPT caps and full non-GPT windows in OpenCode v1 and v2", async () => {
  const models = [
    { id: "test-model", router: routerMetadata() },
    { id: "gpt-route", router: { ...routerMetadata(), provider_model: "claude-haiku-5-5" } },
    { id: "grok-4.7", router: { ...routerMetadata(), provider_model: "grok-4.7", limits: { context_window: 500_000, max_output_tokens: 128_000 } } },
  ]
  mock.method(globalThis, "fetch", async () => new Response(JSON.stringify({
    data: models,
  })))
  const options = { apiKey: "test-secret", baseURL: "http://localhost:8002" }
  const hooks = await plugin.server({}, options)
  const config = {}
  await hooks.config(config)
  assert.deepEqual(config.provider["ramp-router"].models["test-model"].limit, {
    context: 272_000,
    output: 128_000,
  })
  assert.equal(config.provider["ramp-router"].models["gpt-route"].limit.context, 1_000_000)
  assert.equal(config.provider["ramp-router"].models["grok-4.7"].limit.context, 500_000)

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
  assert.deepEqual(add.mock.calls[0].arguments[0].models.map((model) => model.limit.context), [272_000, 1_000_000, 500_000])
})
