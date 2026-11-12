"""Opt-in repository attribution for local Router setups.

``ramp router agent enable`` turns on the listener of the Ramp Router
background agent (``ramp-router-agent``, installed by ``router_schedule``) and
refreshes the Router setups so Codex, Claude Code, OpenCode, and Pi send their
requests through it. The agent adds the Git repository to each model request's
metadata, which Router stores as request tags, so spend can be split by
repository with nothing new for the user to do or send.

Off by default. Writing a setup is fail-open: unless the helper answers its
health check, configured clients get the direct Router URL as before.

Cursor remains out of scope because its model calls come from Cursor's cloud.
Conductor and Hermes are out of scope for this preview.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import io
import json
import os
import secrets
import socket
import statistics
import sys
import time
import urllib.parse
from collections import Counter
from collections.abc import Collection
from pathlib import Path

import click
import httpx

from ramp_cli.commands import router_local_auth, router_schedule
from ramp_cli.config.settings import config_dir

DEFAULT_PORT = 47317
SUPPORTED_CLIENTS = ("codex", "claude-code")
HEALTH_TIMEOUT_SECONDS = 0.5
START_WAIT_SECONDS = 8.0
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost"})
# The agent's health answer (ramp-router-agent's proxy.rs).
SERVICE_NAME = "ramp-router-agent"
HEALTH_PATH = "/__ramp_router_agent/health"
UPSTREAM_PREFIX = "/u/"
# The hosts the agent forwards to without being told (proxy.rs ROUTER_HOSTS).
ROUTER_HOSTS = frozenset(
    {
        "api.router.com",
        "internal-api.router.com",
        "qa-api.router.com",
        "qa-internal-api.router.com",
        "router-api.ramp.com",
    }
)
ROUTER_HOST_SUFFIX = ".router.com"


def router_host_allowed(host: str, extra_hosts: Collection[str] = ()) -> bool:
    host = host.lower().rstrip(".")
    return (
        host in ROUTER_HOSTS or host.endswith(ROUTER_HOST_SUFFIX) or host in extra_hosts
    )


def split_upstream_path(path: str) -> tuple[str, str] | None:
    """``/u/<host>/<rest>?q`` to (host, ``/<rest>?q``); None for other paths."""
    parts = urllib.parse.urlsplit(path)
    if not parts.path.startswith(UPSTREAM_PREFIX):
        return None
    netloc, _, tail = parts.path[len(UPSTREAM_PREFIX) :].partition("/")
    if not netloc:
        return None
    upstream_path = f"/{tail}"
    if parts.query:
        upstream_path += f"?{parts.query}"
    return netloc, upstream_path


def read_extra_hosts(path: Path | None) -> frozenset[str]:
    """Custom Router hosts recorded when repository tagging was enabled."""
    if path is None:
        return frozenset()
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return frozenset()
    hosts = settings.get("hosts") if isinstance(settings, dict) else None
    if not isinstance(hosts, list):
        return frozenset()
    return frozenset(h.lower() for h in hosts if isinstance(h, str) and h)


def settings_path() -> Path:
    return config_dir() / "router-repo-helper.json"


def health_token(*, create: bool = False) -> str | None:
    """User-private key for health challenge responses; never sent to the listener."""
    if not hasattr(os, "getuid") or not hasattr(os, "O_NOFOLLOW"):
        return None
    path = config_dir() / "router-agent-health-token"
    try:
        if create:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with router_local_auth.private_lock(path.with_suffix(".lock")):
            try:
                token = router_local_auth.read_private(path)
            except FileNotFoundError:
                token = ""
            if len(token) == 64 and all(c in "0123456789abcdef" for c in token):
                return token
            if create:
                token = secrets.token_hex(32)
                router_local_auth.write_private(path, token)
                return token
    except (OSError, ValueError):
        pass
    return None


def log_path() -> Path:
    """One line per proxied request: attribution facts, never content."""
    return config_dir() / "router-repo-helper.log"


def service_log_path() -> Path:
    """The agent's own output and this module's failures."""
    return router_schedule.agent_log_path()


def _log(message: str) -> None:
    try:
        path = service_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {message}\n")
    except OSError:
        pass


def load_settings() -> dict:
    try:
        settings = json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return settings if isinstance(settings, dict) else {}


def _save_settings(settings: dict) -> None:
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def enabled() -> bool:
    return load_settings().get("enabled") is True


def listener_port() -> int | None:
    """The port the agent should listen on, or None while tagging is off.

    ``keep_listening`` holds the listener after a disable that could not
    point every setup back at Router: those setups would otherwise lose it.
    """
    settings = load_settings()
    if settings.get("enabled") is True or settings.get("keep_listening") is True:
        return port()
    return None


def port() -> int:
    value = load_settings().get("port")
    if isinstance(value, int) and 1024 <= value <= 65535:
        return value
    return DEFAULT_PORT


# ---------------------------------------------------------------------------
# URLs: wrapped on write, unwrapped on read
# ---------------------------------------------------------------------------


def wrap_url(base_url: str, helper_port: int) -> str | None:
    """``https://host/v1`` to ``http://127.0.0.1:<port>/u/host/v1``."""
    parts = urllib.parse.urlsplit(base_url.strip())
    if parts.scheme != "https" or not parts.hostname:
        return None
    return (
        f"http://127.0.0.1:{helper_port}{UPSTREAM_PREFIX}"
        f"{parts.netloc}{parts.path.rstrip('/')}"
    )


