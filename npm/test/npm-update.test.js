'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { update: realUpdate, npmCommand, validateArguments, legacyHelpArguments } = require('../cli/lib/npm-update');
// Unit tests stub the native-parser boundary. Process-level tests below use
// the real helper/validator, and public pytest covers native Click validation.
const update = options => realUpdate({ validate: async () => {}, ...options });
const { installationContext } = require('../cli/lib/npm-context');
const { install } = require('../cli/lib/installer');
const { sandbox, fixture } = require('./helpers');

function receipt(p, context, extra = {}) {
  fs.mkdirSync(p.dist, { recursive: true });
  fs.writeFileSync(p.state, JSON.stringify({ owner: '@ramp/cli', package: '@ramp/router', version: 'v1.2.3', updateContext: context, ...extra }));
}

function globalContext(p) {
  const prefix = path.join(p.home, 'npm prefix $cash');
  const modules = path.join(prefix, 'lib', 'node_modules');
  const directory = path.join(modules, '@ramp', 'router');
  fs.mkdirSync(directory, { recursive: true });
  fs.writeFileSync(path.join(directory, 'package.json'), JSON.stringify({ name: '@ramp/router' }));
  return { kind: 'global', prefix, modules };
}

test('records global prefixes, nested Router dependencies, npx and project ownership', t => {
  const p = sandbox(t);
  const context = globalContext(p);
  const cliDir = path.join(context.modules, '@ramp', 'cli');
  assert.deepEqual(installationContext({ cliDir, env: {} }), context);
  assert.deepEqual(installationContext({ cliDir: path.join(context.modules, '@ramp', 'router', 'node_modules', '@ramp', 'cli'), env: {} }), context);
  assert.deepEqual(installationContext({ cliDir, env: { npm_command: 'exec' } }), { kind: 'npx' });
  assert.deepEqual(installationContext({ cliDir: path.join(p.home, '_npx', '123', 'node_modules', '@ramp', 'cli'), env: {} }), { kind: 'npx' });
  fs.writeFileSync(path.join(p.home, 'package.json'), '{"name":"project"}');
  assert.deepEqual(installationContext({ cliDir: path.join(p.home, 'node_modules', '@ramp', 'cli'), env: {} }), { kind: 'project' });
  assert.deepEqual(installationContext({ cliDir: path.join(p.home, 'node_modules', '@ramp', 'cli'), env: { npm_command: 'exec' } }), { kind: 'project' });
  const localRouter = path.join(p.home, 'node_modules', '@ramp', 'router');
  fs.mkdirSync(localRouter, { recursive: true });
  fs.writeFileSync(path.join(localRouter, 'package.json'), '{"name":"@ramp/router"}');
  assert.deepEqual(installationContext({ cliDir: path.join(localRouter, 'node_modules', '@ramp', 'cli'), env: { npm_command: 'exec' } }), { kind: 'project' });
  const windows = installationContext({ cliDir: path.join(context.prefix, 'node_modules', '@ramp', 'cli'), env: {}, platform: 'win32' });
  assert.equal(windows.prefix, context.prefix);
});

test('global updates use the owning package and exact prefix without onboarding or native pins', async t => {
  const p = sandbox(t);
  const context = globalContext(p);
  receipt(p, context);
  const commands = [];
  await update({ directory: p.dist, env: { ...p.env, npm_config_package: 'other@1', npm_config_ignore_scripts: 'true', RAMP_CLI_FORCE_UPDATE: '1' }, write() {}, execute: async (command, env) => {
    commands.push(command);
    assert.equal(env.RAMP_NPM_UPDATE, '1');
    assert.equal(env.RAMP_ROUTER_SKIP_ONBOARDING, '1');
    assert.equal(env.RAMP_CLI_INSTALL_DIR, p.root);
    assert.equal(env.RAMP_CLI_VERSION, undefined);
    assert.equal(env.RAMP_CLI_FORCE_UPDATE, undefined);
    assert.equal(env.npm_config_package, undefined);
    assert.equal(env.npm_config_ignore_scripts, undefined);
  } });
  assert.deepEqual(commands[0], ['npm', 'update', '--global', '--prefix', context.prefix, '@ramp/router', '--ignore-scripts=false', '--foreground-scripts', '--registry=https://registry.npmjs.org/']);
});

test('npx updates run the latest owning package informationally, never globally or bare Router', async t => {
  const p = sandbox(t);
  receipt(p, { kind: 'npx' });
  let command;
  await update({ directory: p.dist, env: p.env, write() {}, execute: async value => { command = value; } });
  assert.deepEqual(command, ['npm', 'exec', '--yes', '--package=@ramp/router@latest', '--ignore-scripts=false', '--foreground-scripts', '--registry=https://registry.npmjs.org/', '--', 'router', '--version']);
  assert.equal(command.includes('--global'), false);
});

