"""ramp env [sandbox|production] command."""

from __future__ import annotations

import click

from ramp_cli.config import settings
from ramp_cli.output.formatter import print_agent_json, resolve_format


class EnvCommand(click.Command):
    """Custom command that adds a 'Commands' section listing environments."""

    def format_help(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        self.format_usage(ctx, formatter)
        with formatter.section("Commands"):
            formatter.write_dl(
                [
                    ("sandbox", "Operate in demo.ramp.com"),
                    ("production", "Operate in app.ramp.com"),
                ]
            )
        self.format_epilog(ctx, formatter)


@click.command("env", cls=EnvCommand)
@click.argument("environment", required=False, default=None)
@click.pass_context
def env_cmd(ctx: click.Context, environment: str | None) -> None:
    """Show or set the default environment."""
    if environment is None:
        fmt = resolve_format(ctx.obj["format"], ctx.obj["config_format"])
        if fmt == "json":
            print_agent_json({"environment": ctx.obj["env"]}, pagination=None)
        else:
            click.echo(ctx.obj["env"])
        return

    if environment == "prod":
        environment = "production"

    if environment not in ("sandbox", "production"):
        raise click.BadParameter(
            f"Unknown environment {environment!r} — use 'sandbox' or 'production'"
        )

    cfg = settings.load()
    cfg.environment = environment
    settings.save(cfg)
    click.echo(f"Default environment set to {environment}.")
