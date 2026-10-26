'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const { execFile } = require('node:child_process');
const { promisify } = require('node:util');
const { pathToFileURL } = require('node:url');
const { updateNetwork } = require('../cli/lib/update-network');
const { launch } = require('../cli/lib/launch');
const { sandbox } = require('./helpers');

const run = promisify(execFile);

test('update curl bounds preserve arguments, user settings and executable paths with quotes', async t => {
  const p = sandbox(t);
  const bin = path.join(p.home, "curl's bin");
  fs.mkdirSync(bin);
  fs.writeFileSync(path.join(bin, 'curl'), '#!/usr/bin/env node\nconsole.log(JSON.stringify({args:process.argv.slice(2),home:process.env.HOME,ca:process.env.CURL_CA_BUNDLE}));\n', { mode: 0o755 });
  const env = { ...p.env, PATH: `${bin}:${p.env.PATH}`, CURL_CA_BUNDLE: '/custom/cert.pem' };
  const bounded = updateNetwork(env);
  t.after(bounded.cleanup);
  const directory = bounded.env.PATH.split(path.delimiter)[0];
  const result = JSON.parse((await run('/bin/sh', ['-c', 'curl -fsSL "https://example.invalid/a b"'], { env: bounded.env })).stdout);
  assert.deepEqual(result.args, ['-fsSL', 'https://example.invalid/a b', '--connect-timeout', '15', '--max-time', '600', '--retry-max-time', '600']);
  assert.equal(result.home, p.home);
  assert.equal(result.ca, env.CURL_CA_BUNDLE);
  assert.equal(env.PATH.startsWith(directory), false, 'parent environment is unchanged');
  bounded.cleanup();
  assert.equal(fs.existsSync(directory), false);
});

test('a stalled transfer times out before replacement and permits a later retry', async t => {
  const p = sandbox(t);
  const installed = path.join(p.home, 'installed');
  fs.writeFileSync(installed, 'old CLI');
  const server = http.createServer((request, response) => {
    if (request.url === '/good') response.end('new CLI');
    // The other route deliberately never sends a response.
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => { server.closeAllConnections(); server.close(); });
  const bounded = updateNetwork(p.env, { connectTimeout: 1, maxTime: 0.2 });
  t.after(bounded.cleanup);
  const url = `http://127.0.0.1:${server.address().port}`;
  const script = 'curl -q --noproxy "*" -fsS "$1" -o "$2.download" && mv "$2.download" "$2"';
  await assert.rejects(run('/bin/sh', ['-c', script, 'installer', `${url}/stall`, installed], { env: bounded.env, timeout: 5000 }), error => error.code === 28);
  assert.equal(fs.readFileSync(installed, 'utf8'), 'old CLI');
  await run('/bin/sh', ['-c', script, 'installer', `${url}/good`, installed], { env: bounded.env, timeout: 5000 });
  assert.equal(fs.readFileSync(installed, 'utf8'), 'new CLI');
});

test('finishing a transfer does not put the subsequent replacement under a kill timer', async t => {
  const p = sandbox(t);
  const bounded = updateNetwork(p.env, { connectTimeout: 1, maxTime: 0.1 });
  t.after(bounded.cleanup);
  const source = path.join(p.home, 'download');
  const installed = path.join(p.home, 'installed');
  fs.writeFileSync(source, 'new CLI');
  await run('/bin/sh', ['-c', 'curl -q -fsS "$1" -o "$2" && sleep 1 && printf replaced >> "$2"', 'installer', pathToFileURL(source).href, installed], { env: bounded.env, timeout: 5000 });
  assert.equal(fs.readFileSync(installed, 'utf8'), 'new CLIreplaced');
});

test('missing curl fails before starting an updater', () => {
  assert.throws(() => updateNetwork({ PATH: '/nonexistent/ramp-test-bin' }), /curl is required/);
});

test('updater launch cleans up its private curl wrapper on exit, cancellation and spawn failure', async t => {
  const directories = [];
  const mkdtemp = fs.mkdtempSync;
  t.mock.method(fs, 'mkdtempSync', (...args) => {
    const directory = mkdtemp(...args);
    directories.push(directory);
    return directory;
  });
  const args = ['-e', 'const fs=require("node:fs");const p=require("node:path");if(!fs.readFileSync(p.join(process.env.PATH.split(p.delimiter)[0],"curl"),"utf8").includes("--max-time 600"))process.exit(99);'];
  assert.equal((await launch(process.execPath, args, 'ignore', false, true)).code, 0);
  assert.equal((await launch(process.execPath, ['-e', 'process.kill(process.pid,"SIGTERM")'], 'ignore', false, true)).signal, 'SIGTERM');
  await assert.rejects(launch('/nonexistent/ramp-test-updater', [], 'ignore', false, true), /ENOENT/);
  assert.equal(directories.length, 3);
  for (const directory of directories) assert.equal(fs.existsSync(directory), false);
});
