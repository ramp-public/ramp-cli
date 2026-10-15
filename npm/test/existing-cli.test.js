'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { resolveCLI, findExisting, isOlder } = require('../cli/lib/existing-cli');
const { install, paths } = require('../cli/lib/installer');
const { sandbox, fixture } = require('./helpers');

async function existing(t, version = `v${require('../cli/package.json').nativeCliVersion}`) {
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

test('declining an update preserves and reuses an older CLI that supports Router', async t => {
  const p = await existing(t, 'v0.1.0');
  let asked = 0;
  const selected = await resolveCLI({ ...p, session: { write() {} }, confirm: async () => { asked++; return false; }, update: offline, download: offline });
  assert.equal(selected, p.selected);
  assert.equal(asked, 1);
  assert.equal(fs.existsSync(p.root), false);
});

test('headless reuse does not update an older installation or ask questions', async t => {
  const p = await existing(t, 'v0.1.0');
  assert.equal(await resolveCLI({ ...p, update: offline, confirm: offline, download: offline }), p.selected);
  assert.equal(fs.existsSync(p.root), false);
});

test('approved updates call the existing CLI updater and recheck its version', async t => {
  const p = await existing(t, 'v0.1.0');
  let updated = 0;
  const session = { write() {}, stdio: 'inherit' };
  const selected = await resolveCLI({ ...p, session, confirm: async () => true, download: offline, update: async (executable, args, stdio) => {
    assert.equal(executable, p.selected);
    assert.deepEqual(args, ['update']);
    assert.equal(stdio, session.stdio);
    updated++;
    await fixture(p.home, { version: `v${require('../cli/package.json').nativeCliVersion}` });
    return { code: 0 };
  } });
  assert.equal(selected, p.selected);
  assert.equal(updated, 1);
  assert.equal(fs.existsSync(p.dist), false);
  assert.equal(fs.existsSync(path.join(p.root, '.existing-update-lock')), false);
});

test('failed optional updates can still use a working compatible external CLI', async t => {
  const p = await existing(t, 'v0.1.0');
  const messages = [];
  assert.equal(await resolveCLI({ ...p, session: { write: value => messages.push(value) }, confirm: async () => true, update: async () => ({ code: 23 }), download: offline }), p.selected);
  assert.ok(messages.some(text => text.includes('did not complete')));
  assert.equal(fs.existsSync(p.dist), false);
});

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
