"""Click root group and command registration."""

from __future__ import annotations

import os
import sys

import click

from ramp_cli import __version__ as VERSION
from ramp_cli.commands.applications import applications_group
from ramp_cli.commands.auth import auth_group
from ramp_cli.commands.config import config_group
from ramp_cli.commands.env import env_cmd
from ramp_cli.commands.feedback import feedback_cmd
from ramp_cli.commands.skills import skills_group
from ramp_cli.easter_eggs.flip import card_cmd
from ramp_cli.easter_eggs.invoice import invoice_cmd
from ramp_cli.easter_eggs.nyc import nyc_cmd
from ramp_cli.easter_eggs.rampy import rampy_cmd
from ramp_cli.errors import EXIT_RUNTIME, ApiError, AuthRequiredError, RampCLIError
from ramp_cli.output.help import (
    BoxHelpFormatter,
    make_box_formatter,
    suppress_help_text,
)

# ── Display constants ────────────────────────────────────────────────────────

_CATEGORY_REMAP: dict[str, str] = {
    "cards": "funds",
    "agent_cards": "funds",
}

_RESOURCE_HELP: dict[str, str] = {
    "accounting": "Manage tracking categories and GL codes for expense classification",
    "bills": "Review, approve, and manage vendor bills and invoices",
    "funds": "Manage funds (budgets/cards), activate cards, and make agent card payments",
    "general": "Post comments, explain declines, answer policy questions, and search help center",
    "purchase_orders": "Search and view purchase order details",
    "receipts": "Upload and attach receipts to transactions and reimbursements",
    "reimbursements": "Submit, review, and manage out-of-pocket expense reimbursements",
    "requests": "Make and review requests for funds and purchases",
    "transactions": "Search, review, and manage card transaction data and metadata",
    "travel": "Search and book flights, hotels, and manage trip itineraries",
    "users": "Look up user details, org charts, and search the company directory",
}

# ── Formatter patches ────────────────────────────────────────────────────────


def _box_make_formatter(self: click.Context) -> click.HelpFormatter:
    return make_box_formatter(self, _is_agent_mode())


click.Group.format_help_text = suppress_help_text  # type: ignore[method-assign]
click.Command.format_help_text = suppress_help_text  # type: ignore[method-assign]
click.Context.make_formatter = _box_make_formatter  # type: ignore[method-assign]


# ── Group classes ────────────────────────────────────────────────────────────


