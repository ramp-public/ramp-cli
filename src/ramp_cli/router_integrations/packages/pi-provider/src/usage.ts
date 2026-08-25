import type {
  ExtensionAPI,
  ExtensionContext,
} from "@earendil-works/pi-coding-agent"

const DEFAULT_USAGE_BASE_URL = "https://app.router.com"
const DEFAULT_DATA_PLANE_BASE_URL = "https://router-api.ramp.com/v1"
const USAGE_FETCH_TIMEOUT_MS = 3_000
const USAGE_REFRESH_MIN_INTERVAL_MS = 5_000
const WIDGET_KEY = "ramp-router-usage"
const BAR_WIDTH = 24

const NVIDIA_GREEN = "\u001b[38;2;118;185;0m"
const RAMP_YELLOW = "\u001b[38;2;228;242;34m"
const ANTHROPIC_TERRACOTTA = "\u001b[38;2;217;119;87m"
const MUTED = "\u001b[2m"
const BOLD = "\u001b[1m"
const RESET = "\u001b[0m"

export type SessionUsage = {
  requestCount: number
  spendUSD: number
  referenceModel?: string
  referenceCostUSD?: number
  lastModel?: string
  lastModelProvider?: string
  switchyardEnabled: boolean
}

export function usageOriginFromBaseURL(baseURL: string): string {
  const normalized = baseURL.replace(/\/+$/, "")
  if (
    normalized === DEFAULT_DATA_PLANE_BASE_URL ||
    normalized === DEFAULT_DATA_PLANE_BASE_URL.replace(/\/v1$/, "")
  ) {
    return DEFAULT_USAGE_BASE_URL
  }
  return normalized.replace(/\/v1$/, "")
}

function usageNumber(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) && value >= 0
    ? value
    : undefined
}

function usageText(value: unknown): string | undefined {
  if (typeof value !== "string") return undefined
  const safe = value.replace(/[\u0000-\u001f\u007f-\u009f]/g, "").trim()
  return safe ? [...safe].slice(0, 128).join("") : undefined
}

export async function fetchSessionUsage(input: {
  usageOrigin: string
  apiKey: string
  sessionID: string
  fetch?: typeof globalThis.fetch
  timeoutMs?: number
}): Promise<SessionUsage | undefined> {
  const query = new URLSearchParams({
    client_session_id: input.sessionID,
    include_switchyard_routing_enabled: "true",
    include_last_model: "true",
  })
  try {
    const response = await (input.fetch ?? globalThis.fetch)(
      `${input.usageOrigin.replace(/\/+$/, "")}/session-usage/usage/session?${query}`,
      {
        headers: { authorization: `Bearer ${input.apiKey}` },
        signal: AbortSignal.timeout(input.timeoutMs ?? USAGE_FETCH_TIMEOUT_MS),
      },
    )
    if (!response.ok) return undefined
    const payload: unknown = await response.json()
    if (!payload || typeof payload !== "object") return undefined
    const session = (payload as { session?: unknown }).session
    if (!session || typeof session !== "object") return undefined
    const record = session as Record<string, unknown>
    const requestCount = usageNumber(record.request_count)
    const spendUSD = usageNumber(record.spend_usd)
    if (requestCount === undefined || spendUSD === undefined) return undefined
    const referenceModel = usageText(record.reference_model)
    const referenceCostUSD = usageNumber(record.reference_cost_usd)
    const lastModel = usageText(record.last_model)
    const lastModelProvider = usageText(record.last_model_provider)
    return {
      requestCount,
      spendUSD,
      ...(referenceModel !== undefined ? { referenceModel } : {}),
      ...(referenceCostUSD !== undefined ? { referenceCostUSD } : {}),
      ...(lastModel !== undefined ? { lastModel } : {}),
      ...(lastModelProvider !== undefined ? { lastModelProvider } : {}),
      switchyardEnabled:
        (payload as { switchyard_routing_enabled?: unknown })
          .switchyard_routing_enabled === true,
    }
  } catch {
    return undefined
  }
}

function formatUSD(value: number): string {
  return `$${value.toFixed(2)}`
}

function referenceModelLabel(identifier: string): string {
  return identifier
    .split("-")
    .filter(Boolean)
    .map((part) =>
      /^\d/.test(part) ? part : part[0]!.toUpperCase() + part.slice(1),
    )
    .join(" ")
}

function filledCells(value: number, peak: number): number {
  if (value <= 0 || peak <= 0) return 0
  return Math.min(BAR_WIDTH, Math.round((value / peak) * BAR_WIDTH))
}

function color(code: string, text: string, enabled: boolean): string {
  return enabled ? `${code}${text}${RESET}` : text
}

