import assert from "node:assert/strict"
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { afterEach, describe, it, mock } from "node:test"
import { calculateCost } from "@earendil-works/pi-ai"
import { ModelRuntime } from "@earendil-works/pi-coding-agent"

import { parseRouterMetadata } from "../src/discovery.ts"
import registerRouterProvider from "../src/index.ts"

function metadata() {
  return {
    schema_version: 1,
    limits: { context_window: 1_000_000, max_output_tokens: 128_000 },
    pricing: {
      input: "2", output: "10", cache_read_input: "1/5",
      cache_write_input: "0", cache_write_input_5m: "5/2",
      long_context_token_threshold: 272_000,
      long_context: {
        input: "4", output: "15", cache_read_input: "2/5",
        cache_write_input: "0", cache_write_input_5m: "5",
      },
    },
  }
}

const expectedCost = {
  input: 2, output: 10, cacheRead: 0.2, cacheWrite: 2.5,
  tiers: [{ inputTokensAbove: 272_000, input: 4, output: 15, cacheRead: 0.4, cacheWrite: 5 }],
}

function usage(overrides) {
  return {
    input: 0, output: 128_000, cacheRead: 0, cacheWrite: 0, ...overrides,
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
  }
}

function assertCost(cost, expected) {
  for (const key of Object.keys(expected)) {
    assert.ok(Math.abs(cost[key] - expected[key]) < 1e-10, `${key}: ${cost[key]} != ${expected[key]}`)
  }
}

afterEach(() => mock.restoreAll())

describe("native Pi context pricing", () => {
  const cases = [
    ["keeps the standard band at the threshold", { input: 272_000 },
      { input: 0.544, output: 1.28, cacheRead: 0, cacheWrite: 0, total: 1.824 }],
    ["reprices the whole request one token above", { input: 272_001 },
      { input: 1.088004, output: 1.92, cacheRead: 0, cacheWrite: 0, total: 3.008004 }],
    ["includes cache reads and writes at the threshold", { input: 72_000, cacheRead: 199_000, cacheWrite: 1_000 },
      { input: 0.144, output: 1.28, cacheRead: 0.0398, cacheWrite: 0.0025, total: 1.4663 }],
    ["uses long rates when cached input crosses the threshold", { input: 72_001, cacheRead: 199_000, cacheWrite: 1_000 },
      { input: 0.288004, output: 1.92, cacheRead: 0.0796, cacheWrite: 0.005, total: 2.292604 }],
    ["prices 1h writes from the selected band's input rate", { input: 72_001, cacheRead: 198_000, cacheWrite: 2_000, cacheWrite1h: 1_000 },
      { input: 0.288004, output: 1.92, cacheRead: 0.0792, cacheWrite: 0.013, total: 2.300204 }],
    ["does not select a tier from output length", { input: 1, output: 300_000 },
      { input: 0.000002, output: 3, cacheRead: 0, cacheWrite: 0, total: 3.000002 }],
  ]
  for (const [name, tokens, expected] of cases) {
    it(name, () => {
      const parsed = parseRouterMetadata(metadata(), undefined, "test-model")
      assert.equal(parsed.contextWindow, 272_000)
      assert.deepEqual(parsed.pricing, expectedCost)
      assertCost(calculateCost({ cost: parsed.pricing }, usage(tokens)), expected)
    })
  }
})

it("preserves native cost tiers offline and rejects malformed cached tiers", async () => {
  const home = mkdtempSync(join(tmpdir(), "pi-cost-tiers-"))
  const keys = ["PI_CODING_AGENT_DIR", "PI_OFFLINE", "RAMP_ROUTER_BASE_URL"]
  const previous = Object.fromEntries(keys.map((key) => [key, process.env[key]]))
  process.env.PI_CODING_AGENT_DIR = home
  process.env.RAMP_ROUTER_BASE_URL = "http://localhost:8002"
  delete process.env.PI_OFFLINE
  try {
    writeFileSync(join(home, "auth.json"), JSON.stringify({
      "ramp-router": { type: "api_key", key: "test-secret" },
    }))
    mock.method(globalThis, "fetch", async () => new Response(JSON.stringify({
      data: [{ id: "test-model", owned_by: "vertex", router: metadata() }],
    })))
    const registerProvider = mock.fn()
    await registerRouterProvider({ registerProvider })
    const [provider] = registerProvider.mock.calls[0].arguments
    assert.deepEqual(provider.getModels()[0].cost, expectedCost)
    const runtime = await ModelRuntime.create({
      authPath: join(home, "auth.json"), modelsPath: null,
      modelsStorePath: join(home, "models-store.json"),
      refreshOnCreate: false, allowModelNetwork: false,
    })
    runtime.registerNativeProvider(provider)
    await runtime.refresh({ providers: ["ramp-router"], allowNetwork: false })
    const effective = runtime.getModel("ramp-router", "test-model")
    assert.deepEqual(effective.cost, expectedCost)
    assertCost(calculateCost(effective, usage({ input: 272_001 })), { total: 3.008004 })
    const cachePath = join(home, "ramp-router-model-cache.json")
    const cache = JSON.parse(readFileSync(cachePath, "utf8"))
    assert.deepEqual(cache.models[0].cost, expectedCost)

    process.env.PI_OFFLINE = "1"
    const fetch = mock.method(globalThis, "fetch", async () => {
      throw new Error("offline")
    })
    const restore = async () => {
      const registration = mock.fn()
      await registerRouterProvider({ registerProvider: registration })
      return registration.mock.calls[0].arguments[0].getModels()
    }
    const [restored] = await restore()
    assert.deepEqual(restored.cost, expectedCost)
    assertCost(calculateCost(restored, usage({ input: 272_001 })), { total: 3.008004 })
    assert.equal(fetch.mock.callCount(), 0)

    const tier = expectedCost.tiers[0]
    const invalidTiers = [
      null, {}, [null],
      [{ ...tier, inputTokensAbove: "272000" }],
      [{ ...tier, inputTokensAbove: -1 }],
      [{ ...tier, inputTokensAbove: 1.5 }],
      [{ ...tier, input: "4" }],
      [{ ...tier, output: -1 }],
      [{ ...tier, cacheRead: undefined }],
      [{ ...tier, headers: { authorization: "injected" } }],
    ]
    for (const tiers of invalidTiers) {
      const invalid = structuredClone(cache)
      invalid.models[0].cost.tiers = tiers
      writeFileSync(cachePath, JSON.stringify(invalid))
      assert.deepEqual(await restore(), [])
    }
    writeFileSync(cachePath, JSON.stringify({ ...cache, version: 7 }))
    assert.deepEqual(await restore(), [])
  } finally {
    for (const key of keys) {
      if (previous[key] === undefined) delete process.env[key]
      else process.env[key] = previous[key]
    }
    rmSync(home, { recursive: true, force: true })
  }
})
