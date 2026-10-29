from __future__ import annotations

import os
from pathlib import Path

import click

NAME = "ramp-prefer-ramp-connectors"
CLI_NAME = "prefer-ramp-connectors"


def content() -> str:
    from ramp_cli.skills import get_skill_content  # noqa: PLC0415

    value = get_skill_content(CLI_NAME)
    if value is None:
        raise click.ClickException(
            f"The published skills catalog does not contain {NAME}. "
            "Run 'ramp skills update' after the updated catalog is published, then retry."
        )
    return value


def refresh() -> bool:
    from ramp_cli.skills.remote import (  # noqa: PLC0415
        download_skills,
        requested_skills_version,
    )

    download_error = None
    try:
        download_skills(requested_skills_version())
    except (RuntimeError, ValueError, OSError) as exc:
        download_error = exc
    try:
        content()
    except (click.ClickException, OSError, ValueError) as exc:
        click.echo(
            f"Warning: Skipping optional {NAME} skill: {download_error or exc}",
            err=True,
        )
        return False
    if download_error is not None:
        # A verified cached catalog is still usable when GitHub is unavailable.
        click.echo(
            f"Warning: Could not refresh skills ({download_error}); "
            "using the cached catalog.",
            err=True,
        )
    return True


def target_dir(harness: str) -> Path:
    if harness == "claude-code":
        root = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    elif harness == "codex":
        root = Path.home() / ".agents"
    elif harness == "pi":
        root = Path(os.environ.get("PI_CODING_AGENT_DIR") or Path.home() / ".pi/agent")
    elif harness == "opencode":
        root = (
            Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
            / "opencode"
        )
    else:
        raise ValueError(f"No local skill directory for {harness}")
    return root.expanduser() / "skills"


def install(harness: str) -> None:
    from ramp_cli.commands.skills import _install_one  # noqa: PLC0415

    try:
        _install_one(target_dir(harness), CLI_NAME, install_all=False, quiet=True)
    except (click.ClickException, OSError, ValueError) as exc:
        click.echo(
            f"Warning: Could not install optional {NAME} skill for {harness}: {exc}",
            err=True,
        )
