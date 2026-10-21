'use strict';

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { paths, readState } = require('./installer');
const { windowsEntrypoint, WINDOWS_MARKER } = require('./update-guard');
const { confirm } = require('./terminal');

const shellQuote = value => `'${value.replace(/'/g, "'\\''")}'`;
const powershellQuote = value => `'${value.replace(/'/g, "''")}'`;

function exposeCommand(options = {}) {
  const p = paths(options);
  const executable = path.resolve(options.nativeExecutable || p.executable);
  const platform = options.platform || process.platform;
  // An npm-managed CLI is reached only through update-guarded entrypoints; a
  // reused CLI from another installer keeps its own updater.
  const managed = executable.startsWith(`${p.root}${path.sep}`);
  const target = managed && p.entry ? p.entry : executable;
  fs.mkdirSync(p.bin, { recursive: true });
  if (platform === 'win32') {
    const command = path.join(p.bin, 'ramp.cmd');
    const body = managed
      ? windowsEntrypoint(executable, readState(p)?.package || (options.command === 'router' ? '@ramp/router' : '@ramp/cli'))
      : `@echo off\r\n"${executable.replace(/%/g, '%%')}" %*\r\nexit /b %ERRORLEVEL%\r\n`;
    // A .cmd launcher works in PowerShell and Command Prompt without changing
    // PowerShell's script execution policy.
    const nativeHere = fs.existsSync(path.join(p.bin, 'ramp.exe')) && fs.existsSync(executable) && fs.realpathSync(path.join(p.bin, 'ramp.exe')) === fs.realpathSync(executable);
    const current = fs.existsSync(command) ? fs.readFileSync(command, 'utf8') : null;
    if ((!nativeHere && ['ramp.exe', 'ramp.ps1'].some(name => fs.existsSync(path.join(p.bin, name)))) || (current !== null && current !== body && !current.startsWith(WINDOWS_MARKER))) {
      throw new Error(`An existing ramp command occupies ${p.bin}; it was preserved. Run ${p.executable} directly or choose RAMP_CLI_BIN_DIR.`);
    }
    if (!nativeHere && current !== body) fs.writeFileSync(command, body);
  } else {
    const command = path.join(p.bin, 'ramp');
    try { fs.symlinkSync(target, command); }
    catch (error) {
      if (error.code !== 'EEXIST') throw error;
      const same = path.resolve(command) === target
        || (fs.lstatSync(command).isSymbolicLink() && path.resolve(p.bin, fs.readlinkSync(command)) === target)
        || (fs.existsSync(command) && fs.existsSync(target) && fs.realpathSync(command) === fs.realpathSync(target));
      // A link into the managed root is ours, e.g. one targeting another
      // architecture's distribution or the bare executable; retarget it.
      const owned = !same && fs.lstatSync(command).isSymbolicLink() && path.resolve(p.bin, fs.readlinkSync(command)).startsWith(`${p.root}${path.sep}`);
      if (!same && !owned) throw new Error(`An existing ${command} was preserved. Run ${target} directly or choose RAMP_CLI_BIN_DIR.`);
      if (owned) {
        const stage = fs.mkdtempSync(path.join(p.bin, '.ramp-'));
        try {
          fs.symlinkSync(target, path.join(stage, 'ramp'));
          fs.renameSync(path.join(stage, 'ramp'), command);
        } finally { fs.rmSync(stage, { recursive: true, force: true }); }
      }
    }
  }
  if (options.command !== 'router') return p.bin;
  const windows = platform === 'win32';
  const router = path.join(p.bin, windows ? 'router.cmd' : 'router');
  if (windows && ['router.exe', 'router.ps1', 'router.bat'].some(name => fs.existsSync(path.join(p.bin, name)))) throw new Error(`An existing router command in ${p.bin} was preserved. Choose RAMP_CLI_BIN_DIR for the Router shortcut.`);
  const marker = windows ? '@echo off\r\nrem @ramp/router native launcher\r\n' : '#!/bin/sh\n# @ramp/router native launcher\n';
  const body = windows
    ? `${marker}setlocal DisableDelayedExpansion\r\n${managed ? 'set RAMP_NO_UPDATE_CHECK=1\r\n' : ''}if "%~1"=="--version" goto version\r\nif "%~1"=="-V" goto version\r\nif "%~1"=="" set RAMP_TUI=1\r\n"${executable.replace(/%/g, '%%')}" router %*\r\nexit /b %ERRORLEVEL%\r\n:version\r\n"${executable.replace(/%/g, '%%')}" --version\r\nexit /b %ERRORLEVEL%\r\n`
    : `${marker}case "\${1-}" in --version|-V) exec ${shellQuote(target)} --version ;; esac\nif [ "$#" -eq 0 ]; then export RAMP_TUI=1; fi\nexec ${shellQuote(target)} router "$@"\n`;
  try { fs.writeFileSync(router, body, { flag: 'wx', mode: 0o755 }); }
  catch (error) {
    if (error.code !== 'EEXIST') throw error;
    if (!fs.lstatSync(router).isFile() || !fs.readFileSync(router, 'utf8').startsWith(marker)) throw new Error(`An existing ${router} was preserved. Choose RAMP_CLI_BIN_DIR for the Router shortcut.`);
    if (fs.readFileSync(router, 'utf8') !== body) {
      const stage = fs.mkdtempSync(path.join(p.bin, '.router-'));
      try {
        const next = path.join(stage, 'router');
        fs.writeFileSync(next, body, { flag: 'wx', mode: 0o755 });
        fs.renameSync(next, router);
      } finally { fs.rmSync(stage, { recursive: true, force: true }); }
    }
  }
  return p.bin;
}

