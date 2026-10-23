/** The credential and endpoint must change together after /login or an env override. */
export type SavedRouterConnection = {
  baseUrl?: string
  localApiKey?: string
  usageApiKey?: string
}

export type RouterConnection = {
  baseUrl: string
  apiKey: string
  usageApiKey: string
}

/** Return the HTTPS upstream only for the CLI's local-agent URL format. */
export function directRouterURL(baseUrl: string): string {
  const url = new URL(baseUrl)
  if (
    url.protocol !== "http:" ||
    !["127.0.0.1", "localhost"].includes(url.hostname) ||
    !url.pathname.startsWith("/u/")
  ) return baseUrl
  const [authority, ...path] = url.pathname.slice(3).split("/")
  const direct = new URL(`https://${authority}/${path.join("/")}${url.search}`)
  if (!authority || direct.username || direct.password) {
    throw new Error("Invalid local Router endpoint; run ramp router configure pi")
  }
  return direct.toString()
}

export function resolveRouterConnection(
  baseUrl: string,
  apiKey: string,
  saved: SavedRouterConnection,
): RouterConnection {
  const direct = directRouterURL(baseUrl)
  if (!apiKey.startsWith("ramp-local-")) {
    // Pi /login and runtime overrides supply real Router keys. The existing
    // local agent does not know them: use its HTTPS upstream, never loopback.
    return { baseUrl: direct, apiKey, usageApiKey: apiKey }
  }
  if (
    apiKey !== saved.localApiKey || !saved.usageApiKey ||
    saved.usageApiKey.startsWith("ramp-local-") || !saved.baseUrl ||
    new URL(direct).origin !== new URL(directRouterURL(saved.baseUrl)).origin
  ) {
    // A token for one stack must not release its real key to another stack.
    throw new Error("The local Router token does not match this endpoint. Use Pi /login for this deployment or run ramp router configure pi.")
  }
  return {
    baseUrl,
    apiKey: direct === baseUrl ? saved.usageApiKey : apiKey,
    usageApiKey: saved.usageApiKey,
  }
}
