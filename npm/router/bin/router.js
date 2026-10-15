#!/usr/bin/env node
'use strict';
require('@ramp/cli/launcher').main('router', process.argv.slice(2)).catch(error => {
  console.error(`@ramp/router: ${error.message}`);
  process.exitCode = 1;
});
