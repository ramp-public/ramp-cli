import assert from "node:assert/strict"
import { describe, it, mock } from "node:test"

import { createUsageTracker } from "../src/usage-tracker.ts"

const usage = (requestCount = 1) => ({
  requestCount,
  spendUSD: 0.03,
  lastModel: "gpt-5.6-sol",
  lastModelProvider: "openai",
  switchyardEnabled: false,
})

function harness({ routerSession = () => true, syncSession, responses = [usage()] } = {}) {
  const pending = new Map()
  const delays = []
  let clock = 10_000
  let sequence = 0
  const fetchUsage = mock.fn(async () => responses.shift())
  const tracker = createUsageTracker(
    {
      options: { apiKey: "test-key", usageBaseURL: "https://router.example", rampCliVersion: "0.2.41" },
      modelDisplayName: (id) => id,
      isRouterSession: routerSession,
      ...(syncSession ? { syncSession } : {}),
    },
    {
      fetchUsage,
      now: () => clock,
      setTimer: (callback, delay) => {
        const id = ++sequence
        pending.set(id, callback)
        delays.push(delay)
        return id
      },
      clearTimer: (id) => pending.delete(id),
    },
  )
  const settle = () => new Promise((resolve) => setImmediate(resolve))
  return {
    tracker,
    fetchUsage,
    pending,
    delays,
    settle,
    elapse: (elapsed) => { clock += elapsed },
    advance: async (elapsed = 1_000) => {
      assert.equal(pending.size, 1)
      clock += elapsed
      const [id, callback] = pending.entries().next().value
      pending.delete(id)
      callback()
      await settle()
    },
  }
}

