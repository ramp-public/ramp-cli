'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { resolveCLI } = require('./existing-cli');
const { launch } = require('./launch');
const { shouldOnboard, onboardingSession } = require('./onboarding');

const packageName = file => { try { return JSON.parse(fs.readFileSync(file, 'utf8')).name; } catch { return null; } };

// The global package whose `npm update -g` moves this install. Decided from
// the global layout, not lifecycle order (npm may run hooks in either order or
// concurrently): Router owns it when this @ramp/cli is Router's nested
// dependency or sits beside a global @ramp/router.
function globalPackage(command, cliDir = path.join(__dirname, '..')) {
  if (command === 'router') return '@ramp/router';
  const modules = path.dirname(path.dirname(cliDir));
  return [path.join(path.dirname(modules), 'package.json'), path.join(modules, '@ramp', 'router', 'package.json')]
    .some(file => packageName(file) === '@ramp/router') ? '@ramp/router' : '@ramp/cli';
}

async function postinstall(command = 'ramp', options = {}) {
  // Dependency and npx installs never onboard. Only the Router package's own
  // global lifecycle does so; @ramp/cli is install-only in every context.
  const env = options.env || process.env;
  const automatic = command === 'router' && env.npm_config_global === 'true' && shouldOnboard([], { env });
  const session = automatic ? (options.onboardingSession || onboardingSession)([], { env, lifecycle: env.npm_config_foreground_scripts !== 'true' }) : null;
  const write = text => session ? session.write(text) : process.stderr.write(`${text}\n`);
  let executable;
  try {
    // A headless install can defer the UI, including when an older curl CLI
    // needs an interactive update before the workspace is available.
    executable = await (options.resolveCLI || resolveCLI)({ command: session ? command : 'ramp', package: env.npm_config_global === 'true' ? globalPackage(command, options.cliDir) : command === 'router' ? '@ramp/router' : '@ramp/cli', globalInstall: env.npm_config_global === 'true', session, update: options.launch || launch });
    if (!automatic || !session) {
      write(command === 'router' ? 'Ramp CLI ready. Run `router` in an interactive terminal to open Ramp Router.' : 'Ramp CLI ready. Run `ramp --help` to get started.');
      return;
    }
    write('Ramp CLI ready. Opening the Router workspace...');
    const result = await (options.launch || launch)(executable, ['router', 'ui'], session.stdio);
    if (result.code !== 0 || result.signal) write('Ramp CLI remains installed. Retry with `router` in an interactive terminal.');
  } catch (error) {
    if (!automatic || !executable) throw error;
    write(`Could not open the Router workspace: ${error.message}\nRetry with \`router\` in an interactive terminal.`);
  } finally { session?.close(); }
}

module.exports = { postinstall, globalPackage };
