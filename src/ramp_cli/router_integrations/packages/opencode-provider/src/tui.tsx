/** @jsxImportSource @opentui/solid */
import { For, Show, createMemo, createSignal } from "solid-js"
import type { JSX } from "@opentui/solid"
import type {
  TuiPlugin,
  TuiPluginModule,
  TuiSlotContext,
} from "@opencode-ai/plugin/tui"
import type { Plugin as TuiPluginV2 } from "@opencode/plugin/tui"

import {
  resolveProviderID,
  routerPluginOptions,
} from "./options.ts"
import { createUsageTracker } from "./usage-tracker.ts"
import { subscribeV1UsageEvents } from "./v1-usage-events.ts"
import type { SidebarBarRow, SidebarUsageView } from "./sidebar-view.ts"
import { showRampCLIUpdateNotice } from "./update-notice.ts"

const PLUGIN_ID = "@ramp/router-opencode-provider"
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

type SectionProps = {
  view: () => SidebarUsageView | undefined
  updateNotice: () => string | undefined
  /** Theme colors, in whichever form the host's theme exposes them. */
  text: () => unknown
  textMuted: () => unknown
}

/**
 * The Ramp Router sidebar section, rendering the Claude Code status line's
 * exact layout: Switchyard state first, the routed-model header with the
 * delta beside it, then the session cost against the reference model as two
 * fixed-width bars sharing one scale.
 */
function RouterSection(props: SectionProps): JSX.Element {
  const text = () => props.text() as string
  const textMuted = () => props.textMuted() as string
  return (
    <Show when={props.view() || props.updateNotice()}>
      <box flexDirection="column">
        <Show when={props.view()}>
          {(v) => (
            <>
              <Show when={v().switchyardEnabled}>
                <text fg={NVIDIA_GREEN}>Switchyard enabled</text>
                <text> </text>
              </Show>
              <Show when={v().routedTo || v().delta}>
                <box flexDirection="row" gap={2}>
                  <Show when={v().routedTo}>
                    <text fg={text()}>
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
                    <text fg={text()}>{`${row.label} `}</text>
                    <text fg={barFill(row)}>{"█".repeat(row.filled)}</text>
                    <text fg={textMuted()}>{"░".repeat(row.empty)}</text>
                    <text fg={text()}>{` ${row.cost}`}</text>
                  </box>
                )}
              </For>
            </>
          )}
        </Show>
        <Show when={props.updateNotice()}>
          {(notice) => (
            <>
              <Show when={props.view()}>
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
}

/**
 * The OpenCode v1 TUI plugin, loaded from tui.json.
 *
 * Everything here is best-effort and silent on failure: a session Router has
 * not seen, a Router that is down, or a TUI without our provider all render
 * as no section rather than an error, and nothing rendered or thrown may
 * include the credential.
 */
const RouterTuiV1: TuiPlugin = async (api, rawOptions) => {
  const options = routerPluginOptions(rawOptions)
  const providerID = resolveProviderID(options)
  const [updateNotice, setUpdateNotice] = createSignal<string>()

  // Deliberately not awaited: the bounded, fail-open hook reads cached state
  // while the TUI continues becoming ready.
  void showRampCLIUpdateNotice(api.ui.toast, options).then(setUpdateNotice)

  const tracker = createUsageTracker({
    options,
    modelDisplayName: (modelID) => {
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
    },
    isRouterSession: (sessionID) => {
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
      return undefined
    },
  })

  // V1 may publish a completed assistant or idle status without delivering
  // session.idle to the TUI. Any completion starts the bounded ingestion retry.
  subscribeV1UsageEvents(api, providerID, tracker.onIdle)
  api.lifecycle.onDispose(tracker.dispose)

  api.slots.register({
    order: SIDEBAR_ORDER,
    slots: {
      sidebar_content: (ctx, props) => {
        // First paint of a session that has not idled yet, such as reopening
        // an existing conversation. The interval gate keeps this from
        // re-querying on every re-render.
        void tracker.refresh(props.session_id)
        const view = createMemo(() => tracker.view(props.session_id))
        return (
          <RouterSection
            view={view}
            updateNotice={updateNotice}
            text={() => ctx.theme.current.text}
            textMuted={() => ctx.theme.current.textMuted}
          />
        )
      },
    },
  })
}

/**
 * The OpenCode v2 CLI plugin, loaded from cli.json (or automatically for the
 * package listed in opencode.json). Same section, same rules, v2's data API.
 */
const setupRouterTuiV2: TuiPluginV2.Definition["setup"] = (context) => {
  const options = routerPluginOptions(context.options)
  const providerID = resolveProviderID(options)
  const [updateNotice, setUpdateNotice] = createSignal<string>()

  void showRampCLIUpdateNotice(
    (toast) => context.ui.toast.show(toast),
    options,
  ).then(setUpdateNotice)

  const tracker = createUsageTracker({
    options,
    modelDisplayName: (modelID) => {
      try {
        const models = context.data.location.model.list(context.location) ?? []
        const model = models.find(
          (candidate) =>
            candidate.providerID === providerID && candidate.id === modelID,
        )
        return model?.name || undefined
      } catch {
        return undefined
      }
    },
    isRouterSession: (sessionID) => {
      try {
        const messages = context.data.session.message.list(sessionID)
        for (let index = messages.length - 1; index >= 0; index -= 1) {
          const message = messages[index]
          if (message?.type !== "assistant") continue
          return message.model.providerID === providerID
        }
      } catch {
        // An unreadable session is not ours to query.
      }
      return undefined
    },
    syncSession: (sessionID) => context.data.session.message.sync(sessionID),
  })

  const stopIdle = context.data.on("session.idle", (event) => {
    tracker.onIdle(event.data.sessionID)
  })
  // V2 emits execution.succeeded on the normal completion path. Its idle
  // message can reach the TUI without a session.idle event; either event
  // starts the same bounded retry sequence for delayed Router ingestion.
  const stopSucceeded = context.data.on("session.execution.succeeded", (event) => {
    tracker.onIdle(event.data.sessionID)
  })
  const unslot = context.ui.slot({
    append: "sidebar.content",
    render: ({ sessionID }) => {
      void tracker.refresh(sessionID)
      const view = createMemo(() => tracker.view(sessionID))
      return (
        <RouterSection
          view={view}
          updateNotice={updateNotice}
          text={() => context.theme.text.base}
          textMuted={() => context.theme.text.muted}
        />
      )
    },
  })

  return () => {
    stopIdle()
    stopSucceeded()
    unslot()
    tracker.dispose()
  }
}

// Referenced so the context type this file relies on stays typechecked.
export type RouterSidebarContext = Readonly<TuiSlotContext>

/**
 * One module for both generations: v1 reads `tui`, v2 reads `setup`. Neither
 * `define` helper is called because both are identity functions and
 * importing `@opencode/plugin/tui` at runtime would fail under v1.
 */
const plugin = {
  id: PLUGIN_ID,
  tui: RouterTuiV1,
  setup: setupRouterTuiV2,
} satisfies TuiPluginModule & TuiPluginV2.Definition

export default plugin
