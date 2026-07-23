"""Configure coding agents to use Ramp Router."""

import json
import os
import re
import tempfile
import tomllib
from pathlib import Path

import click
import httpx
import json5

from ramp_cli.output.formatter import print_agent_json, resolve_format

ROUTER_BASE_URL = "https://router-api.ramp.com/v1"
ROUTER_UI_URL = "https://router.ramp.com"
ROUTER_PROVIDER = "ramp-router"
CLIENT_NAMES = {"codex": "Codex", "opencode": "OpenCode", "pi": "Pi"}


def _codex_provider(api_key: str) -> str:
    bearer_token = json.dumps(api_key)
    return f'''[model_providers.ramp-router]
name = "Ramp Router"
base_url = "{ROUTER_BASE_URL}"
wire_api = "responses"
supports_websockets = false
experimental_bearer_token = {bearer_token}
'''


_TABLE_HEADER = re.compile(r"^\s*\[.*]\s*(?:#.*)?$")
_OWNED_CODEX_TABLE = re.compile(
    r"^\s*\[\s*(?:model_providers|\"model_providers\"|'model_providers')\s*\.\s*"
    r"(?:ramp-router|\"ramp-router\"|'ramp-router')\s*(?:\.|])"
)
_OWNED_CODEX_ROOT_KEY = re.compile(
    r"^\s*(?:model|model_provider|model_catalog_json)\s*="
)
_OWNED_ROOT_KEYS = ("model", "model_provider", "model_catalog_json")


@click.group("router", help="Configure coding agents to use Ramp Router")
def router_group() -> None:
    pass


@router_group.command(
    "configure", help="Configure one coding agent, or all agents when omitted"
)
@click.argument(
    "client",
    type=click.Choice(tuple(CLIENT_NAMES), case_sensitive=False),
    required=False,
)
@click.option(
    "--api-key",
    metavar="KEY",
    help="Ramp Router API key. Prompts when omitted.",
)
@click.pass_context
def router_configure(
    ctx: click.Context, client: str | None, api_key: str | None
) -> None:
    if ctx.obj["no_input"] and api_key is None:
        raise click.UsageError(
            "Pass --api-key when using non-interactive mode. "
            f"Create a key at {ROUTER_UI_URL}."
        )

    clients = (client,) if client else tuple(CLIENT_NAMES)
    client_names = ", ".join(CLIENT_NAMES[item] for item in clients)
    fmt = resolve_format(ctx.obj["format"], ctx.obj["config_format"])
    if fmt != "json":
        click.echo(f"Let's connect {client_names} to Ramp Router.")
    if api_key is None:
        click.echo(
            f"Create or copy an API key at {ROUTER_UI_URL}, then paste it below."
        )
        click.echo()
        api_key = click.prompt("API key", hide_input=True)
    api_key = api_key.strip()
    if not api_key:
        raise click.UsageError("The API key cannot be empty.")

    models = _fetch_models(api_key)
    results = []
    failures = []
    for item in clients:
        try:
            path, catalog_path, default_model = _configure_client(item, api_key, models)
        except click.ClickException as exc:
            failures.append(f"{CLIENT_NAMES[item]}: {exc.message}")
            continue
        result = {
            "client": item,
            "config_path": str(path),
            "provider": ROUTER_PROVIDER,
            "default_model": default_model,
            "models_available": len(models),
        }
        if catalog_path is not None:
            result["model_catalog_path"] = str(catalog_path)
        results.append(result)

    if failures and fmt == "json":
        raise click.ClickException("Could not configure " + "; ".join(failures))
    if fmt == "json":
        payload = results[0] if client and results else {"clients": results}
        print_agent_json(payload, pagination=None)
    else:
        for result in results:
            item = result["client"]
            client_name = CLIENT_NAMES[item]
            click.echo(f"Added {len(models)} Ramp Router model(s) to {client_name}.")
            restart = (
                "Restart OpenCode, then open /models"
                if item == "opencode"
                else (
                    "Open /model" if item == "pi" else "Restart Codex, then open /model"
                )
            )
            click.echo(f"{restart} to choose one.")

    if failures:
        raise click.ClickException("Could not configure " + "; ".join(failures))
    restore_client = f" {client}" if client else ""
    if fmt != "json":
        click.echo(
            f"Run 'ramp router unconfigure{restore_client}' to restore the previous "
            "settings."
        )