test('projects do not update shared global installs; unknown receipts fall back to instructions', async t => {
  const p = sandbox(t);
  receipt(p, globalContext(p));
  const messages = [];
  const execute = () => { throw new Error('Must not run npm'); };
  await update({ directory: p.dist, context: { kind: 'project' }, write: text => messages.push(text), execute });
  assert.match(messages[0], /project package manager and lockfile/);
  receipt(p, { kind: 'unknown' });
  await update({ directory: p.dist, write: text => messages.push(text), execute });
  assert.match(messages[1], /npm update -g @ramp\/router/);
});

test('npx keeps the recorded global owner; missing or inconsistent owners fail closed', async t => {
  const p = sandbox(t);
  const context = globalContext(p);
  receipt(p, context);
  let command;
  await update({ directory: p.dist, context: { kind: 'npx' }, write() {}, execute: async value => { command = value; } });
  assert.equal(command[1], 'update');
  receipt(p, { ...context, prefix: p.home });
  await assert.rejects(update({ directory: p.dist, write() {} }), /inconsistent global prefix/);
  receipt(p, context);
  fs.unlinkSync(path.join(context.modules, '@ramp', 'router', 'package.json'));
  await assert.rejects(update({ directory: p.dist, write() {} }), /ENOENT/);
  receipt(p, { kind: 'npx' }, { channel: 'internal' });
  await assert.rejects(update({ directory: p.dist, write() {} }), /only supports public/);
});

test('npm failures and pending refreshes are reported instead of claiming update success', async t => {
  const p = sandbox(t);
  receipt(p, { kind: 'npx' });
  await assert.rejects(update({ directory: p.dist, write() {}, execute: async () => { throw new Error('offline'); } }), /offline/);
  receipt(p, { kind: 'npx' }, { refreshPending: true });
  await assert.rejects(update({ directory: p.dist, write() {}, execute: async () => {} }), /Router refresh failed/);
});

test('Windows invokes npm through JS, without interpolating a shell command', t => {
  const p = sandbox(t);
  const script = path.join(p.home, 'npm-cli.js');
  fs.writeFileSync(script, '');
  assert.deepEqual(npmCommand(['update', '--prefix', 'C:\\prefix & spaces'], { npm_execpath: script }, 'win32'), [process.execPath, script, 'update', '--prefix', 'C:\\prefix & spaces']);
});

test('persistent and npm launchers execute npm updates without running the native updater', async t => {
  const p = sandbox(t);
  await install({ ...p, download: (await fixture(p.home)).download });
  const state = JSON.parse(fs.readFileSync(p.state));
  const context = globalContext(p);
  fs.writeFileSync(p.state, JSON.stringify({ ...state, package: '@ramp/router', updateContext: context }));
  // No real registry access: this fake npm records exactly what the production
  // helper spawns, including prefix paths with spaces and metacharacters.
  const npm = path.join(p.home, 'runtime-bin', 'npm');
  fs.unlinkSync(npm);
  fs.writeFileSync(npm, '#!/usr/bin/env node\nrequire("node:fs").appendFileSync(process.env.NPM_TEST_LOG, JSON.stringify(process.argv.slice(2)) + "\\n");\n', { mode: 0o755 });
  const log = path.join(p.home, 'npm-calls.jsonl');
  const nativeLog = path.join(p.home, 'native-calls.jsonl');
  const env = { ...p.env, NPM_TEST_LOG: log, ROUTER_TEST_LOG: nativeLog };
  for (const [command, args] of [[p.entry, ['-qo', 'json', 'update']], [process.execPath, [path.join(__dirname, '../cli/bin/ramp.js'), 'update']]]) {
    const result = spawnSync(command, args, { env, encoding: 'utf8' });
    assert.equal(result.status, 0, result.stderr);
    assert.match(result.stdout, /Updating @ramp\/router through npm/);
  }
  assert.equal(fs.existsSync(nativeLog), false);
  assert.equal(fs.readFileSync(log, 'utf8').trim().split('\n').length, 2);
});

