import assert from "node:assert/strict"
import { describe, it } from "node:test"

import {
  fetchWorkspaceDefault,
  workspaceDefaultApplier,
  workspaceDefaultCache,
} from "../src/workspace-default.ts"

function settings(harnesses) {
  return new Response(
    JSON.stringify({ version: 3, model_policy_version: 1, harnesses }),
    { status: 200 },
  )
}

const available = { model: "gemini-3.8-flash", effective_model: "gemini-3.8-flash", status: "available" }

describe("fetchWorkspaceDefault", () => {
  it("reads OpenCode's effective model from the key-scoped settings", async () => {
    const calls = []
    const model = await fetchWorkspaceDefault({
      usageOrigin: "https://qa-internal.router.com/",
      apiKey: "router-secret",
      rampCliVersion: "0.2.56",
      fetch: async (url, init) => {
        calls.push([url, init.headers])
        return settings({ opencode: available, pi: { effective_model: "other" } })
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

  for (const [name, respond] of [
    ["an unset default", () => settings({ opencode: { model: null, effective_model: null, status: "unset" } })],
    ["a default the policy no longer allows", () => settings({ opencode: { model: "gone", effective_model: null, status: "unavailable" } })],
    ["a Router that lists only other harnesses", () => settings({ pi: available })],
    ["an older Router without the endpoint", () => new Response("", { status: 404 })],
    ["a rejected key", () => new Response("{}", { status: 403 })],
    ["a malformed payload", () => new Response("[]", { status: 200 })],
    ["a network failure", () => { throw new TypeError("offline") }],
  ]) {
    it(`reads ${name} as no default`, async () => {
      const model = await fetchWorkspaceDefault({
        usageOrigin: "https://router.example",
        apiKey: "k",
        fetch: async () => respond(),
      })
      assert.equal(model, undefined)
    })
  }

  // Routers before the one-time Switchyard push answered `switchyard` (its
  // virtual model) for every harness with source "switchyard"; that is not
  // a workspace default and is ignored. A `switchyard` the admin chose, or
  // one without a recognized source, is used as given.
  it("ignores the switchyard model older Routers selected for the key", async () => {
    const model = await fetchWorkspaceDefault({
      usageOrigin: "https://router.example",
        apiKey: "k",
      fetch: async () =>
        settings({ opencode: { model: "gpt-5.6-sol", effective_model: "switchyard", status: "available", effective_model_source: "switchyard" } }),
    })
    assert.equal(model, undefined)
  })

  for (const [name, entry] of [
    ["a workspace default", { model: "switchyard", effective_model: "switchyard", status: "available", effective_model_source: "workspace" }],
    ["an unknown source", { model: "gpt-5.6-sol", effective_model: "switchyard", status: "available", effective_model_source: "mystery" }],
    ["no source field", { model: "gpt-5.6-sol", effective_model: "switchyard", status: "available" }],
  ]) {
    it(`takes the switchyard model as named for ${name}`, async () => {
      const model = await fetchWorkspaceDefault({
        usageOrigin: "https://router.example",
        apiKey: "k",
        fetch: async () => settings({ opencode: entry }),
      })
      assert.equal(model, "switchyard")
    })
  }

  it("gives up on a slow Router instead of waiting", async () => {
    const model = await fetchWorkspaceDefault({
      usageOrigin: "https://router.example",
      apiKey: "k",
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
      apiKey: "k",
      ttlMs: 1000,
      now: () => clock,
      fetch: async () => {
        fetches += 1
        return settings({ opencode: { effective_model: `pick-${fetches}` } })
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

function events(...list) {
  return (async function* () {
    for (const event of list) yield event
  })()
}

const created = (sessionID, extra = {}) => ({
  type: "session.created",
  data: { sessionID, projectID: "p", slug: "s", version: "2", ...extra },
})

/** A stand-in for OpenCode's agent domain: transforms replay on reload. */
function fakeAgents(definitions) {
  const transforms = new Set()
  let agents = []
  let reloads = 0
  const build = () => {
    agents = definitions.map((agent) => ({ ...agent }))
    const editor = {
      list: () => agents,
      update: (id, update) => {
        let agent = agents.find((candidate) => candidate.id === id)
        if (!agent) {
          agent = { id, mode: "all", hidden: false }
          agents.push(agent)
        }
        update(agent)
      },
    }
    for (const transform of transforms) transform(editor)
  }
  let defaultModel
  const modelTransforms = []
  const rebuildModels = () => {
    defaultModel = { providerID: "ramp-router", modelID: "gpt-5.6-sol" }
    const editor = { default: { set: (providerID, modelID) => (defaultModel = { providerID, modelID }) } }
    for (const transform of modelTransforms) transform(editor)
  }
  return {
    domain: {
      transform: async (callback) => {
        transforms.add(callback)
        build()
        return {
          dispose: async () => {
            transforms.delete(callback)
            build()
          },
        }
      },
      reload: async () => {
        reloads += 1
        build()
      },
      list: async () => ({ location: { directory: "/work" }, data: agents }),
    },
    modelDomain: {
      transform: async (callback) => {
        modelTransforms.push(callback)
        rebuildModels()
      },
      reload: async () => rebuildModels(),
    },
    models: () => Object.fromEntries(agents.map((agent) => [agent.id, agent.model])),
    defaultModel: () => defaultModel,
    reloads: () => reloads,
  }
}

const sol = { id: "gpt-5.6-sol", providerID: "ramp-router" }
const definitions = [
  { id: "build", mode: "primary", hidden: false },
  { id: "plan", mode: "primary", hidden: false },
  // A primary agent whose definition names a model keeps it.
  { id: "review", mode: "primary", hidden: false, model: sol },
  { id: "general", mode: "subagent", hidden: false, model: sol },
  { id: "compaction", mode: "primary", hidden: true },
]
const untouched = { review: sol, general: sol, compaction: undefined }

function fakeTimers() {
  const intervals = []
  return {
    setInterval: (callback, ms) => {
      const handle = { callback, ms, cleared: false }
      intervals.push(handle)
      return handle
    },
    clearInterval: (handle) => {
      handle.cleared = true
    },
    intervals,
  }
}

function applier({
  modelID = "gemini-3.8-flash",
  agents = fakeAgents(definitions),
  modelIDs,
  configured,
  timers = fakeTimers(),
} = {}) {
  let answer = modelID === "none" ? undefined : modelID
  const made = workspaceDefaultApplier({
    agent: agents.domain,
    model: agents.modelDomain,
    providerID: "ramp-router",
    modelIDs: new Set(modelIDs ?? ["gpt-5.6-sol", "gemini-3.8-flash", "gpt-5.5"]),
    defaults: { get: async () => answer },
    configured,
    timers,
  })
  return { made, agents, timers, answer: (next) => (answer = next) }
}

const flash = { id: "gemini-3.8-flash", providerID: "ramp-router" }

describe("workspaceDefaultApplier", () => {
  it("gives the primary agents and the registry default the organization default", async () => {
    const { made, agents } = applier()
    await made.refresh()
    assert.equal(made.current(), "gemini-3.8-flash")
    assert.deepEqual(agents.models(), { build: flash, plan: flash, ...untouched })
    assert.deepEqual(agents.defaultModel(), { providerID: "ramp-router", modelID: "gemini-3.8-flash" })
    assert.equal(agents.reloads(), 1)
  })

  it("ignores a default this key's catalog does not serve", async () => {
    const { made, agents } = applier({ modelID: "elsewhere" })
    await made.refresh()
    assert.equal(made.current(), undefined)
    assert.deepEqual(agents.models(), { build: undefined, plan: undefined, ...untouched })
    assert.equal(agents.reloads(), 0)
  })

  it("starts on the switchyard virtual model when the key's catalog lists it", async () => {
    const switchyard = { id: "switchyard", providerID: "ramp-router" }
    const { made, agents } = applier({
      modelID: "switchyard",
      modelIDs: ["gpt-5.6-sol", "gemini-3.8-flash", "switchyard"],
    })
    await made.refresh()
    assert.equal(made.current(), "switchyard")
    assert.deepEqual(agents.models(), { build: switchyard, plan: switchyard, ...untouched })
    assert.deepEqual(agents.defaultModel(), { providerID: "ramp-router", modelID: "switchyard" })
  })

  it("leaves the agents alone when the workspace has no default", async () => {
    const { made, agents } = applier({ modelID: "none" })
    await made.refresh()
    assert.deepEqual(agents.models(), { build: undefined, plan: undefined, ...untouched })
    assert.equal(agents.reloads(), 0)
  })

  it("rebuilds the agents only when the default changes", async () => {
    const { made, agents, answer } = applier()
    await made.refresh()
    await made.refresh()
    assert.equal(agents.reloads(), 1)
    answer("gpt-5.6-sol")
    await made.refresh()
    assert.equal(agents.reloads(), 2)
    assert.deepEqual(agents.models().plan, sol)
    answer(undefined)
    await made.refresh()
    assert.equal(agents.reloads(), 3)
    assert.deepEqual(agents.models(), { build: undefined, plan: undefined, ...untouched })
    assert.deepEqual(agents.defaultModel(), { providerID: "ramp-router", modelID: "gpt-5.6-sol" })
  })

  it("reaches agents the user's config defines after the plugin loaded", async () => {
    const { made, agents } = applier()
    await made.refresh()
    // OpenCode's config-agent plugin registers behind external plugins and
    // creates the configured agents there.
    await agents.domain.transform((editor) => {
      editor.update("writer", (agent) => {
        agent.mode = "primary"
      })
      editor.update("pinned", (agent) => {
        agent.mode = "primary"
        agent.model = sol
      })
    })
    assert.equal(agents.models().writer, undefined)
    await made.watch(events({ type: "agent.updated", data: {} }))
    await new Promise((resolve) => setImmediate(resolve))
    assert.deepEqual(agents.models(), { build: flash, plan: flash, writer: flash, pinned: sol, ...untouched })
    // Settled: a further look finds nothing left to reach.
    const reloads = agents.reloads()
    await made.refresh()
    assert.equal(agents.reloads(), reloads)
  })

  it("takes its model back from a built-in agent the user's config makes a subagent", async () => {
    const { made, agents } = applier()
    await made.refresh()
    // The config-agent plugin applies the configured mode behind this one.
    await agents.domain.transform((editor) => {
      editor.update("build", (agent) => {
        agent.mode = "subagent"
      })
    })
    assert.deepEqual(agents.models().build, flash)
    await made.watch(events({ type: "agent.updated", data: {} }))
    await new Promise((resolve) => setImmediate(resolve))
    assert.deepEqual(agents.models(), { build: undefined, plan: flash, ...untouched })
    const reloads = agents.reloads()
    await made.refresh()
    assert.equal(agents.reloads(), reloads)
  })

  it("records the model a session without one first requests", async () => {
    const switched = []
    const sessions = { bare: {}, chosen: { model: { providerID: "ramp-router", id: "gpt-5.5" } } }
    let hook
    const session = {
      get: async ({ sessionID }) => sessions[sessionID],
      switchModel: async (input) => void switched.push(input),
      hook: async (name, callback) => {
        assert.equal(name, "model.request")
        hook = callback
        return { dispose: async () => {} }
      },
    }
    const made = workspaceDefaultApplier({
      agent: fakeAgents(definitions).domain,
      model: fakeAgents(definitions).modelDomain,
      providerID: "ramp-router",
      modelIDs: new Set(["gemini-3.8-flash"]),
      defaults: { get: async () => "gemini-3.8-flash" },
      session,
      timers: fakeTimers(),
    })
    await made.refresh()
    const request = (sessionID, kind = "primary", model = flash) => hook({ sessionID, kind, model })
    await request("bare", "title")
    await request("chosen")
    assert.deepEqual(switched, [])
    await request("bare", "primary", { ...flash, variant: "high" })
    await request("bare", "primary", { providerID: "ramp-router", id: "gpt-5.5" })
    assert.deepEqual(switched, [
      { sessionID: "bare", model: { providerID: "ramp-router", id: "gemini-3.8-flash", variant: "high" } },
    ])
  })

  it("re-reads the default on a timer while the process lives", async () => {
    const { made, agents, timers, answer } = applier()
    await made.refresh()
    const watching = made.watch(events())
    await watching
    const [interval] = timers.intervals
    assert.equal(interval.ms, 60_000)
    assert.equal(interval.cleared, true)
    answer("gpt-5.6-sol")
    interval.callback()
    await new Promise((resolve) => setImmediate(resolve))
    assert.equal(made.current(), "gpt-5.6-sol")
    assert.deepEqual(agents.models().build, sol)
  })

  it("re-reads the default as new top-level sessions are created", async () => {
    const { made, agents, answer } = applier()
    await made.refresh()
    answer("gpt-5.6-sol")
    await made.watch(
      events(
        { type: "session.idle", data: { sessionID: "old" } },
        created("child", { parentID: "parent" }),
      ),
    )
    assert.equal(agents.reloads(), 1)
    await made.watch(events(created("new")))
    await new Promise((resolve) => setImmediate(resolve))
    assert.equal(made.current(), "gpt-5.6-sol")
    assert.equal(agents.reloads(), 2)
  })

  it("falls back to the model configure chose when the workspace has none", async () => {
    const five = { id: "gpt-5.5", providerID: "ramp-router" }
    const { made, agents, answer } = applier({ modelID: "none", configured: "gpt-5.5" })
    // Applied as soon as the transforms register, before Router answers.
    assert.equal(made.current(), "gpt-5.5")
    assert.deepEqual(agents.models(), { build: five, plan: five, ...untouched })
    assert.deepEqual(agents.defaultModel(), { providerID: "ramp-router", modelID: "gpt-5.5" })
    await made.refresh()
    assert.equal(agents.reloads(), 0)
    answer("gemini-3.8-flash")
    await made.refresh()
    assert.deepEqual(agents.models().build, flash)
    answer(undefined)
    await made.refresh()
    assert.deepEqual(agents.models().build, five)
    assert.equal(agents.reloads(), 2)
  })

  it("ignores a configured model this key's catalog does not serve", async () => {
    const { made, agents } = applier({ modelID: "none", configured: "retired" })
    await made.refresh()
    assert.equal(made.current(), undefined)
    assert.deepEqual(agents.models(), { build: undefined, plan: undefined, ...untouched })
  })

  it("keeps the agents as they are when the read fails", async () => {
    const agents = fakeAgents(definitions)
    const made = workspaceDefaultApplier({
      agent: agents.domain,
      model: agents.modelDomain,
      providerID: "ramp-router",
      modelIDs: new Set(["gemini-3.8-flash"]),
      defaults: {
        get: async () => {
          throw new Error("boom")
        },
      },
    })
    await made.refresh()
    assert.equal(made.current(), undefined)
    assert.equal(agents.reloads(), 0)
  })
})
