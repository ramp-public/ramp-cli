"""Cursor's local provider settings for Ramp Router setup.

Cursor keeps its Models / API Keys screen in a SQLite state database. While
Cursor is quit, ``ramp router configure cursor`` writes the OpenAI base-URL
override, turns the OpenAI key on, stores the key encrypted the way Cursor
does, and adds Router's model names. A receipt keeps the settings from before
the first write so ``unconfigure`` can restore them.
"""

import ctypes
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

# Redirects Cursor's user-data directory. Tests set it so detection never
# reads a developer's real Cursor state, and the app bundle is then ignored.
USER_DIR_ENV = "RAMP_CURSOR_USER_DIR"
_APP_BUNDLE = Path("/Applications/Cursor.app")
# The row of Cursor's state database holding its application settings as JSON,
# the OpenAI base-URL override among them.
_SETTINGS_KEY = (
    "src.vs.platform.reactivestorage.browser.reactiveStorageServiceImpl"
    ".persistentStorage.applicationUser"
)
# The encrypted OpenAI key and the secret-storage index that lists it.
_OPENAI_KEY_SECRET = "secret://cursorAuth/openAIKey"
_SECRET_INDEX_KEY = "secret://storage"
_RECEIPT_FILENAME = "cursor-router-state.json"
# Rows where Cursor caches its team's admin settings as JSON. Their
# "byokDisabled" flag means the team blocks personal API keys, and Cursor then
# ignores the OpenAI key and base-URL override.
_ADMIN_SETTINGS_KEYS = ("adminSettings.cached", "autorun.cachedAdminSettings")


class CursorSettingsError(RuntimeError):
    """Cursor's local settings could not be read or written."""


def user_dir() -> Path:
    """Locate Cursor's user-data directory for this platform."""
    configured = os.environ.get(USER_DIR_ENV)
    if configured:
        return Path(configured).expanduser()
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "Cursor" / "User"


def is_installed() -> bool:
    """Report whether Cursor appears to be present on this machine."""
    if user_dir().exists():
        return True
    if USER_DIR_ENV in os.environ:
        return False
    return _APP_BUNDLE.exists() or shutil.which("cursor") is not None


def override_base_url() -> str | None:
    """Return Cursor's OpenAI base-URL override, or None when unset or unreadable.

    The database is opened read-only: Cursor may be running and owns it.
    """
    return read_override_base_url()[1]


def read_override_base_url(user: Path | None = None) -> tuple[bool, str | None]:
    """(whether Cursor's settings were read, the override or None when unset).

    Tells "Cursor has no override" from "Cursor's settings could not be read",
    for example while a running Cursor holds the database's write lock.
    ``user`` is Cursor's user-data directory, this process's by default.
    """
    path = state_db_path(user)
    if not path.is_file():
        return False, None
    try:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=1)
        try:
            row = connection.execute(
                "SELECT value FROM ItemTable WHERE key = ?", (_SETTINGS_KEY,)
            ).fetchone()
        finally:
            connection.close()
        settings = json.loads(row[0]) if row else {}
    except (sqlite3.Error, OSError, UnicodeError, ValueError, TypeError):
        return False, None
    value = settings.get("openAIBaseUrl") if isinstance(settings, dict) else None
    if not isinstance(value, str):
        return True, None
    return True, value.strip().rstrip("/") or None


def personal_keys_blocked(user: Path | None = None) -> bool:
    """Whether Cursor's team blocks personal API keys, as Cursor last cached it.

    Cursor refreshes the cache while it runs, so a change an admin just made
    shows up once Cursor has been open. False when the settings can't be read.
    """
    path = state_db_path(user)
    if not path.is_file():
        return False
    try:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=1)
        try:
            rows = connection.execute(
                "SELECT value FROM ItemTable WHERE key IN (?, ?)", _ADMIN_SETTINGS_KEYS
            ).fetchall()
        finally:
            connection.close()
        settings = [json.loads(value) for (value,) in rows]
    except (sqlite3.Error, OSError, UnicodeError, ValueError, TypeError):
        return False
    return any(
        isinstance(item, dict) and item.get("byokDisabled") is True for item in settings
    )


def state_db_path(user: Path | None = None) -> Path:
    """Return the SQLite file holding Cursor's application settings."""
    return (user or user_dir()) / "globalStorage" / "state.vscdb"


def model_names_to_add(model_names: list[str], aliases: dict[str, str]) -> list[str]:
    """Names Cursor's OpenAI override can serve, in catalog order.

    Cursor sends any ``claude-`` id to its Anthropic key, which cannot point
    at Router, so Claude models are listed by their Router alias instead.
    """
    by_model = {model: alias for alias, model in aliases.items()}
    names: list[str] = []
    for name in model_names:
        name = by_model.get(name, name)
        if name and not name.startswith("claude-") and name not in names:
            names.append(name)
    return names


