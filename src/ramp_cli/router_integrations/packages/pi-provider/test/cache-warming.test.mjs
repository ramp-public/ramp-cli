import assert from "node:assert/strict"
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { afterEach, beforeEach, it, mock } from "node:test"
import { ModelRuntime } from "@earendil-works/pi-coding-agent"
import registerRouterProvider from "../src/index.ts"

const { CacheWarmer } = await import(new URL(
  "./core/cache-warmer.js", import.meta.resolve("@earendil-works/pi-coding-agent"),
))
const modelID = "claude-fable-5"
let home

beforeEach(() => {
  home = mkdtempSync(join(tmpdir(), "router-cache-warming-"))
  process.env.PI_CODING_AGENT_DIR = home
  process.env.RAMP_ROUTER_BASE_URL = "http://localhost:8002"
  writeFileSync(join(home, "auth.json"), JSON.stringify({
    "ramp-router": { type: "api_key", key: "test-secret" },
  }))
  mock.method(globalThis, "fetch", async () => new Response(JSON.stringify({ data: [
    { id: modelID, owned_by: "anthropic", router: metadata(modelID) },
    { id: "gpt-5.4", owned_by: "openai", router: metadata("gpt-5.4") },
    { id: "router-model", owned_by: "router", router: metadata("router-model") },
  ] })))
})

afterEach(() => {
  mock.restoreAll()
  mock.timers.reset()
  delete process.env.PI_CODING_AGENT_DIR
  delete process.env.RAMP_ROUTER_BASE_URL
  delete process.env.PI_OFFLINE
  rmSync(home, { recursive: true, force: true })
})

function metadata(id) {
  return {
    schema_version: 1,
    request_name: id,
    display_name: id,
    listing: { order: 0 },
    limits: { context_window: 128000, max_output_tokens: 16384 },
    pricing: { input: "10", output: "25", cache_read_input: "0.5", cache_write_input: "12.5" },
    capabilities: {
      modalities: { input: ["text"], output: ["text"] },
      tools: { supported: true },
      reasoning: { efforts: [{ value: "high" }], default_effort: "high" },
    },
  }
}

async function register() {
  let provider
  await registerRouterProvider({ registerProvider: (value) => { provider = value } })
  return provider
}

it("publishes native cache lifetimes and replay compatibility online and from old caches", async () => {
  const runtime = await ModelRuntime.create({
    modelsPath: null, refreshOnCreate: false, allowModelNetwork: false,
  })
  const native = runtime.getProvider("anthropic").getModels().find(({ id }) => id === modelID)
  assert.deepEqual(native.promptCache, { short: 300, long: 3600 })
  assert.equal(native.compat.forceAdaptiveThinking, true)

  for (const offline of [false, true]) {
    if (offline) process.env.PI_OFFLINE = "1"
    const provider = await register()
    const model = provider.getModels().find(({ id }) => id === modelID)
    assert.deepEqual(model.promptCache, native.promptCache)
    assert.equal(model.compat.forceAdaptiveThinking, true)
    assert.equal(model.cost.cacheRead, 0.5)
    assert.equal(model.contextWindow, 128000)
    assert.equal(provider.getAllModels().find(({ id }) => id === modelID).promptCache.short, 300)
    model.promptCache.short = 1
    assert.equal(provider.getModels().find(({ id }) => id === modelID).promptCache.short, 300)
    assert.equal(provider.getModels().find(({ id }) => id === "router-model").promptCache, undefined)
    assert.equal(provider.getModels().find(({ id }) => id === "gpt-5.4").promptCache, undefined)
  }
  assert.equal(globalThis.fetch.mock.callCount(), 1)
  const cache = JSON.parse(readFileSync(join(home, "ramp-router-model-cache.json"), "utf8"))
  assert.equal(JSON.stringify(cache).includes("promptCache"), false)
})

it("takes updated native lifetimes without refreshing Router's cached catalog", async () => {
  await register()
  process.env.PI_OFFLINE = "1"
  const create = ModelRuntime.create.bind(ModelRuntime)
  mock.method(ModelRuntime, "create", async (options) => {
    const runtime = await create(options)
    const getProvider = runtime.getProvider.bind(runtime)
    runtime.getProvider = (id) => {
      const provider = getProvider(id)
      return id === "anthropic" ? {
        ...provider,
        getModels: () => provider.getModels().map((model) => ({
          ...model, promptCache: { short: 120 },
        })),
      } : provider
    }
    return runtime
  })
  const provider = await register()
  assert.deepEqual(provider.getModels().find(({ id }) => id === modelID).promptCache, { short: 120 })
  assert.equal(globalThis.fetch.mock.callCount(), 1)
})