def is_wrapped(url: object) -> bool:
    if not isinstance(url, str):
        return False
    try:
        parts = urllib.parse.urlsplit(url.strip())
        return (
            parts.scheme == "http"
            and parts.hostname in _LOOPBACK_HOSTS
            and split_upstream_path(parts.path) is not None
        )
    except ValueError:  # malformed, so not the helper; keep scanning the rest
        return False


def unwrap_url(url: str) -> str:
    """The Router URL behind a helper URL; any other URL is returned as is.

    Every reader of a stored setup goes through this, so refresh, connection
    checks, and deployment matching keep seeing the real Router endpoint.
    """
    if not is_wrapped(url):
        return url
    target = split_upstream_path(urllib.parse.urlsplit(url.strip()).path)
    assert target is not None
    netloc, upstream_path = target
    return f"https://{netloc}{upstream_path.rstrip('/')}"


def attribution_url(base_url: str, *, start: bool = True) -> str:
    """The URL to write into a local client setup; never raises.

    The helper URL only while the feature is on, the endpoint is a Router
    host, and the helper is answering. Otherwise the direct URL, as before.
    ``start=False`` only checks that the helper answers instead of starting it.
    """
    try:
        if not enabled():
            return base_url
        hostname = urllib.parse.urlsplit(base_url).hostname
        extra_hosts = read_extra_hosts(settings_path())
        if not hostname or not router_host_allowed(hostname, extra_hosts):
            return base_url
        if not (ensure_running() if start else answering()):
            return base_url
        return wrap_url(base_url, port()) or base_url
    except Exception as exc:
        _log(f"attribution_url failed, keeping the direct URL: {exc!r}")
        return base_url


def answering(attempts: int = 3) -> bool:
    """Whether the helper answers; a few tries, so one slow reply is not an outage."""
    return any(health() is not None for _ in range(attempts))


def client_credentials(base_url: str, api_key: str) -> tuple[str, str]:
    """Select the endpoint and credential together; never send a real key locally."""
    direct = unwrap_url(base_url)
    endpoint = attribution_url(direct)
    if not is_wrapped(endpoint):
        return direct, api_key
    try:
        key = health_token(create=True)
        if key is None:
            raise ValueError("Router agent health key is unavailable")
        upstream = urllib.parse.urlsplit(direct).netloc.lower()
        token = router_local_auth.register(
            config_dir() / "router-agent-credentials", key, upstream, api_key
        )
        return endpoint, token
    except (OSError, ValueError):
        # Do not put the credential or exception contents in diagnostics.
        _log("Could not save the local credential; keeping the direct Router endpoint")
        return direct, api_key


CURSOR_BINDING_FILENAME = "router-agent-cursor.json"


def cursor_binding_path() -> Path:
    """Which stored credential the agent uploads Cursor's repository tags with.

    Kept beside the settings, outside router-agent-credentials/, so nothing
    reading that directory takes it for a credential.
    """
    return config_dir() / CURSOR_BINDING_FILENAME


def bind_cursor(base_url: str, api_key: str, user_dir: Path) -> None:
    """Point the agent's Cursor uploads at the key Cursor was given; never raises.

    Cursor's servers call Router with the real key, so Cursor gets no local
    token. Uploading with that same key files the tags under the same Router
    user. This runs whether or not tagging is on, as Claude Desktop setup does:
    enabling tagging never revisits Cursor, whose copy of the key is encrypted
    in the Keychain. The binding names the credential file rather than its
    local token, and carries Cursor's folder because the agent service does not
    inherit XDG_CONFIG_HOME or RAMP_CURSOR_USER_DIR.
    """
    try:
        health = health_token(create=True)
        if health is None or api_key.startswith(router_local_auth.TOKEN_PREFIX):
            unbind_cursor()
            return
        upstream = urllib.parse.urlsplit(unwrap_url(base_url)).netloc.lower()
        directory = config_dir() / "router-agent-credentials"
        token = router_local_auth.register(directory, health, upstream, api_key)
        binding = {
            "upstream": upstream,
            "credential": router_local_auth.credential_path(directory, token).stem,
            "user_dir": str(user_dir.expanduser().absolute()),
        }
        router_local_auth.write_private(cursor_binding_path(), json.dumps(binding))
    except Exception:
        # An earlier binding would upload with a key Cursor may no longer use.
        unbind_cursor()
        _log(
            "Could not record Cursor's Router credential; Cursor repository tags stay off"
        )


def unbind_cursor() -> None:
    """Stop the agent's Cursor uploads; never raises."""
    try:
        cursor_binding_path().unlink(missing_ok=True)
    except OSError as exc:
        _log(f"Could not remove the Cursor binding: {exc!r}")


def cursor_bound_user_dir() -> Path | None:
    """Cursor's folder as the binding names it (what the agent scans), or None."""
    path = cursor_binding_path()
    # No binding, no private-file checks: they need os.getuid, which Windows lacks.
    if not path.exists() and not path.is_symlink():
        return None
    try:
        binding = json.loads(router_local_auth.read_private(path))
    except (OSError, ValueError):
        return None
    user = binding.get("user_dir") if isinstance(binding, dict) else None
    return Path(user) if isinstance(user, str) and Path(user).is_absolute() else None


CURSOR_UPLOAD_STATUS_FILENAME = "router-agent-cursor-status.json"
CURSOR_BOUND = "bound"
CURSOR_NEEDS_SETUP = "needs_setup"
CURSOR_STALE = "stale"
CURSOR_BROKEN = "broken"
CURSOR_UNUSED = "unused"


def cursor_upload_status_path() -> Path:
    """The agent's last Cursor upload: when, how many records, and the outcome."""
    return config_dir() / CURSOR_UPLOAD_STATUS_FILENAME


def _cursor_last_upload(since: int) -> dict | None:
    """The agent's last attempt, unless it predates the current binding."""
    try:
        upload = json.loads(cursor_upload_status_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(upload, dict) or not isinstance(upload.get("outcome"), str):
        return None
    at = upload.get("at")
    # An attempt from before the last setup used the old key or host.
    if type(at) is not int or not since <= at < 2**32:
        return None
    return upload


def _agent_upload_base(netloc: str, extra_hosts: Collection[str]) -> str | None:
    """Where the agent would upload for ``netloc`` (proxy.rs ``default_upstream``)."""
    hostport = netloc.rpartition("@")[2]
    if hostport.startswith("["):
        host, closed, after = hostport[1:].partition("]")
        if not closed:
            return None
        port = after[1:] if after.startswith(":") else ""
    else:
        host, colon, port = hostport.rpartition(":")
        if not colon:
            host, port = hostport, ""
    if port and (not (port.isascii() and port.isdigit()) or int(port) > 65535):
        return None
    host = host.lower()
    if not host or not router_host_allowed(host, extra_hosts):
        return None
    return f"https://{host}:{port}" if port and int(port) else f"https://{host}"


def _authority(url: str) -> tuple[str, int] | None:
    """Host and port Router is reached at, ignoring case, a root dot and :443."""
    try:
        parts = urllib.parse.urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    if not host:
        return None
    return host.rstrip("."), port or 443


def _shown_authority(netloc: str) -> str:
    """``host[:port]`` without any userinfo, for status output."""
    return netloc.rpartition("@")[2].lower()


def _header_safe(value: str) -> bool:
    """What ``hyper::header::HeaderValue::from_str`` accepts."""
    return all(c == "\t" or (" " <= c and c != "\x7f") for c in value)


def _cursor_credential_ok(upstream: str, digest: object) -> bool:
    """Whether the agent can load the binding's key (cursor.rs ``credential``)."""
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(c not in "0123456789abcdef" for c in digest)
    ):
        return False
    path = config_dir() / "router-agent-credentials" / f"{digest}.json"
    try:
        record = json.loads(router_local_auth.read_private(path))
    except (OSError, ValueError):
        return False
    if not isinstance(record, dict):
        return False
    key = record.get("api_key")
    return (
        record.get("upstream") == upstream
        and isinstance(key, str)
        and bool(key)
        and not key.startswith(router_local_auth.TOKEN_PREFIX)
        and _header_safe(key)
    )


