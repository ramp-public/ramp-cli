import type { Config, Plugin } from "@opencode-ai/plugin"

import { RAMP_CLI_VERSION_HEADER, discoverRouterModels } from "./discovery.ts"
import type { RouterModel } from "./discovery.ts"
import {
  openCodeModalities,
  reasoningEffortSettings,
  routerModelDisplayName,
  supportsReasoning,
} from "./mapping.ts"
import {
  DEFAULT_API_KEY_ENV,
  LEGACY_API_KEY_ENV,
  nonEmpty,
  resolveAPIKey,
  resolveBaseURL,
  resolveProviderID,
  resolveProviderName,
  resolveRampCliVersion,
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

/** OpenCode v1 keys variants by name. */
function routerReasoningVariants(
  model: RouterModel,
): Record<string, Record<string, unknown>> | undefined {
  const settings = reasoningEffortSettings(model)
  return settings.length > 0 ? Object.fromEntries(settings) : undefined
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

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
}

/**
 * The OpenCode v1 server plugin: mutates the global config's provider entry
 * from the `config` hook. OpenCode v2 never calls this; see ./v2.ts.
 */
export const RouterProviderV1: Plugin = async (_input, rawOptions) => {
  const options = routerPluginOptions(rawOptions)
  const providerID = resolveProviderID(options)
  const name = resolveProviderName(options)
  const apiKeyEnv = nonEmpty(options.apiKeyEnv, DEFAULT_API_KEY_ENV)
  const inlineAPIKey = nonEmpty(options.apiKey, "")
  const rampCliVersion = resolveRampCliVersion(options)

  return {
    config: async (config) => {
      const baseURL = resolveBaseURL(options)
      const apiKey = resolveAPIKey(options)
      if (!apiKey) {
        throw new Error(
          `Set the plugin apiKey option or ${apiKeyEnv} before starting OpenCode`,
        )
      }

      const discovered = await discoverRouterModels({
        baseURL,
        apiKey,
        ...(rampCliVersion ? { rampCliVersion } : {}),
      })
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
              name: routerModelDisplayName(model),
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
      const existingHeaders = existing?.options?.headers
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
          // OpenCode spreads these options into the @ai-sdk/openai factory,
          // which sends `headers` on every request; a user's own headers on
          // the existing entry are kept alongside.
          ...(rampCliVersion
            ? {
                headers: {
                  ...(isRecord(existingHeaders) ? existingHeaders : {}),
                  [RAMP_CLI_VERSION_HEADER]: rampCliVersion,
                },
              }
            : {}),
        },
        models,
      }
    },
  }
}
