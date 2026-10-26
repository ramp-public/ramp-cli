'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { install, syncReceipt, paths, platformKey } = require('../cli/lib/installer');
const { sandbox, fixture } = require('./helpers');

test('maps every published asset and rejects incompatible targets', () => {
  for (const [platform, arch, expected] of [
    ['darwin', 'arm64', 'darwin-arm64'], ['darwin', 'x64', 'darwin-amd64'],
    ['linux', 'arm64', 'linux-arm64'], ['linux', 'x64', 'linux-amd64'],
    ['win32', 'x64', 'windows-amd64'],
  ]) assert.equal(platformKey(platform, arch), expected);
  for (const [platform, arch] of [['win32', 'arm64'], ['linux', 'ia32'], ['freebsd', 'x64']]) assert.throws(() => platformKey(platform, arch), /Unsupported platform/);
});

test('installs the entire distribution into a path with spaces and records its version', async t => {
  const p = sandbox(t);
  const f = await fixture(p.home);
  const executable = await install({ ...p, download: f.download });
  assert.equal(executable, p.executable);
  assert.equal(fs.readFileSync(path.join(p.dist, 'supporting-library.txt'), 'utf8'), 'standalone runtime');
  assert.equal(JSON.parse(fs.readFileSync(p.state)).version, 'v0.0.0');
  assert.equal(spawnSync(executable, ['--version']).status, 0);
  assert.equal(fs.readdirSync(p.root).some(name => name.startsWith('.stage-')), false);
  // The stable `ramp` entrypoint survives upgrades and is recreated on reuse.
  assert.ok(fs.readFileSync(p.entry, 'utf8').includes(`exec '${p.executable}' "$@"`));
  fs.unlinkSync(p.entry);
  await install({ ...p, download: () => { throw new Error('not needed'); } });
  assert.equal(spawnSync(p.entry, ['--version']).status, 0);
});

test('each architecture keeps its own distribution', async t => {
  const p = sandbox(t);
  const arm = paths({ home: p.home, env: p.env, platform: 'darwin', arch: 'arm64' });
  const intel = paths({ home: p.home, env: p.env, platform: 'darwin', arch: 'x64' });
  assert.equal(arm.root, intel.root);
  assert.notEqual(arm.dist, intel.dist);
  assert.equal(path.dirname(arm.executable), arm.dist);
});

test('reuses compatible managed installations offline, including latest', async t => {
  const p = sandbox(t);
  await install({ ...p, download: (await fixture(p.home)).download });
  const offline = () => { throw new Error('must not download'); };
  assert.equal(await install({ ...p, download: offline }), p.executable);
  assert.equal(await install({ ...p, env: { ...p.env, RAMP_CLI_VERSION: 'latest' }, download: offline, latestVersion: offline }), p.executable);
});

test('records the actual latest release tag and updates only when requested', async t => {
  const p = sandbox(t);
  const f = await fixture(p.home, { version: 'v1.2.3' });
  await install({ ...p, env: { ...p.env, RAMP_CLI_VERSION: 'latest' }, latestVersion: async () => 'v1.2.3', download: f.download });
  assert.equal(JSON.parse(fs.readFileSync(p.state)).version, 'v1.2.3');
  const next = await fixture(p.home, { version: 'v1.2.4' });
  await install({ ...p, env: { ...p.env, RAMP_CLI_VERSION: 'latest', RAMP_CLI_FORCE_UPDATE: '1' }, latestVersion: async () => 'v1.2.4', download: next.download });
  assert.equal(JSON.parse(fs.readFileSync(p.state)).version, 'v1.2.4');
});

test('a failed download, invalid checksum, or unlaunchable upgrade preserves the working distribution', async t => {
  const p = sandbox(t);
  const f = await fixture(p.home);
  await install({ ...p, download: f.download });
  const state = fs.readFileSync(p.state, 'utf8');
  const upgrade = { ...p, env: { ...p.env, RAMP_CLI_VERSION: 'v0.0.1' } };
  await assert.rejects(install({ ...upgrade, download: async () => { throw new Error('network unavailable'); } }), /network unavailable/);
  await assert.rejects(install({ ...upgrade, download: async (url, target) => {
    if (url.endsWith('.sha256')) fs.writeFileSync(target, '0'.repeat(64));
    else await f.download(url, target);
  } }), /SHA-256/);
  await assert.rejects(install({ ...upgrade, download: (await fixture(p.home, { version: 'v0.0.1', broken: true })).download }), /could not run/);
  assert.equal(fs.readFileSync(p.state, 'utf8'), state);
  assert.equal(spawnSync(p.executable, ['--version']).status, 0);
  assert.equal(fs.existsSync(path.join(p.root, '.install-lock')), false);
});

