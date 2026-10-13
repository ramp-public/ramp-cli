import assert from "node:assert/strict"
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { afterEach, describe, it, mock } from "node:test"
import * as anthropic from "@earendil-works/pi-ai/api/anthropic-messages"
import * as openai from "@earendil-works/pi-ai/api/openai-responses"

import { parseRouterMetadata } from "../src/discovery.ts"
import registerRouterProvider from "../src/index.ts"

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
  const tier = { inputTokensAbove: 272_000, input: 4, output: 15, cacheRead: 0.4, cacheWrite: 0 }
  const cases = [
    ["caps at the inclusive standard-price threshold", {}, 272_000, tier],
    ["keeps an unpriced window", { pricing: undefined }, 1_000_000],
    ["keeps a window without a long-context band", { long_context: undefined }, 1_000_000],
    ["keeps a window without a threshold", { long_context_token_threshold: undefined }, 1_000_000],
    ["ignores an incomplete price band", { long_context: { input: "4" } }, 1_000_000],
    ["ignores a malformed price band", { long_context: { input: "bad", output: "15" } }, 1_000_000],
    ["accepts fractional price strings", { long_context: { input: "9/2", output: "31/2" } }, 272_000,
      { ...tier, input: 4.5, output: 15.5, cacheRead: 0 }],
    ["never enlarges a smaller window", { context_window: 128_000 }, 128_000, tier],
    ["keeps a window equal to the threshold", { context_window: 272_000 }, 272_000, tier],
    ["caps a window one token above the threshold", { context_window: 272_001 }, 272_000, tier],
    ["never enlarges a window below the threshold", { long_context_token_threshold: 2_000_000 }, 1_000_000,
      { ...tier, inputTokensAbove: 2_000_000 }],
    ...[null, 0, -1, 1.5, "272000", Number.MAX_SAFE_INTEGER + 1, NaN, Infinity].map(
      (threshold) => [
        `ignores an invalid threshold: ${threshold}`,
        { long_context_token_threshold: threshold },
        1_000_000,
      ],
    ),
  ]

  for (const [name, overrides, expected, expectedTier] of cases) {
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
        assert.deepEqual(parsed.pricing, {
          input: 2, output: 10, cacheRead: 0.2, cacheWrite: 0,
          ...(expectedTier ? { tiers: [expectedTier] } : {}),
        })
      }
      assert.equal(metadata.limits.context_window, overrides.context_window ?? 1_000_000)
    })
  }
})

afterEach(() => mock.restoreAll())

it("publishes, refreshes, and caches Pi's capped window", async () => {
  const home = mkdtempSync(join(tmpdir(), "pi-long-context-"))
  const previousHome = process.env.PI_CODING_AGENT_DIR
  const previousOffline = process.env.PI_OFFLINE
  process.env.PI_CODING_AGENT_DIR = home
  delete process.env.PI_OFFLINE
  try {
    writeFileSync(join(home, "auth.json"), JSON.stringify({
      "ramp-router": { type: "api_key", key: "test-secret" },
    }))
    const metadata = routerMetadata()
    mock.method(globalThis, "fetch", async () => new Response(JSON.stringify({
      data: [{ id: "test-model", owned_by: "vertex", router: metadata }],
    })))
    const registerProvider = mock.fn()
    await registerRouterProvider({ registerProvider })
    const [provider] = registerProvider.mock.calls[0].arguments
    assert.equal(provider.getModels()[0].contextWindow, 272_000)
    assert.equal(provider.getModels()[0].maxTokens, 128_000)

    metadata.pricing.long_context_token_threshold = 200_000
    await provider.refreshModels({
      credential: { type: "api_key", key: "test-secret" },
      allowNetwork: true,
      signal: new AbortController().signal,
      publish: async ({ update }) => {
        update?.()
        return true
      },
    })
    assert.equal(provider.getModels()[0].contextWindow, 200_000)
    assert.equal(provider.getModels()[0].cost.tiers[0].inputTokensAbove, 200_000)
    const cached = JSON.parse(readFileSync(join(home, "ramp-router-model-cache.json"), "utf8"))
    assert.equal(cached.models[0].contextWindow, 200_000)

    process.env.PI_OFFLINE = "1"
    mock.method(globalThis, "fetch", async () => {
      throw new Error("offline")
    })
    const offlineProvider = mock.fn()
    await registerRouterProvider({ registerProvider: offlineProvider })
    assert.equal(offlineProvider.mock.calls[0].arguments[0].getModels()[0].contextWindow, 200_000)
  } finally {
    if (previousHome === undefined) delete process.env.PI_CODING_AGENT_DIR
    else process.env.PI_CODING_AGENT_DIR = previousHome
    if (previousOffline === undefined) delete process.env.PI_OFFLINE
    else process.env.PI_OFFLINE = previousOffline
    rmSync(home, { recursive: true, force: true })
  }
})

