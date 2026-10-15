'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { isUpdateCommand } = require('../cli/lib/update-guard');

test('recognizes update after root flags, but not its help or other commands', () => {
  for (const args of [
    ['update'], ['--agent', 'update'], ['-e', 'sandbox', 'update'], ['--profile', 'x', '-q', 'update', '--yes'], ['--output=json', 'update'],
    ['-ojson', 'update'], ['-esandbox', 'update'], ['-qo', 'json', 'update'], ['-qojson', 'update'], ['-qq', 'update'], ['-qqojson', 'update'], ['-qqo', 'json', 'update'], ['--', 'update'], ['--agent', '--', 'update'],
  ]) assert.equal(isUpdateCommand(args), true, args.join(' '));
  for (const args of [
    [], ['update', '--help'], ['update', '-h'], ['status', 'update'], ['--env', 'update'], ['skills', 'update'],
    ['-o', 'update'], ['-qh', 'update'], ['--help', 'update'], ['--', 'status'], ['--'], ['--', 'update', '--help'],
  ]) assert.equal(isUpdateCommand(args), false, args.join(' '));
});
