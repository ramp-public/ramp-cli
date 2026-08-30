import assert from "node:assert/strict"
import { cpSync, mkdtempSync } from "node:fs"
import { tmpdir } from "node:os"
import { dirname, join, resolve } from "node:path"
import { pathToFileURL, fileURLToPath } from "node:url"
import { describe, it } from "node:test"

const packageRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..")
const workspaceRoot = resolve(packageRoot, "../..")

describe("packaged OpenCode integration", () => {
  it("loads its server plugin outside a repository node_modules hierarchy", async () => {
    const isolatedRoot = mkdtempSync(join(tmpdir(), "router-opencode-production-"))
    const installedPackage = join(isolatedRoot, "opencode-provider")
    cpSync(packageRoot, installedPackage, {
      recursive: true,
      filter: (source) => !source.includes(`${join(packageRoot, "test")}`),
    })

    assert.equal(installedPackage.startsWith(workspaceRoot), false)
    const integration = await import(
      pathToFileURL(join(installedPackage, "src/index.ts")).href
    )
    assert.equal(typeof integration.default, "object")
    assert.equal(typeof integration.default.server, "function")
  })
})
