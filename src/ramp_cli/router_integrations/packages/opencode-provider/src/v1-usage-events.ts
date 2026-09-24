import type { TuiPluginApi } from "@opencode-ai/plugin/tui"

/** V1 can update the assistant message before (or without) delivering session.idle. */
export function subscribeV1UsageEvents(
  api: Pick<TuiPluginApi, "event" | "lifecycle">,
  providerID: string,
  onIdle: (sessionID: string) => void,
): void {
  const stopIdle = api.event.on("session.idle", (event) => {
    onIdle(event.properties.sessionID)
  })
  const stopStatus = api.event.on("session.status", (event) => {
    if (event.properties.status.type === "idle") onIdle(event.properties.sessionID)
  })
  const stopMessage = api.event.on("message.updated", (event) => {
    const message = event.properties.info
    if (message.role === "assistant" && message.providerID === providerID && message.time.completed) {
      onIdle(event.properties.sessionID)
    }
  })
  api.lifecycle.onDispose(stopIdle)
  api.lifecycle.onDispose(stopStatus)
  api.lifecycle.onDispose(stopMessage)
}
