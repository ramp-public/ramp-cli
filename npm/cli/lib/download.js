'use strict';

const fs = require('node:fs');
const https = require('node:https');
const { pipeline } = require('node:stream/promises');

// Follow GitHub's asset redirects, with a deadline and TLS verification intact.
function response(url, redirects = 5) {
  if (new URL(url).protocol !== 'https:') throw new Error('Release downloads require HTTPS.');
  return new Promise((resolve, reject) => {
    const request = https.get(url, { headers: { 'User-Agent': '@ramp/cli' } }, res => {
      if ([301, 302, 303, 307, 308].includes(res.statusCode)) {
        res.resume();
        if (!res.headers.location || !redirects) return reject(new Error('Too many release redirects.'));
        try { resolve(response(new URL(res.headers.location, url).href, redirects - 1)); }
        catch (error) { reject(error); }
      } else if (res.statusCode !== 200) {
        res.resume();
        reject(new Error(`Download failed: HTTP ${res.statusCode} (${url}).`));
      } else {
        // Cover a stalled body as well as connection setup.
        res.on('close', () => clearTimeout(timer));
        resolve(res);
      }
    });
    const timer = setTimeout(() => request.destroy(new Error(`Download timed out (${url}).`)), 120_000);
    request.on('error', error => { clearTimeout(timer); reject(error); });
    request.on('response', res => { if (res.statusCode !== 200) clearTimeout(timer); });
  });
}

async function download(url, destination) {
  const res = await response(url);
  await pipeline(res, fs.createWriteStream(destination, { flags: 'wx' }));
}

async function latestVersion() {
  const res = await response('https://api.github.com/repos/ramp-public/ramp-cli/releases/latest');
  let body = '';
  for await (const chunk of res) {
    body += chunk;
    if (body.length > 2_000_000) { res.destroy(); throw new Error('Release metadata is too large.'); }
  }
  return JSON.parse(body).tag_name;
}

module.exports = { download, latestVersion, response };
