'use strict';

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const quote = value => `'${value.replace(/'/g, "'\\''")}'`;

// The reused CLI may predate this launcher, so its Python updater cannot be
// taught new timeouts here. Bound each curl request (including requests made
// by the hosted installer), never the installation/replacement process itself.
function updateNetwork(env, { connectTimeout = 15, maxTime = 600 } = {}) {
  const curl = (env.PATH || '').split(path.delimiter).map(dir => path.resolve(dir || '.', 'curl')).find(file => {
    try { fs.accessSync(file, fs.constants.X_OK); return fs.statSync(file).isFile(); }
    catch { return false; }
  });
  if (!curl) throw new Error('curl is required to update the existing Ramp CLI. Install curl, then run `ramp update` and retry.');
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'ramp-update-network-'));
  const cleanup = () => fs.rmSync(directory, { recursive: true, force: true });
  try {
    // Keep the user's normal curl configuration (proxy/CA settings). Put the
    // limits last so that an older installer's flags cannot disable them.
    fs.writeFileSync(path.join(directory, 'curl'), `#!/bin/sh\nexec ${quote(curl)} "$@" --connect-timeout ${connectTimeout} --max-time ${maxTime} --retry-max-time ${Math.ceil(maxTime)}\n`, { mode: 0o700 });
    return { env: { ...env, PATH: `${directory}${path.delimiter}${env.PATH || ''}` }, cleanup };
  } catch (error) { cleanup(); throw error; }
}

module.exports = { updateNetwork };
