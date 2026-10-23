"""The Ramp Router background agent, which keeps Router setups fresh.

Session-start hooks only help while an agent starts sessions with hooks on.
An opt-in login service keeps every Router setup fresh regardless:

- The Ramp Router agent (``ramp-router-agent``, shipped next to ``ramp``) runs
  as one launchd agent / systemd user service. Every few minutes it asks Router
  whether anything changed, using a manifest this module writes, and runs
  `ramp router sync --scheduled` only when it did or a refresh is due. With
  repository tagging on (router_repo), the same process also listens for
  agent requests and adds the repository to them.
- Without the agent binary (a source or wheel install), a plain launchd agent
  or systemd timer runs `ramp router sync --scheduled` every 15 minutes.

Everything here is fail-open: install and uninstall never raise, and
failures go to the router-sync log.

The job's environment lives in an env file the job loads when it starts, not
in the job definition, so a changed environment never needs a reload. The
definition changes only when the ramp executable or config folder moves, and
reloading it never happens while a scheduled refresh is running.
"""

from __future__ import annotations

import contextlib
import json
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ramp_cli.commands import conductor, router_sync
from ramp_cli.commands.router_sync import (
    SYNC_OPT_OUT_ENV,
    log_failure,
    log_path,
    ramp_executable,
    sync_lock,
)
from ramp_cli.config.settings import config_dir

try:
    import fcntl
except ImportError:  # pragma: no cover - no job is installed on Windows
    fcntl = None

LAUNCHD_LABEL = "com.ramp.router-sync"
SYSTEMD_NAME = "ramp-router-sync"
INTERVAL_SECONDS = 15 * 60
AGENT_LABEL = "com.ramp.router"
AGENT_SYSTEMD_NAME = "ramp-router"
AGENT_BINARY = "ramp-router-agent"
# The agent's own check is a few small requests, so it can look more often.
AGENT_INTERVAL_SECONDS = 5 * 60
# A development build of the agent to install instead of the bundled one.
AGENT_PATH_ENV = "RAMP_ROUTER_AGENT"
# The agent and git live in fixed places; its definition never follows the
# installing shell's PATH (the refresh it runs loads the env file instead).
_AGENT_SERVICE_PATH = "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin"
# CA bundles IT points tools at when it inspects TLS. A service does not
# inherit the user's shell, so the agent gets the files seen here as extra
# trust roots (never replacing the system store).
CA_ENV_VARS = (
    "NODE_EXTRA_CA_CERTS",
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
)
EXTRA_CA_ENV = "RAMP_ROUTER_EXTRA_CA_FILES"
# Set by background runs (the scheduled job and the session hook's detached
# refresh), which never reload the job or undo an opt-out.
SCHEDULED_ENV = "RAMP_ROUTER_SCHEDULED_RUN"
# Conductor's Codex wrapper parks the user's own CODEX_HOME here before pointing
# CODEX_HOME at its isolated home (router.CONDUCTOR_ORIGINAL_CODEX_HOME_ENV).
_CONDUCTOR_ORIGINAL_CODEX_HOME_ENV = "RAMP_CONDUCTOR_ORIGINAL_CODEX_HOME"
FULL_REFRESH_SECONDS = 24 * 60 * 60
_FALLBACK_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
# Retry a failed scheduled refresh after an hour rather than on every tick,
# doubling while it keeps failing, up to FULL_REFRESH_SECONDS.
RETRY_SECONDS = 60 * 60
# How long removing the job waits for a running refresh to finish writing.
# Past it, the opt-out marker stops later ticks and the next install retries.
REMOVE_WAIT_SECONDS = 120


# Where the agents' configs live. A scheduled refresh must write the same
# files an interactive one would, so the installing shell's values are kept.
# Router endpoint overrides are deliberately absent: a one-off override on a
# single refresh must not pin every later background run to it.
_LOCATION_ENVS = (
    "XDG_CONFIG_HOME",
    "CLAUDE_CONFIG_DIR",
    "CODEX_HOME",
    "OPENCODE_CONFIG",
    "OPENCODE_TUI_CONFIG",
    "PI_CODING_AGENT_DIR",
    "HERMES_HOME",
    "RAMP_OPENCODE_MAJOR",
)
# The location variables above that hold paths. The job runs from a different
# working directory, so relative values are saved as absolute paths.
_PATH_LOCATION_ENVS = frozenset(_LOCATION_ENVS) - {"RAMP_OPENCODE_MAJOR"}
# Preferences a background run must honor the way the user's shell would.
_PREFERENCE_ENVS = ("RAMP_NO_UPDATE_CHECK", AGENT_PATH_ENV)


