import type { ExtensionAPI } from "@earendil-works/pi-coding-agent"
import {
  createProvider,
  envApiKeyAuth,
  type Model,
  type ThinkingLevelMap,
} from "@earendil-works/pi-ai"
import { openAIResponsesApi } from "@earendil-works/pi-ai/compat"

import {
  closeSync,
  constants as fsConstants,
  fstatSync,
  openSync,
  readFileSync,
  readSync,
} from "node:fs"
import { homedir } from "node:os"
import { join } from "node:path"

import { discoverRouterModels, normalizeBaseURL } from "./discovery.ts"
import type { RouterModel as DiscoveredModel } from "./discovery.ts"

const PROVIDER_ID = "ramp-router"
const API_KEY_ENVS = ["RAMP_ROUTER_API_KEY", "LLM_GATEWAY_API_KEY"]
const BASE_URL_ENVS = ["RAMP_ROUTER_BASE_URL", "LLM_GATEWAY_BASE_URL"]
const DEFAULT_BASE_URL = "https://router-api.ramp.com/v1"
const MAX_SESSION_ID_BYTES = 128
const MAX_SESSION_HEADER_BYTES = 4096
const SESSION_ID_PATTERN = /^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$/
const LINEAGE_HEADERS = [
  "X-Gateway-Client",
  "X-Session-Id",
  "X-Parent-Session-Id",
  "X-Forked-From-Session-Id",
] as const

// Written by "ramp router configure pi" beside the credential it also writes.
// Pi hands an extension no configuration of its own, so without this the
// Router to call could only come from the environment, which means setting it
// again in every shell and losing it in any launcher that does not.
const CONFIG_FILE = "ramp-router-config.json"
const PI_THINKING_LEVELS = [
  "off",
  "minimal",
  "low",
  "medium",
  "high",
  "xhigh",
  "max",
] as const

type RouterModel = Model<"openai-responses">

function firstEnvironmentValue(names: string[]): string | undefined {
  for (const name of names) {
    const value = process.env[name]
    if (value) return value
  }
  return undefined
}

function boundedSessionID(value: unknown): string | undefined {
  if (
    typeof value !== "string" ||
    Buffer.byteLength(value, "utf8") > MAX_SESSION_ID_BYTES ||
    !SESSION_ID_PATTERN.test(value)
  ) {
    return undefined
  }
  return value
}

/**
 * Resolve Pi's fork source without reading any conversation entries.
 *
 * A Pi child session records its parent as a file path, while Router needs the
 * parent's stable session ID. The session header is always the first JSONL
 * record, so cap the read and refuse files whose first record exceeds the cap.
 */
function parentSessionID(parentSessionPath: unknown): string | undefined {
  if (typeof parentSessionPath !== "string" || !parentSessionPath) {
    return undefined
  }

  let descriptor: number | undefined
  try {
    descriptor = openSync(
      parentSessionPath,
      // O_NONBLOCK ensures a malicious/extension-supplied FIFO cannot stall
      // the inference request before fstat rejects it as a non-regular file.
      fsConstants.O_RDONLY | fsConstants.O_NOFOLLOW | fsConstants.O_NONBLOCK,
    )
    if (!fstatSync(descriptor).isFile()) return undefined
    const buffer = Buffer.alloc(MAX_SESSION_HEADER_BYTES + 1)
    const bytesRead = readSync(
      descriptor,
      buffer,
      0,
      buffer.length,
      0,
    )
    const newline = buffer.subarray(0, bytesRead).indexOf(0x0a)
    if (newline < 0) return undefined

    const firstLine = buffer.subarray(0, newline)
    const parsed: unknown = JSON.parse(firstLine.toString("utf8"))
    if (!parsed || typeof parsed !== "object") return undefined
    const header = parsed as { type?: unknown; id?: unknown }
    if (header.type !== "session") return undefined
    return boundedSessionID(header.id)
  } catch {
    return undefined
  } finally {
    if (descriptor !== undefined) closeSync(descriptor)
  }
}

function replaceHeader(
  headers: Record<string, string | null>,
  name: string,
  value?: string,
): void {
  const normalizedName = name.toLowerCase()
  for (const existingName of Object.keys(headers)) {
    if (existingName.toLowerCase() === normalizedName) {
      delete headers[existingName]
    }
  }
  if (value !== undefined) headers[name] = value
}


function supportsReasoning(model: DiscoveredModel): boolean {
  return model.metadata.reasoningEfforts.length > 0
}

/**
 * Map Pi's fixed thinking levels onto the efforts the model accepts.
 *
 * Router reports the accepted set from the provider itself, which no rule
 * about the model's name predicts: gpt-5.6-sol takes six efforts but refuses
 * "minimal". A level Pi offers that the model rejects is a failed request.
 */
function thinkingLevelMap(model: DiscoveredModel): ThinkingLevelMap {
  const supported = new Set(
    model.metadata.reasoningEfforts.map((effort) => effort.value),
  )
  return Object.fromEntries(
    PI_THINKING_LEVELS.map((level) => {
      const effort = level === "off" ? "none" : level
      return [level, supported.has(effort) ? effort : null]
    }),
  )
}

