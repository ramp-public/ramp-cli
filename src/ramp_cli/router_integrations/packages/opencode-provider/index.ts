// OpenCode v2 loads a local plugin directory by resolving `<dir>/index` and
// `<dir>/tui` directly rather than through package.json exports, so the
// entrypoints live at the package root and forward to the sources.
export { default } from "./src/index.ts"
export {
  discoverRouterModels,
  normalizeBaseURL,
  toV2Model,
  toV2Provider,
} from "./src/index.ts"
