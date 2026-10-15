'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { spawn, spawnSync } = require('node:child_process');
const { once } = require('node:events');
const fs = require('node:fs');
const { install } = require('../cli/lib/installer');
const { sandbox, fixture } = require('./helpers');

const bin = name => path.join(__dirname, '..', name === 'ramp' ? 'cli' : 'router', 'bin', `${name}.js`);

test('launcher preserves arguments and exit codes without editing global users shell settings', async t => {
  const p = sandbox(t);
  await install({ ...p, download: (await fixture(p.home)).download });
  const args = ['spaces stay together', '$HOME', '"quotes"', '; untouched', '--flag=value'];
  const result = spawnSync(process.execPath, [bin('ramp'), ...args], { encoding: 'utf8', env: { ...p.env, ROUTER_TEST_EXIT: '17', npm_command: '' } });
  assert.equal(result.status, 17);
  assert.deepEqual(JSON.parse(result.stdout), args);
  assert.equal(fs.existsSync(path.join(p.home, '.bashrc')), false);
});

test('explicit Router commands forward failure without removing the installation', async t => {
  const p = sandbox(t);
  await install({ ...p, download: (await fixture(p.home)).download });
  const result = spawnSync(process.execPath, [bin('router'), 'status'], { encoding: 'utf8', env: { ...p.env, ROUTER_TEST_EXIT: '23', npm_command: '' } });
  assert.equal(result.status, 23);
  assert.deepEqual(JSON.parse(result.stdout), ['router', 'status']);
  assert.ok(fs.existsSync(p.executable));
});

test('help/version work headlessly without shell changes or onboarding', async t => {
  const p = sandbox(t);
  await install({ ...p, download: (await fixture(p.home)).download });
  for (const flag of ['--help', '--version']) {
    const log = path.join(p.home, `${flag}.jsonl`);
    const result = spawnSync(process.execPath, [bin('router'), flag], { encoding: 'utf8', env: { ...p.env, CI: 'true', npm_command: 'exec', ROUTER_TEST_LOG: log } });
    assert.equal(result.status, 0, result.stderr);
    assert.deepEqual(JSON.parse(fs.readFileSync(log, 'utf8')), flag === '--version' ? ['--version'] : ['router', '--help']);
    assert.equal(fs.existsSync(path.join(p.home, '.bashrc')), false);
    assert.equal(fs.existsSync(p.bin), false);
  }
});

test('bare router opens the workspace; explicit subcommands still forward in CI', async t => {
  const p = sandbox(t);
  await install({ ...p, download: (await fixture(p.home)).download });
  const log = path.join(p.home, 'invocations.jsonl');
  const env = { ...p.env, npm_command: 'exec', ROUTER_TEST_LOG: log };
  const result = spawnSync(process.execPath, [bin('router')], { encoding: 'utf8', env, detached: process.platform !== 'win32' });
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(JSON.parse(result.stdout), ['router', 'ui']);
  assert.deepEqual(JSON.parse(fs.readFileSync(log, 'utf8')), ['router', 'ui']);
  assert.equal(fs.existsSync(path.join(p.home, '.bashrc')), false);
  assert.equal(fs.readlinkSync(path.join(p.bin, 'ramp')), p.entry);
  const explicit = spawnSync(process.execPath, [bin('router'), 'status'], { encoding: 'utf8', env: { ...env, CI: 'true', RAMP_ROUTER_SKIP_ONBOARDING: '1' } });
  assert.equal(explicit.status, 0, explicit.stderr);
  assert.deepEqual(JSON.parse(explicit.stdout), ['router', 'status']);
});

test('bare router invokes the workspace without injecting configuration arguments', async t => {
  const p = sandbox(t);
  await install({ ...p, download: (await fixture(p.home)).download });
  const env = { ...p.env, npm_command: 'exec', CI: 'true', RAMP_ROUTER_SKIP_ONBOARDING: '1' };
  const result = spawnSync(process.execPath, [bin('router')], { encoding: 'utf8', env, input: 'not configuration input', detached: process.platform !== 'win32' });
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(JSON.parse(result.stdout), ['router', 'ui']);
  assert.equal(fs.existsSync(path.join(p.home, '.bashrc')), false);
});

