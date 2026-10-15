#!/usr/bin/env node
'use strict';
require('@ramp/cli/postinstall').postinstall('router').catch(error => {
  console.error(`@ramp/router: ${error.message}`);
  process.exitCode = 1;
});
