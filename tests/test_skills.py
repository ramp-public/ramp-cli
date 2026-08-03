"""Tests for skill discovery, listing, showing, and installing."""

from __future__ import annotations

import json
import re
import shutil
import tomllib

import pytest
from click.testing import CliRunner

from ramp_cli.config import settings
from ramp_cli.main import cli
from ramp_cli.skills import (
    detect_agent_dir,
    get_skill_content,
    install_skill,
    installed_skill_name,
    skill_names,
)
from ramp_cli.specs import SKILLS_DIR

_REMOTE_SKILLS = [
    "agentic-purchase",
    "apply-for-account",
    "approval-dashboard",
    "book-flight",
    "book-hotel",
    "browser-automation",
    "card-management",
    "get-started",
    "incorporate",
    "manage-bills",
    "manage-procurement",
    "payment-lookup",
    "receipt-compliance",
    "spend-analysis",
    "submit-procurement-request",
    "submit-reimbursement",
    "transaction-cleanup",
    "vendor-document-upload",
    "x402-pay",
]


@pytest.fixture(autouse=True)
def skill_catalog(isolated_config):
    """Provide a small verified remote catalog for CLI behavior tests."""
    root = settings.config_dir() / "skills" / "v-test"
    for name in _REMOTE_SKILLS:
        title = name.replace("-", " ").title()
        body = title
        if name == "browser-automation":
            body = "Browser Automation\n\nUse playwright-cli."
        elif name == "vendor-document-upload":
            body = "Upload vendor documents\n\nRun ramp vendors attach-document."
        skill = root / name / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text(
            f"---\nname: ramp-{name}\ndescription: {title}\n---\n# {body}\n"
        )
    active = root.parent / "active.json"
    active.write_text(json.dumps({"version": root.name}))
    return root


class TestSkillDiscovery:
    def test_package_does_not_bundle_skill_content(self):
        assert not any(SKILLS_DIR.glob("*/SKILL.md"))

    def test_skill_names_discovers_all(self):
        """All 19 skills should be discovered from the active catalog."""
        names = skill_names()
        assert len(names) == 19
        assert "x402-pay" in names
        assert "get-started" in names
        assert "agentic-purchase" in names
        assert "card-management" in names
        assert "book-flight" in names
        assert "book-hotel" in names
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
        assert "submit-procurement-request" in names

    def test_legacy_cli_names_map_to_renamed_remote_skills(self):
        assert "name: ramp-apply-for-account" in get_skill_content("apply-to-ramp")
        assert "name: ramp-incorporate" in get_skill_content("incorporate-with-ramp")

    def test_readme_lists_all_skills(self):
        """The CLI skill index should mention every catalog skill."""
        readme = (SKILLS_DIR / "README.md").read_text()
        for name in skill_names():
            assert f"`{name}`" in readme

    def test_skill_names_empty_dir(self, tmp_path, monkeypatch):
        """Returns empty list when skills dir has no skill subdirectories."""
        monkeypatch.setattr("ramp_cli.skills.active_skills_dir", lambda: tmp_path)
        assert skill_names() == []


class TestSkillsList:
    def test_list_skills_json(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["--agent", "skills", "list"])
        assert result.exit_code == 0

        data = json.loads(result.output)
        assert len(data["data"]) == 19
        names = {s["name"] for s in data["data"]}
        assert "get-started" in names
        assert "browser-automation" in names
        assert "card-management" in names
        assert "agentic-purchase" in names
        assert "incorporate-with-ramp" in names
        assert "book-flight" in names
        assert "book-hotel" in names
        assert "manage-procurement" in names
        assert "manage-bills" in names
        assert "vendor-document-upload" in names
        assert "submit-procurement-request" in names

    def test_list_skills_human(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["--human", "skills", "list"])
        assert result.exit_code == 0
        assert "19 Skills" in result.output
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

    def test_show_all_skills_returns_content(self, skill_catalog):
        """Every discovered skill should be fetchable via skills show."""
        runner = CliRunner()
        for name in skill_names():
            result = runner.invoke(cli, ["skills", "show", name])
            assert result.exit_code == 0, f"skills show {name} failed"
            remote_name = {
                "apply-to-ramp": "apply-for-account",
                "incorporate-with-ramp": "incorporate",
            }.get(name, name)
            file_content = (skill_catalog / remote_name / "SKILL.md").read_text()
            assert result.output.strip() == file_content.strip(), (
                f"skills show {name} output doesn't match SKILL.md"
            )


