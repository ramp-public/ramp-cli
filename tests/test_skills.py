"""Tests for skill discovery, listing, showing, and installing."""

from __future__ import annotations

import json
import re

from click.testing import CliRunner

from ramp_cli.main import CATEGORY_REMAP, cli
from ramp_cli.skills import (
    SKILLS_DIR,
    detect_agent_dir,
    get_skill_content,
    install_skill,
    skill_names,
)
from ramp_cli.specs import AGENT_TOOL_SPEC
from ramp_cli.tools.parser import parse_spec


class TestSkillDiscovery:
    def test_skill_names_discovers_all(self):
        """All 8 skills should be discovered from the skills/ directory."""
        names = skill_names()
        assert len(names) == 8
        assert "agentic-purchase" in names
        assert "browser-automation" in names
        assert "approval-dashboard" in names
        assert "receipt-compliance" in names
        assert "submit-reimbursement" in names
        assert "transaction-cleanup" in names
        assert "apply-to-ramp" in names
        assert "vendor-document-upload" in names

    def test_skill_names_empty_dir(self, tmp_path, monkeypatch):
        """Returns empty list when skills dir has no skill subdirectories."""
        monkeypatch.setattr("ramp_cli.skills.SKILLS_DIR", tmp_path)
        assert skill_names() == []


class TestSkillsList:
    def test_list_skills_json(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["--agent", "skills", "list"])
        assert result.exit_code == 0

        data = json.loads(result.output)
        assert len(data["data"]) == 8
        names = {s["name"] for s in data["data"]}
        assert "browser-automation" in names
        assert "agentic-purchase" in names
        assert "vendor-document-upload" in names

    def test_list_skills_human(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["--human", "skills", "list"])
        assert result.exit_code == 0
        assert "8 Skills" in result.output
        assert "browser-automation" in result.output
        assert "vendor-document-upload" in result.output


