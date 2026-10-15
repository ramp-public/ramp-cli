'use strict';

const fs = require('node:fs');
const tty = require('node:tty');
// Read only this answer: buffering stdin here can consume keystrokes intended
// for the native onboarding process that follows.
async function confirm(session, question) {
  const input = session.stdio === 'inherit' ? 0 : session.stdio[0];
  const output = session.stdio === 'inherit' ? 1 : session.stdio[1];
  // A session attached to the controlling terminal runs inside an npm
  // lifecycle script, where npm redraws its progress spinner over the current
  // line. End the question there so the spinner cannot erase it.
  const prompt = `${question} [Y/n]${session.stdio === 'inherit' ? ' ' : '\n'}`;
  const byte = Buffer.alloc(1);
  while (true) {
    fs.writeSync(output, prompt);
    let answer = '';
    while (true) {
      let count;
      try { count = fs.readSync(input, byte, 0, 1, null); }
      catch (error) {
        if (error.code !== 'EAGAIN') throw error;
        await new Promise(resolve => setTimeout(resolve, 20));
        continue;
      }
      if (!count) return false;
      if (byte[0] === 10) break;
      answer += byte.toString();
    }
    answer = answer.trim().toLowerCase();
    if (['n', 'no'].includes(answer)) return false;
    if (['', 'y', 'yes'].includes(answer)) return true;
  }
}

function terminal(options = {}) {
  const io = options.fs || fs;
  const runtime = options.process || process;
  const isatty = options.isatty || tty.isatty;
  if (runtime.stdin.isTTY && runtime.stdout.isTTY) {
    return { stdio: 'inherit', write: text => runtime.stderr.write(`${text}\n`), close() {} };
  }
  if (options.allowCaptured === false) return null;
  // npm normally captures lifecycle stdio. Attach to the controlling terminal
  // directly so prompts remain visible and interactive during global install.
  const descriptors = [];
  try {
    const windows = runtime.platform === 'win32';
    const input = io.openSync(windows ? 'CONIN$' : '/dev/tty', 'r');
    descriptors.push(input);
    const output = io.openSync(windows ? 'CONOUT$' : '/dev/tty', windows ? 'r+' : 'w');
    descriptors.push(output);
    if (!isatty(input) || !isatty(output)) throw new Error('No interactive terminal.');
    return {
      stdio: [input, output, output],
      write: text => io.writeSync(output, `${text}\n`),
      close: () => descriptors.forEach(fd => io.closeSync(fd)),
    };
  } catch {
    descriptors.forEach(fd => io.closeSync(fd));
    return null;
  }
}

module.exports = { terminal, confirm };