test('an upgraded distribution performs full Router refresh, never skills, and retries a failure', async t => {
  const p = sandbox(t);
  await install({ ...p, download: (await fixture(p.home)).download });
  const next = await fixture(p.home, { version: 'v0.0.1' });
  const calls = [];
  const options = { ...p, env: { ...p.env, RAMP_CLI_VERSION: 'v0.0.1' }, download: next.download, refreshRun: async (executable, args, settings) => {
    calls.push(args);
    assert.equal(executable, p.executable);
    assert.equal(settings.env.RAMP_NO_UPDATE_CHECK, '1');
    assert.equal(settings.env.PATH.split(path.delimiter)[0], p.dist);
    throw new Error('Router offline');
  } };
  await install(options);
  assert.deepEqual(calls, [['--no-input', 'router', 'refresh']]);
  assert.equal(JSON.parse(fs.readFileSync(p.state)).refreshPending, true);
  await install({ ...options, download: () => { throw new Error('No download expected'); }, refreshRun: async (_executable, args) => { calls.push(args); return {}; } });
  assert.equal(JSON.parse(fs.readFileSync(p.state)).refreshPending, false);
  assert.deepEqual(calls[1], ['--no-input', 'router', 'refresh']);
  assert.ok(fs.existsSync(p.executable));
});

test('Router owns an npx receipt in either lifecycle order without taking over global ownership', async t => {
  for (const order of [['@ramp/cli', '@ramp/router'], ['@ramp/router', '@ramp/cli']]) {
    const p = sandbox(t);
    const options = { ...p, cliDir: path.join(p.home, '_npx', '123', 'node_modules', '@ramp', 'cli'), download: (await fixture(p.home)).download };
    for (const packageName of order) await install({ ...options, package: packageName });
    assert.equal(JSON.parse(fs.readFileSync(p.state)).package, '@ramp/router');
    const state = JSON.parse(fs.readFileSync(p.state));
    fs.writeFileSync(p.state, JSON.stringify({ ...state, package: '@ramp/cli', updateContext: globalContext(p) }));
    await install({ ...options, package: '@ramp/router' });
    assert.equal(JSON.parse(fs.readFileSync(p.state)).package, '@ramp/cli');
  }
});

test('all update entrypoints forward invalid arguments for native validation before any npm update or refresh', async t => {
  const p = sandbox(t);
  await install({ ...p, download: (await fixture(p.home)).download });
  const state = JSON.parse(fs.readFileSync(p.state));
  fs.writeFileSync(p.state, JSON.stringify({ ...state, package: '@ramp/router', updateContext: globalContext(p), refreshPending: true }));
  const before = fs.readFileSync(p.state, 'utf8');
  const npm = path.join(p.home, 'runtime-bin', 'npm');
  fs.unlinkSync(npm);
  fs.writeFileSync(npm, '#!/usr/bin/env node\nrequire("node:fs").writeFileSync(process.env.NPM_TEST_LOG, "unexpected npm update");\n', { mode: 0o755 });
  const npmLog = path.join(p.home, 'npm-calls');
  const nativeLog = path.join(p.home, 'native-calls');
  const validationLog = path.join(p.home, 'validation-calls');
  const env = { ...p.env, NPM_TEST_LOG: npmLog, ROUTER_TEST_LOG: nativeLog, ROUTER_TEST_VALIDATION_LOG: validationLog, ROUTER_TEST_VALIDATE_EXIT: '2' };
  for (const args of [['update', '--dry-run'], ['update', 'bogus'], ['--env', 'bogus', 'update'], ['update', '--profile', 'bogus'], ['--agent', '--human', 'update']]) {
    for (const command of [p.entry, process.execPath]) {
      const forwarded = command === process.execPath ? [path.join(__dirname, '../cli/bin/ramp.js'), ...args] : args;
      const result = spawnSync(command, forwarded, { env, encoding: 'utf8' });
      assert.equal(result.status, 2, result.stderr);
      const validations = fs.readFileSync(validationLog, 'utf8').trim().split('\n').map(JSON.parse);
      assert.deepEqual(validations.at(-1), [...args, '--_npm-validate']);
      assert.equal(fs.existsSync(npmLog), false);
      assert.equal(fs.existsSync(nativeLog), false);
      assert.equal(fs.readFileSync(p.state, 'utf8'), before);
    }
  }
  await assert.rejects(validateArguments(p.dist, JSON.parse(before), ['update', 'bogus'], env), error => error.exitCode === 2);
});

test('legacy argument shape permits root flags but never forwards update-specific arguments to help', () => {
  for (const [args, expected] of [
    [['update'], ['update', '--help']],
    [['update', '--quiet', '--env', 'sandbox'], ['--quiet', '--env', 'sandbox', 'update', '--help']],
    [['--env=sandbox', '-qo', 'json', 'update'], ['--env=sandbox', '-qo', 'json', 'update', '--help']],
    [['-qqojson', '-esandbox', 'update'], ['-qqojson', '-esandbox', 'update', '--help']],
    [['--', 'update'], ['--', 'update', '--help']],
    [['update', '--'], ['update', '--help']],
  ]) assert.deepEqual(legacyHelpArguments(args), expected);
  for (const args of [
    ['update', '--dry-run'], ['update', 'bogus'], ['update', '--help'], ['update', '--', 'bogus'],
    ['--env'], ['update', '-o'], ['-x', 'update'], ['--unknown', 'update'], ['-q', 'other'], [],
    ['update', '--env=sandbox'], ['update', '-qojson'],
  ]) assert.throws(() => legacyHelpArguments(args), error => error.exitCode === 2);
});