class ToolGroup(click.Group):
    """A group whose subcommands are agent tools. Labels section 'Tools'
    and shows correct usage: `ramp <resource> <tool> [OPTIONS]`."""

    def format_usage(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        prog = ctx.command_path
        formatter.write_usage(prog, "<tool> [OPTIONS]")

    def format_commands(
        self, ctx: click.Context, formatter: click.HelpFormatter
    ) -> None:
        rows = []
        for name in self.list_commands(ctx):
            cmd = self.get_command(ctx, name)
            if cmd is None or getattr(cmd, "hidden", False):
                continue
            rows.append((name, cmd.get_short_help_str(limit=150)))
        if rows:
            with formatter.section("Tools"):
                formatter.write_dl(rows)

    @staticmethod
    def build(name: str, tools: list, help_text: str) -> ToolGroup:
        """Build a ToolGroup from a list of ToolDefs."""
        from ramp_cli.tools.commands import build_tool_command

        @click.group(
            name=name,
            cls=ToolGroup,
            help=help_text,
            invoke_without_command=True,
        )
        @click.pass_context
        def group(ctx: click.Context) -> None:
            if not ctx.invoked_subcommand:
                click.echo(ctx.get_help())

        for tool in tools:
            group.add_command(build_tool_command(tool), tool.alias or tool.name)

        return group


class RampGroup(click.Group):
    """Root CLI group. Discovers tool categories from the spec and splits
    help into Commands (CLI builtins) and Resources (tool categories)."""

    def format_usage(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        prog = ctx.command_path
        formatter.write_usage(
            prog, f"<command> [OPTIONS]\n       {prog} <resource> <tool> [OPTIONS]"
        )

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        """Hoist root flags to the front so they work anywhere in the command."""
        root_flags = {
            "--env",
            "-e",
            "--output",
            "-o",
            "--quiet",
            "-q",
            "--no-input",
            "--wide",
            "--agent",
            "--human",
        }
        takes_value = {"--env", "-e", "--output", "-o"}
        root_args: list[str] = []
        rest: list[str] = []
        i = 0
        while i < len(args):
            arg = args[i]
            if arg in takes_value:
                root_args.append(arg)
                if i + 1 < len(args):
                    i += 1
                    root_args.append(args[i])
            elif arg in root_flags:
                root_args.append(arg)
            else:
                rest.append(arg)
            i += 1
        return super().parse_args(ctx, root_args + rest)

    def list_commands(self, ctx: click.Context) -> list[str]:
        base = set(super().list_commands(ctx))
        multi, singletons = self._split_categories(ctx)
        visible = set(multi.keys())
        if singletons:
            visible.add("general")
        return sorted(base | visible)

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        # Eagerly-registered commands first (auth, config, etc.)
        cmd = super().get_command(ctx, cmd_name)
        if cmd is not None:
            return cmd

        multi, singletons = self._split_categories(ctx)

        # "general" collects singleton categories
        if cmd_name == "general" and singletons:
            help_text = _RESOURCE_HELP.get(
                "general", f"General ({len(singletons)} tools)"
            )
            return ToolGroup.build("general", singletons, help_text)

        # Multi-tool category group (e.g. "ramp transactions")
        if cmd_name in multi:
            tools = multi[cmd_name]
            fallback = f"{cmd_name.replace('_', ' ').title()} ({len(tools)} tools)"
            help_text = _RESOURCE_HELP.get(cmd_name, fallback)
            return ToolGroup.build(cmd_name, tools, help_text)

        # Flat tool access (e.g. "ramp get-funds") for agents
        from ramp_cli.tools.commands import build_tool_command
        from ramp_cli.tools.registry import get_tool

        tool_def = get_tool(cmd_name, env=self._resolve_env(ctx))
        if tool_def is not None:
            return build_tool_command(tool_def)

        return None

    def format_commands(
        self, ctx: click.Context, formatter: click.HelpFormatter
    ) -> None:
        """Split help into Commands (CLI builtins) and Resources (tool categories)."""
        multi, singletons = self._split_categories(ctx)
        resource_names = set(multi.keys())
        if singletons:
            resource_names.add("general")

        command_rows = []
        resource_rows = []
        for name in self.list_commands(ctx):
            cmd = self.get_command(ctx, name)
            if cmd is None or getattr(cmd, "hidden", False):
                continue
            help_text = cmd.get_short_help_str(limit=150)
            if name in resource_names:
                resource_rows.append((name, help_text))
            else:
                command_rows.append((name, help_text))

        if command_rows:
            with formatter.section("Commands"):
                formatter.write_dl(command_rows)
        if resource_rows:
            with formatter.section("Resources"):
                formatter.write_dl(resource_rows)

    def _resolve_env(self, ctx: click.Context | None) -> str:
        from ramp_cli.config.settings import resolve_environment

        flag_env = (ctx.params.get("flag_env") or "") if ctx else ""
        return resolve_environment(flag_env)

    def _split_categories(self, ctx: click.Context) -> tuple[dict[str, list], list]:
        """Split categories into multi-tool groups and singleton tools.

        Applies _CATEGORY_REMAP to merge related categories (e.g. cards → funds),
        then splits multi-tool groups from singletons.
        """
        from ramp_cli.tools.registry import list_categories

        cats = list_categories(self._resolve_env(ctx))

        # Remap and merge categories
        merged: dict[str, list] = {}
        for cat, tools in cats.items():
            target = _CATEGORY_REMAP.get(cat, cat)
            merged.setdefault(target, []).extend(tools)

        multi: dict[str, list] = {}
        singletons: list = []
        for cat, tools in merged.items():
            if len(tools) > 1:
                multi[cat] = tools
            else:
                singletons.extend(tools)
        return multi, singletons


# ── CLI definition ───────────────────────────────────────────────────────────


@click.group(
    cls=RampGroup,
    help="Ramp Developer CLI",
    context_settings={
        "help_option_names": ["-h", "--help"],
        "auto_envvar_prefix": "RAMP",
    },
)
@click.version_option(VERSION, prog_name="ramp-cli")
@click.option(
    "--env", "-e", "flag_env", default=None, help="Environment: sandbox or production"
)
@click.option(
    "--output", "-o", "flag_output", default=None, help="Output format: json or table"
)
@click.option(
    "--quiet", "-q", is_flag=True, default=False, help="Suppress progress output"
)
@click.option(
    "--no-input", is_flag=True, default=False, help="Disable interactive prompts"
)
@click.option(
    "--wide", is_flag=True, default=False, help="Show all columns in table output"
)
@click.option(
    "--agent",
    "flag_agent",
    is_flag=True,
    default=False,
    help="Machine-readable JSON output (default when piped)",
)
@click.option(
    "--human",
    "flag_human",
    is_flag=True,
    default=False,
    help="Human-readable table output (default in terminal)",
)
@click.pass_context
def cli(
    ctx: click.Context,
    flag_env: str,
    flag_output: str,
    quiet: bool,
    no_input: bool,
    wide: bool,
    flag_agent: bool,
    flag_human: bool,
) -> None:
    """Ramp Developer CLI

    \b
    Authenticate:  ramp auth login
    Browse:        ramp transactions --help
    Use a tool:    ramp funds list --funds_to_retrieve MY_FUNDS
    Environment:   ramp env [sandbox|production]
    """
    if flag_agent and flag_human:
        raise click.UsageError("--agent and --human are mutually exclusive")

    if flag_agent and flag_output and flag_output.lower() == "table":
        click.echo("Warning: --agent overrides -o table; outputting JSON", err=True)
    if flag_human and flag_output and flag_output.lower() == "json":
        click.echo("Warning: --human overrides -o json; outputting table", err=True)

    VALID_ENVS = {"sandbox", "production", "prod"}
    if flag_env is not None and flag_env not in VALID_ENVS:
        raise click.BadParameter(
            f"invalid environment '{flag_env}'. Choose from: {', '.join(sorted(VALID_ENVS))}",
            param_hint="'-e'",
        )

    VALID_FORMATS = {"json", "table"}
    if flag_output is not None and flag_output.lower() not in VALID_FORMATS:
        raise click.BadParameter(
            f"unsupported format '{flag_output}'. Choose from: json, table",
            param_hint="'-o'",
        )

    from ramp_cli.config.settings import load, resolve_environment
    from ramp_cli.output.formatter import set_quiet

    cfg = load()
    env = resolve_environment(flag_env)

    ctx.ensure_object(dict)
    ctx.obj["env"] = env
    ctx.obj["flag_env"] = flag_env
    if flag_agent:
        effective_format = "json"
    elif flag_human:
        effective_format = "table"
    else:
        effective_format = flag_output
    ctx.obj["format"] = effective_format
    ctx.obj["config_format"] = cfg.format
    ctx.obj["quiet"] = quiet
    ctx.obj["no_input"] = no_input or flag_agent
    ctx.obj["wide"] = wide
    ctx.obj["agent_mode"] = flag_agent

    set_quiet(quiet)


# ── Command registration ────────────────────────────────────────────────────

cli.add_command(applications_group)
cli.add_command(auth_group)
cli.add_command(card_cmd)
cli.add_command(config_group)
cli.add_command(env_cmd)
cli.add_command(feedback_cmd)
cli.add_command(invoice_cmd)
cli.add_command(nyc_cmd)
cli.add_command(rampy_cmd)
cli.add_command(skills_group)


# ── Utilities ────────────────────────────────────────────────────────────────


def _is_agent_mode() -> bool:
    """Check if agent mode is active. Uses sys.argv as fallback for pre-parse errors."""
    if "--agent" in sys.argv:
        return True
    if "--human" in sys.argv:
        return False
    return not (hasattr(sys.stdout, "isatty") and sys.stdout.isatty())


def _handle_error(code: int, message: str, *, exit_code: int | None = None) -> None:
    """Print error in agent or human mode and exit.

    code: the error code shown in agent JSON (e.g., HTTP 401 for auth errors).
    exit_code: the POSIX exit status (0-255). Defaults to code if not provided.
    """
    from ramp_cli.output.formatter import print_error_json

    if _is_agent_mode():
        print_error_json(code, message)
    else:
        click.echo(f"Error: {message}", err=True)
    sys.exit(exit_code if exit_code is not None else code)


def main() -> None:
    try:
        cli(standalone_mode=False)
    except click.exceptions.Abort:
        _handle_error(130, "Operation aborted by user")
    except AuthRequiredError as e:
        _handle_error(401, str(e), exit_code=e.code)
    except ApiError as e:
        _handle_error(e.status_code, str(e), exit_code=EXIT_RUNTIME)
    except RampCLIError as e:
        _handle_error(e.code, str(e))
    except click.UsageError as e:
        if _is_agent_mode():
            from ramp_cli.output.formatter import print_error_json

            print_error_json(e.exit_code, e.format_message())
        else:
            BoxHelpFormatter._suppress_wave = True
            try:
                e.show()
            finally:
                BoxHelpFormatter._suppress_wave = False
        sys.exit(e.exit_code)
    except click.ClickException as e:
        if _is_agent_mode():
            from ramp_cli.output.formatter import print_error_json

            print_error_json(e.exit_code, e.format_message())
        else:
            e.show()
        sys.exit(e.exit_code)
    except Exception as e:
        if os.environ.get("RAMP_DEBUG"):
            import traceback

            traceback.print_exc()
        _handle_error(EXIT_RUNTIME, f"{type(e).__name__}: internal error")


if __name__ == "__main__":
    main()
