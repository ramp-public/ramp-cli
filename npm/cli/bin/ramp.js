#!/usr/bin/env node
'use strict';
require('../lib/launch').main('ramp', process.argv.slice(2)).catch(error => {
  console.error(`@ramp/cli: ${error.message}`);
  process.exitCode = error.exitCode || 1;
});
