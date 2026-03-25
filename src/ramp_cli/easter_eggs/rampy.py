"""ramp rampy — secret Rampy animations."""

import click


@click.command("rampy", hidden=False, help="Meet Rampy (--skate, --surf, --coin-game)")
@click.option("--skate", is_flag=True, default=False, help="Skateboard animation")
@click.option("--surf", is_flag=True, default=False, help="Surfer animation")
@click.option("--coin-game", is_flag=True, default=False, help="Coin chase game")
@click.option(
    "--duration", default=10.0, show_default=True, help="Animation duration in seconds"
)
def rampy_cmd(skate: bool, surf: bool, coin_game: bool, duration: float) -> None:
    modes = sum([skate, surf, coin_game])
    if modes > 1:
        raise click.UsageError(
            "--skate, --surf, and --coin-game are mutually exclusive"
        )

    if skate:
        from ramp_cli.animations.rampy import show_rampy

        show_rampy(duration=duration)
    elif surf:
        from ramp_cli.animations.rampy_surf import show_rampy_surf

        show_rampy_surf(duration=duration)
    elif coin_game:
        from ramp_cli.output.rampy_coin_game import show_coin_game

        show_coin_game()
    else:
        # Default — idle standing Rampy
        from ramp_cli.animations.rampy_idle import show_rampy_idle

        show_rampy_idle(duration=duration)
