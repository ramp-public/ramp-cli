"""Click root group and command registration."""

from __future__ import annotations

import os
import sys
import traceback
from dataclasses import dataclass
from enum import StrEnum

import click

from ramp_cli import __version__ as VERSION
from ramp_cli.commands.applications import applications_group
from ramp_cli.commands.auth import auth_group
from ramp_cli.commands.config import config_group
from ramp_cli.commands.env import env_cmd
from ramp_cli.commands.feedback import feedback_cmd
from ramp_cli.commands.skills import skills_group
from ramp_cli.commands.tools import tools_group
from ramp_cli.commands.update import update_cmd
from ramp_cli.config.settings import load, resolve_environment
from ramp_cli.easter_eggs.flip import card_cmd
from ramp_cli.easter_eggs.invoice import invoice_cmd
from ramp_cli.easter_eggs.nyc import nyc_cmd
from ramp_cli.easter_eggs.rampy import rampy_cmd
from ramp_cli.errors import EXIT_RUNTIME, ApiError, AuthRequiredError, RampCLIError
from ramp_cli.output.formatter import print_error_json, set_quiet
from ramp_cli.output.help import (
    BoxHelpFormatter,
    make_box_formatter,
    suppress_help_text,
)
from ramp_cli.specs.sync import maybe_sync
from ramp_cli.tools.commands import build_tool_command
from ramp_cli.tools.registry import get_tool, list_categories
from ramp_cli.version_check import check_for_update, emit_update_notice

# ── Enums & data ─────────────────────────────────────────────────────────────


class OutputMode(StrEnum):
    JSON = "json"
    TABLE = "table"


class Resource(StrEnum):
    """Tool categories exposed as CLI resource groups."""

    ACCOUNTING = "accounting"
    BILLS = "bills"
    FUNDS = "funds"
    GENERAL = "general"
    PURCHASE_ORDERS = "purchase_orders"
    RECEIPTS = "receipts"
    REIMBURSEMENTS = "reimbursements"
    REQUESTS = "requests"
    TRANSACTIONS = "transactions"
    TRAVEL = "travel"
    TREASURY = "treasury"
    USERS = "users"
    VENDORS = "vendors"

    @property
    def help_text(self) -> str:
        return _RESOURCE_HELP[self.value]


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
    "treasury": "Query treasury account balances, transfers, and investment positions",
    "users": "Look up user details, org charts, and search the company directory",
    "vendors": "Upload and manage vendor documents and track bulk upload progress",
}

CATEGORY_REMAP: dict[str, str] = {
    "cards": Resource.FUNDS,
    "agent_cards": Resource.FUNDS,
}

VALID_ENVS = frozenset({"sandbox", "production", "prod"})
VALID_FORMATS = frozenset({m.value for m in OutputMode})


@dataclass(slots=True)
class CLIContext:
    """Typed container for ctx.obj — replaces the raw dict."""

    env: str
    flag_env: str | None
    format: str | None
    config_format: str
    quiet: bool
    no_input: bool
    wide: bool
    agent_mode: bool

    @classmethod
    def from_params(
        cls,
        flag_env: str | None,
        flag_output: str | None,
        quiet: bool,
        no_input: bool,
        wide: bool,
        flag_agent: bool,
        flag_human: bool,
    ) -> CLIContext:
        if flag_agent:
            fmt = OutputMode.JSON
        elif flag_human:
            fmt = OutputMode.TABLE
        else:
            fmt = flag_output

        cfg = load()
        return cls(
            env=resolve_environment(flag_env),
            flag_env=flag_env,
            format=fmt,
            config_format=cfg.format,
            quiet=quiet,
            no_input=no_input or flag_agent,
            wide=wide,
            agent_mode=flag_agent,
        )

    def to_dict(self) -> dict:
        """Convert to dict for ctx.obj (Click expects a dict)."""
        return {
            "env": self.env,
            "flag_env": self.flag_env,
            "format": self.format,
            "config_format": self.config_format,
            "quiet": self.quiet,
            "no_input": self.no_input,
            "wide": self.wide,
            "agent_mode": self.agent_mode,
        }


# ── Arg parsing ──────────────────────────────────────────────────────────────

