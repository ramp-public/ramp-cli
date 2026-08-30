"""Hermes Agent (NousResearch hermes-agent) helpers for Ramp Router setup.

Hermes keeps configuration in ``$HERMES_HOME/config.yaml`` and secrets in
``$HERMES_HOME/.env`` (both default under ``~/.hermes``). Configuration
reads and writes go through ``hermes config get/set/unset`` — Hermes's
stable scripting surface — instead of editing the YAML directly, so its
managed-install guards, profile resolution, key coercion, and credential
lifecycle keep applying. The API key is the one exception: it is written
straight into ``.env`` line-by-line, because passing a secret through a
subprocess argument would expose it in process listings.

Verified against hermes-agent main (Aug 2026):
  * ``config get <key> --json`` prints JSON and exits 0; a missing key
    prints ``Config key not set: <key>`` and exits 1.
  * ``config set --force a.b value`` creates nested mappings, promotes a
    scalar ``model`` to a mapping (the scalar becomes ``model.default``),
    and skips the unknown-key notice for keys this Hermes version does not
    recognize yet.
  * ``config unset a.b`` removes whole mappings as well as leaves.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import click

HERMES_HOME_ENV = "HERMES_HOME"

#: The variable Router's docs tell Hermes users to set; the bundled Hermes
#: ``router`` provider and the ``providers.router.key_env`` entry written by
#: ``ramp router configure hermes`` both read it.
ROUTER_KEY_ENV = "RAMP_ROUTER_API_KEY"


def hermes_home() -> Path:
    """Locate the Hermes home directory, exactly as Hermes resolves it."""
    configured = os.environ.get(HERMES_HOME_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".hermes"


def config_path() -> Path:
    return hermes_home() / "config.yaml"


def env_path() -> Path:
    return hermes_home() / ".env"


def hermes_executable() -> str | None:
    """The ``hermes`` command, or None when Hermes is not installed."""
    return shutil.which("hermes")


def _run_config(*args: str) -> subprocess.CompletedProcess:
    executable = hermes_executable()
    if executable is None:
        raise click.ClickException(
            "Hermes is not installed ('hermes' is not on PATH). Install Hermes "
            "Agent from https://github.com/NousResearch/hermes-agent to manage "
            "its Router setup."
        )
    try:
        return subprocess.run(
            [executable, "config", *args],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise click.ClickException(f"Could not run 'hermes config': {exc}") from None


def read_config_json(key: str) -> object | None:
    """Return the configured value for *key*, or None when it is not set.

    Only Hermes's documented miss ("Config key not set") answers None; any
    other failure raises, so callers never mistake an unreadable
    configuration for an absent one — a refresh that did so would fall back
    to the default Router endpoint and send a stored credential to a
    different environment.
    """
    completed = _run_config("get", key, "--json")
    if completed.returncode != 0:
        output = f"{completed.stdout}\n{completed.stderr}"
        if "Config key not set" in output:
            return None
        detail = (completed.stderr or completed.stdout or "").strip()
        raise click.ClickException(
            f"Could not read Hermes config key {key!r}"
            + (f": {detail}" if detail else ".")
        )
    try:
        return json.loads(completed.stdout)
    except (json.JSONDecodeError, ValueError):
        # None is reserved for the documented miss. Malformed output on a
        # successful exit must not read as "absent": a snapshot taken from it
        # would record the user's real setting as missing and a later restore
        # would delete it.
        raise click.ClickException(
            f"Could not parse Hermes config key {key!r} as JSON."
        ) from None


def _config_argument(value: object) -> str:
    """Render one snapshot leaf as the argument ``hermes config set`` expects.

    Hermes auto-coerces ``true``/``false`` and numeric strings back to their
    typed values, so round-tripping a bool or number through the string
    argument restores the original type.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def set_config_value(key: str, value: object) -> None:
    """Write one configuration value through ``hermes config set``.

    ``--force`` keeps scripted writes quiet for keys the installed Hermes
    version does not recognize yet (its documented purpose), and authorizes
    restoring a scalar over a section this CLI's setup turned into a
    mapping.
    """
    completed = _run_config("set", "--force", key, _config_argument(value))
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise click.ClickException(
            f"Could not write Hermes config key {key!r}"
            + (f": {detail}" if detail else ".")
        )


def set_config_tree(key: str, value: object) -> None:
    """Write a snapshot value back, descending into nested mappings.

    ``hermes config set`` accepts scalars only, so restoration covers the
    scalar-and-mapping shapes Hermes model and provider entries actually
    use. A list or explicit-null leaf has no CLI representation and is
    skipped rather than stringified into a different type.
    """
    if isinstance(value, dict):
        for child_key, child_value in value.items():
            if child_value is None:
                continue
            set_config_tree(f"{key}.{child_key}", child_value)
        return
    if isinstance(value, (list, tuple)):
        return
    set_config_value(key, value)


def unset_config_key(key: str) -> None:
    """Remove *key* (leaf or whole mapping); a key already absent is fine.

    Any other failure raises: an unconfigure that shrugged off a rejected
    unset would go on to delete the credential and the restoration receipt
    while the Router settings it reported removing remained active.
    """
    completed = _run_config("unset", key)
    if completed.returncode == 0:
        return
    output = f"{completed.stdout}\n{completed.stderr}"
    if "Config key not set" in output:
        return
    detail = (completed.stderr or completed.stdout or "").strip()
    raise click.ClickException(
        f"Could not remove Hermes config key {key!r}"
        + (f": {detail}" if detail else ".")
    )


