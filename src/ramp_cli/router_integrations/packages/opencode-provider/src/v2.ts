import type { Plugin } from "@opencode/plugin"

import { RAMP_CLI_VERSION_HEADER, discoverRouterModels } from "./discovery.ts"
import type { RouterModel } from "./discovery.ts"
import {
  defaultReasoningSettings,
  openCodeModalities,
  reasoningEffortSettings,
  routerModelDisplayName,
} from "./mapping.ts"
import {
  DEFAULT_API_KEY_ENV,
  nonEmpty,
  resolveAPIKey,
  resolveUsageAPIKey,
  resolveBaseURL,
  resolveDefaultModel,
  resolveProviderID,
  resolveProviderName,
  resolveRampCliVersion,
  resolveUsageOrigin,
  routerPluginOptions,
} from "./options.ts"
import {
  type SessionDomainLike,
  workspaceDefaultApplier,
  workspaceDefaultCache,
} from "./workspace-default.ts"
import { applyTurnHeader, turnTracker } from "./turn-id.ts"

/**
 * Router speaks the OpenAI Responses API for every model it serves, so the
 * native Responses runtime is used regardless of the upstream vendor. The
 * generic "openai" package would pick Chat Completions for names it does not
 * recognize, and Router's translation layer is only on /v1/responses.
 */
export const V2_PROVIDER_PACKAGE = "@opencode/ai/providers/openai/responses"

/**
 * OpenCode v2's Model.Info, written structurally so the plugin does not need
 * `@opencode/plugin` at runtime: v1 hosts cannot resolve that package and the
 * schema's branded ids are plain strings once decoded.
 */
export type V2ModelInfo = {
  id: string
  modelID: string
  providerID: string
  name: string
  settings?: Record<string, unknown>
  capabilities: { tools: boolean; input: string[]; output: string[] }
  variants: Array<{ id: string; settings: Record<string, unknown> }>
  time: { released: number }
  cost: Array<{
    input: number
    output: number
    cache: { read: number; write: number }
  }>
  status: "alpha" | "beta" | "deprecated" | "active"
  enabled: boolean
  limit: { context: number; output: number }
}

export type V2ProviderInfo = {
  id: string
  name: string
  activation: "auto" | "enabled" | "disabled"
  package: string
  settings?: Record<string, unknown>
  headers?: Record<string, string>
  body?: Record<string, unknown>
}

function releaseTimestamp(date: string | undefined): number {
  if (!date || !/^\d{4}-\d{2}-\d{2}$/.test(date)) return 0
  // v2 compares release times with Date.now() to select recent models and
  // sorts the catalog by them, so the schema's finite number is UTC millis.
  const released = Date.parse(date)
  return Number.isFinite(released) && new Date(released).toISOString().slice(0, 10) === date
    ? released
    : 0
}

/**
 * Map one Router model onto OpenCode v2's native model shape.
 *
 * Every field OpenCode v2 requires is stated from Router's metadata rather
 * than left to OpenCode's defaults, which assume a 200k window and 32k output
 * for anything unspecified. Fields v2 ignores (temperature, attachment,
 * reasoning flag) are not carried over.
 */
export function toV2Model(providerID: string, model: RouterModel): V2ModelInfo {
  const metadata = model.metadata
  // Router records "active", "deprecated" or "retired". v2 has no retired
  // state, so a retired model is presented as deprecated: still described,
  // but never offered.
  const withdrawn = metadata.status === "deprecated" || metadata.status === "retired"
  return {
    id: model.id,
    modelID: model.id,
    providerID,
    name: routerModelDisplayName(model),
    // Model-level defaults, applied when no variant is chosen. Every
    // OpenCode v2 Responses route otherwise defaults every request to
    // `include: ["reasoning.encrypted_content"]`, and names any "gpt-5" model
    // a medium-effort reasoner. Only OpenAI asks for the previous turn's
    // reasoning back and Router names exactly what to send; the providers
    // Router translates for reject an include they never asked for. So the
    // model states Router's include list (empty included) and Router's own
    // default effort in place of those guesses.
    settings: {
      ...defaultReasoningSettings(model),
      include: [...metadata.reasoningInclude],
    },
    capabilities: {
      // OpenCode v2 assumes tool support unless told otherwise, matching how
      // v1 treated an unstated tool_call.
      tools: metadata.toolCalls ?? true,
      input: openCodeModalities(metadata.inputModalities),
      output: openCodeModalities(metadata.outputModalities),
    },
    variants: reasoningEffortSettings(model).map(([effort, settings]) => ({
      id: effort,
      settings,
    })),
    time: { released: releaseTimestamp(metadata.releaseDate) },
    cost: metadata.pricing
      ? [
          {
            input: metadata.pricing.input,
            output: metadata.pricing.output,
            cache: {
              read: metadata.pricing.cacheRead,
              write: metadata.pricing.cacheWrite,
            },
          },
        ]
      : [],
    status: withdrawn ? "deprecated" : "active",
    // v2 has no status-driven hiding; a withdrawn model is kept in the
    // catalog but taken out of the picker, as the v1→v2 config migration
    // does for `status: deprecated`.
    enabled: !withdrawn,
    limit: {
      context: metadata.contextWindow,
      output: metadata.maxOutputTokens,
    },
  }
}