def _job_environment() -> dict[str, str]:
    """The installing shell's PATH, config locations, and preferences for the job.

    PATH lets refresh find the same agent binaries. Inside the scheduled run
    these are the values it loaded from the env file, so rewriting it there
    changes nothing.
    """
    source = dict(os.environ)
    parked = source.get(_CONDUCTOR_ORIGINAL_CODEX_HOME_ENV)
    if parked:
        # Run from inside Conductor's Codex: keep the user's own Codex home.
        source["CODEX_HOME"] = parked
    elif parked is not None and _is_conductor_codex_home(source.get("CODEX_HOME")):
        # Parked empty means the user had the default ~/.codex. A CODEX_HOME
        # Conductor's sync hook already restored is the user's and is kept.
        source.pop("CODEX_HOME", None)
    environment = {"PATH": source.get("PATH") or _FALLBACK_PATH}
    for name in (*_LOCATION_ENVS, *_PREFERENCE_ENVS, *CA_ENV_VARS):
        value = source.get(name)
        if name == "HERMES_HOME" and value:
            # The only reader that trims its value (hermes_agent.py).
            value = value.strip()
        if not value:
            continue
        if name in CA_ENV_VARS or name == AGENT_PATH_ENV:
            value = str(Path(value).expanduser().absolute())
        elif name in _PATH_LOCATION_ENVS:
            # Readers expand ~ themselves. Relative values are joined, not
            # normalized: collapsing ".." past a symlink changes the target.
            if not value.startswith("~") and not os.path.isabs(value):
                value = os.path.join(os.getcwd(), value)
        environment[name] = value
    return environment


def _is_conductor_codex_home(value: str | None) -> bool:
    if not value:
        return False
    # router's conductor artifacts dir and CONDUCTOR_CODEX_HOME; router imports
    # this module, so the layout is restated rather than imported.
    isolated = conductor.settings_path().parent / "ramp-router" / "codex-home"
    return Path(value).expanduser().resolve() == isolated.resolve()


def env_file_path() -> Path:
    return config_dir() / "router-sync-env.json"


def load_job_environment(path: str) -> None:
    """Apply the env file a scheduled run was started with; never raises."""
    try:
        environment = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log_failure(f"could not read the scheduled sync environment: {exc!r}")
        return
    if isinstance(environment, dict):
        os.environ.update({str(k): str(v) for k, v in environment.items()})


def _opt_out_marker() -> Path:
    # Beside the job's locks, not in config_dir(): an interactive opt-out and
    # the running job may resolve different XDG_CONFIG_HOMEs, and both must
    # find the same marker.
    return router_sync.job_lock_dir() / "router-sync-opted-out"


def opted_out() -> bool:
    """Whether an interactive run opted out since this job was installed."""
    return _opt_out_marker().exists()


def _enabled_marker() -> Path:
    # A per-user preference, independent of the invoking shell's config root.
    return router_sync.job_lock_dir() / "router-background-enabled"


def background_enabled() -> bool:
    """Off until explicitly enabled; refresh/update never change this choice."""
    return _enabled_marker().exists() and not opted_out()


def enable_background() -> None:
    """Persist explicit consent, then install/repair the service."""
    with _schedule_lock():
        marker = _enabled_marker()
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
        _opt_out_marker().unlink(missing_ok=True)
    install()


def disable_background() -> None:
    """Persist the opt-out and safely remove the service, preserving setups."""
    _remove(opt_out=True)


def background_status() -> dict:
    """Report the saved preference separately from actual service state."""
    service, state = "none", AGENT_NOT_INSTALLED
    if agent_installed():
        service, state = "agent", agent_state()
    elif _job_installed():
        service = "timer"
        running = (
            _run("launchctl", "print", f"gui/{os.getuid()}/{LAUNCHD_LABEL}")
            if sys.platform == "darwin"
            else _run("systemctl", "--user", "is-active", f"{SYSTEMD_NAME}.timer")
        )
        state = "scheduled" if running else AGENT_NOT_LOADED
    return {
        "enabled": background_enabled(),
        "service": service,
        "state": state,
        "environment_opt_out": bool(os.environ.get(SYNC_OPT_OUT_ENV)),
    }


def _state_file() -> Path:
    return config_dir() / "router-sync.json"


def load_state() -> dict:
    """Applied versions keyed by a digest of (base_url, key), plus the last refresh."""
    try:
        state = json.loads(_state_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def save_state(state: dict) -> None:
    path = _state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(state), encoding="utf-8")
    os.replace(temp, path)


def full_refresh_due(state: dict) -> bool:
    last = state.get("last_full_refresh")
    if not isinstance(last, int | float):
        return True
    return time.time() - last >= FULL_REFRESH_SECONDS


def retry_pending(state: dict) -> bool:
    """Whether a recent failed refresh is still backing off."""
    retry_after = state.get("retry_after")
    return isinstance(retry_after, int | float) and time.time() < retry_after


def retry_due(state: dict) -> bool:
    """Whether a failed refresh has backed off long enough to try again."""
    retry_after = state.get("retry_after")
    return isinstance(retry_after, int | float) and time.time() >= retry_after


def retry_after_failure(state: dict, now: float) -> dict:
    """The retry fields after a failed refresh: an hour, doubling to a day.

    ``state`` carries ``retry_delay`` only while earlier refreshes keep
    failing; a successful refresh drops both fields.
    """
    previous = state.get("retry_delay")
    if isinstance(previous, int | float) and previous > 0:
        delay = min(previous * 2, FULL_REFRESH_SECONDS)
    else:
        delay = RETRY_SECONDS
    return {"retry_after": now + delay, "retry_delay": delay}


