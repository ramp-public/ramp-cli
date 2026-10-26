'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { postinstall, globalPackage } = require('../cli/lib/postinstall');
const { launchUpdate } = require('../cli/lib/launch');

test('install-time CLI resolution also uses the network-bounded updater', async () => {
  await postinstall('ramp', { env: {}, resolveCLI: async ({ update }) => {
    assert.equal(update, launchUpdate);
    return '/test/ramp';
  } });
});

test('interactive global Router install opens the workspace once', async () => {
  const calls = [];
  const session = { stdio: 'inherit', write: text => calls.push(['message', text]), close: () => calls.push(['close']) };
  await postinstall('router', {
    env: { npm_config_global: 'true', CI: '' },
    onboardingSession: () => session,
    resolveCLI: async ({ command, session: selected, globalInstall }) => {
      assert.equal(command, 'router');
      assert.equal(globalInstall, true);
      assert.equal(selected, session);
      return '/test/ramp';
    },
    launch: async (executable, args, stdio) => {
      calls.push(['launch', executable, args, stdio]);
      return { code: 0 };
    },
  });
  assert.deepEqual(calls.filter(call => call[0] === 'launch'), [['launch', '/test/ramp', ['router', 'ui'], 'inherit']]);
  assert.deepEqual(calls.at(-1), ['close']);
});

test('headless global Router install defers the UI', async () => {
  const calls = [];
  await postinstall('router', {
    env: { npm_config_global: 'true' },
    onboardingSession: () => null,
    resolveCLI: async ({ command }) => {
      assert.equal(command, 'ramp');
      return '/test/ramp';
    },
    launch: async () => { calls.push('launch'); return { code: 0 }; },
  });
  assert.deepEqual(calls, []);
});

test('only a global install lifecycle claims the install receipt', async () => {
  let seen;
  await postinstall('ramp', { env: {}, resolveCLI: async ({ globalInstall }) => { seen = globalInstall; return '/test/ramp'; } });
  assert.equal(seen, false);
});

test('a global @ramp/cli lifecycle names Router when Router is installed globally, in any hook order', async t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'ramp-global-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const pkg = (dir, name) => { fs.mkdirSync(dir, { recursive: true }); fs.writeFileSync(path.join(dir, 'package.json'), JSON.stringify({ name })); return dir; };
  const modules = path.join(root, 'lib', 'node_modules');
  const standalone = pkg(path.join(modules, '@ramp', 'cli'), '@ramp/cli');
  assert.equal(globalPackage('ramp', standalone), '@ramp/cli');
  const router = pkg(path.join(modules, '@ramp', 'router'), '@ramp/router');
  const nested = pkg(path.join(router, 'node_modules', '@ramp', 'cli'), '@ramp/cli');
  assert.equal(globalPackage('ramp', nested), '@ramp/router');
  assert.equal(globalPackage('ramp', standalone), '@ramp/router', 'a standalone CLI beside global Router still defers to Router');
  assert.equal(globalPackage('router'), '@ramp/router');
  let claimed;
  await postinstall('ramp', { env: { npm_config_global: 'true', CI: 'true' }, cliDir: nested, resolveCLI: async options => { claimed = options.package; return '/test/ramp'; } });
  assert.equal(claimed, '@ramp/router');
});
