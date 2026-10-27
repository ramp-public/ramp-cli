import { normalizeBaseURL } from "./discovery.ts"
import { usageOriginFromBaseURL } from "./usage.ts"
import type { PluginOptions } from "@opencode-ai/plugin"

export const DEFAULT_PROVIDER_ID = "ramp-router"
export const DEFAULT_PROVIDER_NAME = "Ramp Router"
export const DEFAULT_API_KEY_ENV = "RAMP_ROUTER_API_KEY"
export const LEGACY_API_KEY_ENV = "LLM_GATEWAY_API_KEY"
export const DEFAULT_BASE_URL_ENV = "RAMP_ROUTER_BASE_URL"
export const LEGACY_BASE_URL_ENV = "LLM_GATEWAY_BASE_URL"
export const DEFAULT_BASE_URL = "https://api.router.com/v1"
export const USAGE_BASE_URL_ENV = "RAMP_ROUTER_USAGE_BASE_URL"

export type RouterPluginOptions = PluginOptions & {
  providerID?: string
  name?: string
  baseURL?: string
  usageBaseURL?: string
  apiKey?: string
  /** Direct dashboard credential when apiKey is a local agent token. */
  usageApiKey?: string
  apiKeyEnv?: string
  rampExecutable?: string
  rampCliVersion?: string
  /** "compact" (recommended models) or "all", as `ramp router configure` saved it. */
  modelView?: string
  /** The model `ramp router configure` chose for new sessions. */
  defaultModel?: string
}

export function nonEmpty(value: unknown, fallback: string): string {
  return typeof value === "string" && value.trim().length > 0 ? value.trim() : fallback
}

/** Read the tuple options the configure command wrote, tolerating none. */
export function routerPluginOptions(rawOptions: unknown): RouterPluginOptions {
  return (rawOptions ?? {}) as RouterPluginOptions
}

export function resolveProviderID(options: RouterPluginOptions): string {
  return nonEmpty(options.providerID, DEFAULT_PROVIDER_ID)
}

export function resolveProviderName(options: RouterPluginOptions): string {
  return nonEmpty(options.name, DEFAULT_PROVIDER_NAME)
}

/** The data-plane URL exactly as given, before /v1 normalization. */
export function rawBaseURL(options: RouterPluginOptions): string {
  return (
    options.baseURL ??
    process.env[DEFAULT_BASE_URL_ENV] ??
    process.env[LEGACY_BASE_URL_ENV] ??
    DEFAULT_BASE_URL
  )
}

export function resolveBaseURL(options: RouterPluginOptions): string {
  return normalizeBaseURL(rawBaseURL(options))
}

export function resolveAPIKey(options: RouterPluginOptions): string | undefined {
  const apiKeyEnv = nonEmpty(options.apiKeyEnv, DEFAULT_API_KEY_ENV)
  return (
    nonEmpty(options.apiKey, "") ||
    process.env[apiKeyEnv] ||
    process.env[LEGACY_API_KEY_ENV] ||
    undefined
  )
}

export function resolveUsageAPIKey(options: RouterPluginOptions): string | undefined {
  const key = resolveAPIKey(options)
  return key?.startsWith("ramp-local-")
    ? nonEmpty(options.usageApiKey, "") || key
    : key
}

/** The CLI version recorded at configure time, absent for a hand-written entry. */
export function resolveRampCliVersion(options: RouterPluginOptions): string | undefined {
  return nonEmpty(options.rampCliVersion, "") || undefined
}

/** Only the compact view changes the listing; "all" is Router's default. */
export function resolveModelView(options: RouterPluginOptions): "compact" | undefined {
  return options.modelView === "compact" ? "compact" : undefined
}

/** The configured starting model, absent for a hand-written entry. */
export function resolveDefaultModel(options: RouterPluginOptions): string | undefined {
  return nonEmpty(options.defaultModel, "") || undefined
}

/**
 * The dashboard origin serving the session-usage endpoint.
 *
 * The configure command records the origin it derived, and the environment
 * can name one for a run against another stack. Otherwise the origin is
 * derived from the data-plane URL exactly as configure does.
 */
export function resolveUsageOrigin(options: RouterPluginOptions): string {
  const configured =
    nonEmpty(options.usageBaseURL, "") ||
    nonEmpty(process.env[USAGE_BASE_URL_ENV], "")
  if (configured) return configured.replace(/\/+$/, "")
  return usageOriginFromBaseURL(rawBaseURL(options))
}