@router_group.command(
    "unconfigure", help="Restore one coding agent, or all agents when omitted"
)
@click.argument(
    "client",
    type=click.Choice(tuple(CLIENT_NAMES), case_sensitive=False),
    required=False,
)
@click.pass_context
def router_unconfigure(ctx: click.Context, client: str | None) -> None:
    clients = (client,) if client else tuple(CLIENT_NAMES)
    results = []
    failures = []
    for item in clients:
        path = _codex_config_path() if item == "codex" else _json_config_path(item)
        if not client and not (path.parent / "ramp-router-state.json").exists():
            continue
        try:
            _unconfigure_client(item, path)
        except click.ClickException as exc:
            failures.append(f"{CLIENT_NAMES[item]}: {exc.message}")
            continue
        results.append({"client": item, "config_path": str(path), "removed": True})

    if not results and not failures:
        raise click.ClickException("Ramp Router is not configured in any coding agent.")
    fmt = resolve_format(ctx.obj["format"], ctx.obj["config_format"])
    if failures and fmt == "json":
        raise click.ClickException("Could not unconfigure " + "; ".join(failures))
    if fmt == "json":
        payload = results[0] if client and results else {"clients": results}
        print_agent_json(payload, pagination=None)
    else:
        for result in results:
            client_name = CLIENT_NAMES[result["client"]]
            click.echo(
                f"Removed Ramp Router and restored your previous {client_name} "
                "settings."
            )
            if result["client"] in ("codex", "opencode"):
                click.echo(f"Restart {client_name} to apply the change.")
    if failures:
        raise click.ClickException("Could not unconfigure " + "; ".join(failures))


def _unconfigure_client(client: str, path: Path) -> None:
    if client != "codex":
        _unconfigure_json_client(client, path)
        return
    state_path = path.parent / "ramp-router-state.json"
    if not state_path.exists():
        raise click.ClickException("Ramp Router is not configured in Codex.")

    existing, _ = _read_codex_config(path)
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        root_values = state["root"]
        previous_provider = state["provider"]
        if not isinstance(root_values, dict) or not isinstance(previous_provider, list):
            raise ValueError
    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
        raise click.ClickException(
            f"Could not read Ramp Router setup state from {state_path}."
        ) from None

    chunks = _config_chunks(existing)
    chunks[0] = [_render_root_config(chunks[0], root_values)]
    preserved = "".join(
        "".join(chunk)
        for chunk in chunks
        if not chunk or not _OWNED_CODEX_TABLE.match(chunk[0])
    ).rstrip()
    restored_provider = "".join(previous_provider).strip()
    updated = (
        f"{preserved}\n\n{restored_provider}\n"
        if restored_provider
        else f"{preserved}\n"
        if preserved
        else ""
    )

    try:
        if updated:
            tomllib.loads(updated)
        _write_private_file(path, updated)
        (path.parent / "ramp-router-models.json").unlink(missing_ok=True)
        state_path.unlink()
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise click.ClickException(
            f"Could not restore Codex config {path}: {exc}"
        ) from None


def _codex_config_path() -> Path:
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    return codex_home.expanduser() / "config.toml"


def _json_config_path(client: str) -> Path:
    if client == "opencode":
        configured = os.environ.get("OPENCODE_CONFIG")
        if configured:
            return Path(configured).expanduser()
        return Path.home() / ".config" / "opencode" / "opencode.json"
    pi_home = Path(os.environ.get("PI_CODING_AGENT_DIR", Path.home() / ".pi" / "agent"))
    return pi_home.expanduser() / "models.json"


def _configure_client(
    client: str, api_key: str, models: list[str]
) -> tuple[Path, Path | None, str]:
    if client == "codex":
        path = _codex_config_path()
        catalog_path, default_model = _configure_codex(path, api_key, models)
        return path, catalog_path, default_model

    path = _json_config_path(client)
    default_model = "gpt-5.4" if "gpt-5.4" in models else models[0]
    _configure_json_client(client, path, api_key, models, default_model)
    return path, None, default_model


