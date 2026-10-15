'use strict';
const path = require('node:path');
const { root, pack } = require('./packages');
for (const artifact of pack(path.join(root, 'artifacts'))) console.log(artifact.archive);
