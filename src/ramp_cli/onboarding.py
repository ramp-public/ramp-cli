"""First-time user experience: welcome tips, sample prompts, and category hints.

Tracks onboarding state in config.toml (first_login_at, tools_used) and
surfaces contextual guidance at key moments:

  1. Post-login   — welcome message with sample prompts (first OAuth only)
  2. First tool   — per-category tips when a resource is used for the first time
  3. On demand    — `ramp getting-started` command for a full orientation
"""

from __future__ import annotations

import sys
import time
from typing import TextIO

from ramp_cli.config import settings
from ramp_cli.config.constants import environment_usage
from ramp_cli.output.formatter import print_agent_json

# ── Sample prompts by category ──────────────────────────────────────────────

CATEGORY_SAMPLES: dict[str, list[str]] = {
    "transactions": [
        "ramp transactions list --transactions_to_retrieve MY_TRANSACTIONS",
        "ramp transactions search --query 'coffee'",
    ],
    "funds": [
        "ramp funds list --funds_to_retrieve MY_FUNDS",
        "ramp funds create --json '{...}'  (create a budget or card)",
    ],
    "bills": [
        "ramp bills search --query 'vendor name'",
        "ramp bills pending  (bills awaiting your approval)",
    ],
    "reimbursements": [
        "ramp reimbursements list",
        "ramp reimbursements submit --json '{...}'",
    ],
    "users": [
        "ramp users me  (your profile)",
        "ramp users search --query 'name'",
    ],
    "accounting": [
        "ramp accounting categories  (tracking categories & GL codes)",
    ],
    "travel": [
        "ramp travel search-flight  (search flights)",
    ],
    "receipts": [
        "ramp receipts upload <TRANSACTION_ID> --file_path receipt.pdf",
    ],
    "purchase_orders": [
        "ramp purchase_orders search",
    ],
    "requests": [
        "ramp requests list",
    ],
    "treasury": [
        "ramp treasury balances",
    ],
    "vendors": [
        "ramp vendors upload --json '{...}'",
    ],
    "general": [
        "ramp general explain-decline <TRANSACTION_ID>",
        "ramp general ask-policy --question 'Can I expense ...'",
    ],
}

# ── Category discovery tips ─────────────────────────────────────────────────

CATEGORY_TIPS: dict[str, str] = {
    "transactions": (
        "Tip: Add --wide to see all columns, or pipe to jq for scripting:\n"
        "       ramp transactions list --agent | jq '.data'"
    ),
    "funds": (
        "Tip: Use 'ramp funds list --funds_to_retrieve ALL_FUNDS' to see "
        "every budget and card in your organization."
    ),
    "bills": (
        "Tip: 'ramp bills pending' shows bills waiting for your approval. "
        "Combine with --agent for automation."
    ),
    "reimbursements": (
        "Tip: Attach a receipt when submitting: "
        "'ramp receipts upload <ID> --file_path receipt.pdf'"
    ),
    "users": (
        "Tip: 'ramp users me' returns your own profile — useful for "
        "verifying auth and finding your user ID."
    ),
    "accounting": (
        "Tip: Use tracking categories to classify expenses by department, "
        "project, or custom dimensions."
    ),
    "travel": (
        "Tip: Combine flight search with --agent to build automated booking workflows."
    ),
    "receipts": (
        "Tip: Upload receipts right after a transaction to keep compliance green."
    ),
    "general": (
        "Tip: 'explain-decline' tells you why a card was declined "
        "and suggests how to fix it."
    ),
}


# ── State helpers ────────────────────────────────────────────────────────────


def is_first_login() -> bool:
    """True if no prior successful login has been recorded."""
    cfg = settings.load()
    return cfg.first_login_at == 0


def record_first_login() -> None:
    """Stamp the first-login timestamp (idempotent — only writes once)."""
    cfg = settings.load()
    if cfg.first_login_at == 0:
        cfg.first_login_at = int(time.time())
        settings.save(cfg)


def get_used_categories() -> set[str]:
    """Return the set of categories the user has invoked at least once."""
    cfg = settings.load()
    if not cfg.tools_used:
        return set()
    return set(cfg.tools_used.split())


def record_category_used(category: str) -> bool:
    """Record that *category* was used.  Returns True if this was the first time."""
    cfg = settings.load()
    used = set(cfg.tools_used.split()) if cfg.tools_used else set()
    if category in used:
        return False
    used.add(category)
    cfg.tools_used = " ".join(sorted(used))
    settings.save(cfg)
    return True


