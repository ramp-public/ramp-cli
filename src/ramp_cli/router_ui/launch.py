"""Dispatch without importing Textual in scripts, help, or machine-mode commands."""

import os
import sys

import click

from ramp_cli.output.formatter import resolve_format

DISPLAY_KEY = "ramp_cli.router_ui.display"
DEFAULT_INLINE_HEIGHT = 38


def can_launch(ctx: click.Context) -> bool:
    """Whether commands open the workspace on their own. Only RAMP_TUI=1 does;
    otherwise they keep the line-by-line CLI and `ramp router ui` opens it."""
    return interactive_terminal(ctx) and auto_launch()


def auto_launch() -> bool:
    return os.environ.get("RAMP_TUI") == "1"


def interactive_terminal(ctx: click.Context) -> bool:
    state = ctx.obj or {}
    return (
        not state.get("agent_mode")
        and not state.get("no_input")
        and not state.get("quiet")
        and os.environ.get("TERM") != "dumb"
        and os.environ.get("RAMP_NO_TUI") != "1"
        and resolve_format(state.get("format"), state.get("config_format", ""))
        == "table"
        and sys.stdin.isatty()
        and sys.stdout.isatty()
        # Textual's drivers draw on the process's original stderr, so a
        # redirected one would capture the keyboard and show nothing.
        and _is_terminal(sys.__stderr__)
    )


def _is_terminal(stream) -> bool:
    try:
        return stream is not None and stream.isatty()
    except (OSError, ValueError):
        return False


def _workspace_origin(options: dict) -> str | None:
    """The Router account the workspace acts on: an explicit origin, else the
    deployment a configure --ui-url or --base-url names, else the saved one."""
    if options.get("origin"):
        return options["origin"]
    if options.get("ui_url"):
        return options["ui_url"]
    base_url = (options.get("base_url") or "").rstrip("/")
    if not base_url:
        return None
    from ramp_cli.commands import router  # noqa: PLC0415 - router imports us

    known = router.KNOWN_DEPLOYMENT_UI_URLS.get(base_url)
    return known or base_url.removesuffix("/v1").rstrip("/")


def launch(
    ctx: click.Context,
    page: str = "home",
    *,
    explicit: bool = False,
    **options,
) -> bool:
    if not (can_launch(ctx) or (explicit and interactive_terminal(ctx))):
        return False
    # Set by `ramp router` / `ramp router ui` from --inline, --height, --theme.
    display = ctx.meta.get(DISPLAY_KEY, {})
    inline = display.get("inline", False)
    inline_height = display.get("inline_height", DEFAULT_INLINE_HEIGHT)
    theme_mode = display.get("theme_mode")
    if inline and sys.platform == "win32":
        raise click.UsageError(
            "Inline Router UI is supported on macOS/Linux. Omit --inline on Windows."
        )
    # Keep the full-screen framework out of every noninteractive command.
    from ramp_cli.router_ui.app import RouterApp  # noqa: PLC0415
    from ramp_cli.router_ui.service import RouterService  # noqa: PLC0415

    service = RouterService(dict(ctx.obj), origin=_workspace_origin(options))
    app = RouterApp(
        service,
        page=page,
        options=options,
        inline_height=inline_height if inline else None,
        theme_mode=theme_mode,
    )
    try:
        if inline:
            app.run(inline=True, inline_no_clear=False)
        else:
            app.run()
    finally:
        service.close()
    return True
