import { RAMP_CLI_VERSION_HEADER } from "./discovery.ts"

/**
 * The organization's starting model for new OpenCode sessions.
 *
 * A Router workspace admin can choose a model per coding harness. Router
 * resolves that choice against the workspace's model policy and reports it
 * to API-key callers as `effective_model`, null when nothing is set, the
 * model is no longer allowed, or the key is personal. Only the harness's own
 * integration can honor the promise Router's admin UI makes: fresh sessions
 * start on the organization default, sessions in progress or resumed keep
 * their model, and the user can still switch within a session.
 */
export const WORKSPACE_DEFAULT_HARNESS = "opencode"
/** Reused for every session created within this window, then refetched. */
export const WORKSPACE_DEFAULT_TTL_MS = 60_000
const DEFAULT_TIMEOUT_MS = 2_000

export type WorkspaceDefaultInput = {
  usageOrigin: string
  apiKey: string
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
    const response = await fetcher(`${origin}/self-service/coding-agent-settings`, {
      headers: {
        authorization: `Bearer ${input.apiKey}`,
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

/** Any OpenCode v2 event; read structurally. */
export type SessionEventLike = { readonly type: string; readonly data?: unknown }

function isNewTopLevelSession(event: SessionEventLike): boolean {
  if (event.type !== "session.created") return false
  const data = event.data
  if (!data || typeof data !== "object") return false
  const { parentID } = data as { parentID?: unknown }
  return typeof parentID !== "string" || !parentID
}

/** The parts of OpenCode's agent editor and domain this module uses. */
export type AgentLike = {
  readonly id: string
  readonly mode: "subagent" | "primary" | "all"
  readonly hidden: boolean
  model?: { id: string; providerID: string }
}
export type AgentEditorLike = {
  list(): readonly AgentLike[]
  update(id: string, update: (agent: AgentLike) => void): void
}
export type RegistrationLike = { dispose(): Promise<void> }
export type AgentDomainLike = {
  transform(callback: (editor: AgentEditorLike) => void): Promise<RegistrationLike>
  reload(): Promise<void>
  /** The agents after every transform, in the API's response envelope. */
  list(): Promise<{
    readonly data: ReadonlyArray<Pick<AgentLike, "id" | "mode" | "hidden"> & { model?: unknown }>
  }>
}

export type ModelRefLike = { readonly providerID: string; readonly id: string; readonly variant?: string }
/** A model request about to be made for a session. */
export type SessionModelRequestLike = {
  readonly sessionID: string
  readonly model: ModelRefLike
  readonly kind: string
}
type SessionLike = { readonly model?: unknown }
/** The parts of OpenCode's session domain this module uses. */
export type SessionDomainLike = {
  /** The session, bare (the plugin's domain) or in the API's response envelope. */
  get(input: { sessionID: string }): Promise<SessionLike | { readonly data: SessionLike }>
  switchModel(input: { sessionID: string; model: ModelRefLike }): Promise<unknown>
  hook(
    name: "model.request",
    callback: (input: SessionModelRequestLike) => Promise<void> | void,
  ): Promise<RegistrationLike>
}

function hasRecordedModel(found: SessionLike | { readonly data: SessionLike }): boolean {
  const session = "data" in found && found.data && typeof found.data === "object" ? found.data : found
  return Boolean((session as SessionLike).model)
}

/** Agents a user can start a session with. */
function selectable(agent: Pick<AgentLike, "mode" | "hidden">): boolean {
  return agent.mode !== "subagent" && !agent.hidden
}

export type ModelEditorLike = {
  readonly default: { set(providerID: string, modelID: string): void }
}
export type ModelDomainLike = {
  transform(callback: (editor: ModelEditorLike) => void): Promise<unknown>
  reload(): Promise<void>
}

export type ApplyWorkspaceDefaultInput = {
  agent: AgentDomainLike
  model: ModelDomainLike
  providerID: string
  /** Models this provider currently offers, by id. */
  modelIDs: ReadonlySet<string>
  defaults: Pick<WorkspaceDefaultCache, "get">
  /** The model `ramp router configure` chose, used when no organization default applies. */
  configured?: string | undefined
  /** When given, a session that runs without a recorded model keeps the one it first used. */
  session?: SessionDomainLike | undefined
  /** How often a long-running process re-reads the default; the cache TTL by default. */
  pollMs?: number
  timers?: Pick<typeof globalThis, "setInterval" | "clearInterval">
}

export type WorkspaceDefaultApplier = {
  /** The model currently given to new sessions, if any. */
  current(): string | undefined
  /** Re-read the default and, when it changed, rebuild models and agents. */
  refresh(): Promise<void>
  /**
   * Keep the default current while the process lives: re-read it on a timer
   * and as new top-level sessions are created, and reach agents defined
   * after the plugin loaded.
   */
  watch(events: AsyncIterable<SessionEventLike>): Promise<void>
}

/**
 * Start every new session on the organization default, or else on the
 * model `ramp router configure` chose.
 *
 * OpenCode's TUI picks a new session's model from, in order, a model the
 * user chose on the home screen, the agent's own model, and then the
 * configured/recent fallbacks; an existing session keeps the model recorded
 * on it. Setting the primary agents' model is therefore what reaches a new
 * interactive session without touching sessions in progress or resumed, and
 * a switch the user makes in a session stands because the session's own
 * model wins from then on. The registry default is set too, for sessions
 * created without a model (`opencode run`); a `model` in the user's own
 * config still takes precedence there, which is why configure records its
 * choice as a plugin option instead. Subagents, and an agent whose
 * definition names a model, keep it. A default the provider does not
 * currently serve is ignored.
 *
 * Transforms apply in registration order and OpenCode materializes the
 * agents of the user's config after external plugins load, so an agent
 * defined there is not in the list the first time the transform runs, and
 * a built-in agent the config turns into a subagent still reads as primary
 * when it does. When, after every transform, a selectable agent is still
 * without a model or an agent this transform filled is no longer
 * selectable, the registration is moved behind the ones that came later.
 *
 * `opencode run` without `--model` records no model on its session, and
 * OpenCode resolves such a session against the current registry default on
 * every run, so a later default would move an `opencode run --continue`
 * conversation. The model a session first makes a request with is therefore
 * recorded on it, which is what the TUI does for its own sessions.
 */
export function workspaceDefaultApplier(
  input: ApplyWorkspaceDefaultInput,
): WorkspaceDefaultApplier {
  const served = (modelID: string | undefined) =>
    modelID && input.modelIDs.has(modelID) ? modelID : undefined
  const configured = served(input.configured)
  let current = configured
  let inFlight: Promise<void> | undefined
  /** Agents the last run of the transform gave a model to. */
  let filled = new Set<string>()
  const registerAgents = () =>
    input.agent.transform((editor) => {
      filled = new Set()
      if (!current) return
      const model = { id: current, providerID: input.providerID }
      for (const agent of editor.list()) {
        if (!selectable(agent) || agent.model) continue
        editor.update(agent.id, (candidate) => {
          candidate.model = { ...model }
        })
        filled.add(agent.id)
      }
    })
  let agents = registerAgents()
  const recorded = new Set<string>()
  const keepFirstModel = async (request: SessionModelRequestLike) => {
    if (request.kind !== "primary" || recorded.has(request.sessionID)) return
    recorded.add(request.sessionID)
    try {
      if (hasRecordedModel(await input.session!.get({ sessionID: request.sessionID }))) return
      const { providerID, id, variant } = request.model
      await input.session!.switchModel({
        sessionID: request.sessionID,
        model: { providerID, id, ...(variant === undefined ? {} : { variant }) },
      })
    } catch {
      // The session goes on resolving its model as before.
    }
  }
  const registration = Promise.all([
    input.model.transform((editor) => {
      if (current) editor.default.set(input.providerID, current)
    }),
    agents,
    // An OpenCode without this hook still gets the default for new sessions.
    input.session?.hook("model.request", keepFirstModel).catch(() => undefined),
  ])
  const reachLateAgents = async () => {
    if (!current) return
    const listed = await input.agent.list()
    const unreached = listed.data.some((agent) => selectable(agent) && !agent.model)
    const overreached = listed.data.some((agent) => !selectable(agent) && filled.has(agent.id))
    if (!unreached && !overreached) return
    const previous = await agents
    await previous.dispose()
    agents = registerAgents()
    await agents
  }
  const refresh = () => {
    if (inFlight) return inFlight
    inFlight = (async () => {
      try {
        await registration
        const next = served(await input.defaults.get()) ?? configured
        if (next !== current) {
          current = next
          await input.model.reload()
          await input.agent.reload()
        }
        await reachLateAgents()
      } catch {
        // The agents keep the models OpenCode gave them.
      } finally {
        inFlight = undefined
      }
    })()
    return inFlight
  }
  return {
    current: () => current,
    refresh,
    watch: async (events) => {
      const timers = input.timers ?? globalThis
      const timer = timers.setInterval(() => void refresh(), input.pollMs ?? WORKSPACE_DEFAULT_TTL_MS)
      ;(timer as { unref?: () => void }).unref?.()
      try {
        for await (const event of events) {
          if (isNewTopLevelSession(event) || event.type === "agent.updated") void refresh()
        }
      } finally {
        timers.clearInterval(timer)
      }
    },
  }
}