test('older pinned parsers can update through both launchers without invoking the native updater', async t => {
  const p = sandbox(t);
  await install({ ...p, env: { ...p.env, RAMP_CLI_VERSION: 'v0.2.59' }, download: (await fixture(p.home, { version: 'v0.2.59' })).download });
  const state = JSON.parse(fs.readFileSync(p.state));
  fs.writeFileSync(p.state, JSON.stringify({ ...state, updateContext: globalContext(p) }));
  const npm = path.join(p.home, 'runtime-bin', 'npm');
  fs.unlinkSync(npm);
  fs.writeFileSync(npm, '#!/usr/bin/env node\nrequire("node:fs").appendFileSync(process.env.NPM_TEST_LOG, JSON.stringify(process.argv.slice(2)) + "\\n");\n', { mode: 0o755 });
  const npmLog = path.join(p.home, 'npm-calls');
  const nativeLog = path.join(p.home, 'native-calls');
  const validationLog = path.join(p.home, 'validation-calls');
  const env = { ...p.env, RAMP_CLI_VERSION: 'v0.2.59', ROUTER_TEST_LEGACY: '1', NPM_TEST_LOG: npmLog, ROUTER_TEST_LOG: nativeLog, ROUTER_TEST_VALIDATION_LOG: validationLog };
  for (const command of [p.entry, process.execPath]) {
    const args = ['-qo', 'json', 'update', '--env', 'sandbox'];
    const result = spawnSync(command, command === p.entry ? args : [path.join(__dirname, '../cli/bin/ramp.js'), ...args], { env, encoding: 'utf8' });
    assert.equal(result.status, 0, result.stderr);
    assert.match(result.stdout, /Updating @ramp\/router through npm/);
    const validations = fs.readFileSync(validationLog, 'utf8').trim().split('\n').map(JSON.parse);
    assert.deepEqual(validations.at(-2), ['update', '--_npm-validate']);
    assert.deepEqual(validations.at(-1), ['--env', 'sandbox', '-qo', 'json', 'update', '--help']);
  }
  assert.equal(fs.readFileSync(npmLog, 'utf8').trim().split('\n').length, 2);
  assert.equal(fs.existsSync(nativeLog), false);
});

test('legacy validation rejects malformed commands and invalid root values without updating or refreshing', async t => {
  const p = sandbox(t);
  await install({ ...p, download: (await fixture(p.home)).download });
  const state = JSON.parse(fs.readFileSync(p.state));
  fs.writeFileSync(p.state, JSON.stringify({ ...state, updateContext: globalContext(p), refreshPending: true }));
  const before = fs.readFileSync(p.state, 'utf8');
  const nativeLog = path.join(p.home, 'native-calls');
  const env = { ...p.env, ROUTER_TEST_LEGACY: '1', ROUTER_TEST_LOG: nativeLog };
  for (const args of [['update', '--dry-run'], ['update', 'bogus'], ['--env', 'bogus', 'update'], ['--profile', 'missing', 'update'], ['--agent', '--human', 'update']]) {
    // The stand-in's help exit models invalid values; shape failures must be
    // rejected before help, while public tests cover the real Click callbacks.
    await assert.rejects(realUpdate({ directory: p.dist, args, env: { ...env, ROUTER_TEST_VALIDATE_EXIT: '2' }, write() {}, execute: () => { assert.fail('Must not run npm'); } }), error => error.exitCode === 2);
    assert.equal(fs.existsSync(nativeLog), false);
    assert.equal(fs.readFileSync(p.state, 'utf8'), before);
  }
});

test('unrelated native probe failures never enable legacy fallback', async t => {
  const p = sandbox(t);
  await install({ ...p, download: (await fixture(p.home)).download });
  const record = JSON.parse(fs.readFileSync(p.state));
  const log = path.join(p.home, 'validation-calls');
  for (const exitCode of [1, 2]) {
    fs.writeFileSync(log, '');
    await assert.rejects(validateArguments(p.dist, record, ['update'], { ...p.env, ROUTER_TEST_PROBE_EXIT: String(exitCode), ROUTER_TEST_VALIDATION_LOG: log }), error => error.exitCode === exitCode);
    assert.deepEqual(fs.readFileSync(log, 'utf8').trim().split('\n').map(JSON.parse), [['update', '--_npm-validate']]);
  }
});
