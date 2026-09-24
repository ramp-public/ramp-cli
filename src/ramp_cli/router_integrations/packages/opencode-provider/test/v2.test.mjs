import assert from "node:assert/strict"
import { afterEach, describe, it, mock } from "node:test"

import plugin, { toV2Model, toV2Provider } from "../src/index.ts"

function routerMetadata(id, extra = {}) {
  return {
    schema_version: 1,
    request_name: id,
    display_name: extra.display_name ?? id,
    provider_display_name: extra.provider ?? "OpenAI",
    listing: { order: 0 },
    limits: { context_window: 400000, max_output_tokens: 128000 },
    capabilities: {
      modalities: { input: ["text", "image", "hologram"], output: ["text"] },
      tools: { supported: true },
      reasoning: extra.reasoning ?? { efforts: [], default_effort: "" },
      temperature: false,
    },
    pricing: { input: "1.25", output: "10", cache_read_input: "1/8", cache_write_input: "0" },
    status: extra.status ?? "active",
  }
}

const openaiReasoning = {
  efforts: [
    { value: "low", description: "Fast" },
    { value: "high", description: "Deep" },
  ],
  default_effort: "high",
  summary: { request_parameter_supported: true, values: ["auto", "detailed"] },
  continuation: { request_include: ["reasoning.encrypted_content"] },
}

const translatedReasoning = {
  efforts: [{ value: "high", description: "Deep" }],
  default_effort: "high",
  summary: { request_parameter_supported: false, values: [] },
  continuation: { request_include: [] },
}

function modelList(...models) {
  return new Response(JSON.stringify({ object: "list", data: models }), { status: 200 })
}

/** A stand-in for the parts of OpenCode v2's plugin context the plugin uses. */
function fakeContext(options, { existing } = {}) {
  const added = []
  const removed = []
  const editor = {
    get: (id) => existing?.[id],
    remove: (id) => removed.push(id),
    add: (input) => added.push(input),
    list: () => [],
    update: () => {},
    models: { set() {}, update() {}, remove() {} },
  }
  const transforms = []
  return {
    ctx: {
      options,
      provider: {
        transform: async (callback) => {
          transforms.push(callback)
          callback(editor)
          return { dispose: async () => {} }
        },
        reload: async () => {},
      },
    },
    added,
    removed,
    transforms,
  }
}

afterEach(() => {
  mock.restoreAll()
  delete process.env.RAMP_ROUTER_API_KEY
  delete process.env.LLM_GATEWAY_API_KEY
})

describe("dual v1/v2 package shape", () => {
  it("exposes both generations' entrypoints from one default export", () => {
    // v1 (>= 1.18.3) reads `server`; v2 reads `setup`. Neither generation
    // runs the other's implementation, so both must be present and distinct.
    assert.equal(plugin.id, "@ramp/router-opencode-provider")
    assert.equal(typeof plugin.server, "function")
    assert.equal(typeof plugin.setup, "function")
    assert.notEqual(plugin.server, plugin.setup)
  })

  it("keeps both generations' TUI entrypoints in the sidebar module", async () => {
    // The TUI module is JSX, which node's type stripping cannot load, so the
    // dual shape is checked at the source: v1 reads `tui`, v2 reads `setup`.
    const { readFile } = await import("node:fs/promises")
    const source = await readFile(new URL("../src/tui.tsx", import.meta.url), "utf8")
    assert.match(source, /^\s+tui: RouterTuiV1,$/m)
    assert.match(source, /^\s+setup: setupRouterTuiV2,$/m)
    assert.match(source, /satisfies TuiPluginModule & TuiPluginV2\.Definition/)
  })

  it("does not import @opencode/plugin at runtime", async () => {
    // v1 hosts cannot resolve the v2 package, so a runtime import would break
    // every v1 user; the type-only imports must erase completely.
    const { readFile } = await import("node:fs/promises")
    for (const file of ["index.ts", "v1.ts", "v2.ts", "tui.tsx"]) {
      const source = await readFile(new URL(`../src/${file}`, import.meta.url), "utf8")
      for (const line of source.split("\n")) {
        if (line.includes('"@opencode/plugin')) {
          assert.match(line, /^import type /, `${file}: ${line}`)
        }
      }
    }
  })
})