export function usageWidgetLines(
  usage: SessionUsage,
  options?: { color?: boolean },
): string[] | undefined {
  if (usage.requestCount <= 0) return undefined
  const colors = options?.color ?? true
  const compared =
    usage.referenceModel !== undefined &&
    usage.referenceCostUSD !== undefined &&
    usage.referenceCostUSD > 0
  const referenceLabel = compared
    ? referenceModelLabel(usage.referenceModel!)
    : undefined
  const labelWidth = Math.max("Ramp".length, referenceLabel?.length ?? 0)
  const peak = Math.max(usage.spendUSD, compared ? usage.referenceCostUSD! : 0)
  const lines: string[] = []
  let delta: string | undefined
  if (compared) {
    const percent = Math.round(
      ((usage.spendUSD - usage.referenceCostUSD!) / usage.referenceCostUSD!) *
        100,
    )
    delta = `${percent > 0 ? "+" : ""}${percent}% vs ${referenceLabel}`
  }

  if (usage.switchyardEnabled) {
    lines.push(color(NVIDIA_GREEN, "Switchyard enabled", colors))
  }
  const routed = usage.lastModel
    ? `Routed to: ${usage.lastModel}${
        usage.lastModelProvider ? ` via ${usage.lastModelProvider}` : ""
      }`
    : undefined
  if (routed || delta) {
    lines.push(
      [
        ...(routed ? [color(BOLD, routed, colors)] : []),
        ...(delta ? [color(RAMP_YELLOW, delta, colors)] : []),
      ].join("  "),
    )
  }

  const bar = (label: string, value: number, fill: string): string => {
    const filled = filledCells(value, peak)
    return `${label.padEnd(labelWidth)} ${color(fill, "█".repeat(filled), colors)}${color(MUTED, "░".repeat(BAR_WIDTH - filled), colors)} ${formatUSD(value)}`
  }
  lines.push(bar("Ramp", usage.spendUSD, RAMP_YELLOW))
  if (compared) {
    lines.push(
      bar(referenceLabel!, usage.referenceCostUSD!, ANTHROPIC_TERRACOTTA),
    )
  }
  return lines
}

export function registerUsageWidget(
  pi: ExtensionAPI,
  input: {
    baseURL: string
    resolveAPIKey: () => Promise<string | undefined>
    fetch?: typeof globalThis.fetch
    schedule?: (callback: () => void, delayMs: number) => unknown
    cancelScheduled?: (handle: unknown) => void
  },
): void {
  let generation = 0
  let settledTimer: unknown
  const schedule = input.schedule ?? ((callback, delayMs) =>
    setTimeout(callback, delayMs))
  const cancelScheduled = input.cancelScheduled ?? ((handle) =>
    clearTimeout(handle as ReturnType<typeof setTimeout>))

  const cancelSettledRefresh = (): void => {
    if (settledTimer === undefined) return
    cancelScheduled(settledTimer)
    settledTimer = undefined
  }

  const setWidget = (
    ctx: ExtensionContext,
    lines: string[] | undefined,
  ): void => {
    try {
      ctx.ui.setWidget(WIDGET_KEY, lines)
    } catch {
      // Older and non-interactive hosts keep running without the optional UI.
    }
  }

  const invalidate = (ctx: ExtensionContext): number => {
    generation += 1
    cancelSettledRefresh()
    setWidget(ctx, undefined)
    return generation
  }

  const activeSession = (ctx: ExtensionContext): string | undefined => {
    if (
      process.env.PI_OFFLINE !== undefined ||
      ctx.mode !== "tui" ||
      ctx.model?.provider !== "ramp-router"
    ) return undefined
    const sessionID = ctx.sessionManager.getSessionId()
    return typeof sessionID === "string" && sessionID ? sessionID : undefined
  }

  const fetchAndRender = async (
    ctx: ExtensionContext,
    requestGeneration: number,
  ): Promise<void> => {
    const sessionID = activeSession(ctx)
    if (!sessionID || generation !== requestGeneration) return
    try {
      const apiKey = await input.resolveAPIKey()
      if (
        !apiKey ||
        generation !== requestGeneration ||
        activeSession(ctx) !== sessionID
      ) return
      const usage = await fetchSessionUsage({
        usageOrigin: usageOriginFromBaseURL(input.baseURL),
        apiKey,
        sessionID,
        ...(input.fetch ? { fetch: input.fetch } : {}),
      })
      if (
        generation !== requestGeneration ||
        activeSession(ctx) !== sessionID
      ) return
      setWidget(ctx, usage ? usageWidgetLines(usage) : undefined)
    } catch {
      // Usage is best-effort and must never surface auth or network failures.
    }
  }

  const scheduleSettledRefresh = (ctx: ExtensionContext): void => {
    const sessionID = activeSession(ctx)
    const timerGeneration = invalidate(ctx)
    if (!sessionID) return
    setWidget(ctx, ["Updating Router usage..."])
    settledTimer = schedule(() => {
      settledTimer = undefined
      if (generation !== timerGeneration) return
      if (activeSession(ctx) !== sessionID) {
        invalidate(ctx)
        return
      }
      void fetchAndRender(ctx, timerGeneration)
    }, USAGE_REFRESH_MIN_INTERVAL_MS)
  }

  pi.on?.("session_start", (_event, ctx) => {
    const requestGeneration = invalidate(ctx)
    void fetchAndRender(ctx, requestGeneration)
  })
  pi.on?.("agent_settled", (_event, ctx) => {
    scheduleSettledRefresh(ctx)
  })
  pi.on?.("model_select", (_event, ctx) => {
    invalidate(ctx)
  })
  pi.on?.("session_shutdown", (_event, ctx) => {
    invalidate(ctx)
  })
}