test('a failed workspace launch keeps the CLI and preserves the native exit code', async t => {
  const p = sandbox(t);
  await install({ ...p, download: (await fixture(p.home)).download });
  const result = spawnSync(process.execPath, [bin('router')], { encoding: 'utf8', env: { ...p.env, npm_command: 'exec', ROUTER_TEST_EXIT: '23' }, detached: process.platform !== 'win32' });
  assert.equal(result.status, 23, result.stderr);
  assert.match(result.stderr, /remains installed/);
  assert.ok(fs.existsSync(p.executable));
  assert.ok(fs.existsSync(path.join(p.bin, 'router')));
  assert.equal(fs.existsSync(path.join(p.home, '.bashrc')), false);
});

test('router forwards explicit subcommands without adding configure or requiring a terminal', async t => {
  const p = sandbox(t);
  await install({ ...p, download: (await fixture(p.home)).download });
  for (const args of [['status'], ['refresh', '--no-browser'], ['ui'], ['strategies', 'list', '--json', '$literal; value'], ['--output', 'json', 'status']]) {
    const result = spawnSync(process.execPath, [bin('router'), ...args], { encoding: 'utf8', env: { ...p.env, CI: 'true', RAMP_ROUTER_SKIP_ONBOARDING: '1' } });
    assert.equal(result.status, 0, result.stderr);
    assert.deepEqual(JSON.parse(result.stdout), ['router', ...args]);
  }
});

test('SIGINT reaches the native child and preserves cancellation', { timeout: 10_000 }, async t => {
  const p = sandbox(t);
  await install({ ...p, download: (await fixture(p.home)).download });
  const child = spawn(process.execPath, [bin('ramp'), 'wait'], { env: p.env, stdio: ['ignore', 'pipe', 'pipe'] });
  let stderr = '';
  child.stderr.on('data', chunk => { stderr += chunk; });
  const completion = once(child, 'exit');
  t.after(() => { if (child.exitCode === null) child.kill('SIGKILL'); });
  await once(child.stdout, 'data');
  child.kill('SIGINT');
  const [code, signal] = await completion;
  assert.equal(code, 130, stderr);
  assert.equal(signal, null);
});

test('SIGTERM remains a termination signal rather than becoming exit zero', { timeout: 10_000 }, async t => {
  const p = sandbox(t);
  await install({ ...p, download: (await fixture(p.home)).download });
  const child = spawn(process.execPath, [bin('ramp'), 'wait'], { env: p.env, stdio: ['ignore', 'pipe', 'pipe'] });
  let stderr = '';
  child.stderr.on('data', chunk => { stderr += chunk; });
  const completion = once(child, 'exit');
  t.after(() => { if (child.exitCode === null && child.signalCode === null) child.kill('SIGKILL'); });
  await once(child.stdout, 'data');
  child.kill('SIGTERM');
  const [code, signal] = await completion;
  assert.equal(code, null, stderr);
  assert.equal(signal, 'SIGTERM');
});

test('the launcher answers update for a managed CLI and gives it a guarded PATH', async t => {
  const p = sandbox(t);
  const log = path.join(p.home, 'invocations.jsonl');
  await install({ ...p, package: '@ramp/router', download: (await fixture(p.home)).download });
  const env = { ...p.env, ROUTER_TEST_LOG: log, npm_command: '' };
  const update = spawnSync(process.execPath, [bin('ramp'), '--quiet', 'update'], { encoding: 'utf8', env });
  assert.equal(update.status, 0, update.stderr);
  assert.match(update.stdout, /npm update -g @ramp\/router/);
  assert.equal(fs.existsSync(log), false);
  const child = JSON.parse(spawnSync(process.execPath, [bin('ramp'), 'env'], { encoding: 'utf8', env }).stdout);
  assert.equal(child.noUpdateCheck, '1');
  assert.equal(child.path.split(path.delimiter)[0], path.dirname(p.entry));
});

