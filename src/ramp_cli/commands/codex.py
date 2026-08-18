"""ramp codex commands for OpenAI Codex CLI settings in config.toml.

Codex reads context-compaction settings from root keys in
``$CODEX_HOME/config.toml`` (``~/.codex/config.toml`` by default):

- ``model_auto_compact_token_limit`` — token count that triggers automatic
  history compaction. Codex silently clamps/ignores values above roughly 90%
  of the model context window.
- ``model_auto_compact_token_limit_scope`` — which tokens count toward the
  limit: ``total`` (default) or ``body_after_prefix``.
- ``model_context_window`` — optional override for the context window Codex
  assumes for the model.

These commands only manage those root keys and preserve everything else in
the file, including comments and unrelated tables. Every rewrite is verified
by reparsing before it is written; a file whose layout defeats the rewriter
(for example a multiline string whose lines look like TOML structure) is
refused rather than corrupted.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import click

from ramp_cli.commands.router import (
    _codex_config_path,
    _config_chunks,
    _read_codex_config,
    _write_private_file,
    codex_config_lock,
)
from ramp_cli.output.formatter import print_agent_json, resolve_format

LIMIT_KEY = "model_auto_compact_token_limit"
SCOPE_KEY = "model_auto_compact_token_limit_scope"
WINDOW_KEY = "model_context_window"
COMPACTION_KEYS = (LIMIT_KEY, SCOPE_KEY, WINDOW_KEY)
SCOPE_CHOICES = ("total", "body_after_prefix")

# Codex silently clamps/ignores auto-compact limits above ~90% of the context
# window, so a value in that range would not behave the way it reads.
CLAMP_FRACTION = 0.9

# TOML also accepts these keys quoted, so a bare-only matcher would leave a
# quoted assignment in place and write an unparseable duplicate next to it.
# Longest key first so the regex cannot stop at the shorter prefix.
_COMPACTION_ROOT_KEY = re.compile(
    r"^\s*(?:"
    + "|".join(
        alternative
        for key in (SCOPE_KEY, LIMIT_KEY, WINDOW_KEY)
        for alternative in (key, f'"{key}"', f"'{key}'")
    )
    + r")\s*="
)


@click.group("codex", help="Manage OpenAI Codex CLI settings")
def codex_group() -> None:
    pass


@codex_group.command(
    "compaction",
    help=(
        "Show or set Codex context-compaction settings "
        "(model_auto_compact_token_limit and friends) in config.toml"
    ),
)
@click.option(
    "--limit",
    type=click.IntRange(min=1),
    default=None,
    help=(f"Token count that triggers automatic history compaction ({LIMIT_KEY})"),
)
@click.option(
    "--scope",
    type=click.Choice(SCOPE_CHOICES),
    default=None,
    help=f"Which tokens count toward the limit ({SCOPE_KEY})",
)
@click.option(
    "--context-window",
    type=click.IntRange(min=1),
    default=None,
    help=f"Context window Codex assumes for the model ({WINDOW_KEY})",
)
@click.option(
    "--clear",
    is_flag=True,
    help="Remove all compaction settings from config.toml",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Print the settings that would be written without touching config.toml",
)
@click.pass_context
def codex_compaction(
    ctx: click.Context,
    limit: int | None,
    scope: str | None,
    context_window: int | None,
    clear: bool,
    dry_run: bool,
) -> None:
    fmt = resolve_format(ctx.obj["format"], ctx.obj["config_format"])
    path = _codex_config_path()
    existing, data = _read_codex_config(path)
    current = {key: _root_value(data, key) for key in COMPACTION_KEYS}

    requested = {LIMIT_KEY: limit, SCOPE_KEY: scope, WINDOW_KEY: context_window}
    changing = any(value is not None for value in requested.values())
    if clear and changing:
        raise click.UsageError(
            "--clear cannot be combined with --limit/--scope/--context-window."
        )
    if not clear and not changing:
        if dry_run:
            raise click.UsageError(
                "--dry-run requires a change; pass --limit, --scope, "
                "--context-window, or --clear."
            )
        _show(fmt, path, current)
        return

    desired = _desired(current, requested, clear)
    updated = desired != current
    if updated:
        # Proves the rewrite is safe on dry runs too, so a --dry-run that
        # prints success is one this command could actually perform.
        _render_verified(path, existing, data, desired)
    if updated and not dry_run:
        with codex_config_lock(path):
            # Re-read under the lock: a concurrent writer — another compaction
            # invocation or a Router configure — may have rewritten the file
            # since the snapshot above, and rendering from that stale snapshot
            # would silently undo its change.
            existing, data = _read_codex_config(path)
            current = {key: _root_value(data, key) for key in COMPACTION_KEYS}
            desired = _desired(current, requested, clear)
            updated = desired != current
            if updated:
                _write_private_file(
                    path, _render_verified(path, existing, data, desired)
                )
    warnings = _warnings(desired)

    if fmt == "json":
        payload = {
            "path": str(path),
            **desired,
            "updated": updated,
            "dry_run": dry_run,
        }
        if warnings:
            payload["warnings"] = warnings
        print_agent_json(payload, pagination=None)
        return

    if dry_run:
        click.echo(f"Would update {path}:")
    elif updated:
        click.echo(f"Updated {path}:")
    else:
        click.echo(f"No changes needed in {path}:")
    _echo_values(desired)
    for warning in warnings:
        click.secho(f"Warning: {warning}", fg="yellow", err=True)


def _desired(current: dict, requested: dict, clear: bool) -> dict:
    if clear:
        return dict.fromkeys(COMPACTION_KEYS)
    return {
        key: requested[key] if requested[key] is not None else current[key]
        for key in COMPACTION_KEYS
    }


def _root_value(data: dict, key: str):
    value = data.get(key)
    # A table under the same name is not a compaction setting; showing it
    # would render a dict where a number belongs.
    return None if isinstance(value, dict) else value


def _show(fmt: str, path: Path, current: dict) -> None:
    if fmt == "json":
        print_agent_json({"path": str(path), **current}, pagination=None)
        return
    click.echo(f"Codex config: {path}")
    _echo_values(current)


def _echo_values(values: dict) -> None:
    for key in COMPACTION_KEYS:
        value = values[key]
        rendered = "(not set)" if value is None else value
        click.echo(f"  {key} = {rendered}")


def _warnings(desired: dict) -> list[str]:
    warnings = []
    limit = desired[LIMIT_KEY]
    window = desired[WINDOW_KEY]
    if (
        isinstance(limit, int)
        and isinstance(window, int)
        and limit > window * CLAMP_FRACTION
    ):
        warnings.append(
            f"{LIMIT_KEY} ({limit}) is above ~90% of {WINDOW_KEY} ({window}); "
            "Codex silently clamps or ignores limits in that range."
        )
    if desired[SCOPE_KEY] is not None and limit is None:
        warnings.append(
            f"{SCOPE_KEY} has no effect without {LIMIT_KEY}; set --limit too."
        )
    return warnings


def _render_verified(path: Path, existing: str, data: dict, desired: dict) -> str:
    for key, value in desired.items():
        if value is not None and isinstance(data.get(key), dict):
            raise click.ClickException(
                f"Codex config {path} defines {key} as a table, which this "
                "command cannot rewrite; remove that table manually first."
            )
    rendered = _render_config(existing, desired)
    _verify_rewrite(path, data, rendered, desired)
    return rendered


def _render_config(existing: str, desired: dict) -> str:
    chunks = _config_chunks(existing)
    root = "".join(
        line for line in chunks[0] if not _COMPACTION_ROOT_KEY.match(line)
    ).rstrip()
    settings = "\n".join(
        # json.dumps renders ints and strings as valid TOML; None means the
        # key is removed rather than written.
        f"{key} = {json.dumps(value)}"
        for key, value in desired.items()
        if value is not None
    )
    rest = "".join("".join(chunk) for chunk in chunks[1:])
    if root and settings:
        head = f"{root}\n{settings}\n"
    elif settings:
        head = f"{settings}\n"
    else:
        head = f"{root}\n" if root else ""
    if rest and head and not head.endswith("\n\n"):
        head = f"{head}\n"
    return f"{head}{rest}"


def _verify_rewrite(path: Path, data: dict, rendered: str, desired: dict) -> None:
    """Refuse any rewrite that would not reparse to exactly the intended edit.

    The rewriter works on lines, and TOML multiline strings may contain lines
    that look like table headers or like the managed keys. Reparsing the
    result and comparing it against the original document catches every such
    misread, so the failure mode is a refusal instead of a corrupted config.
    """
    try:
        reparsed = tomllib.loads(rendered)
    except tomllib.TOMLDecodeError:
        reparsed = None
    if (
        reparsed is not None
        and all(_root_value(reparsed, key) == value for key, value in desired.items())
        and _without_compaction_keys(reparsed) == _without_compaction_keys(data)
    ):
        return
    raise click.ClickException(
        f"Could not update Codex config {path} without changing unrelated "
        "content (its layout defeats this command's rewriting); edit the "
        "compaction keys manually."
    )


def _without_compaction_keys(data: dict) -> dict:
    return {
        key: value
        for key, value in data.items()
        # A same-named table is unrelated content the rewrite must preserve,
        # so it stays in the comparison; only the managed scalars are elided.
        if key not in COMPACTION_KEYS or isinstance(value, dict)
    }
