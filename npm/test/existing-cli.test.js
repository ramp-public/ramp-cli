'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { resolveCLI, findExisting, isOlder } = require('../cli/lib/existing-cli');
const { install, paths } = require('../cli/lib/installer');
const { sandbox, fixture } = require('./helpers');

async function existing(t, version = `v${require('../cli/package.json').nativeCliVersion}`) {
  if (process.getuid) t.mock.method(process, 'getuid', () => 1000);
  const p = sandbox(t);
  const f = await fixture(p.home, { version });
  const native = path.join(p.home, 'fixture', 'main.dist', p.name);
  const bin = path.join(p.home, 'existing bin');
  fs.mkdirSync(bin);
  const executable = path.join(bin, 'ramp');
  fs.symlinkSync(native, executable);
  return { ...p, ...f, native, selected: executable, env: { ...p.env, RAMP_CLI_VERSION: '', PATH: `${bin}:${p.env.PATH}` } };
}

const offline = () => { throw new Error('Must not download or update'); };

test('reuses a current external CLI without downloading or creating a managed distribution', async t => {
  const p = await existing(t);
  assert.equal(await resolveCLI({ ...p, download: offline }), p.selected);
  assert.equal(fs.existsSync(p.root), false);
  assert.equal(fs.readlinkSync(p.selected), p.native);
});

test('a newer external CLI is reused without prompting or downgrading it', async t => {
  const p = await existing(t, 'v99.0.0');
  assert.equal(await resolveCLI({ ...p, session: { write() {} }, confirm: offline, download: offline }), p.selected);
  assert.equal(fs.existsSync(p.dist), false);
});

test('finds the public curl installation even when its bin directory is not on PATH', async t => {
  const p = await existing(t);
  const bin = path.join(p.home, '.local', 'bin');
  fs.mkdirSync(bin, { recursive: true });
  const executable = path.join(bin, 'ramp');
  fs.symlinkSync(p.native, executable);
  const env = { ...p.env, PATH: p.runtimePath };
  assert.equal(await resolveCLI({ ...p, env, download: offline }), executable);
});

test('finds native curl files behind a shadowing npm shim and never executes our own wrappers', async t => {
  const p = await existing(t);
  const pkg = path.join(p.home, 'fake npm package');
  fs.mkdirSync(path.join(pkg, 'bin'), { recursive: true });
  fs.writeFileSync(path.join(pkg, 'package.json'), '{"name":"@ramp/router"}');
  const shim = path.join(pkg, 'bin', 'ramp.js');
  fs.writeFileSync(shim, '#!/bin/sh\nexit 97\n', { mode: 0o755 });
  const bin = path.join(p.home, '.local', 'bin');
  fs.mkdirSync(bin, { recursive: true });
  fs.symlinkSync(shim, path.join(bin, 'ramp'));
  const lib = path.join(p.home, '.local', 'lib', 'ramp-cli');
  fs.mkdirSync(lib, { recursive: true });
  fs.symlinkSync(p.native, path.join(lib, p.name));
  assert.equal(await resolveCLI({ ...p, env: { ...p.env, PATH: `${bin}:${p.runtimePath}` }, download: offline }), path.join(lib, p.name));
});

test('our managed ramp shortcut is not mistaken for an external installation', async t => {
  const p = await existing(t);
  fs.mkdirSync(p.dist, { recursive: true });
  const executable = path.join(p.dist, p.name);
  fs.writeFileSync(executable, '#!/bin/sh\nexit 97\n', { mode: 0o755 });
  fs.mkdirSync(p.bin);
  fs.symlinkSync(executable, path.join(p.bin, 'ramp'));
  assert.equal((await findExisting({ ...p, env: { ...p.env, PATH: `${p.bin}:${p.env.PATH}` } })).executable, p.selected);
});

test('RAMP_NO_UPDATE_CHECK and headless installs keep an older CLI that supports Router without updating it', async t => {
  const p = await existing(t, 'v0.1.0');
  assert.equal(await resolveCLI({ ...p, env: { ...p.env, RAMP_NO_UPDATE_CHECK: '1' }, autoUpdate: true, update: offline, download: offline }), p.selected);
  assert.equal(await resolveCLI({ ...p, update: offline, download: offline }), p.selected, 'headless npm installs never update');
  assert.equal(await resolveCLI({ ...p, autoUpdate: true, forUpdate: true, update: offline, download: offline }), p.selected, '`ramp update` runs the updater itself');
  assert.equal(fs.existsSync(p.root), false);
});

