"""Unit tests for ramp_cli.specs.sync — fetch_spec and maybe_sync."""

import json
import os
import time
from unittest.mock import MagicMock, patch

import httpx
import pytest

from ramp_cli.specs.sync import _COOLDOWN_SECONDS, fetch_spec, maybe_sync

FAKE_SPEC = {"paths": {"/v1/agent-tools/a": {}, "/v1/agent-tools/b": {}, "/other": {}}}


def _mock_response(*, json_body=None, status_code=200, headers=None):
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.headers = headers or {}
    resp.json.return_value = json_body or {}
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "err", request=MagicMock(), response=resp
        )
    return resp


@pytest.fixture()
def _mock_httpx(request):
    """Patch httpx.Client to return pre-configured responses.

    Set ``request.param`` to a list of responses that will be returned
    by successive ``client.get()`` calls.
    """
    responses = request.param
    client_instance = MagicMock()
    client_instance.get.side_effect = responses
    client_instance.__enter__ = MagicMock(return_value=client_instance)
    client_instance.__exit__ = MagicMock(return_value=False)
    with patch("ramp_cli.specs.sync.httpx.Client", return_value=client_instance):
        yield client_instance


@pytest.fixture()
def sync_paths(tmp_path, monkeypatch):
    """Point local_agent_tool_spec and local_agent_tool_hash at tmp_path."""
    monkeypatch.setattr(
        "ramp_cli.specs.sync.local_agent_tool_spec",
        lambda env: tmp_path / f"spec-{env}.json",
    )
    monkeypatch.setattr(
        "ramp_cli.specs.sync.local_agent_tool_hash",
        lambda env: tmp_path / f"hash-{env}.txt",
    )
    return tmp_path


class TestFetchSpec:
    @pytest.mark.parametrize(
        "_mock_httpx",
        [
            [
                _mock_response(json_body=FAKE_SPEC),
                _mock_response(json_body={"content_hash": "abc123"}),
            ]
        ],
        indirect=True,
    )
    def test_writes_spec_and_hash(self, _mock_httpx, sync_paths):
        count = fetch_spec("production")

        assert count == 2
        spec_file = sync_paths / "spec-production.json"
        hash_file = sync_paths / "hash-production.txt"
        assert json.loads(spec_file.read_text()) == FAKE_SPEC
        assert hash_file.read_text() == "abc123"

    @pytest.mark.parametrize(
        "_mock_httpx",
        [
            [
                _mock_response(json_body=FAKE_SPEC),
                _mock_response(json_body={}),
            ]
        ],
        indirect=True,
    )
    def test_no_content_hash_writes_empty(self, _mock_httpx, sync_paths):
        fetch_spec("sandbox")
        assert (sync_paths / "hash-sandbox.txt").read_text() == ""

    @pytest.mark.parametrize(
        "_mock_httpx",
        [
            [
                _mock_response(json_body=FAKE_SPEC),
                _mock_response(json_body={"content_hash": "prod_hash"}),
                _mock_response(json_body={"paths": {"/v1/agent-tools/x": {}}}),
                _mock_response(json_body={"content_hash": "sandbox_hash"}),
            ]
        ],
        indirect=True,
    )
    def test_per_env_isolation(self, _mock_httpx, sync_paths):
        """Production and sandbox get separate cache files."""
        fetch_spec("production")
        fetch_spec("sandbox")

        prod_spec = json.loads((sync_paths / "spec-production.json").read_text())
        sandbox_spec = json.loads((sync_paths / "spec-sandbox.json").read_text())
        assert prod_spec != sandbox_spec

        assert (sync_paths / "hash-production.txt").read_text() == "prod_hash"
        assert (sync_paths / "hash-sandbox.txt").read_text() == "sandbox_hash"

    @pytest.mark.parametrize(
        "_mock_httpx",
        [[_mock_response(json_body=FAKE_SPEC)]],
        indirect=True,
    )
    def test_known_hash_skips_hash_request(self, _mock_httpx, sync_paths):
        """When known_hash is provided, only one request (spec) is made."""
        fetch_spec("production", known_hash="pre-fetched")

        assert (sync_paths / "hash-production.txt").read_text() == "pre-fetched"
        # Only one get() call (spec), not two
        assert _mock_httpx.get.call_count == 1


