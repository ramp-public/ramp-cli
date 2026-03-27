"""Skill discovery — locate and parse bundled SKILL.md files.

Skills are bundled inside this package directory.  Each subdirectory that
contains a SKILL.md is a skill.  The path is resolved via __file__ so it
works in both editable and installed (wheel) builds.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

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
        fm = _parse_frontmatter(path.read_text())
        desc = fm.get("description", "")
        # Take only the first sentence for the short description
        first_line = desc.split(". ")[0].rstrip(".") if desc else ""
        skills.append({"name": name, "description": first_line})
    return skills


def get_skill_content(name: str) -> str | None:
    """Return full SKILL.md content for a skill, or None if not found."""
    path = SKILLS_DIR / name / "SKILL.md"
    if path.is_file():
        return path.read_text()
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
        content = dest_file.read_text()
        if "user-invocable:" not in content:
            content = content.replace(
                "\n---\n",
                "\nuser-invocable: true\n---\n",
                1,
            )
            dest_file.write_text(content)

    return status
