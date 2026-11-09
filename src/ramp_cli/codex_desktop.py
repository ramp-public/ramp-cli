import asyncio
import json
import os
import stat
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import click
from websockets.asyncio.client import unix_connect
from websockets.exceptions import WebSocketException


class _RefreshUnavailable(Exception):
    pass


def _matches_config(expected: object, actual: object) -> bool:
    # config/read expands provider defaults (including command-auth cwd and
    # refresh interval). Match every saved setting without rejecting defaults.
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and _matches_config(value, actual[key])
            for key, value in expected.items()
        )
    return expected == actual


def refresh_catalog(config_path: Path, catalog_path: Path) -> dict[str, str]:
    """Best-effort invalidation of an existing, verified Router backend.

    The conventional daemon socket is only a candidate, not evidence that
    Desktop is attached. Never spawn a server or infer support from a CLI
    version. The empty provider patch is an indirect, experimental hook.
    The caller holds the Codex config lock after successful disk persistence.
    """
    try:
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
        provider = config.get("model_providers", {}).get("ramp-router", {})
        if (
            config.get("model_provider") != "ramp-router"
            or config.get("model_catalog_json") != str(catalog_path.resolve())
            or not provider.get("base_url")
            or not provider.get("auth", {}).get("command")
        ):
            raise _RefreshUnavailable("backend_mismatch")
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        expected = {row["slug"] for row in catalog["models"]}
        if not expected or not all(isinstance(slug, str) and slug for slug in expected):
            raise _RefreshUnavailable("catalog_unavailable")
        socket_path = (
            config_path.parent / "app-server-control" / "app-server-control.sock"
        )
        try:
            info = socket_path.lstat()
            if stat.S_ISLNK(info.st_mode) and info.st_uid == os.getuid():
                # Codex publishes a symlink to a short socket path (Unix
                # socket path lengths are limited). Check its target too.
                socket_path = socket_path.resolve(strict=True)
                info = socket_path.stat()
        except FileNotFoundError:
            raise _RefreshUnavailable("transport_missing") from None
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            raise _RefreshUnavailable("backend_mismatch")
        asyncio.run(
            asyncio.wait_for(
                _refresh_catalog_backend(socket_path, config_path, config, expected),
                timeout=2,
            )
        )
    except _RefreshUnavailable as exc:
        reason = str(exc)
    except TimeoutError:
        reason = "timeout"
    except (OSError, WebSocketException):
        reason = "backend_unreachable"
    except (ValueError, TypeError, KeyError, AttributeError):
        reason = "invalid_response"
    except Exception:
        # This optional step must never undo disk success or prevent other
        # clients from refreshing. Leave process interrupts uncaught.
        reason = "unexpected_error"
    else:
        return {"status": "backend_confirmed", "reason": "catalog_matched"}
    # No raw backend errors or config values: results may appear in sync logs.
    return {"status": "disk_only", "reason": reason}


async def _refresh_catalog_backend(
    socket_path: Path, config_path: Path, config: dict, expected: set[str]
) -> None:
    async with unix_connect(
        str(socket_path),
        uri="ws://localhost/rpc",
        open_timeout=1,
        close_timeout=0.1,
        max_size=4 * 1024 * 1024,
        proxy=None,
    ) as connection:
        request_id = 0

        async def rpc(method: str, params: dict) -> dict:
            nonlocal request_id
            request_id += 1
            await connection.send(
                json.dumps({"id": request_id, "method": method, "params": params})
            )
            while True:
                response = json.loads(await connection.recv())
                if response.get("id") != request_id:
                    continue
                if "error" in response:
                    code = response["error"].get("code")
                    raise _RefreshUnavailable(
                        "unsupported_rpc"
                        if code in (-32600, -32601, -32602)
                        else "rpc_failed"
                    )
                result = response["result"]
                if not isinstance(result, dict):
                    raise _RefreshUnavailable("invalid_response")
                return result

        await rpc(
            "initialize",
            {
                "clientInfo": {"name": "ramp-cli-catalog-refresh", "version": "1"},
                "capabilities": {"experimentalApi": True},
            },
        )
        await connection.send('{"method":"initialized"}')
        backend = await rpc("config/read", {"includeLayers": True})
        # A socket under CODEX_HOME is insufficient: require the backend's
        # user layer to name this config and its effective Router settings to
        # match. Overrides/profiles pointing elsewhere must fail closed.
        if not any(
            layer.get("name", {}).get("type") == "user"
            and layer["name"].get("file") == str(config_path.resolve())
            and not layer.get("disabledReason")
            for layer in backend.get("layers", [])
        ):
            raise _RefreshUnavailable("backend_mismatch")
        effective = backend["config"]
        if (
            effective.get("model_provider") != "ramp-router"
            or effective.get("model_catalog_json") != config["model_catalog_json"]
            or not _matches_config(
                config["model_providers"]["ramp-router"],
                effective.get("model_providers", {}).get("ramp-router"),
            )
        ):
            raise _RefreshUnavailable("backend_mismatch")
        await rpc(
            "modelProvider/config/write",
            {"settings": {"type": "customGateway", "providerId": "ramp-router"}},
        )
        actual = set()
        cursors = set()
        cursor = None
        while True:
            page = await rpc(
                "model/list", {"includeHidden": True, "limit": 100, "cursor": cursor}
            )
            actual.update(row["id"] for row in page["data"])
            cursor = page.get("nextCursor")
            if cursor is None:
                break
            if not isinstance(cursor, str) or not cursor or cursor in cursors:
                raise _RefreshUnavailable("invalid_response")
            cursors.add(cursor)
        if actual != expected:
            raise _RefreshUnavailable("catalog_mismatch")


def catalog_refresh_message(result: dict[str, str]) -> str:
    if result["status"] == "backend_confirmed":
        return (
            "Codex catalog saved; the reachable backend confirmed the model list. "
            "Desktop attachment and picker rendering are unverified; reopen Codex "
            "if its picker is stale."
        )
    return (
        "Codex catalog saved; live backend refresh was not confirmed "
        f"({result['reason']}). Restart Codex to load the saved catalog. "
        "Desktop's backend transport is not established."
    )


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
