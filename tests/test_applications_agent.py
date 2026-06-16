"""Integration coverage for generated application commands."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from click.testing import CliRunner

from ramp_cli.errors import ApiError
from ramp_cli.main import cli
from ramp_cli.tools.parser import ToolDef
from ramp_cli.tools.registry import reload


def _disable_sync(monkeypatch) -> None:
    reload("production")
    monkeypatch.setattr("ramp_cli.main.maybe_sync", lambda env, force=False: None)
    monkeypatch.setattr(
        "ramp_cli.tools.commands.maybe_sync",
        lambda env, force=False: None,
    )


def test_application_group_combines_create_and_generated_aliases(
    isolated_config, monkeypatch
):
    _disable_sync(monkeypatch)

    result = CliRunner().invoke(cli, ["applications", "--help"])

    assert result.exit_code == 0
    for name in ("create", "progress", "edit", "upload", "followups", "accounts"):
        assert name in result.output
    assert "patch_application_update_resource" not in result.output


def test_generated_command_discovery_respects_environment(isolated_config, monkeypatch):
    _disable_sync(monkeypatch)
    sandbox_tool = ToolDef(
        name="sandbox_progress",
        alias="sandbox-progress",
        category="applications",
        path="/developer/v1/applications/progress",
        http_method="get",
        summary="Sandbox application progress",
        description="Get sandbox application progress.",
    )
    requested_envs = []

    def fake_list_tool_defs(env):
        requested_envs.append(env)
        return [sandbox_tool] if env == "sandbox" else []

    monkeypatch.setattr(
        "ramp_cli.tools.commands.list_tool_defs",
        fake_list_tool_defs,
    )

    result = CliRunner().invoke(
        cli,
        ["--env", "sandbox", "applications", "sandbox-progress", "--help"],
    )

    assert result.exit_code == 0
    assert requested_envs
    assert set(requested_envs) == {"sandbox"}


def test_application_edit_preserves_partial_body(isolated_config, monkeypatch):
    _disable_sync(monkeypatch)
    request = MagicMock(return_value=b'{"business":{"business_name_legal":"Acme LLC"}}')
    monkeypatch.setattr("ramp_cli.client.api.RampClient.patch", request)

    result = CliRunner().invoke(
        cli,
        [
            "--agent",
            "applications",
            "edit",
            "--json",
            '{"business":{"business_name_legal":"Acme LLC"}}',
        ],
    )

    assert result.exit_code == 0
    path, body = request.call_args.args
    assert path == "/developer/v1/applications"
    assert json.loads(body) == {"business": {"business_name_legal": "Acme LLC"}}


def test_document_upload_omits_unset_multipart_fields(
    tmp_path, isolated_config, monkeypatch
):
    _disable_sync(monkeypatch)
    document = tmp_path / "statement.pdf"
    document.write_bytes(b"%PDF-test")
    captured = {}

    def fake_post_multipart(self, path, data, files):
        filename, file_obj, content_type = files["file"]
        captured.update(
            path=path,
            data=data,
            filename=filename,
            contents=file_obj.read(),
            content_type=content_type,
        )
        return b'{"id":"doc-1","purpose":"APPLICATION"}'

    monkeypatch.setattr(
        "ramp_cli.client.api.RampClient.post_multipart",
        fake_post_multipart,
    )

    result = CliRunner().invoke(
        cli,
        [
            "--agent",
            "applications",
            "upload",
            "--file",
            str(document),
            "--purpose",
            "FOLLOWUP",
            "--followup_id",
            "followup-1",
        ],
    )

    assert result.exit_code == 0
    assert captured == {
        "path": "/developer/v1/applications/documents",
        "data": {"purpose": "FOLLOWUP", "followup_id": "followup-1"},
        "filename": "statement.pdf",
        "contents": b"%PDF-test",
        "content_type": "application/pdf",
    }


def test_delete_document_supports_path_ids_and_empty_204(isolated_config, monkeypatch):
    _disable_sync(monkeypatch)
    request = MagicMock(return_value=b"")
    monkeypatch.setattr("ramp_cli.client.api.RampClient.delete", request)

    result = CliRunner().invoke(
        cli,
        ["--agent", "applications", "delete-document", "doc/id"],
    )

    assert result.exit_code == 0
    request.assert_called_once_with(
        "/developer/v1/applications/documents/doc%2Fid",
        None,
    )
    assert json.loads(result.output)["data"] == [{}]


def test_document_pagination_returns_reusable_start_cursor(
    isolated_config, monkeypatch
):
    _disable_sync(monkeypatch)
    next_url = "https://api.ramp.com/developer/v1/applications/documents?start=2"
    request = MagicMock(
        return_value=json.dumps(
            {"data": [{"id": "doc-1"}], "page": {"next": next_url}}
        ).encode()
    )
    monkeypatch.setattr("ramp_cli.client.api.RampClient.get", request)

    result = CliRunner().invoke(
        cli,
        ["--agent", "applications", "documents", "--page_size", "2"],
    )

    assert result.exit_code == 0
    request.assert_called_once_with(
        "/developer/v1/applications/documents",
        {"page_size": "2"},
    )
    assert json.loads(result.output)["pagination"]["next_cursor"] == "2"


def test_developer_api_validation_details_are_preserved():
    error = ApiError(
        400,
        json.dumps(
            {
                "message": "Application update failed validation",
                "errors": [
                    {
                        "field": "business.incorporation.ein_number",
                        "message": "Invalid EIN",
                    }
                ],
            }
        ),
    )

    message = str(error)
    assert "Application update failed validation" in message
    assert "business.incorporation.ein_number" in message
    assert "Invalid EIN" in message