class TestMaybeSync:
    @pytest.mark.parametrize(
        "_mock_httpx",
        [[_mock_response(json_body={"content_hash": "newhash"})]],
        indirect=True,
    )
    @patch("ramp_cli.specs.sync.fetch_spec")
    def test_hash_file_missing_triggers_fetch(
        self, mock_fetch, _mock_httpx, sync_paths
    ):
        maybe_sync("production")
        mock_fetch.assert_called_once_with("production", known_hash="newhash")

    def test_fresh_hash_file_skips_network(self, sync_paths):
        hash_file = sync_paths / "hash-production.txt"
        hash_file.write_text("somehash")

        with patch("ramp_cli.specs.sync.httpx.Client") as mock_cls:
            maybe_sync("production")
            mock_cls.assert_not_called()

    @pytest.mark.parametrize(
        "_mock_httpx",
        [[_mock_response(json_body={"content_hash": "samehash"})]],
        indirect=True,
    )
    @patch("ramp_cli.specs.sync.fetch_spec")
    def test_stale_hash_same_value_touches_file(
        self, mock_fetch, _mock_httpx, sync_paths
    ):
        hash_file = sync_paths / "hash-production.txt"
        hash_file.write_text("samehash")
        old_time = time.time() - _COOLDOWN_SECONDS - 100
        os.utime(hash_file, (old_time, old_time))

        maybe_sync("production")

        mock_fetch.assert_not_called()
        assert time.time() - hash_file.stat().st_mtime < 10

    @pytest.mark.parametrize(
        "_mock_httpx",
        [[_mock_response(json_body={"content_hash": "newhash"})]],
        indirect=True,
    )
    @patch("ramp_cli.specs.sync.fetch_spec")
    def test_stale_hash_different_value_fetches(
        self, mock_fetch, _mock_httpx, sync_paths
    ):
        hash_file = sync_paths / "hash-production.txt"
        hash_file.write_text("oldhash")
        old_time = time.time() - _COOLDOWN_SECONDS - 100
        os.utime(hash_file, (old_time, old_time))

        maybe_sync("production")

        mock_fetch.assert_called_once_with("production", known_hash="newhash")

    def test_network_error_silently_returns(self, sync_paths):
        client_instance = MagicMock()
        client_instance.get.side_effect = httpx.ConnectError("offline")
        client_instance.__enter__ = MagicMock(return_value=client_instance)
        client_instance.__exit__ = MagicMock(return_value=False)

        with patch("ramp_cli.specs.sync.httpx.Client", return_value=client_instance):
            maybe_sync("production")  # should not raise

    def test_stale_production_does_not_suppress_sandbox(self, sync_paths):
        """Per-env cooldown: a fresh production hash must not block sandbox checks."""
        prod_hash = sync_paths / "hash-production.txt"
        prod_hash.write_text("prod_hash")  # fresh — written just now

        with patch("ramp_cli.specs.sync.httpx.Client") as mock_cls:
            client_instance = MagicMock()
            mock_cls.return_value = client_instance
            client_instance.__enter__ = MagicMock(return_value=client_instance)
            client_instance.__exit__ = MagicMock(return_value=False)
            client_instance.get.return_value = _mock_response(
                json_body={"content_hash": "sandbox_hash"}
            )

            with patch("ramp_cli.specs.sync.fetch_spec") as mock_fetch:
                maybe_sync("sandbox")
                mock_fetch.assert_called_once_with("sandbox", known_hash="sandbox_hash")