describe("toV2Model", () => {
  it("maps Router metadata onto OpenCode v2's native model shape", () => {
    const model = toV2Model("ramp-router", {
      id: "gpt-5.6-sol",
      ownedBy: "openai",
      metadata: {
        schemaVersion: 1,
        displayName: "GPT-5.6 Sol",
        providerDisplayName: "OpenAI",
        listingOrder: 0,
        contextWindow: 400000,
        maxOutputTokens: 128000,
        inputModalities: ["text", "image", "hologram"],
        outputModalities: ["text"],
        toolCalls: true,
        reasoningEfforts: [
          { value: "low", description: "Fast" },
          { value: "high", description: "Deep" },
        ],
        defaultEffort: "high",
        reasoningSummaryValues: ["auto", "detailed"],
        reasoningSummaryRequestable: true,
        reasoningInclude: ["reasoning.encrypted_content"],
        pricing: { input: 1.25, output: 10, cacheRead: 0.125, cacheWrite: 0 },
        status: "active",
        temperature: false,
        releaseDate: "2026-01-01",
      },
    })

    assert.deepEqual(model, {
      id: "gpt-5.6-sol",
      modelID: "gpt-5.6-sol",
      providerID: "ramp-router",
      name: "GPT-5.6 Sol via OpenAI",
      settings: {
        reasoningEffort: "high",
        reasoningSummary: "auto",
        include: ["reasoning.encrypted_content"],
      },
      capabilities: { tools: true, input: ["text", "image"], output: ["text"] },
      // v1's variants object becomes v2's array with an id per entry, and
      // the per-effort settings are the same ones v1 sends.
      variants: [
        {
          id: "low",
          settings: {
            reasoningEffort: "low",
            reasoningSummary: "auto",
            include: ["reasoning.encrypted_content"],
          },
        },
        {
          id: "high",
          settings: {
            reasoningEffort: "high",
            reasoningSummary: "auto",
            include: ["reasoning.encrypted_content"],
          },
        },
      ],
      time: { released: Date.parse("2026-01-01") },
      // cache_read/cache_write become cache.read/cache.write.
      cost: [{ input: 1.25, output: 10, cache: { read: 0.125, write: 0 } }],
      status: "active",
      enabled: true,
      limit: { context: 400000, output: 128000 },
    })
  })

  it("uses the schema's zero fallback only for missing or invalid release dates", () => {
    const metadata = {
      schemaVersion: 1,
      displayName: "Model",
      providerDisplayName: "OpenAI",
      listingOrder: 0,
      contextWindow: 128000,
      maxOutputTokens: 16384,
      inputModalities: ["text"],
      outputModalities: ["text"],
      reasoningEfforts: [],
      reasoningSummaryValues: [],
      reasoningSummaryRequestable: false,
      reasoningInclude: [],
    }
    const released = (releaseDate) =>
      toV2Model("ramp-router", {
        id: "model",
        metadata: { ...metadata, ...(releaseDate === undefined ? {} : { releaseDate }) },
      }).time.released

    assert.equal(released("2024-02-29"), Date.parse("2024-02-29"))
    for (const date of [undefined, "", "not-a-date", "2026-02-30", "2026-13-01", "2026-01-01T12:00:00Z"]) {
      assert.equal(released(date), 0, `invalid release date ${date}`)
    }
  })

  it("keeps the include list Router states, empty included, and sends no summary where none is read", () => {
    const model = toV2Model("ramp-router", {
      id: "claude-sonnet-4-6",
      metadata: {
        schemaVersion: 1,
        displayName: "Claude Sonnet 4.6",
        providerDisplayName: "Anthropic",
        listingOrder: 1,
        contextWindow: 200000,
        maxOutputTokens: 64000,
        inputModalities: [],
        outputModalities: [],
        reasoningEfforts: [{ value: "high", description: "Deep" }],
        defaultEffort: "high",
        reasoningSummaryValues: [],
        reasoningSummaryRequestable: false,
        reasoningInclude: [],
      },
    })

    // OpenCode v2's Responses routes would otherwise add
    // reasoning.encrypted_content to every request.
    assert.deepEqual(model.settings, { reasoningEffort: "high", include: [] })
    assert.deepEqual(model.variants, [{ id: "high", settings: { reasoningEffort: "high" } }])
    // Unstated modalities still mean text, and unstated tool support is
    // assumed as OpenCode itself assumes it.
    assert.deepEqual(model.capabilities, { tools: true, input: ["text"], output: ["text"] })
    assert.deepEqual(model.cost, [])
  })

  for (const status of ["deprecated", "retired"]) {
    it(`disables a ${status} model instead of dropping it`, () => {
      const model = toV2Model("ramp-router", {
        id: "old",
        metadata: {
          schemaVersion: 1,
          displayName: "Old",
          providerDisplayName: "OpenAI",
          listingOrder: 2,
          contextWindow: 1000,
          maxOutputTokens: 100,
          inputModalities: ["text"],
          outputModalities: ["text"],
          reasoningEfforts: [],
          reasoningSummaryValues: [],
          reasoningSummaryRequestable: false,
          reasoningInclude: [],
          status,
        },
      })
      // v2 has no retired state, so both lifecycle ends read as deprecated
      // and neither is selectable.
      assert.equal(model.status, "deprecated")
      assert.equal(model.enabled, false)
      assert.deepEqual(model.variants, [])
      assert.deepEqual(model.settings, { include: [] })
    })
  }
})

