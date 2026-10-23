"""Access level derived from OAuth scopes.

Core ignores individual scopes for first-party CLI tokens and enforces the
user's role permissions instead. The only scope signal it honors is whether
any scope ends in ``:write``: tokens without one are read-only.
"""

from __future__ import annotations

from collections.abc import Iterable

READ_ONLY = "read-only"
READ_WRITE = "read/write"


def access_level(scopes: Iterable[str]) -> str | None:
    """Return the access level for granted scopes, or None when unknown."""
    scope_set = set(scopes)
    if not scope_set:
        return None
    if any(scope.endswith(":write") for scope in scope_set):
        return READ_WRITE
    return READ_ONLY


def read_only_scopes(scopes: str) -> str:
    """Keep only the ``:read`` scopes from a space-separated scope string."""
    return " ".join(scope for scope in scopes.split() if scope.endswith(":read"))
