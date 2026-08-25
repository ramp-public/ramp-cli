import assert from "node:assert/strict"
import { afterEach, describe, it, mock } from "node:test"

import {
  fetchSessionUsage,
  registerUsageWidget,
  usageWidgetLines,
} from "../src/usage.ts"

afterEach(() => {
  delete process.env.PI_OFFLINE
})

function payload(overrides = {}) {
  return {
    session: {
      request_count: 4,
      spend_usd: 0.42,
      reference_model: "claude-opus-5",
      reference_cost_usd: 1.13,
      last_model: "gpt-5.4",
      last_model_provider: "openai",
      ...overrides,
    },
    switchyard_routing_enabled: true,
  }
}

function response(overrides = {}) {
  return new Response(JSON.stringify(payload(overrides)), { status: 200 })
}

function usage(overrides = {}) {
  return {
    requestCount: 4,
    spendUSD: 0.42,
    referenceModel: "claude-opus-5",
    referenceCostUSD: 1.13,
    lastModel: "gpt-5.4",
    lastModelProvider: "openai",
    switchyardEnabled: true,
    ...overrides,
  }
}

describe("Pi Router session usage", () => {
  it("queries the client-agnostic usage contract with Pi's credential", async () => {
    const fetcher = mock.fn(async () =>
      new Response(JSON.stringify(payload()), { status: 200 }),
    )

    assert.deepEqual(
      await fetchSessionUsage({
        usageOrigin: "https://app.router.com",
        apiKey: "usage-secret",
        sessionID: "pi-session",
        fetch: fetcher,
      }),
      usage(),
    )
    assert.equal(
      fetcher.mock.calls[0].arguments[0],
      "https://app.router.com/session-usage/usage/session" +
        "?client_session_id=pi-session" +
        "&include_switchyard_routing_enabled=true" +
        "&include_last_model=true",
    )
    assert.deepEqual(fetcher.mock.calls[0].arguments[1].headers, {
      authorization: "Bearer usage-secret",
    })
  })

  it("fails open for unavailable or invalid usage", async () => {
    for (const respond of [
      async () => new Response("denied", { status: 401 }),
      async () => new Response("not-json", { status: 200 }),
      async () => new Response(JSON.stringify({ session: {} }), { status: 200 }),
      async () => {
        throw new Error("network down with usage-secret")
      },
    ]) {
      assert.equal(
        await fetchSessionUsage({
          usageOrigin: "https://app.router.com",
          apiKey: "usage-secret",
          sessionID: "pi-session",
          fetch: respond,
        }),
        undefined,
      )
    }
  })
})

describe("usageWidgetLines", () => {
  it("renders Router attribution and Claude Code's comparison semantics", () => {
    assert.deepEqual(usageWidgetLines(usage(), { color: false }), [
      "Switchyard enabled",
      "Routed to: gpt-5.4 via openai  -63% vs Claude Opus 5",
      "Ramp          █████████░░░░░░░░░░░░░░░ $0.42",
      "Claude Opus 5 ████████████████████████ $1.13",
    ])
  })

  it("omits invalid usage and fields Router did not state", () => {
    assert.equal(
      usageWidgetLines(usage({ requestCount: 0, spendUSD: 0 })),
      undefined,
    )
    assert.deepEqual(
      usageWidgetLines(
        usage({
          referenceModel: undefined,
          referenceCostUSD: undefined,
          lastModelProvider: undefined,
          switchyardEnabled: false,
        }),
        { color: false },
      ),
      [
        "Routed to: gpt-5.4",
        "Ramp ████████████████████████ $0.42",
      ],
    )
  })
})