async function withOutputProvider(ownedBy, run) {
  const home = mkdtempSync(join(tmpdir(), "pi-output-budget-"))
  const keys = ["PI_CODING_AGENT_DIR", "PI_OFFLINE", "RAMP_ROUTER_BASE_URL"]
  const previous = Object.fromEntries(keys.map((key) => [key, process.env[key]]))
  process.env.PI_CODING_AGENT_DIR = home
  process.env.RAMP_ROUTER_BASE_URL = "http://localhost:8002"
  delete process.env.PI_OFFLINE
  try {
    writeFileSync(join(home, "auth.json"), JSON.stringify({
      "ramp-router": { type: "api_key", key: "test-secret" },
    }))
    const id = ownedBy === "anthropic" ? "claude-sonnet-4-6" : "gpt-5.4"
    mock.method(globalThis, "fetch", async () => new Response(JSON.stringify({
      data: [{ id, owned_by: ownedBy, router: routerMetadata() }],
    })))
    const registerProvider = mock.fn()
    await registerRouterProvider({ registerProvider })
    const [provider] = registerProvider.mock.calls[0].arguments
    await run(provider, home)
  } finally {
    for (const key of keys) {
      if (previous[key] === undefined) delete process.env[key]
      else process.env[key] = previous[key]
    }
    rmSync(home, { recursive: true, force: true })
  }
}

describe("output budgets", () => {
  for (const ownedBy of ["openai", "anthropic"]) {
    it(`matches native ${ownedBy} output budgeting`, async () => {
      await withOutputProvider(ownedBy, async (provider, home) => {
        const [model] = provider.getModels()
        assert.equal(model.contextWindow, 272_000)
        const contextForTokens = (tokens) => ({ messages: [
          { role: "user", content: [{ type: "text", text: "hello" }], timestamp: 1 },
          {
            role: "assistant",
            content: [{ type: "text", text: "ok" }],
            api: model.api,
            provider: model.provider,
            model: model.id,
            usage: {
              input: tokens - 1, output: 1, totalTokens: tokens, cacheRead: 0, cacheWrite: 0,
              cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
            },
            stopReason: "stop",
            timestamp: 2,
          },
        ] })
        const context = contextForTokens(255_000)
        const capture = async (activeProvider, activeModel, method, options = {}, input = context) => {
          let payload
          const result = await activeProvider[method](activeModel, input, {
            apiKey: "test-secret",
            maxRetries: 0,
            fetch: async (_url, init) => {
              payload = JSON.parse(init.body)
              return new Response(JSON.stringify({ error: { message: "fixture stop" } }), {
                status: 400,
                headers: { "content-type": "application/json" },
              })
            },
            ...options,
          }).result()
          assert.equal(result.stopReason, "error")
          assert.ok(payload)
          return ownedBy === "anthropic" ? payload.max_tokens : payload.max_output_tokens
        }
        const native = ownedBy === "anthropic" ? anthropic : openai
        const matchesNative = async (activeProvider, activeModel, method, options = {}, input = context) => {
          const actual = await capture(activeProvider, activeModel, method, options, input)
          assert.equal(actual, await capture(native, activeModel, method, options, input))
          return actual
        }
        for (const method of ["streamSimple", "stream"]) {
          await matchesNative(provider, model, method)
          assert.equal(await matchesNative(provider, model, method, { maxTokens: 4_096 }), 4_096)
          await matchesNative(provider, { ...model, maxTokens: 32_768 }, method)
        }
        assert.equal(await matchesNative(provider, model, "streamSimple"), 12_904)
        assert.equal(await matchesNative(provider, model, "streamSimple", {}, contextForTokens(1_000)), 128_000)
        assert.equal(provider.getModels()[0].contextWindow, 272_000)

        let reasoningPayload
        await matchesNative(provider, { ...model, reasoning: true }, "streamSimple", {
          reasoning: "high",
          onPayload: (payload) => {
            reasoningPayload = structuredClone(payload)
          },
        })
        if (ownedBy === "openai") assert.equal(reasoningPayload.reasoning.effort, "high")
        else assert.ok(reasoningPayload.thinking)

        await matchesNative(provider, { ...model, contextWindow: 260_000 }, "streamSimple")
        assert.equal(await matchesNative(provider, { ...model, contextWindow: 1_000_000 }, "streamSimple"), 128_000)

        process.env.PI_OFFLINE = "1"
        mock.method(globalThis, "fetch", async () => {
          throw new Error("offline")
        })
        const offlineProvider = mock.fn()
        await registerRouterProvider({ registerProvider: offlineProvider })
        const cached = offlineProvider.mock.calls[0].arguments[0]
        const [cachedModel] = cached.getModels()
        assert.equal(cachedModel.contextWindow, 272_000)
        assert.equal(await matchesNative(cached, cachedModel, "streamSimple"), 12_904)

        const cachePath = join(home, "ramp-router-model-cache.json")
        const cache = JSON.parse(readFileSync(cachePath, "utf8"))
        for (const window of [undefined, 0, -1, 1.5, "272000"]) {
          const invalid = structuredClone(cache)
          invalid.models[0].contextWindow = window
          writeFileSync(cachePath, JSON.stringify(invalid))
          const invalidProvider = mock.fn()
          await registerRouterProvider({ registerProvider: invalidProvider })
          assert.deepEqual(invalidProvider.mock.calls[0].arguments[0].getModels(), [])
        }
        for (const version of [6, 7, 8]) {
          writeFileSync(cachePath, JSON.stringify({ ...cache, version }))
          const legacyProvider = mock.fn()
          await registerRouterProvider({ registerProvider: legacyProvider })
          assert.deepEqual(legacyProvider.mock.calls[0].arguments[0].getModels(), [])
        }
      })
    })
  }
})
