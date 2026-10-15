'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { terminal, confirm } = require('../cli/lib/terminal');

const runtime = platform => ({ platform, stdin: { isTTY: false }, stdout: { isTTY: false } });

test('headless installation returns no terminal and cleans partial handles', () => {
  const closed = [];
  let opens = 0;
  const io = { openSync: () => { if (++opens === 2) throw new Error('no console'); return 10; }, closeSync: fd => closed.push(fd) };
  assert.equal(terminal({ fs: io, process: runtime('linux') }), null);
  assert.deepEqual(closed, [10]);
});

test('direct noninteractive commands do not reopen a controlling terminal', () => {
  const io = { openSync: () => { throw new Error('Must not reopen a terminal for piped commands'); } };
  assert.equal(terminal({ fs: io, process: runtime('linux'), allowCaptured: false }), null);
});

test('Windows opens the native console instead of requiring a Unix terminal', () => {
  const opened = [], closed = [], writes = [];
  const io = {
    openSync: (file, mode) => { opened.push([file, mode]); return 10 + opened.length; },
    closeSync: fd => closed.push(fd), writeSync: (fd, text) => writes.push([fd, text]),
  };
  const session = terminal({ fs: io, process: runtime('win32'), isatty: () => true });
  assert.deepEqual(opened, [['CONIN$', 'r'], ['CONOUT$', 'r+']]);
  assert.deepEqual(session.stdio, [11, 12, 12]);
  session.write('Starting onboarding');
  assert.deepEqual(writes, [[12, 'Starting onboarding\n']]);
  session.close();
  assert.deepEqual(closed, [11, 12]);
});

test('prompts on the controlling terminal end the line so npm\'s spinner cannot erase them', async t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'ramp-confirm-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const ask = async answer => {
    fs.writeFileSync(path.join(directory, 'in'), answer);
    const input = fs.openSync(path.join(directory, 'in'), 'r');
    const output = fs.openSync(path.join(directory, 'out'), 'w');
    try { return await confirm({ stdio: [input, output, output] }, 'Update?'); }
    finally { fs.closeSync(input); fs.closeSync(output); }
  };
  assert.equal(await ask('maybe\nn\n'), false);
  assert.equal(fs.readFileSync(path.join(directory, 'out'), 'utf8'), 'Update? [Y/n]\nUpdate? [Y/n]\n');
  assert.equal(await ask('\n'), true);
});
