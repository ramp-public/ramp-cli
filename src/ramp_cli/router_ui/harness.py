"""Shared, noninteractive operations for an existing harness connection."""

import hashlib
import hmac
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path

import click

from ramp_cli.commands import router


@dataclass(frozen=True)
class Connection:
    client: str
    path: Path | None
    key: str = field(repr=False)
    base_url: str

    @property
    def token(self) -> str:
        return hashlib.sha256(f"{self.key}\0{self.base_url}".encode()).hexdigest()


@contextmanager
def connection(client: str):
    if client not in router.AGENT_NAMES:
        raise click.BadParameter("Unknown harness.", param_hint="client")
    if client == "cowork":
        key, endpoint = router.claude_cowork.configured_router_connection()
        base_url = endpoint.rstrip("/")
        if not base_url.endswith("/v1"):
            base_url += "/v1"
        yield Connection(client, None, key, base_url)
        return
    path = router._client_config_path(client)
    transaction = (
        router._hermes_config_lock()
        if client == "hermes"
        else router._conductor_settings_lock(path)
        if client == "conductor"
        else nullcontext()
    )
    with transaction:
        if client not in router.configured_router_clients():
            raise click.UsageError("Connect this harness to Router before editing it.")
        key = router._stored_router_api_key(client, path)
        endpoint = router._stored_router_base_url(client, path)
        if not endpoint:
            raise click.ClickException(
                "The saved Router endpoint is missing. Repair this connection first."
            )
        yield Connection(client, path, key, endpoint)


def connected(client: str) -> Connection:
    """The saved connection of a harness, or a usage error if it isn't connected."""
    try:
        with connection(client) as current:
            return current
    except click.UsageError:
        raise click.UsageError(
            f"{router.AGENT_NAMES[client]} isn't connected to Router. "
            "Connect it with 'ramp router configure' first."
        ) from None


def check_connection(current: Connection, expected: str | None):
    if expected and not hmac.compare_digest(current.token, expected):
        raise click.ClickException(
            "This harness connection changed while you were editing. Reload before saving."
        )


def _models(current: Connection):
    models = router._fetch_models(
        current.key,
        claude_code_view=current.client == "claude-code",
        model_view="all" if current.client == "claude-code" else None,
        base_url=current.base_url,
    )
    choices = [(model.metadata.display_name, model.id) for model in models]
    if current.client == "codex":
        catalog = router._fetch_codex_catalog(current.key, base_url=current.base_url)
        choices = [
            (model.get("display_name") or model["slug"], model["slug"])
            for model in catalog["models"]
        ]
    # Newest releases first; sorted() is stable, so Router's order breaks ties
    # and models without a release date keep their place at the end.
    released = {model.id: getattr(model, "created", 0) for model in models}
    choices = sorted(choices, key=lambda choice: -released.get(choice[1], 0))
    return models, choices


def mask_key(key: str) -> str:
    """A key's first and last four characters; short keys reveal nothing."""
    key = key.strip()
    if len(key) < 16:
        return "…"
    return f"{key[:4]}…{key[-4:]}"


def summaries() -> list[dict]:
    """Every supported harness, from local state only (no network).

    ``installed`` is whether the harness is on this machine, ``connected``
    whether Router set it up here. A connected harness also reports its
    default ``model``, Router ``endpoint``, and a masked ``api_key``.
    """
    installed = set(router._installed_clients())
    if router._codex_app_installed():
        installed.add("codex")
    if router.conductor.is_installed():
        installed.add("conductor")
    connected_clients = set(router.configured_router_clients())
    rows = []
    for client, name in router.CLIENT_NAMES.items():
        row = {
            "harness": client,
            "name": name,
            "installed": client in installed,
            "connected": client in connected_clients,
            "model": None,
            "endpoint": None,
            "api_key": None,
            "model_managed_by_app": client == "conductor",
            "error": None,
        }
        if client in connected_clients:
            try:
                path = router._client_config_path(client)
                row["endpoint"] = router._stored_router_base_url(client, path)
                row["api_key"] = mask_key(router._stored_router_api_key(client, path))
                if client != "conductor":
                    row["model"] = router._configured_model(client, path)
                if client == "codex":
                    _, live = router._read_codex_config(path)
                    if live.get("model_provider") != router.ROUTER_PROVIDER:
                        # A receipt outlived the switch back to another provider.
                        row.update(connected=False, model=None, endpoint=None)
                        row["api_key"] = None
            except (click.ClickException, OSError, UnicodeError, ValueError):
                row.update(model=None, endpoint=None, api_key=None)
                row["error"] = "Can't read this harness's configuration."
        rows.append(row)
    return rows