for (const session of [{ write() {}, stdio: 'inherit' }, null]) test(`${session ? 'workspace' : 'command'} launches update an older CLI in place without asking`, async t => {
  const p = await existing(t, 'v0.1.0');
  let updated = 0;
  const selected = await resolveCLI({ ...p, session, autoUpdate: true, confirm: offline, download: offline, update: async (executable, args, stdio) => {
    assert.equal(executable, p.selected);
    assert.deepEqual(args, ['--no-input', 'update']);
    assert.deepEqual(stdio, session ? session.stdio : ['ignore', 2, 2]);
    updated++;
    await fixture(p.home, { version: `v${require('../cli/package.json').nativeCliVersion}` });
    return { code: 0 };
  } });
  assert.equal(selected, p.selected);
  assert.equal(updated, 1);
  assert.equal(fs.existsSync(p.dist), false);
  assert.equal(fs.existsSync(path.join(p.root, '.existing-update-lock')), false);
});

test('an update that leaves the CLI old is retried after a day, not on every launch', async t => {
  const p = await existing(t, 'v0.1.0');
  let updated = 0;
  const options = { ...p, autoUpdate: true, download: offline, update: async () => ({ code: updated++ ? 0 : 1 }) };
  for (let i = 0; i < 2; i++) assert.equal(await resolveCLI(options), p.selected);
  assert.equal(updated, 1);
  const day = new Date(Date.now() - 86_400_000);
  fs.utimesSync(path.join(p.root, '.existing-update-attempt'), day, day);
  assert.equal(await resolveCLI(options), p.selected);
  assert.equal(updated, 2);
});

test('failed updates warn and reuse a working old external CLI for existing commands', async t => {
  const p = await existing(t, 'v0.1.0');
  const messages = [];
  assert.equal(await resolveCLI({ ...p, session: { write: value => messages.push(value) }, update: async () => ({ code: 23 }), download: offline }), p.selected);
  assert.ok(messages.some(text => text.includes('did not complete')));
  assert.ok(messages.some(text => text.includes('Using existing Ramp CLI')));
  assert.equal(fs.existsSync(p.dist), false);
});

test('a busy updater never falls through to an old CLI', async t => {
  const p = await existing(t, 'v0.1.0');
  t.mock.method(require('proper-lockfile'), 'lock', async () => { throw new Error('busy'); });
  await assert.rejects(resolveCLI({ ...p, autoUpdate: true, update: offline }), /Wait for it to finish.*requested command was not run/s);
});

test('failed and cached updates block enable-agent but still execute existing commands through both launchers', async t => {
  const p = await existing(t, 'v0.1.0');
  const log = path.join(p.home, 'invocations.jsonl');
  for (const mode of ['router', 'ramp']) {
    for (const command of [['configure', '--enable-agent'], ['status'], ['configure', '--enable-agent', '--help']]) {
      const args = mode === 'router' ? command : ['router', ...command];
      const source = `
        require(${JSON.stringify(require.resolve('../cli/package.json'))}).nativeCliVersion = ${JSON.stringify(require('../cli/package.json').nativeCliVersion)};
        Object.defineProperty(process.stdin, 'isTTY', {value:true});
        Object.defineProperty(process.stderr, 'isTTY', {value:true});
        process.getuid = () => 1000;
        require(${JSON.stringify(require.resolve('../cli/lib/launch'))}).main(${JSON.stringify(mode)}, ${JSON.stringify(args)})
          .catch(error => { console.error(error.message); process.exitCode = 1; });
      `;
      const result = spawnSync(process.execPath, ['-e', source], { env: { ...p.env, ROUTER_TEST_LOG: log, ROUTER_TEST_EXIT: '28', npm_command: '' }, encoding: 'utf8', timeout: 10000 });
      if (command.length === 2) {
        assert.equal(result.status, 1, result.stderr);
        assert.match(result.stderr, /requested feature requires 0.2.71.*requested command was not run/s);
      } else {
        assert.equal(result.status, 28, result.stderr); // Preserve the native command's exit status.
        assert.match(result.stdout, new RegExp(command[0]));
      }
    }
  }
  const invocations = fs.readFileSync(log, 'utf8').trim().split('\n').map(line => JSON.parse(line));
  assert.equal(invocations.filter(args => args.includes('update')).length, 1);
  assert.equal(invocations.filter(args => args.includes('status')).length, 2);
  assert.equal(invocations.some(args => args.includes('configure') && !args.includes('--help')), false);
});

