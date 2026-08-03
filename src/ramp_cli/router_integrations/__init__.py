"""Bundled coding-agent integrations for Ramp Router."""

from importlib.resources import files
from pathlib import Path

_PACKAGES = Path(str(files(__name__))) / "packages"


def integration_package_path(client: str) -> Path:
    """Return the bundled local package used by an agent client."""
    package = {
        "opencode": "opencode-provider",
        "pi": "pi-provider",
    }.get(client)
    if package is None:
        raise ValueError(f"{client} does not use a bundled plugin")
    return _PACKAGES / package