def _configure_json_client(
    client: str,
    path: Path,
    api_key: str,
    models: list[str],
    default_model: str,
) -> None:
    existing = _read_json_config(client, path)
    settings_path = path.parent / "settings.json"
    settings = _read_json_config(client, settings_path) if client == "pi" else None
    state_path = path.parent / "ramp-router-state.json"
    state = None
    if not state_path.exists():
        providers = existing.get("provider" if client == "opencode" else "providers")
        providers = providers if isinstance(providers, dict) else {}
        state_data = {
            "provider_present": ROUTER_PROVIDER in providers,
            "provider": providers.get(ROUTER_PROVIDER),
        }
        if client == "opencode":
            state_data.update(
                {"model_present": "model" in existing, "model": existing.get("model")}
            )
        else:
            state_data.update(
                {
                    "default_provider_present": "defaultProvider" in settings,
                    "default_provider": settings.get("defaultProvider"),
                    "default_model_present": "defaultModel" in settings,
                    "default_model": settings.get("defaultModel"),
                }
            )
        state = json.dumps(state_data, indent=2) + "\n"

    if client == "opencode":
        providers = existing.setdefault("provider", {})
        if not isinstance(providers, dict):
            raise click.ClickException(
                f"Could not update OpenCode config {path}: 'provider' must be an object."
            )
        providers[ROUTER_PROVIDER] = {
            "npm": "@ai-sdk/openai",
            "name": "Ramp Router",
            "options": {"baseURL": ROUTER_BASE_URL, "apiKey": api_key},
            "models": {model: {"name": model} for model in models},
        }
        existing["model"] = f"{ROUTER_PROVIDER}/{default_model}"
    else:
        providers = existing.setdefault("providers", {})
        if not isinstance(providers, dict):
            raise click.ClickException(
                f"Could not update Pi config {path}: 'providers' must be an object."
            )
        providers[ROUTER_PROVIDER] = {
            "baseUrl": ROUTER_BASE_URL,
            "api": "openai-responses",
            "apiKey": api_key,
            "models": [
                {"id": model, "name": model, "input": ["text", "image"]}
                for model in models
            ],
        }
        settings["defaultProvider"] = ROUTER_PROVIDER
        settings["defaultModel"] = default_model

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if state is not None:
            _write_private_file(state_path, state)
        _write_private_file(path, json.dumps(existing, indent=2) + "\n")
        if settings is not None:
            _write_private_file(settings_path, json.dumps(settings, indent=2) + "\n")
    except OSError as exc:
        raise click.ClickException(
            f"Could not write {CLIENT_NAMES[client]} config {path}: {exc}"
        ) from None


def _unconfigure_json_client(client: str, path: Path) -> None:
    state_path = path.parent / "ramp-router-state.json"
    if not state_path.exists():
        raise click.ClickException(
            f"Ramp Router is not configured in {CLIENT_NAMES[client]}."
        )
    existing = _read_json_config(client, path)
    settings_path = path.parent / "settings.json"
    settings = _read_json_config(client, settings_path) if client == "pi" else None
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        provider_present = state["provider_present"]
        if not isinstance(provider_present, bool):
            raise ValueError
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, ValueError):
        raise click.ClickException(
            f"Could not read Ramp Router setup state from {state_path}."
        ) from None

    provider_key = "provider" if client == "opencode" else "providers"
    providers = existing.get(provider_key)
    if not isinstance(providers, dict):
        raise click.ClickException(
            f"Could not restore {CLIENT_NAMES[client]} config {path}: "
            f"'{provider_key}' must be an object."
        )
    if provider_present:
        providers[ROUTER_PROVIDER] = state.get("provider")
    else:
        providers.pop(ROUTER_PROVIDER, None)
        if not providers:
            existing.pop(provider_key)

    if client == "opencode":
        if str(existing.get("model", "")).startswith(f"{ROUTER_PROVIDER}/"):
            _restore_json_value(
                existing,
                "model",
                state.get("model_present"),
                state.get("model"),
            )
    elif settings.get("defaultProvider") == ROUTER_PROVIDER:
        _restore_json_value(
            settings,
            "defaultProvider",
            state.get("default_provider_present"),
            state.get("default_provider"),
        )
        _restore_json_value(
            settings,
            "defaultModel",
            state.get("default_model_present"),
            state.get("default_model"),
        )

    try:
        _write_private_file(path, json.dumps(existing, indent=2) + "\n")
        if settings is not None:
            _write_private_file(settings_path, json.dumps(settings, indent=2) + "\n")
        state_path.unlink()
    except OSError as exc:
        raise click.ClickException(
            f"Could not restore {CLIENT_NAMES[client]} config {path}: {exc}"
        ) from None


def _restore_json_value(data: dict, key: str, present: object, value: object) -> None:
    if present is True:
        data[key] = value
    else:
        data.pop(key, None)


def _read_json_config(client: str, path: Path) -> dict:
    try:
        if not path.exists():
            return {}
        data = json5.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("the top-level value must be an object")
        return data
    except (OSError, UnicodeError, ValueError) as exc:
        raise click.ClickException(
            f"Could not read {CLIENT_NAMES[client]} config {path}: {exc}"
        ) from None


