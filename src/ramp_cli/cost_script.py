"""Run a Router cost script with this CLI's own Python.

Router serves its Claude Code status line and Codex cost hook as Python
scripts with a ``python3`` shebang. Windows ignores shebangs and often has no
real python3, so there the agents run ``ramp router run-cost-script <path>``
instead. Agents run these after nearly every update, so ``ramp_cli.main``
dispatches here before importing the rest of the CLI.

Kept to the standard library for the same reason.
"""

from __future__ import annotations

# The scripts' own imports, named here so a frozen build bundles them: the
# scripts are downloaded at configure time and never seen by the compiler.
import decimal  # noqa: F401
import hashlib  # noqa: F401
import io
import json  # noqa: F401
import ntpath
import os
import re
import runpy
import shlex
import stat  # noqa: F401
import sys
import time  # noqa: F401
import urllib.error  # noqa: F401
import urllib.parse  # noqa: F401
import urllib.request  # noqa: F401
from pathlib import Path

COMMAND = ("router", "run-cost-script")
KEY_FILE_OPTION = "--key-file"
BASE_URL_OPTION = "--base-url"


def run_if_invoked(argv: list[str]) -> None:
    """Run and exit when ``argv`` is exactly a cost-script invocation."""
    if tuple(argv[1:3]) == COMMAND:
        sys.exit(run(argv[3:]))


def run(args: list[str]) -> int:
    """Run ``[--key-file P] [--base-url U] SCRIPT [ARG...]``; return its exit code.

    ``--key-file`` and ``--base-url`` become ROUTER_API_KEY and
    ROUTER_BASE_URL, which the POSIX hook command sets with shell syntax that
    cmd.exe does not have. Stdin, stdout, argv and the exit code pass through.
    """
    args = list(args)
    environment: dict[str, str] = {}
    while len(args) >= 2 and args[0] in (KEY_FILE_OPTION, BASE_URL_OPTION):
        option, value = args.pop(0), args.pop(0)
        if option == BASE_URL_OPTION:
            environment["ROUTER_BASE_URL"] = value
            continue
        try:
            environment["ROUTER_API_KEY"] = Path(value).read_text("utf-8").strip()
        except (OSError, UnicodeError):
            # Same as the POSIX form's `2>/dev/null`: the script stays silent.
            environment["ROUTER_API_KEY"] = ""
    if not args:
        print(
            "usage: ramp router run-cost-script [--key-file PATH] "
            "[--base-url URL] SCRIPT [ARG...]",
            file=sys.stderr,
        )
        return 2
    os.environ.update(environment)
    _use_utf8_stdio()
    sys.argv = list(args)
    try:
        runpy.run_path(args[0], run_name="__main__")
    except SystemExit as exc:
        return _exit_code(exc.code)
    finally:
        sys.stdout.flush()
    return 0


def _use_utf8_stdio() -> None:
    """Read and write UTF-8 like the agents do.

    Windows pipes default to the ANSI code page, which cannot encode the
    scripts' bar and separator characters.
    """
    for name in ("stdin", "stdout", "stderr"):
        stream = getattr(sys, name)
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8")


def _exit_code(code: object) -> int:
    if code is None:
        return 0
    if isinstance(code, int):
        return code
    print(code, file=sys.stderr)
    return 1


# One word Git Bash, cmd.exe and PowerShell all pass through verbatim.
# Backslashes are escapes to Git Bash, so the status line keeps forward
# slashes; the Codex hook, never run by Git Bash, puts backslashes back.
_BARE_WORD = re.compile(r"[A-Za-z0-9_.:/~+-]+")


def windows_executable() -> str | None:
    """The native ramp executable for Windows agent configs to run, or None.

    The running binary rather than the ramp.cmd shortcut on PATH, which would
    put a cmd.exe start in front of every status line refresh, or another
    install that comes first on PATH.
    """
    invoked = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    # pip and uv's ramp.exe launchers drop ".exe" from argv[0] before main().
    for candidate in (invoked, f"{invoked}.exe"):
        if candidate.lower().endswith(".exe") and os.path.isfile(candidate):
            return candidate
    import shutil  # noqa: PLC0415

    return shutil.which("ramp")


