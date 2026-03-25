"""Tests for skill discovery, listing, showing, and installing."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from ramp_cli.main import cli
from ramp_cli.skills import detect_agent_dir, install_skill, skill_names


class TestSkillDiscovery:
    def test_skill_names_discovers_all(self):
        """All 6 skills should be discovered from the skills/ directory."""
        names = skill_names()
        assert len(names) == 6
        assert "agentic-purchase" in names
        assert "browser-automation" in names
        assert "approval-dashboard" in names
        assert "receipt-compliance" in names
        assert "transaction-cleanup" in names
        assert "apply-to-ramp" in names

    def test_skill_names_missing_dir(self, monkeypatch):
        """Returns empty list when skills dir does not exist."""
        monkeypatch.setattr("ramp_cli.skills.SKILLS_DIR", Path("/nonexistent/path"))

        # Re-import won't help since SKILLS_DIR is module-level, use the monkeypatched value
        assert skill_names() == []  # noqa: this still uses the original import but SKILLS_DIR is patched


class TestSkillsList:
    def test_list_skills_json(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["--agent", "skills", "list"])
        assert result.exit_code == 0
        import json

        data = json.loads(result.output)
        assert len(data["data"]) == 6
        names = {s["name"] for s in data["data"]}
        assert "browser-automation" in names
        assert "agentic-purchase" in names

    def test_list_skills_human(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["--human", "skills", "list"])
        assert result.exit_code == 0
        assert "6 Skills" in result.output
        assert "browser-automation" in result.output


class TestSkillsShow:
    def test_show_skill(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["skills", "show", "browser-automation"])
        assert result.exit_code == 0
        assert "Browser Automation" in result.output
        assert "playwright-cli" in result.output

    def test_show_skill_not_found(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["skills", "show", "nonexistent"])
        assert result.exit_code != 0


class TestSkillsInstall:
    def test_install_single(self, tmp_path):
        """Install one skill to a tmp directory."""
        status = install_skill("browser-automation", tmp_path)
        assert status == "installed"
        dest = tmp_path / "browser-automation" / "SKILL.md"
        assert dest.is_file()
        assert "Browser Automation" in dest.read_text()

    def test_install_all(self, tmp_path):
        """--all installs all 6 skills."""
        runner = CliRunner()
        result = runner.invoke(
            cli, ["skills", "install", "--all", "--target", str(tmp_path)]
        )
        assert result.exit_code == 0
        installed = [d.name for d in tmp_path.iterdir() if d.is_dir()]
        assert len(installed) == 6

    def test_install_overwrites(self, tmp_path):
        """Installing twice succeeds and returns 'updated' on second run."""
        install_skill("browser-automation", tmp_path)
        status = install_skill("browser-automation", tmp_path)
        assert status == "updated"
        dest = tmp_path / "browser-automation" / "SKILL.md"
        assert dest.is_file()

    def test_install_injects_user_invocable(self, tmp_path):
        """Install to a .claude/skills/ target injects user-invocable: true."""
        claude_skills = tmp_path / ".claude" / "skills"
        claude_skills.mkdir(parents=True)
        install_skill("browser-automation", claude_skills)
        content = (claude_skills / "browser-automation" / "SKILL.md").read_text()
        assert "user-invocable: true" in content

    def test_install_no_inject_for_other_targets(self, tmp_path):
        """Install to a non-.claude target does not inject user-invocable."""
        install_skill("browser-automation", tmp_path)
        content = (tmp_path / "browser-automation" / "SKILL.md").read_text()
        assert "user-invocable" not in content

    def test_install_no_skills_dir(self, monkeypatch):
        """Error when skills directory is missing."""
        fake = Path("/nonexistent/path")
        monkeypatch.setattr("ramp_cli.skills.SKILLS_DIR", fake)
        monkeypatch.setattr("ramp_cli.commands.skills.SKILLS_DIR", fake)
        runner = CliRunner()
        result = runner.invoke(
            cli, ["skills", "install", "--all", "--target", "/tmp/test"]
        )
        assert result.exit_code != 0

    def test_install_requires_name_or_all(self):
        """No args should produce a usage error."""
        runner = CliRunner()
        result = runner.invoke(cli, ["skills", "install"])
        assert result.exit_code != 0
        assert "Provide a skill name or use --all" in result.output


class TestDetectAgentDir:
    def test_detect_finds_claude_dir(self, tmp_path, monkeypatch):
        """Detects .claude/skills/ when it exists."""
        claude_dir = tmp_path / ".claude" / "skills"
        claude_dir.mkdir(parents=True)
        monkeypatch.chdir(tmp_path)
        result = detect_agent_dir()
        assert result == claude_dir

    def test_detect_returns_none(self, tmp_path, monkeypatch):
        """Returns None when no agent dir exists."""
        monkeypatch.chdir(tmp_path)
        result = detect_agent_dir()
        assert result is None
