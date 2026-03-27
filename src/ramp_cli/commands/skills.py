"""Browse and install agent skill instructions."""

from __future__ import annotations

from pathlib import Path

import click

from ramp_cli.output.formatter import print_agent_json, resolve_format
from ramp_cli.output.help import BoxHelpFormatter
from ramp_cli.skills import (
    detect_agent_dir,
    get_skill_content,
    install_skill,
    list_skills,
    skill_names,
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

    if install_all:
        names = available
    else:
        assert name is not None  # guaranteed by the early check above
        if name not in available:
            raise click.BadParameter(
                f"Unknown skill: {name}. Available: {', '.join(available)}",
                param_hint="'NAME'",
            )
        names = [name]

    for skill_name_val in names:
        status = install_skill(skill_name_val, target)
        click.echo(
            f"  {status.capitalize()} {skill_name_val} → {target / skill_name_val}/"
        )

    click.echo(f"\n  {len(names)} skill(s) installed to {target}")
