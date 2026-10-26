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

test('only the known agent-enablement feature has a native version floor', () => {
  const { requiredVersion } = require('../cli/lib/launch');
  assert.equal(requiredVersion(['router', 'configure', '--enable-agent']), '0.2.71');
  assert.equal(requiredVersion(['--no-input', 'router', 'configure', '--enable-agent']), '0.2.71');
  assert.equal(requiredVersion(['router', '--human', 'configure', '--enable-agent']), '0.2.71');
  for (const args of [[], ['router'], ['router', 'status'], ['auth', 'login'], ['router', 'configure'], ['router', 'configure', '--enable-agent', '--help'], ['--version']]) {
    assert.equal(requiredVersion(args), null);
  }
});

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

test('bare router delegates terminal handling to native Router; subcommands still forward in CI', async t => {
  const p = sandbox(t);
  await install({ ...p, download: (await fixture(p.home)).download });
  const log = path.join(p.home, 'invocations.jsonl');
  const tuiLog = path.join(p.home, 'tui.jsonl');
  const env = { ...p.env, npm_command: 'exec', ROUTER_TEST_LOG: log, ROUTER_TEST_TUI_LOG: tuiLog, RAMP_TUI: '' };
  const result = spawnSync(process.execPath, [bin('router')], { encoding: 'utf8', env, detached: process.platform !== 'win32' });
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(JSON.parse(result.stdout), ['router']);
  assert.deepEqual(JSON.parse(fs.readFileSync(log, 'utf8')), ['router']);
  assert.equal(fs.readFileSync(tuiLog, 'utf8').trim().split('\n').at(-1), '"1"');
  assert.equal(fs.existsSync(path.join(p.home, '.bashrc')), false);
  assert.equal(fs.readlinkSync(path.join(p.bin, 'ramp')), p.entry);
  const explicit = spawnSync(process.execPath, [bin('router'), 'status'], { encoding: 'utf8', env: { ...env, CI: 'true', RAMP_ROUTER_SKIP_ONBOARDING: '1' } });
  assert.equal(explicit.status, 0, explicit.stderr);
  assert.deepEqual(JSON.parse(explicit.stdout), ['router', 'status']);
  assert.equal(fs.readFileSync(tuiLog, 'utf8').trim().split('\n').at(-1), 'null');
  const ramp = spawnSync(process.execPath, [bin('ramp'), 'router'], { encoding: 'utf8', env });
  assert.equal(ramp.status, 0, ramp.stderr);
  assert.equal(fs.readFileSync(tuiLog, 'utf8').trim().split('\n').at(-1), 'null');
});

test('bare headless router preserves native help exit code without a workspace retry warning', async t => {
  const p = sandbox(t);
  await install({ ...p, download: (await fixture(p.home)).download });
  const env = { ...p.env, npm_command: 'exec', CI: 'true', RAMP_ROUTER_SKIP_ONBOARDING: '1', ROUTER_TEST_EXIT: '2' };
  const result = spawnSync(process.execPath, [bin('router')], { encoding: 'utf8', env, input: 'not configuration input', detached: process.platform !== 'win32' });
  assert.equal(result.status, 2, result.stderr);
  assert.deepEqual(JSON.parse(result.stdout), ['router']);
  assert.equal(result.stderr, '');
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

for (const [signal, exitCode, mode = 'ramp'] of [
  ['SIGINT', null], ['SIGTERM', null], ['SIGHUP', null], ['SIGINT', 130], ['SIGINT', 130, 'router'],
]) test(`${mode}: ${signal} during auto-update${exitCode ? ` handled as exit ${exitCode}` : ''} cancels the launch without running the requested write`, { timeout: 10_000, skip: process.platform === 'win32' }, async t => {
  const p = sandbox(t);
  const other = path.join(p.home, 'other bin');
  fs.mkdirSync(other);
  const log = path.join(p.home, 'invocations.jsonl');
  fs.writeFileSync(path.join(other, 'ramp'), `#!/usr/bin/env node
const fs = require('node:fs');
const args = process.argv.slice(2);
fs.appendFileSync(process.env.ROUTER_TEST_LOG, JSON.stringify(args) + '\\n');
if (args[0] === '--version') console.log('ramp, version 0.1.0');
else if (args.includes('update')) {
  // Like Click Abort in the real CLI, handle SIGINT with a numeric exit status.
  if (${exitCode} === 130) process.on('SIGINT', () => process.exit(130));
  setInterval(() => {}, 1000); console.error('UPDATE_READY');
}
else { console.log('REQUESTED_WRITE_RAN'); }
`, { mode: 0o755 });
  const script = `
process.getuid = () => 1000;
Object.defineProperty(process.stdin, 'isTTY', { value: true });
Object.defineProperty(process.stderr, 'isTTY', { value: true });
require(${JSON.stringify(require.resolve('../cli/package.json'))}).nativeCliVersion = '1.0.0';
process.argv = [process.execPath, ${JSON.stringify(bin(mode))}, ...${JSON.stringify(mode === 'ramp' ? ['funds', 'create'] : ['configure'])}];
require(${JSON.stringify(bin(mode))});
`;
  const child = spawn(process.execPath, ['-e', script], { env: { ...p.env, RAMP_CLI_VERSION: '', RAMP_NO_UPDATE_CHECK: '', PATH: `${other}:${p.runtimePath}`, ROUTER_TEST_LOG: log }, stdio: ['ignore', 'pipe', 'pipe'] });
  let stderr = '', stdout = '';
  child.stdout.on('data', chunk => { stdout += chunk; });
  const ready = new Promise(resolve => child.stderr.on('data', chunk => { stderr += chunk; if (stderr.includes('UPDATE_READY')) resolve(); }));
  const completion = once(child, 'exit');
  t.after(() => { if (child.exitCode === null && child.signalCode === null) child.kill('SIGKILL'); });
  await ready;
  child.kill(signal);
  const [code, actualSignal] = await completion;
  assert.equal(actualSignal, exitCode ? null : signal, stderr);
  assert.equal(code, exitCode, stderr);
  assert.equal(stdout, '');
  assert.deepEqual(fs.readFileSync(log, 'utf8').trim().split('\n').map(line => JSON.parse(line)), [['--version'], ['--version'], ['--no-input', 'update']]);
  assert.equal(fs.existsSync(path.join(p.root, '.existing-update-lock')), false);
  assert.equal(fs.existsSync(path.join(p.root, '.existing-update-attempt')), false);
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
