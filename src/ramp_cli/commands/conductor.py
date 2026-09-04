"""Conductor.build path helpers for Ramp Router setup.

Conductor (conductor.build) is a macOS app that runs coding-agent sessions in
parallel git worktrees. It has no model backend of its own: each workspace
spawns the vendored Claude Code and Codex binaries Conductor ships, and its
user settings file can point those launches at replacement executables. The
Router setup written by ``ramp router configure conductor`` uses exactly that
surface — two wrapper executables that route the vendored binaries through
Ramp Router without touching the user's own Claude Code or Codex setup.

Only path resolution and host detection live here; the configure and
unconfigure transactions stay in ``ramp_cli.commands.router`` beside the
other coding agents, which share its private helpers.
"""

import os
import sys
from pathlib import Path

# Conductor reads its user settings from a fixed dotfile directory. The
# override exists for tests, which must never write into a developer's real
# Conductor setup, and mirrors how the other agents' homes are redirected.
CONDUCTOR_HOME_ENV = "CONDUCTOR_HOME"
# Where Conductor's auto-updater keeps the vendored agent binaries it spawns.
# The stable ``bin`` symlinks survive Conductor upgrades, so the wrappers can
# reference them without chasing versioned directories.
APP_SUPPORT_ENV = "RAMP_CONDUCTOR_APP_SUPPORT"
_APP_SUPPORT_DIR = "Library/Application Support/com.conductor.app"
_APP_BUNDLE = Path("/Applications/Conductor.app")
# The advisory lock every Router setup transaction takes, kept beside the
# settings so it derives from nothing but their location. Like the other
# agents' lock files it is never unlinked, so a settings directory holding
# only this file is one the setup created and later emptied — not evidence of
# a Conductor.
LOCK_FILENAME = ".ramp-conductor-settings.lock"


def conductor_home() -> Path:
    """Locate the directory holding Conductor's user settings."""
    return Path(os.environ.get(CONDUCTOR_HOME_ENV, "~/.conductor")).expanduser()


def settings_path() -> Path:
    """Locate Conductor's user settings file."""
    return conductor_home() / "settings.toml"


def artifacts_dir() -> Path:
    """Locate the directory holding every Router artifact written here."""
    return conductor_home() / "ramp-router"


def app_support_dir() -> Path:
    """Locate Conductor's application-support directory."""
    configured = os.environ.get(APP_SUPPORT_ENV)
    if configured:
        return Path(configured).expanduser()
    return Path.home() / _APP_SUPPORT_DIR


def vendored_binary(name: str) -> Path:
    """Locate one of the agent binaries Conductor vendors and spawns.

    Resolved through Conductor's stable ``bin`` symlinks rather than the
    versioned ``agent-binaries`` directories, so a Conductor upgrade does not
    strand the wrappers on a deleted version.
    """
    return app_support_dir() / "bin" / name


def host_is_supported() -> bool:
    """Report whether this host can run Conductor at all.

    Conductor only ships for macOS. A seam rather than an inline platform
    read, so tests exercise both answers on whatever platform CI runs.
    """
    return sys.platform == "darwin"


def is_installed() -> bool:
    """Report whether Conductor appears to be present on this machine.

    Conductor only ships for macOS, so no other platform can host it. Any of
    the app bundle, its application-support state, or its settings directory
    is accepted as evidence: setup only needs the settings directory to
    exist to take effect, and the two app directories cover a Conductor that
    was installed or launched without settings having been written yet.

    The app bundle is consulted only when the application-support override is
    unset: the override exists so tests control detection completely, and a
    developer's real /Applications must not leak through it.
    """
    if not host_is_supported():
        return False
    if app_support_dir().exists() or _home_shows_use(conductor_home()):
        return True
    return _APP_BUNDLE.exists() and APP_SUPPORT_ENV not in os.environ


def _home_shows_use(home: Path) -> bool:
    """Report whether the settings directory holds anything but our lock."""
    try:
        return any(entry.name != LOCK_FILENAME for entry in home.iterdir())
    except OSError:
        return False