# ---------------------------------------------------------------------------
# The agent
# ---------------------------------------------------------------------------


def agent_executable(ramp: Path | None = None) -> Path | None:
    """The agent binary shipped beside ``ramp``, or None for this install."""
    # The env file keeps a development build for runs that start without the
    # installing shell's environment (the agent's refresh, session hooks).
    override = os.environ.get(AGENT_PATH_ENV) or _job_env_value(AGENT_PATH_ENV)
    if override:
        candidate: Path | None = Path(override).expanduser()
    else:
        ramp = ramp or ramp_executable()
        # Beside the real file: installers link `ramp` into a bin directory.
        candidate = Path(os.path.realpath(ramp)).parent / AGENT_BINARY if ramp else None
    if candidate and candidate.is_file() and os.access(candidate, os.X_OK):
        return _homebrew_opt_path(Path(os.path.abspath(candidate)))
    return None


def _job_env_value(name: str) -> str | None:
    try:
        environment = json.loads(env_file_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = environment.get(name) if isinstance(environment, dict) else None
    return value if isinstance(value, str) and value else None


def _homebrew_opt_path(path: Path) -> Path:
    """``<prefix>/opt/<formula>/...`` for a file in a versioned Homebrew Cellar.

    ``brew upgrade`` deletes the old Cellar version, so a service pointing
    into it fails to start after the next login. The opt link follows upgrades.
    """
    parts = path.parts
    for index in range(len(parts) - 2):
        if parts[index] == "Cellar":
            stable = Path(*parts[:index], "opt", parts[index + 1], *parts[index + 3 :])
            if os.path.realpath(stable) == os.path.realpath(path):
                return stable
            break
    return path


def _service_manager() -> bool:
    if sys.platform == "darwin":
        return True
    return sys.platform.startswith("linux") and shutil.which("systemctl") is not None


def agent_supported() -> bool:
    return _service_manager() and agent_executable() is not None


def manifest_path() -> Path:
    """What the agent checks, written after each scheduled sync (mode 0600)."""
    return config_dir() / "router-agent-sync.json"


def agent_log_path() -> Path:
    """The agent's own output (the refreshes it runs log to router-sync.log)."""
    return config_dir() / "router-agent.log"


def _agent_settings_path() -> Path:
    return config_dir() / "router-agent.json"


def write_manifest(manifest: dict) -> None:
    """Replace the agent's manifest; it holds Router keys, so it is 0600."""
    path = manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle)
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def clear_manifest() -> None:
    """Make the agent run a sync soon; that sync writes a fresh manifest."""
    manifest_path().unlink(missing_ok=True)


def ca_files() -> list[str]:
    """CA bundles the agent trusts: those seen before plus any set right now.

    ``ramp`` runs from shells, harness hooks, and GUI apps, not all of which
    carry the variables, so a file is kept once seen until it is gone.
    """
    try:
        settings = json.loads(_agent_settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        settings = {}
    known = settings.get("ca_files") if isinstance(settings, dict) else None
    seen = [f for f in known if isinstance(f, str)] if isinstance(known, list) else []
    for name in CA_ENV_VARS:
        value = os.environ.get(name, "").strip()
        if value:
            seen.append(os.path.abspath(os.path.expanduser(value)))
    files = [f for f in dict.fromkeys(seen) if os.path.isfile(f)]
    if files != known:
        with contextlib.suppress(OSError):
            path = _agent_settings_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"ca_files": files}) + "\n", encoding="utf-8")
    return files


def _listener_port() -> int | None:
    # router_repo imports this module.
    from ramp_cli.commands import router_repo  # noqa: PLC0415

    return router_repo.listener_port()


def agent_build(agent: Path) -> str:
    """Identifies the agent binary on disk, so an updated one changes the service.

    Updates replace the binary in place at the same path. Without this the
    definition stays identical, install() never reloads it, and the old agent
    keeps running from its deleted file until the next login.
    """
    try:
        st = agent.stat()
    except OSError:
        return "unknown"
    return f"{st.st_size}-{st.st_mtime_ns}"


def _agent_arguments(agent: Path, ramp: Path, listen: int | None) -> list[str]:
    arguments = [
        str(agent),
        "serve",
        "--build",
        agent_build(agent),
        "--sync-ramp",
        str(ramp),
        "--sync-env-file",
        str(env_file_path().absolute()),
        "--sync-manifest",
        str(manifest_path().absolute()),
        "--sync-opt-out",
        str(_opt_out_marker().absolute()),
        "--sync-interval",
        str(AGENT_INTERVAL_SECONDS),
        "--sync-log",
        str(log_path().absolute()),
    ]
    if listen is not None:
        from ramp_cli.commands import router_repo  # noqa: PLC0415

        if router_repo.health_token(create=True) is None:
            raise OSError("could not create the agent's private health key")
        arguments += [
            "--port",
            str(listen),
            "--settings",
            str(router_repo.settings_path().absolute()),
            "--log",
            str(router_repo.log_path().absolute()),
        ]
    return arguments


