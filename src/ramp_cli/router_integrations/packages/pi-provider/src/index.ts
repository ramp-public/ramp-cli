import {
  ModelRuntime,
  VERSION as PI_VERSION,
  readStoredCredential,
  type ExtensionAPI,
} from "@earendil-works/pi-coding-agent"
import {
  createProvider,
  envApiKeyAuth,
  lazyStream,
  type Credential,
  type Model,
  type ProviderRequestOptions,
  type ProviderStreams,
  type RefreshModelsContext,
  type ThinkingLevelMap,
} from "@earendil-works/pi-ai"
import { openAIResponsesApi } from "@earendil-works/pi-ai/api/openai-responses.lazy"

import {
  closeSync,
  constants as fsConstants,
  fstatSync,
  fsyncSync,
  linkSync,
  mkdirSync,
  openSync,
  readFileSync,
  readSync,
  renameSync,
  unlinkSync,
  writeFileSync,
} from "node:fs"
import { createHmac, randomBytes, randomUUID } from "node:crypto"
import { homedir } from "node:os"
import { join } from "node:path"

import { discoverRouterModels, normalizeBaseURL } from "./discovery.ts"
import type { RouterModel as DiscoveredModel } from "./discovery.ts"
import { registerUsageWidget } from "./usage.ts"

const PROVIDER_ID = "ramp-router"
const API_KEY_ENVS = ["RAMP_ROUTER_API_KEY", "LLM_GATEWAY_API_KEY"]
const BASE_URL_ENVS = ["RAMP_ROUTER_BASE_URL", "LLM_GATEWAY_BASE_URL"]
const DEFAULT_BASE_URL = "https://router-api.ramp.com/v1"
// Only the first uncached launch waits for discovery. A stalled Router must not
// inherit the general 10-second request deadline and hold every Pi startup.
const STARTUP_DISCOVERY_TIMEOUT_MS = 2_000
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
const RUNTIME_MODELS_FILE = "ramp-router-runtime-models.json"
const MODEL_CACHE_FILE = "ramp-router-model-cache.json"
const MODEL_CACHE_KEY_FILE = "ramp-router-model-cache-key"
const MODEL_CACHE_VERSION = 2
// Local safety ceilings, not Router catalog limits. Oversized private state is
// discarded and rebuilt from the bounded startup request instead of read into
// memory during every Pi launch.
const MAX_MODEL_CACHE_BYTES = 4 * 1024 * 1024
const MAX_MODEL_CACHE_KEY_BYTES = 65
const RUNTIME_BOOTSTRAP_MINIMUM = [0, 84, 1] as const
const PROCESS_CREDENTIAL_IDENTITY_KEY = randomBytes(32)
let cacheIdentityState:
  | { path: string; key?: Buffer; persistent: boolean }
  | undefined
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

/**
 * The isolated credential-runtime options used below landed in Pi 0.84.1.
 *
 * Ramp installs this extension by path, so npm cannot enforce the peer floor
 * against an already-installed Pi. Refuse only the model bootstrap on an
 * older or prerelease host; provider registration and the lineage hook still
 * load so ordinary Pi startup remains usable.
 */
export function supportsRuntimeModelBootstrap(version: string): boolean {
  const match = /^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$/.exec(
    version,
  )
  if (!match) return false
  if (match[4] !== undefined) return false

  const actual = match.slice(1, 4).map(Number)
  if (actual.some((part) => !Number.isSafeInteger(part))) return false
  for (let index = 0; index < RUNTIME_BOOTSTRAP_MINIMUM.length; index += 1) {
    const actualPart = actual[index] ?? 0
    const minimumPart = RUNTIME_BOOTSTRAP_MINIMUM[index] ?? 0
    if (actualPart !== minimumPart) return actualPart > minimumPart
  }
  return true
}

function firstEnvironmentValue(names: string[]): string | undefined {
  for (const name of names) {
    const value = process.env[name]
    if (value) return value
  }
  return undefined
}

