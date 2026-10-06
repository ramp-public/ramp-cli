"""Presentation-independent Router operations for the full-screen interface.

Account operations reuse the existing client and profile transaction logic.
Harness operations run the *same installed CLI* in a noninteractive child, so
its locks, preflight checks, rollback, and legacy integrations remain authoritative.
No Click prompts or process-wide stdout redirection run on the UI thread.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime, time, timedelta, timezone, tzinfo
from decimal import Decimal, InvalidOperation
from threading import Event, Lock
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import click
import httpx

from ramp_cli.client.router_user import (
    RouterUserClient,
    remember_origin,
    selected_origin,
)
from ramp_cli.commands import claude_code, router
from ramp_cli.commands.router_user import (
    _all_keys,
    _key_details,
    _owned_key,
    _routing_profiles,
    _validate_key_name,
    create_named_key,
    key_mutation_request,
)
from ramp_cli.commands.router_user_strategies import (
    ProfileDraft,
    _default_profile,
    _profiles,
    _validate_name,
    delete_profile,
    save_draft,
)
from ramp_cli.errors import EXIT_AUTH_REQUIRED, ApiError, RampCLIError
from ramp_cli.router_ui import harness
from ramp_cli.router_ui.cache import CACHE_TTL, ReadCache, cached, mutation

USAGE_WINDOWS = ((1, "Today"), (3, "3 days"), (7, "7 days"), (30, "1 month"))
USAGE_DAYS = tuple(days for days, _label in USAGE_WINDOWS)
USAGE_GROUPS = ("model", "key")


def local_timezone() -> tzinfo:
    """The IANA zone the server buckets days in; UTC when it can't be named.

    The UTC fallback needs no timezone database, which Windows doesn't ship.
    """
    candidates = [os.environ.get("TZ", "").removeprefix(":")]
    try:
        target = os.path.realpath("/etc/localtime")
    except OSError:
        target = ""
    if "zoneinfo/" in target:
        candidates.append(target.split("zoneinfo/", 1)[1])
    for name in candidates:
        if name:
            try:
                return ZoneInfo(name)
            except (ValueError, ZoneInfoNotFoundError):
                continue
    return timezone.utc


def zone_name(zone: tzinfo) -> str:
    """The IANA name Router buckets days by; the UTC fallback has no key."""
    return getattr(zone, "key", None) or "UTC"


def local_days_window(days: int, zone: tzinfo) -> tuple[datetime, datetime, list]:
    """The last ``days`` calendar days ending today, as UTC bounds and local dates."""
    today = datetime.now(zone).date()
    dates = [today - timedelta(days=offset) for offset in range(days - 1, -1, -1)]
    start = datetime.combine(dates[0], time.min, zone)
    end = datetime.combine(today + timedelta(days=1), time.min, zone)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc), dates


def _decimal(value: object) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(0)
    return amount if amount.is_finite() else Decimal(0)


def _count(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def daily_series(usage: dict, dates: list, zone: tzinfo) -> dict:
    """Zero-filled day × group cells; groups by spend, the server's "other" last."""
    known = set(dates)
    cells: dict[tuple, dict] = {}
    groups: dict[str, dict] = {}
    for point in usage.get("series") or []:
        if not isinstance(point, dict):
            continue
        try:
            started = datetime.fromisoformat(str(point["period_start_at"]))
        except (KeyError, ValueError):
            continue
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        day = started.astimezone(zone).date()
        if day not in known:
            continue
        group_id = safe_text(point.get("group") or point.get("model") or "")
        group = groups.setdefault(
            group_id,
            {
                "id": group_id,
                "label": safe_text(
                    point.get("group_label")
                    or point.get("model_display_name")
                    or group_id
                    or "Unknown"
                ),
                "spend_usd": Decimal(0),
                "request_count": 0,
                "total_tokens": 0,
            },
        )
        cell = cells.setdefault(
            (day, group_id),
            {"spend_usd": Decimal(0), "request_count": 0, "total_tokens": 0},
        )
        for target in (group, cell):
            target["spend_usd"] += _decimal(point.get("spend_usd"))
            target["request_count"] += _count(point.get("request_count"))
            target["total_tokens"] += _count(point.get("total_tokens"))
    ordered = sorted(
        groups.values(),
        key=lambda g: (
            g["id"].startswith("__other") or g["id"] == "Other models",
            -g["spend_usd"],
            g["label"],
        ),
    )
    # Router names its catch-all bucket "Other models" (or keys); "Other" is enough.
    for group in ordered:
        if group["id"].startswith("__other") or group["id"] == "Other models":
            group["label"] = "Other"
    # Keys often share a name ("Ramp CLI"); a short id keeps the legend unambiguous.
    labels = [group["label"] for group in ordered]
    for group in ordered:
        if labels.count(group["label"]) > 1 and not group["id"].startswith("__"):
            group["label"] = f"{group['label']} · {group['id'][:8]}"
    empty = {"spend_usd": Decimal(0), "request_count": 0, "total_tokens": 0}
    rows = []
    for day in dates:
        by_group = {g["id"]: dict(cells.get((day, g["id"]), empty)) for g in ordered}
        rows.append(
            {
                "date": day.isoformat(),
                "spend_usd": sum(
                    (c["spend_usd"] for c in by_group.values()), Decimal(0)
                ),
                "request_count": sum(c["request_count"] for c in by_group.values()),
                "total_tokens": sum(c["total_tokens"] for c in by_group.values()),
                "groups": by_group,
            }
        )
    return {"groups": ordered, "series": rows}