def _fetch_models(api_key: str) -> list[str]:
    try:
        response = httpx.get(
            f"{ROUTER_BASE_URL}/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (401, 403):
            raise click.ClickException(
                "That API key wasn't accepted by Ramp Router. "
                f"Create or copy a key at {ROUTER_UI_URL} and try again."
            ) from None
        raise click.ClickException(
            "Ramp Router couldn't validate the key "
            f"(HTTP {exc.response.status_code}). Please try again."
        ) from None
    except (httpx.HTTPError, ValueError):
        raise click.ClickException(
            "We couldn't reach Ramp Router. Check your connection and try again."
        ) from None

    models = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        raise click.ClickException(
            "Ramp Router returned an unexpected response. Please try again."
        )
    model_ids = list(
        dict.fromkeys(
            model["id"].strip()
            for model in models
            if isinstance(model, dict)
            and isinstance(model.get("id"), str)
            and model["id"].strip()
        )
    )
    if not model_ids:
        raise click.ClickException("No models are available for this Ramp Router key.")
    return model_ids


def _configure_codex(path: Path, api_key: str, models: list[str]) -> tuple[Path, str]:
    existing, existing_data = _read_codex_config(path)
    chunks = _config_chunks(existing)

    state_path = path.parent / "ramp-router-state.json"
    state = None
    if not state_path.exists():
        previous_provider = [
            "".join(chunk)
            for chunk in chunks
            if chunk and _OWNED_CODEX_TABLE.match(chunk[0])
        ]
        state = (
            json.dumps(
                {
                    "root": {
                        key: existing_data[key]
                        for key in _OWNED_ROOT_KEYS
                        if key in existing_data
                    },
                    "provider": previous_provider,
                },
                indent=2,
            )
            + "\n"
        )

    catalog_path = path.parent / "ramp-router-models.json"
    default_model = "gpt-5.4" if "gpt-5.4" in models else models[0]
    chunks[0] = [
        _render_root_config(
            chunks[0],
            {
                "model": default_model,
                "model_provider": "ramp-router",
                "model_catalog_json": str(catalog_path.resolve()),
            },
        )
    ]

    preserved = "".join(
        "".join(chunk)
        for chunk in chunks
        if not chunk or not _OWNED_CODEX_TABLE.match(chunk[0])
    ).rstrip()
    provider = _codex_provider(api_key)
    updated = f"{preserved}\n\n{provider}" if preserved else provider
    catalog = (
        json.dumps(
            {
                "models": [
                    _codex_model(model, priority)
                    for priority, model in enumerate(models)
                ]
            },
            indent=2,
        )
        + "\n"
    )

    try:
        tomllib.loads(updated)
        path.parent.mkdir(parents=True, exist_ok=True)
        if state is not None:
            _write_private_file(state_path, state)
        _write_private_file(catalog_path, catalog)
        _write_private_file(path, updated)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise click.ClickException(
            f"Could not write Codex config {path}: {exc}"
        ) from None
    return catalog_path, default_model


def _read_codex_config(path: Path) -> tuple[str, dict]:
    try:
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        data = tomllib.loads(existing) if existing else {}
        return existing, data
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise click.ClickException(
            f"Could not read Codex config {path}: {exc}"
        ) from None


def _config_chunks(config: str) -> list[list[str]]:
    chunks: list[list[str]] = [[]]
    for line in config.splitlines(keepends=True):
        if _TABLE_HEADER.match(line):
            chunks.append([])
        chunks[-1].append(line)
    return chunks


def _render_root_config(lines: list[str], values: dict) -> str:
    root = "".join(line for line in lines if not _OWNED_CODEX_ROOT_KEY.match(line))
    root = root.rstrip()
    settings = "\n".join(
        f"{key} = {json.dumps(value)}" for key, value in values.items()
    )
    if root and settings:
        return f"{root}\n{settings}\n"
    if settings:
        return f"{settings}\n"
    return f"{root}\n" if root else ""


def _codex_model(model: str, priority: int) -> dict:
    return {
        "slug": model,
        "display_name": model,
        "description": "Available through Ramp Router",
        "supported_reasoning_levels": [],
        "shell_type": "shell_command",
        "visibility": "list",
        "supported_in_api": True,
        "priority": priority,
        "base_instructions": (
            "You are a coding agent. Follow the user's instructions and use the "
            "available tools to complete tasks."
        ),
        "supports_reasoning_summary_parameter": False,
        "supports_reasoning_summaries": False,
        "default_reasoning_summary": "none",
        "support_verbosity": False,
        "truncation_policy": {"mode": "bytes", "limit": 10000},
        "supports_parallel_tool_calls": True,
        "experimental_supported_tools": [],
        "input_modalities": ["text", "image"],
    }


def _write_private_file(path: Path, content: str) -> None:
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