for (const owner of ["openai", "router"]) {
  it(`inherits OpenAI TTLs only for OpenAI-owned models (${owner})`, async () => {
    const create = ModelRuntime.create.bind(ModelRuntime)
    mock.method(ModelRuntime, "create", async (options) => {
      const runtime = await create(options)
      const getProvider = runtime.getProvider.bind(runtime)
      runtime.getProvider = (id) => {
        const provider = getProvider(id)
        return id === "openai" ? {
          ...provider,
          getModels: () => provider.getModels().map((model) => ({
            ...model, promptCache: { short: 300 },
          })),
        } : provider
      }
      return runtime
    })
    globalThis.fetch.mock.mockImplementation(async () => new Response(JSON.stringify({ data: [
      { id: "gpt-5.4", owned_by: owner, router: metadata("gpt-5.4") },
    ] })))
    const provider = await register()
    const [model] = provider.getModels()
    assert.deepEqual(model.promptCache, owner === "openai" ? { short: 300 } : undefined)
    const warmer = new CacheWarmer(provider, {}, () => "idle")
    warmer.start({ model, context: { messages: [] }, options: { cacheRetention: "long" } }, () => true)
    assert.equal(warmer.status.reason, "cache lifetime unavailable")
    warmer.start({ model, context: { messages: [] }, options: { cacheRetention: "none" } }, () => true)
    assert.equal(warmer.status.reason, "request disabled prompt caching")
  })
}

it("lets Pi schedule and replay an idle warm request through the Router adapter", { timeout: 10000 }, async () => {
  const provider = await register()
  const runtime = await ModelRuntime.create({
    modelsPath: null, refreshOnCreate: false, allowModelNetwork: false,
  })
  runtime.registerNativeProvider(provider)
  await runtime.refresh({ providers: ["ramp-router"], allowNetwork: false })
  const model = runtime.getModel("ramp-router", modelID)
  assert.deepEqual(model.promptCache, { short: 300, long: 3600 })
  mock.timers.enable({ apis: ["setTimeout", "Date"] })
  let onWarmed
  const warmed = new Promise((resolve) => { onWarmed = resolve })
  const session = {
    getBranch: () => [{ type: "message", message: {
      role: "assistant", usage: { input: 100000, cacheRead: 0, cacheWrite: 0 },
    } }],
    appendUsage: mock.fn(),
  }
  const warmer = new CacheWarmer(runtime, session, () => "idle")
  warmer.onWarmed = onWarmed
  let payload
  const fetcher = mock.fn(async (_url, init) => {
    payload = JSON.parse(init.body)
    const events = [
      { type: "message_start", message: {
        id: "msg-warm", type: "message", role: "assistant", model: modelID,
        content: [], stop_reason: null, stop_sequence: null,
        usage: { input_tokens: 0, output_tokens: 0, cache_read_input_tokens: 100000 },
      } },
      { type: "message_delta", delta: { stop_reason: "end_turn", stop_sequence: null }, usage: { output_tokens: 1 } },
      { type: "message_stop" },
    ]
    return new Response(events.map((event) => `event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`).join(""), {
      headers: { "content-type": "text/event-stream" },
    })
  })
  try {
    warmer.start({
      model,
      context: { messages: [{ role: "user", content: "hello", timestamp: Date.now() }] },
      options: { apiKey: "test-secret", reasoning: "high", cacheRetention: "short", fetch: fetcher },
    }, () => true)
    warmer.onAgentSettled()
    assert.equal(warmer.status.state, "scheduled")
    assert.equal(warmer.status.decision.phase, "idle")
    mock.timers.tick(270000)
    await warmed
    assert.equal(fetcher.mock.callCount(), 1)
    assert.equal(payload.model, modelID)
    assert.equal(payload.max_tokens, 1)
    assert.equal(payload.thinking.type, "adaptive")
    assert.equal(session.appendUsage.mock.calls[0].arguments[0], "cache_warm")
  } finally {
    warmer.cancel()
  }
})
