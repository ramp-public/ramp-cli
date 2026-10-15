'use strict';

const fs = require('node:fs');
const path = require('node:path');

const packageNamed = dir => { try { return JSON.parse(fs.readFileSync(path.join(dir, 'package.json'), 'utf8')).name; } catch { return null; } };
const topLevel = dir => {
  const modules = path.dirname(path.dirname(dir));
  return path.basename(modules) === 'node_modules' ? { modules, owner: path.dirname(modules) } : null;
};

function installationContext({ cliDir = path.join(__dirname, '..'), env = process.env, platform = process.platform } = {}) {
  // The package's location wins over npm's execution environment: npm exec
  // and npx also run project-local packages. A real cache is unambiguously npx.
  if (cliDir.split(path.sep).includes('_npx')) return { kind: 'npx' };
  let found = topLevel(cliDir);
  if (!found) return { kind: 'unknown' };
  if (packageNamed(found.owner) === '@ramp/router') found = topLevel(found.owner);
  if (!found) return { kind: 'unknown' };
  if (fs.existsSync(path.join(found.owner, 'package.json'))) return { kind: 'project' };
  if (env.npm_command === 'exec') return { kind: 'npx' };
  const prefix = platform === 'win32' ? found.owner : path.dirname(found.owner);
  // Do not mistake an arbitrary node_modules directory for a Unix global prefix.
  if (platform !== 'win32' && path.basename(found.owner) !== 'lib') return { kind: 'unknown' };
  return { kind: 'global', prefix, modules: found.modules };
}

module.exports = { installationContext };
