import { createSignal } from "solid-js"

import { resolveAPIKey, resolveRampCliVersion, resolveUsageOrigin } from "./options.ts"
import type { RouterPluginOptions } from "./options.ts"
import { sidebarUsageView } from "./sidebar-view.ts"
import type { SidebarUsageView } from "./sidebar-view.ts"
import { fetchSessionUsage } from "./usage.ts"
import type { SessionUsage } from "./usage.ts"

const USAGE_FETCH_TIMEOUT_MS = 3_000
const USAGE_REFRESH_MIN_INTERVAL_MS = 5_000
// Allow TUI message state and Router ingestion to catch up after idle. These
// are delays between attempts, not a repeating background poll.
const IDLE_RETRY_DELAYS_MS = [1_000, 4_000]

export type UsageHost = {
  options: RouterPluginOptions
  modelDisplayName: (modelID: string) => string | undefined
  /** True for Router, false for another provider, undefined until an assistant arrives. */
  isRouterSession: (sessionID: string) => boolean | undefined
  /** V2's message cache can still be behind when the idle event arrives. */
  syncSession?: (sessionID: string) => Promise<void>
}

type Timer = ReturnType<typeof setTimeout>
type TrackerDeps = {
  fetchUsage?: typeof fetchSessionUsage
  setTimer?: (callback: () => void, delayMs: number) => Timer
  clearTimer?: (timer: Timer) => void
  now?: () => number
}

/** Usage is scoped to a session, including a bounded post-idle race recovery. */
export function createUsageTracker(host: UsageHost, deps: TrackerDeps = {}) {
  const { options } = host
  const rampCliVersion = resolveRampCliVersion(options)
  const [usages, setUsages] = createSignal<Record<string, SessionUsage>>({})
  const inFlight = new Set<string>()
  const lastFetched = new Map<string, number>()
  const timers = new Map<string, Timer>()
  const generations = new Map<string, number>()
  const activeRetries = new Set<string>()
  const fetchUsage = deps.fetchUsage ?? fetchSessionUsage
  const setTimer = deps.setTimer ?? setTimeout
  const clearTimer = deps.clearTimer ?? clearTimeout
  const now = deps.now ?? Date.now
  let disposed = false

  const clearUsage = (sessionID: string): void => {
    lastFetched.delete(sessionID)
    setUsages((previous) => {
      if (!(sessionID in previous)) return previous
      const next = { ...previous }
      delete next[sessionID]
      return next
    })
  }

  const refresh = async (
    sessionID: string,
    afterIdle = false,
    previousRequestCount?: number,
    publish = true,
  ): Promise<"found" | "retry" | "stale" | "settling" | "non-router"> => {
    if (disposed || !sessionID || inFlight.has(sessionID) || (!afterIdle && activeRetries.has(sessionID))) {
      return "retry"
    }
    inFlight.add(sessionID)
    try {
      // An idle event may beat the assistant in TUI state. Sync once if the
      // cached assistant is absent or from another provider: it may belong to
      // the previous turn. A confirmed non-Router result ends this retry chain.
      let routerSession = host.isRouterSession(sessionID)
      if (routerSession !== true && afterIdle && host.syncSession) {
        try {
          await host.syncSession(sessionID)
        } catch {
          return "retry"
        }
        if (disposed) return "retry"
        routerSession = host.isRouterSession(sessionID)
      }
      if (routerSession === false) {
        clearUsage(sessionID)
        return "non-router"
      }
      if (routerSession !== true) return "retry"
      const last = lastFetched.get(sessionID)
      if (!afterIdle && last !== undefined && now() - last < USAGE_REFRESH_MIN_INTERVAL_MS) {
        return "retry"
      }
      const apiKey = resolveAPIKey(options)
      if (!apiKey) {
        if (!activeRetries.has(sessionID)) clearUsage(sessionID)
        return "retry"
      }
      const usage = await fetchUsage({
        usageOrigin: resolveUsageOrigin(options),
        apiKey,
        sessionID,
        ...(rampCliVersion ? { rampCliVersion } : {}),
        timeoutMs: USAGE_FETCH_TIMEOUT_MS,
      })
      if (disposed) return "retry"
      lastFetched.set(sessionID, now())
      // Keep last-known usage only while the bounded post-idle retry is active.
      if (usage) {
        // A valid response can still be the previous turn's totals. A single
        // new request can also be only part of a multi-request turn: wait for
        // the bounded post-idle settling window before showing its aggregate.
        if (previousRequestCount !== undefined && usage.requestCount <= previousRequestCount) {
          return "stale"
        }
        if (!publish) return "settling"
        setUsages((previous) => ({ ...previous, [sessionID]: usage }))
        return "found"
      }
      if (!activeRetries.has(sessionID)) clearUsage(sessionID)
      return "retry"
    } catch {
      // Usage is an optional display; retry after idle without surfacing errors.
      if (!afterIdle && !activeRetries.has(sessionID)) clearUsage(sessionID)
      return "retry"
    } finally {
      inFlight.delete(sessionID)
    }
  }

  const retryAfterIdle = async (
    sessionID: string,
    attempt: number,
    generation: number,
    previousRequestCount?: number,
  ): Promise<void> => {
    // Router does not expose a turn-ingestion completion marker. Sample
    // through the entire bounded window even on the first turn, rather than
    // treating the first observed request as the complete turn.
    const result = await refresh(
      sessionID,
      true,
      previousRequestCount,
      attempt >= IDLE_RETRY_DELAYS_MS.length,
    )
    if (disposed || generations.get(sessionID) !== generation) return
    if ((result !== "retry" && result !== "stale" && result !== "settling") || attempt >= IDLE_RETRY_DELAYS_MS.length) {
      activeRetries.delete(sessionID)
      if (result === "retry") clearUsage(sessionID)
      return
    }
    const timer = setTimer(() => {
      timers.delete(sessionID)
      void retryAfterIdle(sessionID, attempt + 1, generation, previousRequestCount)
    }, IDLE_RETRY_DELAYS_MS[attempt]!)
    timers.set(sessionID, timer)
  }

  const onIdle = (sessionID: string): void => {
    if (disposed || !sessionID) return
    const timer = timers.get(sessionID)
    if (timer) clearTimer(timer)
    timers.delete(sessionID)
    const generation = (generations.get(sessionID) ?? 0) + 1
    generations.set(sessionID, generation)
    activeRetries.add(sessionID)
    void retryAfterIdle(sessionID, 0, generation, usages()[sessionID]?.requestCount)
  }

  const view = (sessionID: string): SidebarUsageView | undefined => {
    const usage = usages()[sessionID]
    return usage ? sidebarUsageView(usage, { modelDisplayName: host.modelDisplayName }) : undefined
  }

  const dispose = (): void => {
    disposed = true
    for (const timer of timers.values()) clearTimer(timer)
    timers.clear()
    generations.clear()
    activeRetries.clear()
  }

  return { refresh, onIdle, view, dispose }
}