function configurePath(bin, options = {}) {
  const env = options.env || process.env;
  const home = options.home || os.homedir();
  const platform = options.platform || process.platform;
  const separator = platform === 'win32' ? ';' : ':';
  if ((env.PATH || '').split(separator).some(part => platform === 'win32' ? part.toLowerCase() === bin.toLowerCase() : part === bin)) return;
  if (platform === 'win32') {
    const script = `$p=[Environment]::GetEnvironmentVariable('Path','User'); $b=${powershellQuote(bin)}; if (($p -split ';') -notcontains $b) { [Environment]::SetEnvironmentVariable('Path', (($p + ';' + $b).Trim(';')), 'User') }`;
    const result = (options.spawnSync || spawnSync)('powershell.exe', ['-NoProfile', '-NonInteractive', '-EncodedCommand', Buffer.from(script, 'utf16le').toString('base64')], { windowsHide: true, stdio: 'ignore' });
    if (result.status !== 0) throw new Error(`Add ${bin} to your Windows user PATH manually.`);
  } else {
    const shell = path.basename(env.SHELL || '');
    let files;
    let line = `export PATH=${shellQuote(bin)}:"$PATH"`;
    if (shell === 'zsh') files = [path.join(env.ZDOTDIR || home, '.zshrc')];
    else if (shell === 'bash') files = [path.join(home, '.bashrc'), ['.bash_profile', '.bash_login', '.profile'].map(name => path.join(home, name)).find(file => fs.existsSync(file)) || path.join(home, platform === 'darwin' ? '.bash_profile' : '.profile')];
    else if (shell === 'fish') {
      files = [path.join(env.XDG_CONFIG_HOME || path.join(home, '.config'), 'fish', 'config.fish')];
      line = `fish_add_path -- '${bin.replace(/\\/g, '\\\\').replace(/'/g, "\\'")}'`;
    } else throw new Error(`Add ${bin} to PATH in your shell startup file to use ramp in future terminals.`);
    for (const file of files) {
      const current = fs.existsSync(file) ? fs.readFileSync(file, 'utf8') : '';
      if (!current.split('\n').includes(line)) {
        fs.mkdirSync(path.dirname(file), { recursive: true });
        fs.appendFileSync(file, `\n# Added by @ramp/cli\n${line}\n`);
      }
    }
  }
}

async function refreshPath(session, options = {}) {
  const bin = exposeCommand(options);
  const env = options.env || process.env;
  const separator = (options.platform || process.platform) === 'win32' ? ';' : ':';
  const onPath = (env.PATH || '').split(separator).some(part =>
    separator === ';' ? part.toLowerCase() === bin.toLowerCase() : part === bin);
  if (!onPath) {
    if (!session || !await (options.confirm || confirm)(session, 'Refresh PATH for future terminals?')) return;
    configurePath(bin, options);
  }
  env.PATH = onPath ? env.PATH : `${bin}${separator}${env.PATH || ''}`;
}

module.exports = { exposeCommand, configurePath, refreshPath };
