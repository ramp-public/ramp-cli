"""Cursor's local provider settings for Ramp Router setup.

Cursor keeps its Models / API Keys screen in a SQLite state database. While
Cursor is quit, ``ramp router configure cursor`` writes the OpenAI base-URL
override, turns the OpenAI key on, stores the key encrypted the way Cursor
does, and adds Router's model names. A receipt keeps the settings from before
the first write so ``unconfigure`` can restore them.
"""

import copy
import ctypes
import hashlib
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Iterable
from pathlib import Path

# Redirects Cursor's user-data directory. Tests set it so detection never
# reads a developer's real Cursor state, and the app bundle is then ignored.
USER_DIR_ENV = "RAMP_CURSOR_USER_DIR"
_APP_BUNDLE = Path("/Applications/Cursor.app")
# The name of Cursor's main process.
_PROCESS_NAME = "Cursor"
# How long setup waits for a Cursor that is still quitting, and how often it
# checks in the meantime.
QUIT_WAIT_SECONDS = 5.0
_QUIT_POLL_SECONDS = 0.5
# Other Cursor builds, each with its own user-data folder beside Cursor's.
_OTHER_BUILDS = ("Cursor Nightly",)
# Cursor's macOS bundle id. Quitting and reopening by id never touches
# Cursor Nightly, which is a separate app.
APP_BUNDLE_ID = "com.todesktop.230313mzl4w4u92"
# How long setup waits for Cursor to quit once asked. Cursor may first ask
# the user to confirm, for example while agents are running.
QUIT_CONFIRM_SECONDS = 30.0
# The quit request itself can wait on macOS asking whether this terminal may
# control Cursor, which it asks only once.
_QUIT_REQUEST_TIMEOUT = 60
# AppleScript errors for Automation access refused. Any other answer to the
# quit event says nothing reliable: Electron answers it before Cursor has
# closed, or shown its own confirm dialog. Whether Cursor exits decides.
_APPLE_EVENTS_DENIED = ("-1743", "-1744")
QUIT_DONE = "quit"
QUIT_DENIED = "denied"
QUIT_TIMEOUT = "timeout"
QUIT_FAILED = "failed"
# After a quit didn't happen and the user agreed to force it.
QUIT_FORCED = "force_quit"
QUIT_FORCE_FAILED = "force_quit_failed"
# How long a force quit waits after SIGTERM before SIGKILL, and after SIGKILL.
FORCE_TERM_SECONDS = 5.0
FORCE_KILL_SECONDS = 5.0
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


class CursorKeychainError(CursorSettingsError):
    """The keychain did not hand over Cursor's encryption key."""


def settings_shortcut() -> str:
    """The keys that open Cursor Settings on this platform."""
    return "Cmd+Shift+J" if sys.platform == "darwin" else "Ctrl+Shift+J"


def quit_instruction() -> str:
    """How to close Cursor completely here; closing its windows isn't enough."""
    if sys.platform == "darwin":
        return "Quit Cursor (Cmd+Q)"
    return "Close Cursor fully (File > Exit)"


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


def _connect_to_read(path: Path) -> sqlite3.Connection:
    """Open Cursor's state database to read it.

    Read-only while Cursor may be running, since it owns the file. Once Cursor
    is known to be closed on macOS, read-write without creating the file: a
    Cursor that was force quit may have left a journal mid-write, and only a
    writable connection can roll it back (or replay a WAL) before reading.
    """
    mode = "rw" if sys.platform == "darwin" and running_pids() == [] else "ro"
    return sqlite3.connect(f"{path.as_uri()}?mode={mode}", uri=True, timeout=1)


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
        connection = _connect_to_read(path)
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
        connection = _connect_to_read(path)
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


def other_build_user_dirs() -> list[tuple[str, Path]]:
    """Other Cursor builds' user-data folders that hold settings, by build name.

    Cursor Nightly keeps its own folder beside Cursor's, so a missing Cursor
    database can mean the user runs that build instead. Empty when the user
    directory is redirected, as detection then ignores the real machine.
    """
    if os.environ.get(USER_DIR_ENV):
        return []
    base = user_dir().parent.parent
    found = []
    for name in _OTHER_BUILDS:
        user = base / name / "User"
        if state_db_path(user).is_file():
            found.append((name, user))
    return found


