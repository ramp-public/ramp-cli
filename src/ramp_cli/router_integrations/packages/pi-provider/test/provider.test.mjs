import assert from "node:assert/strict"
import { execFileSync, spawn } from "node:child_process"
import fs, {
  chmodSync,
  existsSync,
  mkdtempSync,
  readFileSync,
  statSync,
  symlinkSync,
  unlinkSync,
  writeFileSync,
} from "node:fs"
import { syncBuiltinESMExports } from "node:module"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { afterEach, beforeEach, describe, it, mock } from "node:test"
import { ModelRuntime } from "@earendil-works/pi-coding-agent"

import registerRouterProvider, {
  discoverRouterModels,
  normalizeBaseURL,
  supportsRuntimeModelBootstrap,
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

beforeEach(() => {
  // Keep tests isolated from a developer's real stored Router credential.
  process.env.PI_CODING_AGENT_DIR = mkdtempSync(join(tmpdir(), "pi-home-"))
})

afterEach(() => {
  mock.restoreAll()
  delete process.env.RAMP_ROUTER_API_KEY
  delete process.env.RAMP_ROUTER_BASE_URL
  delete process.env.RAMP_ROUTER_CONTEXT_WINDOW
  delete process.env.RAMP_ROUTER_MAX_OUTPUT_TOKENS
  delete process.env.LLM_GATEWAY_API_KEY
  delete process.env.LLM_GATEWAY_BASE_URL
  delete process.env.PI_OFFLINE
  delete process.env.PI_PROVIDER_TEST_KEY
  delete process.env.PI_CODING_AGENT_DIR
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

describe("supportsRuntimeModelBootstrap", () => {
  it("fails closed before stable Pi 0.84.1", () => {
    assert.equal(supportsRuntimeModelBootstrap("0.81.1"), false)
    assert.equal(supportsRuntimeModelBootstrap("0.84.0"), false)
    assert.equal(supportsRuntimeModelBootstrap("0.84.1-beta.1"), false)
    assert.equal(supportsRuntimeModelBootstrap("0.84.1"), true)
    assert.equal(supportsRuntimeModelBootstrap("0.84.1+build.7"), true)
    assert.equal(supportsRuntimeModelBootstrap("0.85.0-beta.1"), false)
    assert.equal(supportsRuntimeModelBootstrap("1.0.0"), true)
    assert.equal(supportsRuntimeModelBootstrap("9007199254740992.0.0"), false)
    assert.equal(supportsRuntimeModelBootstrap("unknown"), false)
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
      "user-agent": "ramp-cli-pi-provider",
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
  it("discovers models from Pi's stored credential during ordinary startup", async () => {
    const home = mkdtempSync(join(tmpdir(), "pi-home-"))
    writeFileSync(
      join(home, "auth.json"),
      JSON.stringify({
        "ramp-router": { type: "api_key", key: "stored-secret" },
      }),
    )
    process.env.RAMP_ROUTER_API_KEY = "ambient-secret-must-not-win"
    process.env.PI_CODING_AGENT_DIR = home
    const fetcher = mock.method(globalThis, "fetch", async (_url, init) => {
      assert.deepEqual(init.headers, {
        authorization: "Bearer stored-secret",
        "user-agent": "ramp-cli-pi-provider",
      })
      return new Response(
        JSON.stringify({
          object: "list",
          data: [
            {
              id: "gpt-5.4",
              owned_by: "openai",
              router: routerMetadata("gpt-5.4"),
            },
          ],
        }),
        { status: 200 },
      )
    })
    const registerProvider = mock.fn()

    await registerRouterProvider({ registerProvider })

    assert.equal(fetcher.mock.callCount(), 1)
    const [provider] = registerProvider.mock.calls[0].arguments
    assert.deepEqual(provider.getModels().map((model) => model.id), ["gpt-5.4"])
  })

  it("does not replace an incompatible stored credential with an ambient key", async () => {
    const home = process.env.PI_CODING_AGENT_DIR
    writeFileSync(
      join(home, "auth.json"),
      JSON.stringify({
        "ramp-router": {
          type: "oauth",
          access: "stored-oauth-access",
          refresh: "stored-oauth-refresh",
          expires: Date.now() + 60_000,
        },
      }),
    )
    process.env.RAMP_ROUTER_API_KEY = "ambient-secret-must-not-win"
    const fetcher = mock.method(globalThis, "fetch", async () => {
      throw new Error("ambient credential reached discovery")
    })
    const registerProvider = mock.fn()

    await registerRouterProvider({ registerProvider })

    assert.equal(fetcher.mock.callCount(), 0)
    const [provider] = registerProvider.mock.calls[0].arguments
    assert.deepEqual(provider.getModels(), [])
  })

  it("uses Pi's resolved stored credential templates", async () => {
    const home = process.env.PI_CODING_AGENT_DIR
    process.env.PI_PROVIDER_TEST_KEY = "resolved-secret"
    writeFileSync(
      join(home, "auth.json"),
      JSON.stringify({
        "ramp-router": { type: "api_key", key: "$PI_PROVIDER_TEST_KEY" },
      }),
    )
    const fetcher = mock.method(globalThis, "fetch", async (_url, init) => {
      assert.equal(init.headers.authorization, "Bearer resolved-secret")
      return new Response(
        JSON.stringify({
          data: [
            {
              id: "gpt-5.4",
              owned_by: "openai",
              router: routerMetadata("gpt-5.4"),
            },
          ],
        }),
        { status: 200 },
      )
    })
    const registerProvider = mock.fn()

    await registerRouterProvider({ registerProvider })

    assert.equal(fetcher.mock.callCount(), 1)
    const [provider] = registerProvider.mock.calls[0].arguments
    assert.deepEqual(provider.getModels().map((model) => model.id), ["gpt-5.4"])
  })

  it("restores the cached catalog without network access in offline mode", async () => {
    const home = process.env.PI_CODING_AGENT_DIR
    writeFileSync(
      join(home, "auth.json"),
      JSON.stringify({
        "ramp-router": { type: "api_key", key: "stored-secret" },
      }),
    )
    mock.method(globalThis, "fetch", async () =>
      new Response(
        JSON.stringify({
          data: [
            {
              id: "gpt-5.4",
              owned_by: "openai",
              router: routerMetadata("gpt-5.4"),
            },
          ],
        }),
        { status: 200 },
      ),
    )
    await registerRouterProvider({ registerProvider: mock.fn() })

    const cachePath = join(home, "ramp-router-model-cache.json")
    const cacheKeyPath = join(home, "ramp-router-model-cache-key")
    const cacheText = readFileSync(cachePath, "utf8")
    assert.equal(cacheText.includes("stored-secret"), false)
    assert.equal(statSync(cachePath).mode & 0o777, 0o600)
    assert.equal(statSync(cacheKeyPath).mode & 0o777, 0o600)

    process.env.PI_OFFLINE = "1"
    globalThis.fetch.mock.mockImplementation(async () => {
      throw new Error("offline launch attempted a network request")
    })
    const registerProvider = mock.fn()
    await registerRouterProvider({ registerProvider })

    assert.equal(globalThis.fetch.mock.callCount(), 1)
    const [provider] = registerProvider.mock.calls[0].arguments
    assert.deepEqual(provider.getModels().map((model) => model.id), ["gpt-5.4"])
  })

  it("restores private cache files on Windows without POSIX mode checks", async () => {
    const home = process.env.PI_CODING_AGENT_DIR
    writeFileSync(
      join(home, "auth.json"),
      JSON.stringify({
        "ramp-router": { type: "api_key", key: "stored-secret" },
      }),
    )
    mock.method(globalThis, "fetch", async () =>
      new Response(
        JSON.stringify({
          data: [
            {
              id: "gpt-5.4",
              owned_by: "openai",
              router: routerMetadata("gpt-5.4"),
            },
          ],
        }),
        { status: 200 },
      ),
    )
    await registerRouterProvider({ registerProvider: mock.fn() })
    chmodSync(join(home, "ramp-router-model-cache.json"), 0o666)
    chmodSync(join(home, "ramp-router-model-cache-key"), 0o666)
    process.env.PI_OFFLINE = "1"

    const platform = Object.getOwnPropertyDescriptor(process, "platform")
    Object.defineProperty(process, "platform", { value: "win32" })
    try {
      const registerProvider = mock.fn()
      await registerRouterProvider({ registerProvider })
      assert.deepEqual(
        registerProvider.mock.calls[0].arguments[0].getModels().map((model) => model.id),
        ["gpt-5.4"],
      )
    } finally {
      Object.defineProperty(process, "platform", platform)
    }
  })

  it("rejects request-affecting or malformed fields in the private cache", async () => {
    const home = process.env.PI_CODING_AGENT_DIR
    writeFileSync(
      join(home, "auth.json"),
      JSON.stringify({
        "ramp-router": { type: "api_key", key: "stored-secret" },
      }),
    )
    mock.method(globalThis, "fetch", async () =>
      new Response(
        JSON.stringify({
          data: [
            {
              id: "gpt-5.4",
              owned_by: "openai",
              router: routerMetadata("gpt-5.4"),
            },
          ],
        }),
        { status: 200 },
      ),
    )
    await registerRouterProvider({ registerProvider: mock.fn() })

    const cachePath = join(home, "ramp-router-model-cache.json")
    const cache = JSON.parse(readFileSync(cachePath, "utf8"))
    cache.models[0].headers = { authorization: "Bearer injected" }
    cache.models[0].cost.input = "not-a-number"
    writeFileSync(cachePath, JSON.stringify(cache))
    process.env.PI_OFFLINE = "1"

    const registerProvider = mock.fn()
    await registerRouterProvider({ registerProvider })

    const [provider] = registerProvider.mock.calls[0].arguments
    assert.deepEqual(provider.getModels(), [])
  })

  it("does not follow or load an oversized private cache", async () => {
    const home = process.env.PI_CODING_AGENT_DIR
    writeFileSync(
      join(home, "auth.json"),
      JSON.stringify({
        "ramp-router": { type: "api_key", key: "stored-secret" },
      }),
    )
    mock.method(globalThis, "fetch", async () =>
      new Response(
        JSON.stringify({
          data: [
            {
              id: "gpt-5.4",
              owned_by: "openai",
              router: routerMetadata("gpt-5.4"),
            },
          ],
        }),
        { status: 200 },
      ),
    )
    await registerRouterProvider({ registerProvider: mock.fn() })

    const cachePath = join(home, "ramp-router-model-cache.json")
    const externalPath = join(home, "external-cache.json")
    writeFileSync(externalPath, readFileSync(cachePath), { mode: 0o600 })
    unlinkSync(cachePath)
    symlinkSync(externalPath, cachePath)
    process.env.PI_OFFLINE = "1"

    let registerProvider = mock.fn()
    await registerRouterProvider({ registerProvider })
    assert.deepEqual(
      registerProvider.mock.calls[0].arguments[0].getModels(),
      [],
    )

    unlinkSync(cachePath)
    writeFileSync(cachePath, Buffer.alloc(4 * 1024 * 1024 + 1), { mode: 0o600 })
    chmodSync(cachePath, 0o600)
    registerProvider = mock.fn()
    await registerRouterProvider({ registerProvider })
    assert.deepEqual(
      registerProvider.mock.calls[0].arguments[0].getModels(),
      [],
    )
    assert.equal(globalThis.fetch.mock.callCount(), 1)
  })

  it("rejects duplicate models and remapped reasoning efforts in the private cache", async () => {
    const home = process.env.PI_CODING_AGENT_DIR
    writeFileSync(
      join(home, "auth.json"),
      JSON.stringify({
        "ramp-router": { type: "api_key", key: "stored-secret" },
      }),
    )
    const reasoningMetadata = routerMetadata("gpt-5.4")
    reasoningMetadata.capabilities.reasoning.efforts = [
      { value: "none", description: "" },
      { value: "low", description: "" },
      { value: "max", description: "" },
    ]
    mock.method(globalThis, "fetch", async () =>
      new Response(
        JSON.stringify({
          data: [
            {
              id: "gpt-5.4",
              owned_by: "openai",
              router: reasoningMetadata,
            },
          ],
        }),
        { status: 200 },
      ),
    )
    await registerRouterProvider({ registerProvider: mock.fn() })

    const cachePath = join(home, "ramp-router-model-cache.json")
    const cache = JSON.parse(readFileSync(cachePath, "utf8"))
    cache.models[0].thinkingLevelMap.low = "max"
    cache.models.push(structuredClone(cache.models[0]))
    writeFileSync(cachePath, JSON.stringify(cache))
    process.env.PI_OFFLINE = "1"

    const registerProvider = mock.fn()
    await registerRouterProvider({ registerProvider })

    const [provider] = registerProvider.mock.calls[0].arguments
    assert.deepEqual(provider.getModels(), [])
  })

  it("keeps concurrent cache writers parseable and private", async () => {
    const home = process.env.PI_CODING_AGENT_DIR
    writeFileSync(
      join(home, "auth.json"),
      JSON.stringify({
        "ramp-router": { type: "api_key", key: "stored-secret" },
      }),
    )
    mock.method(globalThis, "fetch", async () =>
      new Response(
        JSON.stringify({
          data: [
            {
              id: "gpt-5.4",
              owned_by: "openai",
              router: routerMetadata("gpt-5.4"),
            },
          ],
        }),
        { status: 200 },
      ),
    )

    await Promise.all(
      Array.from({ length: 8 }, () =>
        registerRouterProvider({ registerProvider: mock.fn() }),
      ),
    )

    const cachePath = join(home, "ramp-router-model-cache.json")
    const cache = JSON.parse(readFileSync(cachePath, "utf8"))
    assert.equal(cache.models[0].id, "gpt-5.4")
    assert.equal(statSync(cachePath).mode & 0o777, 0o600)
    assert.equal(
      JSON.parse(readFileSync(join(home, "ramp-router-runtime-models.json"), "utf8"))
        .providers["ramp-router"].models.length,
      0,
    )
  })

  it("keeps live discovery usable when hard links are unavailable", async () => {
    const home = process.env.PI_CODING_AGENT_DIR
    writeFileSync(
      join(home, "auth.json"),
      JSON.stringify({
        "ramp-router": { type: "api_key", key: "stored-secret" },
      }),
    )
    mock.method(globalThis, "fetch", async () =>
      new Response(
        JSON.stringify({
          data: [
            {
              id: "gpt-5.4",
              owned_by: "openai",
              router: routerMetadata("gpt-5.4"),
            },
          ],
        }),
        { status: 200 },
      ),
    )
    const realLinkSync = fs.linkSync
    fs.linkSync = () => {
      const error = new Error("hard links unavailable")
      error.code = "EINVAL"
      throw error
    }
    syncBuiltinESMExports()
    const registerProvider = mock.fn()
    try {
      await registerRouterProvider({ registerProvider })
    } finally {
      fs.linkSync = realLinkSync
      syncBuiltinESMExports()
    }

    const [provider] = registerProvider.mock.calls[0].arguments
    assert.deepEqual(provider.getModels().map((model) => model.id), ["gpt-5.4"])
    assert.equal(existsSync(join(home, "ramp-router-model-cache-key")), false)
    assert.equal(existsSync(join(home, "ramp-router-model-cache.json")), false)
  })

  it("adopts one complete cache identity across concurrent processes", async () => {
    const home = process.env.PI_CODING_AGENT_DIR
    const winnerReady = join(home, "winner-ready")
    const loserReady = join(home, "loser-ready")
    const winnerPublished = join(home, "winner-published")
    const secret = "stored-secret"
    writeFileSync(
      join(home, "auth.json"),
      JSON.stringify({
        "ramp-router": { type: "api_key", key: secret },
      }),
    )

    const childScript = String.raw`
      const fs = require("node:fs");
      const { syncBuiltinESMExports } = require("node:module");
      const waitArray = new Int32Array(new SharedArrayBuffer(4));
      const waitFor = (path) => {
        const deadline = Date.now() + 15000;
        while (!fs.existsSync(path)) {
          if (Date.now() >= deadline) process.exit(3);
          Atomics.wait(waitArray, 0, 0, 5);
        }
      };
      const role = process.env.TEST_ROLE;
      const realLinkSync = fs.linkSync.bind(fs);
      fs.linkSync = (source, destination) => {
        if (role === "winner") {
          fs.writeFileSync(process.env.TEST_WINNER_READY, "");
          waitFor(process.env.TEST_LOSER_READY);
          realLinkSync(source, destination);
          fs.writeFileSync(process.env.TEST_WINNER_PUBLISHED, "");
          return;
        }
        fs.writeFileSync(process.env.TEST_LOSER_READY, "");
        waitFor(process.env.TEST_WINNER_PUBLISHED);
        realLinkSync(source, destination);
      };
      syncBuiltinESMExports();
      globalThis.fetch = async () => new Response(JSON.stringify({
        data: [{
          id: "concurrent-model",
          owned_by: "openai",
          router: {
            schema_version: 1,
            request_name: "concurrent-model",
            display_name: "concurrent-model",
            listing: { order: 0 },
            limits: { context_window: 128000, max_output_tokens: 16384 },
            capabilities: {
              modalities: { input: ["text"], output: ["text"] },
              tools: { supported: true },
              reasoning: { efforts: [], default_effort: "" },
            },
          },
        }],
      }), { status: 200 });
      import(process.env.TEST_INDEX_URL).then(async ({ default: register }) => {
        let provider;
        await register({ registerProvider(value) { provider = value; } });
        const cache = JSON.parse(fs.readFileSync(process.env.TEST_CACHE_PATH, "utf8"));
        fs.writeFileSync(process.env.TEST_RESULT_PATH, JSON.stringify({
          models: provider.getModels().map(({ id }) => id),
          credentialIdentity: cache.credentialIdentity,
        }));
      }).catch(() => process.exit(4));
    `
    const indexUrl = new URL("../src/index.ts", import.meta.url).href
    const cachePath = join(home, "ramp-router-model-cache.json")
    const startChild = (role) => {
      const resultPath = join(home, `${role}-result.json`)
      const child = spawn(process.execPath, ["-e", childScript], {
        env: {
          PI_CODING_AGENT_DIR: home,
          RAMP_ROUTER_BASE_URL: "https://router.invalid/v1",
          TEST_ROLE: role,
          TEST_INDEX_URL: indexUrl,
          TEST_CACHE_PATH: cachePath,
          TEST_RESULT_PATH: resultPath,
          TEST_WINNER_READY: winnerReady,
          TEST_LOSER_READY: loserReady,
          TEST_WINNER_PUBLISHED: winnerPublished,
        },
        stdio: "ignore",
      })
      const done = new Promise((resolve, reject) => {
        const timer = setTimeout(() => {
          child.kill("SIGKILL")
          reject(new Error(`${role} cache publisher timed out`))
        }, 20000)
        child.once("error", (error) => {
          clearTimeout(timer)
          reject(error)
        })
        child.once("exit", (code) => {
          clearTimeout(timer)
          if (code === 0) resolve()
          else reject(new Error(`${role} cache publisher exited ${code}`))
        })
      })
      return { done, resultPath }
    }

    const winner = startChild("winner")
    const loser = startChild("loser")
    await Promise.all([winner.done, loser.done])

    const winnerResult = JSON.parse(readFileSync(winner.resultPath, "utf8"))
    const loserResult = JSON.parse(readFileSync(loser.resultPath, "utf8"))
    assert.deepEqual(winnerResult.models, ["concurrent-model"])
    assert.deepEqual(loserResult.models, ["concurrent-model"])
    assert.equal(
      winnerResult.credentialIdentity,
      loserResult.credentialIdentity,
    )
    assert.match(
      readFileSync(join(home, "ramp-router-model-cache-key"), "utf8"),
      /^[0-9a-f]{64}\n$/,
    )
    assert.equal(
      statSync(join(home, "ramp-router-model-cache-key")).mode & 0o777,
      0o600,
    )
    assert.deepEqual(
      JSON.parse(readFileSync(cachePath, "utf8")).models.map(({ id }) => id),
      ["concurrent-model"],
    )
    assert.equal(
      fs.readdirSync(home).some(
        (name) => name.endsWith(".lock") || name.endsWith(".tmp"),
      ),
      false,
    )
  })

  it("does not repair a malformed cache identity winner", async () => {
    const home = process.env.PI_CODING_AGENT_DIR
    const keyPath = join(home, "ramp-router-model-cache-key")
    const malformed = "not-a-private-cache-key\n"
    writeFileSync(
      join(home, "auth.json"),
      JSON.stringify({
        "ramp-router": { type: "api_key", key: "stored-secret" },
      }),
    )
    writeFileSync(keyPath, malformed, { mode: 0o600 })
    mock.method(globalThis, "fetch", async () =>
      new Response(
        JSON.stringify({
          data: [
            {
              id: "live-model",
              owned_by: "openai",
              router: routerMetadata("live-model"),
            },
          ],
        }),
        { status: 200 },
      ),
    )
    const registerProvider = mock.fn()
    await registerRouterProvider({ registerProvider })

    const [provider] = registerProvider.mock.calls[0].arguments
    assert.deepEqual(provider.getModels().map(({ id }) => id), ["live-model"])
    assert.equal(readFileSync(keyPath, "utf8"), malformed)
    assert.equal(existsSync(join(home, "ramp-router-model-cache.json")), false)
    assert.equal(
      fs.readdirSync(home).some(
        (name) => name.endsWith(".lock") || name.endsWith(".tmp"),
      ),
      false,
    )
  })

  it("does not restore a catalog cached for another Router endpoint", async () => {
    const home = process.env.PI_CODING_AGENT_DIR
    writeFileSync(
      join(home, "auth.json"),
      JSON.stringify({
        "ramp-router": { type: "api_key", key: "stored-secret" },
      }),
    )
    process.env.RAMP_ROUTER_BASE_URL = "https://first.example/v1"
    mock.method(globalThis, "fetch", async (url) =>
      new Response(
        JSON.stringify({
          data: [
            {
              id: String(url).includes("first.example") ? "first-model" : "second-model",
              owned_by: "openai",
              router: routerMetadata(
                String(url).includes("first.example") ? "first-model" : "second-model",
              ),
            },
          ],
        }),
        { status: 200 },
      ),
    )
    await registerRouterProvider({ registerProvider: mock.fn() })

    process.env.RAMP_ROUTER_BASE_URL = "https://second.example/v1"
    const registerProvider = mock.fn()
    await registerRouterProvider({ registerProvider })

    assert.equal(globalThis.fetch.mock.callCount(), 2)
    assert.equal(
      globalThis.fetch.mock.calls[1].arguments[0],
      "https://second.example/v1/models",
    )
    const [provider] = registerProvider.mock.calls[0].arguments
    assert.deepEqual(
      provider.getModels().map(({ id, baseUrl }) => ({ id, baseUrl })),
      [{ id: "second-model", baseUrl: "https://second.example/v1" }],
    )
  })

  it("does not restore a catalog cached for another Router credential", async () => {
    const home = process.env.PI_CODING_AGENT_DIR
    const authPath = join(home, "auth.json")
    writeFileSync(
      authPath,
      JSON.stringify({
        "ramp-router": { type: "api_key", key: "first-secret" },
      }),
    )
    mock.method(globalThis, "fetch", async (_url, init) => {
      const credential = init.headers.authorization.replace("Bearer ", "")
      const id = credential === "first-secret" ? "first-model" : "second-model"
      return new Response(
        JSON.stringify({
          data: [
            {
              id,
              owned_by: "openai",
              router: routerMetadata(id),
            },
          ],
        }),
        { status: 200 },
      )
    })
    await registerRouterProvider({ registerProvider: mock.fn() })

    writeFileSync(
      authPath,
      JSON.stringify({
        "ramp-router": { type: "api_key", key: "second-secret" },
      }),
    )
    const registerProvider = mock.fn()
    await registerRouterProvider({ registerProvider })

    assert.equal(globalThis.fetch.mock.callCount(), 2)
    assert.equal(
      globalThis.fetch.mock.calls[1].arguments[1].headers.authorization,
      "Bearer second-secret",
    )
    const [provider] = registerProvider.mock.calls[0].arguments
    assert.deepEqual(provider.getModels().map((model) => model.id), [
      "second-model",
    ])
  })

  it("guards direct and retained-model requests with the active catalog credential", async () => {
    const home = process.env.PI_CODING_AGENT_DIR
    writeFileSync(
      join(home, "auth.json"),
      JSON.stringify({
        "ramp-router": { type: "api_key", key: "current-secret" },
      }),
    )
    mock.method(globalThis, "fetch", async () =>
      new Response(
        JSON.stringify({
          data: [
            {
              id: "current-model",
              owned_by: "openai",
              router: routerMetadata("current-model"),
            },
          ],
        }),
        { status: 200 },
      ),
    )
    const registerProvider = mock.fn()
    await registerRouterProvider({ registerProvider })
    const [provider] = registerProvider.mock.calls[0].arguments
    const [model] = provider.getModels()
    const context = { messages: [] }

    const fixedFailure = async (candidate, options, pattern) => {
      const result = await provider.streamSimple(candidate, context, options).result()
      assert.equal(result.stopReason, "error")
      assert.match(result.errorMessage, pattern)
      assert.doesNotMatch(result.errorMessage, /current-secret|other-secret/)
    }

    await fixedFailure(
      model,
      { apiKey: "other-secret" },
      /catalog does not match the active credential/,
    )
    await fixedFailure(
      { ...model, id: "retained-model" },
      { apiKey: "current-secret" },
      /model is not in the active catalog/,
    )
    model.id = "mutated-retained-model"
    await fixedFailure(
      model,
      { apiKey: "current-secret" },
      /model is not in the active catalog/,
    )
    assert.deepEqual(provider.getModels().map(({ id }) => id), [
      "current-model",
    ])
    for (const candidate of [
      { ...model, provider: "openai" },
      { ...model, api: "openai-completions" },
      { ...model, baseUrl: "https://other.example/v1" },
    ]) {
      await fixedFailure(
        candidate,
        { apiKey: "current-secret" },
        /model does not match this provider/,
      )
    }
    for (const headers of [
      { Authorization: "Bearer current-secret" },
      { authorization: null },
      { AUTHORIZATION: "" },
      { "CF-AIG-Authorization": "Bearer current-secret" },
      { "cf-aig-authorization": null },
    ]) {
      await fixedFailure(
        model,
        { apiKey: "current-secret", headers },
        /request authorization is ambiguous/,
      )
    }
    assert.equal(provider.fetchDeferred, undefined)
    assert.equal(provider.cancelDeferred, undefined)
  })

  it("guards the final Responses payload after sampling and extension transforms", async () => {
    const home = process.env.PI_CODING_AGENT_DIR
    writeFileSync(
      join(home, "auth.json"),
      JSON.stringify({
        "ramp-router": { type: "api_key", key: "current-secret" },
      }),
    )
    mock.method(globalThis, "fetch", async () =>
      new Response(
        JSON.stringify({
          data: [
            {
              id: "current-model",
              owned_by: "openai",
              router: routerMetadata("current-model"),
            },
          ],
        }),
        { status: 200 },
      ),
    )
    const registerProvider = mock.fn()
    await registerRouterProvider({ registerProvider })
    const [provider] = registerProvider.mock.calls[0].arguments
    const [model] = provider.getModels()
    const context = { messages: [] }

    const blockedFetch = mock.fn(async () => {
      throw new Error("the guarded request must not reach fetch")
    })
    const blocked = async (options, pattern) => {
      const result = await provider.streamSimple(model, context, {
        apiKey: "current-secret",
        fetch: blockedFetch,
        maxRetries: 0,
        ...options,
      }).result()
      assert.equal(result.stopReason, "error")
      assert.match(result.errorMessage, pattern)
      assert.doesNotMatch(result.errorMessage, /current-secret|uncatalogued/)
    }

    await blocked(
      { samplingParams: { model: "uncatalogued-model" } },
      /request model is not in the active catalog/,
    )
    await blocked(
      { samplingParams: { store: true } },
      /payload violates provider invariants/,
    )
    await blocked(
      {
        onPayload: (payload) => {
          payload.model = "uncatalogued-model"
        },
      },
      /request model is not in the active catalog/,
    )
    await blocked(
      {
        onPayload: (payload) => ({
          ...payload,
          model: "uncatalogued-model",
        }),
      },
      /request model is not in the active catalog/,
    )
    await blocked(
      {
        onPayload: (payload, requestModel) => {
          requestModel.id = "uncatalogued-model"
          payload.model = "uncatalogued-model"
        },
      },
      /request model is not in the active catalog/,
    )
    await blocked(
      { onPayload: () => null },
      /request model is not in the active catalog/,
    )
    assert.equal(blockedFetch.mock.callCount(), 0)

    let outbound
    const captureFetch = mock.fn(async (_url, init) => {
      outbound = JSON.parse(init.body)
      return new Response(
        JSON.stringify({ error: { message: "fixture stop" } }),
        { status: 400, headers: { "content-type": "application/json" } },
      )
    })
    const tunedModel = {
      ...model,
      name: "Locally tuned model",
      reasoning: true,
      thinkingLevelMap: { off: "none", high: "high" },
      input: ["text", "image"],
      cost: {
        input: 1,
        output: 2,
        cacheRead: 3,
        cacheWrite: 4,
      },
      contextWindow: 64_000,
      maxTokens: 4_096,
      samplingParams: { temperature: 0.25 },
      compat: {
        supportsDeveloperRole: false,
        supportsStrictMode: true,
      },
    }
    let observedRequestModel
    await provider.streamSimple(tunedModel, context, {
      apiKey: "current-secret",
      fetch: captureFetch,
      maxRetries: 0,
      onPayload: (payload, requestModel) => {
        observedRequestModel = structuredClone(requestModel)
        return { ...payload, metadata: { fixture: true } }
      },
    }).result()

    assert.equal(captureFetch.mock.callCount(), 1)
    assert.equal(outbound.model, "current-model")
    assert.equal(outbound.store, false)
    assert.equal(outbound.stream, true)
    assert.equal(outbound.temperature, 0.25)
    assert.deepEqual(outbound.metadata, { fixture: true })
    assert.deepEqual(observedRequestModel, tunedModel)
    assert.deepEqual(provider.getModels().map(({ id }) => id), [
      "current-model",
    ])
  })

  it("snapshots request headers before asynchronous Responses dispatch", async () => {
    const home = process.env.PI_CODING_AGENT_DIR
    writeFileSync(
      join(home, "auth.json"),
      JSON.stringify({
        "ramp-router": { type: "api_key", key: "current-secret" },
      }),
    )
    mock.method(globalThis, "fetch", async () =>
      new Response(
        JSON.stringify({
          data: [
            {
              id: "current-model",
              owned_by: "openai",
              router: routerMetadata("current-model"),
            },
          ],
        }),
        { status: 200 },
      ),
    )
    const registerProvider = mock.fn()
    await registerRouterProvider({ registerProvider })
    const [provider] = registerProvider.mock.calls[0].arguments
    const [model] = provider.getModels()
    const context = { messages: [] }

    const outboundAuthorizations = []
    const captureFetch = mock.fn(async (_url, init) => {
      const headers = new Headers(init.headers)
      outboundAuthorizations.push(headers.get("authorization"))
      return new Response(
        JSON.stringify({ error: { message: "fixture stop" } }),
        { status: 400, headers: { "content-type": "application/json" } },
      )
    })
    const retainedHeaders = {}
    await provider.streamSimple(model, context, {
      apiKey: "current-secret",
      headers: retainedHeaders,
      fetch: captureFetch,
      maxRetries: 0,
      onPayload: (payload) => {
        retainedHeaders.Authorization = "Bearer late-secret"
        return payload
      },
    }).result()

    let ownKeyReads = 0
    const statefulHeaders = new Proxy({}, {
      ownKeys: () => {
        ownKeyReads += 1
        return ownKeyReads === 1 ? [] : ["Authorization"]
      },
      getOwnPropertyDescriptor: () => ({
        configurable: true,
        enumerable: true,
      }),
      get: () => "Bearer proxy-secret",
    })
    await provider.streamSimple(model, context, {
      apiKey: "current-secret",
      headers: statefulHeaders,
      fetch: captureFetch,
      maxRetries: 0,
    }).result()

    assert.equal(captureFetch.mock.callCount(), 2)
    assert.deepEqual(outboundAuthorizations, [
      "Bearer current-secret",
      "Bearer current-secret",
    ])
    assert.equal(ownKeyReads, 1)
  })

  it("filters catalogs with Pi's exact effective credential", async () => {
    process.env.RAMP_ROUTER_API_KEY = "environment-secret"
    mock.method(globalThis, "fetch", async () =>
      new Response(
        JSON.stringify({
          data: [
            {
              id: "environment-model",
              owned_by: "openai",
              router: routerMetadata("environment-model"),
            },
          ],
        }),
        { status: 200 },
      ),
    )
    const registerProvider = mock.fn()
    await registerRouterProvider({ registerProvider })
    const [provider] = registerProvider.mock.calls[0].arguments

    assert.deepEqual(
      provider.filterModels(provider.getModels(), undefined).map(({ id }) => id),
      ["environment-model"],
    )
    assert.deepEqual(
      provider.filterModels(provider.getModels(), {
        type: "api_key",
        key: "other-secret",
      }),
      [],
    )
  })

  it("accepts a resolved effective credential that begins with an exclamation", async () => {
    const home = process.env.PI_CODING_AGENT_DIR
    writeFileSync(
      join(home, "auth.json"),
      JSON.stringify({
        "ramp-router": { type: "api_key", key: "$!opaque-secret" },
      }),
    )
    mock.method(globalThis, "fetch", async () =>
      new Response(
        JSON.stringify({
          data: [
            {
              id: "escaped-model",
              owned_by: "openai",
              router: routerMetadata("escaped-model"),
            },
          ],
        }),
        { status: 200 },
      ),
    )
    const registerProvider = mock.fn()
    await registerRouterProvider({ registerProvider })
    const [provider] = registerProvider.mock.calls[0].arguments

    assert.deepEqual(
      provider.filterModels(provider.getModels(), {
        type: "api_key",
        key: "!opaque-secret",
      }).map(({ id }) => id),
      ["escaped-model"],
    )
    let updated = false
    await provider.refreshModels({
      credential: { type: "api_key", key: "!opaque-secret" },
      stored: undefined,
      allowNetwork: false,
      signal: new AbortController().signal,
      publish: async ({ update }) => {
        update?.()
        updated = true
        return true
      },
    })
    assert.equal(updated, true)
    assert.deepEqual(provider.getModels().map(({ id }) => id), [
      "escaped-model",
    ])
  })

  it("publishes a background catalog scoped by credential-local template env", async () => {
    const home = process.env.PI_CODING_AGENT_DIR
    const cachePath = join(home, "ramp-router-model-cache.json")
    writeFileSync(
      join(home, "auth.json"),
      JSON.stringify({
        "ramp-router": {
          type: "api_key",
          key: "$LOCAL_ROUTER_KEY",
          env: { LOCAL_ROUTER_KEY: "credential-secret" },
        },
      }),
    )
    let requestCount = 0
    mock.method(globalThis, "fetch", async () => {
      requestCount += 1
      const id = requestCount === 1 ? "cached-model" : "refreshed-model"
      return new Response(
        JSON.stringify({
          data: [
            { id, owned_by: "openai", router: routerMetadata(id) },
          ],
        }),
        { status: 200 },
      )
    })
    await registerRouterProvider({ registerProvider: mock.fn() })

    const realRenameSync = fs.renameSync
    let markBackgroundCached
    const backgroundCached = new Promise((resolve) => {
      markBackgroundCached = resolve
    })
    fs.renameSync = (source, destination) => {
      const result = realRenameSync(source, destination)
      if (destination === cachePath) markBackgroundCached()
      return result
    }
    syncBuiltinESMExports()
    try {
      const registerProvider = mock.fn()
      await registerRouterProvider({ registerProvider })
      const [provider] = registerProvider.mock.calls[0].arguments
      await backgroundCached

      assert.deepEqual(provider.getModels().map(({ id }) => id), [
        "refreshed-model",
      ])
      assert.deepEqual(
        JSON.parse(readFileSync(cachePath, "utf8")).models.map(({ id }) => id),
        ["refreshed-model"],
      )
      assert.deepEqual(
        provider.filterModels(provider.getModels(), {
          type: "api_key",
          key: "credential-secret",
        }).map(({ id }) => id),
        ["refreshed-model"],
      )
    } finally {
      fs.renameSync = realRenameSync
      syncBuiltinESMExports()
    }
  })

  it("does not publish a background catalog after the credential changes", async () => {
    const home = process.env.PI_CODING_AGENT_DIR
    const authPath = join(home, "auth.json")
    let authResolutionCount = 0
    let finishAuthRecheck
    const authRechecked = new Promise((resolve) => {
      finishAuthRecheck = resolve
    })
    const realCreate = ModelRuntime.create.bind(ModelRuntime)
    mock.method(ModelRuntime, "create", async (...args) => {
      const runtime = await realCreate(...args)
      const realGetAuth = runtime.getAuth.bind(runtime)
      runtime.getAuth = async (...getAuthArgs) => {
        const result = await realGetAuth(...getAuthArgs)
        authResolutionCount += 1
        if (authResolutionCount === 3) finishAuthRecheck()
        return result
      }
      return runtime
    })
    process.env.PI_PROVIDER_TEST_KEY = "first-secret"
    writeFileSync(
      authPath,
      JSON.stringify({
        "ramp-router": { type: "api_key", key: "$PI_PROVIDER_TEST_KEY" },
      }),
    )
    let finishRefresh
    let requestCount = 0
    mock.method(globalThis, "fetch", async () => {
      requestCount += 1
      if (requestCount > 1) {
        await new Promise((resolve) => {
          finishRefresh = resolve
        })
      }
      return new Response(
        JSON.stringify({
          data: [
            {
              id: requestCount === 1 ? "cached-model" : "refreshed-model",
              owned_by: "openai",
              router: routerMetadata(
                requestCount === 1 ? "cached-model" : "refreshed-model",
              ),
            },
          ],
        }),
        { status: 200 },
      )
    })
    await registerRouterProvider({ registerProvider: mock.fn() })

    const registerProvider = mock.fn()
    await registerRouterProvider({ registerProvider })
    assert.equal(typeof finishRefresh, "function")
    const cachePath = join(home, "ramp-router-model-cache.json")
    const cacheBeforeRefresh = readFileSync(cachePath, "utf8")
    process.env.PI_PROVIDER_TEST_KEY = "second-secret"
    finishRefresh()
    await authRechecked

    assert.equal(registerProvider.mock.callCount(), 1)
    assert.equal(readFileSync(cachePath, "utf8"), cacheBeforeRefresh)
    const [provider] = registerProvider.mock.calls[0].arguments
    assert.deepEqual(
      provider.filterModels(provider.getModels(), {
        type: "api_key",
        key: "second-secret",
      }),
      [],
    )
  })

  it("publishes a refreshed live catalog when background cache persistence fails", async () => {
    const home = process.env.PI_CODING_AGENT_DIR
    const cachePath = join(home, "ramp-router-model-cache.json")
    writeFileSync(
      join(home, "auth.json"),
      JSON.stringify({
        "ramp-router": { type: "api_key", key: "stored-secret" },
      }),
    )
    let requestCount = 0
    mock.method(globalThis, "fetch", async () => {
      requestCount += 1
      const id = requestCount === 1 ? "cached-model" : "refreshed-model"
      return new Response(
        JSON.stringify({
          data: [
            { id, owned_by: "openai", router: routerMetadata(id) },
          ],
        }),
        { status: 200 },
      )
    })
    await registerRouterProvider({ registerProvider: mock.fn() })
    const cacheBeforeRefresh = readFileSync(cachePath, "utf8")

    const realRenameSync = fs.renameSync
    let markCacheWriteAttempted
    const cacheWriteAttempted = new Promise((resolve) => {
      markCacheWriteAttempted = resolve
    })
    fs.renameSync = (source, destination) => {
      if (destination === cachePath) {
        markCacheWriteAttempted()
        throw Object.assign(new Error("simulated cache write failure"), {
          code: "EACCES",
        })
      }
      return realRenameSync(source, destination)
    }
    syncBuiltinESMExports()

    try {
      const registerProvider = mock.fn()
      let sessionStart
      await registerRouterProvider({
        registerProvider,
        on: (event, handler) => {
          if (event === "session_start") sessionStart = handler
        },
      })
      const [provider] = registerProvider.mock.calls[0].arguments
      let markSnapshotRefreshed
      const snapshotRefreshed = new Promise((resolve) => {
        markSnapshotRefreshed = resolve
      })
      const refresh = mock.fn(async (options) => {
        assert.deepEqual(options, {
          providers: ["ramp-router"],
          allowNetwork: false,
        })
        markSnapshotRefreshed()
      })
      sessionStart(
        { type: "session_start", reason: "startup" },
        { modelRegistry: { refresh } },
      )
      await cacheWriteAttempted
      await snapshotRefreshed

      assert.deepEqual(provider.getModels().map((model) => model.id), [
        "refreshed-model",
      ])
      assert.equal(readFileSync(cachePath, "utf8"), cacheBeforeRefresh)
      assert.equal(registerProvider.mock.callCount(), 1)
      assert.equal(refresh.mock.callCount(), 1)
    } finally {
      fs.renameSync = realRenameSync
      syncBuiltinESMExports()
    }
  })

  it("does not let startup refresh overwrite a newer host refresh", async () => {
    const home = process.env.PI_CODING_AGENT_DIR
    let authResolutionCount = 0
    let finishBackgroundAuth
    let markBackgroundAuthStarted
    const backgroundAuthStarted = new Promise((resolve) => {
      markBackgroundAuthStarted = resolve
    })
    const realCreate = ModelRuntime.create.bind(ModelRuntime)
    mock.method(ModelRuntime, "create", async (...args) => {
      const runtime = await realCreate(...args)
      const realGetAuth = runtime.getAuth.bind(runtime)
      runtime.getAuth = async (...getAuthArgs) => {
        authResolutionCount += 1
        if (authResolutionCount === 4) {
          markBackgroundAuthStarted()
          await new Promise((resolve) => {
            finishBackgroundAuth = resolve
          })
        }
        return realGetAuth(...getAuthArgs)
      }
      return runtime
    })
    process.env.PI_PROVIDER_TEST_KEY = "stored-secret"
    writeFileSync(
      join(home, "auth.json"),
      JSON.stringify({
        "ramp-router": { type: "api_key", key: "$PI_PROVIDER_TEST_KEY" },
      }),
    )
    let requestCount = 0
    mock.method(globalThis, "fetch", async () => {
      requestCount += 1
      const id = requestCount === 1
        ? "cached-model"
        : requestCount === 2
          ? "stale-background-model"
          : "new-host-model"
      return new Response(
        JSON.stringify({
          data: [
            { id, owned_by: "openai", router: routerMetadata(id) },
          ],
        }),
        { status: 200 },
      )
    })
    await registerRouterProvider({ registerProvider: mock.fn() })

    const registerProvider = mock.fn()
    await registerRouterProvider({ registerProvider })
    const provider = registerProvider.mock.calls[0].arguments[0]
    await backgroundAuthStarted
    await provider.refreshModels({
      credential: { type: "api_key", key: "stored-secret" },
      allowNetwork: true,
      signal: new AbortController().signal,
      publish: async ({ update }) => {
        update?.()
        return true
      },
    })
    assert.equal(typeof finishBackgroundAuth, "function")
    finishBackgroundAuth()
    await new Promise((resolve) => setImmediate(resolve))

    assert.equal(registerProvider.mock.callCount(), 1)
    assert.deepEqual(provider.getModels().map((model) => model.id), [
      "new-host-model",
    ])
    assert.deepEqual(
      JSON.parse(
        readFileSync(join(home, "ramp-router-model-cache.json"), "utf8"),
      ).models.map((model) => model.id),
      ["new-host-model"],
    )
  })

  it("does not publish a host catalog after stored auth changes", async () => {
    const home = process.env.PI_CODING_AGENT_DIR
    const authPath = join(home, "auth.json")
    writeFileSync(
      authPath,
      JSON.stringify({
        "ramp-router": { type: "api_key", key: "first-secret" },
      }),
    )
    let finishRefresh
    let markRefreshStarted
    const refreshStarted = new Promise((resolve) => {
      markRefreshStarted = resolve
    })
    let requestCount = 0
    mock.method(globalThis, "fetch", async () => {
      requestCount += 1
      if (requestCount === 2) {
        markRefreshStarted()
        await new Promise((resolve) => {
          finishRefresh = resolve
        })
      }
      const id = requestCount === 1 ? "first-model" : "stale-model"
      return new Response(
        JSON.stringify({
          data: [{ id, owned_by: "openai", router: routerMetadata(id) }],
        }),
        { status: 200 },
      )
    })
    const registerProvider = mock.fn()
    await registerRouterProvider({ registerProvider })
    const [provider] = registerProvider.mock.calls[0].arguments
    const cachePath = join(home, "ramp-router-model-cache.json")
    const cacheBeforeRefresh = readFileSync(cachePath, "utf8")
    let updated = false
    const refresh = provider.refreshModels({
      // Pi's real online phase always passes the resolved stored/environment
      // credential, rather than leaving this undefined.
      credential: { type: "api_key", key: "first-secret" },
      allowNetwork: true,
      signal: new AbortController().signal,
      publish: async ({ update }) => {
        update?.()
        updated = updated || Boolean(update)
        return true
      },
    })
    await refreshStarted
    writeFileSync(
      authPath,
      JSON.stringify({
        "ramp-router": { type: "api_key", key: "second-secret" },
      }),
    )
    finishRefresh()
    await refresh

    assert.equal(updated, true)
    assert.notEqual(readFileSync(cachePath, "utf8"), cacheBeforeRefresh)
    assert.deepEqual(
      provider.filterModels(provider.getModels(), {
        type: "api_key",
        key: "second-secret",
      }),
      [],
    )
    const request = await provider.streamSimple(
      provider.getModels()[0],
      { messages: [] },
      { apiKey: "second-secret" },
    ).result()
    assert.equal(request.stopReason, "error")
    assert.match(
      request.errorMessage,
      /catalog does not match the active credential/,
    )
  })

  it("does not misclassify a stale stored credential as a runtime override", async () => {
    const home = process.env.PI_CODING_AGENT_DIR
    const authPath = join(home, "auth.json")
    writeFileSync(
      authPath,
      JSON.stringify({
        "ramp-router": { type: "api_key", key: "first-secret" },
      }),
    )
    mock.method(globalThis, "fetch", async (_url, init) => {
      const id = init.headers.authorization === "Bearer first-secret"
        ? "first-model"
        : "second-model"
      return new Response(
        JSON.stringify({
          data: [{ id, owned_by: "openai", router: routerMetadata(id) }],
        }),
        { status: 200 },
      )
    })
    const registerProvider = mock.fn()
    await registerRouterProvider({ registerProvider })
    const [provider] = registerProvider.mock.calls[0].arguments

    // Pi read A, then an external configure wrote B before Pi's offline phase.
    writeFileSync(
      authPath,
      JSON.stringify({
        "ramp-router": { type: "api_key", key: "second-secret" },
      }),
    )
    const signal = new AbortController().signal
    const publish = async ({ update }) => {
      update?.()
      return true
    }
    await provider.refreshModels({
      credential: { type: "api_key", key: "first-secret" },
      allowNetwork: false,
      signal,
      publish,
    })
    await provider.refreshModels({
      credential: { type: "api_key", key: "first-secret" },
      allowNetwork: true,
      signal,
      publish,
    })

    assert.deepEqual(
      provider.filterModels(provider.getModels(), {
        type: "api_key",
        key: "second-secret",
      }),
      [],
    )
    assert.equal(
      JSON.parse(
        readFileSync(join(home, "ramp-router-model-cache.json"), "utf8"),
      ).models[0].id,
      "first-model",
    )
  })

  it("does not publish an initial catalog after the credential changes", async () => {
    const home = process.env.PI_CODING_AGENT_DIR
    const authPath = join(home, "auth.json")
    writeFileSync(
      authPath,
      JSON.stringify({
        "ramp-router": { type: "api_key", key: "first-secret" },
      }),
    )
    let finishDiscovery
    let markDiscoveryStarted
    const discoveryStarted = new Promise((resolve) => {
      markDiscoveryStarted = resolve
    })
    mock.method(globalThis, "fetch", async () => {
      markDiscoveryStarted()
      await new Promise((resolve) => {
        finishDiscovery = resolve
      })
      return new Response(
        JSON.stringify({
          data: [
            {
              id: "first-model",
              owned_by: "openai",
              router: routerMetadata("first-model"),
            },
          ],
        }),
        { status: 200 },
      )
    })
    const registerProvider = mock.fn()
    const registration = registerRouterProvider({ registerProvider })
    await discoveryStarted
    assert.equal(typeof finishDiscovery, "function")

    writeFileSync(
      authPath,
      JSON.stringify({
        "ramp-router": { type: "api_key", key: "second-secret" },
      }),
    )
    finishDiscovery()
    await registration

    assert.equal(registerProvider.mock.callCount(), 1)
    assert.deepEqual(registerProvider.mock.calls[0].arguments[0].getModels(), [])
    assert.equal(existsSync(join(home, "ramp-router-model-cache.json")), false)
  })

  it("does not resolve or refresh another configured provider", async () => {
    const home = process.env.PI_CODING_AGENT_DIR
    const marker = join(home, "unrelated-provider-was-resolved")
    writeFileSync(
      join(home, "auth.json"),
      JSON.stringify({
        "ramp-router": { type: "api_key", key: "stored-secret" },
        unrelated: { type: "api_key", key: `!touch ${marker}` },
      }),
    )
    mock.method(globalThis, "fetch", async () =>
      new Response(
        JSON.stringify({
          data: [
            {
              id: "gpt-5.4",
              owned_by: "openai",
              router: routerMetadata("gpt-5.4"),
            },
          ],
        }),
        { status: 200 },
      ),
    )

    await registerRouterProvider({ registerProvider: mock.fn() })

    assert.equal(existsSync(marker), false)
  })

  it("ignores Pi's unscoped provider cache during host registration", async () => {
    process.env.RAMP_ROUTER_API_KEY = "current-secret"
    mock.method(globalThis, "fetch", async () =>
      new Response(
        JSON.stringify({
          data: [
            {
              id: "current-model",
              owned_by: "openai",
              router: routerMetadata("current-model"),
            },
          ],
        }),
        { status: 200 },
      ),
    )
    let provider
    await registerRouterProvider({
      registerProvider: (candidate) => {
        provider = candidate
      },
    })

    const publications = []
    await provider.refreshModels({
      credential: { type: "api_key", key: "current-secret" },
      stored: {
        models: [
          {
            id: "stale-model",
            name: "stale-model",
            provider: "ramp-router",
            api: "openai-responses",
            baseUrl: "https://old-router.example/v1",
            reasoning: false,
            input: ["text"],
            cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
            contextWindow: 1,
            maxTokens: 1,
          },
        ],
      },
      allowNetwork: false,
      signal: new AbortController().signal,
      publish: async (publication) => {
        publications.push(publication.persist)
        publication.update?.()
        return true
      },
    })

    assert.deepEqual(publications, [null])
    assert.deepEqual(provider.getModels().map((model) => model.id), [
      "current-model",
    ])
  })

  it("ignores and removes Pi's legacy store through the real host lifecycle", async () => {
    const home = process.env.PI_CODING_AGENT_DIR
    const authPath = join(home, "auth.json")
    const modelsPath = join(home, "host-models.json")
    const modelsStorePath = join(home, "models-store.json")
    writeFileSync(
      authPath,
      JSON.stringify({
        "ramp-router": { type: "api_key", key: "current-secret" },
      }),
    )
    writeFileSync(modelsPath, JSON.stringify({ providers: {} }))
    writeFileSync(
      modelsStorePath,
      JSON.stringify({
        "ramp-router": {
          models: [
            {
              id: "stale-model",
              name: "stale-model",
              provider: "ramp-router",
              api: "openai-responses",
              baseUrl: "https://old-router.example/v1",
              reasoning: false,
              input: ["text"],
              cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
              contextWindow: 1,
              maxTokens: 1,
            },
          ],
        },
      }),
    )
    mock.method(globalThis, "fetch", async (_url, init) => {
      const id = init.headers.authorization === "Bearer second-secret"
        ? "second-model"
        : "current-model"
      return new Response(
        JSON.stringify({
          data: [
            {
              id,
              owned_by: "openai",
              router: routerMetadata(id),
            },
          ],
        }),
        { status: 200 },
      )
    })

    let provider
    await registerRouterProvider({
      registerProvider: (candidate) => {
        provider = candidate
      },
    })
    const runtime = await ModelRuntime.create({
      authPath,
      modelsPath,
      modelsStorePath,
      refreshOnCreate: false,
      allowModelNetwork: false,
    })
    runtime.registerNativeProvider(provider)
    // Explicitly supersede and await the fire-and-forget registration refresh
    // before asserting its durable cleanup.
    const cleanupRefresh = await runtime.refresh({
      providers: ["ramp-router"],
      allowNetwork: false,
    })
    assert.equal(cleanupRefresh.aborted, false)
    assert.equal(cleanupRefresh.errors.size, 0)
    // registerNativeProvider owns an earlier fire-and-forget offline refresh.
    // Its publication chain can still be draining after our superseding
    // refresh resolves, so fence the observable file result rather than race
    // an asynchronous host lifecycle implementation detail.
    for (let attempt = 0; attempt < 100; attempt += 1) {
      if (
        !Object.hasOwn(
          JSON.parse(readFileSync(modelsStorePath, "utf8")),
          "ramp-router",
        )
      ) break
      await new Promise((resolve) => setTimeout(resolve, 5))
    }
    assert.equal(
      Object.hasOwn(JSON.parse(readFileSync(modelsStorePath, "utf8")), "ramp-router"),
      false,
    )
    assert.deepEqual(runtime.getModels("ramp-router").map((model) => model.id), [
      "current-model",
    ])

    await runtime.refresh({
      providers: ["ramp-router"],
      allowNetwork: true,
      force: true,
    })
    assert.deepEqual(runtime.getModels("ramp-router").map((model) => model.id), [
      "current-model",
    ])

    // Pi performs this offline synchronization immediately after a runtime
    // credential change. The prior credential's catalog must disappear before
    // the follow-up network refresh can make the new one selectable.
    await runtime.setRuntimeApiKey("ramp-router", "second-secret")
    assert.deepEqual(runtime.getModels("ramp-router"), [])

    await runtime.refresh({
      providers: ["ramp-router"],
      allowNetwork: true,
      force: true,
    })
    assert.deepEqual(runtime.getModels("ramp-router").map((model) => model.id), [
      "second-model",
    ])
    assert.deepEqual(
      JSON.parse(
        readFileSync(join(home, "ramp-router-model-cache.json"), "utf8"),
      ).models.map((model) => model.id),
      ["second-model"],
    )
    assert.equal(
      Object.hasOwn(JSON.parse(readFileSync(modelsStorePath, "utf8")), "ramp-router"),
      false,
    )
  })

  it("still starts Pi when startup discovery is unavailable", async () => {
    process.env.RAMP_ROUTER_API_KEY = "test-secret"
    mock.method(globalThis, "fetch", async () => {
      throw new Error("private network failure")
    })
    const registerProvider = mock.fn()

    await registerRouterProvider({ registerProvider })

    assert.equal(registerProvider.mock.callCount(), 1)
    const [provider] = registerProvider.mock.calls[0].arguments
    assert.deepEqual(provider.getModels(), [])
  })

  it("uses the startup-specific deadline for a stalled first discovery", async () => {
    process.env.RAMP_ROUTER_API_KEY = "test-secret"
    const startupController = new AbortController()
    const timeoutValues = []
    mock.method(AbortSignal, "timeout", (milliseconds) => {
      timeoutValues.push(milliseconds)
      if (milliseconds === 2_000) {
        queueMicrotask(() => startupController.abort())
        return startupController.signal
      }
      return new AbortController().signal
    })
    mock.method(globalThis, "fetch", async (_url, init) =>
      new Promise((_resolve, reject) => {
        init.signal.addEventListener(
          "abort",
          () => reject(init.signal.reason),
          { once: true },
        )
      }),
    )
    const registerProvider = mock.fn()

    await registerRouterProvider({ registerProvider })

    assert.ok(timeoutValues.includes(2_000))
    const [provider] = registerProvider.mock.calls[0].arguments
    assert.deepEqual(provider.getModels(), [])
  })

  it("discovers reasoning-aware Responses models with provider auth", async () => {
    writeFileSync(
      join(process.env.PI_CODING_AGENT_DIR, "auth.json"),
      JSON.stringify({
        "ramp-router": { type: "api_key", key: "test-secret" },
      }),
    )
    process.env.RAMP_ROUTER_BASE_URL = "http://localhost:8002"
    process.env.PI_OFFLINE = "1"
    mock.method(globalThis, "fetch", async (_url, init) => {
      assert.deepEqual(init.headers, {
        authorization: "Bearer test-secret",
        "user-agent": "ramp-cli-pi-provider",
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

    await registerRouterProvider({ registerProvider, on: mock.fn() })
    delete process.env.PI_OFFLINE

    assert.equal(registerProvider.mock.callCount(), 1)
    const [provider] = registerProvider.mock.calls[0].arguments
    assert.equal(provider.id, "ramp-router")
    assert.equal(provider.name, "Ramp Router")
    assert.equal(provider.baseUrl, "http://localhost:8002/v1")
    assert.equal(typeof provider.stream, "function")
    assert.deepEqual(provider.getModels(), [])

    const publications = []
    await provider.refreshModels({
      credential: { type: "api_key", key: "test-secret" },
      allowNetwork: true,
      signal: new AbortController().signal,
      publish: async ({ persist, update }) => {
        publications.push(persist)
        update?.()
        return true
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
    assert.deepEqual(publications, [null, undefined])
    const cached = JSON.parse(
      readFileSync(
        join(process.env.PI_CODING_AGENT_DIR, "ramp-router-model-cache.json"),
        "utf8",
      ),
    )
    assert.deepEqual(cached.models.map((model) => model.id), [
      "gpt-4o",
      "gpt-5.4",
      "claude-sonnet-4-6",
      "audio-only",
    ])
  })

  it("does not cache a superseded Pi refresh", async () => {
    const home = process.env.PI_CODING_AGENT_DIR
    writeFileSync(
      join(home, "auth.json"),
      JSON.stringify({
        "ramp-router": { type: "api_key", key: "test-secret" },
      }),
    )
    process.env.RAMP_ROUTER_BASE_URL = "http://localhost:8002"
    process.env.PI_OFFLINE = "1"
    mock.method(globalThis, "fetch", async () =>
      new Response(
        JSON.stringify({
          data: [
            {
              id: "superseded-model",
              owned_by: "openai",
              router: routerMetadata("superseded-model"),
            },
          ],
        }),
        { status: 200 },
      ),
    )
    let provider
    await registerRouterProvider({
      registerProvider: (candidate) => {
        provider = candidate
      },
    })
    delete process.env.PI_OFFLINE
    const signal = new AbortController().signal
    let publicationCount = 0

    await provider.refreshModels({
      credential: { type: "api_key", key: "test-secret" },
      allowNetwork: true,
      signal,
      publish: async () => {
        publicationCount += 1
        // The cleanup crosses the first gate, then a newer Pi refresh starts
        // before this response can publish its models.
        if (publicationCount === 1) return true
        return false
      },
    })

    assert.equal(publicationCount, 2)
    assert.equal(existsSync(join(home, "ramp-router-model-cache.json")), false)
    assert.deepEqual(provider.getModels(), [])
  })

  it("uses production Router by default", async () => {
    // Pointed at an empty directory: the plugin reads the configured Router
    // from the agent directory, so a developer whose own Pi is set up would
    // otherwise see their stack here.
    process.env.PI_CODING_AGENT_DIR = mkdtempSync(join(tmpdir(), "pi-home-"))
    const registerProvider = mock.fn()

    await registerRouterProvider({ registerProvider, on: mock.fn() })

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

    assert.equal(await registeredBaseUrl(), "http://127.0.0.1:28362/v1")
  })

  it("still lets the environment win for a one-off run", async () => {
    const home = mkdtempSync(join(tmpdir(), "pi-home-"))
    writeFileSync(
      join(home, "ramp-router-config.json"),
      JSON.stringify({ baseUrl: "http://127.0.0.1:28362/v1" }),
    )
    process.env.PI_CODING_AGENT_DIR = home
    process.env.RAMP_ROUTER_BASE_URL = "https://other.example/v1"

    assert.equal(await registeredBaseUrl(), "https://other.example/v1")
  })

  it("falls back to production when nothing was recorded", async () => {
    process.env.PI_CODING_AGENT_DIR = mkdtempSync(join(tmpdir(), "pi-home-"))
    delete process.env.RAMP_ROUTER_BASE_URL

    assert.equal(await registeredBaseUrl(), "https://router-api.ramp.com/v1")
  })
})

async function registeredBaseUrl() {
  let seen
  await registerRouterProvider({
    registerProvider: (p) => { seen = p.baseUrl },
    on: mock.fn(),
  })
  return seen
}

async function lineageHeaders({
  provider = "ramp-router",
  sessionID = "019ff2af-7ce1-7000-8000-000000000001",
  parentSession,
  initialHeaders = { "Existing-Header": "preserved" },
} = {}) {
  let handler
  await registerRouterProvider({
    registerProvider: () => {},
    on: (event, candidate) => {
      if (event === "before_provider_headers") handler = candidate
    },
  })
  assert.equal(typeof handler, "function")

  const headers = { ...initialHeaders }
  handler(
    { type: "before_provider_headers", headers },
    {
      model: { provider },
      sessionManager: {
        getSessionId: () => sessionID,
        getHeader: () => ({
          type: "session",
          id: sessionID,
          ...(parentSession ? { parentSession } : {}),
        }),
      },
    },
  )
  return headers
}

describe("Pi session lineage headers", () => {
  it("emits the durable session ID only for Ramp Router", async () => {
    assert.deepEqual(await lineageHeaders(), {
      "Existing-Header": "preserved",
      "X-Gateway-Client": "pi",
      "X-Session-Id": "019ff2af-7ce1-7000-8000-000000000001",
    })
    assert.deepEqual(await lineageHeaders({ provider: "openai" }), {
      "Existing-Header": "preserved",
    })
  })

  it("replaces differently-cased stale lineage headers", async () => {
    assert.deepEqual(
      await lineageHeaders({
        initialHeaders: {
          "x-gateway-client": "stale",
          "x-session-id": "stale",
          "X-PARENT-SESSION-ID": "stale",
          "x-forked-from-session-id": "stale",
        },
      }),
      {
        "X-Gateway-Client": "pi",
        "X-Session-Id": "019ff2af-7ce1-7000-8000-000000000001",
      },
    )
  })

  it("resolves a fork source from only the parent session header", async () => {
    const directory = mkdtempSync(join(tmpdir(), "pi-parent-"))
    const parent = join(directory, "parent.jsonl")
    writeFileSync(
      parent,
      [
        JSON.stringify({
          type: "session",
          version: 3,
          id: "019ff2af-7ce1-7000-8000-000000000000",
          timestamp: "2026-08-11T00:00:00.000Z",
          cwd: "/tmp",
        }),
        // The resolver must not parse conversation entries after this header.
        "not-json-private-conversation-data",
      ].join("\n"),
    )

    assert.deepEqual(await lineageHeaders({ parentSession: parent }), {
      "Existing-Header": "preserved",
      "X-Gateway-Client": "pi",
      "X-Session-Id": "019ff2af-7ce1-7000-8000-000000000001",
      "X-Parent-Session-Id": "019ff2af-7ce1-7000-8000-000000000000",
      "X-Forked-From-Session-Id": "019ff2af-7ce1-7000-8000-000000000000",
    })
  })

  it("omits invalid, unreadable, and oversized parent headers", async () => {
    const directory = mkdtempSync(join(tmpdir(), "pi-parent-"))
    const invalid = join(directory, "invalid.jsonl")
    const oversized = join(directory, "oversized.jsonl")
    const unterminated = join(directory, "unterminated.jsonl")
    writeFileSync(invalid, '{"type":"session","id":"bad id"}\n')
    writeFileSync(oversized, `${"x".repeat(4097)}\n`)
    writeFileSync(
      unterminated,
      JSON.stringify({ type: "session", id: "valid-but-unterminated" }),
    )

    for (const parentSession of [
      join(directory, "missing.jsonl"),
      invalid,
      oversized,
      unterminated,
    ]) {
      assert.deepEqual(await lineageHeaders({ parentSession }), {
        "Existing-Header": "preserved",
        "X-Gateway-Client": "pi",
        "X-Session-Id": "019ff2af-7ce1-7000-8000-000000000001",
      })
    }
  })

  it(
    "rejects a FIFO parent path without blocking the inference request",
    { skip: process.platform === "win32" },
    async () => {
      const directory = mkdtempSync(join(tmpdir(), "pi-parent-"))
      const fifo = join(directory, "parent.fifo")
      execFileSync("mkfifo", [fifo])

      assert.deepEqual(await lineageHeaders({ parentSession: fifo }), {
        "Existing-Header": "preserved",
        "X-Gateway-Client": "pi",
        "X-Session-Id": "019ff2af-7ce1-7000-8000-000000000001",
      })
    },
  )

  it("does not emit invalid or self-referential session ancestry", async () => {
    assert.deepEqual(await lineageHeaders({ sessionID: "bad id" }), {
      "Existing-Header": "preserved",
    })

    const directory = mkdtempSync(join(tmpdir(), "pi-parent-"))
    const parent = join(directory, "parent.jsonl")
    writeFileSync(
      parent,
      `${JSON.stringify({
        type: "session",
        id: "019ff2af-7ce1-7000-8000-000000000001",
      })}\n`,
    )
    assert.deepEqual(await lineageHeaders({ parentSession: parent }), {
      "Existing-Header": "preserved",
      "X-Gateway-Client": "pi",
      "X-Session-Id": "019ff2af-7ce1-7000-8000-000000000001",
    })
  })
})
