"""ramp update — self-update to the latest CLI version."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import click

from ramp_cli import __version__
from ramp_cli.commands.router import CLIENT_NAMES, configured_router_clients
from ramp_cli.version_check import (
    latest_version,
    parse_version,
    suppress_next_update_notice,
)

# This URL serves ramp/agent-cards-site/public/install.sh, not a file from this
# repository. Every install.oss.sh change must be copied there byte-for-byte
# and deployed from agent-cards-site so fresh installs and self-updates agree.
_INSTALL_URL = "https://agents.ramp.com/install.sh"
_SKILLS_CATALOG_HOST = "api.github.com"
_SKILL_TARGETS = (
    ("Claude", Path(".claude/skills")),
    ("Codex", Path(".codex/skills")),
)


def _install_optional_skills(ramp_executable: Path) -> None:
    for agent, relative_target in _SKILL_TARGETS:
        target = Path.home() / relative_target
        retry_command = f"ramp skills install --all --target ~/{relative_target}"
        try:
            result = subprocess.run(
                _installed_invocation(
                    ramp_executable,
                    "skills",
                    "install",
                    "--all",
                    "--target",
                    str(target),
                )
            )
        except OSError:
            result = None
        if result is not None and result.returncode == 0:
            click.echo(f"{agent} skills installed.")
        else:
            click.echo(
                f"Ramp CLI update succeeded, but optional {agent} skills could not "
                f"be installed. Skills catalog host: {_SKILLS_CATALOG_HOST}. Retry with:\n"
                f"  {retry_command}",
                err=True,
            )


def _installed_executable() -> Path:
    bin_dir = Path(os.environ.get("RAMP_INSTALL_DIR") or Path.home() / ".local/bin")
    return bin_dir.expanduser() / "ramp"


def _installed_invocation(installed: Path, *args: str) -> list[str]:
    command = [str(installed), *args]
    return ["sh", *command] if sys.platform == "win32" else command


def _installed_version(installed: Path) -> str:
    try:
        result = subprocess.run(
            _installed_invocation(installed, "--version"),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    match = re.search(r"\b(\d+\.\d+\.\d+)\b", result.stdout)
    return match.group(1) if result.returncode == 0 and match else "unknown"


def _same_executable(left: Path, right: str | Path) -> bool:
    try:
        return left.samefile(right)
    except OSError:
        return left.absolute() == Path(right).absolute()


def _running_version(resolved: str) -> str:
    invoked = Path(sys.argv[0])
    if invoked.name == "ramp" and invoked.parent == Path("."):
        return __version__
    if any(
        _same_executable(candidate, resolved)
        for candidate in (invoked, Path(sys.executable))
    ):
        return __version__
    return "unknown; not executed"


def _warn_if_shadowed(installed: Path) -> None:
    resolved = shutil.which("ramp")
    if resolved is None:
        return

    if _same_executable(installed, resolved):
        return

    resolved_version = _running_version(resolved)
    installed_version = _installed_version(installed)
    click.secho(
        "WARNING: The updated ramp CLI is shadowed on PATH.",
        fg="yellow",
        bold=True,
    )
    click.echo(f"  Resolved ramp:  {resolved} (v{resolved_version})")
    click.echo(f"  Installed ramp: {installed} (v{installed_version})")
    click.echo("Run the updated CLI directly:")
    direct_command = _installed_invocation(installed, "--version")
    click.echo(f"  {' '.join(shlex.quote(part) for part in direct_command)}")
    click.echo("Use the updated CLI in this shell with:")
    click.echo(f"  export PATH={shlex.quote(str(installed.parent))}:$PATH && hash -r")


def _is_homebrew_install() -> bool:
    """Return True if the running binary lives in the ramp-cli Homebrew Cellar.

    Matches paths like `/opt/homebrew/Cellar/ramp-cli/<ver>/...` (Apple Silicon),
    `/usr/local/Cellar/ramp-cli/<ver>/...` (Intel), and Linuxbrew. Does NOT
    match other formulae's Cellars — e.g. a source/uv install running on a
    Homebrew-managed Python interpreter under `Cellar/python@3.12/...`.
    """
    try:
        exe_parts = Path(sys.executable).resolve().parts
    except OSError:
        return False
    for i, part in enumerate(exe_parts[:-1]):
        if part == "Cellar" and exe_parts[i + 1] == "ramp-cli":
            return True
    return False


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

    router_clients = configured_router_clients()
    if _is_homebrew_install():
        click.echo("This ramp was installed via Homebrew. Run:")
        command = "brew update && brew upgrade ramp-cli"
        if router_clients:
            command += " && ramp router refresh"
        click.echo(f"  {command}")
        return

    if not shutil.which("curl"):
        raise click.ClickException(
            "curl is required for updates. Install curl and try again."
        )

    if router_clients:
        names = ", ".join(CLIENT_NAMES[client] for client in router_clients)
        click.echo(f"Refreshing Router setup for configured agents only: {names}.")

    click.echo("Installing...")
    installed_executable = _installed_executable()
    install_command = (
        'tmp="$(mktemp)" && trap \'rm -f "$tmp"\' 0 && '
        f'curl -fsSL {_INSTALL_URL} -o "$tmp" && sh "$tmp" --no-skills'
    )
    run = subprocess.run(["sh", "-c", install_command])
    if run.returncode != 0:
        raise click.ClickException(f"Update failed. Try manually:\n  {install_command}")
    suppress_next_update_notice()
    click.echo(
        "Ramp CLI update succeeded; the installed binary passed checksum verification."
    )
    _warn_if_shadowed(installed_executable)

    if router_clients:
        # Invoke the CLI after installation instead of teaching the separately
        # hosted installer a new flag. Refresh is receipt-scoped and reads the
        # credentials already stored for each configured agent.
        refresh_command = _installed_invocation(
            installed_executable, "router", "refresh"
        )
        refresh = subprocess.run(refresh_command)
        if refresh.returncode != 0:
            raise click.ClickException(
                "Ramp CLI was updated, but existing Router configurations "
                "could not be refreshed. Run:\n"
                f"  {' '.join(shlex.quote(part) for part in refresh_command)}"
            )
    else:
        click.echo("Installing optional agent skills...")
        _install_optional_skills(installed_executable)
