"""Download and activate independently released Ramp skills."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

import httpx

from ramp_cli.config.settings import config_dir

_PUBLIC_REPO = "ramp-public/skills"
_ARCHIVE_NAME = "ramp-skills.tar.gz"
_CHECKSUM_NAME = f"{_ARCHIVE_NAME}.sha256"
_VALID_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_VALID_DIGEST = re.compile(r"[0-9a-fA-F]{64}")
_MAX_ARCHIVE_BYTES = 20 * 1024 * 1024
_MAX_CHECKSUM_BYTES = 1024
_MAX_SKILL_BYTES = 2 * 1024 * 1024
_MAX_TOTAL_SKILL_BYTES = 20 * 1024 * 1024
_MAX_SKILLS = 200
_MAX_ARCHIVE_MEMBERS = (_MAX_SKILLS * 2) + 10
_GITHUB_API_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


def _cache_dir() -> Path:
    return config_dir() / "skills"


def _active_path() -> Path:
    return _cache_dir() / "active.json"


def _catalog_has_skills(path: Path) -> bool:
    """Return whether a cached catalog contains at least one skill."""
    try:
        return any(
            child.is_dir() and (child / "SKILL.md").is_file()
            for child in path.iterdir()
        )
    except OSError:
        return False


def active_skills_dir() -> Path | None:
    """Return the active verified remote catalog, if one is available."""
    try:
        metadata = json.loads(_active_path().read_text(encoding="utf-8"))
        version = metadata["version"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        return None
    if not isinstance(version, str) or not _VALID_VERSION.fullmatch(version):
        return None
    path = _cache_dir() / version
    return path if path.is_dir() and _catalog_has_skills(path) else None


def active_skills_version() -> str | None:
    """Return the active remote catalog version."""
    path = active_skills_dir()
    return path.name if path else None


def latest_skills_version(*, require_immutable: bool = True) -> str | None:
    """Resolve the latest public skills release without changing local state."""
    try:
        with httpx.Client(timeout=10.0, follow_redirects=True) as client:
            release = _release_metadata(client, require_immutable=require_immutable)
    except (httpx.HTTPError, RuntimeError, TypeError, ValueError):
        return None
    return release["tag_name"]


def requested_skills_version() -> str | None:
    """Return the optional environment pin after validating it."""
    version = os.environ.get("RAMP_SKILLS_VERSION")
    if version and _VALID_VERSION.fullmatch(version):
        return version
    return None


def download_skills(version: str | None = None) -> str:
    """Download, verify, validate, and atomically activate a skills release."""
    if version is not None and not _VALID_VERSION.fullmatch(version):
        raise ValueError(f"Invalid skills version: {version}")

    try:
        with httpx.Client(timeout=30.0, follow_redirects=True) as client:
            release = _release_metadata(client, version)
            version = release["tag_name"]
            archive_url, archive_digest = _release_asset(
                release, _ARCHIVE_NAME, _MAX_ARCHIVE_BYTES
            )
            checksum_url, checksum_digest = _release_asset(
                release, _CHECKSUM_NAME, _MAX_CHECKSUM_BYTES
            )
            archive = _download_asset(
                client,
                archive_url,
                _MAX_ARCHIVE_BYTES,
                _ARCHIVE_NAME,
                archive_digest,
            )
            checksum = _download_asset(
                client,
                checksum_url,
                _MAX_CHECKSUM_BYTES,
                _CHECKSUM_NAME,
                checksum_digest,
            )
    except httpx.HTTPError as exc:
        raise RuntimeError(
            f"Could not download skills release {version or 'latest'}: {exc}"
        ) from exc

    try:
        checksum_text = checksum.decode("ascii")
    except UnicodeDecodeError as exc:
        raise RuntimeError(
            f"Invalid checksum file for skills release {version}"
        ) from exc
    checksum_parts = checksum_text.strip().split(maxsplit=1)
    expected = checksum_parts[0] if checksum_parts else ""
    if not _VALID_DIGEST.fullmatch(expected):
        raise RuntimeError(f"Invalid checksum file for skills release {version}")
    actual = hashlib.sha256(archive).hexdigest()
    if actual != expected.lower():
        raise RuntimeError(f"Checksum verification failed for skills release {version}")

    cache = _cache_dir()
    cache.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".download-", dir=cache))
    previous = staging.with_name(f"{staging.name}.previous")
    try:
        _extract_skills(archive, staging)
        destination = cache / version
        had_destination = destination.exists() or destination.is_symlink()
        if had_destination:
            destination.replace(previous)
        try:
            staging.replace(destination)
            _activate(version)
        except BaseException:
            _remove_path(destination)
            if had_destination:
                previous.replace(destination)
            raise
    except BaseException:
        _remove_path(staging)
        raise
    _remove_path(previous)
    return version


def _remove_path(path: Path) -> None:
    """Best-effort cleanup for a cached directory, file, or symlink."""
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    else:
        shutil.rmtree(path, ignore_errors=True)


def _release_metadata(
    client: httpx.Client,
    version: str | None = None,
    *,
    require_immutable: bool = True,
) -> dict[str, object]:
    """Fetch and validate a published, non-prerelease GitHub Release."""
    release_path = "latest" if version is None else f"tags/{version}"
    response = client.get(
        f"https://api.github.com/repos/{_PUBLIC_REPO}/releases/{release_path}",
        headers=_GITHUB_API_HEADERS,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("GitHub returned invalid skills release metadata")

    tag_name = payload.get("tag_name")
    if not isinstance(tag_name, str) or not _VALID_VERSION.fullmatch(tag_name):
        raise RuntimeError("GitHub returned an invalid skills release tag")
    if version is not None and tag_name != version:
        raise RuntimeError(f"GitHub returned the wrong skills release for {version}")
    if payload.get("draft") is not False or payload.get("prerelease") is not False:
        raise RuntimeError(f"Skills release {tag_name} is not a published full release")
    if require_immutable and payload.get("immutable") is not True:
        raise RuntimeError(f"Skills release {tag_name} is not immutable")
    return payload


def _release_asset(
    release: dict[str, object], name: str, max_bytes: int
) -> tuple[str, str]:
    """Return a validated release asset URL and GitHub-provided SHA-256 digest."""
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise RuntimeError("GitHub returned invalid skills release assets")
    matching = [
        asset
        for asset in assets
        if isinstance(asset, dict) and asset.get("name") == name
    ]
    if len(matching) != 1:
        raise RuntimeError(f"Skills release must contain exactly one {name} asset")

    asset = matching[0]
    size = asset.get("size")
    if (
        asset.get("state") != "uploaded"
        or not isinstance(size, int)
        or isinstance(size, bool)
        or size < 0
        or size > max_bytes
    ):
        raise RuntimeError(f"Skills release contains an invalid {name} asset")

    tag_name = release["tag_name"]
    expected_url = (
        f"https://github.com/{_PUBLIC_REPO}/releases/download/{tag_name}/{name}"
    )
    if asset.get("browser_download_url") != expected_url:
        raise RuntimeError(f"Skills release contains an invalid {name} URL")

    digest = asset.get("digest")
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        raise RuntimeError(f"Skills release is missing the GitHub digest for {name}")
    digest_value = digest.removeprefix("sha256:")
    if not _VALID_DIGEST.fullmatch(digest_value):
        raise RuntimeError(
            f"Skills release contains an invalid GitHub digest for {name}"
        )
    return expected_url, digest_value.lower()


def _download_asset(
    client: httpx.Client,
    url: str,
    max_bytes: int,
    name: str,
    expected_digest: str,
) -> bytes:
    """Stream one release asset with a hard in-memory size limit."""
    chunks: list[bytes] = []
    size = 0
    with client.stream("GET", url) as response:
        response.raise_for_status()
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > max_bytes:
                raise RuntimeError(
                    f"Skills release {name} exceeds the download size limit"
                )
            chunks.append(chunk)
    content = b"".join(chunks)
    if hashlib.sha256(content).hexdigest() != expected_digest:
        raise RuntimeError(f"GitHub digest verification failed for {name}")
    return content


def _extract_skills(archive: bytes, destination: Path) -> None:
    """Extract only valid SKILL.md entries after checksum verification."""
    fd, archive_name = tempfile.mkstemp(prefix="ramp-skills-", suffix=".tar.gz")
    try:
        with os.fdopen(fd, "wb") as archive_file:
            archive_file.write(archive)
        with tarfile.open(archive_name, mode="r:gz") as tar:
            extracted: set[str] = set()
            total_skill_bytes = 0
            for member_number, member in enumerate(tar, start=1):
                if member_number > _MAX_ARCHIVE_MEMBERS:
                    raise RuntimeError("Skills archive contains too many entries")
                path = PurePosixPath(member.name)
                if (
                    member.issym()
                    or member.islnk()
                    or path.is_absolute()
                    or ".." in path.parts
                ):
                    raise RuntimeError("Skills archive contains an unsafe path")
                if member.isdir():
                    continue
                if (
                    not member.isfile()
                    or len(path.parts) != 3
                    or path.parts[0] != "skills"
                    or path.parts[2] != "SKILL.md"
                ):
                    raise RuntimeError("Skills archive contains an unexpected entry")
                if member.size > _MAX_SKILL_BYTES:
                    raise RuntimeError("Skills archive contains an oversized skill")
                total_skill_bytes += member.size
                if total_skill_bytes > _MAX_TOTAL_SKILL_BYTES:
                    raise RuntimeError(
                        "Skills archive exceeds the extracted size limit"
                    )
                public_name = path.parts[1]
                if not public_name.startswith("ramp-"):
                    raise RuntimeError(
                        f"Skills archive contains an invalid skill: {public_name}"
                    )
                name = public_name.removeprefix("ramp-")
                if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
                    raise RuntimeError(
                        f"Skills archive contains an invalid skill: {public_name}"
                    )
                if name in extracted:
                    raise RuntimeError(
                        f"Skills archive contains duplicate skill: {name}"
                    )
                source = tar.extractfile(member)
                if source is None:
                    raise RuntimeError(f"Could not read skill from archive: {name}")
                content = source.read()
                if not content.startswith(b"---\n"):
                    raise RuntimeError(f"Skill is missing frontmatter: {name}")
                try:
                    content.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise RuntimeError(f"Skill is not valid UTF-8: {name}") from exc
                skill_dir = destination / name
                skill_dir.mkdir()
                (skill_dir / "SKILL.md").write_bytes(content)
                extracted.add(name)
                if len(extracted) > _MAX_SKILLS:
                    raise RuntimeError("Skills archive contains too many skills")
    except tarfile.TarError as exc:
        raise RuntimeError("Skills release is not a valid tar.gz archive") from exc
    finally:
        Path(archive_name).unlink(missing_ok=True)
    if not extracted:
        raise RuntimeError("Skills archive does not contain any skills")


def _activate(version: str) -> None:
    path = _active_path()
    fd, temp_name = tempfile.mkstemp(prefix=".active-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as metadata:
            json.dump({"version": version}, metadata)
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise
