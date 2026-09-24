import assert from "node:assert/strict"
import { cpSync, existsSync, mkdtempSync, readFileSync } from "node:fs"
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
    assert.equal(typeof integration.default.setup, "function")
  })

  it("resolves OpenCode v2's root-level entrypoints", async () => {
    // v2 loads a local plugin directory as `<dir>/index` and `<dir>/tui`
    // rather than through package.json exports, so both must exist at the
    // package root and forward the same default export.
    const isolatedRoot = mkdtempSync(join(tmpdir(), "router-opencode-production-"))
    const installedPackage = join(isolatedRoot, "opencode-provider")
    cpSync(packageRoot, installedPackage, {
      recursive: true,
      filter: (source) => !source.includes(`${join(packageRoot, "test")}`),
    })

    const root = await import(pathToFileURL(join(installedPackage, "index.ts")).href)
    const source = await import(pathToFileURL(join(installedPackage, "src/index.ts")).href)
    assert.equal(root.default, source.default)
    assert.equal(typeof root.discoverRouterModels, "function")
    assert.equal(existsSync(join(installedPackage, "tui.ts")), true)
    const packageJSON = JSON.parse(
      readFileSync(join(installedPackage, "package.json"), "utf8"),
    )
    assert.deepEqual(packageJSON.exports, {
      ".": "./index.ts",
      "./server": "./index.ts",
      "./tui": "./tui.ts",
    })
  })
})
