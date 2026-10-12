import { RAMP_CLI_VERSION_HEADER } from "./discovery.ts"

/**
 * The organization's starting model for new Pi sessions.
 *
 * A Router workspace admin can choose a model per coding harness. Router
 * resolves that choice against the workspace's model policy and reports it
 * to API-key callers as `effective_model`, null when nothing is set, the
 * model is no longer allowed, or the key is personal. Only the harness's own
 * integration can honor the promise Router's admin UI makes: fresh sessions
 * start on the organization default, sessions in progress or resumed keep
 * their model, and the user can still switch within a session.
 */
export const WORKSPACE_DEFAULT_HARNESS = "pi"
/** Reused for every session started within this window, then refetched. */
export const WORKSPACE_DEFAULT_TTL_MS = 60_000
const DEFAULT_TIMEOUT_MS = 2_000

export type WorkspaceDefaultInput = {
  usageOrigin: string
  resolveAPIKey: () => Promise<string | undefined>
  rampCliVersion?: string
  fetch?: typeof globalThis.fetch
  timeoutMs?: number
}

/**
 * Fetch the organization default, reporting nothing rather than failing.
 *
 * The default is a bonus on top of a working setup: a Router that is down,
 * slow, older than this endpoint, or lists only other harnesses must all read
 * as "no default" rather than an error surfaced into a session.
 */
export async function fetchWorkspaceDefault(
  input: WorkspaceDefaultInput,
): Promise<string | undefined> {
  const fetcher = input.fetch ?? globalThis.fetch
  const origin = input.usageOrigin.replace(/\/+$/, "")
  try {
    const apiKey = await input.resolveAPIKey()
    if (!apiKey) return undefined
    const response = await fetcher(`${origin}/self-service/coding-agent-settings`, {
      headers: {
        authorization: `Bearer ${apiKey}`,
        ...(input.rampCliVersion
          ? { [RAMP_CLI_VERSION_HEADER]: input.rampCliVersion }
          : {}),
      },
      signal: AbortSignal.timeout(input.timeoutMs ?? DEFAULT_TIMEOUT_MS),
    })
    if (!response.ok) return undefined
    const payload: unknown = await response.json()
    if (!payload || typeof payload !== "object") return undefined
    const harnesses = (payload as { harnesses?: unknown }).harnesses
    if (!harnesses || typeof harnesses !== "object") return undefined
    const entry = (harnesses as Record<string, unknown>)[WORKSPACE_DEFAULT_HARNESS]
    if (!entry || typeof entry !== "object") return undefined
    const model = (entry as { effective_model?: unknown }).effective_model
    return typeof model === "string" && model ? model : undefined
  } catch {
    return undefined
  }
}

export type WorkspaceDefaultCache = {
  /** The current default, fetched at most once per TTL window per process. */
  get(): Promise<string | undefined>
  /** Start a fetch now so the first session finds the answer ready. */
  prefetch(): void
}

export function workspaceDefaultCache(
  input: WorkspaceDefaultInput & { ttlMs?: number; now?: () => number },
): WorkspaceDefaultCache {
  const ttl = input.ttlMs ?? WORKSPACE_DEFAULT_TTL_MS
  const now = input.now ?? Date.now
  let inFlight: Promise<string | undefined> | undefined
  let cached: { at: number; value: string | undefined } | undefined
  const get = () => {
    if (cached && now() - cached.at < ttl) return Promise.resolve(cached.value)
    if (!inFlight) {
      inFlight = fetchWorkspaceDefault(input).then((value) => {
        cached = { at: now(), value }
        inFlight = undefined
        return value
      })
    }
    return inFlight
  }
  return {
    get,
    prefetch: () => {
      void get()
    },
  }
}

/** The parts of Pi's `session_start` event and context this module reads. */
export type SessionStartLike = {
  reason: "startup" | "reload" | "new" | "resume" | "fork"
}
export type ModelLike = { provider: string; id: string }
export type SessionStartContextLike = {
  sessionManager: { getEntries(): ReadonlyArray<{ type: string }> }
  modelRegistry: { find(provider: string, modelId: string): ModelLike | undefined }
  /** Pi exposes this as a live getter: it follows `/model` switches. */
  readonly model: ModelLike | undefined
  /** False in `--print`/`--mode json`, where the prompt is sent right after start. */
  hasUI?: boolean
}

function sameModel(a: ModelLike | undefined, b: ModelLike | undefined): boolean {
  return a?.provider === b?.provider && a?.id === b?.id
}

/**
 * Entries that mean a conversation already exists. Pi records the starting
 * model and thinking level in every session as it opens, so only messages
 * tell a continued conversation from a fresh one.
 */
const CONTINUED_ENTRY_TYPES = new Set(["message"])

function hasConversation(context: Pick<SessionStartContextLike, "sessionManager">): boolean {
  return context.sessionManager
    .getEntries()
    .some((entry) => CONTINUED_ENTRY_TYPES.has(entry.type))
}

/**
 * Whether this `session_start` opens a brand-new conversation.
 *
 * Pi names the reason: `new` is `/new`; `resume`, `fork`, and `reload`
 * continue a conversation and keep its model. `startup` covers both a fresh
 * launch and `pi --continue`/`--resume`, told apart by whether the loaded
 * session already holds messages. An explicit `--model` on
 * the command line is the user's choice for this launch, so it stands.
 */
export function startsNewConversation(
  event: SessionStartLike,
  context: Pick<SessionStartContextLike, "sessionManager">,
  argv: readonly string[] = process.argv,
): boolean {
  if (event.reason === "new") return true
  if (event.reason !== "startup") return false
  if (argv.includes("--model")) return false
  return !hasConversation(context)
}

export type ApplyWorkspaceDefaultInput = {
  providerId: string
  defaults: Pick<WorkspaceDefaultCache, "get">
  setModel: (model: ModelLike) => Promise<boolean>
  argv?: readonly string[]
}

/**
 * A `session_start` handler that starts new conversations on the
 * organization default, without touching Pi's configured default for later
 * sessions. Interactively the handler returns at once and the switch lands
 * when the (usually prefetched) answer is in; a model the user picks before
 * then is a choice for this session and stands, and so does the model a
 * conversation already under way (a prompt passed on the command line, say)
 * started on. Without a UI the prompt is sent as soon as start-up
 * completes, so there the handler holds start-up for the answer, bounded by
 * the fetch's own short timeout. A default Pi's registry does not currently
 * offer for this provider is ignored.
 */
export function applyWorkspaceDefaultOnSessionStart(input: ApplyWorkspaceDefaultInput) {
  return (event: SessionStartLike, context: SessionStartContextLike): Promise<void> => {
    if (!startsNewConversation(event, context, input.argv)) return Promise.resolve()
    const starting = context.model
    const applied = input.defaults
      .get()
      .then(async (modelId) => {
        if (!modelId) return
        const model = context.modelRegistry.find(input.providerId, modelId)
        if (!model) return
        if (!sameModel(context.model, starting)) return
        if (sameModel(starting, model)) return
        if (hasConversation(context)) return
        await input.setModel(model)
      })
      .catch(() => {
        // The session simply keeps the model Pi gave it.
      })
    return context.hasUI === false ? applied : Promise.resolve()
  }
}
