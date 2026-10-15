'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { exposeCommand, configurePath, refreshPath } = require('../cli/lib/commands');
const { sandbox } = require('./helpers');
const { install } = require('../cli/lib/installer');
const { fixture } = require('./helpers');

test('persistent command references the native distribution and preserves unrelated ramp commands', t => {
  const p = sandbox(t);
  assert.equal(exposeCommand(p), p.bin);
  assert.equal(fs.readlinkSync(path.join(p.bin, 'ramp')), p.entry);
  assert.equal(exposeCommand(p), p.bin); // Even a temporarily dangling owned symlink is reusable.
  fs.unlinkSync(path.join(p.bin, 'ramp'));
  fs.writeFileSync(path.join(p.bin, 'ramp'), 'unrelated command');
  assert.throws(() => exposeCommand(p), /preserved/);
  assert.equal(fs.readFileSync(path.join(p.bin, 'ramp'), 'utf8'), 'unrelated command');
});

test('a managed ramp link to another architecture is retargeted', t => {
  const p = sandbox(t);
  const other = path.join(p.root, 'darwin-other', 'ramp-darwin-other');
  fs.mkdirSync(p.bin, { recursive: true });
  fs.symlinkSync(other, path.join(p.bin, 'ramp'));
  assert.equal(exposeCommand(p), p.bin);
  assert.equal(fs.readlinkSync(path.join(p.bin, 'ramp')), p.entry);
  assert.equal(fs.readdirSync(p.bin).some(name => name.startsWith('.ramp-')), false);
});

test('shell PATH edits preserve settings, handle shell metacharacters, and do not duplicate entries', t => {
  const p = sandbox(t);
  const bin = path.join(p.home, "bin 'with' $dollars `backticks`");
  fs.writeFileSync(path.join(p.home, '.bashrc'), '# existing setting\n');
  configurePath(bin, p);
  configurePath(bin, p);
  const text = fs.readFileSync(path.join(p.home, '.bashrc'), 'utf8');
  assert.equal(text.split('# Added by @ramp/cli').length, 2);
  assert.ok(text.startsWith('# existing setting\n'));
  const probe = spawnSync('bash', ['-c', 'source "$1"; printf "%s" "$PATH"', '--', path.join(p.home, '.bashrc')], { encoding: 'utf8', env: p.env });
  assert.equal(probe.status, 0);
  assert.equal(probe.stdout.split(':')[0], bin);
});

test('unknown shells get manual instructions without creating startup files', t => {
  const p = sandbox(t);
  assert.throws(() => configurePath(p.bin, { ...p, env: { ...p.env, SHELL: '/unknown' } }), /future terminals/);
  assert.equal(fs.existsSync(path.join(p.home, '.profile')), false);
});

test('zsh honors ZDOTDIR and fish honors XDG_CONFIG_HOME', t => {
  const p = sandbox(t);
  const dot = path.join(p.home, 'zdot');
  configurePath(p.bin, { ...p, env: { ...p.env, SHELL: '/bin/zsh', ZDOTDIR: dot } });
  assert.match(fs.readFileSync(path.join(dot, '.zshrc'), 'utf8'), /export PATH/);
  const xdg = path.join(p.home, 'xdg');
  configurePath(p.bin, { ...p, env: { ...p.env, SHELL: '/bin/fish', XDG_CONFIG_HOME: xdg } });
  assert.match(fs.readFileSync(path.join(xdg, 'fish/config.fish'), 'utf8'), /fish_add_path/);
});

test('Windows writes a cache-independent cmd launcher and handles an empty user PATH', t => {
  const p = sandbox(t);
  const options = { ...p, platform: 'win32', arch: 'x64' };
  exposeCommand(options);
  const cmd = fs.readFileSync(path.join(p.bin, 'ramp.cmd'), 'utf8');
  assert.match(cmd, /ramp-windows-amd64\.exe/);
  assert.match(cmd, /%\*/);
  assert.equal(fs.existsSync(path.join(p.bin, 'ramp.ps1')), false);
  assert.equal(cmd.includes('_npx'), false);
  const router = fs.readFileSync(path.join(p.bin, 'router.cmd'), 'utf8');
  assert.match(router, /router ui/);
  assert.match(router, /router %\*/);
  assert.equal(router.includes('_npx'), false);
  let script;
  configurePath(p.bin, { ...options, env: { ...p.env, PATH: '' }, spawnSync: (command, args) => {
    assert.equal(command, 'powershell.exe');
    script = Buffer.from(args.at(-1), 'base64').toString('utf16le');
    return { status: 0 };
  } });
  assert.match(script, /GetEnvironmentVariable\('Path','User'\)/);
  assert.equal(script.includes('$p.TrimEnd'), false);
  assert.match(script, /SetEnvironmentVariable/);
});


test('accepting refresh saves future PATH and updates onboarding environment', async t => {
  const p = sandbox(t);
  let asked = 0;
  await refreshPath({ write() {} }, { ...p, confirm: async () => { asked++; return true; } });
  assert.equal(asked, 1);
  assert.equal(p.env.PATH.split(':')[0], p.bin);
  assert.ok(fs.existsSync(path.join(p.home, '.bashrc')));
  await refreshPath({ write() {} }, { ...p, confirm: async () => { throw new Error('Already on PATH'); } });
});

test('declining refresh preserves shell configuration and PATH while keeping the CLI installed', async t => {
  const p = sandbox(t);
  const before = p.env.PATH;
  await refreshPath({ write() {} }, { ...p, confirm: async () => false });
  assert.equal(p.env.PATH, before);
  assert.equal(fs.existsSync(path.join(p.home, '.bashrc')), false);
  assert.equal(fs.readlinkSync(path.join(p.bin, 'ramp')), p.entry);
});