_ROOT_FLAGS = frozenset(
    {
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
)
_FLAGS_WITH_VALUE = frozenset({"--env", "-e", "--output", "-o"})


def _split_root_flags(args: list[str]) -> tuple[list[str], list[str]]:
    """Separate root flags from subcommand args."""
    root: list[str] = []
    rest: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in _FLAGS_WITH_VALUE:
            root.append(arg)
            if i + 1 < len(args):
                i += 1
                root.append(args[i])
        elif arg in _ROOT_FLAGS:
            root.append(arg)
        else:
            rest.append(arg)
        i += 1
    return root, rest


# ── Formatter patches ────────────────────────────────────────────────────────


def _box_make_formatter(self: click.Context) -> click.HelpFormatter:
    return make_box_formatter(self, _is_agent_mode())


click.Group.format_help_text = suppress_help_text  # type: ignore[method-assign]
click.Command.format_help_text = suppress_help_text  # type: ignore[method-assign]
click.Context.make_formatter = _box_make_formatter  # type: ignore[method-assign]


# ── Group classes ────────────────────────────────────────────────────────────


class ToolGroup(click.Group):
    """A group whose subcommands are agent tools."""

    def format_usage(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        formatter.write_usage(ctx.command_path, "<tool> [OPTIONS]")

    def format_commands(
        self, ctx: click.Context, formatter: click.HelpFormatter
    ) -> None:
        rows = [
            (name, cmd.get_short_help_str(limit=150))
            for name in self.list_commands(ctx)
            if (cmd := self.get_command(ctx, name))
            and not getattr(cmd, "hidden", False)
        ]
        if rows:
            with formatter.section("Tools"):
                formatter.write_dl(rows)

    @staticmethod
    def build(name: str, tools: list, help_text: str) -> ToolGroup:
        @click.group(
            name=name, cls=ToolGroup, help=help_text, invoke_without_command=True
        )
        @click.pass_context
        def group(ctx: click.Context) -> None:
            if not ctx.invoked_subcommand:
                click.echo(ctx.get_help())

        for tool in tools:
            group.add_command(build_tool_command(tool), tool.alias or tool.name)
        return group


class RampGroup(click.Group):
    """Root CLI group with flag hoisting."""

    def format_usage(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        prog = ctx.command_path
        formatter.write_usage(
            prog, f"<command> [OPTIONS]\n       {prog} <resource> <tool> [OPTIONS]"
        )

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        root, rest = _split_root_flags(args)
        return super().parse_args(ctx, root + rest)

    def list_commands(self, ctx: click.Context) -> list[str]:
        base = set(super().list_commands(ctx))
        multi, singletons = self._split_categories(ctx)
        resource_names = set(multi.keys())
        if singletons:
            resource_names.add(Resource.GENERAL)
        return sorted(base | resource_names)

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        if cmd := super().get_command(ctx, cmd_name):
            return cmd

        multi, singletons = self._split_categories(ctx)

        if cmd_name == Resource.GENERAL and singletons:
            return ToolGroup.build(
                Resource.GENERAL,
                singletons,
                _RESOURCE_HELP.get(
                    Resource.GENERAL, f"General ({len(singletons)} tools)"
                ),
            )

        if cmd_name in multi:
            tools = multi[cmd_name]
            aliases = ", ".join(sorted(t.alias or t.name for t in tools))
            fallback = f"{cmd_name.replace('_', ' ').title()} \u2014 {aliases}"
            return ToolGroup.build(
                cmd_name, tools, _RESOURCE_HELP.get(cmd_name, fallback)
            )

        if tool_def := get_tool(cmd_name, env=self._resolve_env(ctx)):
            return build_tool_command(tool_def)

        return None

    def format_commands(
        self, ctx: click.Context, formatter: click.HelpFormatter
    ) -> None:
        multi, singletons = self._split_categories(ctx)
        resource_names = set(multi.keys())
        if singletons:
            resource_names.add(Resource.GENERAL)

        command_rows, resource_rows = [], []
        for name in self.list_commands(ctx):
            cmd = self.get_command(ctx, name)
            if cmd is None or getattr(cmd, "hidden", False):
                continue
            row = (name, cmd.get_short_help_str(limit=150))
            (resource_rows if name in resource_names else command_rows).append(row)

        if command_rows:
            with formatter.section("Commands"):
                formatter.write_dl(command_rows)
        if resource_rows:
            with formatter.section("Resources"):
                formatter.write_dl(resource_rows)

    def _resolve_env(self, ctx: click.Context | None) -> str:
        flag_env = (ctx.params.get("flag_env") or "") if ctx else ""
        return resolve_environment(flag_env)

    def _split_categories(self, ctx: click.Context) -> tuple[dict[str, list], list]:
        env = self._resolve_env(ctx)
        if not getattr(ctx, "_ramp_synced", False):
            maybe_sync(env)
            ctx._ramp_synced = True  # type: ignore[attr-defined]
        cats = list_categories(env)

        merged: dict[str, list] = {}
        for cat, tools in cats.items():
            merged.setdefault(CATEGORY_REMAP.get(cat, cat), []).extend(tools)

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
    _validate_flags(flag_env, flag_output, flag_agent, flag_human)

    cli_ctx = CLIContext.from_params(
        flag_env=flag_env,
        flag_output=flag_output,
        quiet=quiet,
        no_input=no_input,
        wide=wide,
        flag_agent=flag_agent,
        flag_human=flag_human,
    )
    ctx.ensure_object(dict)
    ctx.obj.update(cli_ctx.to_dict())
    set_quiet(quiet)


def _validate_flags(
    flag_env: str | None,
    flag_output: str | None,
    flag_agent: bool,
    flag_human: bool,
) -> None:
    if flag_agent and flag_human:
        raise click.UsageError("--agent and --human are mutually exclusive")

    if flag_agent and flag_output and flag_output.lower() == OutputMode.TABLE:
        click.echo("Warning: --agent overrides -o table; outputting JSON", err=True)
    if flag_human and flag_output and flag_output.lower() == OutputMode.JSON:
        click.echo("Warning: --human overrides -o json; outputting table", err=True)

    if flag_env is not None and flag_env not in VALID_ENVS:
        raise click.BadParameter(
            f"invalid environment '{flag_env}'. Choose from: {', '.join(sorted(VALID_ENVS))}",
            param_hint="'-e'",
        )
    if flag_output is not None and flag_output.lower() not in VALID_FORMATS:
        raise click.BadParameter(
            f"unsupported format '{flag_output}'. Choose from: json, table",
            param_hint="'-o'",
        )


# ── Command registration ────────────────────────────────────────────────────

for _cmd in (
    applications_group,
    auth_group,
    card_cmd,
    config_group,
    env_cmd,
    feedback_cmd,
    invoice_cmd,
    nyc_cmd,
    rampy_cmd,
    skills_group,
    tools_group,
    update_cmd,
):
    cli.add_command(_cmd)


# ── Error handling ───────────────────────────────────────────────────────────


def _is_agent_mode() -> bool:
    if "--agent" in sys.argv:
        return True
    if "--human" in sys.argv:
        return False
    return not (hasattr(sys.stdout, "isatty") and sys.stdout.isatty())


def _emit_error(code: int, message: str) -> None:
    if _is_agent_mode():
        print_error_json(code, message)
    else:
        click.echo(f"Error: {message}", err=True)


def main() -> None:
    check_for_update()
    try:
        cli(standalone_mode=False)
    except click.exceptions.Abort:
        _emit_error(130, "Operation aborted by user")
        sys.exit(130)
    except AuthRequiredError as e:
        _emit_error(401, str(e))
        sys.exit(e.code)
    except ApiError as e:
        _emit_error(e.status_code, str(e))
        sys.exit(EXIT_RUNTIME)
    except RampCLIError as e:
        _emit_error(e.code, str(e))
        sys.exit(e.code)
    except click.UsageError as e:
        if _is_agent_mode():
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
            print_error_json(e.exit_code, e.format_message())
        else:
            e.show()
        sys.exit(e.exit_code)
    except Exception as e:
        if os.environ.get("RAMP_DEBUG"):
            traceback.print_exc()
        _emit_error(EXIT_RUNTIME, f"{type(e).__name__}: internal error")
        sys.exit(EXIT_RUNTIME)
    finally:
        try:
            emit_update_notice(_is_agent_mode())
        except Exception:
            pass


if __name__ == "__main__":
    main()
