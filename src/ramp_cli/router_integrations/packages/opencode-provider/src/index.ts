import type { PluginModule } from "@opencode-ai/plugin"
import type { Plugin } from "@opencode/plugin"

import { RouterProviderV1 } from "./v1.ts"
import { setupRouterProviderV2 } from "./v2.ts"

export const PLUGIN_ID = "@ramp/router-opencode-provider"

/**
 * One package for both OpenCode generations, in the shape OpenCode documents
 * for the transition: v1 (>= 1.18.29 for object entrypoints) calls `server()`
 * and v2 calls `setup()`. Neither host runs the other's implementation.
 *
 * `Plugin.define` is not called because it is an identity function and
 * importing `@opencode/plugin` at runtime would fail under v1, which does not
 * ship that package. The type annotations keep both contracts checked.
 */
const plugin = {
  id: PLUGIN_ID,
  setup: setupRouterProviderV2,
  server: RouterProviderV1,
} satisfies PluginModule & Plugin.Plugin

export default plugin
export { discoverRouterModels, normalizeBaseURL } from "./discovery.ts"
export { toV2Model, toV2Provider } from "./v2.ts"
