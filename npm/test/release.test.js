'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { manifests } = require('../scripts/packages');
const { platforms, validatePublishEnvironment, validateNativeRelease, validateNativeReceipt, waitUntilInstallable, publishArtifacts } = require('../scripts/publish');

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

test('Router is published only after the CLI version is installable', async () => {
  const events = [];
  await publishArtifacts(artifacts, { metadata: async () => null, available: async artifact => events.push(`wait ${artifact.name}`), publish: async artifact => events.push(`publish ${artifact.name}`) });
  assert.deepEqual(events, ['publish @ramp/cli', 'wait @ramp/cli', 'publish @ramp/router']);
  const writes = [];
  await assert.rejects(publishArtifacts(artifacts, { metadata: async () => null, available: async () => { throw new Error('not installable'); }, publish: async artifact => writes.push(artifact.name) }), /not installable/);
  assert.deepEqual(writes, ['@ramp/cli']);
});

test('waiting for an installable version polls until it appears or gives up', async () => {
  let lookups = 0;
  const sleeps = [];
  await waitUntilInstallable(artifacts[0], async () => (++lookups < 3 ? null : published(artifacts[0])), { sleep: async ms => sleeps.push(ms) });
  assert.equal(lookups, 3);
  assert.deepEqual(sleeps, [30_000, 30_000]);
  await assert.rejects(waitUntilInstallable(artifacts[0], async () => null, { attempts: 2, sleep: async () => {} }), /not installable yet/);
});

test('waiting for an installable version retries registry errors and timeouts', async () => {
  let lookups = 0;
  const sleeps = [];
  await waitUntilInstallable(artifacts[0], async name => {
    assert.equal(name, artifacts[0].name);
    lookups++;
    if (lookups === 1) throw new Error('HTTP 503');
    if (lookups === 2) return null;
    if (lookups === 3) throw new DOMException('fetch timed out', 'TimeoutError');
    return published(artifacts[0]);
  }, { attempts: 4, interval: 123, sleep: async ms => sleeps.push(ms) });
  assert.equal(lookups, 4);
  assert.deepEqual(sleeps, [123, 123, 123]);
});

test('waiting for an installable version rethrows the final lookup error only after exhausting attempts', async () => {
  for (const attempts of [1, 3]) {
    let lookups = 0;
    const sleeps = [];
    const finalError = new Error('registry unavailable');
    await assert.rejects(waitUntilInstallable(artifacts[0], async () => {
      lookups++;
      throw lookups === attempts ? finalError : new Error('transient registry error');
    }, { attempts, sleep: async ms => sleeps.push(ms) }), error => error === finalError);
    assert.equal(lookups, attempts);
    assert.deepEqual(sleeps, Array(attempts - 1).fill(30_000));
  }
});

test('an earlier lookup error does not replace the final not-installable error', async () => {
  let lookups = 0;
  await assert.rejects(waitUntilInstallable(artifacts[0], async () => {
    if (++lookups === 1) throw new Error('transient registry error');
    return null;
  }, { attempts: 2, sleep: async () => {} }), /not installable yet/);
  assert.equal(lookups, 2);
});
