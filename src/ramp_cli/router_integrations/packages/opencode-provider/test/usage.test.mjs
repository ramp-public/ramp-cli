import assert from "node:assert/strict"
import { afterEach, describe, it, mock } from "node:test"

import {
  fetchSessionUsage,
  formatUSD,
  referenceModelLabel,
  usageOriginFromBaseURL,
} from "../src/usage.ts"
import { sidebarUsageView } from "../src/sidebar-view.ts"
import { resolveAPIKey, resolveUsageOrigin } from "../src/options.ts"

function usagePayload(overrides = {}) {
  return {
    session: {
      client_session_id: "ses_123",
      first_event_at: "2026-08-15T00:00:00Z",
      last_event_at: "2026-08-15T00:05:00Z",
      request_count: 4,
      total_tokens: 12000,
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

afterEach(() => {
  mock.restoreAll()
  delete process.env.RAMP_ROUTER_API_KEY
  delete process.env.RAMP_ROUTER_BASE_URL
  delete process.env.RAMP_ROUTER_USAGE_BASE_URL
  delete process.env.LLM_GATEWAY_API_KEY
  delete process.env.LLM_GATEWAY_BASE_URL
})

describe("usageOriginFromBaseURL", () => {
  it("pairs the production data plane with the production dashboard", () => {
    assert.equal(
      usageOriginFromBaseURL("https://router-api.ramp.com/v1"),
      "https://app.router.com",
    )
    assert.equal(
      usageOriginFromBaseURL("https://router-api.ramp.com"),
      "https://app.router.com",
    )
  })

  it("treats an override as a single-origin deployment", () => {
    assert.equal(
      usageOriginFromBaseURL("http://localhost:8002/v1/"),
      "http://localhost:8002",
    )
  })

  it("falls back to the production dashboard for an unparseable URL", () => {
    assert.equal(usageOriginFromBaseURL("not a url"), "https://app.router.com")
  })
})

describe("resolveUsageOrigin", () => {
  it("prefers the recorded option, then the environment, then derivation", () => {
    assert.equal(
      resolveUsageOrigin({ usageBaseURL: "https://dashboard.example/" }),
      "https://dashboard.example",
    )
    process.env.RAMP_ROUTER_USAGE_BASE_URL = "https://env.example"
    assert.equal(resolveUsageOrigin({}), "https://env.example")
    delete process.env.RAMP_ROUTER_USAGE_BASE_URL
    assert.equal(
      resolveUsageOrigin({ baseURL: "http://localhost:8002/v1" }),
      "http://localhost:8002",
    )
    assert.equal(resolveUsageOrigin({}), "https://app.router.com")
  })
})

describe("resolveAPIKey", () => {
  it("prefers the inline key and falls back through the environments", () => {
    process.env.RAMP_ROUTER_API_KEY = "env-key"
    assert.equal(resolveAPIKey({ apiKey: "inline-key" }), "inline-key")
    assert.equal(resolveAPIKey({}), "env-key")
    delete process.env.RAMP_ROUTER_API_KEY
    process.env.LLM_GATEWAY_API_KEY = "legacy-key"
    assert.equal(resolveAPIKey({}), "legacy-key")
    delete process.env.LLM_GATEWAY_API_KEY
    assert.equal(resolveAPIKey({}), undefined)
  })
})

describe("fetchSessionUsage", () => {
  it("queries the session-usage endpoint with the credential", async () => {
    const fetcher = mock.fn(async () =>
      new Response(JSON.stringify(usagePayload()), { status: 200 }),
    )

    const fetched = await fetchSessionUsage({
      usageOrigin: "https://app.router.com",
      apiKey: "usage-secret",
      sessionID: "ses_123",
      fetch: fetcher,
    })

    assert.deepEqual(fetched, {
      requestCount: 4,
      spendUSD: 0.42,
      referenceModel: "claude-opus-5",
      referenceCostUSD: 1.13,
      lastModel: "gpt-5.4",
      lastModelProvider: "openai",
      switchyardEnabled: true,
    })
    assert.equal(fetcher.mock.callCount(), 1)
    assert.equal(
      fetcher.mock.calls[0].arguments[0],
      "https://app.router.com/session-usage/usage/session" +
        "?client_session_id=ses_123" +
        "&include_switchyard_routing_enabled=true" +
        "&include_last_model=true",
    )
    assert.deepEqual(fetcher.mock.calls[0].arguments[1].headers, {
      authorization: "Bearer usage-secret",
    })
  })

  it("keeps last_model when Router recorded no provider attribution", async () => {
    // last_model_provider is independently optional and never returned
    // without last_model.
    const fetcher = mock.fn(async () =>
      new Response(
        JSON.stringify(usagePayload({ last_model_provider: undefined })),
        { status: 200 },
      ),
    )

    const fetched = await fetchSessionUsage({
      usageOrigin: "https://app.router.com",
      apiKey: "usage-secret",
      sessionID: "ses_123",
      fetch: fetcher,
    })

    assert.equal(fetched.lastModel, "gpt-5.4")
    assert.equal(fetched.lastModelProvider, undefined)
  })

  it("tolerates a session without last-model or reference fields", async () => {
    const fetcher = mock.fn(async () =>
      new Response(
        JSON.stringify({
          session: { request_count: 1, spend_usd: 0.05 },
        }),
        { status: 200 },
      ),
    )

    assert.deepEqual(
      await fetchSessionUsage({
        usageOrigin: "https://app.router.com",
        apiKey: "usage-secret",
        sessionID: "ses_123",
        fetch: fetcher,
      }),
      { requestCount: 1, spendUSD: 0.05, switchyardEnabled: false },
    )
  })

  it("reports nothing for HTTP failures, bad JSON, and foreign shapes", async () => {
    for (const respond of [
      async () => new Response("denied", { status: 401 }),
      async () => new Response("not json", { status: 200 }),
      async () => new Response(JSON.stringify({ unrelated: true }), { status: 200 }),
      async () => {
        throw new Error("network down")
      },
    ]) {
      assert.equal(
        await fetchSessionUsage({
          usageOrigin: "https://app.router.com",
          apiKey: "usage-secret",
          sessionID: "ses_123",
          fetch: mock.fn(respond),
        }),
        undefined,
      )
    }
  })
})

describe("formatUSD", () => {
  it("renders two decimals exactly as the Claude Code status line", () => {
    assert.equal(formatUSD(0.42), "$0.42")
    assert.equal(formatUSD(0.005), "$0.01")
    assert.equal(formatUSD(0), "$0.00")
  })
})

describe("referenceModelLabel", () => {
  it("reads a Router model name the way Claude Code shows it", () => {
    assert.equal(referenceModelLabel("claude-opus-5"), "Claude Opus 5")
    assert.equal(referenceModelLabel("gpt-5.4"), "Gpt 5.4")
  })
})

describe("sidebarUsageView", () => {
  it("renders the Claude Code status line shape", () => {
    // 30 columns leave 10 bar cells after the 13-column label column, the
    // 5-column costs, and the two separating spaces.
    const view = sidebarUsageView(usage(), { availableWidth: 30 })

    assert.equal(view.switchyardEnabled, true)
    assert.equal(view.routedTo, "Routed to: gpt-5.4 via openai")
    assert.equal(view.delta, "-63% vs Claude Opus 5")
    // Both bars span the same fixed width, scaled to the larger figure, with
    // labels padded so both start at the same column.
    assert.deepEqual(view.bars, [
      {
        kind: "ramp",
        label: "Ramp         ",
        filled: 4,
        empty: 6,
        cost: "$0.42",
      },
      {
        kind: "reference",
        label: "Claude Opus 5",
        filled: 10,
        empty: 0,
        cost: "$1.13",
      },
    ])
  })

  it("fits the whole bar row inside the sidebar's default content width", () => {
    const view = sidebarUsageView(usage())

    for (const bar of view.bars) {
      // label + space + bar + space + cost
      const row = `${bar.label} ${"█".repeat(bar.filled)}${"░".repeat(bar.empty)} ${bar.cost}`
      assert.equal(row.length, 36)
    }
  })

  it("names the routed model with the display name when one is known", () => {
    const view = sidebarUsageView(usage(), {
      modelDisplayName: (modelID) =>
        modelID === "gpt-5.4" ? "GPT-5.4 via OpenAI" : undefined,
    })

    assert.equal(view.routedTo, "Routed to: GPT-5.4 via OpenAI")
  })

  it("drops the via suffix when Router recorded no provider attribution", () => {
    const view = sidebarUsageView(usage({ lastModelProvider: undefined }))

    assert.equal(view.routedTo, "Routed to: gpt-5.4")
  })

  it("caps the bars at Claude Code's width and never shrinks them below the minimum", () => {
    const wide = sidebarUsageView(usage(), { availableWidth: 200 })
    for (const bar of wide.bars) {
      assert.equal(bar.filled + bar.empty, 24)
    }

    const narrow = sidebarUsageView(usage(), { availableWidth: 12 })
    for (const bar of narrow.bars) {
      assert.equal(bar.filled + bar.empty, 10)
    }
  })

  it("pads a zero spend with an entirely empty bar", () => {
    // Costs "$0.00" and "$10.00" leave 10 cells of a 31-column row.
    const view = sidebarUsageView(
      usage({ spendUSD: 0, referenceCostUSD: 10 }),
      { availableWidth: 31 },
    )

    assert.equal(view.bars[0].filled, 0)
    assert.equal(view.bars[0].empty, 10)
    assert.equal(view.bars[0].cost, "$0.00")
  })

  it("marks a spend above the reference as an increase", () => {
    const view = sidebarUsageView(usage({ spendUSD: 2.26 }))
    assert.equal(view.delta, "+100% vs Claude Opus 5")
  })

  it("omits what Router did not state", () => {
    // "Ramp" and "$0.42" leave 10 cells of a 21-column row.
    const view = sidebarUsageView(
      usage({
        referenceModel: undefined,
        referenceCostUSD: undefined,
        lastModel: undefined,
        lastModelProvider: undefined,
        switchyardEnabled: false,
      }),
      { availableWidth: 21 },
    )

    assert.equal(view.switchyardEnabled, false)
    assert.equal(view.routedTo, undefined)
    assert.equal(view.delta, undefined)
    // With nothing to compare against, the lone spend is its own peak.
    assert.deepEqual(view.bars, [
      { kind: "ramp", label: "Ramp", filled: 10, empty: 0, cost: "$0.42" },
    ])
  })

  it("shows nothing for a session Router has not billed", () => {
    assert.equal(
      sidebarUsageView(usage({ requestCount: 0, spendUSD: 0 })),
      undefined,
    )
  })
})
