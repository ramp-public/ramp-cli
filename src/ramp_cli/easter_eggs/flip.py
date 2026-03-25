"""ramp card — secret card flip animation."""

import click


@click.command("card", hidden=True, help="\U0001f0cf")
@click.option(
    "--duration", default=10.0, show_default=True, help="Animation duration in seconds"
)
def card_cmd(duration: float) -> None:
    from ramp_cli.animations.card import show_card

    show_card(duration=duration)
