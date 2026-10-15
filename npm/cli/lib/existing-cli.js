'use strict';

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { execFile } = require('node:child_process');
const { promisify } = require('node:util');
const lockfile = require('proper-lockfile');
const { install, paths, readState, syncReceipt } = require('./installer');
const { confirm } = require('./terminal');

const run = promisify(execFile);

function isOlder(version, minimum) {
  const a = version.split('-')[0].split('.').map(Number);
  const b = minimum.split('-')[0].split('.').map(Number);
  for (let i = 0; i < 3; i++) if (a[i] !== b[i]) return a[i] < b[i];
  return version.includes('-') && !minimum.includes('-');
}

function wrapperFile(file, managedRoot) {
  const resolved = fs.realpathSync(file);
  // `file` is canonical, so compare it with the canonical managed root: a
  // symlinked install directory or parent (a symlinked home) must still match.
  try { managedRoot = fs.realpathSync(managedRoot); } catch { /* Not installed yet. */ }
  if (resolved === managedRoot || resolved.startsWith(`${managedRoot}${path.sep}`)) return true;
  // npm's Unix symlinks resolve into bin/. Ignore every installed/cached copy
  // of this package, not just the current one, to avoid launcher recursion.
  try {
    if (['@ramp/cli', '@ramp/router'].includes(JSON.parse(fs.readFileSync(path.join(path.dirname(resolved), '..', 'package.json'), 'utf8')).name)) return true;
  } catch { /* Not an npm package shim. */ }
  return false;
}

async function probe(file, options) {
  const env = { ...(options.env || process.env), RAMP_NO_UPDATE_CHECK: '1', RAMP_VERSION: '' };
  const { stdout } = await (options.run || run)(file, ['--version'], { env, timeout: 10_000, windowsHide: true });
  const version = stdout.match(/\bramp(?:-cli)?,?\s+(?:version\s+)?v?(\d+\.\d+\.\d+(?:-[\w.]+)?)/i)?.[1];
  if (!version) throw new Error('Not a Ramp CLI executable.');
  return { executable: file, version };
}

async function findExisting(options = {}) {
  const env = options.env || process.env;
  const home = options.home || os.homedir();
  const p = paths(options);
  const windows = (options.platform || process.platform) === 'win32';
  // Only native .exe candidates on Windows: npm .cmd launchers must not be
  // executed through a shell or mistaken for an existing native installation.
  const name = windows ? 'ramp.exe' : 'ramp';
  const candidates = (env.PATH || '').split(windows ? ';' : ':').filter(dir => path.isAbsolute(dir)).map(dir => path.join(dir, name));
  candidates.push(path.join(home, '.local', 'bin', name), path.join(home, '.local', 'lib', 'ramp-cli', p.name));
  const seen = new Set();
  for (const file of candidates) {
    try {
      const real = fs.realpathSync(file);
      if (seen.has(real) || wrapperFile(file, p.root)) continue;
      seen.add(real);
      fs.accessSync(file, windows ? fs.constants.F_OK : fs.constants.X_OK);
      return await probe(file, options);
    } catch { /* Try the next executable, including one hidden by npm's shim. */ }
  }
  return null;
}

async function resolveCLI(options = {}) {
  const env = options.env || process.env;
  const managedCLI = () => {
    const p = paths(options);
    // Validate an update against the current native parser before upgrading
    // the runtime or retrying a pending Router refresh.
    return options.forUpdate && readState(p) && fs.existsSync(p.executable) ? p.executable : install(options);
  };
  // Explicit pins/force-update select the verified npm-managed distribution.
  if (env.RAMP_CLI_VERSION || env.RAMP_CLI_FORCE_UPDATE === '1' || env.RAMP_NPM_UPDATE === '1') return managedCLI();
  let found = await findExisting(options);
  if (!found) return managedCLI();
  const minimum = require('../package.json').nativeCliVersion;
  const session = options.session;
  if (isOlder(found.version, minimum) && session) {
    session.write(`Found Ramp CLI ${found.version} at ${found.executable} (this launcher targets ${minimum}).`);
    if (await (options.confirm || confirm)(session, 'Update the existing Ramp CLI before opening Router?')) {
      const root = paths(options).root;
      fs.mkdirSync(root, { recursive: true });
      const unlock = await lockfile.lock(root, { lockfilePath: path.join(root, '.existing-update-lock'), stale: 10_000, retries: { retries: 40, minTimeout: 250, maxTimeout: 1_000 } });
      try {
        found = await probe(found.executable, options);
        if (isOlder(found.version, minimum)) {
          const result = await options.update(found.executable, ['update'], session.stdio);
          if (result.code !== 0 || result.signal) session.write('Ramp update did not complete. Checking whether the existing CLI can still open Router.');
          found = await probe(found.executable, options);
          if (isOlder(found.version, minimum)) session.write(`Ramp remains at ${found.version}; follow the updater's instructions for your installation method.`);
        }
      } finally { await unlock(); }
    }
  }
  // An older release can still be used if it has the Router terminal UI.
  // Never silently overwrite it or create a second installation just for age.
  if (options.command === 'router') {
    try {
      await (options.run || run)(found.executable, ['router', 'ui', '--help'], { env: { ...env, RAMP_NO_UPDATE_CHECK: '1', RAMP_VERSION: '' }, timeout: 10_000, windowsHide: true });
    } catch {
      throw new Error(`Ramp CLI at ${found.executable} does not support the Router UI. Run \`ramp update\` or explicitly select an npm-managed release with RAMP_CLI_VERSION.`);
    }
  }
  syncReceipt(options);
  if (session) session.write(`Using Ramp CLI ${found.version} at ${found.executable}.`);
  return found.executable;
}

module.exports = { resolveCLI, findExisting, isOlder };