def cursor_tagging(
    override_url: str | None, routes_to_router: bool, *, override_known: bool = True
) -> dict | None:
    """Whether the agent can upload Cursor's repository tags, for ``status``.

    None when Cursor neither routes to Router nor has a binding. Reads local
    files only, so it never touches the Keychain or the network. The checks
    follow the agent's own, so "bound" means the agent will find the key.
    """
    if not routes_to_router and override_url:
        # Any Router host the agent sends to counts, however it is spelled: a
        # custom deployment, case or :443 variants, or the helper-wrapped form.
        try:
            netloc = urllib.parse.urlsplit(unwrap_url(override_url)).netloc
        except ValueError:
            netloc = ""
        routes_to_router = (
            _agent_upload_base(netloc, read_extra_hosts(settings_path())) is not None
        )
    path = cursor_binding_path()
    if not path.exists() and not path.is_symlink():
        return {"state": CURSOR_NEEDS_SETUP} if routes_to_router else None
    try:
        binding = json.loads(router_local_auth.read_private(path))
    except (OSError, ValueError):
        binding = None
    upstream = binding.get("upstream") if isinstance(binding, dict) else None
    if not isinstance(upstream, str) or not upstream:
        return {"state": CURSOR_BROKEN, "reason": "binding_unreadable"}
    result = {"upstream": _shown_authority(upstream)}
    if _agent_upload_base(upstream, read_extra_hosts(settings_path())) is None:
        return {**result, "state": CURSOR_BROKEN, "reason": "host_not_allowed"}
    if not _cursor_credential_ok(upstream, binding.get("credential")):
        return {**result, "state": CURSOR_BROKEN, "reason": "credential_missing"}
    if not routes_to_router and override_known:
        # Cursor no longer sends to Router (its override was cleared, as the
        # TUI's disconnect asks, or points elsewhere), yet the agent still
        # uploads its chats' repositories with the old key.
        return {**result, "state": CURSOR_UNUSED}
    if override_url:
        # Compared the way bind_cursor derived the binding: helper URL unwrapped.
        cursor_url = unwrap_url(override_url)
        if _authority(cursor_url) != _authority(f"https://{upstream}"):
            try:
                netloc = urllib.parse.urlsplit(cursor_url).netloc
            except ValueError:
                netloc = cursor_url
            return {
                **result,
                "state": CURSOR_STALE,
                "cursor_upstream": _shown_authority(netloc),
            }
    try:
        bound_at = path.stat().st_mtime
    except OSError:
        bound_at = 0.0
    return {
        **result,
        "state": CURSOR_BOUND,
        # The agent uploads only while repository tagging is on.
        "uploading": enabled(),
        "last_upload": _cursor_last_upload(int(bound_at)),
    }


def local_token_path(token: str) -> Path:
    return router_local_auth.credential_path(
        config_dir() / "router-agent-credentials", token
    ).with_suffix(".token")