def catalog(client: str) -> dict:
    """The models a connected harness's key can switch to, newest first."""
    if client == "conductor":
        raise click.UsageError(
            "This harness chooses models inside its app, not in a global CLI setting."
        )
    current = connected(client)
    selected = router._configured_model(client, current.path)
    _, choices = _models(current)
    return {
        "harness": client,
        "default_model": selected,
        "models": [
            {"id": value, "name": label, "is_default": value == selected}
            for label, value in choices
        ],
    }


def prices(models) -> dict[str, tuple[str | None, str | None, str | None]]:
    """Each model's input, output, and cached-input prices, by id."""
    rates = {model.id: getattr(model, "pricing", ()) for model in models}
    return {model: pricing for model, pricing in rates.items() if any(pricing)}


# Harnesses a workspace admin can give a starting model in Router: the ones
# whose Router integration can apply it to each new session (the OpenCode
# and Pi plugins in process, Codex through its setup and session hooks).
ORGANIZATION_DEFAULT_HARNESSES = ("codex", "opencode", "pi")


def organization_default(current: Connection) -> str | None:
    """The model this key's workspace admin chose for the harness, if any.

    Router resolves it against the workspace's model policy and reports
    nothing for personal keys. The harness's Router integration applies it
    to new sessions; here it only labels the picker, so any failure (or a
    Router that doesn't list this harness) reads as none.
    """
    if current.client not in ORGANIZATION_DEFAULT_HARNESSES or not current.key:
        return None
    # A label only, so a slow Router mustn't hold up the picker.
    harnesses = router._coding_agent_settings(
        current.key,
        origin=router._settings_origin(current.client, current.path, current.base_url),
        timeout=2,
    )
    return router.admin_defaults.effective_model(harnesses, current.client)


def editor(client: str) -> dict:
    with connection(client) as current:
        app_managed = client in ("conductor", "cowork")
        models, choices = ([], []) if app_managed else _models(current)
        selected = (
            None if app_managed else router._configured_model(client, current.path)
        )
        suggested = None if app_managed else organization_default(current)
        if suggested is not None and client == "codex":
            suggested = _codex_slug(suggested, models, choices)
        # Router lists the policy's models for the workspace; this key may
        # still not serve one, and then there's nothing to offer.
        if suggested not in {value for _label, value in choices}:
            suggested = None
        return {
            "client": client,
            "model": selected,
            "models": choices,
            "prices": prices(models),
            "organization_default": suggested,
            "connection_token": current.token,
            "model_note": "Choose models inside this app; it has no CLI-managed global default."
            if app_managed
            else "",
        }


def _codex_slug(model: str, models, choices) -> str | None:
    """Codex's catalog names models by slug; accept a request id for one too."""
    slugs = {value for _label, value in choices}
    if model in slugs:
        return model
    name = next(
        (item.metadata.display_name for item in models if item.id == model), None
    )
    if name is None:
        return None
    return next(
        (value for label, value in choices if name in (label, value)),
        None,
    )


