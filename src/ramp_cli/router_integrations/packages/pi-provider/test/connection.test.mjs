import assert from "node:assert/strict"
import { describe, it } from "node:test"
import { directRouterURL, resolveRouterConnection } from "../src/connection.ts"

const local = "http://127.0.0.1:47317/u/api.router.com/v1"
const direct = "https://api.router.com/v1"
const token = `ramp-local-${"a".repeat(64)}`
const saved = { baseUrl: local, localApiKey: token, usageApiKey: "real-key" }

describe("Router endpoint and credential pairing", () => {
  it("retains the local token for its local endpoint", () => {
    assert.deepEqual(resolveRouterConnection(local, token, saved), {
      baseUrl: local, apiKey: token, usageApiKey: "real-key",
    })
  })
  it("routes a Pi /login or runtime key directly, never to the local listener", () => {
    assert.deepEqual(resolveRouterConnection(local, "rotated-key", saved), {
      baseUrl: direct, apiKey: "rotated-key", usageApiKey: "rotated-key",
    })
  })
  it("exchanges the saved local token for a same-origin direct env override", () => {
    assert.deepEqual(resolveRouterConnection(direct, token, saved), {
      baseUrl: direct, apiKey: "real-key", usageApiKey: "real-key",
    })
  })
  it("rejects token exchange across deployments or to plaintext endpoints", () => {
    for (const endpoint of [
      "https://internal-api.router.com/v1",
      "http://api.router.com/v1",
      "http://127.0.0.1:47317/u/internal-api.router.com/v1",
      "https://api.router.com.evil.example/v1",
    ]) assert.throws(() => resolveRouterConnection(endpoint, token, saved), /does not match/)
  })
  it("requires a matching complete binding before releasing the upstream key", () => {
    for (const binding of [{}, { ...saved, localApiKey: "different-token" }, { ...saved, usageApiKey: "" }]) {
      assert.throws(() => resolveRouterConnection(direct, token, binding), /does not match/)
    }
  })
  it("allows a new real credential for an explicitly selected other deployment", () => {
    const endpoint = "https://internal-api.router.com/v1"
    assert.deepEqual(resolveRouterConnection(endpoint, "new-stack-key", saved), {
      baseUrl: endpoint, apiKey: "new-stack-key", usageApiKey: "new-stack-key",
    })
  })
  it("preserves custom upstream ports, path prefixes and queries", () => {
    assert.equal(directRouterURL("http://localhost:47317/u/api.router.com:8443/prefix/v1?key=value"),
      "https://api.router.com:8443/prefix/v1?key=value")
    assert.equal(directRouterURL("http://localhost:8000/v1"), "http://localhost:8000/v1")
  })
  it("rejects malformed helper targets", () => {
    assert.throws(() => directRouterURL("http://127.0.0.1:47317/u//v1"))
    assert.throws(() => directRouterURL("http://127.0.0.1:47317/u/user@api.router.com/v1"))
  })
})