def _agent_environment() -> dict[str, str]:
    # The refresh loads the user's update-check preference from the env file.
    environment = {"PATH": _AGENT_SERVICE_PATH}
    files = ca_files()
    if files:
        environment[EXTRA_CA_ENV] = os.pathsep.join(files)
    return environment


def render_agent_plist(agent: Path, ramp: Path, listen: int | None) -> bytes:
    """One launchd agent. Background priority unless it serves requests."""
    plist: dict = {
        "Label": AGENT_LABEL,
        "ProgramArguments": _agent_arguments(agent, ramp, listen),
        "RunAtLoad": True,
        # Agents cannot reach Router while the listener is down, and a sync
        # schedule that stopped would go unnoticed: launchd restarts it.
        "KeepAlive": True,
        "ThrottleInterval": 5,
        # A refresh the agent started finishes even if the agent restarts.
        "AbandonProcessGroup": True,
        "EnvironmentVariables": _agent_environment(),
        "StandardOutPath": str(agent_log_path().absolute()),
        "StandardErrorPath": str(agent_log_path().absolute()),
    }
    if listen is None:
        plist["ProcessType"] = "Background"
        plist["LowPriorityIO"] = True
    else:
        # launchd holds the socket, so a restart never refuses a connection.
        plist["Sockets"] = {
            "Listeners": {
                "SockNodeName": "127.0.0.1",
                "SockServiceName": str(listen),
                "SockType": "stream",
                "SockFamily": "IPv4",
            }
        }
    return plistlib.dumps(plist)


def _systemd_quote(value: str) -> str:
    return value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')


def render_agent_systemd(
    agent: Path, ramp: Path, listen: int | None
) -> tuple[str, str | None]:
    """The service unit, and a socket unit while it serves requests."""
    arguments = " ".join(
        f'"{_systemd_quote(argument).replace("$", "$$")}"'
        for argument in _agent_arguments(agent, ramp, listen)
    )
    environment = "".join(
        f'Environment="{name}={_systemd_quote(value)}"\n'
        for name, value in _agent_environment().items()
        if "\n" not in value
    )
    log = _systemd_quote(str(agent_log_path().absolute()))
    priority = "Nice=10\nIOSchedulingClass=idle\n" if listen is None else ""
    service = (
        "[Unit]\nDescription=Ramp Router background agent\n\n"
        "[Service]\nType=simple\n"
        f"ExecStart={arguments}\n{environment}{priority}"
        # A refresh the agent started finishes even if the agent restarts.
        "KillMode=process\nRestart=always\nRestartSec=2\n"
        f"StandardOutput=append:{log}\nStandardError=append:{log}\n\n"
        "[Install]\nWantedBy=default.target\n"
    )
    if listen is None:
        return service, None
    socket = (
        "[Unit]\nDescription=Ramp Router background agent listener\n\n"
        f"[Socket]\nListenStream=127.0.0.1:{listen}\nBacklog=1024\n\n"
        "[Install]\nWantedBy=sockets.target\n"
    )
    return service, socket


def _agent_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{AGENT_LABEL}.plist"


# What agent_state() reports.
AGENT_RUNNING = "running"
AGENT_DISABLED = "disabled"  # turned off in Login Items (or launchctl disable)
AGENT_NOT_LOADED = "not_loaded"  # installed, but launchd/systemd is not running it
AGENT_NOT_INSTALLED = "not_installed"
LOGIN_ITEMS_HINT = (
    "turn on ramp-router-agent in System Settings > General > Login Items & Extensions"
)


