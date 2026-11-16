import assert from "node:assert/strict"
import { mkdtempSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { describe, it } from "node:test"

import { PICK_SEEN_FILE, newInAppPick, openCodeStateDir } from "../src/in-app-pick.ts"

function stateDir({ recent, seen }) {
  const dir = mkdtempSync(join(tmpdir(), "opencode-state-"))
  if (recent !== undefined) {
    writeFileSync(join(dir, "model.json"), JSON.stringify({ recent, favorite: [], variant: {} }))
  }
  if (seen !== undefined) writeFileSync(join(dir, PICK_SEEN_FILE), JSON.stringify({ seen }))
  return dir
}

const pick = (modelID, providerID = "ramp-router") => ({ providerID, modelID })

describe("newInAppPick", () => {
  it("reports a Router model picked since Ramp CLI last looked", () => {
    const dir = stateDir({ recent: [pick("gpt-7-sol"), pick("gpt-6.1-sol")], seen: "ramp-router/gpt-6.1-sol" })
    assert.equal(newInAppPick("ramp-router", dir), "gpt-7-sol")
  })

  it("ignores a pick Ramp CLI already accounted for", () => {
    const dir = stateDir({ recent: [pick("gpt-7-sol")], seen: "ramp-router/gpt-7-sol" })
    assert.equal(newInAppPick("ramp-router", dir), undefined)
  })

  it("ignores another provider's model", () => {
    const dir = stateDir({ recent: [pick("gpt-7", "openai")], seen: "ramp-router/gpt-6.1-sol" })
    assert.equal(newInAppPick("ramp-router", dir), undefined)
  })

  it("takes no pick before Ramp CLI has looked, or without OpenCode state", () => {
    assert.equal(newInAppPick("ramp-router", stateDir({ recent: [pick("gpt-7-sol")] })), undefined)
    assert.equal(newInAppPick("ramp-router", stateDir({ seen: null })), undefined)
    assert.equal(newInAppPick("ramp-router", stateDir({ recent: [pick("gpt-7-sol")], seen: null })), "gpt-7-sol")
  })

  it("follows XDG_STATE_HOME", () => {
    assert.equal(openCodeStateDir({ XDG_STATE_HOME: "/x/state" }), join("/x/state", "opencode"))
  })
})
