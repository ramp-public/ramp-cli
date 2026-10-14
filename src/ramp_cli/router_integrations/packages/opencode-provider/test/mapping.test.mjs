import assert from "node:assert/strict"
import { describe, it } from "node:test"

import { openCodeModalities } from "../src/mapping.ts"

describe("openCodeModalities", () => {
  it("maps Router's file input to OpenCode's pdf", () => {
    assert.deepEqual(openCodeModalities(["file", "image", "text"]), ["pdf", "image", "text"])
  })

  it("does not repeat a modality Router states twice", () => {
    assert.deepEqual(openCodeModalities(["file", "pdf", "text"]), ["pdf", "text"])
  })

  it("drops modalities OpenCode has no case for and falls back to text", () => {
    assert.deepEqual(openCodeModalities(["text", "hologram"]), ["text"])
    assert.deepEqual(openCodeModalities(["hologram"]), ["text"])
    assert.deepEqual(openCodeModalities([]), ["text"])
  })
})