# ── Welcome message (post-first-login) ──────────────────────────────────────


def show_welcome(env: str, file: TextIO | None = None) -> None:
    """Print a welcome guide after the user's first successful OAuth login."""
    file = file or sys.stderr
    lines = [
        "",
        "  Welcome to the Ramp CLI!  Here are a few things to try:",
        "",
        "  Quick start:",
        "    ramp funds enroll                       — set up Ramp Agent Cards",
        "    ramp users me                            — verify your identity",
        "    ramp transactions list \\",
        "      --transactions_to_retrieve MY_TRANSACTIONS  — your recent transactions",
        "    ramp bills pending                       — bills awaiting approval",
        "",
        "  Explore further:",
        "    ramp --help                              — all commands & resources",
        "    ramp getting-started                     — interactive orientation",
        "    ramp skills list                         — agent skill instructions",
        "",
        f"  Environment: {env}",
        f"  Change default:  ramp env [{environment_usage()}]",
        "",
    ]
    file.write("\n".join(lines))
    file.flush()


# ── First-tool-call hint ────────────────────────────────────────────────────


def maybe_show_category_tip(category: str, file: TextIO | None = None) -> None:
    """If *category* is being used for the first time, print a contextual tip."""
    if not sys.stderr.isatty():
        return
    first_time = record_category_used(category)
    if not first_time:
        return
    tip = CATEGORY_TIPS.get(category)
    if not tip:
        return
    file = file or sys.stderr
    file.write(f"\n  {tip}\n\n")
    file.flush()


# ── Getting-started command output ──────────────────────────────────────────


def print_getting_started(
    env: str,
    scopes: set[str],
    categories: dict[str, list],
    is_json: bool = False,
    file: TextIO | None = None,
) -> None:
    """Render the full getting-started orientation.

    Parameters
    ----------
    env : str
        Current environment name.
    scopes : set[str]
        OAuth scopes granted to the current token.
    categories : dict[str, list[ToolDef]]
        Tool categories available (from the registry).
    is_json : bool
        If True, output machine-readable JSON for agent consumption.
    file : TextIO | None
        Output stream (defaults to stdout).
    """
    file = file or sys.stdout
    used = get_used_categories()
    available_cats = sorted(categories.keys())

    if is_json:
        # Machine-readable output for agent integrations
        data: dict = {
            "environment": env,
            "scopes_granted": len(scopes),
            "categories_available": available_cats,
            "categories_explored": sorted(used),
            "categories_unexplored": sorted(set(available_cats) - used),
            "sample_prompts": {},
        }
        for cat in available_cats:
            if cat in CATEGORY_SAMPLES:
                data["sample_prompts"][cat] = CATEGORY_SAMPLES[cat]
        print_agent_json(data, pagination=None)
        return

    # Human-readable orientation
    lines: list[str] = []
    lines.append("")
    lines.append("  Getting Started with the Ramp CLI")
    lines.append("  " + "=" * 35)
    lines.append("")

    # Status summary
    lines.append(f"  Environment:          {env}")
    lines.append(f"  Scopes granted:       {len(scopes)}")
    lines.append(f"  Resources explored:   {len(used)}/{len(available_cats)}")
    lines.append("")

    # Categorize resources as explored vs unexplored
    unexplored = sorted(set(available_cats) - used)
    if unexplored:
        lines.append("  Resources to explore:")
        for cat in unexplored:
            tools = categories.get(cat, [])
            tool_count = len(tools)
            display = cat.replace("_", " ").title()
            lines.append(f"    {display:<22s} ({tool_count} tools)")
            samples = CATEGORY_SAMPLES.get(cat, [])
            if samples:
                lines.append(f"      Try: {samples[0]}")
        lines.append("")

    if used:
        lines.append("  Resources you've used:")
        for cat in sorted(used):
            if cat in available_cats:
                tools = categories.get(cat, [])
                display = cat.replace("_", " ").title()
                lines.append(f"    {display:<22s} ({len(tools)} tools)")
        lines.append("")

    # Quick reference
    lines.append("  Quick reference:")
    lines.append("    ramp <resource> --help        — see tools in a resource")
    lines.append("    ramp <resource> <tool> --help  — see tool options")
    lines.append("    ramp tools list               — all available tools")
    lines.append("    ramp skills list              — agent skill instructions")
    lines.append("    ramp --agent                  — machine-readable JSON output")
    lines.append("")

    file.write("\n".join(lines) + "\n")
    file.flush()
