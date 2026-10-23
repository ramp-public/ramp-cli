import os
import subprocess
import sys
import time

import click


def _run_quiet(command: list[str], *, timeout: float = 15) -> int:
    try:
        return subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        ).returncode
    except (OSError, subprocess.SubprocessError):
        return -1


def restart_running() -> list[str]:
    if sys.platform != "darwin":
        return []
    restarted = []
    # Codex also ships inside ChatGPT. Target only this user's desktop apps,
    # not the lowercase codex CLI processes or another logged-in user's apps.
    for app in ("Codex", "ChatGPT"):
        probe = ["pgrep", "-u", str(os.getuid()), "-x", app]
        status = _run_quiet(probe, timeout=2)
        if status == 1:
            continue
        failure = (
            f"Ramp Router was configured, but {app} could not be restarted. "
            f"Quit and reopen {app} manually to apply the configuration."
        )
        if (
            status != 0
            or _run_quiet(["osascript", "-e", f'tell application "{app}" to quit']) != 0
        ):
            raise click.ClickException(failure)
        deadline = time.monotonic() + 10
        while True:
            status = _run_quiet(probe, timeout=2)
            if status == 1:
                break
            # A quit request can wait for unsaved work; never force-quit.
            if status != 0 or time.monotonic() >= deadline:
                raise click.ClickException(failure)
            time.sleep(0.25)
        if _run_quiet(["open", "-a", app], timeout=10) != 0:
            raise click.ClickException(
                f"Ramp Router was configured, but {app} could not be reopened. "
                f"Open {app} manually to finish setup."
            )
        restarted.append(app)
    return restarted
