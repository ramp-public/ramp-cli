'use strict';

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const { execFile } = require('node:child_process');
const { promisify } = require('node:util');
const run = promisify(execFile);
const tar = require('tar');
const lockfile = require('proper-lockfile');
const { download, latestVersion } = require('./download');
const { posixEntrypoint, windowsEntrypoint, WINDOWS_MARKER } = require('./update-guard');
const { installationContext } = require('./npm-context');

function platformKey(platform = process.platform, arch = process.arch) {
  const system = { darwin: 'darwin', linux: 'linux', win32: 'windows' }[platform];
  const cpu = { x64: 'amd64', arm64: 'arm64' }[arch];
  if (!system || !cpu || (platform === 'win32' && arch !== 'x64')) {
    throw new Error(`Unsupported platform ${platform}/${arch}. Supported: macOS/Linux x64 and arm64, Windows x64.`);
  }
  return `${system}-${cpu}`;
}

function paths({ home = os.homedir(), env = process.env, platform = process.platform, arch = process.arch } = {}) {
  const key = platformKey(platform, arch);
  const base = platform === 'win32' ? path.join(env.LOCALAPPDATA || path.join(home, 'AppData', 'Local'), 'Ramp', 'CLI', 'npm') : path.join(home, '.local', 'lib', 'ramp-cli-npm');
  const root = path.resolve(env.RAMP_CLI_INSTALL_DIR || base);
  // One distribution per platform key: Rosetta and native Node on Apple Silicon
  // must not replace each other's runtime or strand shortcuts that target it.
  const dist = path.join(root, key);
  const name = `ramp-${key}${platform === 'win32' ? '.exe' : ''}`;
  // A stable `ramp` entrypoint script beside the executable. It guards updates
  // (see update-guard.js), and the native CLI persists this path in agent hooks
  // instead of a temporary npx shim (POSIX only).
  const entry = platform === 'win32' ? null : path.join(dist, 'ramp');
  return { root, dist, key, name, executable: path.join(dist, name), entry, state: path.join(dist, 'install.json'), bin: path.resolve(env.RAMP_CLI_BIN_DIR || (platform === 'win32' ? path.join(base, 'bin') : path.join(home, '.local', 'bin'))) };
}

function readState(p) {
  try {
    const state = JSON.parse(fs.readFileSync(p.state, 'utf8'));
    if (state.owner === '@ramp/cli' && /^v\d+\.\d+\.\d+(?:-[\w.]+)?$/.test(state.version)) return state;
  } catch { /* An absent or incomplete distribution needs installation. */ }
  return null;
}

function writeEntrypoint(p, packageName, directory = p.dist) {
  fs.copyFileSync(path.join(__dirname, 'npm-update.js'), path.join(directory, 'npm-update.js'));
  if (!p.entry) return;
  const file = path.join(directory, path.basename(p.entry));
  const body = posixEntrypoint(p.executable, packageName);
  try { if (!fs.lstatSync(file).isSymbolicLink() && fs.readFileSync(file, 'utf8') === body) return; }
  catch { /* Absent: write it. */ }
  const next = `${file}.${process.pid}.tmp`;
  fs.writeFileSync(next, body, { mode: 0o755 });
  fs.renameSync(next, file);
}

// The Windows `ramp.cmd` shortcut bakes the receipt's package into its update
// guidance. When the receipt moves between @ramp/cli and @ramp/router, rewrite
// a shortcut this package created so it keeps naming the package to update.
function refreshWindowsShortcut(p, packageName, options) {
  if ((options.platform || process.platform) !== 'win32') return;
  const command = path.join(p.bin, 'ramp.cmd');
  try {
    const current = fs.readFileSync(command, 'utf8');
    if (!current.startsWith(WINDOWS_MARKER) || !current.includes(`"${p.executable.replace(/%/g, '%%')}"`)) return;
    const body = windowsEntrypoint(p.executable, packageName);
    if (current === body) return;
    const next = `${command}.${process.pid}.tmp`;
    fs.writeFileSync(next, body);
    fs.renameSync(next, command);
  } catch { /* No shortcut of ours to refresh; the next exposeCommand writes it. */ }
}

function writeState(p, state) {
  const next = `${p.state}.${process.pid}.tmp`;
  fs.writeFileSync(next, JSON.stringify(state, null, 2) + '\n');
  fs.renameSync(next, p.state);
}

async function digest(file) {
  const hash = crypto.createHash('sha256');
  for await (const chunk of fs.createReadStream(file)) hash.update(chunk);
  return hash.digest('hex');
}