def read_env_value(name: str) -> str | None:
    """Read one ``NAME=value`` line from Hermes's .env file.

    Only a missing file reads as "no value". Any other failure — invalid
    UTF-8, a transient I/O error — raises: reporting an unreadable file as
    absence poisoned the configure snapshot with ``env_key: null``, after
    which a retry overwrote the user's key and unconfigure deleted it
    instead of restoring it.

    With duplicate assignments, the last one wins — dotenv semantics, and
    what Hermes effectively reads. Snapshotting the first instead recorded
    the wrong credential and let a restore destroy a key the user appended.
    """
    line = read_env_line(name)
    if line is None:
        return None
    stripped = line.strip()
    if stripped.startswith("export "):
        stripped = stripped[len("export ") :].lstrip()
    return _decode_env_value(stripped[len(name) + 1 :])


def _decode_env_value(rhs: str) -> str | None:
    """Decode an assignment's right-hand side the way dotenv reads it.

    A quoted value ends at its closing quote, with anything after it — such
    as a trailing comment — ignored; an unquoted value ends at a
    whitespace-led ``#``. Stripping quote characters from both ends instead
    turned ``'secret' # note`` into ``secret' # note`` and sent the wrong
    bearer token. The closing quote is the first *unescaped* one, and
    escaped quotes and backslashes inside decode to their literal
    characters, so a credential containing a quote survives intact.
    """
    rhs = rhs.strip()
    if rhs[:1] in ("'", '"'):
        quote = rhs[0]
        decoded: list[str] = []
        index = 1
        while index < len(rhs):
            char = rhs[index]
            if (
                char == "\\"
                and index + 1 < len(rhs)
                and rhs[index + 1]
                in (
                    "\\",
                    quote,
                )
            ):
                decoded.append(rhs[index + 1])
                index += 2
                continue
            if char == quote:
                return "".join(decoded) or None
            decoded.append(char)
            index += 1
        # No unescaped closing quote: fall through to unquoted handling.
    for index, char in enumerate(rhs):
        if char == "#" and (index == 0 or rhs[index - 1].isspace()):
            rhs = rhs[:index]
            break
    return rhs.strip() or None


def read_env_line(name: str) -> str | None:
    """The last ``NAME=`` assignment line, representation intact.

    Quoting and any ``export`` prefix stay intact: a value like
    ``'secret # suffix'`` depends on its dotenv representation, and an
    exported line feeds consumers that source the shared ``.env`` — a
    snapshot that decoded either would restore a line that behaves
    differently. Use :func:`read_env_value` for the decoded value a client
    would send.
    """
    path = env_path()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError) as exc:
        raise click.ClickException(f"Could not read {path}: {exc}") from None
    value: str | None = None
    for line in lines:
        if _is_env_line_for(name, line):
            value = line
    return value


def _is_env_line_for(name: str, line: str) -> bool:
    stripped = line.strip()
    if stripped.startswith("export "):
        stripped = stripped[len("export ") :].lstrip()
    return stripped.startswith(f"{name}=")


def write_env_value(name: str, value: str | None) -> None:
    """Replace or append ``NAME=value`` in Hermes's .env; None removes it.

    Every other line is preserved byte-for-byte. The file is created with
    owner-only permissions when missing, matching how Hermes treats it as
    its secrets store.
    """
    write_env_line(name, f"{name}={value}" if value is not None else None)


def write_env_line(name: str, assignment: str | None) -> None:
    """Replace or append a raw assignment line for *name*; None removes it.

    The restore path hands back the exact snapshot line so quoting and any
    ``export`` prefix survive the round trip.
    """
    path = env_path()
    try:
        existing = path.read_text(encoding="utf-8")
        lines = existing.splitlines()
    except FileNotFoundError:
        existing = None
        lines = []
    except (OSError, UnicodeError) as exc:
        raise click.ClickException(f"Could not read {path}: {exc}") from None

    kept = [line for line in lines if not _is_env_line_for(name, line)]
    if assignment is not None:
        kept.append(assignment)
    if existing is None and assignment is None:
        return
    content = ("\n".join(kept) + "\n") if kept else ""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # A dotfiles-managed .env may be a symlink; write through to its
        # target so the link survives, instead of os.replace() swapping the
        # link itself for a regular file and silently disconnecting it.
        if path.is_symlink():
            path = path.resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
        # Removals rewrite the whole secrets file too, and it can hold
        # credentials the receipt knows nothing about — so they take the same
        # atomic temp-file-and-replace path as writes: an interruption or
        # short write can never truncate unrelated entries. A removal keeps
        # the file's existing permissions; a write that stores a secret
        # enforces owner-only even when a pre-existing .env arrived group- or
        # world-readable under a permissive umask.
        if assignment is None:
            mode = path.stat().st_mode & 0o777
        else:
            mode = 0o600
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(tmp_name, mode)
            os.replace(tmp_name, path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except OSError as exc:
        raise click.ClickException(f"Could not write {path}: {exc}") from None
