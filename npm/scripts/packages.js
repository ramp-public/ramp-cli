'use strict';

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { execFileSync } = require('node:child_process');

const root = path.resolve(__dirname, '..');
const registry = 'https://registry.npmjs.org/';
const packageNames = ['cli', 'router'];
const read = name => JSON.parse(fs.readFileSync(path.join(root, name, 'package.json'), 'utf8'));
const semver = /^\d+\.\d+\.\d+(?:-[a-zA-Z0-9]+(?:[.-][a-zA-Z0-9]+)*)?$/;
// The checked-in manifests never carry a release version; packing stamps one.
const PLACEHOLDER = '0.0.0';

// The Python CLI version is the only version. npm ships the native release of
// the same version, so packing reads it from the source tree (public/src here,
// src/ in ramp-public) and nobody bumps an npm version by hand.
function releaseVersion(directory = root) {
  const source = fs.readFileSync(path.join(directory, '..', 'src', 'ramp_cli', '__init__.py'), 'utf8');
  const version = source.match(/^__version__ = "([^"]+)"$/m)?.[1];
  if (!/^\d+\.\d+\.\d+$/.test(version || '')) throw new Error(`The Python CLI version must be MAJOR.MINOR.PATCH, not ${version}.`);
  return version;
}

function stamp(manifest, version) {
  const stamped = { ...manifest, version };
  if ('nativeCliVersion' in manifest) stamped.nativeCliVersion = version;
  if (manifest.dependencies?.['@ramp/cli']) stamped.dependencies = { ...manifest.dependencies, '@ramp/cli': version };
  return stamped;
}

// The manifests as published: checked-in metadata stamped with the release version.
function manifests(version = releaseVersion()) {
  return validate(stamp(read('cli'), version), stamp(read('router'), version));
}

function validate(cli = read('cli'), router = read('router')) {
  if (cli.name !== '@ramp/cli' || router.name !== '@ramp/router') throw new Error('Unexpected npm package names.');
  if (!semver.test(cli.version) || !semver.test(router.version) || !semver.test(cli.nativeCliVersion)) throw new Error('Package and native CLI versions must be explicit semver values.');
  if (cli.version !== router.version || router.dependencies?.['@ramp/cli'] !== cli.version) throw new Error('Both packages and the exact @ramp/cli dependency must have matching versions.');
  if (cli.nativeCliVersion !== cli.version) throw new Error('npm packages ship the native CLI release of the same version.');
  for (const manifest of [cli, router]) {
    if (manifest.private || manifest.publishConfig?.access !== 'public' || manifest.publishConfig?.registry !== registry) throw new Error('npm packages must explicitly publish publicly to npmjs.org.');
    if (manifest.repository?.url !== 'git+https://github.com/ramp-public/ramp-cli.git') throw new Error('Repository metadata must match the public trusted publisher.');
  }
  if (JSON.stringify(cli.bin) !== JSON.stringify({ ramp: 'bin/ramp.js' }) || JSON.stringify(router.bin) !== JSON.stringify({ router: 'bin/router.js' })) throw new Error('Packages must own disjoint ramp and router commands.');
  return { cli, router };
}

function npm(args, options = {}) {
  // Windows cannot exec npm.cmd without a shell. Reuse npm's JavaScript
  // entrypoint when invoked through npm run, preserving paths with spaces.
  const entrypoint = process.env.npm_execpath;
  if (process.platform === 'win32' && !entrypoint) throw new Error('Run package tooling through npm run on Windows.');
  const executable = entrypoint ? process.execPath : 'npm';
  const result = execFileSync(executable, entrypoint ? [entrypoint, ...args] : args, { encoding: 'utf8', cwd: root, ...options });
  return result;
}

function pack(destination, version = releaseVersion()) {
  const stamped = manifests(version);
  fs.mkdirSync(destination, { recursive: true });
  const stage = fs.mkdtempSync(path.join(os.tmpdir(), 'ramp-npm-pack-'));
  try {
    return packageNames.map(name => {
      const directory = path.join(stage, name);
      fs.cpSync(path.join(root, name), directory, { recursive: true });
      fs.writeFileSync(path.join(directory, 'package.json'), JSON.stringify(stamped[name], null, 2) + '\n');
      const [result] = JSON.parse(npm(['pack', '--ignore-scripts', '--json', '--pack-destination', path.resolve(destination)], { cwd: directory }));
      for (const file of result.files) {
        if (!/^(package\.json|README\.md|LICENSE|bin\/[^/]+\.js|lib\/[^/]+\.js)$/.test(file.path)) throw new Error(`Unexpected file in ${result.name}: ${file.path}`);
      }
      for (const required of ['package.json', 'README.md', 'LICENSE', ...Object.values(read(name).bin)]) {
        if (!result.files.some(file => file.path === required)) throw new Error(`${result.name} is missing ${required}.`);
      }
      return { ...result, archive: path.join(path.resolve(destination), result.filename) };
    });
  } finally { fs.rmSync(stage, { recursive: true, force: true }); }
}

module.exports = { root, registry, PLACEHOLDER, read, validate, releaseVersion, manifests, npm, pack };