export function toV2Provider(input: {
  providerID: string
  name: string
  baseURL: string
  apiKey: string
  rampCliVersion?: string
}): V2ProviderInfo {
  return {
    id: input.providerID,
    name: input.name,
    // The credential is resolved here, so the provider is usable without an
    // OpenCode integration account being connected for it.
    activation: "enabled",
    package: V2_PROVIDER_PACKAGE,
    settings: {
      baseURL: input.baseURL,
      apiKey: input.apiKey,
    },
    // Sent on every inference request, as v1's options.headers were. A
    // user's own `providers.<id>.headers` are merged on top by OpenCode.
    ...(input.rampCliVersion
      ? { headers: { [RAMP_CLI_VERSION_HEADER]: input.rampCliVersion } }
      : {}),
  }
}

type TurnRegistration = { dispose(): Promise<void> }
type TurnSessionLike = {
  hook(
    name: "prompt",
    callback: (event: { sessionID?: unknown; messageID?: unknown }) => void,
  ): Promise<TurnRegistration>
  hook(
    name: "model.request",
    callback: (event: {
      sessionID?: unknown
      model?: { providerID?: unknown }
      headers: Record<string, string>
    }) => void,
  ): Promise<TurnRegistration>
}

/**
 * The OpenCode v2 plugin body. Discovery runs once before the synchronous
 * provider transform, as v2 requires, and the result is captured for replay.
 */
export async function setupRouterProviderV2(
  ctx: Plugin.Context,
): Promise<Plugin.Cleanup> {
  const options = routerPluginOptions(ctx.options)
  const providerID = resolveProviderID(options)
  const name = resolveProviderName(options)
  const apiKeyEnv = nonEmpty(options.apiKeyEnv, DEFAULT_API_KEY_ENV)
  const rampCliVersion = resolveRampCliVersion(options)
  const baseURL = resolveBaseURL(options)
  const apiKey = resolveAPIKey(options)
  if (!apiKey) {
    throw new Error(
      `Set the plugin apiKey option or ${apiKeyEnv} before starting OpenCode`,
    )
  }

  // Read alongside discovery so the answer is usually in by the time the
  // provider registers; its own short timeout bounds any wait below.
  const defaults = workspaceDefaultCache({
    usageOrigin: resolveUsageOrigin(options),
    apiKey: resolveUsageAPIKey(options) ?? apiKey,
    ...(rampCliVersion ? { rampCliVersion } : {}),
  })
  defaults.prefetch()

  const discovered = await discoverRouterModels({
    baseURL,
    apiKey,
    ...(rampCliVersion ? { rampCliVersion } : {}),
  })
  const models = discovered.map((model) => toV2Model(providerID, model))
  const info = toV2Provider({
    providerID,
    name,
    baseURL,
    apiKey,
    ...(rampCliVersion ? { rampCliVersion } : {}),
  })

  // A user's own `providers.<id>` entry (extra headers, a model's name or
  // limits) is layered on top by OpenCode after plugin transforms run, so
  // the overrides v1 merged by hand need no handling here.
  await ctx.provider.transform((editor) => {
    if (editor.get(providerID)) editor.remove(providerID)
    editor.add({
      info: info as unknown as Parameters<typeof editor.add>[0]["info"],
      models: models as unknown as Parameters<typeof editor.add>[0]["models"],
    })
  })

  // The organization default reaches new sessions through the primary
  // agents' model (what the TUI consults for a session that has no model
  // yet) and the registry default; sessions in progress or resumed keep the
  // model recorded on them. Later reads happen in the background as
  // sessions are created.
  const applier = workspaceDefaultApplier({
    // OpenCode brands its ids; the applier only reads and writes plain strings.
    agent: ctx.agent as unknown as Parameters<typeof workspaceDefaultApplier>[0]["agent"],
    model: ctx.model,
    session: ctx.session as unknown as SessionDomainLike,
    configured: resolveDefaultModel(options),
    providerID,
    modelIDs: new Set(models.filter((model) => model.enabled).map((model) => model.id)),
    defaults,
  })
  await applier.refresh()
  const subscription = new AbortController()
  void applier.watch(ctx.event.subscribe({ signal: subscription.signal })).catch(() => {
    // An ended event stream only stops later refreshes.
  })
  const turnRegistrations: TurnRegistration[] = []
  const session = ctx.session as unknown as Partial<TurnSessionLike> | undefined
  if (typeof session?.hook === "function") {
    const turns = turnTracker()
    try {
      // Push each registration as soon as it exists so a later failure can dispose it.
      turnRegistrations.push(
        await session.hook("prompt", (event) => {
          turns.record(event.sessionID, event.messageID)
        }),
      )
      turnRegistrations.push(
        await session.hook("model.request", (event) => {
          if (event.model?.providerID !== providerID) return
          applyTurnHeader(event.headers, turns.current(event.sessionID))
        }),
      )
    } catch {
      // Turn IDs are attribution metadata. A host without these hooks keeps working untagged.
      await Promise.allSettled(turnRegistrations.map((registration) => registration.dispose()))
      turnRegistrations.length = 0
    }
  }
  return () => {
    subscription.abort()
    void Promise.allSettled(turnRegistrations.map((registration) => registration.dispose()))
  }
}