def running_pids() -> list[int] | None:
    """Process ids of this user's Cursor main process: [] when none run, and
    None when that can't be told, which callers treat as running.

    Only the main process matters: it rewrites the settings from memory on
    quit. Its helpers are named "Cursor Helper (…)" and are not matched.
    macOS pgrep leaves out its own ancestors unless given -a, and Cursor is
    one when this runs in Cursor's terminal. On Linux, -a means something else.
    """
    command = ["pgrep", "-u", str(os.getuid()), "-x", _PROCESS_NAME]
    if sys.platform == "darwin":
        command.insert(1, "-a")
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode == 1:
        # pgrep's answer for "no process matched".
        return []
    if completed.returncode != 0:
        return None
    pids = []
    for line in (completed.stdout or "").split():
        try:
            pids.append(int(line))
        except ValueError:
            continue
    return pids


def is_running() -> bool:
    """Whether Cursor is, or may be, running; it rewrites its settings from
    memory on quit."""
    return running_pids() != []


def wait_until_quit(timeout: float = QUIT_WAIT_SECONDS) -> list[int] | None:
    """Give a Cursor that is quitting time to exit.

    After a quit the main process stays alive while it closes windows, stops
    extension hosts, and saves its state, which can take a few seconds.
    Returns [] once it has exited, else what ``running_pids`` last said.
    """
    deadline = time.monotonic() + timeout
    pids = running_pids()
    while pids != [] and time.monotonic() < deadline:
        time.sleep(_QUIT_POLL_SECONDS)
        pids = running_pids()
    return pids


