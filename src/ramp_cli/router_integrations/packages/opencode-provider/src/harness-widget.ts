import { readFileSync } from "node:fs"
import { homedir } from "node:os"
import { join } from "node:path"

/** Reads ramp-cli's router_settings.py file: only a literal false disables. */
export function harnessWidgetEnabled(): boolean {
  try {
    const settings: unknown = JSON.parse(
      readFileSync(join(homedir(), ".config", "ramp", "router-settings.json"), "utf8"),
    )
    return (settings as { harness_widget_enabled?: unknown } | null)?.harness_widget_enabled !== false
  } catch {
    return true
  }
}
