"""Tests for the ramp feedback command."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import httpx
from click.testing import CliRunner

from ramp_cli.main import cli


def _invoke(args, **kwargs):
    runner = CliRunner()
    return runner.invoke(cli, args, catch_exceptions=False, **kwargs)


class TestFeedbackValidation:
    def test_too_short(self, isolated_config):
        result = _invoke(["feedback", "short"])
        assert result.exit_code != 0
        assert "at least 10 characters" in result.output

    def test_too_long(self, isolated_config):
        result = _invoke(["feedback", "x" * 1001])
        assert result.exit_code != 0
        assert "at most 1000 characters" in result.output

    def test_missing_argument(self, isolated_config):
        result = CliRunner().invoke(cli, ["feedback"])
        assert result.exit_code != 0


class TestFeedbackSuccess:
    @patch("ramp_cli.commands.feedback.httpx.Client")
    @patch("ramp_cli.commands.feedback.store")
    def test_unauthenticated_submit(self, mock_store, mock_client_cls, isolated_config):
        mock_store.has_tokens.return_value = False
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_http = MagicMock()
        mock_http.__enter__ = MagicMock(return_value=mock_http)
        mock_http.__exit__ = MagicMock(return_value=False)
        mock_http.get.return_value = mock_resp
        mock_client_cls.return_value = mock_http

        result = _invoke(["feedback", "The transactions endpoint returns stale data"])
        assert result.exit_code == 0
        assert "Feedback submitted" in result.output

        # Verify the request
        call_args = mock_http.get.call_args
        assert "/v1/public/api-feedback/llm" in call_args[0][0]
        params = call_args[1]["params"]
        assert params["source"] == "RAMP_CLI"
        assert "Ramp CLI v" in params["feedback"]
        assert "agent=false" in params["feedback"]
        assert "The transactions endpoint returns stale data" in params["feedback"]

    @patch("ramp_cli.commands.feedback.httpx.Client")
    @patch("ramp_cli.commands.feedback.store")
    def test_authenticated_submit(self, mock_store, mock_client_cls, isolated_config):
        mock_store.has_tokens.return_value = True
        mock_store.get_tokens.return_value = ("fake-access-token", "fake-refresh-token")

        # The enrichment call returns business info, the submit call succeeds
        biz_resp = MagicMock()
        biz_resp.content = json.dumps(
            {
                "id": "biz-uuid-123",
                "business_name_on_card": "Acme Corp",
            }
        ).encode()
        biz_resp.raise_for_status = MagicMock()

        submit_resp = MagicMock()
        submit_resp.raise_for_status = MagicMock()

        mock_http = MagicMock()
        mock_http.__enter__ = MagicMock(return_value=mock_http)
        mock_http.__exit__ = MagicMock(return_value=False)
        # First Client() call is for enrichment (3s timeout), second is for submit (15s timeout)
        mock_http.get.side_effect = [biz_resp, submit_resp]
        mock_client_cls.return_value = mock_http

        result = _invoke(["feedback", "Great API documentation"])
        assert result.exit_code == 0

        # The submit call is the second .get() call
        submit_call_args = mock_http.get.call_args_list[1]
        params = submit_call_args[1]["params"]
        assert "Acme Corp" not in params["feedback"]
        assert "biz=biz-uuid-123" in params["feedback"]

    @patch("ramp_cli.commands.feedback.httpx.Client")
    @patch("ramp_cli.commands.feedback.store")
    def test_business_fetch_failure_graceful(
        self, mock_store, mock_client_cls, isolated_config
    ):
        mock_store.has_tokens.return_value = True
        mock_store.get_tokens.return_value = ("fake-access-token", "fake-refresh-token")

        # Enrichment call fails, submit call succeeds
        enrich_http = MagicMock()
        enrich_http.__enter__ = MagicMock(return_value=enrich_http)
        enrich_http.__exit__ = MagicMock(return_value=False)
        enrich_http.get.side_effect = Exception("network error")

        submit_resp = MagicMock()
        submit_resp.raise_for_status = MagicMock()
        submit_http = MagicMock()
        submit_http.__enter__ = MagicMock(return_value=submit_http)
        submit_http.__exit__ = MagicMock(return_value=False)
        submit_http.get.return_value = submit_resp

        # First Client() for enrichment, second for submit
        mock_client_cls.side_effect = [enrich_http, submit_http]

        result = _invoke(["feedback", "Feedback without business info"])
        assert result.exit_code == 0
        assert "Feedback submitted" in result.output

        # Should still submit, just without business info
        params = submit_http.get.call_args[1]["params"]
        assert "Business:" not in params["feedback"]


class TestFeedbackAgentMode:
    @patch("ramp_cli.commands.feedback.httpx.Client")
    @patch("ramp_cli.commands.feedback.store")
    def test_agent_mode_json_output(self, mock_store, mock_client_cls, isolated_config):
        mock_store.has_tokens.return_value = False
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_http = MagicMock()
        mock_http.__enter__ = MagicMock(return_value=mock_http)
        mock_http.__exit__ = MagicMock(return_value=False)
        mock_http.get.return_value = mock_resp
        mock_client_cls.return_value = mock_http

        result = _invoke(["--agent", "feedback", "Feedback in agent mode"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["schema_version"] == "1.0"
        assert data["data"][0]["message"] == "Feedback submitted successfully"

        # Verify agent=true in the feedback text
        params = mock_http.get.call_args[1]["params"]
        assert "agent=true" in params["feedback"]


class TestFeedbackErrors:
    @patch("ramp_cli.commands.feedback.httpx.Client")
    @patch("ramp_cli.commands.feedback.store")
    def test_timeout_error(self, mock_store, mock_client_cls, isolated_config):
        mock_store.has_tokens.return_value = False
        mock_http = MagicMock()
        mock_http.__enter__ = MagicMock(return_value=mock_http)
        mock_http.__exit__ = MagicMock(return_value=False)
        mock_http.get.side_effect = httpx.TimeoutException("timeout")
        mock_client_cls.return_value = mock_http

        result = _invoke(["feedback", "This should time out"])
        assert result.exit_code != 0
        assert "timed out" in result.output

    @patch("ramp_cli.commands.feedback.httpx.Client")
    @patch("ramp_cli.commands.feedback.store")
    def test_http_error(self, mock_store, mock_client_cls, isolated_config):
        mock_store.has_tokens.return_value = False
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "bad request", request=MagicMock(), response=mock_resp
        )
        mock_http = MagicMock()
        mock_http.__enter__ = MagicMock(return_value=mock_http)
        mock_http.__exit__ = MagicMock(return_value=False)
        mock_http.get.return_value = mock_resp
        mock_client_cls.return_value = mock_http

        result = _invoke(["feedback", "This should get a 400"])
        assert result.exit_code != 0
        assert "400" in result.output

    @patch("ramp_cli.commands.feedback.httpx.Client")
    @patch("ramp_cli.commands.feedback.store")
    def test_network_error(self, mock_store, mock_client_cls, isolated_config):
        mock_store.has_tokens.return_value = False
        mock_http = MagicMock()
        mock_http.__enter__ = MagicMock(return_value=mock_http)
        mock_http.__exit__ = MagicMock(return_value=False)
        mock_http.get.side_effect = httpx.ConnectError("connection refused")
        mock_client_cls.return_value = mock_http

        result = _invoke(["feedback", "This should fail with network error"])
        assert result.exit_code != 0
        assert "Network error" in result.output


class TestFeedbackQuiet:
    @patch("ramp_cli.commands.feedback.httpx.Client")
    @patch("ramp_cli.commands.feedback.store")
    def test_quiet_suppresses_output(
        self, mock_store, mock_client_cls, isolated_config
    ):
        mock_store.has_tokens.return_value = False
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_http = MagicMock()
        mock_http.__enter__ = MagicMock(return_value=mock_http)
        mock_http.__exit__ = MagicMock(return_value=False)
        mock_http.get.return_value = mock_resp
        mock_client_cls.return_value = mock_http

        result = _invoke(["--quiet", "feedback", "Feedback with quiet mode enabled"])
        assert result.exit_code == 0
        assert result.output.strip() == ""
