"""Tests for independently released skill catalogs."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile

import pytest
from click.testing import CliRunner

from ramp_cli.commands.skills import _install_one
from ramp_cli.config.settings import config_dir
from ramp_cli.main import cli
from ramp_cli.skills import (
    get_skill_content,
    install_skill,
    installed_skill_name,
    record_receipt,
    skill_names,
)
from ramp_cli.skills.remote import (
    active_skills_dir,
    active_skills_version,
    download_skills,
    latest_skills_version,
)


class _Response:
    def __init__(self, *, content: bytes = b"", data: object = None) -> None:
        self.content = content
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def raise_for_status(self) -> None:
        pass

    def json(self):
        return self.data

    def iter_bytes(self):
        yield self.content


class _Client:
    def __init__(self, responses: list[_Response]) -> None:
        self.responses = iter(responses)
        self.urls: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def get(self, url: str, **kwargs) -> _Response:
        self.urls.append(url)
        return next(self.responses)

    def stream(self, method: str, url: str, **kwargs) -> _Response:
        self.urls.append(url)
        return next(self.responses)


def _archive(name: str = "ramp-demo", content: str | bytes = "remote") -> bytes:
    body = (
        content
        if isinstance(content, bytes)
        else f"---\nname: {name}\ndescription: {content}\n---\n{content}\n".encode()
    )
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo(f"skills/{name}/SKILL.md")
        info.size = len(body)
        archive.addfile(info, io.BytesIO(body))
    return buffer.getvalue()


def _mock_download(
    monkeypatch: pytest.MonkeyPatch,
    archive: bytes,
    digest: str,
    *,
    version: str = "v2026.07.30",
    github_archive_digest: str | None = None,
    archive_size: int | None = None,
    immutable: bool = True,
) -> _Client:
    checksum = f"{digest}  ramp-skills.tar.gz\n".encode()
    base_url = f"https://github.com/ramp-public/skills/releases/download/{version}"
    client = _Client(
        [
            _Response(
                data={
                    "tag_name": version,
                    "draft": False,
                    "prerelease": False,
                    "immutable": immutable,
                    "assets": [
                        {
                            "name": "ramp-skills.tar.gz",
                            "state": "uploaded",
                            "size": len(archive)
                            if archive_size is None
                            else archive_size,
                            "digest": (
                                "sha256:"
                                + (
                                    github_archive_digest
                                    or hashlib.sha256(archive).hexdigest()
                                )
                            ),
                            "browser_download_url": (f"{base_url}/ramp-skills.tar.gz"),
                        },
                        {
                            "name": "ramp-skills.tar.gz.sha256",
                            "state": "uploaded",
                            "size": len(checksum),
                            "digest": "sha256:" + hashlib.sha256(checksum).hexdigest(),
                            "browser_download_url": (
                                f"{base_url}/ramp-skills.tar.gz.sha256"
                            ),
                        },
                    ],
                }
            ),
            _Response(content=archive),
            _Response(content=checksum),
        ]
    )
    monkeypatch.setattr("ramp_cli.skills.remote.httpx.Client", lambda **kwargs: client)
    return client


def test_download_verifies_and_activates_catalog(monkeypatch):
    archive = _archive(content="new description")
    _mock_download(monkeypatch, archive, hashlib.sha256(archive).hexdigest())

    assert download_skills("v2026.07.30") == "v2026.07.30"

    assert active_skills_version() == "v2026.07.30"
    assert (active_skills_dir() / "demo" / "SKILL.md").is_file()
    assert "demo" in skill_names()
    assert "new description" in get_skill_content("demo")
    assert skill_names() == ["demo"]


def test_redownload_replaces_modified_cached_catalog(monkeypatch):
    first_archive = _archive(content="first")
    _mock_download(
        monkeypatch,
        first_archive,
        hashlib.sha256(first_archive).hexdigest(),
        version="v1",
    )
    download_skills("v1")
    cached_skill = active_skills_dir() / "demo" / "SKILL.md"
    cached_skill.write_text("corrupted")

    replacement_archive = _archive(content="replacement")
    _mock_download(
        monkeypatch,
        replacement_archive,
        hashlib.sha256(replacement_archive).hexdigest(),
        version="v1",
    )

    assert download_skills("v1") == "v1"
    assert "replacement" in cached_skill.read_text()


def test_redownload_restores_cached_catalog_when_activation_fails(monkeypatch):
    first_archive = _archive(content="first")
    _mock_download(
        monkeypatch,
        first_archive,
        hashlib.sha256(first_archive).hexdigest(),
        version="v1",
    )
    download_skills("v1")

    replacement_archive = _archive(content="replacement")
    _mock_download(
        monkeypatch,
        replacement_archive,
        hashlib.sha256(replacement_archive).hexdigest(),
        version="v1",
    )

    def fail_activation(version):
        raise OSError("activation failed")

    monkeypatch.setattr("ramp_cli.skills.remote._activate", fail_activation)

    with pytest.raises(OSError, match="activation failed"):
        download_skills("v1")

    assert "first" in get_skill_content("demo")


def test_latest_version_reports_mutable_public_release(monkeypatch):
    client = _Client(
        [
            _Response(
                data={
                    "tag_name": "v2026.07.30",
                    "draft": False,
                    "prerelease": False,
                    "immutable": False,
                }
            )
        ]
    )
    monkeypatch.setattr("ramp_cli.skills.remote.httpx.Client", lambda **kwargs: client)

    assert latest_skills_version(require_immutable=False) == "v2026.07.30"
    assert client.urls == [
        "https://api.github.com/repos/ramp-public/skills/releases/latest"
    ]


def test_mutable_release_is_rejected(monkeypatch):
    archive = _archive()
    _mock_download(
        monkeypatch,
        archive,
        hashlib.sha256(archive).hexdigest(),
        immutable=False,
    )

    with pytest.raises(RuntimeError, match="is not immutable"):
        download_skills("v2026.07.30")

    assert active_skills_dir() is None


def test_download_without_version_uses_latest_release(monkeypatch):
    archive = _archive()
    client = _mock_download(monkeypatch, archive, hashlib.sha256(archive).hexdigest())

    assert download_skills() == "v2026.07.30"
    assert client.urls[0] == (
        "https://api.github.com/repos/ramp-public/skills/releases/latest"
    )


def test_checksum_failure_does_not_replace_active_catalog(monkeypatch):
    archive = _archive(content="first")
    _mock_download(
        monkeypatch, archive, hashlib.sha256(archive).hexdigest(), version="v1"
    )
    download_skills("v1")

    _mock_download(monkeypatch, _archive(content="bad"), "0" * 64, version="v2")
    with pytest.raises(RuntimeError, match="Checksum verification failed"):
        download_skills("v2")

    assert active_skills_version() == "v1"
    assert "first" in get_skill_content("demo")


def test_github_asset_digest_failure_does_not_activate_catalog(monkeypatch):
    archive = _archive()
    _mock_download(
        monkeypatch,
        archive,
        hashlib.sha256(archive).hexdigest(),
        github_archive_digest="0" * 64,
    )

    with pytest.raises(RuntimeError, match="GitHub digest verification failed"):
        download_skills("v2026.07.30")

    assert active_skills_dir() is None


def test_streaming_download_enforces_size_limit(monkeypatch):
    archive = _archive()
    monkeypatch.setattr("ramp_cli.skills.remote._MAX_ARCHIVE_BYTES", len(archive) - 1)
    _mock_download(
        monkeypatch,
        archive,
        hashlib.sha256(archive).hexdigest(),
        archive_size=1,
    )

    with pytest.raises(RuntimeError, match="exceeds the download size limit"):
        download_skills("v2026.07.30")

    assert active_skills_dir() is None


def test_invalid_active_metadata_has_no_catalog():
    metadata = config_dir() / "skills" / "active.json"
    metadata.parent.mkdir(parents=True)
    metadata.write_text(json.dumps({"version": "../escape"}))

    assert active_skills_dir() is None
    assert skill_names() == []


def test_empty_active_catalog_is_redownloaded(monkeypatch):
    cache = config_dir() / "skills"
    cached_catalog = cache / "v2026.07.30"
    cached_catalog.mkdir(parents=True)
    (cache / "active.json").write_text(json.dumps({"version": "v2026.07.30"}))
    archive = _archive()
    _mock_download(monkeypatch, archive, hashlib.sha256(archive).hexdigest())

    result = CliRunner().invoke(cli, ["--human", "skills", "list"])

    assert result.exit_code == 0
    assert "demo" in result.output
    assert (cached_catalog / "demo" / "SKILL.md").is_file()


def test_explicit_update_command_activates_requested_version(monkeypatch):
    called = []
    monkeypatch.setattr(
        "ramp_cli.commands.skills.download_skills",
        lambda version: called.append(version) or version,
    )

    result = CliRunner().invoke(cli, ["--human", "skills", "update", "--version", "v7"])

    assert result.exit_code == 0
    assert called == ["v7"]
    assert "updated to v7" in result.output


def test_update_command_skips_download_when_latest_is_active(monkeypatch):
    monkeypatch.setattr(
        "ramp_cli.commands.skills.active_skills_version", lambda: "v0.1.0"
    )
    monkeypatch.setattr(
        "ramp_cli.commands.skills.latest_skills_version",
        lambda *, require_immutable=True: "v0.1.0",
    )
    monkeypatch.setattr(
        "ramp_cli.commands.skills.download_skills",
        lambda version: pytest.fail("latest catalog should not be downloaded"),
    )

    result = CliRunner().invoke(cli, ["--human", "skills", "update"])

    assert result.exit_code == 0
    assert "already up to date at v0.1.0" in result.output


@pytest.mark.parametrize(
    "args",
    [
        ["--agent", "skills", "update", "--version", "v7"],
        ["skills", "update", "--version", "v7"],
    ],
)
def test_explicit_update_command_uses_agent_json(monkeypatch, args):
    monkeypatch.setattr(
        "ramp_cli.commands.skills.download_skills", lambda version: version
    )

    result = CliRunner().invoke(cli, args)

    assert result.exit_code == 0
    assert json.loads(result.output) == {
        "schema_version": "1.0",
        "data": [
            {
                "updated": True,
                "version": "v7",
                "source": "ramp-public/skills",
            }
        ],
        "pagination": None,
    }


def test_cached_noninteractive_install_does_not_check_for_update(monkeypatch, tmp_path):
    archive = _archive(name="ramp-browser-automation")
    _mock_download(monkeypatch, archive, hashlib.sha256(archive).hexdigest())
    download_skills("v2026.07.30")

    def unexpected_check():
        raise AssertionError("remote update check should not run")

    monkeypatch.setattr(
        "ramp_cli.commands.skills.latest_skills_version", unexpected_check
    )
    result = CliRunner().invoke(
        cli,
        [
            "--no-input",
            "skills",
            "install",
            "browser-automation",
            "--target",
            str(tmp_path / "skills"),
        ],
    )

    assert result.exit_code == 0


def test_interactive_install_accepts_catalog_update(monkeypatch, tmp_path):
    archive = _archive(name="ramp-browser-automation")
    _mock_download(
        monkeypatch,
        archive,
        hashlib.sha256(archive).hexdigest(),
        version="v1",
    )
    download_skills("v1")
    downloaded = []
    monkeypatch.setattr("ramp_cli.commands.skills._is_interactive", lambda: True)
    monkeypatch.setattr(
        "ramp_cli.commands.skills.requested_skills_version", lambda: None
    )
    monkeypatch.setattr("ramp_cli.commands.skills.latest_skills_version", lambda: "v2")
    monkeypatch.setattr("ramp_cli.commands.skills.active_skills_version", lambda: "v1")
    monkeypatch.setattr(
        "ramp_cli.commands.skills.download_skills",
        lambda version: downloaded.append(version) or version,
    )

    result = CliRunner().invoke(
        cli,
        [
            "--human",
            "skills",
            "install",
            "browser-automation",
            "--target",
            str(tmp_path / "skills"),
        ],
        input="y\n",
    )

    assert result.exit_code == 0, result.output
    assert downloaded == ["v2"]


def test_interactive_install_declines_catalog_update(monkeypatch, tmp_path):
    archive = _archive(name="ramp-browser-automation")
    _mock_download(
        monkeypatch,
        archive,
        hashlib.sha256(archive).hexdigest(),
        version="v1",
    )
    download_skills("v1")
    monkeypatch.setattr("ramp_cli.commands.skills._is_interactive", lambda: True)
    monkeypatch.setattr(
        "ramp_cli.commands.skills.requested_skills_version", lambda: None
    )
    monkeypatch.setattr("ramp_cli.commands.skills.latest_skills_version", lambda: "v2")
    monkeypatch.setattr("ramp_cli.commands.skills.active_skills_version", lambda: "v1")
    monkeypatch.setattr(
        "ramp_cli.commands.skills.download_skills",
        lambda version: pytest.fail("declined update must not download"),
    )

    result = CliRunner().invoke(
        cli,
        [
            "--human",
            "skills",
            "install",
            "browser-automation",
            "--target",
            str(tmp_path / "skills"),
        ],
        input="n\n",
    )

    assert result.exit_code == 0, result.output


def test_interactive_install_update_failure_uses_existing_catalog(
    monkeypatch, tmp_path
):
    archive = _archive(name="ramp-browser-automation")
    _mock_download(
        monkeypatch,
        archive,
        hashlib.sha256(archive).hexdigest(),
        version="v1",
    )
    download_skills("v1")

    def fail_download(version):
        raise OSError("offline")

    monkeypatch.setattr("ramp_cli.commands.skills._is_interactive", lambda: True)
    monkeypatch.setattr(
        "ramp_cli.commands.skills.requested_skills_version", lambda: None
    )
    monkeypatch.setattr("ramp_cli.commands.skills.latest_skills_version", lambda: "v2")
    monkeypatch.setattr("ramp_cli.commands.skills.active_skills_version", lambda: "v1")
    monkeypatch.setattr("ramp_cli.commands.skills.download_skills", fail_download)

    result = CliRunner().invoke(
        cli,
        [
            "--human",
            "skills",
            "install",
            "browser-automation",
            "--target",
            str(tmp_path / "skills"),
        ],
        input="y\n",
    )

    assert result.exit_code == 0, result.output
    assert "using the existing catalog" in result.output


def test_list_downloads_latest_when_no_catalog_is_cached(monkeypatch):
    archive = _archive()
    _mock_download(monkeypatch, archive, hashlib.sha256(archive).hexdigest())

    result = CliRunner().invoke(cli, ["--human", "skills", "list"])

    assert result.exit_code == 0
    assert "now distributed from ramp-public/skills" in result.output
    assert "demo" in result.output
    assert active_skills_version() == "v2026.07.30"


def test_first_catalog_download_honors_environment_pin(monkeypatch):
    archive = _archive()
    client = _mock_download(
        monkeypatch,
        archive,
        hashlib.sha256(archive).hexdigest(),
        version="v7",
    )
    monkeypatch.setenv("RAMP_SKILLS_VERSION", "v7")

    result = CliRunner().invoke(cli, ["--agent", "skills", "list"])

    assert result.exit_code == 0
    assert json.loads(result.output)["data"][0]["name"] == "demo"
    assert client.urls[0].endswith("/releases/tags/v7")
    assert active_skills_version() == "v7"


def test_missing_catalog_reports_download_failure(monkeypatch):
    def failed_download(version=None):
        raise RuntimeError("GitHub unavailable")

    monkeypatch.setattr("ramp_cli.commands.skills.download_skills", failed_download)

    result = CliRunner().invoke(cli, ["--human", "skills", "list"])

    assert result.exit_code != 0
    assert "no verified local catalog is available" in result.output


def test_first_catalog_download_overwrites_existing_managed_skill(
    monkeypatch, tmp_path
):
    target = tmp_path / "skills"
    installed = target / "ramp-browser-automation" / "SKILL.md"
    installed.parent.mkdir(parents=True)
    installed.write_text("user modification")
    record_receipt(target, "ramp-browser-automation")

    archive = _archive(name="ramp-browser-automation", content="remote update")
    _mock_download(monkeypatch, archive, hashlib.sha256(archive).hexdigest())

    result = CliRunner().invoke(
        cli, ["skills", "install", "--all", "--target", str(target)]
    )

    assert result.exit_code == 0
    assert "Updated ramp-browser-automation" in result.output
    assert "remote update" in installed.read_text()


def test_catalog_transition_overwrites_modified_managed_skill(monkeypatch, tmp_path):
    name = "browser-automation"
    archive = _archive(name="ramp-browser-automation")
    _mock_download(monkeypatch, archive, hashlib.sha256(archive).hexdigest())
    download_skills("v2026.07.30")
    install_skill(name, tmp_path)
    record_receipt(tmp_path, installed_skill_name(name))
    installed = tmp_path / installed_skill_name(name) / "SKILL.md"
    installed.write_text("user modification")

    archive = _archive(name="ramp-browser-automation", content="remote update")
    _mock_download(
        monkeypatch,
        archive,
        hashlib.sha256(archive).hexdigest(),
        version="v2",
    )
    download_skills("v2")

    assert _install_one(tmp_path, name, install_all=True)
    assert "remote update" in installed.read_text()


def test_archive_rejects_unsafe_paths(monkeypatch):
    body = b"---\nname: demo\ndescription: demo\n---\n"
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive_file:
        info = tarfile.TarInfo("../ramp-demo/SKILL.md")
        info.size = len(body)
        archive_file.addfile(info, io.BytesIO(body))
    archive = buffer.getvalue()
    _mock_download(
        monkeypatch, archive, hashlib.sha256(archive).hexdigest(), version="v1"
    )

    with pytest.raises(RuntimeError, match="unsafe path"):
        download_skills("v1")

    assert active_skills_dir() is None


def test_archive_rejects_files_outside_expected_layout(monkeypatch):
    body = b"---\nname: demo\ndescription: demo\n---\n"
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive_file:
        info = tarfile.TarInfo("other/ramp-demo/SKILL.md")
        info.size = len(body)
        archive_file.addfile(info, io.BytesIO(body))
    archive = buffer.getvalue()
    _mock_download(monkeypatch, archive, hashlib.sha256(archive).hexdigest())

    with pytest.raises(RuntimeError, match="unexpected entry"):
        download_skills("v2026.07.30")

    assert active_skills_dir() is None


def test_archive_rejects_unprefixed_skill_directory(monkeypatch):
    archive = _archive(name="demo")
    _mock_download(monkeypatch, archive, hashlib.sha256(archive).hexdigest())

    with pytest.raises(RuntimeError, match="invalid skill: demo"):
        download_skills("v2026.07.30")

    assert active_skills_dir() is None


def test_invalid_utf8_does_not_replace_active_catalog(monkeypatch):
    first_archive = _archive(content="first")
    _mock_download(
        monkeypatch,
        first_archive,
        hashlib.sha256(first_archive).hexdigest(),
        version="v1",
    )
    download_skills("v1")

    invalid_archive = _archive(
        content=b"---\nname: ramp-demo\ndescription: invalid\n---\n\xff"
    )
    _mock_download(
        monkeypatch,
        invalid_archive,
        hashlib.sha256(invalid_archive).hexdigest(),
        version="v2",
    )

    with pytest.raises(RuntimeError, match="not valid UTF-8"):
        download_skills("v2")

    assert active_skills_version() == "v1"
    assert "first" in get_skill_content("demo")
