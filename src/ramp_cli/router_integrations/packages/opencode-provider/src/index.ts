import type { Config, Plugin, PluginModule } from "@opencode-ai/plugin"

import { discoverRouterModels } from "./discovery.ts"
import type { RouterModel } from "./discovery.ts"
import {
  DEFAULT_API_KEY_ENV,
  LEGACY_API_KEY_ENV,
  nonEmpty,
  resolveAPIKey,
  resolveBaseURL,
  resolveProviderID,
  resolveProviderName,
  routerPluginOptions,
} from "./options.ts"

type MutableProviderConfig = {
  npm?: string
  name?: string
  env?: string[]
  options?: Record<string, unknown>
  models?: Record<string, Record<string, unknown>>
}

function providerConfig(
  config: Config,
  providerID: string,
): MutableProviderConfig | undefined {
  return config.provider?.[providerID] as MutableProviderConfig | undefined
}

function supportsReasoning(model: RouterModel): boolean {
  return model.metadata.reasoningEfforts.length > 0
}

/**
 * One variant per effort the model actually accepts.
 *
 * Router reports these from the provider itself, which no naming convention
 * predicts: gpt-5-pro takes "high" alone, gpt-5.5-pro starts at "medium", and
 * gpt-5.6-sol takes six levels but refuses "minimal". Offering a level the
 * model rejects turns a menu entry into a failed request.
 */
function routerReasoningVariants(
  model: RouterModel,
): Record<string, Record<string, unknown>> | undefined {
  const metadata = model.metadata
  if (metadata.reasoningEfforts.length === 0) return undefined
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
  return Object.fromEntries(
    metadata.reasoningEfforts.map((effort) => [
      effort.value,
      {
        reasoningEffort: effort.value,
        ...(summary ? { reasoningSummary: summary } : {}),
        ...(include.length > 0 ? { include } : {}),
      },
    ]),
  )
}

/**
 * The remaining facts OpenCode understands, included only when Router states
 * them so an absent value keeps OpenCode's own default rather than becoming a
 * confident wrong answer.
 *
 * Cost matters most: without it OpenCode shows every Router model as free.
 * Status lets it mark a deprecated model, and attachment tells it whether a
 * file can be dropped into the prompt at all.
 */
function routerModelFacts(
  metadata: RouterModel["metadata"],
): Record<string, unknown> {
  const facts: Record<string, unknown> = {}
  if (metadata.pricing) {
    facts.cost = {
      input: metadata.pricing.input,
      output: metadata.pricing.output,
      cache_read: metadata.pricing.cacheRead,
      cache_write: metadata.pricing.cacheWrite,
    }
  }
  if (metadata.status === "active" || metadata.status === "deprecated") {
    facts.status = metadata.status
  }
  if (metadata.temperature !== undefined) facts.temperature = metadata.temperature
  // release_date is deliberately withheld. OpenCode's desktop hides a model
  // that has one unless it is the newest of its "family" released in the last
  // six months, and Router publishes no family, so every model landed in one
  // group and all but the single newest were switched off. Sending half of
  // what that rule needs is worse than sending none: without a date the rule
  // does not apply and every model is offered. Restore this together with a
  // family once Router states one.
  if (metadata.inputModalities.length > 0) {
    // Anything beyond plain text means a file can be attached.
    facts.attachment = metadata.inputModalities.some((kind) => kind !== "text")
  }
  return facts
}

/**
 * Narrow Router's modalities to the ones OpenCode declares.
 *
 * Router also describes modalities OpenCode has no case for, and passing one
 * through would put a value in the provider config it cannot act on. A model
 * whose modalities OpenCode does not share still handles text.
 */
type OpenCodeModality = "text" | "audio" | "image" | "video" | "pdf"
const OPEN_CODE_MODALITIES: readonly string[] = ["text", "audio", "image", "video", "pdf"]

function openCodeModalities(modalities: readonly string[]): OpenCodeModality[] {
  const supported = modalities.filter((kind): kind is OpenCodeModality =>
    OPEN_CODE_MODALITIES.includes(kind),
  )
  return supported.length > 0 ? supported : ["text"]
}

const RouterProvider: Plugin = async (_input, rawOptions) => {
  const options = routerPluginOptions(rawOptions)
  const providerID = resolveProviderID(options)
  const name = resolveProviderName(options)
  const apiKeyEnv = nonEmpty(options.apiKeyEnv, DEFAULT_API_KEY_ENV)
  const inlineAPIKey = nonEmpty(options.apiKey, "")

  return {
    config: async (config) => {
      const baseURL = resolveBaseURL(options)
      const apiKey = resolveAPIKey(options)
      if (!apiKey) {
        throw new Error(
          `Set the plugin apiKey option or ${apiKeyEnv} before starting OpenCode`,
        )
      }

      const discovered = await discoverRouterModels({ baseURL, apiKey })
      const existing = providerConfig(config, providerID)
      const existingModels = existing?.models ?? {}
      const models = Object.fromEntries(
        discovered.map((model) => {
          const reasoning = supportsReasoning(model)
          const variants = reasoning
            ? routerReasoningVariants(model)
            : undefined
          const metadata = model.metadata
          return [
            model.id,
            {
              // Router's display name is the vendor's own, so the picker reads
              // "GPT-5.4" rather than a provider resource path.
              name: `${metadata.displayName} via ${metadata.providerDisplayName || model.ownedBy || "Ramp Router"}`,
              reasoning,
              ...(metadata.toolCalls !== undefined
                ? { tool_call: metadata.toolCalls }
                : {}),
              modalities: {
                input: openCodeModalities(metadata.inputModalities),
                output: openCodeModalities(metadata.outputModalities),
              },
              limit: {
                // A shared default silently truncated long-context models and
                // over-promised short ones. Each model states its own.
                context: metadata.contextWindow,
                output: metadata.maxOutputTokens,
              },
              ...routerModelFacts(metadata),
              ...(variants ? { variants } : {}),
              ...existingModels[model.id],
            },
          ]
        }),
      )

      config.provider ??= {}
      config.provider[providerID] = {
        ...existing,
        npm: "@ai-sdk/openai",
        name,
        env: inlineAPIKey
          ? (existing?.env ?? [])
          : [
              ...new Set([
                ...(existing?.env ?? []),
                apiKeyEnv,
                LEGACY_API_KEY_ENV,
              ]),
            ],
        options: {
          ...existing?.options,
          baseURL,
          ...(inlineAPIKey ? { apiKey: inlineAPIKey } : {}),
        },
        models,
      }
    },
  }
}

const plugin = {
  id: "@ramp/router-opencode-provider",
  server: RouterProvider,
} satisfies PluginModule

export default plugin
export { discoverRouterModels, normalizeBaseURL } from "./discovery.ts"
