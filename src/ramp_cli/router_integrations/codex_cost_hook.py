"""Release-pinned Codex cost hook installed by ramp-cli."""

CODEX_COST_HOOK_SCRIPT = r'''#!/usr/bin/env python3
"""Codex Stop hook: actual session cost vs. Opus 5, served by the router.

Codex has no command-backed status line, and its built-in cost display only
works with ChatGPT-plan authentication, so router users get no cost feedback
in the TUI. This hook is the Codex counterpart to claude-code-statusline: it
runs after each turn, asks the router's API-key-authenticated session usage
endpoint for the session's last routed model (`last_model`), its real cost
(server-computed `spend_usd`), and a like-for-like Opus 5 reference cost
(`reference_cost_usd`), then reports them as a plain-text multiline
`systemMessage`, which Codex renders in the transcript:

    Routed to: GPT-5.6 Sol  -49% vs Claude Opus 5
    Ramp          ████████████░░░░░░░░░░░░ $0.07
    Claude Opus 5 ████████████████████████ $0.14

The routed model is omitted when the endpoint does not return it, so the
hook works against older control planes. Usage events are ingested
asynchronously, so the line reflects the session as settled at the previous
turn: the first turn typically reports nothing, and later turns lag by one
turn's cost.

Codex passes the Stop event JSON on stdin; `session_id` is the thread id,
which the router records as the session's `client_session_id`.

This script ships with the Router dashboard: it is served at
<dashboard origin>/codex-cost-hook (for Ramp, https://router.ramp.com).

Install:
  1. Download it from your deployment's dashboard origin and make it executable:
       mkdir -p ~/.codex && \
         curl -fsSL <dashboard origin>/codex-cost-hook \
           -o ~/.codex/codex-cost-hook && \
         chmod +x ~/.codex/codex-cost-hook
  2. Register it as a Stop hook in ~/.codex/config.toml:
       [[hooks.Stop]]
       hooks = [{ type = "command", command = "~/.codex/codex-cost-hook" }]
  3. Set ROUTER_API_KEY to a Router LLM API key and ROUTER_BASE_URL to the
     same dashboard origin in the environment Codex runs in.

Configuration:
  ROUTER_API_KEY   Router LLM API key. Falls back to OPENAI_API_KEY, the
                   conventional env_key for router-configured Codex providers.
  ROUTER_BASE_URL  Router control-plane base URL (a trailing /v1 is dropped).

Any failure — missing configuration, an unknown session (usage lands in the
router asynchronously), or an HTTP error — prints nothing, so the transcript
stays clean. The API key is never written to disk or logged.
"""

import json
import os
import sys
import urllib.error
import urllib.request
from decimal import Decimal


def _api_key() -> str:
    """The Router LLM API key, never logged or persisted."""
    return (os.environ.get("ROUTER_API_KEY") or os.environ.get("OPENAI_API_KEY") or "").strip()


def _base_url() -> str:
    """Router base URL with no path (the endpoint path is appended).

    A base URL pointing at the data plane (…/v1) shares its origin with the
    control-plane route on single-origin deployments, so drop the /v1 path.
    There is no default host: unset means degrade to no output rather than
    guess a destination for the caller's credential.
    """
    url = (os.environ.get("ROUTER_BASE_URL") or "").strip().rstrip("/")
    if url.endswith("/v1"):
        url = url[: -len("/v1")]
    return url


def _fetch_session_usage(base_url: str, api_key: str, session_id: str):
    """Fetch the session's router cost and Opus 5 reference cost.

    Returns the parsed payload, or None on any failure (the caller degrades
    to no output). Never raises; never logs the key.
    """
    from urllib.parse import urlencode

    query = urlencode(
        {
            "client_session_id": session_id,
            "include_last_model": "true",
        }
    )
    request = urllib.request.Request(
        f"{base_url}/session-usage/usage/session?{query}",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            # urllib's default UA is blocked by the edge WAF (Cloudflare 1010).
            "User-Agent": "router-codex-cost-hook/1.0",
        },
    )
    try:
        # Codex waits for Stop hooks before finishing the turn, so fail open
        # quickly when the usage endpoint is slow or unavailable.
        with urllib.request.urlopen(request, timeout=2) as response:
            payload = json.loads(response.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError) as _error:
        return None
    return payload if isinstance(payload, dict) else None


def _reference_label(reference_model: str) -> str:
    """Human label for the reference model id (claude-opus-5 -> Claude Opus 5)."""
    words = reference_model.replace("-", " ").split()
    return " ".join(word.capitalize() for word in words)


def _served_model_label(served_model: str) -> str:
    """Basename and humanize a routed model id for the compact header."""
    model = served_model.rsplit("/", 1)[-1]
    label = _reference_label(model)
    if label.startswith("Gpt "):
        return f"GPT-{label.removeprefix('Gpt ')}"
    return label


_BAR_WIDTH = 24
_BAR_FULL = "█"
_BAR_EMPTY = "░"


def _cost_line(
    served_model: str,
    actual: Decimal,
    reference: Decimal,
    ref_label: str,
    *,
    bar_width: int = _BAR_WIDTH,
) -> str:
    peak = max(actual, reference)
    actual_fraction = float(actual / peak) if peak > 0 else 0.0
    reference_fraction = float(reference / peak) if peak > 0 else 0.0
    actual_bar = (_BAR_FULL * round(actual_fraction * bar_width)).ljust(bar_width, _BAR_EMPTY)
    reference_bar = (_BAR_FULL * round(reference_fraction * bar_width)).ljust(bar_width, _BAR_EMPTY)

    model = _served_model_label(served_model) if served_model else ""
    header = f"Routed to: {model}" if model else "Router cost"
    if reference > 0:
        pct = (reference - actual) / reference * 100
        sign = "-" if pct >= 0 else "+"
        header = f"{header}  {sign}{abs(int(pct))}% vs {ref_label}"

    label_width = max(len("Ramp"), len(ref_label))
    ramp_row = f"{'Ramp'.ljust(label_width)} {actual_bar} ${actual:.2f}"
    reference_row = f"{ref_label.ljust(label_width)} {reference_bar} ${reference:.2f}"
    return f"{header}\n{ramp_row}\n{reference_row}"


def main() -> None:
    try:
        event = json.load(sys.stdin)
    except json.JSONDecodeError:
        return
    session_id = event.get("session_id") if isinstance(event, dict) else None
    api_key = _api_key()
    base_url = _base_url()
    if not session_id or not api_key or not base_url:
        return

    payload = _fetch_session_usage(base_url, api_key, session_id)
    session = payload.get("session") if payload else None
    if not isinstance(session, dict):
        return
    try:
        actual = Decimal(str(session["spend_usd"]))
        reference = Decimal(str(session["reference_cost_usd"]))
        ref_label = _reference_label(str(session["reference_model"]))
    except (KeyError, TypeError, ValueError, ArithmeticError) as _error:
        # ArithmeticError covers decimal.InvalidOperation without importing it.
        return

    served_model = session.get("last_model")
    message = _cost_line(
        served_model if isinstance(served_model, str) else "",
        actual,
        reference,
        ref_label,
    )
    print(json.dumps({"systemMessage": message}), end="")


if __name__ == "__main__":
    try:
        main()
    except Exception:  # a broken hook must never disturb the turn
        pass
'''
