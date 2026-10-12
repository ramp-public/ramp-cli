import assert from "node:assert/strict"
import { describe, it } from "node:test"

import {
  applyWorkspaceDefaultOnSessionStart,
  fetchWorkspaceDefault,
  startsNewConversation,
  workspaceDefaultCache,
} from "../src/workspace-default.ts"

function settings(harnesses) {
  return new Response(
    JSON.stringify({ version: 3, model_policy_version: 1, harnesses }),
    { status: 200 },
  )
}

const available = { model: "gemini-3.8-flash", effective_model: "gemini-3.8-flash", status: "available" }
const key = async () => "router-secret"

describe("fetchWorkspaceDefault", () => {
  it("reads Pi's effective model from the key-scoped settings", async () => {
    const calls = []
    const model = await fetchWorkspaceDefault({
      usageOrigin: "https://qa-internal.router.com/",
      resolveAPIKey: key,
      rampCliVersion: "0.2.56",
      fetch: async (url, init) => {
        calls.push([url, init.headers])
        return settings({ pi: available, opencode: { effective_model: "other" } })
      },
    })
    assert.equal(model, "gemini-3.8-flash")
    assert.deepEqual(calls, [
      [
        "https://qa-internal.router.com/self-service/coding-agent-settings",
        {
          authorization: "Bearer router-secret",
          "X-Gateway-Ramp-Cli-Version": "0.2.56",
        },
      ],
    ])
  })

  // Router answers `switchyard` (its virtual model) when the key's own
  // Switchyard routing outranks the admin default, with the source named in
  // `effective_model_source`. The name is used as given, and the source
  // field, unknown or absent, is not read.
  for (const [name, entry] of [
    ["the key's Switchyard routing", { model: "gpt-5.6-sol", effective_model: "switchyard", status: "available", effective_model_source: "switchyard" }],
    ["an unknown source", { model: "gpt-5.6-sol", effective_model: "switchyard", status: "available", effective_model_source: "mystery" }],
    ["no source field", { model: "gpt-5.6-sol", effective_model: "switchyard", status: "available" }],
  ]) {
    it(`takes the switchyard model as named for ${name}`, async () => {
      const model = await fetchWorkspaceDefault({
        usageOrigin: "https://router.example",
        resolveAPIKey: key,
        fetch: async () => settings({ pi: entry }),
      })
      assert.equal(model, "switchyard")
    })
  }

  it("does not call Router without a credential", async () => {
    let calls = 0
    const model = await fetchWorkspaceDefault({
      usageOrigin: "https://router.example",
      resolveAPIKey: async () => undefined,
      fetch: async () => {
        calls += 1
        return settings({ pi: available })
      },
    })
    assert.equal(model, undefined)
    assert.equal(calls, 0)
  })

  for (const [name, respond] of [
    ["an unset default", () => settings({ pi: { model: null, effective_model: null, status: "unset" } })],
    ["a default the policy no longer allows", () => settings({ pi: { model: "gone", effective_model: null, status: "unavailable" } })],
    ["a Router that lists only other harnesses", () => settings({ opencode: available })],
    ["an older Router without the endpoint", () => new Response("", { status: 404 })],
    ["a rejected key", () => new Response("{}", { status: 403 })],
    ["a malformed payload", () => new Response("[]", { status: 200 })],
    ["a network failure", () => { throw new TypeError("offline") }],
  ]) {
    it(`reads ${name} as no default`, async () => {
      const model = await fetchWorkspaceDefault({
        usageOrigin: "https://router.example",
        resolveAPIKey: key,
        fetch: async () => respond(),
      })
      assert.equal(model, undefined)
    })
  }

  it("gives up on a slow Router instead of waiting", async () => {
    const model = await fetchWorkspaceDefault({
      usageOrigin: "https://router.example",
      resolveAPIKey: key,
      timeoutMs: 10,
      fetch: (_url, init) =>
        new Promise((_resolve, reject) => {
          init.signal.addEventListener("abort", () => reject(init.signal.reason))
        }),
    })
    assert.equal(model, undefined)
  })
})

describe("workspaceDefaultCache", () => {
  it("fetches once per window and shares an in-flight request", async () => {
    let fetches = 0
    let clock = 0
    const cache = workspaceDefaultCache({
      usageOrigin: "https://router.example",
      resolveAPIKey: key,
      ttlMs: 1000,
      now: () => clock,
      fetch: async () => {
        fetches += 1
        return settings({ pi: { effective_model: `pick-${fetches}` } })
      },
    })
    cache.prefetch()
    assert.deepEqual(await Promise.all([cache.get(), cache.get()]), ["pick-1", "pick-1"])
    assert.equal(fetches, 1)
    clock = 999
    assert.equal(await cache.get(), "pick-1")
    clock = 1000
    assert.equal(await cache.get(), "pick-2")
    assert.equal(fetches, 2)
  })
})

// Pi records the starting model and thinking level as a session opens, so
// even a brand-new session is not literally empty.
const empty = {
  sessionManager: {
    getEntries: () => [{ type: "model_change" }, { type: "thinking_level_change" }],
  },
}
const continued = {
  sessionManager: {
    getEntries: () => [{ type: "model_change" }, { type: "message" }, { type: "message" }],
  },
}

