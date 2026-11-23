"""Shared test fixtures."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

from ramp_cli import codex_hook_trust
from ramp_cli.commands import cursor_models, router
from ramp_cli.config.settings import config_dir

# Coding-agent and helper binaries the CLI looks up on PATH and then runs or
# reads. A developer's real installs must stay invisible to tests; fakes a test
# writes under its own tmp_path are still found.
_HARNESS_EXECUTABLES = frozenset(
    {"claude", "codex", "cursor", "hermes", "opencode", "pi", "cloudflared"}
)
_REAL_WHICH = shutil.which


def _keep_tmp_out_of_the_windows_profile() -> None:
    """Root pytest's temp directories outside the user's profile on Windows.

    %TEMP% lives under %USERPROFILE% there, so code that walks up from a test's
    working directory (skill directory detection, for one) reaches the
    developer's real home and finds their real ~/.codex/skills. Only applies
    when neither --basetemp nor PYTEST_DEBUG_TEMPROOT already chose a root.
    """
    if sys.platform != "win32" or os.environ.get("PYTEST_DEBUG_TEMPROOT"):
        return
    temp = Path(tempfile.gettempdir()).resolve()
    if not temp.is_relative_to(Path.home().resolve()):
        return
    public = Path(os.environ.get("PUBLIC") or r"C:\Users\Public")
    os.environ["PYTEST_DEBUG_TEMPROOT"] = str(public / "ramp-cli-pytest")
    (public / "ramp-cli-pytest").mkdir(parents=True, exist_ok=True)


_keep_tmp_out_of_the_windows_profile()


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "posix_only(reason): skip on Windows; the behavior under test only "
        "exists on macOS or Linux (launchd, systemd, fcntl, Unix sockets, ...)",
    )
    config.addinivalue_line(
        "markers",
        "real_harness_binaries: let shutil.which find coding-agent binaries "
        "installed outside the test's tmp_path",
    )


_WINDOWS_KNOWN_FAILURES = Path(__file__).with_name("windows_known_failures.txt")


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    if sys.platform != "win32":
        return
    lines = _WINDOWS_KNOWN_FAILURES.read_text().splitlines()
    known = {line.split("#", 1)[0].strip() for line in lines} - {""}
    for item in items:
        marker = item.get_closest_marker("posix_only")
        if marker is not None:
            reason = marker.kwargs.get("reason", "requires macOS or Linux")
            item.add_marker(pytest.mark.skip(reason=f"POSIX only: {reason}"))
            continue
        # "tests/test_x.py::Class::test_y[param]": list the whole test or one case.
        test_id = item.nodeid.rsplit("/", 1)[-1]
        if test_id in known or test_id.split("[", 1)[0] in known:
            item.add_marker(
                pytest.mark.skip(
                    reason="known Windows failure, see " + _WINDOWS_KNOWN_FAILURES.name
                )
            )


def _codex_trust_unavailable(*_args, **_kwargs):
    raise codex_hook_trust.TrustUnavailable("not in tests")


@pytest.fixture(autouse=True)
def isolated_config(
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
    request: pytest.FixtureRequest,
):
    """Redirect config to a temp directory so tests don't touch real config.

    Every coding agent this CLI configures keeps its files somewhere under the
    user's home and reads an environment variable to find them. Isolating only
    XDG_CONFIG_HOME covered OpenCode and left the rest pointing at the real
    home, so a test that configured every agent wrote its fixture credential
    into the developer's own Codex and Pi setup.
    """
    # A test may undo its own patches without removing this safety boundary.
    monkeypatch = pytest.MonkeyPatch()
    request.addfinalizer(monkeypatch.undo)
    # Service removal can bypass the public install/uninstall wrappers.
    # Isolate its filesystem paths AND OS calls for every test, including
    # tests invoking agent disable directly. Lifecycle tests supply fakes.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("ramp_cli.commands.router_schedule._run", lambda *_args: False)
    # Reading Claude Code's built-in default starts the developer's real
    # `claude`. Tests that depend on it set their own answer.
    monkeypatch.setattr(
        "ramp_cli.commands.claude_code_default.builtin_default_model",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "ramp_cli.commands.codex_default.builtin_default_model",
        lambda *_args, **_kwargs: None,
    )
    # Trusting Router's Codex hooks starts the developer's real `codex
    # app-server`. Tests that exercise trust supply their own Codex.
    monkeypatch.setattr(
        "ramp_cli.codex_hook_trust.trust_router_hooks", _codex_trust_unavailable
    )
    # Router's fallback rides on the coding-agent settings read; tests that
    # exercise it set their own answer.
    monkeypatch.setattr(
        "ramp_cli.admin_defaults.fallback_model", lambda *_args, **_kwargs: None
    )
    router._bundled_codex_catalog_from.cache_clear()
    # Codex discovers personal skills in ~/.agents independently of CODEX_HOME.
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    # On Windows Path.home() reads USERPROFILE, not HOME, and per-user app
    # state lives under APPDATA and LOCALAPPDATA. Set them everywhere so the
    # sandbox does not depend on which variable a code path happens to read.
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("HOMEDRIVE", raising=False)
    monkeypatch.delenv("HOMEPATH", raising=False)
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(home / "AppData" / "Local"))
    # Running the developer's real coding agents (codex, claude, ...) reads
    # their real defaults and state. Fakes under the test's temp root still
    # resolve; a test that needs a real install opts in with a marker.
    if request.node.get_closest_marker("real_harness_binaries") is None:
        sandbox = tmp_path_factory.getbasetemp().resolve()

        def sandboxed_which(cmd, mode=os.F_OK | os.X_OK, path=None):
            found = _REAL_WHICH(cmd, mode=mode, path=path)
            if found is None:
                return None
            name = Path(os.fspath(cmd)).name.lower()
            stem = name.rsplit(".", 1)[0] if name.endswith((".exe", ".cmd")) else name
            if stem not in _HARNESS_EXECUTABLES:
                return found
            if Path(found).resolve().is_relative_to(sandbox):
                return found
            return None

        monkeypatch.setattr(shutil, "which", sandboxed_which)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    # Explicit OpenCode paths take precedence over the isolated XDG directory.
    monkeypatch.delenv("OPENCODE_CONFIG", raising=False)
    monkeypatch.delenv("OPENCODE_TUI_CONFIG", raising=False)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "pi"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    # Claude Desktop state lives under ~/Library/Application Support and is
    # reached through Path.home(), so without this a test that configures or
    # tears down every agent edits the developer's real Cowork setup.
    monkeypatch.setenv("RAMP_CLAUDE_DESKTOP_APP_SUPPORT", str(tmp_path / "app-support"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path / "conductor"))
    monkeypatch.setenv("RAMP_ANTIGRAVITY_GEMINI_DIR", str(tmp_path / "gemini"))
    monkeypatch.setenv(
        "RAMP_ANTIGRAVITY_APP_SUPPORT", str(tmp_path / "antigravity-support")
    )
    monkeypatch.setattr("ramp_cli.commands.antigravity.is_running", lambda: False)
    monkeypatch.setattr(
        "ramp_cli.commands.antigravity.runs_inside_antigravity",
        lambda _pids=None: False,
    )
    monkeypatch.setattr(
        "ramp_cli.commands.antigravity.restart_running", lambda **_kwargs: False
    )
    # Conductor detection consults its application-support directory; pointing
    # it into the sandbox keeps a developer's real Conductor invisible and
    # gives tests full control over what "installed" means.
    monkeypatch.setenv(
        "RAMP_CONDUCTOR_APP_SUPPORT", str(tmp_path / "conductor-app-support")
    )
    # Cursor detection reads its user-data directory and state database.
    monkeypatch.setenv("RAMP_CURSOR_USER_DIR", str(tmp_path / "cursor-user"))
    # A developer's open Cursor must not change what setup does, and setup
    # must never quit or reopen it. Tests that need a running Cursor say so.
    monkeypatch.setattr("ramp_cli.commands.cursor.running_pids", lambda: [])
    monkeypatch.setattr("ramp_cli.commands.cursor.runs_inside_cursor", lambda: False)
    monkeypatch.setattr("ramp_cli.commands.cursor.reopen", lambda: True)
    # Setup reads Cursor's key from the keychain before quitting it; tests
    # that need that read, or its refusal, say so. The key is never kept
    # from one test to the next.
    monkeypatch.setattr("ramp_cli.commands.cursor.unlock", lambda prompt=True: None)
    monkeypatch.setattr("ramp_cli.commands.cursor._encryption_key", None)
    # Cursor setup asks which models to list; a test that types its own
    # answers gets Enter there: the saved choice, or the recommended list.
    monkeypatch.setattr(
        "ramp_cli.commands.router._pick_cursor_model_scope",
        lambda previous: (
            previous or cursor_models.ModelScope(view=cursor_models.VIEW_COMPACT)
        ),
    )
    # A RouterService pins the process to its Router origin; reset it so one
    # test's origin never decides where the next one's requests go.
    monkeypatch.setattr("ramp_cli.commands.router._session_origin", None)
    # Cloudflare Access tokens come from the developer's own cloudflared and
    # ~/.cloudflared, and detected edges are remembered per process.
    monkeypatch.setattr(
        "ramp_cli.client.router_user._edge_token", lambda _o, **_kw: None
    )
    monkeypatch.setattr("ramp_cli.client.router_user._edge_origins", set())
    monkeypatch.setattr("ramp_cli.client.router_user._edge_rejected", {})
    # Preserve the app names and locations under a sandbox root so detection
    # tests exercise the real search paths without finding a developer's apps.
    monkeypatch.setattr(
        "ramp_cli.commands.router._CODEX_APP_LOCATIONS",
        tuple(
            tmp_path / "apps" / path.relative_to(path.anchor)
            for path in router._CODEX_APP_LOCATIONS
        ),
    )
    # Configuring a fixture must not quit the developer's desktop apps.
    monkeypatch.setattr("ramp_cli.codex_desktop.restart_running", lambda: [])
    # Hermes is configured through its own `hermes config` executable rather
    # than direct file writes, so a developer with Hermes installed would
    # otherwise have tests drive the real binary against their real setup.
    # Tests that exercise the Hermes client re-point this at a test double.
    monkeypatch.setattr("ramp_cli.hermes_agent.hermes_executable", lambda: None)
    # OpenCode configure asks the installed binary which generation it is, so
    # a developer with OpenCode on PATH would otherwise have tests run the real
    # binary and write whichever layout it reports. Tests that exercise the v2
    # layout re-point this at a test double or set RAMP_OPENCODE_MAJOR.
    monkeypatch.setattr("ramp_cli.commands.router._opencode_executable", lambda: None)
    # The CLI reads these, so a developer pointing their own shell at a local
    # stack would otherwise change what the tests assert.
    for leaked in (
        "RAMP_ROUTER_BASE_URL",
        "RAMP_ROUTER_UI_URL",
        "RAMP_ROUTER_ACCESS_ORIGIN",
        "RAMP_ROUTER_ACCESS_TOKEN",
        "LLM_GATEWAY_BASE_URL",
        "RAMP_ROUTER_CONFIGURE_API_KEY",
        "RAMP_ACCESS_TOKEN",
        "RAMP_AGENT_WALLET_API_URL",
        "RAMP_OPENCODE_MAJOR",
        # Claude Code configure reads the shell's auto-compact window to decide
        # whether the settings file must override it.
        "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
    ):
        monkeypatch.delenv(leaked, raising=False)
    monkeypatch.setenv("RAMP_NO_TOOL_AVAILABILITY", "1")
    monkeypatch.setattr("ramp_cli.tools.availability._cache", {})
    # Configure/refresh/unconfigure manage an OS scheduled job (launchd or
    # systemd); tests must never install one on the developer's machine.
    monkeypatch.setattr(
        "ramp_cli.commands.router_schedule.install", lambda **_kwargs: None
    )
    monkeypatch.setattr("ramp_cli.commands.router_schedule.uninstall", lambda: None)
    # Repository tagging rewrites the same always-on agent service.
    monkeypatch.setattr("ramp_cli.commands.router_repo.install_service", lambda: None)
    monkeypatch.setattr("ramp_cli.commands.router_repo.uninstall_service", lambda: None)
    monkeypatch.setattr("ramp_cli.commands.router_schedule.update_agent", lambda: None)
    # A development agent build in the developer's shell must not be picked up.
    monkeypatch.delenv("RAMP_ROUTER_AGENT", raising=False)
    # The job's locks live under the real home, not XDG_CONFIG_HOME.
    monkeypatch.setattr(
        "ramp_cli.commands.router_sync.job_lock_dir", lambda: tmp_path / "job-locks"
    )
    # Pin the resolved ramp entrypoint so hook commands in fixtures are
    # deterministic regardless of how the tests were launched.
    monkeypatch.setattr(
        "ramp_cli.commands.router_sync.ramp_executable",
        lambda: Path("/opt/ramp-cli/bin/ramp"),
    )
    # Hook-mode sync suppresses the shutdown update notice; only main() —
    # which CliRunner never reaches — resets the flag, so clear it here.
    monkeypatch.setattr("ramp_cli.version_check._SUPPRESS_NEXT_UPDATE_NOTICE", False)
    # Router's coding-agent settings are cached per process; one test's
    # Router must not answer the next one's configure.
    monkeypatch.setattr("ramp_cli.admin_defaults._cache", {})
    monkeypatch.setattr("ramp_cli.admin_defaults._pinned", {})
    monkeypatch.setattr("ramp_cli.config.settings.default_environment", lambda: "")

    def refresh_connector_catalog():
        root = config_dir() / "skills" / "v-connectors-test"
        skill = root / "prefer-ramp-connectors" / "SKILL.md"
        skill.parent.mkdir(parents=True, exist_ok=True)
        skill.write_text(
            "---\nname: ramp-prefer-ramp-connectors\n"
            "description: Prefer connected service tools.\n---\n"
            "# Prefer ramp-connectors MCP\n"
            "Use published connector capabilities for Slack and other connected services.\n"
        )
        (root.parent / "active.json").write_text(json.dumps({"version": root.name}))
        return True

    # Configure tests exercise installation without contacting GitHub; release
    # download and verification have their own HTTP-mocked integration coverage.
    monkeypatch.setattr(
        "ramp_cli.router_connectors.skill.refresh", refresh_connector_catalog
    )
    yield tmp_path
