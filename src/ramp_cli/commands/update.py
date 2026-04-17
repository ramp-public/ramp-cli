"""ramp update — self-update to the latest CLI version."""

from __future__ import annotations

import shutil
import subprocess

import click

from ramp_cli import __version__
from ramp_cli.version_check import latest_version, parse_version

_INSTALL_URL = "https://agents.ramp.com/install.sh"


@click.command("update")
def update_cmd() -> None:
    """Update ramp CLI to the latest version."""
    current = __version__

    click.echo(f"Current version: v{current}")
    click.echo("Checking for updates...")

    latest = latest_version()
    if latest is None:
        raise click.ClickException(
            "Could not check for updates. Check your internet connection."
        )

    if parse_version(latest) <= parse_version(current):
        click.echo(f"Already up to date (v{current}).")
        return

    click.echo(f"Update available: v{current} → v{latest}")

    if not shutil.which("curl"):
        raise click.ClickException(
            "curl is required for updates. Install curl and try again."
        )

    click.echo("Installing...")
    run = subprocess.run(["sh", "-c", f"curl -fsSL {_INSTALL_URL} | sh"])
    if run.returncode != 0:
        raise click.ClickException(
            f"Update failed. Try manually:\n  curl -fsSL {_INSTALL_URL} | sh"
        )