def set_model(
    client: str, model: str, *, expected: str | None = None, dry_run: bool = False
) -> dict:
    with connection(client) as current:
        check_connection(current, expected)
        if client in ("conductor", "cowork"):
            raise click.UsageError(
                "This harness chooses models inside its app, not in a global CLI setting."
            )
        models, choices = _models(current)
        if client == "codex":
            # The writer only accepts catalog slugs, so validate the slug the
            # write will use, and a dry run checks exactly what a save would.
            model = _codex_slug(model, models, choices) or ""
        if model not in {value for _, value in choices}:
            raise click.BadParameter(
                "Choose a model currently available to this harness's key.",
                param_hint="--model",
            )
        result = {
            "client": client,
            "default_model": model,
            "config_path": str(current.path),
        }
        if dry_run:
            return {**result, "dry_run": True}
        options = {"base_url": current.base_url, "selected_model": model}
        if client == "codex":
            options["strict_model"] = True
        if client == "opencode":
            # The user asked for this model: a top-level ``model`` of their
            # own, which setup and refresh leave alone, gives way to it.
            options["replace_model"] = True
        if client == "claude-code":
            options.update(
                claude_model_view="all"
                if router._configured_claude_model_view_all(current.path)
                else "compact",
                preserve_model_view_ownership=True,
                # Rechecked under Claude Code's settings lock, right before
                # the write, so a concurrent disconnect or reconnect wins.
                expected_connection=(current.key, current.base_url),
            )
        elif client == "hermes":
            options.update(hermes_require_receipt=True, hermes_lock_held=True)
        if client != "hermes":
            with connection(client) as latest:
                check_connection(latest, current.token)
        usage = router._stored_usage_origin(client, current.path)
        with router._pinned_usage_origin(usage):
            _, saved, _ = router._configure_client(
                client, current.key, models, **options
            )
        if saved != model:
            raise click.ClickException(
                "The model catalog changed during the update. Reload to see the saved default."
            )
        return result


def routing_settings(client: str) -> dict:
    with connection(client) as current:
        return {
            "payload": router._strategy_settings_request(
                current.key, base_url=current.base_url
            ),
            "connection_token": current.token,
        }


def save_routing(client: str, changes: dict, expected: str) -> dict:
    with connection(client) as current:
        check_connection(current, expected)
        result = router._strategy_settings_request(
            current.key, changes, base_url=current.base_url
        )
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
        if "switchyard_config" in changes:
            desired = changes["switchyard_config"]
            switchyard = result.get("switchyard")
            actual = switchyard.get("config") if isinstance(switchyard, dict) else None
            confirmed = (
                isinstance(switchyard, dict)
                and "config" in switchyard
                and (
                    not actual
                    if desired is None
                    else (
                        isinstance(actual, dict)
                        and all(
                            actual.get(name) == value for name, value in desired.items()
                        )
                    )
                )
            )
            if not confirmed:
                raise click.ClickException(
                    "Router did not confirm the requested Switchyard configuration. Reload before retrying."
                )
        return result


def _sharing(client: str, key: str) -> list[str]:
    """Other harnesses saved with the same key, so they switch strategy too."""
    try:
        choices = router._stored_router_api_key_choices()
    except (click.ClickException, OSError, UnicodeError, ValueError):
        return []
    clients = next(
        (names for secret, names in choices if hmac.compare_digest(secret, key)), ()
    )
    return [name for name in clients if name != client]


def routing_profiles(client: str) -> dict:
    with connection(client) as current:
        key, token = current.key, current.token
        profiles = router._key_routing_profiles(key, base_url=current.base_url)
    return {
        "profiles": profiles,
        "shared_with": _sharing(client, key),
        "connection_token": token,
    }


def select_routing_profile(client: str, name: str, expected: str) -> dict:
    with connection(client) as current:
        check_connection(current, expected)
        result = router._select_key_routing_profile(
            current.key, name, base_url=current.base_url
        )
    profile = result.get("routing_profile")
    if (
        not isinstance(profile, dict)
        or str(profile.get("name", "")).casefold() != name.casefold()
    ):
        raise click.ClickException(
            "Router did not confirm the new routing strategy. Reload before retrying."
        )
    return result
