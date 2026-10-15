'use strict';

// An npm-managed native CLI must be updated through npm, never by its own
// curl updater, whatever its version. Everything that starts it (this
// launcher and the shortcuts below) turns off its update notices and answers
// `ramp update` itself, delegating to the persistent npm helper when available.
// Even releases that predate install receipts stay safe from the curl updater.

const VALUE_FLAGS = ['--env', '-e', '--output', '-o', '--profile'];
const FLAGS = ['--quiet', '-q', '--no-input', '--wide', '--agent', '--human'];
// Short root options as Click parses them: grouped (`-qo json`) or with an
// attached value (`-ojson`, `-esandbox`).
const SHORT_VALUES = 'oe';
const SHORT_FLAGS = 'q';

const shellQuote = value => `'${value.replace(/'/g, "'\\''")}'`;

function updateLines(packageName) {
  return [
    `This ramp was installed by npm (${packageName}). Update it with npm:`,
    `  npm update -g ${packageName}`,
    `  npx ${packageName}@latest`,
  ];
}

// Returns how many following arguments a short-option group consumes (0 or 1),
// or null when it is not made only of root options.
function shortGroup(arg) {
  for (let i = 1; i < arg.length; i++) {
    if (SHORT_VALUES.includes(arg[i])) return i === arg.length - 1 ? 1 : 0;
    if (!SHORT_FLAGS.includes(arg[i])) return null;
  }
  return 0;
}

// Mirrors Click's root-option parsing: root options may precede the
// subcommand, and `--` ends them. `ramp update --help` still reaches the
// native help.
function isUpdateCommand(args) {
  for (let i = 0; i < args.length; i++) {
    const arg = args[i];
    let command = arg;
    if (arg === '--') command = args[++i];
    else if (VALUE_FLAGS.includes(arg)) { i++; continue; }
    else if (FLAGS.includes(arg) || /^--(env|output|profile)=/.test(arg)) continue;
    else if (/^-[^-]/.test(arg)) {
      const consumed = shortGroup(arg);
      if (consumed === null) return false;
      i += consumed;
      continue;
    }
    return command === 'update' && !args.slice(i + 1).some(rest => ['--help', '-h', '--_npm-validate'].includes(rest));
  }
  return false;
}

const POSIX_MARKER = '#!/bin/sh\n# @ramp/cli npm entrypoint\n';

function posixEntrypoint(executable, packageName) {
  const target = shellQuote(executable);
  const helper = shellQuote(require('node:path').join(require('node:path').dirname(executable), 'npm-update.js'));
  return `${POSIX_MARKER}RAMP_NO_UPDATE_CHECK=1
export RAMP_NO_UPDATE_CHECK
skip=
after=
command=
for arg do
  if [ -n "$skip" ]; then skip=; continue; fi
  if [ -n "$after" ]; then command=$arg; break; fi
  case $arg in
    --) after=1 ;;
    ${VALUE_FLAGS.join('|')}) skip=1 ;;
    ${FLAGS.join('|')}|--env=*|--output=*|--profile=*) ;;
    --*) break ;;
    -?*)
      rest=\${arg#-}
      while [ -n "$rest" ]; do
        flag=\${rest%"\${rest#?}"}
        rest=\${rest#?}
        case $flag in
          [${SHORT_FLAGS}]) ;;
          [${SHORT_VALUES}]) [ -n "$rest" ] || skip=1; rest= ;;
          *) break 2 ;;
        esac
      done ;;
    *) command=$arg; break ;;
  esac
done
if [ "$command" = update ]; then
  for arg do case $arg in --help|-h|--_npm-validate) exec ${target} "$@" ;; esac; done
  if command -v node >/dev/null 2>&1 && [ -f ${helper} ]; then exec node ${helper} "$@"; fi
  printf '%s\\n' ${updateLines(packageName).map(shellQuote).join(' ')}
  exit 0
fi
exec ${target} "$@"
`;
}

const WINDOWS_MARKER = '@echo off\r\nrem @ramp/cli npm entrypoint\r\n';

function windowsEntrypoint(executable, packageName) {
  const target = `"${executable.replace(/%/g, '%%')}"`;
  const helper = `"${require('node:path').join(require('node:path').dirname(executable), 'npm-update.js').replace(/%/g, '%%')}"`;
  return [
    WINDOWS_MARKER + 'setlocal DisableDelayedExpansion',
    'set RAMP_NO_UPDATE_CHECK=1',
    'call :update %*',
    'if errorlevel 1 goto run',
    'where node >nul 2>nul',
    `if not errorlevel 1 if exist ${helper} (node ${helper} %* & exit /b)`,
    ...updateLines(packageName).map(line => `echo ${line}`),
    'exit /b 0',
    ':run',
    `${target} %*`,
    'exit /b %ERRORLEVEL%',
    // Exit 0 only for `update` (after root flags) without --help or -h.
    ':update',
    'if "%~1"=="" exit /b 1',
    'if "%~1"=="--" (shift & goto command)',
    `for %%f in (${VALUE_FLAGS.join(' ')}) do if /i "%~1"=="%%f" (shift & shift & goto update)`,
    `for %%f in (${FLAGS.join(' ')}) do if /i "%~1"=="%%f" (shift & goto update)`,
    // Attached values (-ojson, --env=sandbox) and -q groups (-qo json).
    'set "arg=%~1"',
    'if /i "%arg:~0,6%"=="--env=" (shift & goto update)',
    'if /i "%arg:~0,9%"=="--output=" (shift & goto update)',
    'if /i "%arg:~0,10%"=="--profile=" (shift & goto update)',
    // Strip every leading q of a group (-qqojson -> -ojson) before classifying.
    ':short',
    'if "%arg:~0,2%"=="-q" if not "%arg:~2,1%"=="" if not "%arg:~2,1%"=="-" (set "arg=-%arg:~2%" & goto short)',
    'if "%arg%"=="-o" (shift & shift & goto update)',
    'if "%arg%"=="-e" (shift & shift & goto update)',
    'if "%arg:~0,2%"=="-o" (shift & goto update)',
    'if "%arg:~0,2%"=="-e" (shift & goto update)',
    'if "%arg%"=="-q" (shift & goto update)',
    ':command',
    'if /i not "%~1"=="update" exit /b 1',
    ':help',
    'shift',
    'if "%~1"=="" exit /b 0',
    'if /i "%~1"=="--help" exit /b 1',
    'if /i "%~1"=="-h" exit /b 1',
    'if /i "%~1"=="--_npm-validate" exit /b 1',
    'goto help',
    '',
  ].join('\r\n');
}

module.exports = { updateLines, isUpdateCommand, posixEntrypoint, windowsEntrypoint, POSIX_MARKER, WINDOWS_MARKER };
