'use strict';

// Only test child processes load this module. Production download URLs and TLS
// verification are unchanged; npm hooks exercise the real installer against a
// local release fixture instead of downloading an actual release for each case.
const https = require('node:https');
const http = require('node:http');
const original = https.get;
https.get = function get(url, options, callback) {
  const parsed = new URL(url);
  if (parsed.hostname === 'github.com' && parsed.pathname.startsWith('/ramp-public/ramp-cli/releases/download/v')) {
    return http.get(`${process.env.ROUTER_TEST_ORIGIN}/${parsed.pathname.split('/').at(-1)}`, options, callback);
  }
  return original.call(this, url, options, callback);
};

// Package tests exercise headless installs. Without this, a hook run from a
// developer's terminal would attach to it through /dev/tty (which detaching
// does not prevent on macOS), so behave as if no controlling terminal exists.
const fs = require('node:fs');
const openSync = fs.openSync;
fs.openSync = function (file, ...rest) {
  if (['/dev/tty', 'CONIN$', 'CONOUT$'].includes(file)) throw Object.assign(new Error(`ENXIO: no controlling terminal, open '${file}'`), { code: 'ENXIO' });
  return openSync.call(this, file, ...rest);
};