def is_running() -> bool:
    """Whether Cursor is running; it rewrites its settings from memory on quit."""
    try:
        completed = subprocess.run(
            ["pgrep", "-u", str(os.getuid()), "-x", "Cursor"],
            capture_output=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def configure(
    api_key: str,
    base_url: str,
    model_names: list[str],
    *,
    encrypt=None,
    prompt: bool = True,
) -> dict:
    """Point Cursor's OpenAI override at Router. Cursor must be quit.

    ``prompt=False`` never shows the macOS permission dialog: the write then
    succeeds only if the Keychain item was already allowed.
    """
    if is_running():
        raise CursorSettingsError("Quit Cursor before configuring it.")
    path = state_db_path()
    settings, secret, index = _read(path)
    receipt = _read_receipt() or {
        "models_added": [],
        "enabled_added": [],
        "previous": {
            "openAIBaseUrl": settings.get("openAIBaseUrl"),
            "useOpenAIKey": settings.get("useOpenAIKey"),
            "openAIKeySecret": secret,
        },
    }
    ai_settings = settings.setdefault("aiSettings", {})
    added = ai_settings.setdefault("userAddedModels", [])
    enabled = ai_settings.setdefault("modelOverrideEnabled", [])
    for name in model_names:
        if name not in added:
            added.append(name)
            if name not in receipt["models_added"]:
                receipt["models_added"].append(name)
        if name not in enabled:
            enabled.append(name)
            enabled_added = receipt.setdefault("enabled_added", [])
            if name not in enabled_added:
                enabled_added.append(name)
    settings["openAIBaseUrl"] = base_url.rstrip("/")
    settings["useOpenAIKey"] = True
    index[_OPENAI_KEY_SECRET] = 1
    secret = encrypt(api_key) if encrypt else encrypt_with_cursor(api_key, prompt)
    # Save the originals before changing anything, so a failed write can
    # still be undone and a retry cannot record Router's values as previous.
    _write_receipt(receipt)
    _write(path, settings, secret, index)
    return receipt


def unconfigure() -> bool:
    """Restore the settings from before the first configure.

    Returns False when this CLI never wrote Cursor's settings.
    """
    receipt = _read_receipt()
    if receipt is None:
        return False
    if is_running():
        raise CursorSettingsError("Quit Cursor before disconnecting it.")
    path = state_db_path()
    settings, _, index = _read(path)
    previous = receipt["previous"]
    for key in ("openAIBaseUrl", "useOpenAIKey"):
        if previous.get(key) is None:
            settings.pop(key, None)
        else:
            settings[key] = previous[key]
    added = set(receipt.get("models_added") or [])
    removals = {
        "userAddedModels": added,
        "modelOverrideEnabled": set(receipt.get("enabled_added", added)),
    }
    ai_settings = settings.get("aiSettings") or {}
    for list_key, names in removals.items():
        if isinstance(ai_settings.get(list_key), list):
            ai_settings[list_key] = [m for m in ai_settings[list_key] if m not in names]
    secret = previous.get("openAIKeySecret")
    if secret is None:
        index.pop(_OPENAI_KEY_SECRET, None)
    _write(path, settings, secret, index)
    _receipt_path().unlink(missing_ok=True)
    return True


def encrypt_with_cursor(api_key: str, prompt: bool = True) -> str:
    """Encrypt a key the way Cursor's Electron safeStorage does on macOS.

    Chromium's scheme: the "Cursor Safe Storage" Keychain password, PBKDF2-SHA1
    (salt "saltysalt", 1003 rounds, 16 bytes), AES-128-CBC with a 16-space IV,
    and a "v10" prefix, stored as ``JSON.stringify`` of a Node Buffer.
    """
    if sys.platform != "darwin":
        raise CursorSettingsError("Writing Cursor's settings needs macOS.")
    password = _keychain_password(prompt)
    key = hashlib.pbkdf2_hmac("sha1", password, b"saltysalt", 1003, 16)
    data = list(b"v10" + _aes_128_cbc_encrypt(key, b" " * 16, api_key.encode()))
    return json.dumps({"type": "Buffer", "data": data}, separators=(",", ":"))


def _aes_128_cbc_encrypt(key: bytes, iv: bytes, plaintext: bytes) -> bytes:
    """Encrypt with PKCS7 padding through macOS CommonCrypto, in-process.

    Keeping the key out of a subprocess keeps it out of the process list.
    """
    common_crypto = ctypes.cdll.LoadLibrary("/usr/lib/system/libcommonCrypto.dylib")
    out = ctypes.create_string_buffer(len(plaintext) + 16)
    moved = ctypes.c_size_t()
    status = common_crypto.CCCrypt(
        ctypes.c_uint32(0),  # kCCEncrypt
        ctypes.c_uint32(0),  # kCCAlgorithmAES
        ctypes.c_uint32(1),  # kCCOptionPKCS7Padding
        key,
        ctypes.c_size_t(len(key)),
        iv,
        plaintext,
        ctypes.c_size_t(len(plaintext)),
        out,
        ctypes.c_size_t(len(out)),
        ctypes.byref(moved),
    )
    if status != 0 or not moved.value:
        raise CursorSettingsError("Cursor's key could not be encrypted.")
    return out.raw[: moved.value]


def _keychain_password(prompt: bool) -> bytes:
    """Read the "Cursor Safe Storage" password through the Security framework.

    Reading it in this process makes the macOS dialog name Ramp rather than
    the ``security`` tool. Without ``prompt``, user interaction is disabled so
    an unattended run fails fast instead of opening a dialog nobody answers.
    """
    security = ctypes.cdll.LoadLibrary(
        "/System/Library/Frameworks/Security.framework/Security"
    )
    service = b"Cursor Safe Storage"
    length = ctypes.c_uint32()
    data = ctypes.c_void_p()
    result: dict = {}

    def find() -> None:
        result["status"] = security.SecKeychainFindGenericPassword(
            None,
            ctypes.c_uint32(len(service)),
            service,
            ctypes.c_uint32(0),
            None,
            ctypes.byref(length),
            ctypes.byref(data),
            None,
        )

    if not prompt:
        security.SecKeychainSetUserInteractionAllowed(ctypes.c_bool(False))
    try:
        # The call blocks until the dialog is answered; a dialog left open
        # behind other windows falls back after two minutes.
        reader = threading.Thread(target=find, daemon=True)
        reader.start()
        reader.join(timeout=120)
    finally:
        if not prompt:
            security.SecKeychainSetUserInteractionAllowed(ctypes.c_bool(True))
    if result.get("status") != 0 or not data.value:
        raise CursorSettingsError(
            "Cursor's encryption key was not available from the keychain."
        )
    try:
        return ctypes.string_at(data.value, length.value)
    finally:
        security.SecKeychainItemFreeContent(None, data)


def _read(path: Path) -> tuple[dict, str | None, dict]:
    """Return Cursor's settings, the encrypted OpenAI key, and the secret index."""
    try:
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)
        try:
            rows = dict(
                connection.execute(
                    "SELECT key, value FROM ItemTable WHERE key IN (?, ?, ?)",
                    (_SETTINGS_KEY, _OPENAI_KEY_SECRET, _SECRET_INDEX_KEY),
                )
            )
        finally:
            connection.close()
        settings = json.loads(rows.get(_SETTINGS_KEY) or "{}")
        index = json.loads(rows.get(_SECRET_INDEX_KEY) or "{}")
    except (sqlite3.Error, OSError, UnicodeError, ValueError) as exc:
        raise CursorSettingsError("Cursor's settings could not be read.") from exc
    if not isinstance(settings, dict) or not isinstance(index, dict):
        raise CursorSettingsError("Cursor's settings could not be read.")
    return settings, rows.get(_OPENAI_KEY_SECRET), index


