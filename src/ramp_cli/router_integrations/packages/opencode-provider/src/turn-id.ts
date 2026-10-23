export const TURN_ID_HEADER = "X-Turn-Id"

const MAX_SESSIONS = 1_000
const MAX_TURN_ID_LENGTH = 256

function validID(value: unknown): value is string {
  return typeof value === "string" && value.length > 0 && value.length <= MAX_TURN_ID_LENGTH
}

/** Add OpenCode's user-message ID as Router's turn ID when it is safe to send. */
export function applyTurnHeader(headers: Record<string, string>, turnID: unknown): void {
  if (validID(turnID)) headers[TURN_ID_HEADER] = turnID
}

/** Bounded session -> current user-message map for OpenCode v2's prompt and model-request hooks. */
export function turnTracker() {
  const turns = new Map<string, string>()
  return {
    record(sessionID: unknown, messageID: unknown): void {
      if (!validID(sessionID) || !validID(messageID)) return
      turns.delete(sessionID)
      turns.set(sessionID, messageID)
      if (turns.size > MAX_SESSIONS) {
        const oldest = turns.keys().next().value
        if (oldest !== undefined) turns.delete(oldest)
      }
    },
    current(sessionID: unknown): string | undefined {
      return validID(sessionID) ? turns.get(sessionID) : undefined
    },
  }
}
