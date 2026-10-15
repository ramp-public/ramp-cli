'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const tar = require('tar');
const { root, PLACEHOLDER, read, validate, releaseVersion, manifests, pack } = require('../scripts/packages');

test('packages have disjoint commands, a public source, and an exact shared installer dependency', () => {
  const { cli, router } = validate();
  assert.deepEqual(cli.bin, { ramp: 'bin/ramp.js' });
  assert.deepEqual(router.bin, { router: 'bin/router.js' });
  assert.equal(router.dependencies['@ramp/cli'], cli.version);
  assert.equal(cli.license, 'MIT');
  assert.equal(router.license, 'MIT');
});

test('npm versions come only from the Python version; the checked-in manifests are never bumped', () => {
  const python = fs.readFileSync(path.join(root, '..', 'src', 'ramp_cli', '__init__.py'), 'utf8').match(/__version__ = "([^"]+)"/)[1];
  assert.equal(releaseVersion(), python);
  const hint = 'npm versions are stamped from the Python version when packing; keep the checked-in placeholder.';
  assert.equal(read('cli').version, PLACEHOLDER, hint);
  assert.equal(read('cli').nativeCliVersion, PLACEHOLDER, hint);
  assert.equal(read('router').version, PLACEHOLDER, hint);
  assert.equal(read('router').dependencies['@ramp/cli'], PLACEHOLDER, hint);
  const lock = JSON.parse(fs.readFileSync(path.join(root, 'package-lock.json'), 'utf8'));
  assert.equal(lock.packages.cli.version, PLACEHOLDER, hint);
  assert.equal(lock.packages.router.version, PLACEHOLDER, hint);
  const { cli, router } = manifests();
  assert.deepEqual([cli.version, cli.nativeCliVersion, router.version, router.dependencies['@ramp/cli']], [python, python, python, python]);
  assert.throws(() => validate({ ...cli, nativeCliVersion: '0.0.1' }, router), /same version/);
});

test('packing stamps the release version into both published manifests', t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'ramp-npm-version-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const artifacts = pack(directory, '9.8.7');
  assert.deepEqual(artifacts.map(artifact => `${artifact.name}@${artifact.version}`), ['@ramp/cli@9.8.7', '@ramp/router@9.8.7']);
  const packed = artifacts.map(artifact => {
    const out = path.join(directory, artifact.name.split('/')[1]);
    fs.mkdirSync(out);
    tar.x({ file: artifact.archive, cwd: out, sync: true, strip: 1 });
    return JSON.parse(fs.readFileSync(path.join(out, 'package.json'), 'utf8'));
  });
  assert.equal(packed[0].nativeCliVersion, '9.8.7');
  assert.equal(packed[1].dependencies['@ramp/cli'], '9.8.7');
  assert.equal(read('cli').version, PLACEHOLDER, 'packing must not edit the checked-in manifest');
  assert.throws(() => releaseVersion(path.join(directory, 'missing', 'npm')), /ENOENT/);
});

test('the native release each npm version installs hides update notices for npm installs', () => {
  // Lockstep versions mean npm installs the native release built from this
  // same source. RAMP_NO_UPDATE_CHECK alone does not hide notices from the
  // shared version cache, so that source must check the npm install receipt.
  const source = path.join(root, '..', 'src', 'ramp_cli');
  assert.match(fs.readFileSync(path.join(source, 'install_record.py'), 'utf8'), /def npm_install_record\(/);
  const versionCheck = fs.readFileSync(path.join(source, 'version_check.py'), 'utf8');
  const getUpdateInfo = versionCheck.slice(versionCheck.indexOf('def get_update_info('));
  assert.match(getUpdateInfo.slice(0, getUpdateInfo.indexOf('_read_cache()')), /if npm_install_record\(\) is not None:\s+return None/);
});

test('release checks reject drifting versions, private publication, and overlapping commands', () => {
  assert.throws(() => validate(read('cli'), { ...read('router'), version: '0.0.1' }), /matching versions/);
  assert.throws(() => validate(read('cli'), { ...read('router'), dependencies: { '@ramp/cli': `^${read('cli').version}` } }), /matching versions/);
  assert.throws(() => validate({ ...read('cli'), private: true }, read('router')), /publicly/);
  assert.throws(() => validate(read('cli'), { ...read('router'), bin: { ramp: 'bin/router.js' } }), /disjoint/);
});
