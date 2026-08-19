"""Tests for packaging bundled Router integrations."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_local_binary_build_bundles_integration_sources_without_node():
    script = (ROOT / "scripts" / "build.sh").read_text()

    assert "build-router-integrations.sh" not in script
    assert "--include-data-dir=src/ramp_cli/router_integrations/packages=" in script
    assert "--include-package-data=ramp_cli" not in script


def test_clients_load_checked_in_typescript_sources():
    packages = ROOT / "src" / "ramp_cli" / "router_integrations" / "packages"
    opencode = json.loads((packages / "opencode-provider" / "package.json").read_text())
    pi = json.loads((packages / "pi-provider" / "package.json").read_text())

    assert opencode["exports"]["."] == "./src/index.ts"
    assert pi["pi"]["extensions"] == ["./src/index.ts"]


def test_standard_wheel_build_packages_sources_without_node():
    pyproject = (ROOT / "pyproject.toml").read_text()
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()

    assert "[tool.hatch.build.hooks.custom]" not in pyproject
    assert "opencode-provider/src" in pyproject
    assert "pi-provider/src" in pyproject
    assert "[tool.hatch.build.targets.sdist.force-include]" in pyproject
    assert "Build source distribution from clean checkout" in workflow
    assert "Build wheel from source distribution" in workflow
    wheel_job = workflow.split("  wheel:", 1)[1]
    assert "setup-node" not in wheel_job


def test_release_build_does_not_need_node():
    workflow = (ROOT / ".github" / "workflows" / "build-binaries.yml").read_text()

    assert "setup-node" not in workflow
    assert "build-router-integrations.sh" not in workflow
    assert "--include-package-data=ramp_cli" not in workflow


def test_windows_build_does_not_block_release():
    workflow = (ROOT / ".github" / "workflows" / "build-binaries.yml").read_text()
    release_job = workflow.split("  release:", 1)[1].split("  upload-windows:", 1)[0]
    windows_job = workflow.split("  build-windows:", 1)[1].split("  release:", 1)[0]

    assert "    needs: build" in release_job.splitlines()
    assert "windows-amd64" not in release_job
    assert "--prerelease" in release_job
    assert "needs: [build-windows, release]" in workflow
    assert "ramp-windows-amd64" in windows_job
    assert "--prerelease=false" in workflow
    assert "--latest" in workflow


def test_generated_router_integration_outputs_remain_ignored():
    gitignore = (ROOT / ".gitignore").read_text()

    assert "dist/" in gitignore
    assert "!src/ramp_cli/router_integrations/packages/*/dist" not in gitignore
