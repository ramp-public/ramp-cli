export type RouterEffort = {
  value: string
  description: string
}

export type RouterPricing = {
  /** US dollars per million tokens, as published by Router. */
  input: number
  output: number
  cacheRead: number
  cacheWrite: number
}

export type RouterModelMetadata = {
  schemaVersion: number
  displayName: string
  /** The provider's public label. "vertex" is shown as "Google Agent
   * Platform", so this cannot be derived from the id. */
  providerDisplayName: string
  description?: string
  /** Router's stable presentation order. Not a quality ranking. */
  listingOrder: number
  /** Router states a window for every model it serves. */
  contextWindow: number
  maxOutputTokens: number
  inputModalities: string[]
  outputModalities: string[]
  toolCalls?: boolean
  reasoningEfforts: RouterEffort[]
  defaultEffort?: string
  reasoningSummaryValues: string[]
  reasoningInclude: string[]
  pricing?: RouterPricing
  /** "active", "deprecated" or "retired" as Router records it. */
  status?: string
  temperature?: boolean
  /** Release date as YYYY-MM-DD, from the model object's created timestamp. */
  releaseDate?: string
}

export type RouterModel = {
  id: string
  ownedBy?: string
  /**
   * Router describes every model it serves, so this is always present.
   * Guessing a model's efforts or limits from the shape of its name produced
   * answers that were confidently wrong, and refusing to guess is the point.
   */
  metadata: RouterModelMetadata
}

type OpenAIModel = {
  id?: unknown
  owned_by?: unknown
  created?: unknown
  router?: unknown
}

type OpenAIModelList = {
  data?: unknown
}

const DEFAULT_DISCOVERY_TIMEOUT_MS = 10_000

/** The metadata shape this client understands. */
const SUPPORTED_SCHEMA_VERSION = 1

export function normalizeBaseURL(value: string): string {
  const url = new URL(value)
  url.hash = ""
  url.search = ""
  url.pathname = url.pathname.replace(/\/+$/, "")
  if (!url.pathname.endsWith("/v1")) {
    url.pathname = `${url.pathname}/v1`.replace(/\/+/g, "/")
  }
  return url.toString().replace(/\/$/, "")
}

function record(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null
    ? (value as Record<string, unknown>)
    : {}
}

function text(value: unknown): string | undefined {
  return typeof value === "string" && value.length > 0 ? value : undefined
}

function count(value: unknown): number | undefined {
  return typeof value === "number" && Number.isSafeInteger(value) && value > 0
    ? value
    : undefined
}

function flag(value: unknown): boolean | undefined {
  return typeof value === "boolean" ? value : undefined
}

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : []
}

function rate(value: unknown): number | undefined {
  if (typeof value !== "string" || value.length === 0) return undefined
  // Router publishes exact decimal strings, including ratios such as "5/4",
  // so that no rate is rounded before a client chooses how to show it.
  const [numerator, denominator] = value.split("/")
  const parsed =
    denominator === undefined ? Number(numerator) : Number(numerator) / Number(denominator)
  return Number.isFinite(parsed) ? parsed : undefined
}

function isoDate(value: unknown): string | undefined {
  // The model object carries a Unix timestamp; clients want a calendar date.
  if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) return undefined
  return new Date(value * 1000).toISOString().slice(0, 10)
}

function pricing(value: unknown): RouterPricing | undefined {
  const rates = record(value)
  const input = rate(rates.input)
  const output = rate(rates.output)
  if (input === undefined || output === undefined) return undefined
  return {
    input,
    output,
    cacheRead: rate(rates.cache_read_input) ?? 0,
    cacheWrite: rate(rates.cache_write_input) ?? 0,
  }
}

function efforts(value: unknown): RouterEffort[] {
  if (!Array.isArray(value)) return []
  const parsed: RouterEffort[] = []
  for (const raw of value) {
    const entry = record(raw)
    const effort = text(entry.value)
    if (effort) {
      parsed.push({ value: effort, description: text(entry.description) ?? "" })
    }
  }
  return parsed
}

/**
 * Read Router's description of one model.
 *
 * A missing or unrecognized schema is an error rather than a cue to fall back.
 * Router publishes this for everything it serves, so its absence means the two
 * are out of step, and configuring an agent from guesses would hide that until
 * a request failed.
 */
