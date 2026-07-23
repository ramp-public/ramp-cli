"""Browse and install agent skill instructions."""

from __future__ import annotations

import shutil
from pathlib import Path

import click

from ramp_cli.output.formatter import print_agent_json, resolve_format
from ramp_cli.output.help import BoxHelpFormatter
from ramp_cli.skills import (
    detect_agent_dir,
    get_skill_content,
    install_skill,
    list_skills,
    managed_skill_names,
    previously_removed_skills,
    record_receipt,
    skill_names,
    uninstall_skills,
)


@click.group("skills", help="Browse and install agent skill instructions")
def skills_group() -> None:
    pass


@skills_group.command("list", help="List all available skills")
@click.pass_context
def skills_list(ctx: click.Context) -> None:
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
        if s["name"] in installed_names:
            desc += "  [installed]"
        dl_rows.append((s["name"], desc))
    with formatter.section(f"{len(skills)} Skills"):
        formatter.write_dl(dl_rows)
    click.echo(formatter.getvalue(), nl=False)


@skills_group.command("show", help="Print the SKILL.md for a skill")
@click.argument("skill_name", required=False)
@click.pass_context
def skills_show(ctx: click.Context, skill_name: str | None) -> None:
    available = skill_names()
    if not skill_name:
        raise click.UsageError(
            "Missing skill name. Run 'ramp skills list' to see available skills."
        )
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

    for skill_name_val in names:
        skill_dir = target / skill_name_val
        created_directory = not skill_dir.exists()
        status = install_skill(skill_name_val, target)
        try:
            record_receipt(target, skill_name_val)
        except OSError as exc:
            if created_directory:
                try:
                    shutil.rmtree(skill_dir)
                except OSError as rollback_exc:
                    raise click.ClickException(
                        f"Could not record ownership for {skill_name_val}: {exc}. "
                        f"Rollback also failed for {skill_dir}: {rollback_exc}"
                    ) from rollback_exc
            raise click.ClickException(
                f"Could not record ownership for {skill_name_val}; "
                f"the install was {'rolled back' if created_directory else 'left in place'}: "
                f"{exc}"
            ) from exc
        click.echo(
            f"  {status.capitalize()} {skill_name_val} → {target / skill_name_val}/"
        )

    click.echo(f"\n  {len(names)} skill(s) installed to {target}")
    if removed:
        click.echo(
            f"  Skipped previously removed: {', '.join(removed)} "
            "(restore: ramp skills install <name>)"
        )


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
