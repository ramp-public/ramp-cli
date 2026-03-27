"""ramp nyc — secret NYC skyline animation."""

import click

from ramp_cli.animations.nyc import show_nyc


@click.command("nyc", hidden=True, help="\U0001f3d9")
@click.option(
    "--duration", default=10.0, show_default=True, help="Animation duration in seconds"
)
def nyc_cmd(duration: float) -> None:
    show_nyc(duration=duration)