test('a reused CLI from another installer keeps its own update command and notices', async t => {
  const p = sandbox(t);
  const version = `v${require('../cli/package.json').nativeCliVersion}`;
  await fixture(p.home, { version });
  const other = path.join(p.home, 'other bin');
  fs.mkdirSync(other);
  fs.symlinkSync(path.join(p.home, 'fixture', 'main.dist', p.name), path.join(other, 'ramp'));
  const env = { ...p.env, RAMP_CLI_VERSION: '', npm_command: '', PATH: `${other}:${p.env.PATH}` };
  assert.deepEqual(JSON.parse(spawnSync(process.execPath, [bin('ramp'), 'update'], { encoding: 'utf8', env }).stdout), ['update']);
  assert.equal(JSON.parse(spawnSync(process.execPath, [bin('ramp'), 'env'], { encoding: 'utf8', env }).stdout).noUpdateCheck, null);
});

test('global layout is detected for launch-time receipt refresh, never for npx or projects', t => {
  const { globalLayout } = require('../cli/lib/launch');
  const root = fs.mkdtempSync(path.join(require('node:os').tmpdir(), 'ramp-layout-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const dir = (...parts) => path.join(root, ...parts);
  assert.equal(globalLayout(dir('lib', 'node_modules', '@ramp', 'cli'), {}), true);
  assert.equal(globalLayout(dir('lib', 'node_modules', '@ramp', 'cli'), { npm_command: 'exec' }), false);
  assert.equal(globalLayout(dir('.npm', '_npx', 'abc', 'node_modules', '@ramp', 'cli'), {}), false);
  fs.mkdirSync(dir('project'), { recursive: true });
  fs.writeFileSync(dir('project', 'package.json'), '{"name":"app"}');
  assert.equal(globalLayout(dir('project', 'node_modules', '@ramp', 'cli'), {}), false);
});

test('a CLI nested under a project-local Router is not a global install, but one under a global Router is', t => {
  const { globalLayout } = require('../cli/lib/launch');
  const root = fs.mkdtempSync(path.join(require('node:os').tmpdir(), 'ramp-nested-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const router = (...base) => { const dir = path.join(root, ...base, 'node_modules', '@ramp', 'router'); fs.mkdirSync(dir, { recursive: true }); fs.writeFileSync(path.join(dir, 'package.json'), '{"name":"@ramp/router"}'); return dir; };
  const globalRouter = router('lib');
  assert.equal(globalLayout(path.join(globalRouter, 'node_modules', '@ramp', 'cli'), {}), true);
  fs.mkdirSync(path.join(root, 'project'), { recursive: true });
  fs.writeFileSync(path.join(root, 'project', 'package.json'), '{"name":"app"}');
  const localRouter = router('project');
  assert.equal(globalLayout(path.join(localRouter, 'node_modules', '@ramp', 'cli'), {}), false);
});

test('a reused native CLI finds its stable ramp even when its binary has another name', t => {
  const { stableRampDir } = require('../cli/lib/launch');
  const root = fs.realpathSync(fs.mkdtempSync(path.join(require('node:os').tmpdir(), 'ramp-stable-')));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const real = path.join(root, 'lib', 'ramp-native');
  fs.mkdirSync(path.dirname(real), { recursive: true });
  fs.writeFileSync(real, '#!/bin/sh\n', { mode: 0o755 });
  const bin = path.join(root, 'bin');
  fs.mkdirSync(bin);
  fs.symlinkSync(real, path.join(bin, 'ramp'));
  const shim = path.join(root, '_npx', 'x', 'bin');
  fs.mkdirSync(shim, { recursive: true });
  fs.symlinkSync(real, path.join(shim, 'ramp'));
  if (process.platform !== 'win32') assert.equal(stableRampDir(real, { PATH: `${shim}${path.delimiter}${bin}` }), bin);
});