test('a working CLI that meets the feature minimum need not match the launcher target', async t => {
  const p = await existing(t, 'v0.2.71');
  const manifest = require('../cli/package.json');
  const target = manifest.nativeCliVersion;
  manifest.nativeCliVersion = '0.2.99';
  t.after(() => { manifest.nativeCliVersion = target; });
  const options = { ...p, autoUpdate: true, requiredVersion: '0.2.71', update: async () => ({ code: 28 }) };
  assert.equal(await resolveCLI(options), p.selected);
  assert.equal(await resolveCLI({ ...options, update: offline }), p.selected, 'cached failure is not a lockout');
});

for (const code of [0, 28]) test(`an updater exiting ${code} must leave a sufficiently new CLI`, async t => {
  const p = await existing(t, 'v0.1.0');
  await assert.rejects(resolveCLI({ ...p, autoUpdate: true, requiredVersion: '0.2.71', update: async () => ({ code }), download: offline }), /requested feature requires 0.2.71.*requested command was not run/s);
  assert.equal(fs.existsSync(path.join(p.root, '.existing-update-lock')), false);
});

for (const damage of ['missing', 'broken']) test(`a ${damage} CLI after updating gives reinstall instructions`, async t => {
  const p = await existing(t, 'v0.1.0');
  await assert.rejects(resolveCLI({ ...p, autoUpdate: true, download: offline, update: async () => {
    if (damage === 'missing') fs.unlinkSync(p.native);
    else fs.writeFileSync(p.native, '#!/bin/sh\nexit 99\n');
    return { code: 0 };
  } }), /could not run after the update.*reinstall with `curl/s);
  assert.equal(fs.existsSync(path.join(p.root, '.existing-update-lock')), false);
});

test('an updater spawn failure still allows existing commands and releases the lock', async t => {
  const p = await existing(t, 'v0.1.0');
  const options = { ...p, autoUpdate: true, update: async () => { throw new Error('spawn EACCES'); } };
  assert.equal(await resolveCLI(options), p.selected);
  await assert.rejects(resolveCLI({ ...options, requiredVersion: '0.2.71' }), /requested feature requires 0.2.71.*Run `ramp update`/s);
  assert.equal(fs.existsSync(path.join(p.root, '.existing-update-lock')), false);
});

test('a current binary can run even if the updater reported a later refresh failure', async t => {
  const p = await existing(t, 'v0.1.0');
  assert.equal(await resolveCLI({ ...p, autoUpdate: true, update: async () => {
    await fixture(p.home, { version: `v${require('../cli/package.json').nativeCliVersion}` });
    return { code: 1 };
  } }), p.selected);
});

for (const result of [{ code: null, signal: 'SIGINT' }, { code: null, signal: 'SIGTERM' }, { code: null, signal: 'SIGHUP' }, { code: 130, signal: null }]) test(`an updater terminated by ${result.signal || `exit ${result.code}`} aborts CLI resolution and releases the lock`, async t => {
  const p = await existing(t, 'v0.1.0');
  let probes = 0;
  await assert.rejects(resolveCLI({ ...p, autoUpdate: true, download: offline,
    run: async () => { probes++; return { stdout: 'ramp, version 0.1.0' }; },
    update: async () => result,
  }), error => result.signal ? error.signal === result.signal : error.exitCode === result.code);
  assert.equal(probes, 2, 'cancellation must not re-probe or continue resolution');
  assert.equal(fs.existsSync(path.join(p.root, '.existing-update-lock')), false);
  assert.equal(fs.existsSync(path.join(p.root, '.existing-update-attempt')), false);
});

for (const selection of ['pin', 'force', 'removed external CLI']) {
  for (const expired of [false, true]) test(`${selection} can install after a${expired ? 'n expired' : ' recent'} external update attempt`, async t => {
    const p = await existing(t, 'v0.1.0');
    assert.equal(await resolveCLI({ ...p, autoUpdate: true, update: async () => ({ code: 1 }), download: offline }), p.selected);
    const attempt = path.join(p.root, '.existing-update-attempt');
    assert.ok(fs.existsSync(attempt));
    assert.equal(fs.existsSync(path.join(p.root, '.ramp-cli-npm')), false);
    if (expired) {
      const day = new Date(Date.now() - 86_400_000);
      fs.utimesSync(attempt, day, day);
    }
    const version = `v${require('../cli/package.json').nativeCliVersion}`;
    const env = { ...p.env };
    if (selection === 'pin') env.RAMP_CLI_VERSION = version;
    if (selection === 'force') env.RAMP_CLI_FORCE_UPDATE = '1';
    if (selection === 'removed external CLI') fs.unlinkSync(p.selected);
    const f = await fixture(p.home, { version });
    assert.equal(await resolveCLI({ ...p, env, download: f.download }), p.executable);
    assert.equal(fs.readFileSync(path.join(p.root, '.ramp-cli-npm'), 'utf8'), '@ramp/cli\n');
  });
}

test('an external CLI without the Router UI gets an actionable error instead of a second installation', async t => {
  const p = await existing(t);
  await assert.rejects(resolveCLI({ ...p, download: offline, run: async (executable, args) => {
    if (args[0] === '--version') return { stdout: 'ramp-cli, version 0.2.49' };
    assert.deepEqual(args, ['router', 'ui', '--help']);
    throw new Error('No Router UI');
  } }), /does not support the Router UI.*ramp update/);
  assert.equal(fs.existsSync(p.root), false);
});

test('generic CLI reuse does not probe or require Router support', async t => {
  const p = await existing(t);
  const calls = [];
  assert.equal(await resolveCLI({ ...p, command: 'ramp', download: offline, run: async (executable, args) => {
    calls.push(args);
    if (args[0] !== '--version') throw new Error('This CLI has no Router support');
    return { stdout: 'ramp-cli, version 0.2.49' };
  } }), p.selected);
  assert.deepEqual(calls, [['--version']]);
  assert.equal(fs.existsSync(p.root), false);
});

test('the CLI npm shim is skipped as well as the Router shim', async t => {
  const p = await existing(t);
  const pkg = path.join(p.home, 'fake cli package');
  fs.mkdirSync(path.join(pkg, 'bin'), { recursive: true });
  fs.writeFileSync(path.join(pkg, 'package.json'), '{"name":"@ramp/cli"}');
  const shim = path.join(pkg, 'bin', 'ramp.js');
  fs.writeFileSync(shim, '#!/bin/sh\nexit 97\n', { mode: 0o755 });
  const directory = path.join(p.home, 'shim bin');
  fs.mkdirSync(directory);
  fs.symlinkSync(shim, path.join(directory, 'ramp'));
  assert.equal((await findExisting({ ...p, env: { ...p.env, PATH: `${directory}:${p.env.PATH}` } })).executable, p.selected);
});

test('explicit release pins use the managed installer instead of an external CLI', async t => {
  const p = await existing(t);
  const f = await fixture(p.home, { version: 'v0.0.0' });
  assert.equal(await resolveCLI({ ...p, env: { ...p.env, RAMP_CLI_VERSION: 'v0.0.0' }, download: f.download }), p.executable);
  assert.equal(fs.existsSync(p.dist), true);
  assert.equal(fs.readlinkSync(p.selected), p.native);
});

test('Windows discovers native exe candidates without invoking npm cmd launchers', async t => {
  const p = sandbox(t);
  const bin = path.join(p.home, 'windows bin');
  fs.mkdirSync(bin);
  const executable = path.join(bin, 'ramp.exe');
  fs.writeFileSync(executable, 'native fixture');
  fs.writeFileSync(path.join(bin, 'ramp.cmd'), 'npm shim');
  const found = await findExisting({ ...p, platform: 'win32', arch: 'x64', env: { ...p.env, PATH: bin }, run: async (file, args) => {
    assert.equal(file, executable);
    assert.deepEqual(args, ['--version']);
    return { stdout: 'ramp-cli, version 0.2.49' };
  } });
  assert.equal(found.executable, executable);
});

test('version comparison handles stable releases, newer versions, and prereleases', () => {
  assert.equal(isOlder('0.2.48', '0.2.49'), true);
  assert.equal(isOlder('0.2.49', '0.2.49'), false);
  assert.equal(isOlder('0.3.0', '0.2.49'), false);
  assert.equal(isOlder('0.2.49-beta.1', '0.2.49'), true);
});

test('a symlinked install root still recognizes the managed CLI as ours', async t => {
  const p = await existing(t);
  const real = path.join(p.home, 'real install');
  fs.mkdirSync(real);
  const link = path.join(p.home, 'linked install');
  fs.symlinkSync(real, link);
  const env = { ...p.env, RAMP_CLI_INSTALL_DIR: link, RAMP_CLI_VERSION: 'v0.0.0' };
  const f = await fixture(p.home);
  const options = { ...p, ...paths({ home: p.home, env }), env };
  const managed = await install({ ...options, download: f.download });
  const bin = path.join(p.home, '.local', 'bin');
  fs.mkdirSync(bin, { recursive: true });
  fs.symlinkSync(managed, path.join(bin, 'ramp'));
  assert.equal(await findExisting({ ...options, env: { ...env, PATH: `${bin}:${p.runtimePath}` } }), null);
});