async function extract(archive, directory) {
  // Nuitka's complete standalone tree lives in main.dist/. Do not accept entries
  // or link targets outside it, including links that could redirect extraction.
  let unsafe;
  await tar.t({ file: archive, strict: true, onReadEntry(entry) {
    const name = entry.path.replace(/\/$/, '');
    if (name !== 'main.dist' && (!name.startsWith('main.dist/') || name.includes('\\') || name.split('/').includes('..'))) unsafe = `Unsafe release archive entry: ${entry.path}`;
    if (entry.type === 'SymbolicLink' || entry.type === 'Link') {
      const target = entry.linkpath;
      const resolved = path.posix.normalize(entry.type === 'Link' ? target : path.posix.join(path.posix.dirname(name), target));
      if (target.includes('\\') || path.posix.isAbsolute(target) || path.win32.isAbsolute(target) || !resolved.startsWith('main.dist/')) unsafe = `Unsafe release archive link: ${name}`;
    } else if (!['File', 'Directory'].includes(entry.type)) {
      unsafe = `Unsupported release archive entry: ${entry.type}`;
    }
  } });
  if (unsafe) throw new Error(unsafe);
  await tar.x({ file: archive, cwd: directory, strip: 1, strict: true });
}

function versionParts(version) { return version.replace(/^v/, '').split('-')[0].split('.').map(Number); }

// True when `version` is strictly newer than `other` (prereleases rank below the release).
function isNewer(version, other) {
  const a = versionParts(version), b = versionParts(other);
  for (let i = 0; i < 3; i++) if (a[i] !== b[i]) return a[i] > b[i];
  return !version.includes('-') && other.includes('-');
}

// A global install that reuses another CLI still owns the shared receipt, so
// `ramp update` must name this package rather than a previous owner.
function syncReceipt(options = {}) {
  const p = paths(options);
  const state = readState(p);
  const packageName = options.package || (options.command === 'router' ? '@ramp/router' : '@ramp/cli');
  if (!state || !options.globalInstall || !fs.existsSync(p.executable)) return;
  const updateContext = installationContext(options);
  writeState(p, { ...state, package: packageName, ...(updateContext.kind === 'global' ? { updateContext } : {}) });
  writeEntrypoint(p, packageName);
  refreshWindowsShortcut(p, packageName, options);
}

async function refreshRouter(p, options) {
  const record = readState(p);
  if (!record?.refreshPending) return;
  try {
    const env = { ...(options.env || process.env), RAMP_NO_UPDATE_CHECK: '1', RAMP_CLI_FORCE_UPDATE: '', RAMP_VERSION: '' };
    if (p.entry) env.PATH = `${p.dist}${path.delimiter}${env.PATH || ''}`;
    const { stdout, stderr } = await (options.refreshRun || run)(p.executable, ['--no-input', 'router', 'refresh'], { env, timeout: 120_000, maxBuffer: 2_000_000, windowsHide: true });
    if (stdout) process.stdout.write(stdout);
    if (stderr) process.stderr.write(stderr);
    writeState(p, { ...record, refreshPending: false });
  } catch (error) {
    console.error(`Ramp CLI updated, but Router refresh failed: ${error.stderr || error.message}\nRetry \`ramp router refresh\`. No skills were updated.`);
  }
}