test('headless refresh never changes shell settings or PATH', async t => {
  const p = sandbox(t);
  const before = p.env.PATH;
  await refreshPath(null, p);
  assert.equal(p.env.PATH, before);
  assert.equal(fs.existsSync(path.join(p.home, '.bashrc')), false);
});

test('persistent router forwards subcommands and survives without npm wrapper files', async t => {
  const p = sandbox(t);
  await install({ ...p, download: (await fixture(p.home)).download });
  exposeCommand(p);
  const router = path.join(p.bin, 'router');
  const body = fs.readFileSync(router, 'utf8');
  assert.equal(body.includes('_npx'), false);
  for (const args of [[], ['status'], ['refresh', '--no-browser'], ['ui'], ['strategies', 'list', '$literal', '; untouched', 'one two']]) {
    const result = spawnSync(router, args, { encoding: 'utf8', env: p.env });
    assert.equal(result.status, 0, result.stderr);
    assert.deepEqual(JSON.parse(result.stdout), ['router', ...(args.length ? args : ['ui'])]);
  }
  assert.equal(spawnSync(router, ['--version'], { env: p.env }).status, 0);
});

test('existing ramp from another installer is preserved while router uses it', async t => {
  const p = sandbox(t);
  await fixture(p.home);
  const native = path.join(p.home, 'fixture', 'main.dist', p.name);
  fs.mkdirSync(p.bin);
  fs.symlinkSync(native, path.join(p.bin, 'ramp'));
  exposeCommand({ ...p, nativeExecutable: path.join(p.bin, 'ramp') });
  assert.equal(fs.readlinkSync(path.join(p.bin, 'ramp')), native);
  const result = spawnSync(path.join(p.bin, 'router'), ['status'], { encoding: 'utf8', env: p.env });
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(JSON.parse(result.stdout), ['router', 'status']);
  assert.equal(fs.existsSync(p.root), false);
});

test('Windows preserves an existing native ramp.exe and gives router the same target', t => {
  const p = sandbox(t);
  fs.mkdirSync(p.bin);
  const executable = path.join(p.bin, 'ramp.exe');
  fs.writeFileSync(executable, 'existing native');
  exposeCommand({ ...p, platform: 'win32', arch: 'x64', nativeExecutable: executable });
  assert.equal(fs.readFileSync(executable, 'utf8'), 'existing native');
  assert.equal(fs.existsSync(path.join(p.bin, 'ramp.cmd')), false);
  assert.ok(fs.readFileSync(path.join(p.bin, 'router.cmd'), 'utf8').includes(executable));
});

test('unrelated router command is preserved and an owned launcher can be retargeted', t => {
  const p = sandbox(t);
  fs.mkdirSync(p.bin);
  const router = path.join(p.bin, 'router');
  fs.writeFileSync(router, 'unrelated router');
  assert.throws(() => exposeCommand(p), /existing.*router.*preserved/);
  assert.equal(fs.readFileSync(router, 'utf8'), 'unrelated router');
  fs.unlinkSync(router);
  exposeCommand(p);
  fs.unlinkSync(path.join(p.bin, 'ramp'));
  const target = path.join(p.home, "other 'quoted' native");
  exposeCommand({ ...p, nativeExecutable: target });
  assert.match(fs.readFileSync(router, 'utf8'), /other/);
  assert.equal(fs.readlinkSync(path.join(p.bin, 'ramp')), target);
});

test('a ramp link straight to the managed executable is moved behind the guarded entrypoint', t => {
  const p = sandbox(t);
  fs.mkdirSync(p.bin, { recursive: true });
  fs.symlinkSync(p.executable, path.join(p.bin, 'ramp'));
  exposeCommand(p);
  assert.equal(fs.readlinkSync(path.join(p.bin, 'ramp')), p.entry);
});

test('shortcuts to a CLI from another installer keep its own updater', t => {
  const p = sandbox(t);
  const other = path.join(p.home, 'elsewhere', 'ramp');
  exposeCommand({ ...p, command: 'router', nativeExecutable: other });
  assert.equal(fs.readlinkSync(path.join(p.bin, 'ramp')), other);
  assert.ok(fs.readFileSync(path.join(p.bin, 'router'), 'utf8').includes(`exec '${other}' router`));
});

test('Windows managed shortcuts guard updates and turn off update notices', t => {
  const p = sandbox(t);
  const w = { ...p, command: 'router', platform: 'win32', arch: 'x64' };
  exposeCommand(w);
  const ramp = fs.readFileSync(path.join(p.bin, 'ramp.cmd'), 'utf8');
  assert.match(ramp, /^@echo off\r\nrem @ramp\/cli npm entrypoint/);
  assert.match(ramp, /set RAMP_NO_UPDATE_CHECK=1/);
  assert.match(ramp, /echo   npm update -g @ramp\/router/);
  assert.match(ramp, /if "%~1"=="--" \(shift & goto command\)/);
  assert.match(ramp, /if "%arg:~0,2%"=="-o" \(shift & goto update\)/);
  // The q-stripping line must sit after :short so `goto short` repeats it.
  assert.ok(ramp.indexOf(':short\r\nif "%arg:~0,2%"=="-q"') > 0);
  assert.match(fs.readFileSync(path.join(p.bin, 'router.cmd'), 'utf8'), /set RAMP_NO_UPDATE_CHECK=1/);
  exposeCommand({ ...w, command: 'ramp' }); // An owned entrypoint is rewritable.
  fs.writeFileSync(path.join(p.bin, 'ramp.cmd'), '@echo off\r\nunrelated\r\n');
  assert.throws(() => exposeCommand(w), /preserved/);
});
