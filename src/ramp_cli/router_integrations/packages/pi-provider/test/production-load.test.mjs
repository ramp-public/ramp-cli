import assert from "node:assert/strict"
import { execFileSync } from "node:child_process"
import { cpSync, mkdtempSync, mkdirSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { dirname, join, resolve } from "node:path"
import { fileURLToPath } from "node:url"
import { describe, it } from "node:test"

const packageRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..")
const workspaceRoot = resolve(packageRoot, "../..")

describe("packaged Pi integration", () => {
  it("loads outside a repository node_modules hierarchy", () => {
    const isolatedRoot = mkdtempSync(join(tmpdir(), "router-pi-production-"))
    const installedPackage = join(isolatedRoot, "pi-provider")
    const piHome = join(isolatedRoot, "pi-home")
    cpSync(packageRoot, installedPackage, {
      recursive: true,
      filter: (source) => !source.includes(`${join(packageRoot, "test")}`),
    })
    mkdirSync(piHome)
    writeFileSync(
      join(piHome, "settings.json"),
      JSON.stringify({ packages: [installedPackage] }),
    )

    assert.equal(installedPackage.startsWith(workspaceRoot), false)
    assert.doesNotThrow(() =>
      execFileSync(join(workspaceRoot, "node_modules/.bin/pi"), ["--list-models"], {
        cwd: isolatedRoot,
        env: {
          ...process.env,
          PI_CODING_AGENT_DIR: piHome,
          PI_OFFLINE: "1",
          NODE_PATH: "",
        },
        encoding: "utf8",
        stdio: "pipe",
      }),
    )
  })
})
