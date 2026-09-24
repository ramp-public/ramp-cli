import assert from "node:assert/strict"
import { describe, it, mock } from "node:test"

import { subscribeV1UsageEvents } from "../src/v1-usage-events.ts"

function harness() {
  const handlers = new Map()
  const disposers = []
  const onIdle = mock.fn()
  const api = {
    event: {
      on(type, handler) {
        handlers.set(type, handler)
        return () => handlers.delete(type)
      },
    },
    lifecycle: { onDispose: (dispose) => disposers.push(dispose) },
  }
  subscribeV1UsageEvents(api, "ramp-router", onIdle)
  return {
    emit: (type, properties) => handlers.get(type)?.({ type, properties }),
    dispose: () => disposers.forEach((dispose) => dispose()),
    handlers,
    onIdle,
  }
}

describe("OpenCode v1 sidebar completion events", () => {
  it("refreshes the first completed Router turn even without a session.idle event", () => {
    const h = harness()
    h.emit("message.updated", {
      sessionID: "first",
      info: { role: "assistant", providerID: "ramp-router", time: { completed: 100 } },
    })
    assert.deepEqual(h.onIdle.mock.calls.map((call) => call.arguments), [["first"]])
    h.dispose()
    assert.equal(h.handlers.size, 0)
  })

  it("ignores incomplete, non-Router and busy updates, but handles both idle signals", () => {
    const h = harness()
    h.emit("message.updated", { sessionID: "other", info: { role: "assistant", providerID: "other", time: { completed: 100 } } })
    h.emit("message.updated", { sessionID: "first", info: { role: "assistant", providerID: "ramp-router", time: {} } })
    h.emit("session.status", { sessionID: "first", status: { type: "busy" } })
    assert.equal(h.onIdle.mock.callCount(), 0)
    h.emit("session.status", { sessionID: "first", status: { type: "idle" } })
    h.emit("session.idle", { sessionID: "first" })
    assert.deepEqual(h.onIdle.mock.calls.map((call) => call.arguments), [["first"], ["first"]])
    h.dispose()
  })
})
