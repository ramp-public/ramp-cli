"""Tests for skill discovery, listing, showing, and installing."""

from __future__ import annotations

import json
import re

from click.testing import CliRunner

from ramp_cli.commands.applications import APPLICATION_EXAMPLE
from ramp_cli.main import (
    _SINGLE_TOOL_RESOURCE_CATEGORIES,
    CATEGORY_ALIAS_GROUPS,
    CATEGORY_LEGACY_GROUPS,
    CATEGORY_REMAP,
    cli,
)
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
        """All 16 skills should be discovered from the skills/ directory."""
        names = skill_names()
        assert len(names) == 16
        assert "get-started" in names
        assert "agentic-purchase" in names
        assert "card-management" in names
        assert "book-flight" in names
        assert "browser-automation" in names
        assert "approval-dashboard" in names
        assert "manage-procurement" in names
        assert "manage-bills" in names
        assert "receipt-compliance" in names
        assert "submit-reimbursement" in names
        assert "transaction-cleanup" in names
        assert "apply-to-ramp" in names
        assert "incorporate-with-ramp" in names
        assert "vendor-document-upload" in names
        assert "payment-lookup" in names
        assert "spend-analysis" in names

    def test_readme_lists_all_skills(self):
        """The public skill index should mention every bundled skill."""
        readme = (SKILLS_DIR / "README.md").read_text()
        for name in skill_names():
            assert f"`{name}`" in readme

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
        assert len(data["data"]) == 16
        names = {s["name"] for s in data["data"]}
        assert "get-started" in names
        assert "browser-automation" in names
        assert "card-management" in names
        assert "agentic-purchase" in names
        assert "incorporate-with-ramp" in names
        assert "book-flight" in names
        assert "manage-procurement" in names
        assert "manage-bills" in names
        assert "vendor-document-upload" in names

    def test_list_skills_human(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["--human", "skills", "list"])
        assert result.exit_code == 0
        assert "16 Skills" in result.output
        assert "browser-automation" in result.output
        assert "manage-procurement" in result.output
        assert "manage-bills" in result.output
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

    def test_show_all_skills_returns_content(self):
        """Every discovered skill should be fetchable via skills show."""
        runner = CliRunner()
        for name in skill_names():
            result = runner.invoke(cli, ["skills", "show", name])
            assert result.exit_code == 0, f"skills show {name} failed"
            file_content = (SKILLS_DIR / name / "SKILL.md").read_text()
            assert result.output.strip() == file_content.strip(), (
                f"skills show {name} output doesn't match SKILL.md"
            )