test('an interrupted swap restores the previous distribution and removes abandoned stages', async t => {
  const p = sandbox(t);
  await install({ ...p, download: (await fixture(p.home)).download });
  fs.renameSync(p.dist, path.join(p.root, `previous-${p.key}`));
  fs.mkdirSync(path.join(p.root, '.stage-abandoned'));
  assert.equal(await install({ ...p, download: () => { throw new Error('not needed'); } }), p.executable);
  assert.equal(fs.existsSync(path.join(p.root, `previous-${p.key}`)), false);
  assert.equal(fs.existsSync(path.join(p.root, '.stage-abandoned')), false);
});

test('concurrent installers serialize and reuse the completed download', async t => {
  const p = sandbox(t);
  const f = await fixture(p.home);
  let downloads = 0;
  const download = async (url, target) => { downloads++; await new Promise(resolve => setTimeout(resolve, 50)); await f.download(url, target); };
  assert.deepEqual(await Promise.all([install({ ...p, download }), install({ ...p, download })]), [p.executable, p.executable]);
  assert.equal(downloads, 2); // One checksum plus one archive, despite two installers.
});

test('waits for a concurrent installer to finish initializing ownership', async t => {
  const p = sandbox(t);
  const f = await fixture(p.home);
  fs.mkdirSync(p.root);
  const unlock = await require('proper-lockfile').lock(p.root, { lockfilePath: path.join(p.root, '.install-lock') });
  const owner = path.join(p.root, '.ramp-cli-npm');
  // Another npm lifecycle hook has opened the marker but not written it yet.
  const fd = fs.openSync(owner, 'wx');
  const pending = install({ ...p, download: f.download });
  const finishOwner = new Promise(resolve => setImmediate(() => {
    fs.writeFileSync(fd, '@ramp/cli\n');
    fs.closeSync(fd);
    resolve(unlock());
  }));
  const [result, finished] = await Promise.allSettled([pending, finishOwner]);
  assert.equal(finished.status, 'fulfilled', finished.reason?.message);
  assert.equal(result.status, 'fulfilled', result.reason?.message);
  assert.equal(result.value, p.executable);
});

test('stale locks left by killed installers are reclaimed', async t => {
  const p = sandbox(t);
  await install({ ...p, download: (await fixture(p.home)).download });
  const lock = path.join(p.root, '.install-lock');
  fs.mkdirSync(lock);
  const old = new Date(Date.now() - 60_000);
  fs.utimesSync(lock, old, old);
  assert.equal(await install(p), p.executable);
});

test('rejects unsafe archives before extraction', async t => {
  const p = sandbox(t);
  const tar = require('tar');
  const source = path.join(p.home, 'unsafe');
  fs.mkdirSync(source);
  fs.writeFileSync(path.join(source, 'unrelated'), 'must not extract');
  const archive = path.join(p.home, 'unsafe.tar.gz');
  await tar.c({ gzip: true, file: archive, cwd: source }, ['unrelated']);
  const checksum = require('node:crypto').createHash('sha256').update(fs.readFileSync(archive)).digest('hex');
  await assert.rejects(install({ ...p, download: async (url, target) => url.endsWith('.sha256') ? fs.writeFileSync(target, checksum) : fs.copyFileSync(archive, target) }), /Unsafe release archive/);
  assert.equal(fs.existsSync(p.dist), false);
});

test('preserves unrelated installation directories and validates version overrides', async t => {
  const p = sandbox(t);
  fs.mkdirSync(p.root);
  fs.writeFileSync(path.join(p.root, '.existing-update-attempt'), 'previous external update');
  fs.mkdirSync(path.join(p.root, '.existing-update-lock'));
  fs.writeFileSync(path.join(p.root, 'important'), 'keep');
  await assert.rejects(install(p), /unmanaged installation/);
  assert.equal(fs.readFileSync(path.join(p.root, 'important'), 'utf8'), 'keep');
  fs.writeFileSync(path.join(p.root, '.ramp-cli-npm'), 'another owner\n');
  await assert.rejects(install(p), /Unrecognized installation owner/);
  assert.equal(fs.existsSync(path.join(p.root, '.install-lock')), false);
  await assert.rejects(install({ ...p, env: { ...p.env, RAMP_CLI_VERSION: '../latest' } }), /release tag/);
});

