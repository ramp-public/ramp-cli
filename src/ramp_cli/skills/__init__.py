"""Skill discovery for the verified ramp-public/skills catalog."""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import tomllib
from pathlib import Path

import tomli_w

from ramp_cli.config.settings import config_dir
from ramp_cli.skills.remote import active_skills_dir

AGENT_SKILL_DIRS = [
    ".claude/skills",
    ".codex/skills",
    ".cursor/skills",
    ".windsurf/skills",
]
INSTALLED_SKILL_PREFIX = "ramp-"
_CLI_TO_REMOTE_SKILL_NAMES = {
    "apply-to-ramp": "apply-for-account",
    "incorporate-with-ramp": "incorporate",
}
_REMOTE_TO_CLI_SKILL_NAMES = {
    remote: cli for cli, remote in _CLI_TO_REMOTE_SKILL_NAMES.items()
}


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
    root = active_skills_dir()
    if root is None:
        return []
    return sorted(
        _REMOTE_TO_CLI_SKILL_NAMES.get(d.name, d.name)
        for d in root.iterdir()
        if d.is_dir() and (d / "SKILL.md").is_file()
    )


def _skill_path(name: str) -> Path | None:
    root = active_skills_dir()
    if root is None:
        return None
    remote_name = _CLI_TO_REMOTE_SKILL_NAMES.get(name, name)
    path = root / remote_name / "SKILL.md"
    return path if path.is_file() else None


def list_skills() -> list[dict[str, str]]:
    """Return list of {name, description} dicts for all available skills."""
    skills: list[dict[str, str]] = []
    for name in skill_names():
        path = _skill_path(name)
        assert path is not None
        fm = _parse_frontmatter(path.read_text(encoding="utf-8"))
        desc = fm.get("description", "")
        # Take only the first sentence for the short description
        first_line = desc.split(". ")[0].rstrip(".") if desc else ""
        skills.append({"name": name, "description": first_line})
    return skills


def get_skill_content(name: str) -> str | None:
    """Return full SKILL.md content for a skill, or None if not found."""
    path = _skill_path(name)
    if path:
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


def installed_skill_name(name: str) -> str:
    """Return the namespaced directory and frontmatter name for a skill."""
    return f"{INSTALLED_SKILL_PREFIX}{name}"


def _rewrite_frontmatter_name(content: str, new_name: str) -> str:
    """Rewrite the frontmatter ``name`` field to ``new_name``.

    Only the leading frontmatter block is touched, never a ``name:`` line
    in the body.
    """
    m = re.match(r"^---\s*\n(.*?\n)---", content, re.DOTALL)
    if not m:
        return content
    block, changed = re.subn(
        r"(?m)^name:\s*.*$", f"name: {new_name}", m.group(1), count=1
    )
    if not changed:
        return content
    return content[: m.start(1)] + block + content[m.end(1) :]


def install_skill(name: str, target_dir: Path) -> str:
    """Copy an available skill into target_dir/ramp-<name>/SKILL.md, rewriting
    its frontmatter name to match. Callers own migration and receipts.

    Returns 'installed' or 'updated'.
    """
    source = _skill_path(name)
    if source is None:
        msg = f"Skill not found: {name}"
        raise FileNotFoundError(msg)

    dest_dir = target_dir / installed_skill_name(name)
    dest_file = dest_dir / "SKILL.md"
    status = "updated" if dest_file.exists() else "installed"

    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest_file)

    content = _rewrite_frontmatter_name(
        dest_file.read_text(encoding="utf-8"), installed_skill_name(name)
    )
    needs_inject = target_dir.name == "skills" and target_dir.parent.name == ".claude"
    if needs_inject and "user-invocable:" not in content:
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


_VALID_NAME = re.compile(r"[a-z0-9][a-z0-9-]*")


def _valid_name(name: object) -> bool:
    return isinstance(name, str) and _VALID_NAME.fullmatch(name) is not None


def _load_state() -> dict[str, dict]:
    """Load exact installed names and removal tombstones per target directory."""
    try:
        raw = tomllib.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    if isinstance(raw.get("targets"), dict):
        return _parse_targets(raw["targets"])
    return _migrate_known(raw.get("known"))


