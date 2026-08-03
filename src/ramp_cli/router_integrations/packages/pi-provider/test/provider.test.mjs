import assert from "node:assert/strict"
import { mkdtempSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { afterEach, describe, it, mock } from "node:test"

import registerRouterProvider, {
  discoverRouterModels,
  normalizeBaseURL,
} from "../src/index.ts"

function routerMetadata(id, inputModalities = ["text", "image"]) {
  // What Router publishes for every model it serves.
  return {
    schema_version: 1,
    request_name: id,
    display_name: id,
    listing: { order: 0 },
    limits: { context_window: 128000, max_output_tokens: 16384 },
    capabilities: {
      modalities: { input: inputModalities, output: ["text"] },
      tools: { supported: true },
      reasoning: { efforts: [], default_effort: "" },
    },
  }
}

afterEach(() => {
  mock.restoreAll()
  delete process.env.RAMP_ROUTER_API_KEY
  delete process.env.RAMP_ROUTER_BASE_URL
  delete process.env.RAMP_ROUTER_CONTEXT_WINDOW
  delete process.env.RAMP_ROUTER_MAX_OUTPUT_TOKENS
  delete process.env.LLM_GATEWAY_API_KEY
  delete process.env.LLM_GATEWAY_BASE_URL
})

describe("normalizeBaseURL", () => {
  it("adds the OpenAI API version exactly once", () => {
    assert.equal(
      normalizeBaseURL("http://localhost:8002"),
      "http://localhost:8002/v1",
    )
    assert.equal(
      normalizeBaseURL("http://localhost:8002/v1/"),
      "http://localhost:8002/v1",
    )
  })
})

describe("discoverRouterModels", () => {
  it("authenticates and returns the caller's model list", async () => {
    const fetcher = mock.fn(async () =>
      new Response(
        JSON.stringify({
          object: "list",
          data: [
            { id: "model-a", object: "model", owned_by: "openai", router: routerMetadata("model-a") },
            { id: "model-b", object: "model", owned_by: "anthropic", router: routerMetadata("model-b") },
          ],
        }),
        { status: 200 },
      ),
    )

    const discovered = await discoverRouterModels({
      baseURL: "http://localhost:8002",
      apiKey: "test-secret",
      fetch: fetcher,
    })
    assert.deepEqual(
      discovered.map(({ id, ownedBy }) => ({ id, ownedBy })),
      [
        { id: "model-a", ownedBy: "openai" },
        { id: "model-b", ownedBy: "anthropic" },
      ],
    )
    // Router describes every model, so every discovered one carries limits.
    for (const model of discovered) {
      assert.equal(model.metadata.contextWindow, 128000)
      assert.equal(model.metadata.maxOutputTokens, 16384)
    }
    assert.equal(
      fetcher.mock.calls[0].arguments[0],
      "http://localhost:8002/v1/models",
    )
    assert.deepEqual(fetcher.mock.calls[0].arguments[1].headers, {
      authorization: "Bearer test-secret",
    })
  })

  it("does not include an invalid response body in its error", async () => {
    const fetcher = mock.fn(async () =>
      new Response("private response body", { status: 200 }),
    )

    await assert.rejects(
      discoverRouterModels({
        baseURL: "http://localhost:8002/v1",
        apiKey: "test-secret",
        fetch: fetcher,
      }),
      (error) =>
        error instanceof Error &&
        error.message === "Ramp Router model discovery returned invalid JSON" &&
        !error.message.includes("private response body"),
    )
  })
})

describe("Pi provider extension", () => {
  it("discovers reasoning-aware Responses models with provider auth", async () => {
    process.env.RAMP_ROUTER_BASE_URL = "http://localhost:8002"
    mock.method(globalThis, "fetch", async (_url, init) => {
      assert.deepEqual(init.headers, {
        authorization: "Bearer test-secret",
      })
      return new Response(
        JSON.stringify({
          object: "list",
          data: [
            { id: "gpt-4o", owned_by: "openai", router: routerMetadata("gpt-4o") },
            { id: "gpt-5.4", owned_by: "openai", router: routerMetadata("gpt-5.4") },
            { id: "claude-sonnet-4-6", owned_by: "anthropic", router: routerMetadata("claude-sonnet-4-6") },
            { id: "audio-only", owned_by: "openai", router: routerMetadata("audio-only", ["audio"]) },
          ],
        }),
        { status: 200 },
      )
    })
    const registerProvider = mock.fn()

    registerRouterProvider({ registerProvider })

    assert.equal(registerProvider.mock.callCount(), 1)
    const [provider] = registerProvider.mock.calls[0].arguments
    assert.equal(provider.id, "ramp-router")
    assert.equal(provider.name, "Ramp Router")
    assert.equal(provider.baseUrl, "http://localhost:8002/v1")
    assert.equal(typeof provider.stream, "function")
    assert.deepEqual(provider.getModels(), [])

    const write = mock.fn(async () => {})
    await provider.refreshModels({
      credential: { type: "api_key", key: "test-secret" },
      allowNetwork: true,
      store: {
        read: async () => undefined,
        write,
        delete: async () => {},
      },
    })

    const models = provider.getModels()
    assert.deepEqual(
      models.map((model) => model.id),
      ["gpt-4o", "gpt-5.4", "claude-sonnet-4-6", "audio-only"],
    )
    // Router says these models do not reason, so Pi offers no thinking levels
    // rather than a set derived from their names.
    assert.equal(models[0].reasoning, false)
    assert.equal(models[0].thinkingLevelMap, undefined)
    assert.equal(models[1].thinkingLevelMap, undefined)
    assert.equal(models[2].thinkingLevelMap, undefined)
    assert.equal(models[0].contextWindow, 128000)
    assert.equal(models[0].maxTokens, 16384)
    assert.deepEqual(models[0].input, ["text", "image"])
    // Unsupported-only metadata must not turn into invented image support.
    assert.deepEqual(models[3].input, ["text"])
    assert.equal(write.mock.callCount(), 1)
  })

  it("uses production Router by default", () => {
    // Pointed at an empty directory: the plugin reads the configured Router
    // from the agent directory, so a developer whose own Pi is set up would
    // otherwise see their stack here.
    process.env.PI_CODING_AGENT_DIR = mkdtempSync(join(tmpdir(), "pi-home-"))
    const registerProvider = mock.fn()

    registerRouterProvider({ registerProvider })

    const [provider] = registerProvider.mock.calls[0].arguments
    assert.equal(provider.baseUrl, "https://router-api.ramp.com/v1")
  })
})

describe("which Router the plugin calls", () => {
  it("uses the one configure recorded, so no shell variable is needed", async () => {
    const home = mkdtempSync(join(tmpdir(), "pi-home-"))
    writeFileSync(
      join(home, "ramp-router-config.json"),
      JSON.stringify({ baseUrl: "http://127.0.0.1:28362/v1" }),
    )
    process.env.PI_CODING_AGENT_DIR = home
    delete process.env.RAMP_ROUTER_BASE_URL

    assert.equal(registeredBaseUrl(), "http://127.0.0.1:28362/v1")
  })

  it("still lets the environment win for a one-off run", async () => {
    const home = mkdtempSync(join(tmpdir(), "pi-home-"))
    writeFileSync(
      join(home, "ramp-router-config.json"),
      JSON.stringify({ baseUrl: "http://127.0.0.1:28362/v1" }),
    )
    process.env.PI_CODING_AGENT_DIR = home
    process.env.RAMP_ROUTER_BASE_URL = "https://other.example/v1"

    assert.equal(registeredBaseUrl(), "https://other.example/v1")
  })

  it("falls back to production when nothing was recorded", async () => {
    process.env.PI_CODING_AGENT_DIR = mkdtempSync(join(tmpdir(), "pi-home-"))
    delete process.env.RAMP_ROUTER_BASE_URL

    assert.equal(registeredBaseUrl(), "https://router-api.ramp.com/v1")
  })
})

function registeredBaseUrl() {
  let seen
  registerRouterProvider({ registerProvider: (p) => { seen = p.baseUrl } })
  return seen
}
