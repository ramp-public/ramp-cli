#!/usr/bin/env node
'use strict';
require('../lib/postinstall').postinstall('ramp').catch(error => {
  console.error(`@ramp/cli: ${error.message}`);
  process.exitCode = 1;
});
