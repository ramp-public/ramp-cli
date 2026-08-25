/** @jsxImportSource @opentui/solid */
import { For, Show, createMemo, createSignal } from "solid-js"
import type {
  TuiPlugin,
  TuiPluginModule,
  TuiSlotContext,
} from "@opencode-ai/plugin/tui"

import {
  resolveAPIKey,
  resolveProviderID,
  resolveUsageOrigin,
  routerPluginOptions,
} from "./options.ts"
import { fetchSessionUsage } from "./usage.ts"
import type { SessionUsage } from "./usage.ts"
import { sidebarUsageView } from "./sidebar-view.ts"
import type { SidebarBarRow } from "./sidebar-view.ts"
import { showRampCLIUpdateNotice } from "./update-notice.ts"

const USAGE_FETCH_TIMEOUT_MS = 3_000
// One idle event ends every turn, but streams of them can land close together
// while the user reads. A short gap keeps that from re-querying Router
// without ever showing a stale figure for long.
const USAGE_REFRESH_MIN_INTERVAL_MS = 5_000
// Built-in sections use orders 100-400; the repo path lives in the separate
// sidebar_footer slot, so anything above 400 sits below them all.
const SIDEBAR_ORDER = 450

// The Claude Code status line's exact palette.
const NVIDIA_GREEN = "#76B900"
const RAMP_YELLOW = "#E4F222"
const ANTHROPIC_TERRACOTTA = "#D97757"

function barFill(row: SidebarBarRow): string {
  return row.kind === "ramp" ? RAMP_YELLOW : ANTHROPIC_TERRACOTTA
}

/**
 * The Ramp Router sidebar section, rendering the Claude Code status line's
 * exact layout: Switchyard state first, the routed-model header with the
 * delta beside it, then the session cost against the reference model as two
 * fixed-width bars sharing one scale.
 *
 * Everything here is best-effort and silent on failure: a session Router has
 * not seen, a Router that is down, or a TUI without our provider all render
 * as no section rather than an error, and nothing rendered or thrown may
 * include the credential.
 */