def upstream_api_key(token: str, base_url: str) -> str:
    if not token.startswith(router_local_auth.TOKEN_PREFIX):
        return token
    try:
        return router_local_auth.resolve(
            config_dir() / "router-agent-credentials",
            token,
            urllib.parse.urlsplit(unwrap_url(base_url)).netloc.lower(),
        )
    except (OSError, ValueError, TypeError) as exc:
        raise click.ClickException(
            "Could not read the Router credential for this local token. "
            "Run 'ramp router configure' to repair the setup."
        ) from exc


# ---------------------------------------------------------------------------
# Health and service
# ---------------------------------------------------------------------------


def health(helper_port: int | None = None) -> dict | None:
    """The helper's health payload, or None when it is not ours or not up."""
    helper_port = helper_port or port()
    token = health_token()
    if token is None:
        return None
    challenge = secrets.token_hex(32)
    try:
        response = httpx.get(
            f"http://127.0.0.1:{helper_port}{HEALTH_PATH}",
            timeout=HEALTH_TIMEOUT_SECONDS,
            trust_env=False,
            headers={"x-ramp-health-challenge": challenge},
        )
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    if response.status_code != 200 or not isinstance(payload, dict):
        return None
    proof = payload.get("health_proof")
    expected = hmac.new(
        token.encode("ascii"),
        f"{SERVICE_NAME}:{helper_port}:{challenge}".encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    if (
        payload.get("service") != SERVICE_NAME
        or payload.get("local_auth") is not True
        or not isinstance(proof, str)
        or len(proof) != 64
        or not proof.isascii()
        or not hmac.compare_digest(proof, expected)
    ):
        return None
    return payload


def _port_taken(helper_port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(HEALTH_TIMEOUT_SECONDS)
        return probe.connect_ex(("127.0.0.1", helper_port)) == 0


def service_supported() -> bool:
    """Whether this install has the agent and a service manager to run it."""
    return router_schedule.agent_supported()


def ensure_running(wait: float = START_WAIT_SECONDS) -> bool:
    """Start the service if needed; report whether the helper is answering."""
    helper_port = port()
    if health(helper_port) is not None:
        return True
    if not service_supported():
        return False
    if _port_taken(helper_port):
        _log(f"port {helper_port} is used by another program; not starting the helper")
        return False
    install_service()
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if health(helper_port) is not None:
            return True
        time.sleep(0.2)
    _log(f"the repository helper did not answer on port {helper_port}")
    return False


def service_installed() -> bool:
    return router_schedule.agent_installed()


def install_service() -> None:
    """Install (or repair) the agent with its listener on; never raises."""
    router_schedule.update_agent()


def uninstall_service() -> None:
    """Turn the agent's listener off; never raises.

    The agent keeps running for background sync, so this only rewrites it, or
    removes it when background sync is off as well.
    """
    router_schedule.update_agent()


# ---------------------------------------------------------------------------
# Log summary for `status`
# ---------------------------------------------------------------------------


def recent_attributions(limit: int = 500) -> dict:
    """Summarize the latest model calls in the helper log."""
    try:
        with log_path().open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - 1_000_000))
            lines = handle.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return {"model_calls": 0}
    events = []
    for line in lines[-limit * 3 :]:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("model_call"):
            events.append(event)
    events = events[-limit:]
    if not events:
        return {"model_calls": 0}
    timings = sorted(float(e.get("attribution_ms") or 0) for e in events)
    return {
        "model_calls": len(events),
        "with_repository": sum(1 for e in events if e.get("repository")),
        "by_source": dict(
            Counter(e.get("repository_source") or "none" for e in events)
        ),
        "top_repositories": Counter(
            e["repository"] for e in events if e.get("repository")
        ).most_common(5),
        "skipped": dict(Counter(e["skipped"] for e in events if e.get("skipped"))),
        "attribution_ms_median": statistics.median(timings),
        "attribution_ms_p95": timings[min(len(timings) - 1, int(len(timings) * 0.95))],
    }


# ---------------------------------------------------------------------------
# Agent lifecycle
# ---------------------------------------------------------------------------


def _router_module():
    """The router command module, imported late because it imports this one."""
    from ramp_cli.commands import router  # noqa: PLC0415

    return router