class TestSkillsInstall:
    def test_install_single(self, tmp_path):
        """Install one skill to a tmp directory."""
        status = install_skill("browser-automation", tmp_path)
        assert status == "installed"
        dest = tmp_path / installed_skill_name("browser-automation") / "SKILL.md"
        assert dest.is_file()
        assert "Browser Automation" in dest.read_text()
        assert "name: ramp-browser-automation" in dest.read_text()

    def test_install_all(self, tmp_path):
        """--all installs all 19 skills."""
        target = tmp_path / "skills"
        runner = CliRunner()
        result = runner.invoke(
            cli, ["skills", "install", "--all", "--target", str(target)]
        )
        assert result.exit_code == 0
        installed = [d.name for d in target.iterdir() if d.is_dir()]
        assert len(installed) == 19
        assert all(name.startswith("ramp-") for name in installed)
        assert "ramp-browser-automation" in installed

    def test_install_creates_missing_nested_target(self, tmp_path):
        """An explicit --target is created along with any missing parents."""
        target = tmp_path / "agent" / ".claude" / "skills"
        runner = CliRunner()

        result = runner.invoke(
            cli,
            ["skills", "install", "browser-automation", "--target", str(target)],
        )

        assert result.exit_code == 0
        assert (target / "ramp-browser-automation" / "SKILL.md").is_file()

    def test_install_overwrites(self, tmp_path):
        """Installing twice succeeds and returns 'updated' on second run."""
        install_skill("browser-automation", tmp_path)
        notes = tmp_path / installed_skill_name("browser-automation") / "notes.txt"
        notes.write_text("keep until uninstall")
        status = install_skill("browser-automation", tmp_path)
        assert status == "updated"
        dest = tmp_path / installed_skill_name("browser-automation") / "SKILL.md"
        assert dest.is_file()
        assert notes.read_text() == "keep until uninstall"

    def test_install_migrates_legacy_directory(self, tmp_path):
        """A receipted legacy install is renamed and its receipt rewritten."""
        legacy = tmp_path / "browser-automation"
        legacy.mkdir()
        (legacy / "SKILL.md").write_text("old bundled content")
        state = settings.config_dir() / "skills.toml"
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(f'[known]\n"{tmp_path.resolve()}" = "browser-automation"\n')

        result = CliRunner().invoke(
            cli,
            ["skills", "install", "browser-automation", "--target", str(tmp_path)],
        )

        assert result.exit_code == 0
        assert "Updated ramp-browser-automation" in result.output
        assert not legacy.exists()
        assert (tmp_path / "ramp-browser-automation" / "SKILL.md").is_file()
        entry = tomllib.loads(state.read_text())["targets"][str(tmp_path.resolve())]
        assert entry["skills"] == ["ramp-browser-automation"]

    def test_install_preserves_untracked_legacy_directory(self, tmp_path):
        """A generic user-authored skill is not mistaken for a Ramp install."""
        legacy_file = tmp_path / "browser-automation" / "SKILL.md"
        legacy_file.parent.mkdir()
        legacy_file.write_text("user-authored content")

        result = CliRunner().invoke(
            cli,
            ["skills", "install", "browser-automation", "--target", str(tmp_path)],
        )

        assert result.exit_code == 0
        assert "Installed ramp-browser-automation" in result.output
        assert legacy_file.read_text() == "user-authored content"
        assert (tmp_path / "ramp-browser-automation" / "SKILL.md").is_file()

    def test_install_injects_user_invocable(self, tmp_path):
        """Install to a .claude/skills/ target injects user-invocable: true."""
        claude_skills = tmp_path / ".claude" / "skills"
        claude_skills.mkdir(parents=True)
        install_skill("browser-automation", claude_skills)
        content = (claude_skills / "ramp-browser-automation" / "SKILL.md").read_text()
        assert "user-invocable: true" in content

    def test_install_no_inject_for_other_targets(self, tmp_path):
        """Install to a non-.claude target does not inject user-invocable."""
        install_skill("browser-automation", tmp_path)
        content = (tmp_path / "ramp-browser-automation" / "SKILL.md").read_text()
        assert "user-invocable" not in content

    def test_all_catalog_skills_have_frontmatter_name(self, skill_catalog):
        """Every catalog skill has the frontmatter name the rewrite targets."""
        for name in skill_names():
            remote_name = {
                "apply-to-ramp": "apply-for-account",
                "incorporate-with-ramp": "incorporate",
            }.get(name, name)
            text = (skill_catalog / remote_name / "SKILL.md").read_text(
                encoding="utf-8"
            )
            match = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
            assert match, f"{name}: missing frontmatter block"
            assert re.search(r"(?m)^name:\s*\S", match.group(1)), (
                f"{name}: frontmatter has no name field"
            )

    def test_install_rewrites_only_frontmatter_name(self, tmp_path, monkeypatch):
        """A column-0 name: line in the skill body is never rewritten."""
        bundle = tmp_path / "bundle"
        (bundle / "demo-skill").mkdir(parents=True)
        (bundle / "demo-skill" / "SKILL.md").write_text(
            "---\n"
            "name: demo-skill\n"
            "description: demo\n"
            "---\n"
            "```yaml\n"
            "name: keep-me\n"
            "```\n"
        )
        monkeypatch.setattr("ramp_cli.skills.active_skills_dir", lambda: bundle)
        target = tmp_path / "out"
        target.mkdir()

        install_skill("demo-skill", target)

        content = (target / "ramp-demo-skill" / "SKILL.md").read_text()
        assert "name: ramp-demo-skill" in content
        assert "name: keep-me" in content
        assert "name: demo-skill" not in content

    def test_install_empty_skills_dir(self, tmp_path, monkeypatch):
        """No skills are available when the active catalog is empty."""
        monkeypatch.setattr("ramp_cli.skills.active_skills_dir", lambda: tmp_path)
        monkeypatch.setattr(
            "ramp_cli.commands.skills.active_skills_dir", lambda: tmp_path
        )
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