async function install(options = {}) {
  const env = options.env || process.env;
  const p = paths(options);
  const requested = env.RAMP_CLI_VERSION || `v${require('../package.json').nativeCliVersion}`;
  const force = env.RAMP_CLI_FORCE_UPDATE === '1';
  if (requested !== 'latest' && !/^v\d+\.\d+\.\d+(?:-[\w.]+)?$/.test(requested)) throw new Error('RAMP_CLI_VERSION must be a release tag such as v0.2.55.');
  fs.mkdirSync(p.root, { recursive: true });
  const owner = path.join(p.root, '.ramp-cli-npm');
  if (!fs.existsSync(owner)) {
    // Windows shortcuts live in <root>\bin by default; npx may create them
    // before any managed install exists, so they never make the root unmanaged.
    const shortcuts = path.dirname(p.bin) === p.root ? path.basename(p.bin) : null;
    if (fs.readdirSync(p.root).some(name => name !== '.ramp-cli-npm' && name !== shortcuts)) throw new Error(`Refusing to overwrite an unmanaged installation at ${p.root}. Choose another RAMP_CLI_INSTALL_DIR.`);
    try { fs.writeFileSync(owner, '@ramp/cli\n', { flag: 'wx' }); }
    catch (error) { if (error.code !== 'EEXIST') throw error; }
  }
  if (fs.readFileSync(owner, 'utf8') !== '@ramp/cli\n') throw new Error(`Unrecognized installation owner at ${p.root}.`);
  const unlock = await lockfile.lock(p.root, {
    lockfilePath: path.join(p.root, '.install-lock'),
    stale: 10_000, update: 2_000,
    retries: { retries: 40, minTimeout: 250, maxTimeout: 1_000 },
  }).catch(error => { throw new Error(`Could not acquire the Ramp installation lock at ${p.root}: ${error.message}`); });
  let stage;
  try {
    const backup = path.join(p.root, `previous-${p.key}`);
    if (fs.existsSync(backup)) {
      if (!fs.existsSync(p.dist)) fs.renameSync(backup, p.dist);
      else fs.rmSync(backup, { recursive: true });
    }
    for (const name of fs.readdirSync(p.root)) {
      if (name.startsWith('.stage-')) fs.rmSync(path.join(p.root, name), { recursive: true, force: true });
    }
    const existing = readState(p);
    // The receipt names the package `npm update -g` must move. npx and global
    // installs share this distribution, so only a global install lifecycle
    // decides it (see postinstall's globalPackage); other runs keep it.
    const requestedPackage = options.package || (options.command === 'router' ? '@ramp/router' : '@ramp/cli');
    const context = installationContext(options);
    const updateContext = options.globalInstall && context.kind === 'global' ? context : existing?.updateContext || context;
    // Router's npx lifecycle may run before or after its CLI dependency. It
    // owns that npx update either way, without taking over a global receipt.
    const routerOwner = updateContext.kind === 'npx' && requestedPackage === '@ramp/router';
    const packageName = options.globalInstall || routerOwner ? requestedPackage : existing?.package || requestedPackage;
    if (existing && existing.platform === p.key && existing.executable === p.name && fs.existsSync(p.executable) && !force && (requested === 'latest' || requested === existing.version || (!env.RAMP_CLI_VERSION && isNewer(existing.version, requested)))) {
      // A newer runtime installed by another co-installed package stays put;
      // only an explicit pin or force-update may roll the shared runtime back.
      writeState(p, { ...existing, package: packageName, updateContext });
      writeEntrypoint(p, packageName);
      refreshWindowsShortcut(p, packageName, options);
      await refreshRouter(p, options);
      return p.executable;
    }
    if (fs.existsSync(p.dist) && !existing) throw new Error(`Refusing to replace an unrecognized distribution at ${p.dist}.`);
    const version = requested === 'latest' ? await (options.latestVersion || latestVersion)() : requested;
    if (!/^v\d+\.\d+\.\d+(?:-[\w.]+)?$/.test(version)) throw new Error('The public release did not contain a valid release tag.');
    stage = fs.mkdtempSync(path.join(p.root, '.stage-'));
    const archive = path.join(stage, 'release.tar.gz');
    const checksum = path.join(stage, 'release.sha256');
    const asset = `ramp-${p.key}.tar.gz`;
    const base = `https://github.com/ramp-public/ramp-cli/releases/download/${version}/${asset}`;
    const fetchFile = options.download || download;
    await fetchFile(`${base}.sha256`, checksum);
    await fetchFile(base, archive);
    const expected = fs.readFileSync(checksum, 'utf8').trim().split(/\s+/)[0].toLowerCase();
    if (!/^[a-f0-9]{64}$/.test(expected) || await digest(archive) !== expected) throw new Error('Ramp CLI archive SHA-256 verification failed; the installed CLI was preserved.');
    const next = path.join(stage, 'dist');
    fs.mkdirSync(next);
    await extract(archive, next);
    const executable = path.join(next, p.name);
    if (!fs.existsSync(executable) || !fs.statSync(executable).isFile()) throw new Error(`Release archive did not contain ${p.name}.`);
    if ((options.platform || process.platform) !== 'win32') {
      fs.chmodSync(executable, 0o755);
      writeEntrypoint(p, packageName, next);
    }
    let stdout;
    try {
      ({ stdout } = await run(executable, ['--version'], { timeout: 15_000, env: { ...env, RAMP_NO_UPDATE_CHECK: '1', RAMP_VERSION: '' } }));
    } catch (error) {
      throw new Error(`Downloaded Ramp CLI could not run --version: ${error.stderr || error.message}`);
    }
    if (stdout.match(/\b\d+\.\d+\.\d+(?:-[\w.]+)?\b/)?.[0] !== version.slice(1)) throw new Error(`Downloaded Ramp CLI reported an unexpected version: ${stdout.trim()}`);
    if (!p.entry) writeEntrypoint(p, packageName, next);
    fs.writeFileSync(path.join(next, 'install.json'), JSON.stringify({ owner: '@ramp/cli', package: packageName, version, platform: p.key, executable: p.name, sha256: expected, updateContext, refreshPending: Boolean(existing && (existing.version !== version || existing.refreshPending)) }, null, 2) + '\n');
    if (existing) fs.renameSync(p.dist, backup);
    try { fs.renameSync(next, p.dist); }
    catch (error) { if (existing) fs.renameSync(backup, p.dist); throw error; }
    fs.rmSync(backup, { recursive: true, force: true });
    refreshWindowsShortcut(p, packageName, options);
    await refreshRouter(p, options);
    return p.executable;
  } finally {
    try { if (stage) fs.rmSync(stage, { recursive: true, force: true }); }
    finally { await unlock(); }
  }
}

module.exports = { install, syncReceipt, paths, platformKey, readState };