def _refresh(
    ctx: click.Context, fmt: str, *, failures_out: list[dict] | None = None
) -> list[dict]:
    """Rewrite the configured setups; quiet in JSON mode so one envelope prints."""
    router_module = _router_module()

    options = {"preserve_sync_opt_out": True}
    if failures_out is not None:
        options["failures_out"] = failures_out
    if fmt != "json":
        return router_module._refresh_clients(ctx, **options)
    with contextlib.redirect_stdout(io.StringIO()):
        return router_module._refresh_clients(ctx, **options)


def enable(ctx: click.Context, helper_port: int | None = None) -> dict:
    """Start the listener and migrate saved endpoints, without rendering a result."""
    router_module = _router_module()

    if not service_supported():
        raise click.ClickException(
            "Repository tagging needs the Ramp Router agent, which ships with the "
            "ramp installers on macOS, and on Linux with systemd."
        )
    settings = load_settings()
    previous = dict(settings)
    if (
        helper_port is not None
        and helper_port != port()
        and listener_port() is not None
    ):
        # Move clients to direct Router URLs before stopping the old listener.
        # A partial/failed refresh leaves them on either direct Router or the
        # still-live old port. After this succeeds, a failed refresh to the new
        # port likewise leaves clients on direct Router or the live new port.
        _save_settings({**previous, "enabled": False, "keep_listening": True})
        try:
            _refresh(ctx, "json")
            if router_module.clients_using_repo_helper():
                raise click.ClickException(
                    "Some local coding agents still point at the old helper port. "
                    "Run 'ramp router refresh' before changing its port."
                )
        finally:
            _save_settings(previous)
    settings["enabled"] = True
    settings.pop("keep_listening", None)
    if helper_port is not None:
        settings["port"] = helper_port
    hostname = urllib.parse.urlsplit(router_module.router_base_url()).hostname
    if hostname and not router_host_allowed(hostname):
        # A custom deployment: the helper may forward to it from now on.
        settings["hosts"] = sorted({*settings.get("hosts", []), hostname.lower()})
    _save_settings(settings)
    if not ensure_running():
        _save_settings(previous)
        uninstall_service()
        raise click.ClickException(
            f"The Router agent's local listener did not start. Details: {service_log_path()}"
        )
    failures: list[dict] = []
    results = _refresh(ctx, "json", failures_out=failures)
    _save_settings({**load_settings(), "refresh_failures": failures})
    clients = router_module.clients_using_repo_helper()
    return {
        "enabled": True,
        "port": port(),
        "clients": clients,
        "refreshed": results,
        "refresh_failures": failures,
    }


def disable(ctx: click.Context) -> dict:
    """Restore saved endpoints, retaining passthrough after a partial failure."""
    router_module = _router_module()

    settings = load_settings()
    settings["enabled"] = False
    # Refresh reconciles the service too. Keep it available until all saved
    # endpoints have been restored, including when refresh only partly succeeds.
    settings["keep_listening"] = True
    _save_settings(settings)
    try:
        results = _refresh(ctx, "json")
    finally:
        # A setup still pointed at the agent would lose Router if its listener
        # stopped, so the listener stays until none is left. Desktop setup picks
        # the helper under this lock; holding it here sees that choice. When
        # the lock cannot be taken, Desktop setup cannot take it either.
        with contextlib.ExitStack() as desktop_lock:
            if sys.platform == "darwin":
                with contextlib.suppress(click.ClickException):
                    desktop_lock.enter_context(
                        router_module.claude_cowork.transaction_lock()
                    )
            remaining = router_module.clients_using_repo_helper()
            if remaining:
                settings["keep_listening"] = True
            else:
                settings.pop("keep_listening", None)
            _save_settings(settings)
            uninstall_service()
    return {"enabled": False, "still_using_helper": remaining, "refreshed": results}


def status() -> dict:
    """Repository-tagging diagnostics for the unified agent status."""
    router_module = _router_module()

    payload = health()
    return {
        "enabled": enabled(),
        "port": port(),
        "service_supported": service_supported(),
        "service_installed": service_installed(),
        "agent": router_schedule.agent_state(),
        "healthy": payload is not None,
        "helper": payload,
        "clients": router_module.clients_using_repo_helper(),
        "log": str(log_path()),
        "recent": recent_attributions(),
        "refresh_failures": load_settings().get("refresh_failures", []),
    }