test('external update metadata and an update lock do not make the install root unmanaged', async t => {
  const p = sandbox(t);
  fs.mkdirSync(p.root);
  const attempt = path.join(p.root, '.existing-update-attempt');
  const lock = path.join(p.root, '.existing-update-lock');
  fs.writeFileSync(attempt, 'previous external update');
  fs.mkdirSync(lock);
  assert.equal(await install({ ...p, download: (await fixture(p.home)).download }), p.executable);
  assert.equal(fs.readFileSync(attempt, 'utf8'), 'previous external update');
  assert.ok(fs.statSync(lock).isDirectory());
});

test('Windows uses a native user-owned location and exe asset', () => {
  const p = paths({ home: '/home/user', env: { LOCALAPPDATA: '/user appdata' }, platform: 'win32', arch: 'x64' });
  assert.equal(p.root, '/user appdata/Ramp/CLI/npm');
  assert.match(p.executable, /ramp-windows-amd64\.exe$/);
});

test('first-run recovery repairs a missing managed executable', async t => {
  const p = sandbox(t);
  const f = await fixture(p.home);
  await install({ ...p, download: f.download });
  fs.unlinkSync(p.executable);
  assert.equal(await install({ ...p, download: f.download }), p.executable);
  assert.equal(spawnSync(p.executable, ['--version']).status, 0);
});


test('default download targets the explicit native CLI version, not npm latest', async t => {
  const p = sandbox(t);
  const version = `v${require('../cli/package.json').nativeCliVersion}`;
  assert.equal(require('../package-lock.json').packages.cli.version, require('../cli/package.json').version);
  const f = await fixture(p.home, { version });
  const urls = [];
  await install({ ...p, env: { ...p.env, RAMP_CLI_VERSION: '' },
    download: async (url, target) => { urls.push(url); await f.download(url, target); },
    latestVersion: async () => { throw new Error('Default must use the explicit native CLI version'); },
  });
  assert.ok(urls.every(url => url.includes(`/download/${version}/`)));
  assert.equal(JSON.parse(fs.readFileSync(path.join(p.dist, 'install.json'), 'utf8')).version, version);
});

test('install record names the npm package that owns the install', async t => {
  const p = sandbox(t);
  const version = `v${require('../cli/package.json').nativeCliVersion}`;
  const f = await fixture(p.home, { version });
  await install({ ...p, env: { ...p.env, RAMP_CLI_VERSION: '' }, package: '@ramp/router', download: f.download });
  const record = JSON.parse(fs.readFileSync(path.join(p.dist, 'install.json'), 'utf8'));
  assert.equal(record.owner, '@ramp/cli');
  assert.equal(record.package, '@ramp/router');
});

test('global install lifecycles own the receipt package; other runs keep it', async t => {
  const p = sandbox(t);
  const f = await fixture(p.home);
  const noDownload = () => { throw new Error('not needed'); };
  const receipt = () => JSON.parse(fs.readFileSync(p.state, 'utf8')).package;
  // npm install -g @ramp/router: the @ramp/cli dependency's lifecycle runs first.
  await install({ ...p, package: '@ramp/cli', globalInstall: true, download: f.download });
  assert.equal(receipt(), '@ramp/cli');
  await install({ ...p, package: '@ramp/router', globalInstall: true, download: noDownload });
  assert.equal(receipt(), '@ramp/router');
  assert.equal(JSON.parse(fs.readFileSync(p.state, 'utf8')).version, 'v0.0.0');
  // Running `ramp`, or a forced reinstall outside a lifecycle, keeps it.
  await install({ ...p, package: '@ramp/cli', download: noDownload });
  await install({ ...p, package: '@ramp/cli', env: { ...p.env, RAMP_CLI_FORCE_UPDATE: '1' }, download: f.download });
  assert.equal(receipt(), '@ramp/router');
  // A later global @ramp/cli install alone (e.g. after an npx Router run) resets it.
  await install({ ...p, package: '@ramp/cli', globalInstall: true, download: noDownload });
  assert.equal(receipt(), '@ramp/cli');
  await install({ ...p, package: '@ramp/router', download: noDownload });
  assert.equal(receipt(), '@ramp/cli');
});

