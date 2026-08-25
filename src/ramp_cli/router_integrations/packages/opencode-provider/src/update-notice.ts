import { execFile } from "node:child_process"

import { nonEmpty } from "./options.ts"
import type { RouterPluginOptions } from "./options.ts"

const SYNC_TIMEOUT_MS = 3_000
const MAX_OUTPUT_BYTES = 64 * 1024

type Toast = (input: {
  variant: "warning"
  message: string
}) => void

type SyncRunner = (executable: string) => Promise<string | undefined>

type UpdateNoticeResponse = {
  updateNotice: string
}

function parseUpdateNotice(output: string): string | undefined {
  try {
    const parsed = JSON.parse(output) as Partial<UpdateNoticeResponse>
    return nonEmpty(parsed.updateNotice, "") || undefined
  } catch {
    return undefined
  }
}

/** Run the existing session-start sync without a shell or an unbounded child. */
export function runRampSessionSync(
  executable: string,
): Promise<string | undefined> {
  return new Promise((resolve) => {
    execFile(
      executable,
      ["router", "sync", "--hook", "--client", "opencode"],
      {
        encoding: "utf8",
        maxBuffer: MAX_OUTPUT_BYTES,
        timeout: SYNC_TIMEOUT_MS,
        windowsHide: true,
      },
      (error, stdout) => resolve(error ? undefined : stdout),
    )
  })
}

/** Surface cached update state in the TUI; every failure is a silent no-op. */
export async function showRampCLIUpdateNotice(
  toast: Toast,
  options: RouterPluginOptions,
  run: SyncRunner = runRampSessionSync,
): Promise<string | undefined> {
  try {
    const output = await run(nonEmpty(options.rampExecutable, "ramp"))
    if (!output) return undefined
    const notice = parseUpdateNotice(output)
    if (notice) toast({ variant: "warning", message: notice })
    return notice
  } catch {
    // Startup notices must never affect TUI readiness or plugin activation.
    return undefined
  }
}
