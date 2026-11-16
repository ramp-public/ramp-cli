import { readFileSync } from "node:fs"
import { homedir } from "node:os"
import { join } from "node:path"

/**
 * The model the user last picked inside OpenCode, when Router hasn't
 * accounted for it yet.
 *
 * OpenCode moves a model to the front of `recent` in its state file
 * `model.json` whenever the user picks one (the model dialog, `--model`).
 * Ramp CLI records the front entry it last accounted for in
 * `ramp-router-model-pick.json` beside it, and saves a newer Router pick as
 * the user's own default on its next configure or refresh. Reading the same
 * two files here lets the very next launch start on that pick, before the CLI
 * has run. Any read failure, or no record yet, means no pick.
 */
export const PICK_SEEN_FILE = "ramp-router-model-pick.json"

export function openCodeStateDir(env: NodeJS.ProcessEnv = process.env): string {
  const configured = env.XDG_STATE_HOME?.trim()
  return join(configured || join(homedir(), ".local", "state"), "opencode")
}

function readJSON(path: string): unknown {
  try {
    return JSON.parse(readFileSync(path, "utf8"))
  } catch {
    return undefined
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
}

export function latestPick(stateDir: string): string | undefined {
  const state = readJSON(join(stateDir, "model.json"))
  const recent = isRecord(state) ? state.recent : undefined
  const first = Array.isArray(recent) ? recent[0] : undefined
  if (!isRecord(first)) return undefined
  const { providerID, modelID } = first
  return typeof providerID === "string" && providerID &&
    typeof modelID === "string" && modelID
    ? `${providerID}/${modelID}`
    : undefined
}

/** The Router model id picked in OpenCode since Ramp CLI last looked. */
export function newInAppPick(
  providerID: string,
  stateDir: string = openCodeStateDir(),
): string | undefined {
  const latest = latestPick(stateDir)
  if (!latest) return undefined
  const record = readJSON(join(stateDir, PICK_SEEN_FILE))
  if (!isRecord(record) || !("seen" in record)) return undefined
  if (record.seen === latest) return undefined
  const prefix = `${providerID}/`
  return latest.startsWith(prefix) ? latest.slice(prefix.length) : undefined
}