describe("startsNewConversation", () => {
  it("counts a fresh launch and /new as new", () => {
    assert.equal(startsNewConversation({ reason: "startup" }, empty, ["pi"]), true)
    assert.equal(startsNewConversation({ reason: "new" }, empty, ["pi"]), true)
  })

  it("leaves --continue, --resume, forks and reloads on their model", () => {
    assert.equal(startsNewConversation({ reason: "startup" }, continued, ["pi", "-c"]), false)
    assert.equal(startsNewConversation({ reason: "resume" }, empty, ["pi"]), false)
    assert.equal(startsNewConversation({ reason: "fork" }, empty, ["pi"]), false)
    assert.equal(startsNewConversation({ reason: "reload" }, empty, ["pi"]), false)
  })

  it("respects an explicit --model for this launch", () => {
    assert.equal(
      startsNewConversation({ reason: "startup" }, empty, ["pi", "--model", "mine"]),
      false,
    )
  })
})

const registry = {
  find: (provider, id) =>
    provider === "ramp-router" && ["gpt-5.6-sol", "gemini-3.8-flash", "switchyard"].includes(id)
      ? { provider, id }
      : undefined,
}

async function run({
  event = { reason: "startup" },
  context = empty,
  modelId = "gemini-3.8-flash",
  current,
  switchedTo,
  promptedMeanwhile = false,
}) {
  const set = []
  let model = current
  let entries = context.sessionManager.getEntries()
  context = { ...context, sessionManager: { getEntries: () => entries } }
  const handler = applyWorkspaceDefaultOnSessionStart({
    providerId: "ramp-router",
    defaults: {
      get: async () => {
        // A `/model` pick, or a prompt from the command line, that lands
        // while Router is still answering.
        if (switchedTo) model = switchedTo
        if (promptedMeanwhile) entries = [...entries, { type: "message" }]
        return modelId === "none" ? undefined : modelId
      },
    },
    setModel: async (next) => {
      set.push(next)
      return true
    },
    argv: ["pi"],
  })
  handler(event, {
    ...context,
    modelRegistry: registry,
    get model() {
      return model
    },
  })
  // The handler returns before the switch lands.
  assert.deepEqual(set, [])
  await new Promise((resolve) => setImmediate(resolve))
  return set
}

describe("applyWorkspaceDefaultOnSessionStart", () => {
  it("starts a fresh launch on the organization default", async () => {
    assert.deepEqual(await run({ current: { provider: "ramp-router", id: "gpt-5.6-sol" } }), [
      { provider: "ramp-router", id: "gemini-3.8-flash" },
    ])
  })

  it("starts /new on the organization default even after a switch", async () => {
    assert.deepEqual(
      await run({ event: { reason: "new" }, context: empty, current: { provider: "ramp-router", id: "gpt-5.6-sol" } }),
      [{ provider: "ramp-router", id: "gemini-3.8-flash" }],
    )
  })

  it("leaves a resumed session on its model", async () => {
    assert.deepEqual(await run({ event: { reason: "resume" }, context: continued }), [])
    assert.deepEqual(await run({ event: { reason: "startup" }, context: continued }), [])
  })

  it("ignores a default this key's catalog does not serve", async () => {
    assert.deepEqual(await run({ modelId: "elsewhere" }), [])
  })

  it("starts on the switchyard virtual model when the key's catalog lists it", async () => {
    assert.deepEqual(
      await run({ modelId: "switchyard", current: { provider: "ramp-router", id: "gpt-5.6-sol" } }),
      [{ provider: "ramp-router", id: "switchyard" }],
    )
  })

  it("does nothing when the workspace has no default", async () => {
    assert.deepEqual(await run({ modelId: "none" }), [])
  })

  it("holds start-up for the answer when there is no UI, as in --print", async () => {
    const set = []
    let answer
    const handler = applyWorkspaceDefaultOnSessionStart({
      providerId: "ramp-router",
      defaults: { get: () => new Promise((resolve) => (answer = resolve)) },
      setModel: async (model) => {
        set.push(model)
        return true
      },
      argv: ["pi", "--print", "hi"],
    })
    const context = { ...empty, modelRegistry: registry, model: undefined, hasUI: false }
    let settled = false
    const pending = handler({ reason: "startup" }, context).then(() => (settled = true))
    await new Promise((resolve) => setImmediate(resolve))
    assert.equal(settled, false)
    answer("gemini-3.8-flash")
    await pending
    assert.deepEqual(set, [{ provider: "ramp-router", id: "gemini-3.8-flash" }])
    // Interactively the same handler returns before the answer.
    await handler({ reason: "startup" }, { ...context, hasUI: true })
    assert.equal(set.length, 1)
  })

  it("leaves a conversation that began while Router was still answering", async () => {
    assert.deepEqual(
      await run({ current: { provider: "ramp-router", id: "gpt-5.6-sol" }, promptedMeanwhile: true }),
      [],
    )
  })

  it("keeps a model the user picked while Router was still answering", async () => {
    assert.deepEqual(
      await run({
        current: { provider: "ramp-router", id: "gpt-5.6-sol" },
        switchedTo: { provider: "anthropic", id: "claude-opus-4" },
      }),
      [],
    )
  })

  it("skips the switch when Pi already starts on the default", async () => {
    assert.deepEqual(await run({ current: { provider: "ramp-router", id: "gemini-3.8-flash" } }), [])
  })

  it("swallows a failed switch", async () => {
    const handler = applyWorkspaceDefaultOnSessionStart({
      providerId: "ramp-router",
      defaults: { get: async () => "gemini-3.8-flash" },
      setModel: async () => {
        throw new Error("No API key")
      },
      argv: ["pi"],
    })
    handler({ reason: "startup" }, { ...empty, modelRegistry: registry, model: undefined })
    await new Promise((resolve) => setImmediate(resolve))
  })
})
