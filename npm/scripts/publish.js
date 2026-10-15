'use strict';

const path = require('node:path');
const { root, registry, manifests, pack, npm } = require('./packages');
const platforms = ['darwin-amd64', 'darwin-arm64', 'linux-amd64', 'linux-arm64', 'windows-amd64'];

function validatePublishEnvironment(env = process.env) {
  if (env.GITHUB_ACTIONS !== 'true' || env.GITHUB_REPOSITORY !== 'ramp-public/ramp-cli'
    || env.GITHUB_REF !== 'refs/heads/main' || env.GITHUB_EVENT_NAME !== 'workflow_dispatch'
    || env.NPM_PUBLISH_ENABLED !== 'true') {
    throw new Error('Publishing requires an opted-in manual GitHub workflow on public main.');
  }
}

function validateNativeRelease(manifest, release) {
  if (release.tag_name !== `v${manifest.nativeCliVersion}` || release.draft || release.prerelease) throw new Error('The targeted native CLI must be a published, stable public release.');
  const assets = new Set(release.assets.map(asset => asset.name));
  for (const platform of platforms) {
    for (const suffix of ['.tar.gz', '.tar.gz.sha256']) {
      const name = `ramp-${platform}${suffix}`;
      if (!assets.has(name)) throw new Error(`Public native release is missing ${name}.`);
    }
  }
}

// The launcher guard covers every native release, but the native install
// receipt (install_record.py) is the second layer for paths that bypass it,
// such as cached update notices. Refuse a native release built before it.
const RECEIPT_SOURCE = 'src/ramp_cli/install_record.py';

function validateNativeReceipt(manifest, receiptSource) {
  if (!receiptSource) throw new Error(`Public native release v${manifest.nativeCliVersion} predates the npm install receipt (${RECEIPT_SOURCE}). Publish after a native release that includes it.`);
}

function alreadyPublished(artifact, metadata) {
  const existing = metadata?.versions?.[artifact.version];
  if (!existing) return false;
  if (existing.dist?.integrity !== artifact.integrity) throw new Error(`${artifact.name}@${artifact.version} already exists with different contents. Release a new CLI version instead.`);
  return true;
}

async function json(url, headers = {}) {
  const response = await fetch(url, { headers, signal: AbortSignal.timeout(30_000) });
  if (response.status === 404) return null;
  if (!response.ok) throw new Error(`Could not read ${url}: HTTP ${response.status}`);
  return response.json();
}

async function publishArtifacts(artifacts, { metadata, publish, tag = 'next' }) {
  // Preflight both packages before writing either. The pack order is CLI first,
  // Router second, so its exact dependency exists before Router is published.
  const current = await Promise.all(artifacts.map(artifact => metadata(artifact.name)));
  const existing = artifacts.map((artifact, index) => alreadyPublished(artifact, current[index]));
  for (let index = 0; index < artifacts.length; index++) {
    if (existing[index] && current[index]['dist-tags']?.[tag] !== artifacts[index].version) throw new Error(`${artifacts[index].name}@${artifacts[index].version} exists, but ${tag} points elsewhere. Promote the tested version with an authenticated npm dist-tag command.`);
  }
  for (let index = 0; index < artifacts.length; index++) {
    const artifact = artifacts[index];
    if (existing[index]) {
      console.log(`${artifact.name}@${artifact.version} already published with identical contents.`);
      continue;
    }
    try { await publish(artifact, tag); }
    catch (error) {
      // A lost response may follow a successful registry commit. Never blindly
      // republish an immutable version; reconcile its exact tarball first.
      const after = await metadata(artifact.name);
      if (!alreadyPublished(artifact, after) || after['dist-tags']?.[tag] !== artifact.version) throw error;
    }
  }
}

async function main() {
  validatePublishEnvironment();
  const tag = process.env.NPM_DIST_TAG || 'next';
  if (!['next', 'latest'].includes(tag)) throw new Error('NPM_DIST_TAG must be next or latest.');
  const { cli } = manifests();
  if (tag === 'latest' && cli.version.includes('-')) throw new Error('Do not publish an npm prerelease under latest.');
  const headers = { 'User-Agent': '@ramp/cli', Accept: 'application/vnd.github+json' };
  if (process.env.GH_TOKEN) headers.Authorization = `Bearer ${process.env.GH_TOKEN}`;
  const release = await json(`https://api.github.com/repos/ramp-public/ramp-cli/releases/tags/v${cli.nativeCliVersion}`, headers);
  if (!release) throw new Error('Matching public native release does not exist yet.');
  validateNativeRelease(cli, release);
  validateNativeReceipt(cli, await json(`https://api.github.com/repos/ramp-public/ramp-cli/contents/${RECEIPT_SOURCE}?ref=v${cli.nativeCliVersion}`, headers));
  const artifacts = pack(path.join(root, 'artifacts'));
  await publishArtifacts(artifacts, {
    tag,
    metadata: name => json(`${registry}${encodeURIComponent(name)}`),
    publish: (artifact, distTag) => npm(['publish', artifact.archive, '--ignore-scripts', '--access', 'public', '--registry', registry, '--tag', distTag], { stdio: 'inherit' }),
  });
}

if (require.main === module) main().catch(error => { console.error(error.message); process.exitCode = 1; });
module.exports = { platforms, validatePublishEnvironment, validateNativeRelease, validateNativeReceipt, alreadyPublished, publishArtifacts };
