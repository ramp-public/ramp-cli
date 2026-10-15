'use strict';

// Copied beside the native executable: persistent shortcuts must not depend on
// a disposable npx cache. Keep this helper dependency-free.
const fs = require('node:fs');
const path = require('node:path');
const { spawn, execFile } = require('node:child_process');
const { promisify } = require('node:util');
const probe = promisify(execFile);

const registry = 'https://registry.npmjs.org/';
const packages = ['@ramp/cli', '@ramp/router'];

function guidance(packageName) {
  return [
    `This ramp was installed by npm (${packageName}). Update it with npm:`,
    `  npm update -g ${packageName}`,
    `  npx ${packageName}@latest`,
  ];
}

function npmCommand(args, env = process.env, platform = process.platform) {
  if (platform !== 'win32') return ['npm', ...args];
  // Never interpolate arguments into cmd.exe. npm's own JS entrypoint also
  // supports prefixes containing spaces and shell metacharacters.
  const candidates = [env.npm_execpath, path.join(path.dirname(process.execPath), 'node_modules', 'npm', 'bin', 'npm-cli.js')];
  const entry = candidates.find(file => file && path.basename(file) === 'npm-cli.js' && fs.existsSync(file));
  if (!entry) throw new Error('Cannot locate npm-cli.js. Run the npm update command from your terminal.');
  return [process.execPath, entry, ...args];
}

async function run(command, env, label = 'npm update') {
  const child = spawn(command[0], command.slice(1), { stdio: 'inherit', env, shell: false });
  const handlers = new Map(['SIGINT', 'SIGTERM', 'SIGHUP'].map(signal => [signal, () => child.kill(signal)]));
  for (const [signal, handler] of handlers) process.on(signal, handler);
  try {
    await new Promise((resolve, reject) => {
      child.once('error', reject);
      child.once('exit', (code, signal) => code === 0 && !signal ? resolve() : reject(Object.assign(new Error(`${label} failed (${signal || code}).`), { exitCode: code || 1 })));
    });
  } finally {
    for (const [signal, handler] of handlers) process.removeListener(signal, handler);
  }
}

function legacyHelpArguments(args) {
  // Old `update` takes no command-specific options or positional arguments.
  // Check only that shape here; its real root parser still validates values,
  // environments, profiles and conflicting flags via `update --help` below.
  const values = ['--env', '-e', '--output', '-o', '--profile'];
  const flags = ['--quiet', '-q', '--no-input', '--wide', '--agent', '--human'];
  const root = [], rest = [];
  const invalid = () => { throw Object.assign(new Error('Invalid update arguments. This native CLI accepts `update` with root options only.'), { exitCode: 2 }); };
  // Match Ramp's exact root-flag hoisting, including options after `update`.
  for (let i = 0; i < args.length; i++) {
    const arg = args[i];
    if (values.includes(arg)) {
      if (i + 1 === args.length) invalid();
      root.push(arg, args[++i]);
    } else if (flags.includes(arg)) root.push(arg);
    else rest.push(arg);
  }
  let i = 0;
  for (; i < rest.length; i++) {
    const arg = rest[i];
    if (arg === '--') { i++; break; }
    if (/^--(env|output|profile)=/.test(arg)) continue;
    if (/^-[^-]/.test(arg)) {
      let needsValue = false;
      for (let j = 1; j < arg.length; j++) {
        if (arg[j] === 'q') continue;
        if ('oe'.includes(arg[j])) { needsValue = j === arg.length - 1; break; }
        invalid();
      }
      if (needsValue && ++i === rest.length) invalid();
      continue;
    }
    break;
  }
  if (rest[i] !== 'update') invalid();
  const tail = rest.slice(i + 1);
  if (tail.length && !(tail.length === 1 && tail[0] === '--')) invalid();
  return [...root, ...rest.slice(0, i), 'update', '--help'];
}

