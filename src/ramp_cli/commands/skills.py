"""Browse and install agent skill instructions."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import click

from ramp_cli.output.formatter import print_agent_json, resolve_format
from ramp_cli.output.help import BoxHelpFormatter
from ramp_cli.skills import (
    detect_agent_dir,
    get_skill_content,
    install_skill,
    installed_skill_name,
    list_skills,
    managed_skill_names,
    previously_removed_skills,
    record_receipt,
    skill_names,
    uninstall_skills,
)
from ramp_cli.skills.remote import (
    active_skills_dir,
    active_skills_version,
    download_skills,
    latest_skills_version,
    requested_skills_version,
)


@click.group("skills", help="Browse and install agent skill instructions")
def skills_group() -> None:
    pass


@skills_group.command("list", help="List all available skills")
@click.pass_context
def skills_list(ctx: click.Context) -> None:
    _ensure_catalog(ctx)
    skills = list_skills()
    fmt = resolve_format(ctx.obj["format"], ctx.obj["config_format"])

    if fmt == "json":
        print_agent_json(skills, pagination=None)
        return

    # Detect installed skills (human mode only — relies on cwd)
    agent_dir = detect_agent_dir()
    installed_names: set[str] = set()
    if agent_dir:
        installed_names = {
            d.name
            for d in agent_dir.iterdir()
            if d.is_dir() and (d / "SKILL.md").is_file()
        }

    formatter = BoxHelpFormatter()
    formatter._suppress_wave = True
    dl_rows = []
    for s in skills:
        desc = s["description"]
        if (
            installed_skill_name(s["name"]) in installed_names
            or s["name"] in installed_names
        ):
            desc += "  [installed]"
        dl_rows.append((s["name"], desc))
    with formatter.section(f"{len(skills)} Skills"):
        formatter.write_dl(dl_rows)
    click.echo(formatter.getvalue(), nl=False)


@skills_group.command("show", help="Print the SKILL.md for a skill")
@click.argument("skill_name", required=False)
@click.pass_context
def skills_show(ctx: click.Context, skill_name: str | None) -> None:
    if not skill_name:
        raise click.UsageError(
            "Missing skill name. Run 'ramp skills list' to see available skills."
        )
    _ensure_catalog(ctx)
    available = skill_names()
    skill_name = skill_name.lower()
    if skill_name not in available:
        raise click.UsageError(
            f"Unknown skill: {skill_name}. Run 'ramp skills list' to see available skills."
        )
    content = get_skill_content(skill_name)
    click.echo(content)


@skills_group.command("install", help="Install skills into an agent skill directory")
@click.argument("name", required=False)
@click.option("--all", "install_all", is_flag=True, help="Install all available skills")
@click.option(
    "--target",
    type=click.Path(file_okay=False, path_type=Path),
    help="Target directory (default: auto-detect agent skill directory)",
)
@click.pass_context
def skills_install(
    ctx: click.Context,
    name: str | None,
    install_all: bool,
    target: Path | None,
) -> None:
    if not name and not install_all:
        raise click.UsageError(
            "Provide a skill name or use --all to install all skills."
        )

    # Resolve target directory
    if target is None:
        target = detect_agent_dir()
        if target is None:
            raise click.UsageError(
                "No agent skill directory found. Use --target to specify one, e.g.:\n"
                "  ramp skills install --all --target .claude/skills"
            )

    downloaded = _ensure_catalog(ctx)
    if not downloaded:
        _offer_catalog_update(ctx)
    available = skill_names()
    removed: list[str] = []

    if install_all:
        removed = previously_removed_skills(target)
        names = [n for n in available if n not in removed]
    else:
        assert name is not None  # guaranteed by the early check above
        if name not in available:
            raise click.BadParameter(
                f"Unknown skill: {name}. Available: {', '.join(available)}",
                param_hint="'NAME'",
            )
        names = [name]

    target.mkdir(parents=True, exist_ok=True)

    installed_count = 0
    for skill_name_val in names:
        if _install_one(
            target,
            skill_name_val,
            install_all=install_all,
        ):
            installed_count += 1

    click.echo(f"\n  {installed_count} skill(s) installed to {target}")
    if removed:
        click.echo(
            f"  Skipped previously removed: {', '.join(removed)} "
            "(restore: ramp skills install <name>)"
        )


@skills_group.command("update", help="Download the latest public skills catalog")
@click.option(
    "--version",
    "version",
    envvar="RAMP_SKILLS_VERSION",
    help="Pin a ramp-public/skills release version (default: latest)",
)
@click.pass_context
def skills_update(ctx: click.Context, version: str | None) -> None:
    """Explicitly update the catalog used by subsequent skill commands."""
    if version is None:
        active_version = active_skills_version()
        latest_version = latest_skills_version(require_immutable=False)
        if active_version is not None and active_version == latest_version:
            if resolve_format(ctx.obj["format"], ctx.obj["config_format"]) == "json":
                print_agent_json(
                    {
                        "updated": False,
                        "version": active_version,
                        "source": "ramp-public/skills",
                    },
                    pagination=None,
                )
            else:
                click.echo(f"Skills catalog is already up to date at {active_version}.")
            return
    try:
        activated = download_skills(version)
    except (RuntimeError, ValueError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
    if resolve_format(ctx.obj["format"], ctx.obj["config_format"]) == "json":
        print_agent_json(
            {
                "updated": True,
                "version": activated,
                "source": "ramp-public/skills",
            },
            pagination=None,
        )
    else:
        click.echo(f"Skills catalog updated to {activated} from ramp-public/skills.")


def _ensure_catalog(ctx: click.Context) -> bool:
    """Download the latest catalog when no verified local copy is active."""
    if active_skills_dir() is not None:
        return False
    if resolve_format(ctx.obj["format"], ctx.obj["config_format"]) != "json":
        click.echo(
            "Ramp skills are now distributed from ramp-public/skills. "
            "Downloading the latest catalog..."
        )
    try:
        download_skills(requested_skills_version())
    except (RuntimeError, ValueError, OSError) as exc:
        raise click.ClickException(
            f"Could not download the Ramp skills catalog and no verified local "
            f"catalog is available: {exc}"
        ) from exc
    return True


def _offer_catalog_update(ctx: click.Context) -> None:
    """Offer remote skills only in a fully interactive human invocation."""
    if (
        ctx.obj.get("no_input")
        or os.environ.get("RAMP_NO_SKILLS_UPDATE_CHECK")
        or not _is_interactive()
        or resolve_format(ctx.obj["format"], ctx.obj["config_format"]) == "json"
    ):
        return None
    version = requested_skills_version() or latest_skills_version()
    if not version or version == active_skills_version():
        return None
    if click.confirm(
        f"Ramp skills {version} is available. Download it before installing?",
        default=False,
    ):
        try:
            download_skills(version)
        except (RuntimeError, ValueError, OSError) as exc:
            click.echo(
                f"Could not update the skills catalog ({exc}); "
                "using the existing catalog.",
                err=True,
            )


def _is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _install_one(
    target: Path,
    name: str,
    *,
    install_all: bool,
) -> bool:
    """Install one available skill into ``target``; True when installed.

    A ramp-<name> directory without a receipt is user-owned: skipped under
    --all, fatal for an explicit install. A receipted legacy install is
    renamed to the namespaced directory first.
    """
    installed_name = installed_skill_name(name)
    skill_dir = target / installed_name
    legacy_dir = target / name
    managed = set(managed_skill_names(target))

    if skill_dir.exists() and installed_name not in managed:
        message = (
            f"{skill_dir} already exists but is not managed by Ramp CLI "
            "(no installation receipt). Move or remove it, then retry."
        )
        if not install_all:
            raise click.ClickException(f"Cannot install {name}: {message}")
        click.echo(f"  Skipped {name}: {message}")
        return False

    created = not skill_dir.exists()
    migrated = False
    try:
        if created and legacy_dir.is_dir() and name in managed:
            legacy_dir.rename(skill_dir)
            migrated = True
        status = install_skill(name, target)
        record_receipt(target, installed_name, replaces=name)
    except OSError as exc:
        raise _undo_failed_install(
            name, skill_dir, legacy_dir, migrated=migrated, created=created, exc=exc
        ) from exc

    click.echo(f"  {status.capitalize()} {installed_name} → {skill_dir}/")
    return True


def _undo_failed_install(
    name: str,
    skill_dir: Path,
    legacy_dir: Path,
    *,
    migrated: bool,
    created: bool,
    exc: OSError,
) -> click.ClickException:
    """Undo a failed install and return the error to raise.

    Rename-back is enough for a migrated directory: its legacy receipt was
    never retired, and a refreshed SKILL.md matches what an update does.
    """
    try:
        if migrated:
            skill_dir.rename(legacy_dir)
            undone = "the migration was rolled back"
        elif created and skill_dir.is_dir():
            shutil.rmtree(skill_dir)
            undone = "the install was rolled back"
        elif not created:
            undone = "the install was left in place"
        else:
            undone = None
    except OSError as rollback_exc:
        return click.ClickException(
            f"Could not install {name}: {exc}. "
            f"Rollback of {skill_dir} also failed: {rollback_exc}"
        )
    if undone:
        return click.ClickException(f"Could not install {name}; {undone}: {exc}")
    return click.ClickException(f"Could not install {name}: {exc}")


@skills_group.command("uninstall", help="Uninstall Ramp-managed skills")
@click.argument("name", required=False)
@click.option(
    "--all", "uninstall_all", is_flag=True, help="Uninstall all managed skills"
)
@click.option(
    "--target",
    type=click.Path(file_okay=False, path_type=Path),
    help="Target directory (default: auto-detect agent skill directory)",
)
@click.option(
    "-y",
    "--yes",
    "assume_yes",
    is_flag=True,
    help="Delete without prompting (for non-interactive use)",
)
def skills_uninstall(
    name: str | None,
    uninstall_all: bool,
    target: Path | None,
    assume_yes: bool,
) -> None:
    if not name and not uninstall_all:
        raise click.UsageError(
            "Provide a skill name or use --all to uninstall all managed skills."
        )
    if name and uninstall_all:
        raise click.UsageError("Provide a skill name or use --all, not both.")

    if target is None:
        target = detect_agent_dir()
        if target is None:
            raise click.UsageError(
                "No agent skill directory found. Use --target to specify one, e.g.:\n"
                "  ramp skills uninstall --all --target ~/.claude/skills"
            )

    managed = managed_skill_names(target)
    if name:
        # Accept the short CLI name for a namespaced receipt.
        if name not in managed and installed_skill_name(name) in managed:
            name = installed_skill_name(name)
        if name not in managed:
            hint = (
                f"Managed skills there: {', '.join(managed)}"
                if managed
                else "No Ramp-managed skills are recorded for that directory."
            )
            raise click.BadParameter(
                f"No installation receipt for '{name}' in {target}. {hint}",
                param_hint="'NAME'",
            )

    requested = managed if uninstall_all else [name]
    if requested:
        _print_uninstall_preview(target, requested)
        if not assume_yes:
            click.confirm("Continue?", default=False, abort=True)

    removed = uninstall_skills(target, None if uninstall_all else [name])
    for skill_name_val in removed:
        click.echo(f"  Uninstalled {skill_name_val} from {target}")
    click.echo(f"\n  {len(removed)} skill(s) uninstalled from {target}")


def _print_uninstall_preview(target: Path, names: list[str]) -> None:
    """Show every receipt-backed path that recursive removal will delete."""
    click.echo(
        "The following managed skill directories and their contents will be removed:\n"
    )
    for name in names:
        skill_dir = target / name
        click.echo(f"  {skill_dir}/")
        if not skill_dir.is_dir():
            click.echo("    (directory already missing; only its receipt will change)")
            continue
        for entry in sorted(skill_dir.rglob("*")):
            relative = entry.relative_to(skill_dir)
            suffix = "/" if entry.is_dir() and not entry.is_symlink() else ""
            click.echo(f"    {relative}{suffix}")
    click.echo(
        "\nThese directories may contain files you created or modified. "
        "Confirming will delete them too."
    )
