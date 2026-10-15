'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { manifests } = require('../scripts/packages');
const { platforms, validatePublishEnvironment, validateNativeRelease, validateNativeReceipt, publishArtifacts } = require('../scripts/publish');

const artifacts = ['cli', 'router'].map(name => ({ name: `@ramp/${name}`, version: '1.2.3', integrity: `sha512-${name}` }));
const published = (artifact, tag = 'next') => ({ 'dist-tags': { [tag]: artifact.version }, versions: { [artifact.version]: { dist: { integrity: artifact.integrity } } } });

test('publishing fails closed outside an opted-in manual public-main workflow', () => {
  const env = { GITHUB_ACTIONS: 'true', GITHUB_REPOSITORY: 'ramp-public/ramp-cli',
    GITHUB_REF: 'refs/heads/main', GITHUB_EVENT_NAME: 'workflow_dispatch', NPM_PUBLISH_ENABLED: 'true' };
  validatePublishEnvironment(env);
  for (const key of Object.keys(env)) {
    assert.throws(() => validatePublishEnvironment({ ...env, [key]: '' }), /manual GitHub workflow/);
  }
  assert.throws(() => validatePublishEnvironment({ ...env, GITHUB_REPOSITORY: 'ramp/ramp-cli' }), /public main/);
  assert.throws(() => validatePublishEnvironment({ ...env, GITHUB_REF: 'refs/heads/feature' }), /public main/);
  assert.throws(() => validatePublishEnvironment({ ...env, GITHUB_EVENT_NAME: 'pull_request' }), /manual/);
});

test('native prerequisite requires a stable public release and all archives/checksums', () => {
  const { cli: manifest } = manifests('1.2.3');
  const release = { tag_name: `v${manifest.nativeCliVersion}`, draft: false, prerelease: false, assets: platforms.flatMap(platform => ['.tar.gz', '.tar.gz.sha256'].map(suffix => ({ name: `ramp-${platform}${suffix}` }))) };
  validateNativeRelease(manifest, release);
  assert.throws(() => validateNativeRelease(manifest, { ...release, draft: true }), /published/);
  assert.throws(() => validateNativeRelease(manifest, { ...release, prerelease: true }), /stable/);
  assert.throws(() => validateNativeRelease(manifest, { ...release, tag_name: 'v0.0.0' }), /targeted/);
  assert.throws(() => validateNativeRelease(manifest, { ...release, assets: release.assets.slice(1) }), /missing ramp-/);
});

test('publishing requires a native release that contains the install receipt', () => {
  const { cli: manifest } = manifests('1.2.3');
  validateNativeReceipt(manifest, { path: 'src/ramp_cli/install_record.py' });
  assert.throws(() => validateNativeReceipt(manifest, null), /predates the npm install receipt/);
});

test('publishes CLI before Router and resumes after only CLI was published', async () => {
  const writes = [];
  await publishArtifacts(artifacts, { metadata: async () => null, publish: async artifact => writes.push(artifact.name) });
  assert.deepEqual(writes, ['@ramp/cli', '@ramp/router']);
  writes.length = 0;
  await publishArtifacts(artifacts, { metadata: async name => name === '@ramp/cli' ? published(artifacts[0]) : null, publish: async artifact => writes.push(artifact.name) });
  assert.deepEqual(writes, ['@ramp/router']);
});

test('preflights both immutable versions before publishing either', async () => {
  const corrupt = published({ ...artifacts[1], integrity: 'sha512-different' });
  const writes = [];
  await assert.rejects(publishArtifacts(artifacts, { metadata: async name => name === '@ramp/router' ? corrupt : null, publish: async artifact => writes.push(artifact.name) }), /different contents/);
  assert.deepEqual(writes, []);
});

test('lost publish response is reconciled only when the registry has the exact tarball', async () => {
  let committed = false;
  await publishArtifacts([artifacts[0]], {
    metadata: async () => committed ? published(artifacts[0]) : null,
    publish: async () => { committed = true; throw new Error('response lost'); },
  });
  await assert.rejects(publishArtifacts([artifacts[0]], { metadata: async () => null, publish: async () => { throw new Error('registry unavailable'); } }), /registry unavailable/);
});

test('an existing version under another tag needs explicit promotion, not republishing', async () => {
  await assert.rejects(publishArtifacts([artifacts[0]], { tag: 'latest', metadata: async () => published(artifacts[0]), publish: async () => { throw new Error('must not publish'); } }), /Promote the tested version/);
  const writes = [];
  await assert.rejects(publishArtifacts(artifacts, { tag: 'latest', metadata: async name => name === '@ramp/router' ? published(artifacts[1]) : null, publish: async artifact => writes.push(artifact.name) }), /Promote the tested version/);
  assert.deepEqual(writes, []);
});