/** Read only a small, owner-private regular file without following symlinks. */
function readPrivateFile(path: string, maxBytes: number): string | undefined {
  let descriptor: number | undefined
  try {
    descriptor = openSync(
      path,
      fsConstants.O_RDONLY | fsConstants.O_NOFOLLOW | fsConstants.O_NONBLOCK,
    )
    const stat = fstatSync(descriptor)
    if (
      !stat.isFile() ||
      stat.size < 0 ||
      stat.size > maxBytes ||
      (process.platform !== "win32" && (stat.mode & 0o077) !== 0) ||
      (process.platform !== "win32" &&
        typeof process.getuid === "function" &&
        stat.uid !== process.getuid())
    ) {
      return undefined
    }

    const buffer = Buffer.alloc(stat.size + 1)
    const bytesRead = readSync(descriptor, buffer, 0, buffer.length, 0)
    if (bytesRead !== stat.size) return undefined
    return buffer.subarray(0, bytesRead).toString("utf8")
  } catch {
    return undefined
  } finally {
    if (descriptor !== undefined) closeSync(descriptor)
  }
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

function piHome(): string {
  return process.env.PI_CODING_AGENT_DIR ?? join(homedir(), ".pi", "agent")
}

/** Read the Router that "ramp router configure pi" recorded, if it did. */
function configuredBaseURL(): string | undefined {
  const home = piHome()
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

type CatalogGeneration = { current: number }

function resolvedCredentialApiKey(credential?: Credential): string | undefined {
  if (credential === undefined) return firstEnvironmentValue(API_KEY_ENVS)
  if (
    credential.type !== "api_key" ||
    typeof credential.key !== "string" ||
    !credential.key
  ) return undefined
  // Pi resolves templates and commands before constructing RefreshModelsContext
  // or applying filterModels. A resulting key may legitimately begin with `!`
  // (for example `$!opaque`); only raw readStoredCredential callers may treat
  // that prefix as command syntax. Runtime overrides are also effective values.
  return credential.key
}

function routerProvider(
  baseUrl: string,
  models: readonly RouterModel[],
  catalogGeneration: CatalogGeneration,
  initialCredentialIdentity?: string,
) {
  // Pi's generic dynamic-provider helper restores models-store.json by only
  // provider ID. That store carries neither Router URL nor credential scope,
  // so using it here could revive another stack's or business's catalog before
  // our private scoped cache runs. Own refresh directly: ignore `stored`, and
  // update only this process after a credential-bound network response.
  const copyModel = (model: RouterModel): RouterModel => structuredClone(model)
  const copyModels = (values: readonly RouterModel[]): RouterModel[] =>
    values.map(copyModel)
  // Never expose the model objects used as request authority. Pi and other
  // extensions retain Model references and can mutate them after selection.
  let currentModels = copyModels(models)
  let catalogCredentialIdentity = initialCredentialIdentity
  const payloadModelError = () =>
    new Error("Ramp Router request model is not in the active catalog")
  const guardedRequestOptions = <T extends ProviderRequestOptions>(
    expectedModelID: string,
    options: T | undefined,
  ): T => {
    const transformPayload = options?.onPayload
    return {
      ...options,
      onPayload: async (payload, model) => {
        const replacement = await transformPayload?.(payload, model)
        // Match the underlying adapter: only `undefined` means "use the
        // original". A hook that explicitly returns null must fail closed.
        const finalPayload = replacement === undefined ? payload : replacement
        if (
          finalPayload === null ||
          typeof finalPayload !== "object" ||
          Array.isArray(finalPayload)
        ) throw payloadModelError()
        let finalModel: unknown
        let store: unknown
        let stream: unknown
        try {
          if (
            !Object.hasOwn(finalPayload, "model") ||
            !Object.hasOwn(finalPayload, "store") ||
            !Object.hasOwn(finalPayload, "stream")
          ) throw payloadModelError()
          finalModel = Reflect.get(finalPayload, "model")
        } catch {
          throw payloadModelError()
        }
        if (finalModel !== expectedModelID) throw payloadModelError()
        try {
          store = Reflect.get(finalPayload, "store")
          stream = Reflect.get(finalPayload, "stream")
        } catch {
          throw new Error("Ramp Router request payload violates provider invariants")
        }
        if (store !== false || stream !== true) {
          throw new Error("Ramp Router request payload violates provider invariants")
        }
        // Detach the top-level request from any object retained by an extension
        // hook, then reassert the guarded fields last. A later mutation of the
        // hook's object cannot change the model or storage semantics sent.
        return {
          ...finalPayload,
          model: expectedModelID,
          store: false,
          stream: true,
        }
      },
    } as T
  }
  const snapshotRequestOptions = <T extends ProviderRequestOptions>(
    options: T | undefined,
  ): T | undefined => {
    if (options === undefined) return undefined
    try {
      const snapshot = { ...options }
      const headers = snapshot.headers
      if (headers === undefined) return snapshot
      if (headers === null || typeof headers !== "object") {
        throw new Error("invalid headers")
      }
      const entries = Object.entries(headers)
      if (entries.some(([, value]) => value !== null && typeof value !== "string")) {
        throw new Error("invalid header value")
      }
      // Forward only a plain, detached snapshot. A retained object or stateful
      // Proxy cannot add Authorization after the guard and before SDK dispatch.
      return {
        ...snapshot,
        headers: Object.fromEntries(entries),
      }
    } catch {
      throw new Error("Ramp Router request headers are invalid")
    }
  }
  const requestModel = (
    model: RouterModel,
    options?: { apiKey?: string; headers?: Record<string, string | null> },
  ): RouterModel => {
    let composed: RouterModel
    try {
      // Pi composes models.json tuning into the Model passed to the provider.
      // Snapshot it before validation so a retained reference cannot change
      // what is checked versus what the Responses adapter later consumes.
      composed = copyModel(model)
    } catch {
      throw new Error("Ramp Router model does not match this provider")
    }
    const apiKey = options?.apiKey
    if (
      !apiKey ||
      credentialIdentity(apiKey) !== catalogCredentialIdentity
    ) {
      throw new Error("Ramp Router catalog does not match the active credential")
    }
    for (const name of Object.keys(options?.headers ?? {})) {
      const normalized = name.toLowerCase()
      if (
        normalized === "authorization" ||
        normalized === "cf-aig-authorization"
      ) {
        throw new Error("Ramp Router request authorization is ambiguous")
      }
    }
    if (
      composed.provider !== PROVIDER_ID ||
      composed.api !== "openai-responses" ||
      composed.baseUrl !== baseUrl
    ) {
      // baseUrl is the destination for the credential checked above, not a
      // presentation/tuning override. Router endpoint changes must use the
      // Ramp config/env path, which scopes discovery and the private cache too.
      throw new Error("Ramp Router model does not match this provider")
    }
    const modelID = composed.id
    const canonical = currentModels.find((candidate) => candidate.id === modelID)
    if (!canonical) {
      throw new Error("Ramp Router model is not in the active catalog")
    }
    // Preserve Pi's validated models.json tuning while pinning the four
    // identity-bearing fields to the private active catalog. The adapter and
    // onPayload hooks receive a disposable copy, never that private authority.
    return {
      ...copyModel(canonical),
      name: composed.name,
      reasoning: composed.reasoning,
      ...(composed.thinkingLevelMap === undefined
        ? {}
        : { thinkingLevelMap: composed.thinkingLevelMap }),
      input: composed.input,
      cost: composed.cost,
      contextWindow: composed.contextWindow,
      maxTokens: composed.maxTokens,
      ...(composed.samplingParams === undefined
        ? {}
        : { samplingParams: composed.samplingParams }),
      ...(composed.compat === undefined ? {} : { compat: composed.compat }),
    }
  }
  const responsesApi: ProviderStreams = openAIResponsesApi()
  const guardedApi: ProviderStreams = {
    stream: (model, context, options) =>
      lazyStream(model, async () => {
        const requestOptions = snapshotRequestOptions(options)
        const canonical = requestModel(model as RouterModel, requestOptions)
        const expectedModelID = canonical.id
        return responsesApi.stream(
          canonical,
          context,
          guardedRequestOptions(expectedModelID, requestOptions),
        )
      }),
    streamSimple: (model, context, options) =>
      lazyStream(model, async () => {
        const requestOptions = snapshotRequestOptions(options)
        const canonical = requestModel(model as RouterModel, requestOptions)
        const expectedModelID = canonical.id
        return responsesApi.streamSimple(
          canonical,
          context,
          guardedRequestOptions(expectedModelID, requestOptions),
        )
      }),
    ...(responsesApi.fetchDeferred
      ? {
          fetchDeferred: (model, handle, options) =>
            lazyStream(model, async () => {
              const requestOptions = snapshotRequestOptions(options)
              const canonical = requestModel(model as RouterModel, requestOptions)
              const expectedModelID = canonical.id
              return responsesApi.fetchDeferred!(
                canonical,
                handle,
                guardedRequestOptions(expectedModelID, requestOptions),
              )
            }),
        }
      : {}),
    ...(responsesApi.cancelDeferred
      ? {
          cancelDeferred: async (model, handle, options) => {
            const requestOptions = snapshotRequestOptions(options)
            const canonical = requestModel(model as RouterModel, requestOptions)
            const expectedModelID = canonical.id
            await responsesApi.cancelDeferred!(
              canonical,
              handle,
              guardedRequestOptions(expectedModelID, requestOptions),
            )
          },
        }
      : {}),
  }
  const provider = createProvider({
    id: PROVIDER_ID,
    name: "Ramp Router",
    baseUrl,
    auth: {
      apiKey: envApiKeyAuth("Ramp Router API key", API_KEY_ENVS),
    },
    models,
    api: guardedApi,
  })
  const registeredProvider = {
    ...provider,
    getModels: () => copyModels(currentModels),
    // Pi owns the effective credential, including runtime --api-key
    // overrides. Filter against that exact resolved value instead of trying
    // to infer whether a refresh credential came from disk or runtime state.
    filterModels: (candidateModels: readonly RouterModel[], credential?: Credential) => {
      const apiKey = resolvedCredentialApiKey(credential)
      return apiKey && credentialIdentity(apiKey) === catalogCredentialIdentity
        ? candidateModels
        : []
    },
    refreshModels: async ({
      credential,
      allowNetwork,
      signal,
      publish,
    }: RefreshModelsContext) => {
      if (signal.aborted) return
      // Pi runs an offline provider-scoped refresh immediately after /login,
      // /logout, or a runtime-key change. That phase must invalidate a catalog
      // discovered for another credential before inference can pair the new
      // key with the old business's models. If Pi has no stored credential,
      // resolve the environment-backed key exactly as inference does.
      const apiKey = allowNetwork
        ? resolvedCredentialApiKey(credential)
        : undefined
      const requestCredentialIdentity = apiKey
        ? credentialIdentity(apiKey)
        : undefined
      if (!allowNetwork) {
        const offlineApiKey = resolvedCredentialApiKey(credential)
        const offlineCredentialIdentity = offlineApiKey
          ? credentialIdentity(offlineApiKey)
          : undefined
        const matchesCatalog =
          offlineCredentialIdentity !== undefined &&
          offlineCredentialIdentity === catalogCredentialIdentity
        if (!matchesCatalog) {
          // Also invalidate the detached startup refresh. Runtime-only API key
          // changes are not visible in auth.json, but Pi always runs this
          // provider-scoped offline phase with the new credential.
          catalogGeneration.current += 1
        }
        await publish({
          // Always remove Pi's legacy provider-ID-only catalog. It cannot be
          // authenticated to this endpoint or credential.
          persist: null,
          ...(!matchesCatalog
            ? {
                update: () => {
                  currentModels = []
                  catalogCredentialIdentity = undefined
                },
              }
            : {}),
        })
        return
      }
      if (!apiKey) {
        throw new Error(
          `Configure Ramp Router with Pi /login or set ${API_KEY_ENVS[0]}`,
        )
      }
      const refreshGeneration = ++catalogGeneration.current
      const discovered = await discoverRouterModels({
        baseURL: baseUrl,
        apiKey,
        ...(signal ? { signal } : {}),
      })
      if (signal.aborted) return
      const refreshed = discovered.map((model) => toPiModel(model, baseUrl))
      if (refreshGeneration !== catalogGeneration.current) return
      if (signal.aborted || refreshGeneration !== catalogGeneration.current) return
      // Cross Pi's refresh-generation gate before mutating either catalog.
      // If /login or a newer refresh superseded this request, publish returns
      // false and the old response cannot touch the live or private cache.
      if (!(await publish({ persist: null }))) return
      if (signal.aborted || refreshGeneration !== catalogGeneration.current) return
      await publish({
        update: () => {
          if (
            signal.aborted ||
            refreshGeneration !== catalogGeneration.current
          ) return
          // Pi invokes update only after its refresh generation is still
          // current. Persist inside that generation-controlled callback so a
          // superseded response can never overwrite a newer credential's
          // private cache.
          try {
            writeModelCache(baseUrl, apiKey, refreshed)
          } catch {
            // The live provider refresh remains useful when its optional
            // startup cache cannot be updated.
          }
          currentModels = copyModels(refreshed)
          catalogCredentialIdentity = requestCredentialIdentity
        },
      })
    },
  }

  return {
    registeredProvider,
    publishBackground: (
      generation: number,
      apiKey: string,
      refreshed: readonly RouterModel[],
    ): "published" | "opaque" | "rejected" => {
      if (generation !== catalogGeneration.current) return "rejected"
      const requestCredentialIdentity = credentialIdentity(apiKey)
      // This path has no Pi refresh-generation callback, so mutate live state
      // only when the credential can be revalidated synchronously in the same
      // JavaScript turn. Command-backed credentials still warm their scoped
      // cache, but wait for Pi's owned refresh before changing live models.
      const configuredIdentity = configuredCredentialIdentityNow()
      if (configuredIdentity === null) {
        try {
          writeModelCache(baseUrl, apiKey, refreshed)
        } catch {
          // The cache is an optional next-launch optimization.
        }
        return "opaque"
      }
      if (configuredIdentity !== requestCredentialIdentity) {
        return "rejected"
      }
      currentModels = copyModels(refreshed)
      catalogCredentialIdentity = requestCredentialIdentity
      try {
        writeModelCache(baseUrl, apiKey, refreshed)
      } catch {
        // A safely published live catalog remains useful when storage fails.
      }
      return "published"
    },
  }
}

/** Replace a private file without exposing a partially-written value. */
function atomicWritePrivate(path: string, contents: string): void {
  const temporaryPath = `${path}.${process.pid}.${randomUUID()}.tmp`
  let descriptor: number | undefined
  try {
    descriptor = openSync(
      temporaryPath,
      fsConstants.O_WRONLY | fsConstants.O_CREAT | fsConstants.O_EXCL,
      0o600,
    )
    writeFileSync(descriptor, contents, { encoding: "utf8" })
    fsyncSync(descriptor)
    closeSync(descriptor)
    descriptor = undefined
    renameSync(temporaryPath, path)
  } finally {
    if (descriptor !== undefined) closeSync(descriptor)
    try {
      unlinkSync(temporaryPath)
    } catch (error) {
      if (
        !error ||
        typeof error !== "object" ||
        !("code" in error) ||
        error.code !== "ENOENT"
      ) {
        throw error
      }
    }
  }
}

function runtimeModelsPath(): string {
  const home = piHome()
  mkdirSync(home, { recursive: true, mode: 0o700 })
  const path = join(home, RUNTIME_MODELS_FILE)
  atomicWritePrivate(
    path,
    JSON.stringify({
      providers: {
        [PROVIDER_ID]: {
          name: "Ramp Router",
          api: "openai-responses",
          baseUrl: DEFAULT_BASE_URL,
          models: [],
        },
      },
    }),
  )
  return path
}

/** Resolve only Router's request credential without refreshing any provider. */
async function routerApiKey(): Promise<string | undefined> {
  const home = piHome()
  const authPath = join(home, "auth.json")
  const storedCredential = readStoredCredential(PROVIDER_ID, authPath)
  const runtime = await ModelRuntime.create({
    authPath,
    modelsPath: runtimeModelsPath(),
    refreshOnCreate: false,
    allowModelNetwork: false,
  })
  const resolved = await runtime.getAuth(PROVIDER_ID)
  if (resolved?.auth.apiKey) return resolved.auth.apiKey

  // Match Pi's real inference precedence. Once a provider has any stored
  // credential, an incompatible or unresolved value is authoritative failure;
  // Pi does not silently switch to an ambient key for the same provider.
  return storedCredential === undefined
    ? firstEnvironmentValue(API_KEY_ENVS)
    : undefined
}

async function routerCredentialIsCurrent(apiKey: string): Promise<boolean> {
  try {
    const stored = readStoredCredential(
      PROVIDER_ID,
      join(piHome(), "auth.json"),
    )
    // Avoid Pi's process-wide credential-store cache for the ordinary literal
    // form written by ramp-cli. This makes an external configure/login visible
    // even when it lands within the filesystem timestamp resolution window.
    if (
      stored?.type === "api_key" &&
      typeof stored.key === "string" &&
      !stored.key.startsWith("!") &&
      !stored.key.includes("$")
    ) {
      return stored.key === apiKey
    }
    if (stored === undefined) {
      return firstEnvironmentValue(API_KEY_ENVS) === apiKey
    }
    return (await routerApiKey()) === apiKey
  } catch {
    return false
  }
}

/**
 * Return the identity of a synchronously observable configured credential.
 * `null` means Pi must resolve a template/command asynchronously, so callers
 * retain the async checks around publication instead of guessing.
 */
function configuredCredentialIdentityNow(): string | undefined | null {
  try {
    const stored = readStoredCredential(
      PROVIDER_ID,
      join(piHome(), "auth.json"),
    )
    if (stored === undefined) {
      const environmentKey = firstEnvironmentValue(API_KEY_ENVS)
      return environmentKey ? credentialIdentity(environmentKey) : undefined
    }
    if (stored.type === "api_key" && typeof stored.key === "string") {
      if (stored.key.startsWith("!")) return null
      const storedEnvironment = stored.env
      if (
        storedEnvironment !== undefined &&
        (storedEnvironment === null ||
          typeof storedEnvironment !== "object" ||
          Array.isArray(storedEnvironment) ||
          Object.values(storedEnvironment).some(
            (value) => typeof value !== "string",
          ))
      ) return null
      const resolved = resolveCredentialTemplate(
        stored.key,
        storedEnvironment as Record<string, string> | undefined,
      )
      return resolved === undefined ? undefined : credentialIdentity(resolved)
    }
    return null
  } catch {
    return null
  }
}

/** Match Pi's non-command config interpolation without executing anything. */
function resolveCredentialTemplate(
  value: string,
  environment?: Record<string, string>,
): string | undefined {
  let resolved = ""
  for (let index = 0; index < value.length;) {
    if (value[index] !== "$") {
      resolved += value[index]
      index += 1
      continue
    }
    const next = value[index + 1]
    if (next === "$" || next === "!") {
      resolved += next
      index += 2
      continue
    }
    if (next === "{") {
      const end = value.indexOf("}", index + 2)
      if (end >= 0) {
        const name = value.slice(index + 2, end)
        if (/^[A-Za-z_][A-Za-z0-9_]*$/.test(name)) {
          const environmentValue = environment?.[name] || process.env[name]
          if (!environmentValue) return undefined
          resolved += environmentValue
          index = end + 1
          continue
        }
      }
      resolved += "$"
      index += 1
      continue
    }
    const name = value.slice(index + 1).match(/^[A-Za-z_][A-Za-z0-9_]*/)?.[0]
    if (name) {
      const environmentValue = environment?.[name] || process.env[name]
      if (!environmentValue) return undefined
      resolved += environmentValue
      index += name.length + 1
      continue
    }
    resolved += "$"
    index += 1
  }
  return resolved
}

function cacheIdentityKey(): Buffer | undefined {
  const home = piHome()
  mkdirSync(home, { recursive: true, mode: 0o700 })
  const path = join(home, MODEL_CACHE_KEY_FILE)
  if (cacheIdentityState?.path === path) {
    return cacheIdentityState.persistent ? cacheIdentityState.key : undefined
  }

  const readExisting = (): Buffer | undefined => {
    const encoded = readPrivateFile(path, MAX_MODEL_CACHE_KEY_BYTES)
    if (encoded === undefined || !/^[0-9a-f]{64}\n?$/.test(encoded)) {
      return undefined
    }
    return Buffer.from(encoded.trimEnd(), "hex")
  }

  const existing = readExisting()
  if (existing) {
    cacheIdentityState = { path, key: existing, persistent: true }
    return existing
  }

  const temporaryPath = `${path}.${process.pid}.${randomUUID()}.tmp`
  let descriptor: number | undefined
  try {
    const generated = randomBytes(32)
    descriptor = openSync(
      temporaryPath,
      fsConstants.O_WRONLY | fsConstants.O_CREAT | fsConstants.O_EXCL,
      0o600,
    )
    writeFileSync(descriptor, `${generated.toString("hex")}\n`, {
      encoding: "utf8",
    })
    fsyncSync(descriptor)
    closeSync(descriptor)
    descriptor = undefined

    try {
      // link(2) gives this initialization create-if-absent semantics. A plain
      // rename could replace another simultaneous launch's identity key.
      linkSync(temporaryPath, path)
      cacheIdentityState = { path, key: generated, persistent: true }
      return generated
    } catch (error) {
      const code =
        error && typeof error === "object" && "code" in error
          ? error.code
          : undefined
      if (code === "EEXIST") {
        const concurrent = readExisting()
        if (concurrent) {
          cacheIdentityState = { path, key: concurrent, persistent: true }
          return concurrent
        }
      }
      // Atomic no-replace publication requires hard links. Valid homes on
      // filesystems without them still get live discovery, but skip the
      // optional persistent cache rather than exposing a partial key or
      // leaving a crash-stale lock that blocks every later launch.
      cacheIdentityState = { path, persistent: false }
      return undefined
    }
  } catch {
    cacheIdentityState = { path, persistent: false }
    return undefined
  } finally {
    if (descriptor !== undefined) {
      try {
        closeSync(descriptor)
      } catch {
        // Persistent cache initialization is optional.
      }
    }
    try {
      unlinkSync(temporaryPath)
    } catch {
      // A unique owner-private temp may remain after an I/O failure, but it
      // contains only random cache-auth material and is never consumed.
    }
  }
}

function credentialIdentity(apiKey: string): string {
  return createHmac("sha256", PROCESS_CREDENTIAL_IDENTITY_KEY)
    .update(apiKey)
    .digest("hex")
}

function cacheCredentialIdentity(apiKey: string): string | undefined {
  const key = cacheIdentityKey()
  return key === undefined
    ? undefined
    : createHmac("sha256", key).update(apiKey).digest("hex")
}

function cachedRouterModel(value: unknown, baseUrl: string): RouterModel | undefined {
  if (!value || typeof value !== "object") return undefined
  const model = value as Record<string, unknown>
  const allowedKeys = new Set([
    "id",
    "name",
    "api",
    "provider",
    "baseUrl",
    "reasoning",
    "thinkingLevelMap",
    "input",
    "cost",
    "contextWindow",
    "maxTokens",
  ])
  if (
    Object.keys(model).some((key) => !allowedKeys.has(key)) ||
    typeof model.id !== "string" ||
    !model.id ||
    typeof model.name !== "string" ||
    model.provider !== PROVIDER_ID ||
    model.api !== "openai-responses" ||
    model.baseUrl !== baseUrl ||
    typeof model.reasoning !== "boolean" ||
    typeof model.contextWindow !== "number" ||
    !Number.isSafeInteger(model.contextWindow) ||
    model.contextWindow <= 0 ||
    typeof model.maxTokens !== "number" ||
    !Number.isSafeInteger(model.maxTokens) ||
    model.maxTokens <= 0 ||
    !Array.isArray(model.input) ||
    model.input.length === 0 ||
    !model.input.every((kind) => kind === "text" || kind === "image")
  ) {
    return undefined
  }

  if (!model.cost || typeof model.cost !== "object") return undefined
  const cost = model.cost as Record<string, unknown>
  const costKeys = ["input", "output", "cacheRead", "cacheWrite"] as const
  if (
    Object.keys(cost).length !== costKeys.length ||
    !costKeys.every(
      (key) =>
        typeof cost[key] === "number" &&
        Number.isFinite(cost[key]) &&
        cost[key] >= 0,
    )
  ) {
    return undefined
  }

  let restoredThinkingLevelMap: ThinkingLevelMap | undefined
  if (model.reasoning) {
    if (!model.thinkingLevelMap || typeof model.thinkingLevelMap !== "object") {
      return undefined
    }
    const mapping = model.thinkingLevelMap as Record<string, unknown>
    if (
      Object.keys(mapping).length !== PI_THINKING_LEVELS.length ||
      !PI_THINKING_LEVELS.every(
        (level) => {
          const expected = level === "off" ? "none" : level
          return (
            Object.hasOwn(mapping, level) &&
            (mapping[level] === null || mapping[level] === expected)
          )
        },
      )
    ) {
      return undefined
    }
    restoredThinkingLevelMap = Object.fromEntries(
      PI_THINKING_LEVELS.map((level) => [level, mapping[level] as string | null]),
    )
  } else if (model.thinkingLevelMap !== undefined) {
    return undefined
  }

  return {
    id: model.id,
    name: model.name,
    api: "openai-responses",
    provider: PROVIDER_ID,
    baseUrl,
    reasoning: model.reasoning,
    ...(restoredThinkingLevelMap
      ? { thinkingLevelMap: restoredThinkingLevelMap }
      : {}),
    input: [...model.input] as RouterModel["input"],
    cost: {
      input: cost.input as number,
      output: cost.output as number,
      cacheRead: cost.cacheRead as number,
      cacheWrite: cost.cacheWrite as number,
    },
    contextWindow: model.contextWindow,
    maxTokens: model.maxTokens,
  }
}

function readModelCache(baseUrl: string, apiKey: string): RouterModel[] {
  try {
    const expectedCredentialIdentity = cacheCredentialIdentity(apiKey)
    if (expectedCredentialIdentity === undefined) return []
    const serialized = readPrivateFile(
      join(piHome(), MODEL_CACHE_FILE),
      MAX_MODEL_CACHE_BYTES,
    )
    if (serialized === undefined) return []
    const parsed: unknown = JSON.parse(serialized)
    if (!parsed || typeof parsed !== "object") return []
    const cache = parsed as {
      version?: unknown
      baseUrl?: unknown
      credentialIdentity?: unknown
      models?: unknown
    }
    if (
      cache.version !== MODEL_CACHE_VERSION ||
      cache.baseUrl !== baseUrl ||
      cache.credentialIdentity !== expectedCredentialIdentity ||
      !Array.isArray(cache.models)
    ) {
      return []
    }
    const models = cache.models.map((model) => cachedRouterModel(model, baseUrl))
    if (!models.every((model): model is RouterModel => model !== undefined)) {
      return []
    }
    if (new Set(models.map((model) => model.id)).size !== models.length) return []
    return models
  } catch {
    return []
  }
}

function writeModelCache(
  baseUrl: string,
  apiKey: string,
  models: readonly RouterModel[],
): void {
  const persistentCredentialIdentity = cacheCredentialIdentity(apiKey)
  if (persistentCredentialIdentity === undefined) return
  const home = piHome()
  mkdirSync(home, { recursive: true, mode: 0o700 })
  const path = join(home, MODEL_CACHE_FILE)
  atomicWritePrivate(
    path,
    JSON.stringify({
      version: MODEL_CACHE_VERSION,
      baseUrl,
      credentialIdentity: persistentCredentialIdentity,
      models,
    }),
  )
}

async function discoverModels(
  baseUrl: string,
  apiKey: string,
  signal?: AbortSignal,
): Promise<RouterModel[]> {
  const discovered = await discoverRouterModels({
    baseURL: baseUrl,
    apiKey,
    ...(signal ? { signal } : {}),
  })
  return discovered.map((model) => toPiModel(model, baseUrl))
}

export default async function registerRouterProvider(pi: ExtensionAPI): Promise<void> {
  // The environment still wins, so a one-off run against another stack does
  // not require rewriting the file.
  const baseUrl = normalizeBaseURL(
    firstEnvironmentValue(BASE_URL_ENVS) ?? configuredBaseURL() ?? DEFAULT_BASE_URL,
  )

  // Resolve Router's credential through Pi's own auth machinery without
  // refreshing any provider. The private cache is scoped to both endpoint and
  // credential, so switching stacks or businesses can never restore models
  // from the previous caller while the fresh catalog is still loading.
  let apiKey: string | undefined
  let startupCredentialIdentity: string | undefined
  let startupModels: RouterModel[] = []
  let restoredCatalog = false
  const catalogGeneration: CatalogGeneration = { current: 0 }
  try {
    if (!supportsRuntimeModelBootstrap(PI_VERSION)) {
      throw new Error("Pi host does not support isolated model bootstrap")
    }
    apiKey = await routerApiKey()
    if (!apiKey) throw new Error("Ramp Router credential is unavailable")
    startupCredentialIdentity = credentialIdentity(apiKey)
    startupModels = readModelCache(baseUrl, apiKey)
    restoredCatalog = startupModels.length > 0

    // Match Pi's own network policy exactly: the CLI implements --offline by
    // defining PI_OFFLINE, and its ModelRuntime treats any defined value as
    // disabled. Only an uncached online launch waits, under a startup-specific
    // deadline; cached launches refresh after registration below.
    if (startupModels.length === 0 && process.env.PI_OFFLINE === undefined) {
      startupModels = await discoverModels(
        baseUrl,
        apiKey,
        AbortSignal.timeout(STARTUP_DISCOVERY_TIMEOUT_MS),
      )
      if (!(await routerCredentialIsCurrent(apiKey))) {
        startupModels = []
        apiKey = undefined
        throw new Error("Ramp Router credential changed during discovery")
      }
      if (startupModels.length > 0) writeModelCache(baseUrl, apiKey, startupModels)
    }
  } catch {
    // A malformed local store or unavailable Router must not prevent Pi from
    // starting with another provider.
  }

  const shouldRefreshInBackground = Boolean(
    apiKey &&
    restoredCatalog &&
    process.env.PI_OFFLINE === undefined,
  )
  // Reserve the startup refresh before registration. If Pi immediately starts
  // a host refresh while registering this provider, that newer refresh bumps
  // the shared generation and wins deterministically.
  const backgroundGeneration = shouldRefreshInBackground
    ? ++catalogGeneration.current
    : undefined

  const { registeredProvider, publishBackground } = routerProvider(
    baseUrl,
    startupModels,
    catalogGeneration,
    startupCredentialIdentity,
  )
  pi.registerProvider(registeredProvider)
  registerUsageWidget(pi, { baseURL: baseUrl, resolveAPIKey: routerApiKey })

  // Extension factories run before session_start, which is the first point at
  // which Pi exposes its ModelRegistry. A cached startup refresh can update the
  // provider closure immediately, then ask Pi to rebuild only this provider's
  // availability snapshot once the registry exists. Never re-register here:
  // native registration triggers an unscoped refresh of every provider.
  let snapshotRegistry:
    | { refresh(options: { providers: readonly string[]; allowNetwork: boolean }): Promise<unknown> }
    | undefined
  let snapshotRefreshPending = false
  let snapshotRefreshRunning = false
  const flushSnapshotRefresh = () => {
    if (
      !snapshotRegistry ||
      !snapshotRefreshPending ||
      snapshotRefreshRunning
    ) return
    snapshotRefreshPending = false
    snapshotRefreshRunning = true
    void snapshotRegistry.refresh({
      providers: [PROVIDER_ID],
      allowNetwork: false,
    }).catch(() => {
      // Live getModels/request guards are already current. A picker snapshot
      // refresh is best-effort and the next Pi refresh will reconcile it.
    }).finally(() => {
      snapshotRefreshRunning = false
      if (snapshotRefreshPending) flushSnapshotRefresh()
    })
  }
  const requestSnapshotRefresh = () => {
    snapshotRefreshPending = true
    flushSnapshotRefresh()
  }
  pi.on?.("session_start", (_event, context) => {
    snapshotRegistry = context.modelRegistry
    flushSnapshotRefresh()
  })

  // A cached launch never waits on the network. Refresh only Router directly,
  // then replace live state under the same credential/generation fence. The
  // private cache is an optional next-launch optimization, not a prerequisite
  // for using the newer catalog in this process.
  if (apiKey && backgroundGeneration !== undefined) {
    void discoverModels(baseUrl, apiKey)
      .then(async (refreshed) => {
        if (refreshed.length > 0) {
          // Pi resolves auth again for inference. If /login or a concurrent
          // configure switched credentials while discovery was in flight,
          // publishing the old caller's catalog would make selectable models
          // disagree with the credential the request will actually use.
          if (!(await routerCredentialIsCurrent(apiKey))) return
          if (backgroundGeneration !== catalogGeneration.current) return
          const publication = publishBackground(
            backgroundGeneration,
            apiKey,
            refreshed,
          )
          if (publication === "published") requestSnapshotRefresh()
        }
      })
      .catch(() => {
        // The restored catalog remains active after a background failure.
      })
  }

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