/**
 * Narrow Router's modalities to the two Pi models.
 *
 * Router describes audio, pdf and video as well, and passing one straight
 * through would put a value in the model definition that Pi has no case for.
 * A model that accepts nothing Pi understands still takes text.
 */
function piInputModalities(modalities: readonly string[] | undefined): RouterModel["input"] {
  const supported = (modalities ?? []).filter(
    (kind): kind is "text" | "image" => kind === "text" || kind === "image",
  )
  return supported.length > 0 ? supported : ["text"]
}

function toPiModel(model: DiscoveredModel, baseUrl: string): RouterModel {
  const reasoning = supportsReasoning(model)
  const metadata = model.metadata
  return {
    // Pi's model selector hardcodes Model.id as each row's label. Model.name
    // participates in search but appears only below the selected row, unlike
    // Codex, which can at least show the display name in a description column.
    // Keep the canonical request name here because Pi also persists this id and
    // passes it to the provider API; using the display name would change the
    // model sent to Router rather than changing presentation alone.
    // TODO(router-client-model-aliases): Persist a current client_model_id plus
    // historical aliases in Router's catalog. On a display-name change, move
    // the previous id into aliases; reject normalized collisions and never
    // reuse an old alias. Advertise only the current id as Codex's slug and
    // Pi's Model.id, then canonicalize current and historical ids to
    // request_name before policy, routing, logs, and accounting.
    id: model.id,
    name: `${metadata.displayName} via ${metadata.providerDisplayName || model.ownedBy || "Ramp Router"}`,
    api: "openai-responses",
    provider: PROVIDER_ID,
    baseUrl,
    reasoning,
    ...(reasoning ? { thinkingLevelMap: thinkingLevelMap(model) } : {}),
    input: piInputModalities(metadata.inputModalities),
    // Router publishes real rates, so Pi's cost display stops reading zero.
    // A model priced at zero would look free rather than unpriced.
    cost: metadata.pricing ?? {
      input: 0,
      output: 0,
      cacheRead: 0,
      cacheWrite: 0,
    },
    // A shared default silently truncated long-context models and
    // over-promised short ones. Each model states its own.
    contextWindow: metadata.contextWindow,
    maxTokens: metadata.maxOutputTokens,
  }
}

/** Read the Router that "ramp router configure pi" recorded, if it did. */
function configuredBaseURL(): string | undefined {
  const home = process.env.PI_CODING_AGENT_DIR ?? join(homedir(), ".pi", "agent")
  try {
    const parsed: unknown = JSON.parse(readFileSync(join(home, CONFIG_FILE), "utf8"))
    if (parsed && typeof parsed === "object") {
      const value = (parsed as { baseUrl?: unknown }).baseUrl
      if (typeof value === "string" && value.trim()) return value.trim()
    }
  } catch {
    // Absent, unreadable or malformed all mean the same thing: nothing was
    // recorded, so fall through to the environment and then the default.
  }
  return undefined
}

export default function registerRouterProvider(pi: ExtensionAPI): void {
  // The environment still wins, so a one-off run against another stack does
  // not require rewriting the file.
  const baseUrl = normalizeBaseURL(
    firstEnvironmentValue(BASE_URL_ENVS) ?? configuredBaseURL() ?? DEFAULT_BASE_URL,
  )

  pi.registerProvider(
    createProvider({
      id: PROVIDER_ID,
      name: "Ramp Router",
      baseUrl,
      auth: {
        apiKey: envApiKeyAuth("Ramp Router API key", API_KEY_ENVS),
      },
      models: [],
      fetchModels: async ({ credential, allowNetwork, signal }) => {
        if (!allowNetwork) return []
        const apiKey =
          credential?.type === "api_key" ? credential.key : undefined
        if (!apiKey) {
          throw new Error(
            `Configure Ramp Router with Pi /login or set ${API_KEY_ENVS[0]}`,
          )
        }
        const discovered = await discoverRouterModels({
          baseURL: baseUrl,
          apiKey,
          ...(signal ? { signal } : {}),
        })
        return discovered.map((model) =>
          toPiModel(model, baseUrl),
        )
      },
      api: openAIResponsesApi(),
    }),
  )

  pi.on?.("before_provider_headers", (event, context) => {
    if (context.model?.provider !== PROVIDER_ID) return

    for (const name of LINEAGE_HEADERS) replaceHeader(event.headers, name)

    const sessionID = boundedSessionID(context.sessionManager.getSessionId())
    if (!sessionID) return

    replaceHeader(event.headers, "X-Gateway-Client", "pi")
    replaceHeader(event.headers, "X-Session-Id", sessionID)

    const forkedFromSessionID = parentSessionID(
      context.sessionManager.getHeader()?.parentSession,
    )
    if (!forkedFromSessionID || forkedFromSessionID === sessionID) return

    // Keep the compatibility parent field during rollout while giving Router
    // an unambiguous conversation-fork source that is not overloaded with
    // control-plane/subagent parentage.
    replaceHeader(event.headers, "X-Parent-Session-Id", forkedFromSessionID)
    replaceHeader(event.headers, "X-Forked-From-Session-Id", forkedFromSessionID)
  })
}

export { discoverRouterModels, normalizeBaseURL } from "./discovery.ts"
