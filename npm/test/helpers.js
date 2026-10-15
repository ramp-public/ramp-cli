'use strict';

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const tar = require('tar');
const { paths, platformKey } = require('../cli/lib/installer');
const { releaseVersion } = require('../scripts/packages');

// Unpacked manifests carry the 0.0.0 placeholder; packing stamps the Python
// version. Stamp the cached manifest the same way so source-tree tests compare
// against a real release.
require('../cli/package.json').nativeCliVersion = releaseVersion();

function sandbox(t) {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'router home $cash '));
  t.after(() => fs.rmSync(home, { recursive: true, force: true }));
  // Never discover or invoke the developer's real Ramp installation. Only the
  // runtime tools are linked into this PATH; fixtures need env node/npm/npx.
  const runtime = path.join(home, 'runtime-bin');
  fs.mkdirSync(runtime);
  fs.symlinkSync(process.execPath, path.join(runtime, 'node'));
  for (const name of ['npm', 'npx']) {
    const executable = (process.env.PATH || '').split(path.delimiter).map(dir => path.join(dir, name)).find(file => fs.existsSync(file));
    if (executable) fs.symlinkSync(fs.realpathSync(executable), path.join(runtime, name));
  }
  const runtimePath = `${runtime}:/usr/bin:/bin`;
  // An outer npm exec may inject npm_config_package=node@22. Passing it to
  // nested npx changes the requested package, so isolate all npm lifecycle
  // configuration as well as HOME/PATH. Individual tests opt back in explicitly.
  const inherited = Object.fromEntries(Object.entries(process.env).filter(([name]) => !name.toLowerCase().startsWith('npm_') && name !== 'NODE_OPTIONS' && name !== 'RAMP_ROUTER_CONFIGURE_API_KEY'));
  const env = { ...inherited, HOME: home, RAMP_CLI_INSTALL_DIR: path.join(home, 'installation with spaces'), RAMP_CLI_BIN_DIR: path.join(home, 'command bin'), RAMP_CLI_VERSION: 'v0.0.0', RAMP_CLI_FORCE_UPDATE: '', RAMP_ROUTER_VERSION: '', RAMP_ROUTER_FORCE_UPDATE: '', SHELL: '/bin/bash', PATH: runtimePath };
  env.CI = '';
  env.RAMP_ROUTER_SKIP_ONBOARDING = '';
  return { home, env, runtimePath, command: 'router', ...paths({ home, env }) };
}

async function fixture(directory, { version = 'v0.0.0', broken = false } = {}) {
  const source = path.join(directory, 'fixture');
  const dist = path.join(source, 'main.dist');
  fs.mkdirSync(dist, { recursive: true });
  const executable = path.join(dist, `ramp-${platformKey()}`);
  // A stand-in that needs a companion file, to catch executable-only installs.
  const script = `#!/usr/bin/env node
const fs = require('node:fs');
const path = require('node:path');
async function main() {
if (!fs.existsSync(path.join(__dirname, 'supporting-library.txt'))) process.exit(99);
const args = process.argv.slice(2);
// Validation is a separate parser-only invocation, not an update or refresh.
// Python tests exercise Click itself; this fixture records forwarding and lets
// launcher tests simulate its exit status without requiring Python in npm CI.
if (args.includes('--_npm-validate')) {
  if (process.env.ROUTER_TEST_VALIDATION_LOG) fs.appendFileSync(process.env.ROUTER_TEST_VALIDATION_LOG, JSON.stringify(args) + '\\n');
  if (process.env.ROUTER_TEST_LEGACY === '1') { console.log(JSON.stringify({ error: { code: 2, message: "No such option '--_npm-validate'." } })); process.exit(2); }
  if (process.env.ROUTER_TEST_PROBE_EXIT) { console.error('Configuration error'); process.exit(Number(process.env.ROUTER_TEST_PROBE_EXIT)); }
  if (args.length === 2 && args[0] === 'update') process.exit(0);
  process.exit(Number(process.env.ROUTER_TEST_VALIDATE_EXIT || 0));
}
if (args.includes('--help') && args.includes('update') && process.env.ROUTER_TEST_LEGACY === '1') {
  if (process.env.ROUTER_TEST_VALIDATION_LOG) fs.appendFileSync(process.env.ROUTER_TEST_VALIDATION_LOG, JSON.stringify(args) + '\\n');
  process.exit(Number(process.env.ROUTER_TEST_VALIDATE_EXIT || 0));
}
if (process.env.ROUTER_TEST_LOG) fs.appendFileSync(process.env.ROUTER_TEST_LOG, JSON.stringify(args) + '\\n');
if (args[0] === '--version') { console.log('ramp, version ${version.slice(1)}'); process.exit(${broken ? 42 : 0}); }
if (args[0] === 'env') { console.log(JSON.stringify({ noUpdateCheck: process.env.RAMP_NO_UPDATE_CHECK || null, path: process.env.PATH })); process.exit(0); }
if (args[0] === 'wait') { process.on('SIGINT', () => process.exit(130)); setInterval(() => {}, 1000); console.log('READY'); }
else { console.log(JSON.stringify(args)); process.exit(Number(process.env.ROUTER_TEST_EXIT || 0)); }
}
main().catch(error => { console.error(error); process.exit(1); });
`;
  fs.writeFileSync(executable, script, { mode: 0o755 });
  fs.writeFileSync(path.join(dist, 'supporting-library.txt'), 'standalone runtime');
  const archive = path.join(directory, `fixture-${version}.tar.gz`);
  await tar.c({ gzip: true, file: archive, cwd: source }, ['main.dist']);
  const checksum = crypto.createHash('sha256').update(fs.readFileSync(archive)).digest('hex');
  const download = async (url, target) => {
    if (url.endsWith('.sha256')) fs.writeFileSync(target, `${checksum}  ramp-${platformKey()}.tar.gz\n`);
    else fs.copyFileSync(archive, target);
  };
  return { archive, checksum, download };
}

module.exports = { sandbox, fixture };