describe("OpenCode sidebar usage tracker", () => {
  it("synchronizes v2 messages after idle when the assistant has not reached the local cache", async () => {
    let cached
    const syncSession = mock.fn(async () => { cached = true })
    const h = harness({ routerSession: () => cached, syncSession, responses: [usage(), usage(), usage()] })
    h.tracker.onIdle("current")
    await h.settle()
    assert.equal(syncSession.mock.callCount(), 1)
    assert.equal(h.fetchUsage.mock.callCount(), 1)
    assert.equal(h.tracker.view("current"), undefined)
    await h.advance()
    await h.advance(4_000)
    assert.ok(h.tracker.view("current"))
    h.tracker.dispose()
  })

  it("retries a missing assistant message after idle without querying non-Router sessions", async () => {
    let belongsToRouter
    const h = harness({ routerSession: () => belongsToRouter, responses: [undefined, usage()] })
    h.tracker.onIdle("current")
    await h.settle()
    assert.equal(h.fetchUsage.mock.callCount(), 0)

    belongsToRouter = true
    await h.advance()
    assert.equal(h.fetchUsage.mock.callCount(), 1)
    assert.equal(h.tracker.view("current"), undefined)
    await h.advance(4_000)
    assert.equal(h.fetchUsage.mock.callCount(), 2)
    assert.ok(h.tracker.view("current"))
    assert.deepEqual(h.delays, [1_000, 4_000])
    assert.equal(h.pending.size, 0)
    h.tracker.dispose()
  })

  it("recovers from an empty usage response even when the sidebar's five-second gate just ran", async () => {
    const h = harness({ responses: [undefined, undefined, usage(), usage()] })
    await h.tracker.refresh("current") // first paint, before Router ingests the turn
    h.tracker.onIdle("current") // arrives inside the 5s gate
    await h.settle()
    assert.equal(h.fetchUsage.mock.callCount(), 2)
    assert.equal(h.tracker.view("current"), undefined)

    await h.advance()
    assert.equal(h.fetchUsage.mock.callCount(), 3)
    assert.equal(h.tracker.view("current"), undefined)
    await h.advance(4_000)
    assert.ok(h.tracker.view("current"))
    assert.equal(h.pending.size, 0)
    h.tracker.dispose()
  })

  it("loads existing sessions on first paint and keeps their last known value during ingestion lag", async () => {
    const h = harness({ responses: [usage(), undefined, usage(2), usage(2)] })
    await h.tracker.refresh("existing")
    assert.ok(h.tracker.view("existing"))
    await h.tracker.refresh("existing")
    assert.equal(h.fetchUsage.mock.callCount(), 1)

    h.tracker.onIdle("existing")
    await h.settle()
    assert.equal(h.fetchUsage.mock.callCount(), 2)
    assert.ok(h.tracker.view("existing"))
    await h.advance()
    assert.equal(h.fetchUsage.mock.callCount(), 3)
    assert.equal(h.pending.size, 1)
    await h.advance(4_000)
    assert.equal(h.fetchUsage.mock.callCount(), 4)
    assert.equal(h.pending.size, 0)
    h.tracker.dispose()
  })

  it("waits for later requests in a multi-request turn before publishing usage", async () => {
    const old = usage(3)
    const partial = { ...usage(4), spendUSD: 0.04, referenceModel: "gpt-5.6-sol" }
    const complete = {
      ...usage(5),
      spendUSD: 0.09,
      referenceModel: "deepseek-v4.1-flash",
      referenceCostUSD: 0.27,
    }
    const h = harness({ responses: [old, partial, partial, complete] })
    await h.tracker.refresh("current")
    h.tracker.onIdle("current")
    await h.settle()
    assert.equal(h.tracker.view("current").bars[0].cost, "$0.03")
    await h.advance()
    assert.equal(h.tracker.view("current").bars[0].cost, "$0.03")
    h.elapse(5_000)
    await h.tracker.refresh("current") // a sidebar render cannot publish the partial result
    assert.equal(h.fetchUsage.mock.callCount(), 3)
    assert.equal(h.tracker.view("current").bars[0].cost, "$0.03")
    await h.advance(4_000)
    assert.equal(h.tracker.view("current").bars[0].cost, "$0.09")
    assert.equal(h.tracker.view("current").delta, "-67% vs Deepseek V4.1 Flash")
    assert.deepEqual(h.delays, [1_000, 4_000])
    h.tracker.dispose()
  })

  it("retries a valid but unchanged usage response until the new turn is ingested", async () => {
    const previous = usage(3)
    const current = { ...usage(4), spendUSD: 0.08 }
    const h = harness({ responses: [previous, previous, previous, current] })
    await h.tracker.refresh("current")
    h.tracker.onIdle("current")
    await h.settle()
    assert.equal(h.fetchUsage.mock.callCount(), 2)
    assert.equal(h.tracker.view("current").bars[0].cost, "$0.03")

    await h.advance()
    assert.equal(h.tracker.view("current").bars[0].cost, "$0.03")
    await h.advance(4_000)
    assert.equal(h.tracker.view("current").bars[0].cost, "$0.08")
    assert.deepEqual(h.delays, [1_000, 4_000])
    assert.equal(h.pending.size, 0)
    h.tracker.dispose()
  })

  it("retains the last known totals if ingestion is still behind after the retry window", async () => {
    const h = harness({ responses: [usage(3), usage(3), usage(3), usage(3)] })
    await h.tracker.refresh("current")
    h.tracker.onIdle("current")
    await h.settle()
    await h.advance()
    await h.advance(4_000)
    assert.equal(h.fetchUsage.mock.callCount(), 4)
    assert.equal(h.tracker.view("current").bars[0].cost, "$0.03")
    assert.equal(h.pending.size, 0)
    h.tracker.dispose()
  })

  it("syncs a cached non-Router assistant only once and never queries Router", async () => {
    const syncSession = mock.fn(async () => {})
    const h = harness({ routerSession: () => false, syncSession })
    h.tracker.onIdle("other")
    await h.settle()
    assert.equal(h.fetchUsage.mock.callCount(), 0)
    assert.equal(syncSession.mock.callCount(), 1)
    assert.equal(h.pending.size, 0)
    h.tracker.dispose()
  })

  it("does not mistake the previous non-Router assistant for the current Router turn", async () => {
    let routerSession = false
    const syncSession = mock.fn(async () => { routerSession = true })
    const h = harness({ routerSession: () => routerSession, syncSession, responses: [usage(), usage(), usage()] })
    h.tracker.onIdle("current")
    await h.settle()
    assert.equal(syncSession.mock.callCount(), 1)
    assert.equal(h.fetchUsage.mock.callCount(), 1)
    assert.equal(h.tracker.view("current"), undefined)
    await h.advance()
    await h.advance(4_000)
    assert.ok(h.tracker.view("current"))
    h.tracker.dispose()
  })

  it("stops after one sync confirms another provider, and cancels unknown-session timers on disposal", async () => {
    let routerSession
    let confirmOther = true
    const syncSession = mock.fn(async () => { if (confirmOther) routerSession = false })
    const h = harness({ routerSession: () => routerSession, syncSession })
    h.tracker.onIdle("other")
    await h.settle()
    assert.equal(syncSession.mock.callCount(), 1)
    assert.equal(h.fetchUsage.mock.callCount(), 0)
    assert.equal(h.pending.size, 0)

    routerSession = undefined
    confirmOther = false
    h.tracker.onIdle("unknown")
    await h.settle()
    assert.equal(h.pending.size, 1)
    h.tracker.dispose()
    assert.equal(h.pending.size, 0)
    await h.tracker.refresh("other")
    assert.equal(h.fetchUsage.mock.callCount(), 0)
  })

  it("keeps prior usage during ingestion retries then clears it when all lookups fail", async () => {
    const h = harness({ responses: [usage(), undefined, undefined, undefined, usage(2), usage(2), usage(2)] })
    await h.tracker.refresh("current")
    assert.ok(h.tracker.view("current"))
    h.tracker.onIdle("current")
    await h.settle()
    assert.ok(h.tracker.view("current"))
    await h.advance()
    assert.ok(h.tracker.view("current"))
    await h.advance(4_000)
    assert.equal(h.tracker.view("current"), undefined)
    assert.equal(h.pending.size, 0)

    h.tracker.onIdle("current")
    await h.settle()
    assert.equal(h.tracker.view("current"), undefined)
    await h.advance()
    await h.advance(4_000)
    assert.ok(h.tracker.view("current"))
    h.tracker.dispose()
  })

  it("clears stale usage after a failed direct lookup without an active retry window", async () => {
    const h = harness({ responses: [usage(), undefined] })
    await h.tracker.refresh("existing")
    assert.ok(h.tracker.view("existing"))
    h.elapse(5_000)
    await h.tracker.refresh("existing")
    assert.equal(h.tracker.view("existing"), undefined)
    assert.equal(h.fetchUsage.mock.callCount(), 2)
    h.tracker.dispose()
  })

  it("drops a prior Router value if the latest assistant is from another provider", async () => {
    let routerSession = true
    const h = harness({ routerSession: () => routerSession })
    await h.tracker.refresh("existing")
    assert.ok(h.tracker.view("existing"))
    routerSession = false
    h.tracker.onIdle("existing")
    await h.settle()
    assert.equal(h.tracker.view("existing"), undefined)
    assert.equal(h.fetchUsage.mock.callCount(), 1)
    assert.equal(h.pending.size, 0)
    h.tracker.dispose()
  })

  it("restarts the bounded retry window when another idle event arrives", async () => {
    const h = harness({ responses: [undefined, undefined, usage(), usage()] })
    h.tracker.onIdle("current")
    await h.settle()
    assert.equal(h.pending.size, 1)
    h.tracker.onIdle("current")
    await h.settle()
    assert.equal(h.pending.size, 1)
    await h.advance()
    assert.equal(h.fetchUsage.mock.callCount(), 3)
    assert.equal(h.tracker.view("current"), undefined)
    await h.advance(4_000)
    assert.equal(h.fetchUsage.mock.callCount(), 4)
    assert.ok(h.tracker.view("current"))
    assert.equal(h.pending.size, 0)
    h.tracker.dispose()
  })
})