def shell_command(executable: str, script: Path) -> str | None:
    """The command for a shell that is Git Bash or PowerShell, or None.

    Claude Code runs the status line through whichever of the two is
    installed. A quoted program name is a string to PowerShell rather than a
    command, so every word must be bare; a path that cannot be is tried in
    its 8.3 short form.
    """
    words = [bare_path(executable), *COMMAND, bare_path(str(script))]
    return " ".join(words) if all(words) else None  # type: ignore[arg-type]


def hook_command(
    executable: str, script: Path, *, key_file: Path, base_url: str
) -> str | None:
    """The Codex hook command, for cmd.exe or PowerShell, or None.

    Codex runs a hook through cmd.exe, or through the session's own shell,
    PowerShell by default on Windows, when it passes one. The two quote
    differently, and PowerShell won't run a quoted program without ``&``, so
    every word is bare: backslash paths, in 8.3 short form where needed.
    """
    words = [
        bare_path(executable, native=True),
        *COMMAND,
        KEY_FILE_OPTION,
        bare_path(str(key_file), native=True),
        BASE_URL_OPTION,
        base_url if _BARE_WORD.fullmatch(base_url) else None,
        bare_path(str(script), native=True),
    ]
    return " ".join(words) if all(words) else None  # type: ignore[arg-type]


def invocation(command: str) -> tuple[dict[str, str], str] | None:
    """Split a written ``<ramp> router run-cost-script`` command, or None.

    Returns the options and the script path, for recognizing and reading
    back the commands :func:`shell_command` and :func:`hook_command` write.
    """
    try:
        # The Codex form keeps Windows backslashes, which POSIX parsing would
        # treat as escapes; the Claude form uses forward slashes.
        words = shlex.split(command, posix="\\" not in command)
    except ValueError:
        return None
    if tuple(words[1:3]) != COMMAND:
        return None
    words = words[3:]
    options: dict[str, str] = {}
    while len(words) >= 2 and words[0] in (KEY_FILE_OPTION, BASE_URL_OPTION):
        options[words[0]] = words[1]
        words = words[2:]
    return (options, words[0]) if len(words) == 1 else None


def same_windows_path(left: str, right: str) -> bool:
    """Compare Windows paths, whether written long or in 8.3 short form."""

    def normalized(path: str) -> str:
        return ntpath.normcase(ntpath.normpath(_long_path(path)))

    return normalized(left) == normalized(right)


def bare_path(path: str, *, native: bool = False) -> str | None:
    """A Windows path as one bare word, or None; ``native`` for backslashes."""
    for candidate in (path, _short_path(path)):
        word = (candidate or "").replace("\\", "/")
        if _BARE_WORD.fullmatch(word):
            return word.replace("/", "\\") if native else word
    return None


def _short_path(path: str) -> str | None:
    """The path with its folder in 8.3 short form, or None where there is none.

    The name stays as written: a file gets its own short name only once it
    exists, and the command is written before the script is downloaded, so
    shortening only the folder keeps the command the same afterwards.
    """
    return _with_folder_converted("GetShortPathNameW", path)


def _long_path(path: str) -> str:
    """Expand 8.3 short folder names, so either form of a path compares equal."""
    return (
        _windows_path_call("GetLongPathNameW", path)
        or _with_folder_converted("GetLongPathNameW", path)
        or path
    )


def _with_folder_converted(function: str, path: str) -> str | None:
    folder, name = ntpath.split(path)
    converted = _windows_path_call(function, folder)
    return ntpath.join(converted, name) if converted else None


def _windows_path_call(function: str, path: str) -> str | None:
    """Call a kernel32 path conversion, or None off Windows or on failure."""
    if os.name != "nt" or not path:
        return None
    import ctypes  # noqa: PLC0415

    buffer = ctypes.create_unicode_buffer(32768)
    convert = getattr(ctypes.windll.kernel32, function)  # type: ignore[attr-defined]
    length = convert(path, buffer, len(buffer))
    return buffer.value if 0 < length < len(buffer) else None
