"""Skill discovery — locate and parse bundled SKILL.md files.

Skills are bundled inside this package directory.  Each subdirectory that
contains a SKILL.md is a skill.  The path is resolved via __file__ so it
works in both editable and installed (wheel) builds.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import tempfile
import tomllib
from pathlib import Path

import tomli_w

from ramp_cli.config.settings import config_dir
from ramp_cli.specs import SKILLS_DIR

AGENT_SKILL_DIRS = [".claude/skills", ".cursor/skills", ".windsurf/skills"]


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Extract name and description from YAML frontmatter.

    Handles multi-line |- strings by joining continuation lines.
    """
    m = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
    if not m:
        return {}
    block = m.group(1)
    result: dict[str, str] = {}
    current_key: str | None = None
    current_lines: list[str] = []

    for line in block.splitlines():
        # New key
        kv = re.match(r"^(\w+):\s*(.*)$", line)
        if kv:
            if current_key and current_lines:
                result[current_key] = " ".join(current_lines).strip()
            current_key = kv.group(1)
            value = kv.group(2).strip()
            if value == "|-" or value == "|":
                current_lines = []
            else:
                current_lines = [value]
        elif current_key:
            current_lines.append(line.strip())

    if current_key and current_lines:
        result[current_key] = " ".join(current_lines).strip()

    # Strip surrounding quotes from YAML string values
    for k, v in result.items():
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
            result[k] = v[1:-1]

    return result


def skill_names() -> list[str]:
    """Return sorted list of skill directory names that contain a SKILL.md."""
    if not SKILLS_DIR.is_dir():
        return []
    return sorted(
        d.name
        for d in SKILLS_DIR.iterdir()
        if d.is_dir() and (d / "SKILL.md").is_file()
    )


def list_skills() -> list[dict[str, str]]:
    """Return list of {name, description} dicts for all available skills."""
    skills: list[dict[str, str]] = []
    for name in skill_names():
        path = SKILLS_DIR / name / "SKILL.md"
        fm = _parse_frontmatter(path.read_text(encoding="utf-8"))
        desc = fm.get("description", "")
        # Take only the first sentence for the short description
        first_line = desc.split(". ")[0].rstrip(".") if desc else ""
        skills.append({"name": name, "description": first_line})
    return skills


def get_skill_content(name: str) -> str | None:
    """Return full SKILL.md content for a skill, or None if not found."""
    path = SKILLS_DIR / name / "SKILL.md"
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return None


def detect_agent_dir() -> Path | None:
    """Walk up from cwd to find the first project root with an agent skill directory."""
    cwd = Path.cwd()
    for directory in [cwd, *cwd.parents]:
        for agent_dir in AGENT_SKILL_DIRS:
            candidate = directory / agent_dir
            if candidate.is_dir():
                return candidate
    return None


def install_skill(name: str, target_dir: Path) -> str:
    """Copy skills/<name>/SKILL.md into target_dir/<name>/SKILL.md.

    If target is under a .claude/skills directory, inject user-invocable: true
    into frontmatter if not present.

    Returns 'installed' or 'updated'.
    """
    source = SKILLS_DIR / name / "SKILL.md"
    if not source.is_file():
        msg = f"Skill not found: {name}"
        raise FileNotFoundError(msg)

    dest_dir = target_dir / name
    dest_file = dest_dir / "SKILL.md"
    status = "updated" if dest_file.exists() else "installed"

    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest_file)

    # Inject user-invocable: true for .claude/skills/ targets
    needs_inject = target_dir.name == "skills" and target_dir.parent.name == ".claude"
    if needs_inject:
        content = dest_file.read_text(encoding="utf-8")
        if "user-invocable:" not in content:
            content = content.replace(
                "\n---\n",
                "\nuser-invocable: true\n---\n",
                1,
            )
            dest_file.write_text(content, encoding="utf-8")

    return status


def _state_path() -> Path:
    """Skill sync state lives in its own file, separate from config.toml, so
    skill writes can never clobber the auth tokens stored there."""
    return config_dir() / "skills.toml"


def _load_known() -> dict[str, str]:
    """Load [known]: resolved target dir → space-separated synced skill names.

    Missing or malformed state degrades to empty (re-seed from disk)."""
    try:
        raw = tomllib.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    known = raw.get("known")
    if not isinstance(known, dict):
        return {}
    return {k: v for k, v in known.items() if isinstance(v, str)}


def previously_removed_skills(target_dir: Path) -> list[str]:
    """Bundled skills recorded as synced into ``target_dir`` but now missing.

    These were deleted by the user and should not be reinstalled.  A target
    with no recorded state seeds from the skills already present on disk.
    """
    available = skill_names()

    def present(name: str) -> bool:
        return (target_dir / name / "SKILL.md").is_file()

    known = set(_load_known().get(str(target_dir.resolve()), "").split()) or {
        n for n in available if present(n)
    }
    return [n for n in available if n in known and not present(n)]


def record_synced_skills(target_dir: Path) -> None:
    """Record all bundled skill names as synced into ``target_dir``.

    Best-effort: an unwritable state file must never break installs.
    """
    known = _load_known()
    known[str(target_dir.resolve())] = " ".join(skill_names())
    path = _state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=".skills.", suffix=".toml", dir=path.parent
        )
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(tomli_w.dumps({"known": known}).encode())
            os.replace(tmp_name, path)
        except OSError:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise
    except OSError:
        pass