def _parse_targets(raw_targets: dict) -> dict[str, dict]:
    state: dict[str, dict] = {}
    for target, raw_entry in raw_targets.items():
        if not isinstance(target, str) or not isinstance(raw_entry, dict):
            continue
        raw_skills = raw_entry.get("skills")
        skills = (
            [name for name in raw_skills if _valid_name(name)]
            if isinstance(raw_skills, list)
            else []
        )
        raw_removed = raw_entry.get("removed")
        removed = (
            [name for name in raw_removed if _valid_name(name)]
            if isinstance(raw_removed, list)
            else []
        )
        if skills or removed:
            state[target] = {
                "skills": sorted(set(skills)),
                "removed": sorted(set(removed)),
            }
    return state


def _migrate_known(raw_known: object) -> dict[str, dict]:
    if not isinstance(raw_known, dict):
        return {}
    state: dict[str, dict] = {}
    for target, names in raw_known.items():
        if not isinstance(target, str) or not isinstance(names, str):
            continue
        skills = [name for name in names.split() if _valid_name(name)]
        if skills:
            state[target] = {"skills": skills, "removed": []}
    return state


def _write_state(state: dict[str, dict]) -> None:
    """Persist receipts atomically, without touching auth configuration.

    A fully drained state removes skills.toml instead of leaving an empty
    stub. Write failures propagate: an install without a durable ownership
    receipt is not a successful install.
    """
    path = _state_path()
    if not state:
        path.unlink(missing_ok=True)
        return
    targets = {
        target: _serialize_entry(entry) for target, entry in sorted(state.items())
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".skills.", suffix=".toml", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(tomli_w.dumps({"targets": targets}).encode())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _serialize_entry(entry: dict) -> dict:
    serialized: dict[str, object] = {}
    if entry["removed"]:
        serialized["removed"] = sorted(entry["removed"])
    if entry["skills"]:
        serialized["skills"] = sorted(entry["skills"])
    return serialized


def previously_removed_skills(target_dir: Path) -> list[str]:
    """Catalog skills that must not be reinstalled by ``install --all``.

    Two signals protect a user's removals: a receipt whose directory no
    longer exists (the skill was deleted by hand) and an entry in the
    target's ``removed`` list (the skill was removed via ``skills
    uninstall``). A target with no recorded state has nothing to protect.
    """
    entry = _load_state().get(str(target_dir.resolve()))
    if entry is None:
        return []
    gone = {
        name
        for name in entry["skills"]
        if not (target_dir / name / "SKILL.md").is_file()
    }
    protected = gone.union(entry["removed"])
    # Either the legacy or the namespaced name protects the catalog skill.
    return [
        name
        for name in skill_names()
        if name in protected or installed_skill_name(name) in protected
    ]


def record_receipt(
    target_dir: Path,
    installed_name: str,
    replaces: str | None = None,
) -> None:
    """Record one successfully installed child directory in ``target_dir``.

    The target plus ``installed_name`` is the exact path Ramp owns. Recording
    it also clears the name from the target's ``removed`` list: an explicit
    install restores a previously removed skill. ``replaces`` retires the
    legacy name's receipt and tombstone in the same write. Write failures
    propagate so the caller can roll back a newly created directory.
    """
    if not _valid_name(installed_name):
        raise ValueError(f"Invalid installed skill name: {installed_name!r}")
    state = _load_state()
    target = str(target_dir.resolve())
    entry = state.setdefault(target, {"skills": [], "removed": []})
    if installed_name not in entry["skills"]:
        entry["skills"].append(installed_name)
    if installed_name in entry["removed"]:
        entry["removed"].remove(installed_name)
    if replaces and replaces != installed_name:
        if replaces in entry["skills"]:
            entry["skills"].remove(replaces)
        if replaces in entry["removed"]:
            entry["removed"].remove(replaces)
    _write_state(state)


def managed_skill_names(target_dir: Path) -> list[str]:
    """Receipt-recorded skill names for ``target_dir``."""
    entry = _load_state().get(str(target_dir.resolve()))
    return sorted(entry["skills"]) if entry else []


def uninstall_skills(
    target_dir: Path,
    names: list[str] | None = None,
) -> list[str]:
    """Remove exact receipt-backed child directories from ``target_dir``."""
    state = _load_state()
    target = str(target_dir.resolve())
    entry = state.get(target)
    if entry is None:
        return []

    managed = set(entry["skills"])
    requested = sorted(managed if names is None else managed & set(names))
    removed: list[str] = []
    for name in requested:
        skill_dir = target_dir / name
        if skill_dir.is_dir():
            shutil.rmtree(skill_dir)
            removed.append(name)
        entry["skills"].remove(name)
        if name not in entry["removed"]:
            entry["removed"].append(name)
        _write_state(state)
    return removed
