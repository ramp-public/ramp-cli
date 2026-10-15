'use strict';
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { pack } = require('./packages');

const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'ramp-npm-check-'));
try {
  for (const artifact of pack(directory)) console.log(`${artifact.name}@${artifact.version}: ${artifact.files.length} allowed files, ${artifact.size} bytes`);
} finally { fs.rmSync(directory, { recursive: true, force: true }); }
