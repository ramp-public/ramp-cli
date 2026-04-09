"""ramp update — self-update to the latest CLI version."""

from __future__ import annotations

import shutil
import subprocess

import click
import httpx

from ramp_cli import __version__

_PUBLIC_REPO = "ramp-public/ramp-cli"
_INSTALL_URL = "https://agents.ramp.com/install.sh"


def _parse_version(v: str) -> tuple[int, ...]:
    """Parse a semver string like '0.1.3' into a comparable tuple."""
    return tuple(int(x) for x in v.split("."))


def _latest_version() -> str | None:
    """Fetch the latest release version from the public GitHub API."""
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(
                f"https://api.github.com/repos/{_PUBLIC_REPO}/releases/latest",
                headers={"Accept": "application/vnd.github+json"},
                follow_redirects=True,
            )
            if resp.status_code == 200:
                tag = resp.json().get("tag_name", "")
                if tag:
                    return tag.lstrip("v")
    except Exception:
        pass

    return None


@click.command("update")
def update_cmd() -> None:
    """Update ramp CLI to the latest version."""
    current = __version__

    click.echo(f"Current version: v{current}")
    click.echo("Checking for updates...")

    latest = _latest_version()
    if latest is None:
        raise click.ClickException(
            "Could not check for updates. Check your internet connection."
        )

    if _parse_version(latest) <= _parse_version(current):
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
