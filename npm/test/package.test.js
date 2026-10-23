'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const crypto = require('node:crypto');
const { once } = require('node:events');
const { execFile } = require('node:child_process');
const { promisify } = require('node:util');
const tar = require('tar');
const { root, pack, manifests } = require('../scripts/packages');
const { sandbox, fixture } = require('./helpers');
const run = promisify(execFile);

function packageRegistry(packages, native) {
  const server = http.createServer((request, response) => {
    if (request.method !== 'GET') { response.writeHead(405); response.end(); return; }
    const name = decodeURIComponent(new URL(request.url, 'http://localhost').pathname.slice(1));
    const archive = packages.find(pkg => `${pkg.manifest.name}.tgz` === name);
    if (archive) { response.end(archive.bytes); return; }
    if (name.endsWith('.tar.gz.sha256')) { response.end(native.checksum); return; }
    if (name.endsWith('.tar.gz')) { response.end(fs.readFileSync(native.archive)); return; }
    const pkg = packages.find(pkg => pkg.manifest.name === name);
    if (!pkg) { response.writeHead(404); response.end(); return; }
    response.setHeader('Content-Type', 'application/json');
    response.end(JSON.stringify({
      name, 'dist-tags': { latest: pkg.manifest.version },
      versions: { [pkg.manifest.version]: {
        ...pkg.manifest,
        dist: { integrity: `sha512-${crypto.createHash('sha512').update(pkg.bytes).digest('base64')}`, tarball: `http://127.0.0.1:${server.address().port}/${encodeURIComponent(name)}.tgz` },
      } },
    }));
  });
  server.listen(0, '127.0.0.1');
  return server;
}

