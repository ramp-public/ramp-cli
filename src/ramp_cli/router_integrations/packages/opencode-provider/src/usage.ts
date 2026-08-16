import { normalizeBaseURL } from "./discovery.ts"

/** The dashboard origin paired with the production data plane. */
const DEFAULT_USAGE_BASE_URL = "https://router.ramp.com"
const DEFAULT_DATA_PLANE_BASE_URL = "https://router-api.ramp.com/v1"
const DEFAULT_USAGE_TIMEOUT_MS = 3_000

export type SessionUsage = {
  requestCount: number
  spendUSD: number
  referenceModel?: string
  referenceCostUSD?: number
  /** The canonical Router name of the model the session last routed to. */
  lastModel?: string
  lastModelProvider?: string
  switchyardEnabled: boolean
}

/**
 * Map the data-plane base URL onto the dashboard origin serving usage.
 *
 * Mirrors the Ramp CLI's own derivation for the Claude Code status line: the
 * production data plane pairs with the production dashboard, while a base-URL
 * override names a single-origin deployment where the same host serves both.
 */
export function usageOriginFromBaseURL(baseURL: string): string {
  let normalized: string
  try {
    normalized = normalizeBaseURL(baseURL)
  } catch {
    return DEFAULT_USAGE_BASE_URL
  }
  if (normalized === DEFAULT_DATA_PLANE_BASE_URL) return DEFAULT_USAGE_BASE_URL
  return normalized.replace(/\/v1$/, "").replace(/\/+$/, "")
}

function usageNumber(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) && value >= 0
    ? value
    : undefined
}

function usageText(value: unknown): string | undefined {
  return typeof value === "string" && value ? value : undefined
}

/**
 * Fetch one session's Router usage, reporting nothing rather than failing.
 *
 * The status display is an extra: a Router that is down, a session Router has
 * not seen, or a payload this plugin does not understand must all read as "no
 * status" rather than an error surfaced into the middle of a chat.
 */
export async function fetchSessionUsage(input: {
  usageOrigin: string
  apiKey: string
  sessionID: string
  fetch?: typeof globalThis.fetch
  timeoutMs?: number
}): Promise<SessionUsage | undefined> {
  const fetcher = input.fetch ?? globalThis.fetch
  const origin = input.usageOrigin.replace(/\/+$/, "")
  const query = new URLSearchParams({
    client_session_id: input.sessionID,
    include_switchyard_routing_enabled: "true",
    include_last_model: "true",
  })
  try {
    const response = await fetcher(
      `${origin}/session-usage/usage/session?${query}`,
      {
        headers: { authorization: `Bearer ${input.apiKey}` },
        signal: AbortSignal.timeout(input.timeoutMs ?? DEFAULT_USAGE_TIMEOUT_MS),
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
    const switchyardEnabled =
      (payload as { switchyard_routing_enabled?: unknown })
        .switchyard_routing_enabled === true
    return {
      requestCount,
      spendUSD,
      ...(referenceModel !== undefined ? { referenceModel } : {}),
      ...(referenceCostUSD !== undefined ? { referenceCostUSD } : {}),
      ...(lastModel !== undefined ? { lastModel } : {}),
      ...(lastModelProvider !== undefined ? { lastModelProvider } : {}),
      switchyardEnabled,
    }
  } catch {
    return undefined
  }
}

export function formatUSD(value: number): string {
  // Two decimals, exactly as the Claude Code status line renders totals.
  return `$${value.toFixed(2)}`
}

/** "claude-opus-5" reads as "Claude Opus 5", matching the Claude status line. */
export function referenceModelLabel(identifier: string): string {
  return identifier
    .split("-")
    .filter((part) => part.length > 0)
    .map((part) => (/^\d/.test(part) ? part : part[0]!.toUpperCase() + part.slice(1)))
    .join(" ")
}