class TestSkillsShow:
    def test_show_skill(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["skills", "show", "browser-automation"])
        assert result.exit_code == 0
        assert "Browser Automation" in result.output
        assert "playwright-cli" in result.output

    def test_show_vendor_document_upload_skill(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["skills", "show", "vendor-document-upload"])
        assert result.exit_code == 0
        assert "Upload vendor documents" in result.output
        assert "ramp vendors attach-document" in result.output

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
        """--all installs all 7 skills."""
        runner = CliRunner()
        result = runner.invoke(
            cli, ["skills", "install", "--all", "--target", str(tmp_path)]
        )
        assert result.exit_code == 0
        installed = [d.name for d in tmp_path.iterdir() if d.is_dir()]
        assert len(installed) == 8

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

    def test_install_empty_skills_dir(self, tmp_path, monkeypatch):
        """No skills available when SKILLS_DIR is empty."""
        monkeypatch.setattr("ramp_cli.skills.SKILLS_DIR", tmp_path)
        runner = CliRunner()
        result = runner.invoke(
            cli, ["skills", "install", "browser-automation", "--target", str(tmp_path)]
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


class TestSkillsBundled:
    """Verify SKILLS_DIR resolves to a directory containing all skills."""

    def test_skills_dir_has_skill_content(self):
        """SKILLS_DIR should contain subdirectories with SKILL.md files."""
        assert SKILLS_DIR.is_dir()
        assert any(SKILLS_DIR.glob("*/SKILL.md"))

    def test_all_skills_have_skill_md(self):
        """Every discovered skill should have a SKILL.md file."""
        for name in skill_names():
            skill_file = SKILLS_DIR / name / "SKILL.md"
            assert skill_file.is_file(), f"{name}/SKILL.md missing from {SKILLS_DIR}"


# ── Tool reference validation ────────────────────────────────────────────────

# Regex to extract `ramp <category> <alias>` from command-like lines.
# Matches lines where `ramp` appears at the start (after optional whitespace,
# prompt chars like `>` or `$`, or backticks) — skips prose mentions.
_RAMP_CMD_RE = re.compile(r"(?:^[\s>`$]*|^\s*\|?\s*)ramp\s+([\w][\w-]*)\s+([\w][\w-]*)")

# Regex to extract --flag_name from a line.
_FLAG_RE = re.compile(r"--([\w]+)")

# Hand-written commands (not from the OpenAPI tool registry).
# Value of None means the second token is an argument, not a subcommand.
HAND_WRITTEN_COMMANDS: dict[str, set[str] | None] = {
    "applications": {"create", "schema", "list", "get", "delete"},
    "auth": {"login", "logout", "status", "switch"},
    "config": {"show", "set", "unset", "path"},
    "env": {"sandbox", "production"},
    "feedback": None,
    "skills": {"list", "show", "install"},
    "tools": {"refresh"},
}

SKILL_COMMAND_REFERENCES = {
    ("vendors", "attach-document"),
    ("vendors", "bulk-upload"),
    ("vendors", "bulk-upload-status"),
}

# Global CLI flags that appear on every command.
GLOBAL_FLAGS = {
    "agent",
    "dry_run",
    "env",
    "example",
    "help",
    "human",
    "json",
    "n",
    "no_input",
    "output",
    "page_size",
    "quiet",
    "wide",
}


def _build_valid_commands() -> set[tuple[str, str]]:
    """Build the set of valid (cli_group, alias) pairs.

    Mirrors the category remapping and singleton→general folding
    from ``RampCLI._split_categories`` in ``main.py``.
    """
    tools = parse_spec(AGENT_TOOL_SPEC)

    # Group by CLI-visible category after remapping.
    merged: dict[str, list] = {}
    for t in tools:
        cli_cat = (
            CATEGORY_REMAP.get(t.category, t.category) if t.category else "general"
        )
        merged.setdefault(cli_cat, []).append(t)

    valid: set[tuple[str, str]] = set()
    for cat, cat_tools in merged.items():
        if len(cat_tools) > 1:
            for t in cat_tools:
                if t.alias:
                    valid.add((cat, t.alias))
        else:
            # Singletons fold into "general"
            for t in cat_tools:
                alias = t.alias or t.name
                valid.add(("general", alias))

    return valid | SKILL_COMMAND_REFERENCES


def _build_tool_param_index() -> dict[tuple[str, str], set[str]]:
    """Map (cli_group, alias) → set of valid parameter names."""
    tools = parse_spec(AGENT_TOOL_SPEC)

    merged: dict[str, list] = {}
    for t in tools:
        cli_cat = (
            CATEGORY_REMAP.get(t.category, t.category) if t.category else "general"
        )
        merged.setdefault(cli_cat, []).append(t)

    index: dict[tuple[str, str], set[str]] = {}
    for cat, cat_tools in merged.items():
        if len(cat_tools) > 1:
            for t in cat_tools:
                if t.alias:
                    key = (cat, t.alias)
                    # Merge params when multiple tools share (category, alias).
                    index.setdefault(key, set()).update(p.name for p in t.params)
        else:
            for t in cat_tools:
                alias = t.alias or t.name
                key = ("general", alias)
                index.setdefault(key, set()).update(p.name for p in t.params)

    return index


def _join_continued_lines(content: str) -> list[str]:
    """Join backslash-continued lines into single logical lines."""
    logical_lines: list[str] = []
    current = ""
    for line in content.splitlines():
        stripped = line.rstrip()
        if stripped.endswith("\\"):
            current += stripped[:-1] + " "
        else:
            current += stripped
            logical_lines.append(current)
            current = ""
    if current:
        logical_lines.append(current)
    return logical_lines


def _extract_ramp_commands(content: str) -> list[tuple[str, str, str]]:
    """Extract (category, alias, full_line) from skill markdown content."""
    results = []
    for line in _join_continued_lines(content):
        for m in _RAMP_CMD_RE.finditer(line):
            results.append((m.group(1), m.group(2), line.strip()))
    return results


class TestSkillToolReferences:
    """Validate that all ramp CLI invocations in SKILL.md files reference real tools."""

    def test_all_skill_tool_references_are_valid(self):
        """Every `ramp <category> <alias>` in SKILL.md must map to a real tool or command."""
        valid_commands = _build_valid_commands()
        errors: list[str] = []

        for name in skill_names():
            content = get_skill_content(name)
            if content is None:
                continue

            for category, alias, line in _extract_ramp_commands(content):
                # Check hand-written commands first.
                if category in HAND_WRITTEN_COMMANDS:
                    allowed = HAND_WRITTEN_COMMANDS[category]
                    if allowed is None or alias in allowed:
                        continue

                # Check tool registry.
                if (category, alias) not in valid_commands:
                    errors.append(
                        f"  [{name}] invalid tool: ramp {category} {alias}\n"
                        f"    line: {line}"
                    )

        assert not errors, (
            f"Found {len(errors)} invalid tool reference(s) in skills:\n"
            + "\n".join(errors)
        )

    def test_all_skill_flag_references_are_valid(self):
        """Flags in tool invocations should match actual tool params or global flags."""
        param_index = _build_tool_param_index()
        errors: list[str] = []

        for name in skill_names():
            content = get_skill_content(name)
            if content is None:
                continue

            for category, alias, line in _extract_ramp_commands(content):
                key = (category, alias)
                if key not in param_index:
                    continue  # hand-written commands tested elsewhere

                valid_params = param_index[key]
                flags = set(_FLAG_RE.findall(line))
                for flag in sorted(flags):
                    if flag in GLOBAL_FLAGS:
                        continue
                    if flag not in valid_params:
                        errors.append(
                            f"  [{name}] unknown flag --{flag} for "
                            f"ramp {category} {alias}\n"
                            f"    valid params: {sorted(valid_params)}\n"
                            f"    line: {line}"
                        )

        assert not errors, (
            f"Found {len(errors)} invalid flag reference(s) in skills:\n"
            + "\n".join(errors)
        )
