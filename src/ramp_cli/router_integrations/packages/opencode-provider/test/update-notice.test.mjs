import assert from "node:assert/strict"
import { describe, it, mock } from "node:test"

import { showRampCLIUpdateNotice } from "../src/update-notice.ts"

describe("OpenCode update notice", () => {
  it("toasts only a valid cached notice and fails open otherwise", async () => {
    const toast = mock.fn()
    const options = { rampExecutable: "/opt/ramp-cli/bin/ramp" }
    const notice =
      "Ramp CLI v99.0.0 is available — run `ramp update` for the latest Router features."

    const pending = await showRampCLIUpdateNotice(
      toast,
      options,
      async (executable) => {
        assert.equal(executable, "/opt/ramp-cli/bin/ramp")
        return JSON.stringify({ updateNotice: notice })
      },
    )
    const current = await showRampCLIUpdateNotice(toast, options, async () => "")
    const failed = await showRampCLIUpdateNotice(toast, options, async () => {
      throw new Error("missing executable")
    })

    assert.equal(pending, notice)
    assert.equal(current, undefined)
    assert.equal(failed, undefined)
    assert.equal(toast.mock.callCount(), 1)
    assert.deepEqual(toast.mock.calls[0].arguments, [
      { variant: "warning", message: notice },
    ])
  })
})