def history_days(usage: dict) -> int:
    """The longest default window the usage history fills: 7 days, 3, or today."""
    active = [
        row["date"]
        for row in usage.get("series") or []
        if row["spend_usd"] > 0 or row.get("request_count")
    ]
    if not active:
        return 1
    dates = [row["date"] for row in usage["series"]]
    # Days from the oldest active day through today, inclusive.
    span = len(dates) - dates.index(min(active))
    return next(days for days in (7, 3, 1) if span >= days)


def model_stats(usage: dict) -> list[dict]:
    """Every model in the window by spend; unlike the series, never folded into Other."""
    models = [
        {
            "id": safe_text(item.get("model") or ""),
            "label": safe_text(item.get("model_display_name") or item.get("model")),
            "spend_usd": _decimal(item.get("spend_usd")),
            "request_count": _count(item.get("request_count")),
            "total_tokens": _count(item.get("total_tokens")),
            "last_used_at": item.get("last_used_at"),
        }
        for item in usage.get("model_stats") or []
        if isinstance(item, dict)
    ]
    return sorted(models, key=lambda model: (-model["spend_usd"], model["label"]))


def safe_text(value: object) -> str:
    """API values are terminal data, never markup or terminal control sequences."""
    return " ".join("".join(char for char in str(value) if char.isprintable()).split())


def safe_message(value: object) -> str:
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", str(value))
    return "\n".join(
        "".join(c for c in line if c.isprintable()) for line in text.splitlines()
    )


class KeyHandoffFailed(RampCLIError):
    """A new key was created but no clipboard took it; ``key`` holds its secret."""

    def __init__(self, key: dict):
        super().__init__(
            "Created the key, but couldn't copy it to the clipboard. "
            "Copy or reveal it here, then paste it into Cursor."
        )
        self.key = key


def _validated_deployment(base_url: str | None, ui_url: str | None) -> dict:
    """Deployment overrides as the CLI validates them, by environment variable."""
    variables = {}
    context = click.Context(click.Command("connect"))
    for flag, value, variable in (
        ("--base-url", base_url, router.ROUTER_BASE_URL_ENV),
        ("--ui-url", ui_url, router.ROUTER_UI_URL_ENV),
    ):
        if value:
            variables[variable] = router._validate_deployment_url(
                context, click.Option([flag]), value
            )
    return variables


def _key_label(secret: str | None, keys: list[dict]) -> str:
    # Key records only expose their last four characters, which is enough to
    # name the key a harness holds without the secret leaving this machine.
    if not secret:
        return "Unavailable"
    if not keys:
        return "Unavailable"
    matches = [
        key
        for key in keys
        if key.get("last_four") and secret.endswith(str(key["last_four"]))
    ]
    if not matches:
        return "Not in your keys"
    if len(matches) > 1:
        return f"Key ending {safe_text(matches[0]['last_four'])}"
    return safe_text(matches[0].get("name") or "Unnamed key")


def _routing_strategy_label(settings: dict) -> str:
    profile = settings.get("routing_profile")
    if isinstance(profile, dict) and profile.get("name"):
        return safe_text(profile["name"])
    enabled = [
        label
        for label, field in (
            ("Flex", "allow_flex_tier_default"),
            ("Switchyard", "switchyard_routing_enabled"),
        )
        if settings.get(field) is True
    ]
    return " + ".join(enabled) if enabled else "Standard"


def public_data(value):
    if isinstance(value, dict):
        return {
            key: public_data(item)
            for key, item in value.items()
            if key not in {"secret", "access_token", "refresh_token", "api_key"}
        }
    if isinstance(value, list):
        return [public_data(item) for item in value]
    return value