def _write(path: Path, settings: dict, secret: str | None, index: dict) -> None:
    upsert = (
        "INSERT INTO ItemTable (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
    )
    try:
        with sqlite3.connect(path, timeout=5) as connection:
            connection.execute(upsert, (_SETTINGS_KEY, json.dumps(settings)))
            connection.execute(upsert, (_SECRET_INDEX_KEY, json.dumps(index)))
            if secret is None:
                connection.execute(
                    "DELETE FROM ItemTable WHERE key = ?", (_OPENAI_KEY_SECRET,)
                )
            else:
                connection.execute(upsert, (_OPENAI_KEY_SECRET, secret))
    except sqlite3.Error as exc:
        raise CursorSettingsError("Cursor's settings could not be written.") from exc


def _receipt_path() -> Path:
    return user_dir() / _RECEIPT_FILENAME


def _read_receipt() -> dict | None:
    try:
        receipt = json.loads(_receipt_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(receipt, dict) or not isinstance(receipt.get("previous"), dict):
        return None
    return receipt


def has_receipt() -> bool:
    """Whether configure wrote Cursor's settings and they can be restored."""
    return _read_receipt() is not None


def _write_receipt(receipt: dict) -> None:
    path = _receipt_path()
    temporary = path.with_suffix(".tmp")
    try:
        temporary.write_text(json.dumps(receipt), encoding="utf-8")
        os.replace(temporary, path)
    except OSError as exc:
        raise CursorSettingsError(
            "Cursor's restore record could not be saved."
        ) from exc