def _launchctl_output(*args: str) -> str:
    """launchctl's stdout ("" if it could not run)."""
    try:
        result = subprocess.run(
            ["launchctl", *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout


def _launchd_disabled(label: str) -> bool:
    """Whether launchd's disabled list holds the label (the Login Items toggle)."""
    for line in _launchctl_output("print-disabled", f"gui/{os.getuid()}").splitlines():
        name, _, value = line.strip().partition("=>")
        if name.strip().strip('"') == label:
            return value.strip() in ("disabled", "true")
    return False


def _launchd_running(target: str) -> bool:
    """Whether launchd has the job running, not merely loaded.

    A loaded job may never have been spawned, or its binary may be missing;
    only a running one has a "pid = " line.
    """
    return any(
        line.strip().startswith("pid = ")
        for line in _launchctl_output("print", target).splitlines()
    )


def agent_state() -> str:
    """Whether the agent this install set up is actually running."""
    if not agent_installed():
        return AGENT_NOT_INSTALLED
    if sys.platform == "darwin":
        if _launchd_disabled(AGENT_LABEL):
            return AGENT_DISABLED
        running = _launchd_running(f"gui/{os.getuid()}/{AGENT_LABEL}")
        return AGENT_RUNNING if running else AGENT_NOT_LOADED
    name = f"{AGENT_SYSTEMD_NAME}.service"
    if _run("systemctl", "--user", "is-active", name):
        return AGENT_RUNNING
    return AGENT_NOT_LOADED


def agent_installed() -> bool:
    if sys.platform == "darwin":
        return _agent_plist_path().exists()
    return (_systemd_dir() / f"{AGENT_SYSTEMD_NAME}.service").exists()


def _install_agent(agent: Path, ramp: Path, listen: int | None) -> None:
    if sys.platform == "darwin":
        _install_agent_launchd(render_agent_plist(agent, ramp, listen))
    elif sys.platform.startswith("linux"):
        _install_agent_systemd(*render_agent_systemd(agent, ramp, listen))


def _install_agent_launchd(content: bytes) -> None:
    plist = _agent_plist_path()
    domain = f"gui/{os.getuid()}"
    target = f"{domain}/{AGENT_LABEL}"
    loaded = _run("launchctl", "print", target)
    try:
        unchanged = plist.read_bytes() == content
    except OSError:
        unchanged = False
    if loaded and unchanged:
        if not _launchd_running(target):
            _run("launchctl", "kickstart", target)  # see below
        return
    # A refresh the agent started runs in its own session and survives the
    # reload, so the agent can be replaced at any time.
    _write_if_changed(plist, content)
    if loaded:
        _run("launchctl", "bootout", target)
    if _launchd_disabled(AGENT_LABEL):
        # The user turned it off in Login Items: loading would fail, and only
        # the user (or an MDM login-items profile) can turn it back on.
        log_failure(f"the Ramp Router agent is off; {LOGIN_ITEMS_HINT}")
        return
    if not _run("launchctl", "bootstrap", domain, str(plist)):
        log_failure("could not load the Ramp Router agent with launchctl")
        return
    # RunAtLoad and KeepAlive spawns wait while the domain is in launchd's
    # on-demand-only mode; a demand spawn starts the agent regardless.
    _run("launchctl", "kickstart", target)


def _install_agent_systemd(service: str, socket: str | None = None) -> None:
    if not _run("systemctl", "--user", "show-environment"):
        return  # no user bus (container, ssh without a session): skip quietly
    unit_dir = _systemd_dir()
    name = f"{AGENT_SYSTEMD_NAME}.service"
    _remember_systemd_dir(unit_dir)
    socket_name = f"{AGENT_SYSTEMD_NAME}.socket"
    changed = _write_if_changed(unit_dir / name, service.encode())
    socket_changed = False
    if socket is None:
        if (unit_dir / socket_name).exists():
            _run("systemctl", "--user", "disable", "--now", socket_name)
            (unit_dir / socket_name).unlink(missing_ok=True)
            changed = True
    else:
        socket_changed = _write_if_changed(unit_dir / socket_name, socket.encode())
        changed = socket_changed or changed
    units = [unit_dir / name]
    if socket is not None:
        units.append(unit_dir / socket_name)
    if not _ensure_systemd_loadable(units):
        return
    if (
        not changed
        and _run("systemctl", "--user", "is-active", name)
        and _run("systemctl", "--user", "is-enabled", name)
        and (
            socket is None
            or (
                _run("systemctl", "--user", "is-active", socket_name)
                and _run("systemctl", "--user", "is-enabled", socket_name)
            )
        )
    ):
        return
    ok = _run("systemctl", "--user", "daemon-reload")
    if ok and socket_changed:
        # The running service holds a copy of the old socket; release it before
        # rebinding the new ListenStream, then start it with the replacement.
        ok = _run("systemctl", "--user", "stop", name)
        ok = ok and _run("systemctl", "--user", "restart", socket_name)
    if ok and socket is not None:
        # Started first, so the service receives the listening socket.
        ok = _run("systemctl", "--user", "enable", "--now", socket_name)
    ok = ok and _run("systemctl", "--user", "enable", name)
    ok = ok and _run("systemctl", "--user", "restart", name)
    if not ok:
        log_failure("could not start the Ramp Router agent with systemctl")


def _uninstall_agent() -> None:
    if sys.platform == "darwin":
        plist = _agent_plist_path()
        if plist.exists():
            plist.unlink()
            _run("launchctl", "bootout", f"gui/{os.getuid()}/{AGENT_LABEL}")
    elif sys.platform.startswith("linux"):
        unit_dir = _systemd_dir()
        names = (f"{AGENT_SYSTEMD_NAME}.socket", f"{AGENT_SYSTEMD_NAME}.service")
        systemctl = shutil.which("systemctl") is not None
        for name in names:
            if systemctl and (unit_dir / name).exists():
                _run("systemctl", "--user", "disable", "--now", name)
            (unit_dir / name).unlink(missing_ok=True)
        if systemctl:
            _run("systemctl", "--user", "daemon-reload")
    clear_manifest()


def update_agent() -> None:
    """Re-render the service after a repository-tagging change; never raises.

    Unlike install(), this never opts back into background sync: an opted-out
    user who enables tagging gets an agent that only serves requests.
    """
    try:
        with _schedule_lock():
            if os.environ.get(SYNC_OPT_OUT_ENV) or not background_enabled():
                _opt_out_marker().parent.mkdir(parents=True, exist_ok=True)
                _opt_out_marker().touch()
            if opted_out() and _listener_port() is None:
                _uninstall_agent()
                return
            _install_service()
    except Exception as exc:
        log_failure(f"Ramp Router agent update failed: {exc!r}")


def _install_service() -> None:
    """The agent when this install has it, else the plain scheduled job."""
    ramp = ramp_executable()
    agent = agent_executable(ramp)
    if ramp is None or not _service_manager():
        return
    if agent is None:
        if agent_installed():
            _uninstall_agent()
        if not background_enabled():
            return  # a missing listener binary must not turn on timer-based sync
        if sys.platform == "darwin":
            _install_launchd()
        else:
            _install_systemd()
        return
    if _job_installed():
        if os.environ.get(SCHEDULED_ENV):
            # This may be the fallback job's own refresh. On launchd, bootout
            # kills it mid-write; let the next interactive install migrate it.
            return
        with sync_lock() as free:
            if not free:
                return
            _install_agent(agent, ramp, _listener_port())
            if agent_state() == AGENT_RUNNING:
                _uninstall_job()
        return
    _install_agent(agent, ramp, _listener_port())


# ---------------------------------------------------------------------------
# The plain scheduled job (installs without the agent)
# ---------------------------------------------------------------------------


def _job_arguments(env_file: Path) -> list[str]:
    return ["router", "sync", "--scheduled", "--env-file", str(env_file.absolute())]


def render_plist(executable: Path, log: Path, env_file: Path) -> bytes:
    return plistlib.dumps(
        {
            "Label": LAUNCHD_LABEL,
            "ProgramArguments": [str(executable.absolute()), *_job_arguments(env_file)],
            "StartInterval": INTERVAL_SECONDS,
            "RunAtLoad": True,
            "ProcessType": "Background",
            "LowPriorityIO": True,
            "StandardOutPath": str(log.absolute()),
            "StandardErrorPath": str(log.absolute()),
        }
    )


def render_systemd(executable: Path, log: Path, env_file: Path) -> tuple[str, str]:
    arguments = " ".join(
        f'"{_systemd_quote(argument).replace("$", "$$")}"'
        for argument in [str(executable.absolute()), *_job_arguments(env_file)]
    )
    log_path = _systemd_quote(str(log.absolute()))
    service = (
        "[Unit]\nDescription=Keep Ramp Router setups fresh\n\n"
        "[Service]\nType=oneshot\n"
        f"ExecStart={arguments}\n"
        f"StandardOutput=append:{log_path}\nStandardError=append:{log_path}\n"
    )
    timer = (
        "[Unit]\nDescription=Keep Ramp Router setups fresh\n\n"
        f"[Timer]\nOnBootSec=5min\nOnUnitActiveSec={INTERVAL_SECONDS // 60}min\n"
        "Persistent=true\n\n[Install]\nWantedBy=timers.target\n"
    )
    return service, timer


def _run(*command: str) -> bool:
    try:
        environment = None
        if command[:2] == ("systemctl", "--user"):
            # The client's config root cannot change the running manager's
            # UnitPath. Put links/enablement files in a directory it does search.
            control_dir = _systemd_control_dir()
            if control_dir is None and command[2] in ("link", "enable", "restart"):
                return False  # do not install into an unverified search path
            environment = {
                **os.environ,
                "XDG_CONFIG_HOME": str((control_dir or _systemd_dir()).parent.parent),
            }
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=30,
            check=False,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _launchd_paths() -> tuple[Path, str]:
    plist = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
    return plist, f"gui/{os.getuid()}"


def _systemd_dir_record() -> Path:
    return router_sync.job_lock_dir() / "router-systemd-dir"


def _remember_systemd_dir(path: Path) -> None:
    # Keep the location across disable/re-enable, login, and config-root changes.
    _write_if_changed(_systemd_dir_record(), f"{path.absolute()}\n".encode())


def _systemd_manager_paths() -> tuple[Path, ...]:
    """Read the running manager's search path, not the caller's environment."""
    try:
        result = subprocess.run(
            ["systemctl", "--user", "show", "--property=UnitPath", "--value"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode == 0:
            # systemctl shell-quotes string-array values, including spaces.
            return tuple(
                Path(p) for p in shlex.split(result.stdout) if Path(p).is_absolute()
            )
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return ()


def _systemd_control_dir(paths: tuple[Path, ...] | None = None) -> Path | None:
    """A persistent, user-owned directory in the manager's current UnitPath."""
    for path in _systemd_manager_paths() if paths is None else paths:
        if path.parts[-2:] != ("systemd", "user"):
            continue  # generators, transient/control units and other special dirs
        try:
            resolved = path.resolve()
            if any(resolved.is_relative_to(root) for root in ("/etc", "/usr", "/run")):
                continue  # never write system-wide or runtime unit directories
            parent = path
            while not parent.exists() and parent != parent.parent:
                parent = parent.parent
            if parent.stat().st_uid == os.getuid() and os.access(parent, os.W_OK):
                return path
        except (OSError, RuntimeError):
            continue
    return None


def _ensure_systemd_loadable(units: list[Path]) -> bool:
    """Link stable storage into a new login's search path before enabling it."""
    paths = _systemd_manager_paths()
    if not paths or _systemd_control_dir(paths) is None:
        log_failure("could not find a writable user unit directory in systemd UnitPath")
        return False
    search_paths = {path.resolve() for path in paths}
    outside = [
        str(unit.absolute())
        for unit in units
        if unit.parent.resolve() not in search_paths
    ]
    if outside and not _run("systemctl", "--user", "link", *outside):
        log_failure("could not link Router units into systemd's current UnitPath")
        return False
    return True


def _systemd_dir() -> Path:
    """Locate unit storage; the current manager may need links to load it."""
    try:
        saved = Path(_systemd_dir_record().read_text().rstrip("\n"))
        if saved.is_absolute() and saved.parts[-2:] == ("systemd", "user"):
            return saved
    except (OSError, ValueError):
        pass
    # Older builds have no location record. Ask the user manager where it loaded
    # our units rather than searching arbitrary directories on the filesystem.
    names = (
        f"{AGENT_SYSTEMD_NAME}.service",
        f"{AGENT_SYSTEMD_NAME}.socket",
        f"{SYSTEMD_NAME}.timer",
        f"{SYSTEMD_NAME}.service",
    )
    if sys.platform.startswith("linux") and shutil.which("systemctl") is not None:
        try:
            result = subprocess.run(
                [
                    "systemctl",
                    "--user",
                    "show",
                    "--property=FragmentPath",
                    "--value",
                    *names,
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            # A missing sibling unit can make show exit nonzero; keep valid
            # paths reported for the units that do exist.
            for line in result.stdout.splitlines():
                path = Path(line)
                if (
                    path.is_absolute()
                    and path.name in names
                    and path.parent.parts[-2:] == ("systemd", "user")
                    and path.is_file()
                ):
                    return path.parent
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    # Only a first install chooses its unit directory from the current shell.
    config_home = os.environ.get("XDG_CONFIG_HOME")
    base = Path(config_home) if config_home else Path.home() / ".config"
    return (base / "systemd" / "user").absolute()


def _write_if_changed(path: Path, content: bytes) -> bool:
    try:
        if path.read_bytes() == content:
            return False
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return True


def _install_launchd() -> None:
    executable = ramp_executable()
    if executable is None:
        return
    plist, domain = _launchd_paths()
    target = f"{domain}/{LAUNCHD_LABEL}"
    content = render_plist(executable, log_path(), env_file_path())
    loaded = _run("launchctl", "print", target)
    try:
        unchanged = plist.read_bytes() == content
    except OSError:
        unchanged = False
    if loaded and unchanged:
        return
    if not loaded:
        _write_if_changed(plist, content)
        _bootstrap(domain, plist)
        return
    if os.environ.get(SCHEDULED_ENV):
        # Reloading would kill this very run; a later install reloads it.
        return
    # A scheduled refresh holds the sync lock; booting the job out would kill
    # it mid-write. The definition is written only together with its reload,
    # so a skipped reload is retried by the next install.
    with sync_lock() as free:
        if not free:
            return
        _write_if_changed(plist, content)
        _run("launchctl", "bootout", target)
        _bootstrap(domain, plist)


def _bootstrap(domain: str, plist: Path) -> None:
    if not _run("launchctl", "bootstrap", domain, str(plist)):
        log_failure("could not load the scheduled router sync with launchctl")


def _install_systemd() -> None:
    executable = ramp_executable()
    if executable is None or shutil.which("systemctl") is None:
        return
    if not _run("systemctl", "--user", "show-environment"):
        return  # no user bus (container, ssh without a session): skip quietly
    service, timer = render_systemd(executable, log_path(), env_file_path())
    unit_dir = _systemd_dir()
    _remember_systemd_dir(unit_dir)
    changed = _write_if_changed(unit_dir / f"{SYSTEMD_NAME}.service", service.encode())
    changed = (
        _write_if_changed(unit_dir / f"{SYSTEMD_NAME}.timer", timer.encode()) or changed
    )
    if not _ensure_systemd_loadable(
        [unit_dir / f"{SYSTEMD_NAME}.service", unit_dir / f"{SYSTEMD_NAME}.timer"]
    ):
        return
    timer_name = f"{SYSTEMD_NAME}.timer"
    if (
        not changed
        and _run("systemctl", "--user", "is-enabled", timer_name)
        and _run("systemctl", "--user", "is-active", timer_name)
    ):
        return
    ok = _run("systemctl", "--user", "daemon-reload") and _run(
        "systemctl", "--user", "enable", "--now", timer_name
    )
    if ok and changed:
        # A restarted timer leaves a running service untouched.
        ok = _run("systemctl", "--user", "restart", timer_name)
    if not ok:
        log_failure("could not enable the scheduled router sync with systemctl")


@contextmanager
def _schedule_lock() -> Iterator[None]:
    """Serialize installing and removing the job.

    A background run can pass the opt-out check and then spend seconds in
    systemctl or launchctl; without this, an opt-out landing in between is
    undone when that run finishes enabling the job. Short-held, unlike the sync
    lock a whole refresh holds. Always taken after the sync lock, never before
    a blocking wait for it, so the two cannot deadlock.
    """
    path = router_sync.job_lock_dir() / "router-schedule.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as lock_file:
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def install(*, preserve_opt_out: bool = False) -> None:
    """Reconcile the saved preference without opting anyone in; never raises."""
    try:
        if os.environ.get(SYNC_OPT_OUT_ENV):
            # The opt-out lives in the user's shell, which background runs
            # never see; the marker keeps one already running from
            # reinstalling, and stops the job's own later ticks.
            _remove(opt_out=True)
            return
        if not background_enabled():
            if agent_installed() or _job_installed() or _listener_port() is not None:
                # Also removes services left by an earlier, default-on build.
                _remove(opt_out=True, only_if_disabled=True)
            return
        with _schedule_lock():
            if not background_enabled():
                return  # a concurrent disable won before we took the lock
            # A background run's environment is the env file it loaded at
            # start, which an interactive configure may have replaced since; it
            # only writes a missing file (a job first installed by the hook).
            if not os.environ.get(SCHEDULED_ENV) or not env_file_path().exists():
                _write_if_changed(
                    env_file_path(),
                    (json.dumps(_job_environment(), indent=2) + "\n").encode(),
                )
            if not os.environ.get(SCHEDULED_ENV):
                # A setup may have changed what the agent should check; the
                # scheduled sync this triggers writes a fresh manifest.
                clear_manifest()
            _install_service()
    except Exception as exc:
        log_failure(f"scheduled router sync install failed: {exc!r}")


def uninstall() -> None:
    """Remove the agent or scheduled job: no Router setup remains; never raises."""
    try:
        _remove(opt_out=False)
    except Exception as exc:
        log_failure(f"scheduled router sync uninstall failed: {exc!r}")


def remove_after_opt_out() -> None:
    """The job's own tick found the opt-out marker: remove the job; never raises.

    The agent never runs a tick while the marker exists; with tagging on it
    keeps serving requests.

    Called before the tick takes the sync lock or writes anything, so the
    bootout ending this very process interrupts nothing.
    """
    try:
        with _schedule_lock():
            if not background_enabled():
                _uninstall(keep_listener=True)
    except Exception as exc:
        log_failure(f"scheduled router sync removal failed: {exc!r}")


def _remove(*, opt_out: bool, only_if_disabled: bool = False) -> None:
    """Remove the job once no refresh is writing.

    Booting out the launchd job kills a running scheduled refresh, which could
    stop it between two writes of a multi-file update. An interactive caller
    waits for the sync lock first. A background caller does not: it may be the
    refresh holding that lock. Either way, if a refresh is still running, the
    marker stops every later tick and the job's next tick removes itself.
    """
    wait = 0 if os.environ.get(SCHEDULED_ENV) else REMOVE_WAIT_SECONDS
    with sync_lock(wait=wait) as free, _schedule_lock():
        if only_if_disabled and background_enabled():
            return  # explicit enable won while we waited for the refresh
        marker = _opt_out_marker()
        if opt_out:
            _enabled_marker().unlink(missing_ok=True)
        if opt_out or not free:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()
        if not free:
            log_failure(
                "a scheduled refresh is still running; its next tick removes the job"
            )
            return
        _uninstall(keep_listener=opt_out)


def _uninstall(*, keep_listener: bool = False) -> None:
    """Remove background sync, and the agent unless it must keep serving requests."""
    _uninstall_job()
    if keep_listener and _listener_port() is not None:
        # The marker stops the agent's checks; its listener stays.
        clear_manifest()
        _install_service()
    else:
        _uninstall_agent()
    env_file_path().unlink(missing_ok=True)


def _job_installed() -> bool:
    if sys.platform == "darwin":
        return _launchd_paths()[0].exists()
    return (_systemd_dir() / f"{SYSTEMD_NAME}.timer").exists()


def _uninstall_job() -> None:
    if sys.platform == "darwin":
        plist, domain = _launchd_paths()
        # Files first: bootout ends a job calling this from its own tick.
        plist.unlink(missing_ok=True)
        _run("launchctl", "bootout", f"{domain}/{LAUNCHD_LABEL}")
        return
    if sys.platform.startswith("linux"):
        unit_dir = _systemd_dir()
        names = (f"{SYSTEMD_NAME}.timer", f"{SYSTEMD_NAME}.service")
        systemctl = shutil.which("systemctl") is not None
        if systemctl:
            if (unit_dir / names[0]).exists():
                # Stops the timer only; a running service finishes its refresh.
                _run("systemctl", "--user", "disable", "--now", names[0])
            if (unit_dir / names[1]).exists():
                # Remove a linked service too, without stopping a running refresh.
                _run("systemctl", "--user", "disable", names[1])
        for name in names:
            (unit_dir / name).unlink(missing_ok=True)
        if systemctl:
            _run("systemctl", "--user", "daemon-reload")