class TestSkillsInstall:
    def test_install_single(self, tmp_path):
        """Install one skill to a tmp directory."""
        status = install_skill("browser-automation", tmp_path)
        assert status == "installed"
        dest = tmp_path / "browser-automation" / "SKILL.md"
        assert dest.is_file()
        assert "Browser Automation" in dest.read_text()

    def test_install_all(self, tmp_path):
        """--all installs all 16 skills."""
        runner = CliRunner()
        result = runner.invoke(
            cli, ["skills", "install", "--all", "--target", str(tmp_path)]
        )
        assert result.exit_code == 0
        installed = [d.name for d in tmp_path.iterdir() if d.is_dir()]
        assert len(installed) == 16

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

    def test_incorporate_with_ramp_skill_country_guidance(self):
        """Incorporation skill should not send agents through the countries lookup."""
        content = (SKILLS_DIR / "incorporate-with-ramp" / "SKILL.md").read_text()
        assert "ramp incorporation countries" not in content
        assert "/developer/v1/incorporation/countries" not in content
        assert "This launch flow is US-only" in content
        assert "non-US founders or country fields" in content

    def test_apply_to_ramp_progress_loop_prioritizes_incorporation(self):
        """Ramp review should not stop independently actionable incorporation work."""
        content = (SKILLS_DIR / "apply-to-ramp" / "SKILL.md").read_text()
        progress_section = self._extract_section(content, "Run The Progress Loop")

        assert progress_section.index(
            "COMPLETE_INCORPORATION"
        ) < progress_section.index("actor=RAMP")
        assert "WAIT_FOR_RAMP" in progress_section
        assert "Do not wait for Ramp approval" in progress_section
        assert "Do not start incorporation formation yet" in progress_section
        assert "ramp applications get --env production --agent" in progress_section
        assert "business.incorporation" in progress_section
        assert "REVIEW_AND_SUBMIT" in progress_section
        assert "date_of_incorporation" in progress_section
        assert "present in the live" in progress_section
        assert "Progress may omit a validation issue" in progress_section
        assert "do not treat missing progress validation" in progress_section
        assert "Do not hand off an applicant-owned browser step" in progress_section
        assert "Ramp-owned actions as stop conditions only after" in progress_section
        assert (
            "Do not use `WAIT_FOR_RAMP` alone as an incorporation signal"
            in progress_section
        )
        assert "person explicitly asked to file an LLC" in progress_section
        assert "needs_incorporation=true" in progress_section
        assert "formed entity/EIN" in progress_section
        assert "ramp skills show incorporate-with-ramp" in progress_section
        assert "same Ramp CLI binary and version" in progress_section
        assert "stale skill snapshots" in progress_section

    def test_apply_to_ramp_unformed_entity_fields_before_doola(self):
        """FA may require provisional incorporation fields before Doola filing."""
        content = (SKILLS_DIR / "apply-to-ramp" / "SKILL.md").read_text()
        section = self._extract_section(content, "Unformed Entity Incorporation Fields")
        normalized_section = " ".join(section.split())

        assert "needs_incorporation=true" in section
        assert "intended/provisional filing facts" in section
        assert "business type and state" in section
        assert "leave EIN empty/null" in section
        assert (
            "do not skip this step because Doola formation has not been submitted"
            in section
        )
        assert "after the person submits FA" in section
        assert "date_of_incorporation" in section
        assert "intended filing date" in normalized_section
        assert "whenever the current" in section
        assert "application/edit schema exposes it" in section
        assert "Ramp backfills that after approval" in normalized_section
        assert "filing state" in content

    def test_apply_to_ramp_minimizes_browser_touchpoints(self):
        """Agents should batch API-writable work before sending users to Ramp."""
        content = (SKILLS_DIR / "apply-to-ramp" / "SKILL.md").read_text()
        section = self._extract_section(content, "Minimize Browser Touchpoints")
        normalized_section = " ".join(section.split())

        assert "accepting the invite email" in section
        assert "phone verification, when progress returns it" in section
        assert "SSN entry" in section
        assert "Onfido identity verification" in section
        assert "During application data collection" in section
        assert "Everything else should be completed through the CLI" in section
        assert "all visible non-sensitive missing facts" in section
        assert "API-writable/non-sensitive application field" in section
        assert "only remaining applicant-owned actions are SSN entry" in section
        assert "SSN entry and optionally phone verification" in normalized_section
        assert "business.incorporation" in section
        assert "domain-mismatch explanation" in section
        assert "residential address" in section
        assert "ownership percentage" in section
        assert "one browser session" in section
        assert (
            "Final review and submission is also applicant-only" in normalized_section
        )
        assert "--wait_for_phone_verification" in section
        assert "--wait_for_identity_verification" in section

    def test_apply_to_ramp_progress_loop_mentions_wait_flags(self):
        """Progress guidance should use built-in waits for applicant handoffs."""
        content = (SKILLS_DIR / "apply-to-ramp" / "SKILL.md").read_text()
        section = self._extract_section(content, "Run The Progress Loop")

        assert "--wait_for_phone_verification" in section
        assert "--wait_for_identity_verification" in section
        assert "--wait_for_action <action_or_key>" in section
        assert "Do not use wait flags with `--dry_run`" in section

    def test_apply_to_ramp_batches_non_sensitive_questions(self):
        """The question guidance should ask for writable fields up front."""
        content = (SKILLS_DIR / "apply-to-ramp" / "SKILL.md").read_text()
        section = self._extract_section(content, "Ask Only For The Next Missing Facts")
        normalized_section = " ".join(section.split())

        assert "all current agent-owned required actions" in normalized_section
        assert "does not have to bounce between Ramp and chat" in normalized_section
        assert "residential address" in section
        assert "Ramp currently supports LLC formation" in normalized_section
        assert "which valid filing state should we use" in normalized_section

    def test_apply_to_ramp_owners_and_control_guidance(self):
        """Agents should capture controller ownership when the officer is an owner."""
        content = (SKILLS_DIR / "apply-to-ramp" / "SKILL.md").read_text()
        section = self._extract_section(content, "Owners And Control")
        normalized_section = " ".join(section.split())

        assert "controlling officer who is also a" in section
        assert "beneficial owner" in section
        assert "`controlling_officer.is_beneficial_owner: true`" in section
        assert "`controlling_officer.ownership_percentage`" in section
        assert "whole number from 25 to 100" in section
        assert "owns less than 25%" in section
        assert "do not mark them as a beneficial owner" in normalized_section
        assert "Do not duplicate the same person in `beneficial_owners`" in section
        assert "normalize owner/controller emails" in section
        assert "case-insensitively" in section
        assert (
            "`beneficial_owners[*].email` matches `controlling_officer.email`"
            in section
        )
        assert "merge" in section
        assert "remove that matching entry from `beneficial_owners`" in section
        assert "never retry by appending the same beneficial owner again" in section
        assert '"beneficial_owners": []' in section
        assert "include each owner's `ownership_percentage`" in section
        assert "`ownership_acknowledgement`" in section
        assert "25%+ beneficial owners" in section

    def test_incorporate_with_ramp_allows_direct_filing_after_fa_submission(self):
        """The filing skill should support direct LLC filing after FA submission."""
        content = (SKILLS_DIR / "incorporate-with-ramp" / "SKILL.md").read_text()
        normalized = " ".join(content.split())

        assert "Once the financing application has been submitted" in normalized
        assert "IN_REVIEW" in normalized
        assert "WAIT_FOR_RAMP" in normalized
        assert "do not wait for FA approval" in normalized
        assert "Do not start formation while the application is only" in normalized
        assert "ready_for_submission=true" in normalized
        assert "direct filing path" in normalized
        assert "file an LLC" in normalized
        assert "Do not report `WAIT_FOR_RAMP` as a stop condition" in content
        assert "absence of an explicit `COMPLETE_INCORPORATION` action" in normalized
        assert "must still check formation status" in content
        assert "--wait_for_action REVIEW_AND_SUBMIT" in content
        assert "--wait_for_action COMPLETE_INCORPORATION" in content

    def test_incorporate_with_ramp_uses_lean_submit_payload(self):
        """Formation submit should reuse FA owner/controller data."""
        content = (SKILLS_DIR / "incorporate-with-ramp" / "SKILL.md").read_text()
        workflow_section = self._extract_section(content, "Workflow")
        normalized = " ".join(content.split())
        normalized_workflow = " ".join(workflow_section.split())

        assert "reuses owner, controller, and identity data" in content
        assert "do not ask for `members`, `responsible_party`" in content
        assert "`RAMP_INCORPORATION_*_SSN_LAST_4`" in content
        assert "Use the lean formation payload" in workflow_section
        assert (
            "Core derives that data from the submitted financing application"
            in normalized_workflow
        )
        assert '"addresses"' in workflow_section
        assert '"members":' not in workflow_section
        assert '"responsible_party":' not in workflow_section
        assert "Stale guidance asks for `members`, `responsible_party`" in content
        assert "Lean submit returns a validation error" in content
        assert "pre_ein.access = LIMITED" in normalized
        assert "`RP_IDENTITY`" in workflow_section
        assert "do not re-collect or re-submit" in normalized_workflow
        assert "FA-sourced identity fields" in workflow_section

    def test_incorporate_with_ramp_requires_same_context_applicant_preflight(self):
        """Formation submit must be preceded by same-context applicant create/get."""
        content = (SKILLS_DIR / "incorporate-with-ramp" / "SKILL.md").read_text()
        section = self._extract_section(content, "Workflow")

        assert "Use the same CLI binary" in content
        assert "same CLI binary, `--env`, OAuth token, and shell session" in section
        assert "ramp incorporation applicant create --agent" in section
        assert "ramp incorporation applicant get --agent" in section
        assert "This is a required pre-submit gate" in section
        assert "Same-context preflight" in section
        assert 'applications get --env "$ENV" --agent | python3' in section
        assert "Do not paste or summarize the full `applications get` output" in section

    def test_incorporate_with_ramp_missing_applicant_recovery(self):
        """The skill should recover missing-applicant errors before retrying submit."""
        content = (SKILLS_DIR / "incorporate-with-ramp" / "SKILL.md").read_text()
        gotchas = self._extract_section(content, "Gotchas")

        assert "No incorporation applicant exists for this business" in gotchas
        assert "Do not retry submit first" in gotchas
        assert "applicant create --env <env> --country-of-residence US" in gotchas
        assert "applicant get --env <env>" in gotchas
        assert "same terminal/auth context" in gotchas
        assert "No incorporation formation exists" in gotchas
        assert "generic auth-token hint" in gotchas
        assert "ramp auth status" in gotchas
        assert "incorporation:read" in gotchas
        assert "incorporation:write" in gotchas
        assert "Do not hardcode `customer_id` or `doolaCustomerId`" in gotchas
        assert "BusinessIncorporationLink.doola_customer_id" in gotchas
        assert "SSN entry still required" in gotchas
        assert "do not run `ramp incorporation submit`" in gotchas

    def _extract_section(self, content: str, heading: str) -> str:
        """Return text from `## heading` up to (but not including) the next `## ` heading."""
        start = content.find(f"## {heading}")
        assert start != -1, f"Section '## {heading}' not found in SKILL.md"
        end = content.find("\n## ", start + 1)
        return content[start:end] if end != -1 else content[start:]

    def test_apply_to_ramp_skill_ssn_browser_handoff_guidance(self):
        """apply-to-ramp skill must instruct the agent to use deep_link_url for SSN."""
        content = (SKILLS_DIR / "apply-to-ramp" / "SKILL.md").read_text()
        ssn_section = self._extract_section(content, "SSN — Browser Handoff Only")
        normalized_ssn_section = " ".join(ssn_section.split())

        # Must never ask for SSN in chat.
        assert "Never ask for or accept SSN" in ssn_section
        assert "CLI prompts" in ssn_section
        assert "env vars" in ssn_section
        assert "Ramp browser form link" in ssn_section
        # Must use non-null language, not just 'includes'.
        assert "non-null" in ssn_section
        assert "deep_link_url" in ssn_section
        # Must use lowercase section_key values as seen in the API.
        assert "controlling_officer" in ssn_section
        assert "beneficial_owners" in ssn_section
        assert "non-sensitive missing fields" in normalized_ssn_section
        assert "collect and PATCH" in ssn_section
        assert (
            "only other remaining applicant-owned action is phone"
            in normalized_ssn_section
        )
        assert "address" in ssn_section
        assert "ownership" in ssn_section
        # ALL_CAPS section keys must not appear in SSN prose (they're not real API values).
        assert "CONTROLLING_OFFICER" not in ssn_section
        assert "BENEFICIAL_OWNERS" not in ssn_section
        # Must reference both KYC follow-up reason enum values (these ARE upper-case in API).
        assert "KYC_SSN_VERIFICATION" in ssn_section
        assert "KYC_SSN_FULL_9_VERIFICATION" in ssn_section
        # Must instruct re-fetching after browser step.
        assert "re-fetch" in ssn_section
        # Must warn against inventing a link.
        assert (
            "Never invent" in ssn_section
            or "never invent" in ssn_section
            or "only" in ssn_section
        )
        # Must include pre-deploy fallback (null deep_link_url) guidance.
        assert "null" in ssn_section
        # Must include cross-skill disambiguation note.
        assert (
            "incorporate-with-ramp" in ssn_section
            or "RAMP_INCORPORATION" in ssn_section
        )

    def test_apply_to_ramp_skill_ssn_safety_boundary(self):
        """apply-to-ramp safety boundaries must explicitly call out SSN in-chat collection."""
        content = (SKILLS_DIR / "apply-to-ramp" / "SKILL.md").read_text()
        safety_section = self._extract_section(content, "Safety Boundaries")

        # The explicit SSN bullet must be present within the Safety Boundaries section.
        assert "Never collect SSN" in safety_section
        # The bullet must reference the deep_link_url handoff.
        assert "deep_link_url" in safety_section

    def test_apply_to_ramp_example_ssn_last_4_is_null(self):
        """APPLICATION_EXAMPLE must not model SSN collection (both fields must be null)."""
        bo = APPLICATION_EXAMPLE["beneficial_owners"][0]
        assert bo["ssn_last_4"] is None, "beneficial_owner ssn_last_4 must be null"
        co = APPLICATION_EXAMPLE["controlling_officer"]
        assert co["ssn_last_4"] is None, "controlling_officer ssn_last_4 must be null"

    def test_apply_to_ramp_example_includes_ownership_percentages(self):
        """APPLICATION_EXAMPLE should model controller-owner percentages."""
        co = APPLICATION_EXAMPLE["controlling_officer"]
        assert co["is_beneficial_owner"] is True
        assert co["ownership_percentage"] == 60

        bo = APPLICATION_EXAMPLE["beneficial_owners"][0]
        assert bo["ownership_percentage"] == 40

    def test_agent_tool_spec_example_ssn_last_4_is_null(self):
        """ApiApplicationResource example in agent-tool.json must also null ssn_last_4."""
        spec = json.loads(AGENT_TOOL_SPEC.read_text())
        example = spec["components"]["schemas"]["ApiApplicationResource"]["example"]
        for bo in example.get("beneficial_owners", []):
            assert bo.get("ssn_last_4") is None, (
                "agent-tool.json ApiApplicationResource example beneficial_owner ssn_last_4 must be null"
            )
        co = example.get("controlling_officer", {})
        assert co.get("ssn_last_4") is None, (
            "agent-tool.json ApiApplicationResource example controlling_officer ssn_last_4 must be null"
        )


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
    "incorporation": {
        "states",
        "industries",
        "countries",
        "applicant",
        "submit",
        "status",
        "documents",
    },
    "skills": {"list", "show", "install"},
    "tools": {"refresh", "schema"},
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
        if len(cat_tools) > 1 or cat in _SINGLE_TOOL_RESOURCE_CATEGORIES:
            for t in cat_tools:
                valid.add((cat, t.alias or t.name))
        else:
            # Singletons fold into "general"
            for t in cat_tools:
                alias = t.alias or t.name
                valid.add(("general", alias))

    # Additive alias groups (e.g. cards): the tool's short alias is also
    # reachable under its original spec category as its own group.
    for t in tools:
        if t.category in CATEGORY_ALIAS_GROUPS:
            valid.add((t.category, t.alias or t.name))

    # Hidden legacy groups remain invokable for backwards compatibility even
    # after the public resource group is remapped.
    for t in tools:
        if t.category in CATEGORY_LEGACY_GROUPS:
            valid.add((t.category, t.alias or t.name))

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
        if len(cat_tools) > 1 or cat in _SINGLE_TOOL_RESOURCE_CATEGORIES:
            for t in cat_tools:
                key = (cat, t.alias or t.name)
                # Merge params when multiple tools share (category, alias).
                index.setdefault(key, set()).update(p.name for p in t.params)
        else:
            for t in cat_tools:
                alias = t.alias or t.name
                key = ("general", alias)
                index.setdefault(key, set()).update(p.name for p in t.params)

    # Additive alias groups (e.g. cards): index the tool's params under its
    # own spec-category group as well.
    for t in tools:
        if t.category in CATEGORY_ALIAS_GROUPS:
            key = (t.category, t.alias or t.name)
            index.setdefault(key, set()).update(p.name for p in t.params)

    # Hidden legacy groups are not shown in help, but command references using
    # the old resource path should still validate.
    for t in tools:
        if t.category in CATEGORY_LEGACY_GROUPS:
            key = (t.category, t.alias or t.name)
            index.setdefault(key, set()).update(p.name for p in t.params)

    # applications progress has CLI-only wait flags layered on top of the
    # generated OpenAPI command.
    index.setdefault(("applications", "progress"), set()).update(
        {
            "wait_for_action",
            "wait_for_phone_verification",
            "wait_for_identity_verification",
            "wait_interval",
            "wait_timeout",
        }
    )

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