const RouterTui: TuiPlugin = async (api, rawOptions) => {
  const options = routerPluginOptions(rawOptions)
  const providerID = resolveProviderID(options)
  const [updateNotice, setUpdateNotice] = createSignal<string>()

  // Deliberately not awaited: the bounded, fail-open hook reads cached state
  // while the TUI continues becoming ready.
  void showRampCLIUpdateNotice(api.ui.toast, options).then(setUpdateNotice)

  const [usages, setUsages] = createSignal<Record<string, SessionUsage>>({})
  const inFlight = new Set<string>()
  const lastFetched = new Map<string, number>()

  /** Resolve a Router model name to the display name the picker shows. */
  const modelDisplayName = (modelID: string): string | undefined => {
    try {
      for (const provider of api.state.provider) {
        if (provider.id !== providerID) continue
        const model = provider.models?.[modelID]
        if (model && typeof model.name === "string" && model.name) {
          return model.name
        }
      }
    } catch {
      // The raw Router name still identifies the model.
    }
    return undefined
  }

  /**
   * Whether the session's latest completed turn routed through this provider.
   *
   * The idle event names every session, not just ours, and the usage query
   * carries the session id to Router with the credential. Only sessions this
   * provider actually served may be looked up; a session OpenCode has no
   * assistant turns for reads as not ours rather than as a guess.
   */
  const isRouterSession = (sessionID: string): boolean => {
    try {
      const messages = api.state.session.messages(sessionID)
      for (let index = messages.length - 1; index >= 0; index -= 1) {
        const message = messages[index]
        if (message?.role !== "assistant") continue
        return message.providerID === providerID
      }
    } catch {
      // An unreadable session is not ours to query.
    }
    return false
  }

  const refresh = async (sessionID: string): Promise<void> => {
    if (!sessionID || inFlight.has(sessionID)) return
    if (!isRouterSession(sessionID)) return
    // One gate for every trigger, idle events included: usage ingestion lags
    // a finished turn by a few seconds anyway, so a fetch suppressed here is
    // caught by the next idle rather than lost.
    const last = lastFetched.get(sessionID)
    if (last !== undefined && Date.now() - last < USAGE_REFRESH_MIN_INTERVAL_MS) {
      return
    }
    inFlight.add(sessionID)
    try {
      const apiKey = resolveAPIKey(options)
      if (!apiKey) return
      const usage = await fetchSessionUsage({
        usageOrigin: resolveUsageOrigin(options),
        apiKey,
        sessionID,
        timeoutMs: USAGE_FETCH_TIMEOUT_MS,
      })
      lastFetched.set(sessionID, Date.now())
      setUsages((previous) => {
        if (usage) return { ...previous, [sessionID]: usage }
        if (!(sessionID in previous)) return previous
        const next = { ...previous }
        delete next[sessionID]
        return next
      })
    } catch {
      // The usage display is an extra; the sidebar keeps working without it.
    } finally {
      inFlight.delete(sessionID)
    }
  }

  // A turn's usage lands in Router through event ingestion, so the idle event
  // marks the earliest moment a fresh figure could exist. Show the last-known
  // value until then; never a spinner.
  const unsubscribe = api.event.on("session.idle", (event) => {
    void refresh(event.properties.sessionID)
  })
  api.lifecycle.onDispose(unsubscribe)

  api.slots.register({
    order: SIDEBAR_ORDER,
    slots: {
      sidebar_content: (ctx, props) => {
        // First paint of a session that has not idled yet, such as reopening
        // an existing conversation. The interval gate keeps this from
        // re-querying on every re-render.
        void refresh(props.session_id)
        const view = createMemo(() => {
          const usage = usages()[props.session_id]
          if (!usage) return undefined
          // The sidebar's content width is fixed, so the bars fit it by
          // construction rather than tracking the terminal, whose width says
          // nothing about the slot's.
          return sidebarUsageView(usage, { modelDisplayName })
        })
        const theme = () => ctx.theme.current
        return (
          <Show when={view() || updateNotice()}>
            <box flexDirection="column">
              <Show when={view()}>
                {(v) => (
                  <>
                    <Show when={v().switchyardEnabled}>
                      <text fg={NVIDIA_GREEN}>Switchyard enabled</text>
                      <text> </text>
                    </Show>
                    <Show when={v().routedTo || v().delta}>
                      <box flexDirection="row" gap={2}>
                        <Show when={v().routedTo}>
                          <text fg={theme().text}>
                            <b>{v().routedTo}</b>
                          </text>
                        </Show>
                        <Show when={v().delta}>
                          <text fg={RAMP_YELLOW}>{v().delta}</text>
                        </Show>
                      </box>
                    </Show>
                    <For each={v().bars}>
                      {(row) => (
                        <box flexDirection="row">
                          <text fg={theme().text}>{`${row.label} `}</text>
                          <text fg={barFill(row)}>{"█".repeat(row.filled)}</text>
                          <text fg={theme().textMuted}>
                            {"░".repeat(row.empty)}
                          </text>
                          <text fg={theme().text}>{` ${row.cost}`}</text>
                        </box>
                      )}
                    </For>
                  </>
                )}
              </Show>
              <Show when={updateNotice()}>
                {(notice) => (
                  <>
                    <Show when={view()}>
                      <text> </text>
                    </Show>
                    <text fg={RAMP_YELLOW} wrapMode="word">
                      {notice()}
                    </text>
                  </>
                )}
              </Show>
            </box>
          </Show>
        )
      },
    },
  })
}

// Referenced so the context type this file relies on stays typechecked.
export type RouterSidebarContext = Readonly<TuiSlotContext>

const plugin = {
  id: "@ramp/router-opencode-provider",
  tui: RouterTui,
} satisfies TuiPluginModule

export default plugin