def _signed_out(error: Exception) -> bool:
    """Whether Router refused the sign-in itself, not just this request."""
    if isinstance(error, ApiError):
        return error.status_code == 401
    return isinstance(error, RampCLIError) and error.code == EXIT_AUTH_REQUIRED


class RouterService:
    def __init__(
        self,
        state: dict,
        *,
        origin: str | None = None,
        cache_ttl: float = CACHE_TTL,
    ):
        self.cache = ReadCache(cache_ttl)
        self._login_lock = Lock()
        self.state = {key: state[key] for key in ("env", "profile") if state.get(key)}
        self.client = RouterUserClient(
            selected_origin(origin, profile=state.get("profile")),
            profile=state.get("profile"),
            no_input=True,
            interactive=False,
        )

    @property
    def client(self) -> RouterUserClient:
        return self._client

    @client.setter
    def client(self, client: RouterUserClient):
        previous = getattr(self, "_client", None)
        self._client = client
        # A replaced client's connections are no longer needed; anything still
        # using them was reading for the old Router and is discarded anyway.
        if previous is not None and previous is not client:
            close = getattr(previous, "close", None)
            if close is not None:
                close()
        # Key checks, model lists, and setup in this process follow the
        # Router the TUI is on, not the production default.
        router.use_router_origin(client.origin)

    @property
    def cache_ttl(self) -> float:
        return self.cache.ttl

    def invalidate(self):
        self.cache.invalidate()

    def prefetch_tabs(self):
        """Warm Harnesses, API Keys, and Strategies once Home has its data."""
        self.cache.prefetch(
            self.harnesses, self.keys, self.profiles, self.experiment_settings
        )

    def close(self):
        self.cache.close()
        self.client.close()

    def account(self, days: int | None = None, group_by: str = "model") -> dict:
        """``days=None`` picks the longest window the usage history fills."""
        result = {"session": self.client.status(), "origin": self.client.origin}
        if result["session"].get("authenticated"):
            try:
                result["billing"] = self.billing()
            except (ApiError, RampCLIError, httpx.HTTPError) as error:
                if _signed_out(error):
                    # A saved sign-in Router no longer honors is no sign-in;
                    # show sign-in controls rather than empty stats.
                    result["session"] = {**self.client.status(), "authenticated": False}
                    result["signed_out"] = str(error)
                    return public_data(result)
                # Billing is supplemental, not a reason to hide login controls.
                result["billing_error"] = str(error)
            try:
                if days is None:
                    usage = self.daily_usage(7, group_by)
                    days = history_days(usage)
                    # Summary stats come from the server, so a shorter
                    # window is fetched rather than sliced from the week.
                    if days != 7:
                        usage = self.daily_usage(days, group_by)
                    result["usage"] = usage
                else:
                    result["usage"] = self.daily_usage(days, group_by)
            except (ApiError, RampCLIError, httpx.HTTPError, ValueError):
                result["usage_error"] = "Usage analytics are unavailable."
        return public_data(result)

    @cached
    def billing(self):
        return self.client.get("/client/billing", timeout=10)

    @cached
    def daily_usage(self, days: int = 7, group_by: str = "model") -> dict:
        if days not in USAGE_DAYS:
            raise click.BadParameter(f"Choose {', '.join(map(str, USAGE_DAYS))} days.")
        if group_by not in USAGE_GROUPS:
            raise click.BadParameter("Break usage down by model or key.")
        zone = local_timezone()
        start, end, dates = local_days_window(days, zone)
        usage = self.client.get(
            "/client/usage/dashboard",
            params={
                "start_at": start.isoformat(),
                "end_at": end.isoformat(),
                "group_by": group_by,
                "timezone": zone_name(zone),
                "include_summary_stats": "true",
            },
            timeout=15,
        )
        if not isinstance(usage, dict):
            raise RampCLIError("Usage analytics are unavailable.")
        return {
            "days": days,
            "group_by": group_by,
            "timezone": zone_name(zone),
            "effective_end_at": usage.get("effective_end_at"),
            "summary": usage.get("summary") or {},
            "models": model_stats(usage),
            "byok_spend_usd": _decimal(
                (usage.get("summary") or {}).get("byok_spend_usd")
            ),
            **{
                name: sum(
                    _count(point.get(name))
                    for point in usage.get("series") or []
                    if isinstance(point, dict)
                )
                for name in ("input_tokens", "cached_input_tokens")
            },
            **daily_series(usage, dates, zone),
        }

    @mutation
    def login(
        self,
        origin: str,
        progress: Callable[[str], None],
        cancel: Event,
        *,
        no_browser: bool = False,
        timeout: int = 900,
        reuse: bool = True,
        on_callback: Callable | None = None,
    ):
        """Connect to ``origin``, signing in only where it has to.

        Cloudflare Access comes first when the Router sits behind it. With
        ``reuse``, a saved sign-in Router still honors is kept (each Router
        keeps its own); otherwise the browser sign-in runs. Nothing changes
        unless it works.
        """
        # One sign-in at a time: a second would race the first for the
        # callback and the saved login.
        if not self._login_lock.acquire(blocking=False):
            raise RampCLIError("A Router sign-in is already in progress.")
        try:
            return self._login(
                origin,
                progress,
                cancel,
                no_browser=no_browser,
                timeout=timeout,
                reuse=reuse,
                on_callback=on_callback,
            )
        finally:
            self._login_lock.release()

    def _login(
        self,
        origin: str,
        progress: Callable[[str], None],
        cancel: Event,
        *,
        no_browser: bool,
        timeout: int,
        reuse: bool,
        on_callback: Callable | None,
    ):
        # no_input: only this explicit, cancellable sign-in may open a browser.
        # Later reads that find the session gone report it instead of starting
        # a sign-in outside the app's cancellation flow.
        candidate = RouterUserClient(
            origin, profile=self.state.get("profile"), no_input=True, interactive=False
        )
        try:
            candidate.ensure_edge_access(
                progress=progress, cancel=cancel, timeout=timeout
            )
            if reuse and self._live(candidate):
                remember_origin(candidate.origin, profile=self.state.get("profile"))
                result = candidate.status()
            else:
                result = candidate.login(
                    progress=progress,
                    cancel=cancel,
                    no_browser=no_browser,
                    timeout=timeout,
                    on_callback=on_callback,
                )
        except BaseException:
            candidate.close()
            raise
        self.client = candidate
        return public_data(result)

    def identity(self) -> tuple:
        """The Router and account that reads answer for; local, no request."""
        status = self.client.status()
        user = status.get("user") if isinstance(status.get("user"), dict) else {}
        return (self.client.origin, bool(status.get("authenticated")), user.get("id"))

    @staticmethod
    def _live(client: RouterUserClient) -> bool:
        if not client.status()["authenticated"]:
            return False
        try:
            # Ask Router itself: a saved sign-in may be revoked or expired.
            client.access_token(check_session=True, recover=False, timeout=10)
        except httpx.HTTPError as error:
            raise RampCLIError(
                f"Can't reach Router at {client.origin}. Check the URL and your connection."
            ) from error
        except RampCLIError as error:
            if _signed_out(error):
                return False
            raise
        return True

    @mutation
    def logout(self):
        self.client.logout()

    @cached
    def keys(self) -> list[dict]:
        keys = _all_keys(self.client)
        try:
            profiles = _routing_profiles(self.client)
        except (ApiError, RampCLIError, httpx.HTTPError):
            profiles = None
            fallback = "Unavailable"
        else:
            fallback = "Account Defaults"
        assignments = {
            str(key_id): profile.get("name") or "Unnamed Profile"
            for profile in profiles or []
            for key_id in profile.get("assigned_api_key_ids") or []
        }
        administered = {
            business
            for business in {key.get("core_business_uuid") for key in keys} - {None}
            if self.administers(str(business))
        }
        return public_data(
            [
                {
                    **key,
                    "routing_profile_name": assignments.get(str(key["id"]), fallback),
                    # An organization's keys take their caps from its admins.
                    "spend_cap_editable": key.get("core_business_uuid") is None
                    or key.get("core_business_uuid") in administered,
                }
                for key in keys
            ]
        )

    def administers(self, business: str) -> bool:
        """Whether this sign-in is a workspace admin of the business.

        Router answers 404 to everyone else, and any failure counts as no.
        """
        try:
            self.client.get(f"/admin/workspaces/{business}")
        except (ApiError, RampCLIError, httpx.HTTPError):
            return False
        return True

    @cached
    def key_details(self, key_id: str, days: int = 7) -> dict:
        return public_data(
            _key_details(self.client, _owned_key(self.client, key_id), days)
        )

    @cached
    def key_profiles(self) -> list[dict]:
        return _routing_profiles(self.client) or []

    @mutation
    def create_key(self, name: str, profile_id: str | None, request_id: str) -> dict:
        valid = _validate_key_name(name)
        if valid is not True:
            raise click.BadParameter(str(valid), param_hint="name")
        profiles = self.key_profiles()
        profile = None
        if profile_id:
            profile = next((p for p in profiles if str(p["id"]) == profile_id), None)
            if profile is None:
                raise click.BadParameter("The selected strategy no longer exists.")
        elif profiles:
            profile = next((p for p in profiles if p.get("is_default")), None)
        return create_named_key(self.client, name, profile, request_id)

    @mutation
    def mutate_key(self, key_id: str, action: str, value: str = ""):
        key = _owned_key(self.client, key_id)
        method, path, body = key_mutation_request(
            self.client,
            key,
            action,
            name=value if action == "rename" else None,
            routing_profile=value if action == "set-strategy" else None,
        )
        return (self.client.patch if method == "PATCH" else self.client.post)(
            path, json=body
        )

    @mutation
    def delete_key(self, key_id: str):
        """Revoke a key for good; apps using it stop working immediately."""
        key = _owned_key(self.client, key_id)
        return self.client.delete(f"/client/api-keys/{key['id']}")

    @mutation
    def set_key_spend_cap(self, key_id: str, amount: str | None):
        """Set (or, with None, remove) a key's cap, keeping its frequency."""
        key = _owned_key(self.client, key_id)
        business = key.get("core_business_uuid")
        if business:
            # Organization keys only take caps through the workspace-admin API,
            # which wants the amount and frequency set or cleared together.
            frequency = key.get("spend_cap_frequency") or "lifetime"
            return self.client.patch(
                f"/admin/workspaces/{business}/api-keys/{key['id']}/spend-cap",
                json={
                    "spend_cap_amount_usd": amount,
                    "spend_cap_frequency": None if amount is None else frequency,
                },
            )
        if "spend_cap_amount_usd" not in key and "lifetime_spend_cap_usd" in key:
            body = {"lifetime_spend_cap_usd": amount}
        else:
            frequency = key.get("spend_cap_frequency") or "lifetime"
            body = {
                "spend_cap_amount_usd": amount,
                "spend_cap_frequency": None if amount is None else frequency,
            }
        return self.client.patch(f"/client/api-keys/{key['id']}", json=body)

    @cached
    def profiles(self) -> list[dict]:
        return _profiles(self.client)

    @cached
    def experiment_settings(self) -> dict:
        return self.client.get("/admin/me/experiment-settings")

    def auto_routing_default(self) -> bool | None:
        """The account's own Jev Auto Routing, which strategies inherit."""
        try:
            settings = self.experiment_settings()
        except Exception:
            return None
        return bool(settings.get("auto_routing_enabled"))

    @mutation
    def save_profile(self, draft: ProfileDraft) -> dict:
        if draft.is_default and draft.name != draft.original_name:
            raise click.UsageError("The default strategy cannot be renamed.")
        valid = _validate_name(draft.name)
        if valid is not True and not draft.is_default:
            raise click.BadParameter(str(valid), param_hint="name")
        profiles = self.profiles()
        default_id = str(_default_profile(profiles)["id"])
        owned = {str(key["id"]) for key in self.keys()}
        if draft.keys - owned:
            raise click.BadParameter(
                "Some selected keys are no longer in your account."
            )
        return save_draft(self.client, draft, default_id)

    @mutation
    def delete_profile(self, profile_id: str):
        profiles = self.profiles()
        target = next((p for p in profiles if str(p["id"]) == profile_id), None)
        if target is None or target.get("is_default"):
            raise click.BadParameter("The default strategy cannot be deleted.")
        delete_profile(
            self.client,
            profile_id,
            str(_default_profile(profiles)["id"]),
            len(target.get("assigned_api_key_ids") or []),
        )

    @cached
    def harnesses(self) -> list[dict]:
        detected = set(router._detected_clients())
        configured = set(router.configured_router_clients())
        # Desktop's own files decide its status, not just the setup receipt:
        # 3P mode with no usable profile silently falls back to first-party.
        desktop = router.claude_cowork.desktop_status()
        if desktop in ("connected", "inactive"):
            configured.add("cowork")
        statuses = {
            "cowork": {
                "inactive": "Not Active",
                "stranded": "Needs Repair",
            }.get(desktop)
        }
        # Cursor's settings live in its own UI; its base-URL override is the
        # only local trace of a Router connection.
        if router.cursor.is_installed():
            detected.add("cursor")
        if router._cursor_routes_to_router():
            configured.add("cursor")
        strategy_cache: dict[str, str] = {}
        account_keys: list[dict] | None = None
        entries = []
        # Connected harnesses first, then ones found on this machine, then
        # the rest of what Router supports, which can still be preconfigured.
        # The two Claude apps sort as one unit, Desktop directly above the CLI,
        # since one Claude setup covers both.
        labels = {**router.AGENT_NAMES, "claude-code": "Claude CLI", "cursor": "Cursor"}
        claude = ("cowork", "claude-code")
        order = list(labels)

        def rank(name: str) -> tuple:
            members = claude if name in claude else (name,)
            group = min(
                (member not in configured, member not in detected) for member in members
            )
            anchor = order.index("claude-code") if name in claude else order.index(name)
            return (*group, anchor, members.index(name))

        for name in sorted(labels, key=rank):
            label = labels[name]
            entry = {
                "id": name,
                "name": label,
                "detected": name in detected or name in configured,
                "configured": name in configured,
            }
            if name not in configured:
                entry.update(default_model="—", routing_strategy="—", api_key="—")
                entries.append(entry)
                continue
            if name == "cursor":
                # Cursor picks models in its chat picker, and the strategy
                # follows whichever key was pasted there, which stays unread.
                entry.update(
                    default_model="Choose in Cursor",
                    routing_strategy="Set by API key",
                    api_key="Set in Cursor",
                )
                entries.append(entry)
                continue

            try:
                with harness.connection(name) as current:
                    key, base_url, token = (
                        current.key,
                        current.base_url,
                        current.token,
                    )
                    if name in {"conductor", "cowork"}:
                        default_model = "Choose in app"
                    elif current.path is None:
                        default_model = "Unavailable"
                    else:
                        try:
                            model = router._configured_model(name, current.path)
                        except (
                            click.ClickException,
                            OSError,
                            UnicodeError,
                            ValueError,
                        ):
                            default_model = "Unavailable"
                        else:
                            default_model = safe_text(model) if model else "Not set"
            except (click.ClickException, OSError, UnicodeError, ValueError):
                entry.update(
                    default_model="Unavailable",
                    routing_strategy="Unavailable",
                    api_key="Unavailable",
                )
                entries.append(entry)
                continue

            strategy = strategy_cache.get(token)
            if strategy is None:
                try:
                    settings = router._strategy_settings_request(key, base_url=base_url)
                except (
                    click.ClickException,
                    httpx.HTTPError,
                    OSError,
                    ValueError,
                ):
                    strategy = "Unavailable"
                else:
                    strategy = _routing_strategy_label(settings)
                strategy_cache[token] = strategy
            if account_keys is None:
                try:
                    account_keys = self.keys()
                except (ApiError, RampCLIError, httpx.HTTPError):
                    account_keys = []
            entry.update(
                default_model=default_model,
                routing_strategy=strategy,
                api_key=_key_label(key, account_keys),
            )
            entries.append(entry)
        # Claude Code's connection covers its CLI; Desktop's Code tab follows
        # Claude Desktop, so a Desktop that isn't connected is called out.
        if (
            "claude-code" in configured
            and "cowork" in detected
            and desktop != "connected"
        ):
            statuses["claude-code"] = "CLI Only"
        for entry in entries:
            if statuses.get(entry["id"]):
                entry["status"] = statuses[entry["id"]]
            # Claude Code's subagent tiers show as rows under it, read locally.
            if entry["id"] == "claude-code" and entry["configured"]:
                entry["subagents"] = self.subagent_tiers()
        return entries

    def harness_editor(self, client: str) -> dict:
        return harness.editor(client)

    @mutation
    def save_harness_model(self, client: str, model: str, expected: str) -> str:
        with harness.connection(client) as current:
            key = current.key
        return self.command(
            [
                "configure",
                "model",
                client,
                "--model",
                model,
                "--expected-connection",
                expected,
            ],
            secret=key,
        )

    def harness_routing(self, client: str) -> dict:
        return harness.routing_settings(client)

    @mutation
    def save_harness_routing(self, client: str, changes: dict, expected: str) -> dict:
        return harness.save_routing(client, changes, expected)

    def harness_profiles(self, client: str) -> dict:
        return harness.routing_profiles(client)

    @mutation
    def select_harness_profile(self, client: str, name: str, expected: str) -> dict:
        return harness.select_routing_profile(client, name, expected)

    @mutation
    def connect_with_new_key(
        self, clients: list[str], name: str, request_id: str, **options
    ) -> str:
        """Create a key on the account's default strategy, then connect with it.

        Raises KeyHandoffFailed, carrying the new key, when Cursor's clipboard
        handoff fails after creation, so the app can offer it explicitly.
        """
        # Before any key exists or any request goes to them: a new bearer key
        # must never reach an endpoint the CLI would refuse.
        _validated_deployment(options.get("base_url"), options.get("ui_url"))
        base_url, _ = self._deployment(options.get("base_url"))
        cursor = "cursor" in clients
        if cursor and len(clients) != 1:
            # connect() refuses this mix; refuse it before a key exists.
            raise click.UsageError("Set up Cursor separately, using an API key.")
        if cursor and not router._clipboard_available():
            raise click.UsageError(
                "Cursor setup hands a new key over through the clipboard, and no "
                "clipboard tool was found. Paste an existing key instead, or run "
                "'ramp router cursor --show-key' in a terminal."
            )
        key = self.create_key(name, None, request_id)
        secret = str(key.get("secret") or "")
        if not secret:
            raise click.ClickException(
                "Router created the key but returned no secret. Create one in API keys and connect with it."
            )
        if cursor and not router._copy_to_clipboard(secret):
            # Nowhere else holds a Cursor key, so it must not end up unseen.
            raise KeyHandoffFailed(key)
        # A fresh key takes a few seconds to be accepted, and setup checks a
        # handed-in key only once, so wait here until the gateway takes it.
        router._fetch_models(
            secret,
            base_url=base_url,
            wait_for_key=True,
        )
        return self.connect(clients, secret=secret, **options)

    def claude_model_view(self) -> str:
        """Claude Code's current model list, or the recommended one if unset."""
        path = router._client_config_path("claude-code")
        if not claude_code.state_path(path).is_file():
            return "compact"
        try:
            return claude_code.model_view(claude_code.read_settings(path), path)
        except (OSError, ValueError, click.ClickException):
            return "compact"

    def stored_keys(self) -> list[tuple[str, tuple[str, ...]]]:
        # Secrets stay in memory and never become selection labels.
        return router._stored_router_api_key_choices()

    def connect_keys(self) -> list[tuple[str, str]]:
        """(label, secret) for each enabled account key held on this machine.

        Router stores only a hash, so a key no harness here holds can't be
        switched to and isn't offered.
        """
        held = [secret for secret, _clients in self.stored_keys()]
        try:
            keys = [key for key in _all_keys(self.client) if key.get("enabled")]
        except (ApiError, RampCLIError, httpx.HTTPError):
            # Without the account's list, name the held keys by their ending.
            return [(f"Key ending {secret[-4:]}", secret) for secret in held]
        endings = [str(key.get("last_four") or "") for key in keys]
        choices = []
        for key, ending in zip(keys, endings, strict=True):
            matches = [secret for secret in held if ending and secret.endswith(ending)]
            # Two keys sharing an ending can't tell which one a secret is.
            if len(matches) != 1 or endings.count(ending) != 1:
                continue
            name = safe_text(key.get("name") or "Unnamed key")
            choices.append((f"{name} · ending {safe_text(ending)}", matches[0]))
        return choices

    def cursor_connected(self) -> bool:
        # Cursor keeps its key in its own account, so only the route shows.
        return router._cursor_routes_to_router()

    @mutation
    def command(
        self,
        arguments: list[str],
        *,
        secret: str | None = None,
        variables: dict[str, str] | None = None,
    ) -> str:
        environment = dict(os.environ)
        environment["RAMP_NO_TUI"] = "1"
        environment["NO_COLOR"] = "1"
        environment.pop(router.CONFIGURE_KEY_ENV, None)
        if variables:
            environment.update(variables)
        if secret:
            environment[router.CONFIGURE_KEY_ENV] = secret
        if getattr(sys, "frozen", False) or "__compiled__" in globals():
            executable = [sys.executable]
        else:
            executable = [
                sys.executable,
                "-c",
                "from ramp_cli.main import main; main()",
            ]
        flags = ["--human", "--no-input"]
        if self.state.get("env"):
            flags += ["--env", self.state["env"]]
        if self.state.get("profile"):
            flags += ["--profile", self.state["profile"]]
        result = subprocess.run(
            [*executable, *flags, "router", *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=environment,
            check=False,
        )
        output = result.stdout or "Completed."
        if secret:
            output = output.replace(secret, "[hidden]")
        # Keep newlines, but never replay ANSI/OSC sequences from a child.
        output = safe_message(output)
        if result.returncode:
            raise RampCLIError(output, code=result.returncode)
        return output

    def _deployment(self, base_url: str | None = None) -> tuple[str, str]:
        """The data plane and dashboard of the Router this TUI is signed in to.

        Keys belong to one deployment, so a key used here is configured and
        validated there, and setup is always told so explicitly. A gateway
        override (``base_url`` or the environment) that names another
        deployment is refused before any key is created or sent to it.
        """
        origin = self.client.origin.rstrip("/")
        expected = router.router_base_url_for(origin)
        overrides = [("--base-url", base_url)] + [
            (name, os.environ.get(name)) for name in router.BASE_URL_ENVS
        ]
        for name, value in overrides:
            value = (value or "").strip().rstrip("/")
            if value and value != expected:
                raise click.UsageError(
                    f"{name} points at {value}, but this workspace is signed in to "
                    f"{origin}, whose keys work at {expected}. Unset {name} or "
                    "sign in to the matching Router."
                )
        return expected, origin

    def connect(
        self,
        clients: list[str],
        *,
        secret: str = "",
        setup_file: str = "",
        base_url: str = "",
        ui_url: str = "",
        model_view: str = "",
    ) -> str:
        if not clients:
            raise click.UsageError("Select at least one harness.")
        if setup_file and secret:
            raise click.UsageError("Use a setup file or an API key, not both.")
        if not setup_file and not secret.strip():
            raise click.UsageError("Provide an API key or a downloaded setup file.")
        _validated_deployment(base_url, ui_url)
        account_base_url, account_ui_url = self._deployment(base_url)
        base_url, ui_url = base_url or account_base_url, ui_url or account_ui_url
        if "cursor" in clients:
            if len(clients) != 1 or setup_file:
                raise click.UsageError("Set up Cursor separately, using an API key.")
            variables = _validated_deployment(base_url, ui_url)
            return self.command(["cursor"], secret=secret, variables=variables)
        arguments = ["configure", "connect", *clients]
        for flag, value in (
            ("--setup-file", setup_file),
            ("--base-url", base_url),
            ("--ui-url", ui_url),
            ("--claude-models", model_view),
        ):
            if value:
                arguments.extend([flag, value])
        return self.command(arguments, secret=secret)

    @cached
    def subagents_available(self) -> bool:
        """Subagent tiers live in Claude Code's Router config, so need one."""
        path = router._client_config_path("claude-code")
        return claude_code.state_path(path).exists()

    def subagents(self) -> dict:
        path = router._client_config_path("claude-code")
        claude_code.read_state(path)
        key = router._stored_router_api_key("claude-code", path)
        models = router._fetch_models(
            key,
            claude_code_view=True,
            model_view_all=True,
            base_url=router._configured_claude_code_router_url(path),
        )
        environment = claude_code.read_settings(path).get("env") or {}
        # Newest releases first, as in the harness model picker.
        models = sorted(models, key=lambda model: -model.created)
        return {
            "models": [(model.metadata.display_name, model.id) for model in models],
            "prices": harness.prices(models),
            "tiers": {
                tier: environment.get(claude_code.SUBAGENT_TIER_ENV_KEYS[tier])
                for tier in router.SUBAGENT_TIERS
            },
        }

    def subagent_tiers(self) -> dict[str, str | None] | None:
        """Each subagent tier's model override; None when unreadable."""
        if not self.subagents_available():
            return None
        path = router._client_config_path("claude-code")
        try:
            environment = claude_code.read_settings(path).get("env")
        except (click.ClickException, OSError, UnicodeError, ValueError):
            return None
        environment = environment if isinstance(environment, dict) else {}
        return {
            tier: environment.get(claude_code.SUBAGENT_TIER_ENV_KEYS[tier])
            for tier in router.SUBAGENT_TIERS
        }

    @mutation
    def save_subagent(self, tier: str, model: str | None) -> None:
        """Point one subagent tier at a model; None restores Claude Code's."""
        if model is not None:
            self.command(["subagents", f"--{tier}", model])
            return
        # Clearing only removes local keys, so it works with Router unreachable.
        path = router._client_config_path("claude-code")
        claude_code.read_state(path)
        router._write_subagent_tiers(path, {tier: None}, [])

    def legacy_settings_for(self, secret: str) -> dict:
        return router._strategy_settings_request(secret)

    @mutation
    def save_legacy_settings(self, secret: str, changes: dict) -> dict:
        result = router._strategy_settings_request(secret, changes)
        withheld = [
            name
            for name, field in router.STRATEGY_SETTINGS.items()
            if field in changes and result[field] != changes[field]
        ]
        if withheld:
            raise click.ClickException(
                router._withheld_strategy_message(
                    withheld, bool(changes[router.STRATEGY_SETTINGS[withheld[0]]])
                )
            )
        return result
