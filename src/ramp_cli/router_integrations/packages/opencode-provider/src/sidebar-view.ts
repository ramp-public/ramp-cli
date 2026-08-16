import { formatUSD, referenceModelLabel } from "./usage.ts"
import type { SessionUsage } from "./usage.ts"

// The Claude Code status line draws 24-cell bars; narrower surfaces shrink,
// but below 10 cells the two lengths stop reading as a comparison.
export const MAX_BAR_WIDTH = 24
export const MIN_BAR_WIDTH = 10
// OpenCode gives the session sidebar a fixed 42 columns; after its padding
// and gap the content area is 36, the same width the built-in sidebar
// sections lay out against.
export const DEFAULT_AVAILABLE_WIDTH = 36

export type SidebarBarRow = {
  kind: "ramp" | "reference"
  /** Padded to the shared label column so both bars start at the same x. */
  label: string
  /** Filled cells, drawn as █ in the series color. */
  filled: number
  /** Remaining cells, drawn as ░ so both bars span the same fixed width. */
  empty: number
  cost: string
}

/**
 * One session's usage in the exact shape the Claude Code status line renders,
 * top to bottom: Switchyard state, the routed-model header with the delta
 * beside it, then the two fixed-width bars scaled to the larger figure.
 */
export type SidebarUsageView = {
  switchyardEnabled: boolean
  /** Header line, e.g. "Routed to: GPT-5.4 via OpenAI". */
  routedTo?: string
  /** Header suffix, e.g. "-63% vs Claude Opus 5"; only with a reference cost. */
  delta?: string
  bars: SidebarBarRow[]
}

function filledCells(value: number, peak: number, width: number): number {
  if (value <= 0 || peak <= 0) return 0
  return Math.min(width, Math.round((value / peak) * width))
}

/**
 * Render one session's usage into the Claude Code status line's shape.
 *
 * A session with no recorded requests has no cost story to tell, so it shows
 * nothing rather than a meaningless "$0.00 vs $0.00".
 */
export function sidebarUsageView(
  usage: SessionUsage,
  options?: {
    /** Columns the whole bar row may occupy, label and cost included. */
    availableWidth?: number
    /** Resolve a Router model name to its display name, when known. */
    modelDisplayName?: (modelID: string) => string | undefined
  },
): SidebarUsageView | undefined {
  if (usage.requestCount <= 0) return undefined

  const compared =
    usage.referenceModel !== undefined &&
    usage.referenceCostUSD !== undefined &&
    usage.referenceCostUSD > 0
  const referenceLabel = compared
    ? referenceModelLabel(usage.referenceModel!)
    : undefined
  const labelWidth = Math.max("Ramp".length, referenceLabel?.length ?? 0)
  const peak = Math.max(usage.spendUSD, compared ? usage.referenceCostUSD! : 0)
  const costs = [
    formatUSD(usage.spendUSD),
    ...(compared ? [formatUSD(usage.referenceCostUSD!)] : []),
  ]
  const costWidth = Math.max(...costs.map((cost) => cost.length))
  // Fit the whole row — label, bar, cost, and their two separating spaces —
  // inside the sidebar's fixed content width, capped at Claude Code's 24
  // cells. Below the floor the bars stop reading as a comparison, so a
  // pathologically long label clips rather than shrinking them further.
  const available = Math.floor(
    options?.availableWidth ?? DEFAULT_AVAILABLE_WIDTH,
  )
  const width = Math.max(
    MIN_BAR_WIDTH,
    Math.min(MAX_BAR_WIDTH, available - labelWidth - costWidth - 2),
  )

  const bars: SidebarBarRow[] = []
  const rampFilled = filledCells(usage.spendUSD, peak, width)
  bars.push({
    kind: "ramp",
    label: "Ramp".padEnd(labelWidth),
    filled: rampFilled,
    empty: width - rampFilled,
    cost: formatUSD(usage.spendUSD),
  })
  let delta: string | undefined
  if (compared) {
    const referenceFilled = filledCells(usage.referenceCostUSD!, peak, width)
    bars.push({
      kind: "reference",
      label: referenceLabel!.padEnd(labelWidth),
      filled: referenceFilled,
      empty: width - referenceFilled,
      cost: formatUSD(usage.referenceCostUSD!),
    })
    const percent = Math.round(
      ((usage.spendUSD - usage.referenceCostUSD!) / usage.referenceCostUSD!) * 100,
    )
    delta = `${percent > 0 ? "+" : ""}${percent}% vs ${referenceLabel}`
  }

  let routedTo: string | undefined
  if (usage.lastModel !== undefined) {
    // The figure comes from usage-event ingestion, so it names the last model
    // Router has recorded, which can trail the live turn by a few seconds.
    const display =
      options?.modelDisplayName?.(usage.lastModel) ??
      (usage.lastModelProvider
        ? `${usage.lastModel} via ${usage.lastModelProvider}`
        : usage.lastModel)
    routedTo = `Routed to: ${display}`
  }

  return {
    switchyardEnabled: usage.switchyardEnabled,
    ...(routedTo !== undefined ? { routedTo } : {}),
    ...(delta !== undefined ? { delta } : {}),
    bars,
  }
}