describe("toV2Provider", () => {
  it("registers an enabled native Responses provider with the key inline", () => {
    assert.deepEqual(
      toV2Provider({
        providerID: "ramp-router",
        name: "Ramp Router",
        baseURL: "https://router.example/v1",
        apiKey: "secret",
        rampCliVersion: "0.2.38",
      }),
      {
        id: "ramp-router",
        name: "Ramp Router",
        activation: "enabled",
        package: "@opencode/ai/providers/openai/responses",
        settings: { baseURL: "https://router.example/v1", apiKey: "secret" },
        headers: { "X-Gateway-Ramp-Cli-Version": "0.2.38" },
      },
    )
  })

  it("sends no version header for a hand-written entry", () => {
    const info = toV2Provider({
      providerID: "ramp-router",
      name: "Ramp Router",
      baseURL: "https://router.example/v1",
      apiKey: "secret",
    })
    assert.equal(info.headers, undefined)
  })
})

describe("OpenCode v2 setup", () => {
  it("discovers Router's models once and registers them through the provider editor", async () => {
    const fetcher = mock.method(globalThis, "fetch", async (url, init) => {
      assert.equal(url, "https://router.example/v1/models")
      assert.equal(init.headers.authorization, "Bearer inline-secret")
      assert.equal(init.headers["X-Gateway-Ramp-Cli-Version"], "0.2.38")
      return modelList(
        { id: "gpt-5.6-sol", created: 1767225600, owned_by: "openai", router: routerMetadata("gpt-5.6-sol", { reasoning: openaiReasoning }) },
        { id: "claude-sonnet-4-6", created: 1e15, owned_by: "anthropic", router: routerMetadata("claude-sonnet-4-6", { provider: "Anthropic", reasoning: translatedReasoning }) },
        { id: "typesafe", owned_by: "typesafe", router: routerMetadata("typesafe") },
      )
    })
    const { ctx, added, removed, transforms } = fakeContext({
      apiKey: "inline-secret",
      baseURL: "https://router.example/v1",
      rampCliVersion: "0.2.38",
    })

    await plugin.setup(ctx)

    assert.equal(fetcher.mock.callCount(), 1)
    assert.equal(transforms.length, 1)
    assert.deepEqual(removed, [])
    assert.equal(added.length, 1)
    const [{ info, models }] = added
    assert.equal(info.id, "ramp-router")
    assert.equal(info.name, "Ramp Router")
    assert.equal(info.activation, "enabled")
    assert.equal(info.package, "@opencode/ai/providers/openai/responses")
    assert.deepEqual(info.settings, { baseURL: "https://router.example/v1", apiKey: "inline-secret" })
    assert.deepEqual(info.headers, { "X-Gateway-Ramp-Cli-Version": "0.2.38" })
    assert.deepEqual(
      models.map((model) => model.id),
      ["gpt-5.6-sol", "claude-sonnet-4-6"],
    )
    assert.deepEqual(models[0].variants.map((variant) => variant.id), ["low", "high"])
    assert.deepEqual(models[1].variants.map((variant) => variant.id), ["high"])
    assert.equal(models[0].time.released, Date.parse("2026-01-01"))
    assert.equal(models[1].time.released, 0)

    // The transform is replayable: OpenCode re-runs it on every registry
    // rebuild and must get the same models without another discovery call.
    const replay = fakeContext({})
    transforms[0]({ ...replay.ctx, get: () => undefined, remove: () => {}, add: (input) => replay.added.push(input) })
    assert.equal(fetcher.mock.callCount(), 1)
    assert.deepEqual(replay.added[0].models, models)
  })

  it("replaces an earlier registration of the same provider id", async () => {
    mock.method(globalThis, "fetch", async () =>
      modelList({ id: "m", router: routerMetadata("m") }),
    )
    const { ctx, added, removed } = fakeContext(
      { apiKey: "k", baseURL: "https://router.example/v1", providerID: "router-custom" },
      { existing: { "router-custom": { provider: { id: "router-custom" }, models: new Map() } } },
    )

    await plugin.setup(ctx)

    assert.deepEqual(removed, ["router-custom"])
    assert.equal(added[0].info.id, "router-custom")
  })

  it("resolves the key from the option, then the env var, then the legacy env var", async () => {
    mock.method(globalThis, "fetch", async (_url, init) => {
      assert.equal(init.headers.authorization, "Bearer from-legacy-env")
      return modelList({ id: "m", router: routerMetadata("m") })
    })
    process.env.LLM_GATEWAY_API_KEY = "from-legacy-env"
    const { ctx, added } = fakeContext({ baseURL: "https://router.example/v1" })

    await plugin.setup(ctx)

    assert.equal(added[0].info.settings.apiKey, "from-legacy-env")
  })

  it("fails setup, without the credential, when no key is available", async () => {
    const fetcher = mock.method(globalThis, "fetch", async () => modelList())
    const { ctx, added } = fakeContext({ apiKeyEnv: "MY_ROUTER_KEY" })

    await assert.rejects(
      plugin.setup(ctx),
      (error) =>
        error instanceof Error &&
        error.message === "Set the plugin apiKey option or MY_ROUTER_KEY before starting OpenCode",
    )
    assert.equal(fetcher.mock.callCount(), 0)
    assert.deepEqual(added, [])
  })

  it("uses the production Router endpoint by default", async () => {
    mock.method(globalThis, "fetch", async (url) => {
      assert.equal(url, "https://router-api.ramp.com/v1/models")
      return modelList({ id: "m", router: routerMetadata("m") })
    })
    const { ctx, added } = fakeContext({ apiKey: "k" })

    await plugin.setup(ctx)

    assert.equal(added[0].info.settings.baseURL, "https://router-api.ramp.com/v1")
  })
})