class TestSkillsSync:
    """Deletion tracking: skills removed by the user stay removed on re-sync."""

    def _install_all(self, target):
        runner = CliRunner()
        result = runner.invoke(
            cli, ["skills", "install", "--all", "--target", str(target)]
        )
        assert result.exit_code == 0
        return result

    def test_deleted_skill_not_reinstalled(self, tmp_path):
        target = tmp_path / "skills"
        assert "19 skill(s) installed" in self._install_all(target).output

        shutil.rmtree(target / "ramp-browser-automation")
        result = self._install_all(target)
        assert "18 skill(s) installed" in result.output
        assert "Skipped previously removed: browser-automation" in result.output
        assert "restore: ramp skills install <name>" in result.output
        assert not (target / "ramp-browser-automation").exists()
        # Deletion persists across further syncs.
        assert "Skipped previously removed" in self._install_all(target).output

    def test_unreceipted_prefixed_dir_is_never_adopted(self, tmp_path):
        """An unreceipted ramp-* directory is skipped, never claimed or deleted."""
        target = tmp_path / "skills"
        install_skill("browser-automation", target)  # on disk, but no receipt
        result = self._install_all(target)
        assert "Skipped browser-automation" in result.output
        assert "not managed by Ramp CLI" in result.output
        assert "18 skill(s) installed" in result.output

        # The unreceipted directory stays untracked and untouched by uninstall.
        result = CliRunner().invoke(
            cli, ["skills", "uninstall", "--all", "--target", str(target), "--yes"]
        )
        assert result.exit_code == 0
        assert "18 skill(s) uninstalled" in result.output
        assert (target / "ramp-browser-automation" / "SKILL.md").is_file()

    def test_two_directory_upgrade_preserves_user_prefixed_dir(self, tmp_path):
        """A user's ramp-<name> dir blocks migration and stays untouched."""
        target = tmp_path / "skills"
        legacy = target / "browser-automation"
        legacy.mkdir(parents=True)
        (legacy / "SKILL.md").write_text("legacy bundled content")
        user_dir = target / "ramp-browser-automation"
        user_dir.mkdir()
        (user_dir / "SKILL.md").write_text("user fork")
        (user_dir / "notes.txt").write_text("user notes")
        state = settings.config_dir() / "skills.toml"
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(f'[known]\n"{target.resolve()}" = "browser-automation"\n')

        result = self._install_all(target)

        assert result.exit_code == 0
        assert "Skipped browser-automation" in result.output
        assert (user_dir / "SKILL.md").read_text() == "user fork"
        assert (user_dir / "notes.txt").read_text() == "user notes"
        assert legacy.is_dir()  # not migrated while the destination is occupied
        entry = tomllib.loads(state.read_text())["targets"][str(target.resolve())]
        assert "browser-automation" in entry["skills"]
        assert "ramp-browser-automation" not in entry["skills"]

        # A later full uninstall removes managed directories only.
        result = CliRunner().invoke(
            cli, ["skills", "uninstall", "--all", "--target", str(target), "--yes"]
        )
        assert result.exit_code == 0
        assert not legacy.exists()
        assert (user_dir / "notes.txt").read_text() == "user notes"

    def test_explicit_install_refuses_unreceipted_prefixed_destination(self, tmp_path):
        target = tmp_path / "skills"
        user_dir = target / "ramp-browser-automation"
        user_dir.mkdir(parents=True)
        (user_dir / "SKILL.md").write_text("user fork")

        result = CliRunner().invoke(
            cli,
            ["skills", "install", "browser-automation", "--target", str(target)],
        )

        assert result.exit_code != 0
        assert "not managed by Ramp CLI" in result.output
        assert (user_dir / "SKILL.md").read_text() == "user fork"

    def test_explicit_install_restores_deleted_skill(self, tmp_path):
        target = tmp_path / "skills"
        self._install_all(target)
        shutil.rmtree(target / "ramp-browser-automation")
        self._install_all(target)

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["skills", "install", "browser-automation", "--target", str(target)],
        )
        assert result.exit_code == 0
        assert (target / "ramp-browser-automation" / "SKILL.md").is_file()

        # And it is synced again afterwards.
        result = self._install_all(target)
        assert "Skipped" not in result.output
        assert "19 skill(s) installed" in result.output

    def test_legacy_known_state_migrates_to_receipts(self, tmp_path):
        target = tmp_path / "skills"
        state = settings.config_dir() / "skills.toml"
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(f'[known]\n"{target.resolve()}" = "some-retired-skill"\n')

        self._install_all(target)
        entry = tomllib.loads(state.read_text())["targets"][str(target.resolve())]
        expected = {installed_skill_name(n) for n in skill_names()}
        assert set(entry["skills"]) == expected | {"some-retired-skill"}

    def test_targets_track_deletions_independently(self, tmp_path):
        """Syncing one target must not mark skills deleted in another (fresh) target."""
        target_a = tmp_path / "a"
        target_b = tmp_path / "b"
        self._install_all(target_a)
        shutil.rmtree(target_a / "ramp-browser-automation")

        result = self._install_all(target_b)
        assert "19 skill(s) installed" in result.output
        assert "Skipped" not in result.output
        # And target A's deletion still holds.
        assert "Skipped previously removed: browser-automation" in (
            self._install_all(target_a).output
        )

    def test_partial_failure_records_each_successful_install(
        self, tmp_path, monkeypatch
    ):
        """A mid-install failure leaves exact receipts for completed copies only."""
        target = tmp_path / "skills"
        real_install = install_skill

        def failing_install(name, target_dir):
            if name > "get-started":
                raise OSError("disk full")
            return real_install(name, target_dir)

        monkeypatch.setattr("ramp_cli.commands.skills.install_skill", failing_install)
        runner = CliRunner()
        result = runner.invoke(
            cli, ["skills", "install", "--all", "--target", str(target)]
        )
        assert result.exit_code != 0
        completed = {
            installed_skill_name(name)
            for name in skill_names()
            if name <= "get-started"
        }
        state = tomllib.loads((settings.config_dir() / "skills.toml").read_text())
        entry = state["targets"][str(target.resolve())]
        assert set(entry["skills"]) == completed
        assert {path.name for path in target.iterdir()} == completed
        monkeypatch.setattr("ramp_cli.commands.skills.install_skill", real_install)

        result = self._install_all(target)
        assert "19 skill(s) installed" in result.output
        assert "Skipped" not in result.output

    def test_receipt_write_failure_rolls_back_new_directory(
        self, tmp_path, monkeypatch
    ):
        target = tmp_path / "skills"

        def failing_receipt(target_dir, installed_name, replaces=None):
            raise OSError("read-only config")

        monkeypatch.setattr("ramp_cli.commands.skills.record_receipt", failing_receipt)
        result = CliRunner().invoke(
            cli,
            ["skills", "install", "browser-automation", "--target", str(target)],
        )

        assert result.exit_code != 0
        assert "install was rolled back" in result.output
        assert not (target / "ramp-browser-automation").exists()
        assert not (settings.config_dir() / "skills.toml").exists()

    def test_receipt_write_failure_renames_migrated_directory_back(
        self, tmp_path, monkeypatch
    ):
        """A failed receipt write renames the migrated directory back."""
        legacy = tmp_path / "browser-automation"
        legacy.mkdir(parents=True)
        (legacy / "SKILL.md").write_text("old bundled content")
        (legacy / "notes.txt").write_text("user notes")
        state = settings.config_dir() / "skills.toml"
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(f'[known]\n"{tmp_path.resolve()}" = "browser-automation"\n')

        def failing_receipt(target_dir, installed_name, replaces=None):
            raise OSError("read-only config")

        monkeypatch.setattr("ramp_cli.commands.skills.record_receipt", failing_receipt)
        result = CliRunner().invoke(
            cli,
            ["skills", "install", "browser-automation", "--target", str(tmp_path)],
        )

        assert result.exit_code != 0
        assert "migration was rolled back" in result.output
        assert not (tmp_path / "ramp-browser-automation").exists()
        assert (legacy / "SKILL.md").is_file()
        assert (legacy / "notes.txt").read_text() == "user notes"
        assert "browser-automation" in state.read_text()

    def test_failure_after_migration_rename_is_rolled_back(self, tmp_path, monkeypatch):
        """A copy failure after the migration rename is rolled back; retry works."""
        legacy = tmp_path / "browser-automation"
        legacy.mkdir(parents=True)
        (legacy / "SKILL.md").write_text("user-edited content")
        (legacy / "notes.txt").write_text("user notes")
        state = settings.config_dir() / "skills.toml"
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(f'[known]\n"{tmp_path.resolve()}" = "browser-automation"\n')

        real_copy2 = shutil.copy2

        def failing_copy2(src, dst, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr("ramp_cli.skills.shutil.copy2", failing_copy2)
        result = CliRunner().invoke(
            cli,
            ["skills", "install", "browser-automation", "--target", str(tmp_path)],
        )

        assert result.exit_code != 0
        assert "rolled back" in result.output
        assert not (tmp_path / "ramp-browser-automation").exists()
        assert (legacy / "SKILL.md").read_text() == "user-edited content"
        assert (legacy / "notes.txt").read_text() == "user notes"
        assert "browser-automation" in state.read_text()

        monkeypatch.setattr("ramp_cli.skills.shutil.copy2", real_copy2)
        result = CliRunner().invoke(
            cli,
            ["skills", "install", "browser-automation", "--target", str(tmp_path)],
        )
        assert result.exit_code == 0
        migrated = tmp_path / "ramp-browser-automation"
        assert (migrated / "notes.txt").read_text() == "user notes"
        assert "name: ramp-browser-automation" in (migrated / "SKILL.md").read_text()


class TestSkillsUninstall:
    def test_uninstall_all_removes_only_managed_skills(self, tmp_path):
        target = tmp_path / "skills"
        user_skill = target / "my-skill" / "SKILL.md"
        user_skill.parent.mkdir(parents=True)
        user_skill.write_text("user-authored")
        runner = CliRunner()
        assert (
            runner.invoke(
                cli, ["skills", "install", "--all", "--target", str(target)]
            ).exit_code
            == 0
        )

        result = runner.invoke(
            cli,
            ["skills", "uninstall", "--all", "--target", str(target), "--yes"],
        )

        assert result.exit_code == 0
        assert "19 skill(s) uninstalled" in result.output
        assert user_skill.read_text() == "user-authored"
        assert not (target / "ramp-browser-automation").exists()

    def test_uninstall_single_explicit_install(self, tmp_path):
        target = tmp_path / "skills"
        runner = CliRunner()
        assert (
            runner.invoke(
                cli,
                ["skills", "install", "browser-automation", "--target", str(target)],
            ).exit_code
            == 0
        )

        result = runner.invoke(
            cli,
            [
                "skills",
                "uninstall",
                "browser-automation",
                "--target",
                str(target),
                "--yes",
            ],
        )

        assert result.exit_code == 0
        assert "1 skill(s) uninstalled" in result.output
        assert not (target / "ramp-browser-automation").exists()

    def test_uninstall_does_not_remove_untracked_same_name(self, tmp_path):
        target = tmp_path / "skills"
        skill = target / "browser-automation" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("user-authored")

        result = CliRunner().invoke(
            cli,
            ["skills", "uninstall", "browser-automation", "--target", str(target)],
        )

        assert result.exit_code != 0
        assert "No installation receipt" in result.output
        assert skill.read_text() == "user-authored"

    def test_uninstall_never_touches_unreceipted_directories(self, tmp_path):
        """Only receipt-recorded directories are deleted, whatever their names."""
        target = tmp_path / "skills"
        runner = CliRunner()
        assert (
            runner.invoke(
                cli,
                ["skills", "install", "browser-automation", "--target", str(target)],
            ).exit_code
            == 0
        )
        user_fork = target / "browser-automation" / "SKILL.md"
        user_fork.parent.mkdir(parents=True)
        user_fork.write_text("user fork")
        user_prefixed = target / "ramp-my-own-skill" / "SKILL.md"
        user_prefixed.parent.mkdir(parents=True)
        user_prefixed.write_text("user prefixed")

        result = runner.invoke(
            cli,
            ["skills", "uninstall", "--all", "--target", str(target), "--yes"],
        )

        assert result.exit_code == 0
        assert "1 skill(s) uninstalled" in result.output
        assert not (target / "ramp-browser-automation").exists()
        assert user_fork.read_text() == "user fork"
        assert user_prefixed.read_text() == "user prefixed"

    def test_uninstalled_skill_not_resurrected_by_install_all(self, tmp_path):
        target = tmp_path / "skills"
        runner = CliRunner()
        assert (
            runner.invoke(
                cli, ["skills", "install", "--all", "--target", str(target)]
            ).exit_code
            == 0
        )
        assert (
            runner.invoke(
                cli,
                [
                    "skills",
                    "uninstall",
                    "browser-automation",
                    "--target",
                    str(target),
                    "--yes",
                ],
            ).exit_code
            == 0
        )

        result = runner.invoke(
            cli, ["skills", "install", "--all", "--target", str(target)]
        )
        assert "Skipped previously removed: browser-automation" in result.output
        assert not (target / "ramp-browser-automation").exists()

        # An explicit named install restores it and clears the record.
        assert (
            runner.invoke(
                cli,
                ["skills", "install", "browser-automation", "--target", str(target)],
            ).exit_code
            == 0
        )
        assert (target / "ramp-browser-automation" / "SKILL.md").is_file()
        result = runner.invoke(
            cli, ["skills", "install", "--all", "--target", str(target)]
        )
        assert "Skipped" not in result.output

    def test_uninstall_of_manually_deleted_skill_keeps_protection(self, tmp_path):
        target = tmp_path / "skills"
        runner = CliRunner()
        assert (
            runner.invoke(
                cli, ["skills", "install", "--all", "--target", str(target)]
            ).exit_code
            == 0
        )
        shutil.rmtree(target / "ramp-browser-automation")

        result = runner.invoke(
            cli,
            [
                "skills",
                "uninstall",
                "browser-automation",
                "--target",
                str(target),
                "--yes",
            ],
        )
        assert result.exit_code == 0
        assert "0 skill(s) uninstalled" in result.output

        result = runner.invoke(
            cli, ["skills", "install", "--all", "--target", str(target)]
        )
        assert "Skipped previously removed: browser-automation" in result.output
        assert not (target / "ramp-browser-automation").exists()

    def test_legacy_receipt_matching_bundle_is_removed(self, tmp_path):
        """Legacy [known] ownership is enough to remove the exact directory."""
        target = tmp_path / "skills"
        legacy = target / "browser-automation"
        legacy.mkdir(parents=True)
        (legacy / "SKILL.md").write_text("old bundled content")
        state = settings.config_dir() / "skills.toml"
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(f'[known]\n"{target.resolve()}" = "browser-automation"\n')
        runner = CliRunner()

        result = runner.invoke(
            cli,
            [
                "skills",
                "uninstall",
                "browser-automation",
                "--target",
                str(target),
                "--yes",
            ],
        )
        assert result.exit_code == 0
        assert "1 skill(s) uninstalled" in result.output
        assert not (target / "browser-automation").exists()

    def test_confirmation_discloses_all_files_and_defaults_to_cancel(self, tmp_path):
        target = tmp_path / "skills"
        runner = CliRunner()
        assert (
            runner.invoke(
                cli,
                ["skills", "install", "browser-automation", "--target", str(target)],
            ).exit_code
            == 0
        )
        skill = target / "ramp-browser-automation"
        nested_file = skill / "scripts" / "custom.sh"
        nested_file.parent.mkdir(parents=True)
        nested_file.write_text("user-authored")

        args = [
            "skills",
            "uninstall",
            "browser-automation",
            "--target",
            str(target),
        ]
        cancelled = runner.invoke(cli, args, input="\n")

        assert cancelled.exit_code != 0
        assert f"{skill}/" in cancelled.output
        assert "SKILL.md" in cancelled.output
        assert "scripts/custom.sh" in cancelled.output
        assert "Confirming will delete them too" in cancelled.output
        assert "Continue? [y/N]" in cancelled.output
        assert nested_file.read_text() == "user-authored"

        confirmed = runner.invoke(cli, args, input="y\n")
        assert confirmed.exit_code == 0
        assert "1 skill(s) uninstalled" in confirmed.output
        assert not skill.exists()

    def test_uninstall_requires_name_or_all(self):
        result = CliRunner().invoke(cli, ["skills", "uninstall"])
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

    def test_detect_finds_codex_dir(self, tmp_path, monkeypatch):
        """Detects .codex/skills/ when it exists."""
        codex_dir = tmp_path / ".codex" / "skills"
        codex_dir.mkdir(parents=True)
        monkeypatch.chdir(tmp_path)

        result = detect_agent_dir()

        assert result == codex_dir

    def test_detect_ignores_bare_codex_dir(self, tmp_path, monkeypatch):
        """Detection does not create a missing Codex skills directory."""
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        monkeypatch.chdir(tmp_path)

        result = detect_agent_dir()

        assert result is None
        assert not (codex_dir / "skills").exists()

    def test_detect_ignores_bare_claude_dir(self, tmp_path, monkeypatch):
        """Detection is read-only: a bare .claude without skills/ is not a match."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        monkeypatch.chdir(tmp_path)

        result = detect_agent_dir()

        assert result is None
        assert not (claude_dir / "skills").exists()

    def test_detect_returns_none(self, tmp_path, monkeypatch):
        """Returns None when no agent dir exists."""
        monkeypatch.chdir(tmp_path)
        result = detect_agent_dir()
        assert result is None
