import assert from "node:assert/strict"
import { afterEach, describe, it, mock } from "node:test"

import plugin, {
  discoverRouterModels,
  normalizeBaseURL,
} from "../src/index.ts"

function reasoningMetadata(id) {
  const metadata = routerMetadata(id)
  metadata.capabilities.reasoning = {
    efforts: [
      { value: "low", description: "Fast" },
      { value: "high", description: "Deep" },
    ],
    default_effort: "low",
  }
  return metadata
}

function routerMetadata(id) {
  // What Router publishes for every model it serves.
  return {
    schema_version: 1,
    request_name: id,
    display_name: id,
    listing: { order: 0 },
    limits: { context_window: 128000, max_output_tokens: 16384 },
    capabilities: {
      modalities: { input: ["text", "image"], output: ["text"] },
      tools: { supported: true },
      reasoning: { efforts: [], default_effort: "" },
    },
  }
}

afterEach(() => {
  mock.restoreAll()
  delete process.env.RAMP_ROUTER_API_KEY
  delete process.env.RAMP_ROUTER_BASE_URL
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
  it("authenticates, validates, and deduplicates the caller's models", async () => {
    const fetcher = mock.fn(async () =>
      new Response(
        JSON.stringify({
          object: "list",
          data: [
            { id: "model-a", object: "model", owned_by: "openai", router: routerMetadata("model-a") },
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
      rampCliVersion: "0.2.38",
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
    assert.equal(fetcher.mock.callCount(), 1)
    assert.equal(
      fetcher.mock.calls[0].arguments[0],
      "http://localhost:8002/v1/models",
    )
    assert.deepEqual(fetcher.mock.calls[0].arguments[1].headers, {
      authorization: "Bearer test-secret",
      "X-Gateway-Ramp-Cli-Version": "0.2.38",
    })
  })

  it("does not include credentials or response bodies in errors", async () => {
    const denied = mock.fn(async () =>
      new Response("private response body", { status: 401 }),
    )
    await assert.rejects(
      discoverRouterModels({
        baseURL: "https://router.example/v1",
        apiKey: "must-not-appear",
        fetch: denied,
      }),
      (error) =>
        error instanceof Error &&
        error.message ===
          "Ramp Router model discovery failed with HTTP 401" &&
        !error.message.includes("must-not-appear") &&
        !error.message.includes("private response body"),
    )
  })

  it("leaves out models that cannot serve Responses traffic", async () => {
    const fetcher = mock.fn(async () =>
      new Response(
        JSON.stringify({
          data: [
            { id: "gpt-5.4", owned_by: "openai", router: routerMetadata("gpt-5.4") },
            {
              id: "jev-by-surface",
              owned_by: "router",
              router: { ...routerMetadata("jev-by-surface"), surfaces: ["systemone"] },
            },
            { id: "jev-by-owner", owned_by: "typesafe", router: routerMetadata("jev-by-owner") },
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
    assert.deepEqual(discovered.map(({ id }) => id), ["gpt-5.4"])
  })
})

describe("OpenCode provider plugin", () => {
  it("registers only dynamically discovered, reasoning-aware models", async () => {
    mock.method(globalThis, "fetch", async () =>
      new Response(
        JSON.stringify({
          object: "list",
          data: [
            { id: "gpt-4o", created: 1767225600, owned_by: "openai", router: routerMetadata("gpt-4o") },
            {
              id: "gpt-5.4",
              owned_by: "openai",
              router: reasoningMetadata("gpt-5.4"),
            },
            { id: "claude-sonnet-4-6", owned_by: "anthropic", router: routerMetadata("claude-sonnet-4-6") },
          ],
        }),
        { status: 200 },
      ),
    )

    const hooks = await plugin.server(
      {},
      {
        apiKey: "inline-secret",
        baseURL: "https://router.example/v1",
      },
    )
    const config = {
      provider: {
        "ramp-router": {
          models: {
            stale: {},
            "gpt-4o": { limit: { output: 2048 } },
          },
        },
      },
    }
    await hooks.config(config)

    const provider = config.provider["ramp-router"]
    assert.equal(provider.npm, "@ai-sdk/openai")
    assert.equal(provider.name, "Ramp Router")
    assert.deepEqual(provider.env, [])
    assert.deepEqual(provider.options, {
      baseURL: "https://router.example/v1",
      apiKey: "inline-secret",
    })
    assert.equal(provider.models["gpt-4o"].limit.output, 2048)
    assert.equal(provider.models["gpt-4o"].reasoning, false)
    assert.equal(Object.hasOwn(provider.models["gpt-4o"], "release_date"), false)
    assert.equal(provider.models["gpt-5.4"].reasoning, true)
    // Variants come from Router's published efforts now, for OpenAI models
    // too. OpenCode's own generated set was only used when Router said
    // nothing, and it disagreed with what the models actually accept.
    assert.deepEqual(Object.keys(provider.models["gpt-5.4"].variants), [
      "low",
      "high",
    ])
    assert.deepEqual(
      Object.keys(provider.models["claude-sonnet-4-6"].variants ?? {}),
      [],
    )
    assert.equal(provider.models.stale, undefined)
  })

  it("uses the production Router endpoint by default", async () => {
    mock.method(globalThis, "fetch", async (url) => {
      assert.equal(url, "https://api.router.com/v1/models")
      return new Response(
        JSON.stringify({ object: "list", data: [{ id: "model-a", router: routerMetadata("model-a") }] }),
        { status: 200 },
      )
    })

    const hooks = await plugin.server({}, { apiKey: "inline-secret" })
    const config = {}
    await hooks.config(config)

    assert.equal(
      config.provider["ramp-router"].options.baseURL,
      "https://api.router.com/v1",
    )
  })

  it("sends the recorded CLI version on every request without dropping the user's headers", async () => {
    // OpenCode spreads provider options into the @ai-sdk/openai factory, so
    // this is the only place a header can be attached to inference requests.
    mock.method(globalThis, "fetch", async (_url, init) => {
      assert.equal(init.headers["X-Gateway-Ramp-Cli-Version"], "0.2.38")
      return new Response(
        JSON.stringify({ object: "list", data: [{ id: "model-a", router: routerMetadata("model-a") }] }),
        { status: 200 },
      )
    })

    const hooks = await plugin.server(
      {},
      { apiKey: "inline-secret", rampCliVersion: "0.2.38" },
    )
    const config = {
      provider: { "ramp-router": { options: { headers: { "X-Mine": "keep" } } } },
    }
    await hooks.config(config)

    assert.deepEqual(config.provider["ramp-router"].options.headers, {
      "X-Mine": "keep",
      "X-Gateway-Ramp-Cli-Version": "0.2.38",
    })
  })
})

describe("reasoning variants", () => {
  async function variantsFor(reasoning) {
    const metadata = routerMetadata("m")
    metadata.capabilities.reasoning = reasoning
    mock.method(globalThis, "fetch", async () =>
      new Response(
        JSON.stringify({
          object: "list",
          data: [{ id: "m", owned_by: "openai", router: metadata }],
        }),
        { status: 200 },
      ),
    )
    const hooks = await plugin.server({}, { apiKey: "k", baseURL: "https://r.example/v1" })
    const config = { provider: {} }
    await hooks.config(config)
    return config.provider["ramp-router"].models.m.variants
  }

  it("asks for a summary only where one can be asked for", async () => {
    const variants = await variantsFor({
      efforts: [{ value: "low", description: "Fast" }],
      summary: { request_parameter_supported: true, values: ["auto", "detailed"] },
      continuation: { request_include: ["reasoning.encrypted_content"] },
    })
    assert.equal(variants.low.reasoningSummary, "auto")
    assert.deepEqual(variants.low.include, ["reasoning.encrypted_content"])
  })

  it("sends neither a summary nor an include where they would be discarded", async () => {
    // xAI accepts both and acts on neither, and the providers Router
    // translates for reject an include they never asked for.
    const variants = await variantsFor({
      efforts: [{ value: "low", description: "Fast" }],
      summary: { request_parameter_supported: false, values: ["auto"] },
      continuation: { request_include: [] },
    })
    assert.equal(variants.low.reasoningSummary, undefined)
    assert.equal(variants.low.include, undefined)
    assert.equal(variants.low.reasoningEffort, "low")
  })
})