async function validateArguments(directory, record, args, env) {
  // Detect the installed parser's capability rather than assuming a release
  // floor: explicit pins, prereleases and existing receipts may all be older.
  if (!/^ramp-(darwin|linux|windows)-(amd64|arm64)(\.exe)?$/.test(record.executable || '')) throw new Error('The npm receipt does not identify a native executable.');
  const executable = path.join(directory, record.executable);
  const validationEnv = { ...env, RAMP_NO_UPDATE_CHECK: '1', RAMP_VERSION: '' };
  let supportsValidation = true;
  try {
    await probe(executable, ['update', '--_npm-validate'], { env: validationEnv, timeout: 15_000, windowsHide: true });
  } catch (error) {
    // Only this exact unsupported-option response enables the legacy path.
    // Outages, timeouts, bad config and other parser failures never bypass it.
    const output = `${error.stdout || ''}\n${error.stderr || ''}`;
    if (error.code === 2 && ['No such option: --_npm-validate', "No such option '--_npm-validate'."].some(message => output.includes(message))) supportsValidation = false;
    else throw Object.assign(error, { exitCode: typeof error.code === 'number' ? error.code : 1 });
  }
  if (supportsValidation) await run([executable, ...args, '--_npm-validate'], validationEnv, 'Update argument validation');
  else {
    // Help exits before the old updater, but still runs root validation. Capture
    // successful help so updating an old pin does not dump help into the output.
    try {
      await probe(executable, legacyHelpArguments(args), { env: validationEnv, timeout: 15_000, windowsHide: true });
    } catch (error) {
      throw Object.assign(error, { exitCode: error.exitCode || (typeof error.code === 'number' ? error.code : 1) });
    }
  }
}

function readRecord(directory) {
  const record = JSON.parse(fs.readFileSync(path.join(directory, 'install.json'), 'utf8'));
  if (record.owner !== '@ramp/cli' || !packages.includes(record.package)) throw new Error('Unrecognized npm installation receipt.');
  return record;
}

async function update({ directory = __dirname, context, args: argv = ['update'], env = process.env, execute = run, validate = validateArguments, write = console.log, platform = process.platform } = {}) {
  const record = readRecord(directory);
  await validate(directory, record, argv, env);
  if (record.channel && record.channel !== 'public') throw new Error('This updater only supports public npm installations. Use your channel-specific updater.');
  // A project command must not mutate a global install merely because both
  // launchers share the same native cache.
  const selected = context?.kind === 'project' ? context : record.updateContext?.kind === 'unknown' ? context : record.updateContext || context;
  if (!selected || !['global', 'npx'].includes(selected.kind)) {
    write(selected?.kind === 'project' ? 'This launcher is a project dependency. Update it through your project package manager and lockfile.' : guidance(record.package).join('\n'));
    return;
  }
  let args;
  if (selected.kind === 'global') {
    if (!path.isAbsolute(selected.prefix || '') || !path.isAbsolute(selected.modules || '')) throw new Error('The npm receipt does not identify a global prefix.');
    const expectedModules = platform === 'win32' ? path.join(selected.prefix, 'node_modules') : path.join(selected.prefix, 'lib', 'node_modules');
    if (path.resolve(selected.modules) !== path.resolve(expectedModules)) throw new Error('The npm receipt has an inconsistent global prefix.');
    const manifest = JSON.parse(fs.readFileSync(path.join(selected.modules, ...record.package.split('/'), 'package.json'), 'utf8'));
    if (manifest.name !== record.package) throw new Error('The owning global npm package is no longer installed.');
    args = ['update', '--global', '--prefix', selected.prefix, record.package];
  } else {
    // Execute an informational command, not bare Router onboarding. Its normal
    // lifecycle/launcher installs the native version pinned by the latest npm package.
    args = ['exec', '--yes', `--package=${record.package}@latest`, '--', record.package === '@ramp/router' ? 'router' : 'ramp', '--version'];
  }
  const nextEnv = { ...env, RAMP_NPM_UPDATE: '1', RAMP_ROUTER_SKIP_ONBOARDING: '1', RAMP_CLI_INSTALL_DIR: path.dirname(directory), RAMP_NO_UPDATE_CHECK: '1' };
  delete nextEnv.RAMP_CLI_VERSION;
  delete nextEnv.RAMP_CLI_FORCE_UPDATE;
  // Do not inherit the outer exec's package request or global/prefix settings.
  for (const key of Object.keys(nextEnv)) if (/^npm_config_(package|global|prefix|ignore_scripts|foreground_scripts|registry)$/i.test(key)) delete nextEnv[key];
  const flags = ['--ignore-scripts=false', '--foreground-scripts', `--registry=${registry}`];
  const separator = args.indexOf('--');
  args.splice(separator < 0 ? args.length : separator, 0, ...flags);
  write(`Updating ${record.package} through npm${selected.kind === 'global' ? ` in ${selected.prefix}` : ''}...`);
  await execute(npmCommand(args, nextEnv, platform), nextEnv);
  const updated = readRecord(directory);
  if (updated.refreshPending) throw new Error('CLI updated, but Router refresh failed. Retry `ramp router refresh`.');
  write(`Ramp CLI is installed at ${updated.version}.`);
}

if (require.main === module) update({ args: process.argv.slice(2) }).catch(error => { console.error(`${error.message}\nThe curl installer was not run.`); process.exitCode = error.exitCode || 1; });
module.exports = { update, npmCommand, validateArguments, legacyHelpArguments };