test('packed packages install, coexist, and launch through a real isolated npm registry', { timeout: 180_000 }, async t => {
  const build = sandbox(t);
  const artifacts = pack(path.join(build.home, 'packed'));
  const packages = artifacts.map(artifact => ({ manifest: JSON.parse(fs.readFileSync(path.join(root, artifact.name.split('/')[1], 'package.json'))), bytes: fs.readFileSync(artifact.archive) }));
  // Serve the locked transitive dependencies locally too. This proves normal
  // registry dependency resolution without requiring unpublished @ramp names
  // or leaking test traffic to a workstation's configured npm proxy.
  const lock = JSON.parse(fs.readFileSync(path.join(root, 'package-lock.json')));
  for (const [directory, info] of Object.entries(lock.packages)) {
    if (!directory.startsWith('node_modules/') || info.link) continue;
    const source = path.join(root, directory);
    const manifest = JSON.parse(fs.readFileSync(path.join(source, 'package.json')));
    const archive = path.join(build.home, `${encodeURIComponent(manifest.name)}.tgz`);
    // Repackage already-installed dependencies directly. npm 10's packing of
    // a directory can run prepare even with --ignore-scripts; never execute a
    // third-party build merely to create a local registry fixture.
    await tar.c({ gzip: true, file: archive, cwd: source, prefix: 'package' }, fs.readdirSync(source));
    packages.push({ manifest, bytes: fs.readFileSync(archive) });
  }
  const native = await fixture(build.home);
  const server = packageRegistry(packages, native);
  await once(server, 'listening');
  t.after(() => { server.closeAllConnections(); server.close(); });
  const origin = `http://127.0.0.1:${server.address().port}`;

  function isolated(t) {
    const p = sandbox(t);
    const config = path.join(p.home, 'empty.npmrc');
    fs.writeFileSync(config, '');
    const globalConfig = path.join(p.home, 'global.npmrc');
    fs.writeFileSync(globalConfig, '');
    const log = path.join(p.home, 'native-calls.jsonl');
    const env = { ...p.env, NODE_OPTIONS: `--require=${path.join(__dirname, 'fixture-preload.js')}`, ROUTER_TEST_ORIGIN: origin, ROUTER_TEST_LOG: log, npm_config_registry: origin, npm_config_userconfig: config, npm_config_globalconfig: globalConfig, npm_config_cache: path.join(p.home, 'cache'), npm_config_prefix: path.join(p.home, 'prefix'), npm_config_audit: 'false', npm_config_fund: 'false', npm_config_update_notifier: 'false' };
    return { ...p, env, log, bin: path.join(env.npm_config_prefix, 'bin') };
  }
  const calls = p => fs.readFileSync(p.log, 'utf8').trim().split('\n').map(JSON.parse);
  const invoke = async (command, args, p, extra = {}) => {
    const result = await run(command, args, { cwd: p.home, env: { ...p.env, ...extra }, timeout: 60_000, maxBuffer: 2_000_000 });
    return result;
  };

  await t.test('global CLI is install-only and declares only ramp', async t => {
    const p = isolated(t);
    await invoke('npm', ['install', '-g', '@ramp/cli'], p);
    assert.ok(fs.existsSync(path.join(p.bin, 'ramp')));
    assert.equal(fs.existsSync(path.join(p.bin, 'router')), false);
    assert.deepEqual(calls(p), [['--version']]);
    assert.deepEqual(JSON.parse((await invoke(path.join(p.bin, 'ramp'), ['funds', 'list', '--json', '$literal; argument'], p)).stdout), ['funds', 'list', '--json', '$literal; argument']);
    assert.equal(fs.existsSync(path.join(p.home, '.bashrc')), false);
  });

  await t.test('headless global install defers the TUI; co-installing CLI preserves router', async t => {
    const p = isolated(t);
    await invoke('npm', ['install', '-g', '@ramp/router'], p);
    assert.equal(calls(p).some(args => args[0] === 'router'), false);
    const router = fs.readlinkSync(path.join(p.bin, 'router'));
    await invoke('npm', ['install', '-g', '@ramp/cli'], p);
    assert.equal(fs.readlinkSync(path.join(p.bin, 'router')), router);
    assert.deepEqual(JSON.parse((await invoke(path.join(p.bin, 'router'), [], p)).stdout), ['router']);
    assert.equal(calls(p).filter(args => args.length === 1 && args[0] === 'router').length, 1);
    assert.deepEqual(JSON.parse((await invoke(path.join(p.bin, 'router'), ['status'], p)).stdout), ['router', 'status']);
    assert.deepEqual(JSON.parse((await invoke(path.join(p.bin, 'ramp'), ['auth', 'status'], p)).stdout), ['auth', 'status']);
    assert.equal(fs.existsSync(path.join(p.home, '.bashrc')), false);
  });

  await t.test('installing Router after CLI does not replace the ramp shim', async t => {
    const p = isolated(t);
    await invoke('npm', ['install', '-g', '@ramp/cli'], p);
    const ramp = fs.readlinkSync(path.join(p.bin, 'ramp'));
    await invoke('npm', ['install', '-g', '@ramp/router'], p, { RAMP_ROUTER_SKIP_ONBOARDING: '1' });
    assert.equal(fs.readlinkSync(path.join(p.bin, 'ramp')), ramp);
    assert.equal(calls(p).some(args => args[0] === 'router'), false);
    assert.ok(fs.existsSync(path.join(p.bin, 'router')));
  });

  await t.test('npx Router delegates to native Router and creates persistent shortcuts', async t => {
    const p = isolated(t);
    const result = await invoke('npx', ['--yes', '@ramp/router'], p);
    assert.deepEqual(JSON.parse(result.stdout), ['router']);
    assert.equal(calls(p).filter(args => args.length === 1 && args[0] === 'router').length, 1);
    assert.equal(fs.existsSync(path.join(p.home, '.bashrc')), false);
    assert.ok(fs.existsSync(path.join(p.env.RAMP_CLI_BIN_DIR, 'router')));
  });

  await t.test('npx configure forwards the background opt-in and keeps a persistent router command', async t => {
    const p = isolated(t);
    const result = await invoke('npx', ['--yes', '@ramp/router', 'configure', '--enable-agent'], p);
    assert.deepEqual(JSON.parse(result.stdout), ['router', 'configure', '--enable-agent']);
    const shortcut = path.join(p.env.RAMP_CLI_BIN_DIR, 'router');
    assert.ok(fs.existsSync(shortcut));
    assert.deepEqual(JSON.parse((await invoke(shortcut, ['configure', '--enable-agent'], p)).stdout), ['router', 'configure', '--enable-agent']);
  });

  for (const setting of ['CI', 'RAMP_ROUTER_SKIP_ONBOARDING']) {
    await t.test(`${setting} suppresses install-time UI but not an explicit npx invocation`, async t => {
      const p = isolated(t);
      await invoke('npm', ['install', '-g', '@ramp/router'], p, { [setting]: '1' });
      assert.equal(calls(p).some(args => args[0] === 'router'), false);
      const result = await invoke('npx', ['--yes', '@ramp/router'], p, { [setting]: '1' });
      assert.deepEqual(JSON.parse(result.stdout), ['router']);
      assert.equal(fs.existsSync(path.join(p.home, '.bashrc')), false);
    });
  }

  await t.test('ignore-scripts defers installation to first command and dependency installs never onboard', async t => {
    const p = isolated(t);
    await invoke('npm', ['install', '-g', '@ramp/router', '--ignore-scripts'], p);
    assert.equal(fs.existsSync(p.executable), false);
    assert.deepEqual(JSON.parse((await invoke(path.join(p.bin, 'router'), ['status'], p)).stdout), ['router', 'status']);
    assert.ok(fs.existsSync(p.executable));
    assert.equal(calls(p).some(args => args[0] === 'router'), true);
    const local = isolated(t);
    await invoke('npm', ['install', '@ramp/router'], local);
    assert.equal(calls(local).some(args => args[0] === 'router'), false);
  });

  await t.test('npx help does not onboard and generic CLI does not expose a router shortcut', async t => {
    const p = isolated(t);
    assert.deepEqual(JSON.parse((await invoke('npx', ['--yes', '@ramp/router', '--help'], p)).stdout), ['router', '--help']);
    assert.equal(fs.existsSync(path.join(p.env.RAMP_CLI_BIN_DIR, 'router')), false);
    await invoke('npx', ['--yes', '@ramp/cli', 'funds', 'list'], p);
    assert.equal(calls(p).some(args => args[0] === 'router' && args[1] !== '--help'), false);
    assert.ok(fs.existsSync(path.join(p.env.RAMP_CLI_BIN_DIR, 'ramp')));
    assert.equal(fs.existsSync(path.join(p.env.RAMP_CLI_BIN_DIR, 'router')), false);
  });

  for (const scope of ['global', 'npx', 'direct', 'deferred', 'project', 'legacy']) {
    const scenario = scope === 'project' ? 'project npx and npm exec updates preserve the global receipt, runtime and pending refresh' : `${scope} ramp update upgrades packed packages and fully refreshes Router without skills`;
    await t.test(scenario, async t => {
      async function serveVersion(version) {
        const stamped = manifests(version);
        for (const artifact of pack(path.join(build.home, `packed-${version}`), version)) {
          const index = packages.findIndex(pkg => pkg.manifest.name === artifact.name);
          packages[index] = { manifest: stamped[artifact.name.split('/')[1]], bytes: fs.readFileSync(artifact.archive) };
        }
        Object.assign(native, await fixture(build.home, { version: `v${version}` }));
      }
      const oldVersion = scope === 'legacy' ? '0.2.59' : '0.0.0';
      const newVersion = scope === 'legacy' ? '0.2.60' : '0.0.1';
      await serveVersion(oldVersion);
      const p = isolated(t);
      p.env.RAMP_CLI_VERSION = `v${oldVersion}`;
      if (scope === 'legacy') p.env.ROUTER_TEST_LEGACY = '1';
      if (scope !== 'npx') await invoke('npm', ['install', '-g', '@ramp/router'], p);
      else await invoke('npx', ['--yes', '@ramp/router', '--version'], p);
      assert.equal(JSON.parse(fs.readFileSync(p.state)).updateContext.kind, scope === 'npx' ? 'npx' : 'global');
      assert.equal(JSON.parse(fs.readFileSync(p.state)).package, '@ramp/router');
      const realNpm = fs.realpathSync(path.join(p.home, 'runtime-bin', 'npm'));
      // Redirect only the test helper's explicit public-registry argument.
      // Everything else uses real npm and real packed package lifecycle scripts.
      const npm = path.join(p.home, 'runtime-bin', 'npm');
      fs.unlinkSync(npm);
      fs.writeFileSync(npm, `#!/usr/bin/env node\nconst { spawnSync } = require('node:child_process');\nconst args = process.argv.slice(2).map(arg => arg === '--registry=https://registry.npmjs.org/' ? ${JSON.stringify(`--registry=${origin}`)} : arg);\nconst result = spawnSync(process.execPath, [${JSON.stringify(realNpm)}, ...args], { stdio: 'inherit', env: process.env });\nprocess.exit(result.status ?? 1);\n`, { mode: 0o755 });
      await serveVersion(newVersion);
      fs.writeFileSync(p.log, '');
      if (scope === 'project') {
        const project = path.join(p.home, 'project');
        fs.mkdirSync(project);
        fs.writeFileSync(path.join(project, 'package.json'), '{"name":"test-project","private":true}');
        const local = { ...p, home: project };
        await invoke('npm', ['install', '@ramp/cli@0.0.1', '--ignore-scripts'], local, { npm_config_prefix: project });
        const state = JSON.parse(fs.readFileSync(p.state));
        fs.writeFileSync(p.state, JSON.stringify({ ...state, refreshPending: true }));
        const before = fs.readFileSync(p.state, 'utf8');
        for (const [command, args] of [['npm', ['exec', '--', 'ramp', 'update']], ['npx', ['--no-install', 'ramp', 'update']]]) {
          const result = await invoke(command, args, local, { npm_config_prefix: project });
          assert.match(result.stdout, /project package manager and lockfile/);
          assert.equal(fs.readFileSync(p.state, 'utf8'), before);
          assert.equal(fs.readFileSync(p.log, 'utf8'), '');
        }
        assert.equal(JSON.parse(fs.readFileSync(path.join(project, 'node_modules', '@ramp', 'cli', 'package.json'))).version, '0.0.1');
        return;
      }
      if (scope === 'direct' || scope === 'deferred') {
        await invoke('npm', ['update', '-g', '@ramp/router', ...(scope === 'deferred' ? ['--ignore-scripts'] : [])], p, { RAMP_CLI_VERSION: '' });
        if (scope === 'deferred') {
          assert.equal(JSON.parse(fs.readFileSync(p.state)).version, 'v0.0.0');
          await invoke(path.join(p.bin, 'router'), ['--version'], p, { RAMP_CLI_VERSION: '' });
        }
      } else {
        const result = await invoke(p.entry, ['update'], p);
        assert.ok(result.stdout.includes(`Ramp CLI is installed at v${newVersion}`), result.stdout);
      }
      const state = JSON.parse(fs.readFileSync(p.state));
      assert.equal(state.version, `v${newVersion}`);
      assert.equal(state.refreshPending, false);
      const invocations = calls(p);
      assert.equal(invocations.filter(args => args.join(' ') === '--no-input router refresh').length, 1);
      assert.equal(invocations.some(args => args.includes('skills') || args.includes('update') || args.join(' ') === 'router ui'), false);
      assert.equal(fs.existsSync(path.join(p.home, '.bashrc')), false);
    });
  }
});
