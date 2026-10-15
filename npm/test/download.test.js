'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const https = require('node:https');
const http = require('node:http');
const { once } = require('node:events');
const { response } = require('../cli/lib/download');

test('download errors include HTTP status and HTTPS cannot be downgraded by a redirect', async t => {
  const server = http.createServer((request, res) => {
    if (request.url === '/redirect') { res.writeHead(302, { location: 'http://insecure.invalid/release' }); res.end(); }
    else { res.writeHead(404); res.end('missing asset'); }
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  t.after(() => server.close());
  t.mock.method(https, 'get', (url, options, callback) => http.get(`http://127.0.0.1:${server.address().port}${new URL(url).pathname}`, options, callback));
  await assert.rejects(response('https://fixture.invalid/404'), /HTTP 404/);
  await assert.rejects(response('https://fixture.invalid/redirect'), /require HTTPS/);
  assert.throws(() => response('http://fixture.invalid/asset'), /require HTTPS/);
});
