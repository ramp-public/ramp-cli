'use strict';

const fs = require('node:fs');
const { spawn } = require('node:child_process');
const path = require('node:path');
const { constants } = require('node:os');
const { resolveCLI } = require('./existing-cli');
const { paths, syncReceipt } = require('./installer');
const { isUpdateCommand } = require('./update-guard');
const { installationContext } = require('./npm-context');
const { exposeCommand, refreshPath } = require('./commands');
const { onboardingSession, informational } = require('./onboarding');

// True when this package sits in a global npm install (not npx's cache or a
// project's node_modules). Lifecycle scripts may have been skipped
// (--ignore-scripts), so launch re-asserts the receipt owner itself. A CLI
// nested under @ramp/router counts only when that Router is itself global.
const topLevel = dir => {
  const modules = path.dirname(path.dirname(dir));
  if (path.basename(modules) !== 'node_modules') return null;
  return { modules, owner: path.dirname(modules) };
};
const packageNamed = dir => { try { return JSON.parse(fs.readFileSync(path.join(dir, 'package.json'), 'utf8')).name; } catch { return null; } };
function globalLayout(cliDir = path.join(__dirname, '..'), env = process.env) {
  if (env.npm_command === 'exec' || cliDir.split(path.sep).includes('_npx')) return false;
  const found = topLevel(cliDir);
  if (!found) return false;
  // A global prefix's lib/ (or Windows prefix) is not itself a package.
  const plain = !fs.existsSync(path.join(found.owner, 'package.json'));
  if (plain) return true;
  if (packageNamed(found.owner) !== '@ramp/router') return false;
  const router = topLevel(found.owner);
  return Boolean(router) && !fs.existsSync(path.join(router.owner, 'package.json'));
}

// The stable `ramp` that resolves to the reused native CLI, so hooks never
// record npx's temporary shim. Prefers a real PATH/~/.local/bin entry.
function stableRampDir(executable, env = process.env) {
  const windows = process.platform === 'win32';
  const name = windows ? 'ramp.exe' : 'ramp';
  let target;
  try { target = fs.realpathSync(executable); } catch { return null; }
  const dirs = [...(env.PATH || '').split(path.delimiter), path.join(require('node:os').homedir(), '.local', 'bin'), path.dirname(path.resolve(executable))];
  for (const dir of dirs) {
    if (!dir || !path.isAbsolute(dir) || dir.split(path.sep).includes('_npx')) continue;
    try { if (fs.realpathSync(path.join(dir, name)) === target) return dir; } catch { /* Not this one. */ }
  }
  return null;
}

const managed = executable => path.resolve(executable).startsWith(`${paths().root}${path.sep}`);

async function launch(executable, args, stdio = 'inherit') {
  const env = { ...process.env, PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8' };
  delete env.RAMP_CLI_FORCE_UPDATE;
  delete env.RAMP_VERSION;
  if (managed(executable)) {
    // npm owns updates (see update-guard.js). With the guarded entrypoint first
    // on PATH, a native CLI that looks itself up (e.g. for agent hooks) finds a
    // stable, guarded `ramp` rather than a temporary npx shim.
    env.RAMP_NO_UPDATE_CHECK = '1';
    const { entry } = paths();
    if (entry && fs.existsSync(entry)) env.PATH = `${path.dirname(entry)}${path.delimiter}${env.PATH || ''}`;
  }
  else if (process.env.npm_command === 'exec') {
    // A reused curl CLI must resolve itself to its own stable path, not the
    // temporary npx shim, when it persists a command into agent hooks.
    const dir = stableRampDir(executable, env);
    if (dir) env.PATH = `${dir}${path.delimiter}${env.PATH || ''}`;
  }
  const child = spawn(executable, args, { stdio, env, shell: false });
  const handlers = new Map(['SIGINT', 'SIGTERM', 'SIGHUP'].map(signal => [signal, () => child.kill(signal)]));
  for (const [signal, handler] of handlers) process.on(signal, handler);
  try {
    return await new Promise((resolve, reject) => {
      child.once('error', reject);
      child.once('exit', (code, signal) => resolve({ code, signal }));
    });
  } finally {
    for (const [signal, handler] of handlers) process.removeListener(signal, handler);
  }
}

async function main(mode, args) {
  const workspace = mode === 'router' && args.length === 0;
  const session = workspace ? onboardingSession(args) : null;
  let result;
  try {
    const executable = await resolveCLI({ command: workspace ? 'router' : 'ramp', package: mode === 'router' ? '@ramp/router' : '@ramp/cli', session, update: launch, forUpdate: mode === 'ramp' && args.includes('update') });
    if (globalLayout()) {
      try { syncReceipt({ command: mode, package: require('./postinstall').globalPackage(mode), globalInstall: true }); }
      catch { /* A receipt we cannot refresh must not block launching. */ }
    }
    if (mode === 'ramp' && managed(executable) && isUpdateCommand(args)) {
      // Never let the native curl updater replace an npm-managed CLI.
      await require('./npm-update').update({ directory: paths().dist, context: installationContext(), args });
      process.exitCode = 0;
      return;
    }
    // npm's global shims need no PATH changes. npx gets a persistent command,
    // but shell startup files are only changed after interactive consent.
    if (process.env.npm_command === 'exec' && !informational(args)) {
      try {
        if (session) await refreshPath(session, { command: mode, nativeExecutable: executable });
        else exposeCommand({ command: mode, nativeExecutable: executable });
      } catch (error) { console.error(`${error.message}\nThe CLI installation succeeded.`); }
    }
    const nativeArgs = mode !== 'router' ? args
      : args.some(arg => ['--version', '-V'].includes(arg)) ? ['--version']
        : workspace ? ['router', 'ui'] : ['router', ...args];
    // Explicit `router` opens the native TUI; it reports a usage error if no
    // interactive terminal is available. CI/skip flags affect only the npm
    // install hook and optional wrapper prompts, not an explicit command.
    result = await launch(executable, nativeArgs, session?.stdio || 'inherit');
  } finally { session?.close(); }
  if (workspace && (result.code !== 0 || result.signal)) console.error('Ramp CLI remains installed. Retry `router` from an interactive terminal.');
  process.exitCode = result.code ?? (128 + (constants.signals[result.signal] || 1));
  if (result.signal && process.platform !== 'win32') {
    // The lock library also registers signal listeners. Restore the default
    // disposition so mirroring the child's signal cannot become exit zero.
    process.removeAllListeners(result.signal);
    process.kill(process.pid, result.signal);
  }
}

module.exports = { main, launch, globalLayout, stableRampDir };