describe("registerUsageWidget", () => {
  const emit = (handlers, event, context, details = {}) =>
    handlers.get(event)({ type: event, ...details }, context)

  function controlledTimer() {
    let nextID = 0
    const active = new Map()
    const schedule = mock.fn((callback, delayMs) => {
      const id = ++nextID
      active.set(id, { callback, delayMs })
      return id
    })
    const cancelScheduled = mock.fn((id) => active.delete(id))
    return {
      active,
      cancelScheduled,
      schedule,
      fireLatest() {
        const id = [...active.keys()].at(-1)
        assert.notEqual(id, undefined)
        const timer = active.get(id)
        active.delete(id)
        timer.callback()
      },
    }
  }

  function fixture(fetcher, options = {}) {
    const timer = controlledTimer()
    const handlers = new Map()
    const setWidget = mock.fn()
    let sessionID = "pi-session"
    const context = {
      mode: "tui",
      model: { provider: "ramp-router" },
      sessionManager: { getSessionId: () => sessionID },
      ui: { setWidget },
    }
    registerUsageWidget(
      {
        on: (event, handler) => handlers.set(event, handler),
      },
      {
        baseURL: "https://router-api.ramp.com/v1",
        resolveAPIKey: options.resolveAPIKey ?? (async () => "usage-secret"),
        fetch: fetcher,
        schedule: timer.schedule,
        cancelScheduled: timer.cancelScheduled,
      },
    )
    return {
      context,
      handlers,
      setSessionID: (value) => {
        sessionID = value
      },
      setWidget,
      timer,
    }
  }

  const flush = () => new Promise((resolve) => setImmediate(resolve))

  it("fetches on startup, then shows updating and re-arms one settled timer", async () => {
    const fetcher = mock.fn(async () => response())
    const { context, handlers, setWidget, timer } = fixture(fetcher)

    emit(handlers, "session_start", context, { reason: "resume" })
    await flush()
    assert.equal(fetcher.mock.callCount(), 1)
    assert.equal(
      setWidget.mock.calls.at(-1).arguments[1].some((line) =>
        line.includes("Routed to"),
      ),
      true,
    )

    emit(handlers, "agent_settled", context)
    assert.equal(fetcher.mock.callCount(), 1)
    assert.equal(timer.active.size, 1)
    assert.equal(timer.schedule.mock.calls.at(-1).arguments[1], 5_000)
    assert.deepEqual(setWidget.mock.calls.at(-1).arguments, [
      "ramp-router-usage",
      ["Updating Router usage..."],
    ])

    emit(handlers, "agent_settled", context)
    assert.equal(timer.active.size, 1)
    assert.equal(timer.cancelScheduled.mock.callCount(), 1)
    timer.fireLatest()
    await flush()
    assert.equal(fetcher.mock.callCount(), 2)
  })

  it("lets a settled event fence an in-flight startup response", async () => {
    let finishStartup
    let requestCount = 0
    const fetcher = mock.fn(() =>
      ++requestCount === 1
        ? new Promise((resolve) => {
            finishStartup = resolve
          })
        : Promise.resolve(response()),
    )
    const { context, handlers, setWidget, timer } = fixture(fetcher)

    emit(handlers, "session_start", context, { reason: "startup" })
    await flush()
    emit(handlers, "agent_settled", context)
    finishStartup(response({ spend_usd: 0.1 }))
    await flush()
    assert.deepEqual(setWidget.mock.calls.at(-1).arguments[1], [
      "Updating Router usage...",
    ])

    timer.fireLatest()
    await flush()
    assert.equal(fetcher.mock.callCount(), 2)
    assert.match(setWidget.mock.calls.at(-1).arguments[1].at(-2), /\$0\.42/)
  })

  it("cancels settled timers on model changes and shutdown", async () => {
    for (const event of ["model_select", "session_shutdown"]) {
      let finishSettled
      let requestCount = 0
      const fetcher = mock.fn(() =>
        ++requestCount === 1
          ? Promise.resolve(response())
          : new Promise((resolve) => {
              finishSettled = resolve
            }),
      )
      const { context, handlers, setWidget, timer } = fixture(fetcher)
      emit(handlers, "session_start", context, { reason: "startup" })
      await flush()
      emit(handlers, "agent_settled", context)
      timer.fireLatest()
      await flush()
      emit(handlers, "agent_settled", context)
      assert.equal(timer.active.size, 1)

      if (event === "model_select") context.model = { provider: "anthropic" }
      emit(handlers, event, context)
      assert.deepEqual(setWidget.mock.calls.at(-1).arguments, [
        "ramp-router-usage",
        undefined,
      ])
      const callsAfterClear = setWidget.mock.callCount()
      finishSettled(response({ spend_usd: 0.9 }))
      await flush()
      assert.equal(timer.active.size, 0)
      assert.equal(setWidget.mock.callCount(), callsAfterClear)
    }
  })

  it("fences old session responses and cancels their timer", async () => {
    const responses = []
    const fetcher = mock.fn(
      () =>
        new Promise((resolve) => {
          responses.push(resolve)
        }),
    )
    const { context, handlers, setSessionID, setWidget, timer } = fixture(fetcher)
    emit(handlers, "session_start", context, { reason: "startup" })
    await flush()
    emit(handlers, "agent_settled", context)

    setSessionID("next-session")
    emit(handlers, "session_start", context, { reason: "switch" })
    await flush()
    assert.equal(timer.active.size, 0)
    assert.equal(fetcher.mock.callCount(), 2)

    responses[1](response({ spend_usd: 0.2 }))
    await flush()
    const callsAfterCurrentResponse = setWidget.mock.callCount()
    responses[0](response({ spend_usd: 0.1 }))
    await flush()
    assert.equal(setWidget.mock.callCount(), callsAfterCurrentResponse)
    assert.match(setWidget.mock.calls.at(-1).arguments[1].at(-2), /\$0\.20/)
  })

  it("does not resolve credentials, schedule, or request usage offline", async () => {
    process.env.PI_OFFLINE = "1"
    const resolveAPIKey = mock.fn(async () => "usage-secret")
    const fetcher = mock.fn(async () => response())
    const { context, handlers, setWidget, timer } = fixture(fetcher, {
      resolveAPIKey,
    })
    emit(handlers, "session_start", context, { reason: "startup" })
    emit(handlers, "agent_settled", context)
    await flush()

    assert.equal(resolveAPIKey.mock.callCount(), 0)
    assert.equal(fetcher.mock.callCount(), 0)
    assert.equal(timer.active.size, 0)
    assert.deepEqual(setWidget.mock.calls.at(-1).arguments, [
      "ramp-router-usage",
      undefined,
    ])
  })
})