export function parseRouterMetadata(
  value: unknown,
  created: unknown,
  identifier: string,
): RouterModelMetadata {
  const router = record(value)
  if (router.schema_version !== SUPPORTED_SCHEMA_VERSION) {
    throw new Error(
      `Ramp Router described ${identifier} in a format this plugin does not ` +
        `understand. Update the Ramp CLI and restart.`,
    )
  }

  const limits = record(router.limits)
  const capabilities = record(router.capabilities)
  const modalities = record(capabilities.modalities)
  const tools = record(capabilities.tools)
  const reasoning = record(capabilities.reasoning)
  const summary = record(reasoning.summary)
  const continuation = record(reasoning.continuation)

  const description = text(router.description)
  const contextWindow = count(limits.context_window)
  const maxOutputTokens = count(limits.max_output_tokens)
  if (contextWindow === undefined || maxOutputTokens === undefined) {
    // A guessed window makes a client truncate or over-promise against a
    // boundary the model does not have, so an unstated one is an error.
    throw new Error(`Ramp Router did not state token limits for ${identifier}`)
  }
  const toolCalls = flag(tools.supported)
  const defaultEffort = text(reasoning.default_effort)
  const rates = pricing(router.pricing)
  const status = text(router.status)
  const temperature = flag(capabilities.temperature)
  const releaseDate = isoDate(created)

  return {
    schemaVersion: SUPPORTED_SCHEMA_VERSION,
    displayName: text(router.display_name) ?? "",
    providerDisplayName: text(router.provider_display_name) ?? "",
    listingOrder: count(record(router.listing).order) ?? Number.MAX_SAFE_INTEGER,
    inputModalities: strings(modalities.input),
    outputModalities: strings(modalities.output),
    reasoningEfforts: efforts(reasoning.efforts),
    reasoningSummaryValues: strings(summary.values),
    reasoningInclude: strings(continuation.request_include),
    contextWindow,
    maxOutputTokens,
    ...(description !== undefined ? { description } : {}),
    ...(toolCalls !== undefined ? { toolCalls } : {}),
    ...(defaultEffort !== undefined ? { defaultEffort } : {}),
    ...(rates !== undefined ? { pricing: rates } : {}),
    ...(status !== undefined ? { status } : {}),
    ...(temperature !== undefined ? { temperature } : {}),
    ...(releaseDate !== undefined ? { releaseDate } : {}),
  }
}

export async function discoverRouterModels(input: {
  baseURL: string
  apiKey: string
  fetch?: typeof globalThis.fetch
  timeoutMs?: number
  signal?: AbortSignal
}): Promise<RouterModel[]> {
  const fetcher = input.fetch ?? globalThis.fetch
  const baseURL = normalizeBaseURL(input.baseURL)
  const timeoutSignal = AbortSignal.timeout(
    input.timeoutMs ?? DEFAULT_DISCOVERY_TIMEOUT_MS,
  )
  const response = await fetcher(`${baseURL}/models`, {
    headers: {
      authorization: `Bearer ${input.apiKey}`,
      // Production rejects an empty/default fetch user agent before Router
      // can return the catalog. This request is model discovery, not an
      // inference, so use the integration's own stable identifier.
      "user-agent": "ramp-cli-pi-provider",
    },
    signal: input.signal
      ? AbortSignal.any([input.signal, timeoutSignal])
      : timeoutSignal,
  })

  if (!response.ok) {
    throw new Error(`Ramp Router model discovery failed with HTTP ${response.status}`)
  }

  let payload: OpenAIModelList
  try {
    payload = (await response.json()) as OpenAIModelList
  } catch {
    throw new Error("Ramp Router model discovery returned invalid JSON")
  }
  if (!Array.isArray(payload.data)) {
    throw new Error("Ramp Router model discovery returned an invalid OpenAI model list")
  }

  const models = new Map<string, RouterModel>()
  for (const raw of payload.data as OpenAIModel[]) {
    const identifier = typeof raw?.id === "string" ? raw.id.trim() : ""
    if (identifier.length === 0) {
      throw new Error("Ramp Router model discovery returned a model without an id")
    }
    if (models.has(identifier)) {
      // Keep the first, matching the CLI, so both agree on which entry won.
      continue
    }
    models.set(identifier, {
      id: identifier,
      ...(typeof raw.owned_by === "string" && raw.owned_by.length > 0 ? { ownedBy: raw.owned_by } : {}),
      metadata: parseRouterMetadata(raw.router, raw.created, identifier),
    })
  }

  if (models.size === 0) {
    throw new Error("Ramp Router model discovery returned no models for this API key")
  }
  return [...models.values()]
}