def _parent_pid(pid: int) -> int:
    try:
        completed = subprocess.run(
            ["ps", "-o", "ppid=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        return int(completed.stdout.strip() or 0)
    except (OSError, subprocess.SubprocessError, ValueError):
        return 0


def runs_inside_cursor() -> bool:
    """Whether Cursor started this process, as in its terminal or an agent.

    Quitting Cursor would then end this process too.
    """
    cursor_pids = set(running_pids() or [])
    if not cursor_pids:
        return False
    pid, seen = os.getppid(), set()
    while pid > 1 and pid not in seen and len(seen) < 64:
        if pid in cursor_pids:
            return True
        seen.add(pid)
        pid = _parent_pid(pid)
    return False


def request_quit(timeout: float = QUIT_CONFIRM_SECONDS) -> str:
    """Ask Cursor to quit the way Cmd+Q does, and wait for it to exit.

    Cursor saves unsaved files for its next launch and may ask the user to
    confirm, for example while agents are running. A cancel in that dialog
    looks the same as a dialog left unanswered. Never force-quits. Returns
    one of the QUIT_* outcomes.
    """
    # Checked first so that a Cursor which exited meanwhile is not launched
    # only to be told to quit.
    script = (
        f'if application id "{APP_BUNDLE_ID}" is running then '
        f'tell application id "{APP_BUNDLE_ID}" to quit'
    )
    try:
        completed = subprocess.run(
            ["osascript", "-e", script],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=_QUIT_REQUEST_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return QUIT_DONE if running_pids() == [] else QUIT_TIMEOUT
    except (OSError, subprocess.SubprocessError):
        return QUIT_FAILED
    error = completed.stderr or ""
    if completed.returncode != 0 and any(
        code in error for code in _APPLE_EVENTS_DENIED
    ):
        return QUIT_DONE if running_pids() == [] else QUIT_DENIED
    return QUIT_DONE if wait_until_quit(timeout) == [] else QUIT_TIMEOUT


def force_quit(
    term_wait: float = FORCE_TERM_SECONDS, kill_wait: float = FORCE_KILL_SECONDS
) -> str:
    """Stop this user's Cursor main process without asking.

    Only the pids ``running_pids`` names are signalled, and only while they
    still are Cursor, so Cursor Nightly, other apps and a reused pid are never
    touched. SIGTERM comes first: Electron handles it the way it handles
    app.quit(), so Cursor saves its state when nothing vetoes the quit. It can
    still be stopped by Cursor's own confirm dialog, so whatever is left after
    ``term_wait`` gets SIGKILL.

    Returns QUIT_FORCED once Cursor is gone, QUIT_DONE when it had already
    quit and nothing was signalled, and QUIT_FORCE_FAILED otherwise.
    """
    found = running_pids()
    if found is None:
        return QUIT_FORCE_FAILED
    targets = set(found)
    signalled = False

    def alive() -> set[int]:
        pids = running_pids()
        # When pgrep can't answer, assume every target is still there.
        return targets if pids is None else targets & set(pids)

    for sig, wait in ((signal.SIGTERM, term_wait), (signal.SIGKILL, kill_wait)):
        left = alive()
        if not left:
            break
        for pid in sorted(left):
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                continue
            except OSError:
                return QUIT_FORCE_FAILED
            signalled = True
        deadline = time.monotonic() + wait
        while alive() and time.monotonic() < deadline:
            time.sleep(_QUIT_POLL_SECONDS)
    # Settings are written only once no Cursor is left, including one that
    # started meanwhile.
    if running_pids() != []:
        return QUIT_FORCE_FAILED
    return QUIT_FORCED if signalled else QUIT_DONE


def reopen() -> bool:
    """Open Cursor again after setup quit it; its windows come back."""
    try:
        completed = subprocess.run(
            ["open", "-b", APP_BUNDLE_ID],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=10,
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
    remove: Iterable[str] = (),
    remove_owned: Iterable[str] = (),
    model_scope: dict | None = None,
) -> dict:
    """Point Cursor's OpenAI override at Router. Cursor must be quit.

    ``prompt=False`` never shows the macOS permission dialog: the write then
    succeeds only if the Keychain item was already allowed.

    ``remove`` names models to take back out, from ``planned_removals``. Only
    what the receipt says setup added or turned on is touched: a row setup
    added goes, and a row the user added that setup only turned on is turned
    off again. ``remove_owned`` names go wherever they are, as setup owns
    them elsewhere: effort-variant names, which the steps may have had the
    user add by hand. ``model_scope`` is saved in the receipt for the next
    run.

    The key is encrypted first, since the keychain dialog can stay open for
    minutes. Only then is Cursor checked, and the settings read and written
    back to back; a Cursor that started during the write is reported too.
    """
    encrypted = encrypt(api_key) if encrypt else encrypt_with_cursor(api_key, prompt)
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
    added_now = []
    for name in model_names:
        if name not in added:
            added.append(name)
            added_now.append(name)
            if name not in receipt["models_added"]:
                receipt["models_added"].append(name)
        if name not in enabled:
            enabled.append(name)
            enabled_added = receipt.setdefault("enabled_added", [])
            if name not in enabled_added:
                enabled_added.append(name)
    # The choice is saved with the first write, so it holds even if the
    # pruning below can't be saved.
    if model_scope is not None:
        receipt["model_scope"] = model_scope
    # Everything setup has put in Cursor, removals included: what the
    # receipt holds until Cursor's database has the new lists.
    everything = copy.deepcopy(receipt)
    removed_now = _remove(receipt, added, enabled, set(remove) - set(model_names))
    for name in dict.fromkeys(remove_owned):
        if name in model_names:
            continue
        present = name in added or name in enabled
        while name in added:
            added.remove(name)
        while name in enabled:
            enabled.remove(name)
        if present and name not in removed_now:
            removed_now.append(name)
    settings["openAIBaseUrl"] = base_url.rstrip("/")
    settings["useOpenAIKey"] = True
    index[_OPENAI_KEY_SECRET] = 1
    # Save the originals before changing anything, so a failed write can
    # still be undone and a retry cannot record Router's values as previous.
    # Names this run removes stay in it until the write lands, so a failed
    # write never leaves them in Cursor with nothing to undo them.
    _write_receipt(everything)
    _write(path, settings, encrypted, index)
    if is_running():
        # Cursor may have loaded the settings from before the write, and
        # would then save those back over Router's on quit.
        raise CursorSettingsError("Cursor was opened while setup wrote its settings.")
    if receipt != everything:
        # Cursor already has the new lists, so pruning the receipt is cleanup:
        # if it fails, the receipt still names every row setup added, which
        # unconfigure and the next run handle the same way.
        try:
            _write_receipt(receipt)
        except CursorSettingsError:
            pass
    return {**receipt, "added_now": added_now, "removed_now": removed_now}


def _remove(receipt: dict, added: list, enabled: list, names: set[str]) -> list[str]:
    """Take ``names`` out where setup put them; edits all three in place."""
    models_added = receipt.setdefault("models_added", [])
    enabled_added = receipt.setdefault("enabled_added", list(models_added))
    removed: list[str] = []
    for name in [name for name in models_added if name in names]:
        # Setup added the row, so the row goes, and its toggle with it when
        # setup turned that on. A toggle the user had on already stays theirs.
        present = name in added or name in enabled
        while name in added:
            added.remove(name)
        if name in enabled_added:
            while name in enabled:
                enabled.remove(name)
            enabled_added.remove(name)
        models_added.remove(name)
        if present:
            removed.append(name)
    for name in [name for name in enabled_added if name in names]:
        # The user added the row and setup only turned it on: turn it off.
        present = name in enabled
        while name in enabled:
            enabled.remove(name)
        enabled_added.remove(name)
        if present:
            removed.append(name)
    return removed


def planned_removals(wanted: Iterable[str]) -> list[str]:
    """Names configure would take out of Cursor's list to keep only ``wanted``.

    Only names the receipt says setup added or turned on, and that Cursor
    still lists; never a model the user added and turned on themselves.
    Empty when this CLI never wrote Cursor's settings.
    """
    receipt = _read_receipt()
    if receipt is None:
        return []
    wanted = set(wanted)
    models_added = [n for n in receipt.get("models_added") or [] if isinstance(n, str)]
    enabled_added = [
        n
        for n in receipt.get("enabled_added", models_added) or []
        if isinstance(n, str)
    ]
    listed: set[str] | None = None
    turned_on: set[str] | None = None
    if running_pids() == []:
        try:
            settings, _, _ = _read(state_db_path())
        except CursorSettingsError:
            pass
        else:
            ai_settings = settings.get("aiSettings") or {}
            if isinstance(ai_settings, dict):
                listed = set(_names(ai_settings.get("userAddedModels")))
                turned_on = set(_names(ai_settings.get("modelOverrideEnabled")))
    removals: list[str] = []
    for name in [*models_added, *enabled_added]:
        if name in wanted or name in removals:
            continue
        if name in models_added:
            present = listed is None or name in listed or name in (turned_on or ())
        else:
            present = turned_on is None or name in turned_on
        if present:
            removals.append(name)
    return removals


def _names(value: object) -> list[str]:
    return [n for n in value if isinstance(n, str)] if isinstance(value, list) else []


def saved_model_scope() -> dict | None:
    """The model-list choice the last configure saved, if any."""
    receipt = _read_receipt()
    scope = receipt.get("model_scope") if receipt else None
    return scope if isinstance(scope, dict) else None


def unconfigure() -> bool:
    """Restore the settings from before the first configure.

    Returns False when this CLI never wrote Cursor's settings.
    """
    receipt = _read_receipt()
    if receipt is None:
        return False
    if is_running():
        raise CursorSettingsError(
            f"Cursor is open. {quit_instruction()}, keep it closed, and run this again."
        )
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


# Cursor's encryption key once the keychain handed it over, so that setup can
# ask for it before quitting Cursor and not again when it writes.
_encryption_key: bytes | None = None


def unlock(prompt: bool = True) -> None:
    """Get Cursor's encryption key from the keychain now, for this process.

    Raises CursorKeychainError when the keychain doesn't hand it over.
    """
    _cursor_encryption_key(prompt)


def _cursor_encryption_key(prompt: bool) -> bytes:
    global _encryption_key
    if sys.platform != "darwin":
        raise CursorSettingsError("Writing Cursor's settings needs macOS.")
    if _encryption_key is None:
        password = _keychain_password(prompt)
        _encryption_key = hashlib.pbkdf2_hmac("sha1", password, b"saltysalt", 1003, 16)
    return _encryption_key


def encrypt_with_cursor(api_key: str, prompt: bool = True) -> str:
    """Encrypt a key the way Cursor's Electron safeStorage does on macOS.

    Chromium's scheme: the "Cursor Safe Storage" Keychain password, PBKDF2-SHA1
    (salt "saltysalt", 1003 rounds, 16 bytes), AES-128-CBC with a 16-space IV,
    and a "v10" prefix, stored as ``JSON.stringify`` of a Node Buffer.
    """
    key = _cursor_encryption_key(prompt)
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
        raise CursorKeychainError(
            "Cursor's encryption key was not available from the keychain."
        )
    try:
        return ctypes.string_at(data.value, length.value)
    finally:
        security.SecKeychainItemFreeContent(None, data)


def _read(path: Path) -> tuple[dict, str | None, dict]:
    """Return Cursor's settings, the encrypted OpenAI key, and the secret index.

    Opened read-write, without creating the file: after a force quit SQLite may
    find a journal Cursor left mid-write, and only a writable connection can
    roll it back (or replay a WAL) before reading. A read-only one fails.
    """
    try:
        connection = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=5)
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
