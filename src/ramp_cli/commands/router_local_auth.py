"""Private files for the Router agent's local credential boundary."""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import stat
import tempfile
from pathlib import Path

TOKEN_PREFIX = "ramp-local-"


def check_directory(path: Path) -> None:
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o022
    ):
        raise OSError("Router agent directory is not owned and protected by this user")


def read_private(path: Path) -> str:
    check_directory(path.parent)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, encoding="utf-8") as handle:
        info = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise OSError("Router agent file is not private to this user")
        contents = handle.read(65537)
        if len(contents) > 65536:
            raise ValueError("Router agent private file is too large")
        return contents


def write_private(path: Path, content: str) -> None:
    """Publish complete 0600 contents; a failed write leaves the old file intact."""
    check_directory(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextlib.contextmanager
def private_lock(path: Path):
    # These files are used only by the Unix native agent.
    import fcntl  # noqa: PLC0415

    check_directory(path.parent)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(fd, "r+") as handle:
        info = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise OSError("Router agent lock is not private to this user")
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def credential_path(directory: Path, token: str) -> Path:
    suffix = token.removeprefix(TOKEN_PREFIX)
    if (
        not token.startswith(TOKEN_PREFIX)
        or len(suffix) != 64
        or any(c not in "0123456789abcdef" for c in suffix)
    ):
        raise ValueError("Invalid Router agent local token")
    return directory / f"{hashlib.sha256(token.encode()).hexdigest()}.json"


def register(directory: Path, health_key: str, upstream: str, api_key: str) -> str:
    """Stable, unguessable token per user, upstream authority and Router key.

    Domain separation keeps local bearer tokens distinct from health proofs.
    Old bindings remain usable by open sessions during refresh or key rotation.
    """
    check_directory(directory.parent)
    directory.mkdir(mode=0o700, exist_ok=True)
    check_directory(directory)
    message = json.dumps(["router-local-credential-v1", upstream, api_key]).encode()
    token = (
        TOKEN_PREFIX
        + hmac.new(health_key.encode(), message, hashlib.sha256).hexdigest()
    )
    record = json.dumps({"upstream": upstream, "api_key": api_key})
    path = credential_path(directory, token)
    write_private(path, record)
    # Codex command auth reads this file; the real key file remains available
    # to the direct cost hook and CLI refresh, never to the local endpoint.
    write_private(path.with_suffix(".token"), token)
    return token


def resolve(directory: Path, token: str, upstream: str) -> str:
    record = json.loads(read_private(credential_path(directory, token)))
    if (
        not isinstance(record, dict)
        or record.get("upstream") != upstream
        or not isinstance(record.get("api_key"), str)
        or not record["api_key"]
        or record["api_key"].startswith(TOKEN_PREFIX)
    ):
        raise ValueError("Router agent token does not belong to this upstream")
    return record["api_key"]
