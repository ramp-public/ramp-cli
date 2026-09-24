import type { RouterModel } from "./discovery.ts"

/** The request settings one reasoning effort adds to a Router request. */
export type ReasoningEffortSettings = {
  reasoningEffort: string
  reasoningSummary?: string
  include?: string[]
}

export function supportsReasoning(model: RouterModel): boolean {
  return model.metadata.reasoningEfforts.length > 0
}

/**
 * One entry per effort the model actually accepts, in Router's order.
 *
 * Router reports these from the provider itself, which no naming convention
 * predicts: gpt-5-pro takes "high" alone, gpt-5.5-pro starts at "medium", and
 * gpt-5.6-sol takes six levels but refuses "minimal". Offering a level the
 * model rejects turns a menu entry into a failed request.
 *
 * Shared by the OpenCode v1 (variants object) and v2 (variants array)
 * mappings so both offer exactly the same menu.
 */
export function reasoningEffortSettings(
  model: RouterModel,
): Array<[effort: string, settings: ReasoningEffortSettings]> {
  const metadata = model.metadata
  if (metadata.reasoningEfforts.length === 0) return []
  // Only OpenAI reads a requested summary shape. xAI accepts the field and
  // discards it, and the providers Router translates for have no such
  // parameter, so asking them for one sends a setting nothing acts on.
  const summary = metadata.reasoningSummaryRequestable
    ? (metadata.reasoningSummaryValues.includes("auto")
        ? "auto"
        : metadata.reasoningSummaryValues[0])
    : undefined
  // Likewise the include list: only OpenAI needs the previous turn's
  // reasoning handed back, and it names what to send. Sending that to a
  // provider that did not ask is a request it will reject.
  const include = metadata.reasoningInclude
  return metadata.reasoningEfforts.map((effort) => [
    effort.value,
    {
      reasoningEffort: effort.value,
      ...(summary ? { reasoningSummary: summary } : {}),
      ...(include.length > 0 ? { include } : {}),
    },
  ])
}

/**
 * The request settings a model gets when no variant is chosen.
 *
 * Without these OpenCode v2 guesses from the model's name (any "gpt-5" is
 * asked for medium effort), which fails on a model such as gpt-5-pro that
 * accepts a single level. Router states each model's default effort, so the
 * default request carries that level with the same summary and include
 * treatment its variant would.
 */
export function defaultReasoningSettings(
  model: RouterModel,
): ReasoningEffortSettings | undefined {
  const defaultEffort = model.metadata.defaultEffort
  if (!defaultEffort) return undefined
  return reasoningEffortSettings(model).find(([effort]) => effort === defaultEffort)?.[1]
}

/**
 * Narrow Router's modalities to the ones OpenCode declares.
 *
 * Router also describes modalities OpenCode has no case for, and passing one
 * through would put a value in the provider config it cannot act on. A model
 * whose modalities OpenCode does not share still handles text.
 */
export type OpenCodeModality = "text" | "audio" | "image" | "video" | "pdf"
const OPEN_CODE_MODALITIES: readonly string[] = ["text", "audio", "image", "video", "pdf"]

export function openCodeModalities(modalities: readonly string[]): OpenCodeModality[] {
  const supported = modalities.filter((kind): kind is OpenCodeModality =>
    OPEN_CODE_MODALITIES.includes(kind),
  )
  return supported.length > 0 ? supported : ["text"]
}

/** The picker label: the vendor's own name rather than a resource path. */
export function routerModelDisplayName(model: RouterModel): string {
  const metadata = model.metadata
  return `${metadata.displayName} via ${metadata.providerDisplayName || model.ownedBy || "Ramp Router"}`
}