test('the entrypoint answers update itself, forwards everything else, and turns off notices', async t => {
  const p = sandbox(t);
  const log = path.join(p.home, 'invocations.jsonl');
  await install({ ...p, package: '@ramp/router', globalInstall: true, download: (await fixture(p.home)).download });
  const env = { ...p.env, ROUTER_TEST_LOG: log };
  for (const args of [['update'], ['--agent', 'update'], ['--env', 'sandbox', 'update'], ['--output=json', 'update'], ['-ojson', 'update'], ['-esandbox', 'update'], ['-qo', 'json', 'update'], ['-qqojson', 'update'], ['--', 'update']]) {
    const result = spawnSync(p.entry, args, { encoding: 'utf8', env });
    assert.equal(result.status, 0, result.stderr);
    assert.match(result.stdout, /installed by npm \(@ramp\/router\)/);
    assert.match(result.stdout, /npm update -g @ramp\/router/);
  }
  assert.equal(fs.existsSync(log), false); // The native updater never ran.
  for (const args of [['update', '--help'], ['status', 'update'], ['--env', 'update'], ['-o', 'update'], ['-qh', 'update'], ['--', 'status']]) {
    assert.deepEqual(JSON.parse(spawnSync(p.entry, args, { encoding: 'utf8', env }).stdout), args);
  }
  assert.equal(JSON.parse(spawnSync(p.entry, ['env'], { encoding: 'utf8', env }).stdout).noUpdateCheck, '1');
  // A later global @ramp/cli install renames the package the entrypoint names.
  await install({ ...p, package: '@ramp/cli', globalInstall: true, download: () => { throw new Error('not needed'); } });
  assert.match(spawnSync(p.entry, ['update'], { encoding: 'utf8', env }).stdout, /npm update -g @ramp\/cli/);
});

test('the Windows shortcut follows the receipt package when it changes', async t => {
  const p = sandbox(t);
  const w = { ...p, platform: 'win32', arch: 'x64' };
  const q = paths(w);
  const state = { owner: '@ramp/cli', package: '@ramp/cli', version: 'v0.0.0', platform: q.key, executable: q.name };
  fs.mkdirSync(q.dist, { recursive: true });
  fs.writeFileSync(path.join(q.root, '.ramp-cli-npm'), '@ramp/cli\n');
  fs.writeFileSync(q.executable, '');
  fs.writeFileSync(q.state, JSON.stringify(state));
  const { exposeCommand } = require('../cli/lib/commands');
  exposeCommand(w);
  const shortcut = path.join(q.bin, 'ramp.cmd');
  assert.match(fs.readFileSync(shortcut, 'utf8'), /npm update -g @ramp\/cli\r\n/);
  await install({ ...w, package: '@ramp/router', globalInstall: true, download: () => { throw new Error('not needed'); } });
  const body = fs.readFileSync(shortcut, 'utf8');
  assert.match(body, /npm update -g @ramp\/router\r\n/);
  assert.equal(body.includes('npm update -g @ramp/cli\r\n'), false);
  fs.writeFileSync(shortcut, '@echo off\r\nunrelated\r\n');
  await install({ ...w, package: '@ramp/cli', globalInstall: true, download: () => { throw new Error('not needed'); } });
  assert.equal(fs.readFileSync(shortcut, 'utf8'), '@echo off\r\nunrelated\r\n');
});

test('a newer shared runtime is not rolled back, and a deferred install still moves the receipt', async t => {
  const p = sandbox(t);
  const f = await fixture(p.home);
  await install({ ...p, package: '@ramp/cli', globalInstall: true, download: f.download });
  const state = JSON.parse(fs.readFileSync(p.state, 'utf8'));
  fs.writeFileSync(p.state, JSON.stringify({ ...state, version: 'v99.0.0' }));
  const noDownload = () => { throw new Error('not needed'); };
  await install({ ...p, env: { ...p.env, RAMP_CLI_VERSION: '' }, download: noDownload });
  assert.equal(JSON.parse(fs.readFileSync(p.state, 'utf8')).version, 'v99.0.0');
  syncReceipt({ ...p, package: '@ramp/router', globalInstall: true });
  assert.equal(JSON.parse(fs.readFileSync(p.state, 'utf8')).package, '@ramp/router');
});

test('Windows shortcuts created in the install root do not block a managed install', async t => {
  const p = sandbox(t);
  const f = await fixture(p.home);
  fs.mkdirSync(path.join(p.root, 'bin'), { recursive: true });
  fs.writeFileSync(path.join(p.root, 'bin', 'ramp.cmd'), '@echo off\n');
  await install({ ...p, bin: undefined, env: { ...p.env, RAMP_CLI_BIN_DIR: path.join(p.root, 'bin') }, download: f.download });
  assert.ok(fs.existsSync(p.executable));
});
