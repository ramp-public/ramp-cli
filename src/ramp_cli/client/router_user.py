"""Router web-user authentication, deliberately separate from inference keys.

Refresh credentials reference a dedicated Router CLI session. The server's ordinary
guard can require browser recovery; no client-side TTL can override that verdict.
Credentials use the Core CLI baseline: private plaintext files and atomic writes.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from http.cookiejar import CookieJar, DefaultCookiePolicy
from pathlib import Path
from threading import Event, Lock, Thread
from urllib.parse import urlencode, urlsplit

import click
import httpx

from ramp_cli.auth import oauth
from ramp_cli.auth.refresh import _refresh_lock
from ramp_cli.config import settings
from ramp_cli.errors import EXIT_AUTH_REQUIRED, ApiError, RampCLIError

DEFAULT_ORIGIN = "https://app.router.com"
# Seconds reads reuse a passed session check. Writes always check first, so a
# revoked session stops every change at once and reads within this window.
SESSION_CHECK_REUSE_SECONDS = 60.0

# Internal Routers sit behind Cloudflare Access, which redirects every request
# without an Access token, API calls included, to its own sign-in page.
_ACCESS_DOMAIN = "cloudflareaccess.com"
# Access tokens `cloudflared` issued this process, with when to stop using them.
_edge_tokens: dict[str, tuple[str, float]] = {}
# Origins seen behind Access this process, even before `cloudflared` knew them.
_edge_origins: set[str] = set()
# Access tokens an origin turned away. cloudflared keeps handing back a token
# until it expires, so having one is no proof Access still accepts it.
_edge_rejected: dict[str, set[str]] = {}


def _edge_redirect(response: httpx.Response) -> bool:
    if not response.is_redirect:
        return False
    host = urlsplit(response.headers.get("location", "")).hostname or ""
    return host == _ACCESS_DOMAIN or host.endswith("." + _ACCESS_DOMAIN)


def _single_token(token: str) -> bool:
    return bool(token) and token.isascii() and not any(c.isspace() for c in token)


def _token_expiry(token: str) -> float:
    try:
        part = token.split(".")[1]
        expiry = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))[
            "exp"
        ]
        return float(expiry)
    except (IndexError, KeyError, TypeError, ValueError):
        return time.time() + 300


def _edge_token(origin: str) -> str | None:
    """A current Access token for ``origin`` from `cloudflared`, if it has one."""
    cached = _edge_tokens.get(origin)
    if cached is not None and time.time() < cached[1]:
        return cached[0]
    binary = shutil.which("cloudflared")
    host = urlsplit(origin).hostname or ""
    # Only ask about apps cloudflared already signed in to, or that redirected,
    # so a public Router never pays for a subprocess.
    known = origin in _edge_origins or any(
        (Path.home() / ".cloudflared").glob(f"{host}-*-token")
    )
    if binary is None or not known:
        return None
    try:
        result = subprocess.run(
            [binary, "access", "token", f"--app={origin}"],
            capture_output=True,
            text=True,
            timeout=15,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    token = result.stdout.strip()
    if result.returncode != 0 or not _single_token(token) or token.count(".") != 2:
        return None
    if token in _edge_rejected.get(origin, ()):
        return None
    _edge_tokens[origin] = (token, _token_expiry(token) - 60)
    return token


def _target_path(profile: str | None) -> Path:
    key = hashlib.sha256((profile or "").encode()).hexdigest()[:16]
    return settings.config_dir() / "router-users" / f"target-{key}.json"


def selected_origin(explicit: str | None = None, *, profile: str | None = None) -> str:
    """Explicit flag > environment > last successful login > public default."""
    override = explicit or os.environ.get("RAMP_ROUTER_UI_URL")
    if override:
        return validate_origin(override)
    return saved_origin(profile=profile) or DEFAULT_ORIGIN


def saved_origin(*, profile: str | None = None) -> str | None:
    """The Router last signed in to or chosen in the TUI's Settings, if any."""
    path = _target_path(profile)
    if not path.exists():
        return None
    try:
        origin = json.loads(path.read_text())["origin"]
        if not isinstance(origin, str):
            raise ValueError("invalid origin")
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise RampCLIError(
            "Invalid saved Router target; pass --ui-url to login."
        ) from exc
    return validate_origin(origin)


def remember_origin(origin: str, *, profile: str | None = None) -> None:
    """Make ``origin`` the Router target later runs pick up (see selected_origin)."""
    _atomic_write(_target_path(profile), {"origin": validate_origin(origin)})


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as output:
            json.dump(data, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def validate_origin(value: str) -> str:
    parsed = urlsplit(value)
    loopback = parsed.hostname in ("localhost", "127.0.0.1")
    if (
        not parsed.hostname
        or parsed.scheme not in (("http", "https") if loopback else ("https",))
        or parsed.username
        or parsed.password
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise click.BadParameter(
            "Router URL must be an HTTPS origin (HTTP is allowed for localhost)"
        )
    return value.rstrip("/")


def _error_code(response: httpx.Response) -> str | None:
    try:
        payload = response.json()
    except ValueError:
        return None
    error = payload.get("error") if isinstance(payload, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    return code if isinstance(code, str) else None


def _jwt_account(token: str) -> tuple[str, str | None] | None:
    """Continuity check only; the API still verifies the JWT's authenticity."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        payload = json.loads(
            base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
        )
        if not isinstance(payload, dict):
            return None
        subject, business = payload.get("sub"), payload.get("rampBusinessUuid")
        if not isinstance(subject, str) or not subject:
            return None
        if business is not None and (not isinstance(business, str) or not business):
            return None
        return subject, business
    except (TypeError, ValueError):
        return None


_RECOVERY = {
    "signed_out": (
        "Not signed in to Router. Run 'ramp router login'.",
        "You're not signed in to Router.",
    ),
    "expired": (
        "Router login has expired or was revoked. Run 'ramp router login'.",
        "Your Router sign-in has expired.",
    ),
    "reauth": (
        "Router needs browser reauthorization. Run 'ramp router login'.",
        "Router needs you to sign in again.",
    ),
    "business_revoked": (
        "Your access to this Ramp business has ended. Run 'ramp router login'.",
        "Your access to this Ramp business has ended.",
    ),
}


def _stdin_is_terminal() -> bool:
    try:
        return click.get_text_stream("stdin").isatty()
    except (OSError, ValueError):
        return False


def _read_pasted_callback(callback: oauth.PkceCallback) -> None:
    """Also accept the callback address typed into the terminal.

    Read on a daemon thread so the browser's own callback can still win; the
    prompt is left behind unanswered if it does.
    """

    def read() -> None:
        while not callback._event.is_set():
            # On stderr, like the sign-in URL: stdout may carry JSON output.
            click.echo(
                "If the browser can't reach this machine, paste the address "
                "it was sent to: ",
                err=True,
                nl=False,
            )
            try:
                url = input()
            except (EOFError, OSError, ValueError):
                return
            if not url.strip():
                continue
            try:
                callback.submit_url(url)
            except click.ClickException as error:
                click.echo(error.format_message(), err=True)

    Thread(target=read, daemon=True).start()


class RouterUserClient:
    def __init__(
        self,
        origin: str,
        *,
        profile: str | None = None,
        no_input: bool = False,
        interactive: bool = False,
    ):
        self.origin = validate_origin(origin)
        self.profile = profile
        self.no_input = no_input
        # A person at a terminal can approve a browser sign-in, so a missing,
        # expired, or revoked login signs in instead of failing.
        self.interactive = interactive and not no_input
        self.namespace = (
            "router-user-"
            + hashlib.sha256(f"{self.origin}\n{profile or ''}".encode()).hexdigest()[
                :32
            ]
        )
        self.path = settings.config_dir() / "router-users" / f"{self.namespace}.json"
        # Keep multi-request operations (read, edit, write) on the account whose
        # data they first used, even if another process replaces the login file.
        self._request_account: tuple[str, str | None] | None = None
        # (refresh token, access token it returned, monotonic deadline) from
        # the last passed session check; read and written under the refresh lock.
        self._session_check: tuple[str, str, float] | None = None
        self._http: httpx.Client | None = None
        self._http_lock = Lock()
        self._closed = False

    def _pool(self) -> httpx.Client:
        """One connection pool per client, so requests reuse TLS connections.

        Timeouts and Access headers go on each request; they can differ. No
        cookies are kept: every request authenticates with its own headers.
        """
        with self._http_lock:
            if self._closed:
                raise RampCLIError("Router client is closed.")
            if self._http is None:
                self._http = httpx.Client(
                    trust_env=False,
                    follow_redirects=False,
                    event_hooks=self._edge_hooks(),
                    cookies=CookieJar(policy=DefaultCookiePolicy(allowed_domains=[])),
                )
            return self._http

    def close(self) -> None:
        with self._http_lock:
            self._closed = True
            pool, self._http = self._http, None
        if pool is not None:
            pool.close()

    def _access_headers(self) -> dict[str, str]:
        """Opt-in edge auth, bound to one origin and never persisted by the CLI."""
        origin = os.environ.get("RAMP_ROUTER_ACCESS_ORIGIN", "").strip().rstrip("/")
        token = os.environ.get("RAMP_ROUTER_ACCESS_TOKEN", "").strip()
        if token and not origin:
            raise RampCLIError(
                "Set RAMP_ROUTER_ACCESS_ORIGIN when providing RAMP_ROUTER_ACCESS_TOKEN.",
                code=4,
            )
        if origin != self.origin:
            edge = _edge_token(self.origin)
            return {"cf-access-token": edge} if edge else {}
        if not _single_token(token):
            raise RampCLIError(
                "Set RAMP_ROUTER_ACCESS_TOKEN to a single valid Access token.", code=4
            )
        return {"cf-access-token": token}

    def _edge_hooks(self) -> dict:
        """Turn an Access redirect into a sign-in prompt, not an API error."""

        def check(response: httpx.Response) -> None:
            if _edge_redirect(response):
                _edge_origins.add(self.origin)
                _edge_tokens.pop(self.origin, None)
                sent = response.request.headers.get("cf-access-token")
                if sent:
                    # Login must run Access sign-in again, not reload this.
                    _edge_rejected.setdefault(self.origin, set()).add(sent)
                raise RampCLIError(
                    f"{self.origin} is behind Cloudflare Access. "
                    "Run 'ramp router login' to sign in.",
                    code=EXIT_AUTH_REQUIRED,
                )

        return {"response": [check]}

    def _edge_protected(self) -> bool:
        if self.origin in _edge_origins:
            return True
        try:
            with httpx.Client(
                timeout=10, trust_env=False, follow_redirects=False
            ) as http:
                response = http.post(self.origin + "/api/auth/cli/session")
        except httpx.HTTPError:
            # Unreachable is for the next request to report.
            return False
        if _edge_redirect(response):
            _edge_origins.add(self.origin)
            return True
        return False

    def ensure_edge_access(
        self,
        *,
        progress: Callable[[str], None] | None = None,
        cancel: Event | None = None,
        timeout: int = 900,
    ) -> None:
        """Sign in to Cloudflare Access first when this Router sits behind it."""
        if self._access_headers() or not self._edge_protected():
            return
        binary = shutil.which("cloudflared")
        if binary is None:
            raise RampCLIError(
                f"{self.origin} is behind Cloudflare Access. Install cloudflared, "
                "or set RAMP_ROUTER_ACCESS_ORIGIN and RAMP_ROUTER_ACCESS_TOKEN.",
                code=EXIT_AUTH_REQUIRED,
            )
        message = "Approve Cloudflare Access in your browser to continue..."
        if progress is not None:
            progress(message)
        else:
            click.echo(message, err=True)
        # Its output would draw over a full-screen UI, so it goes nowhere.
        process = subprocess.Popen(
            [binary, "access", "login", self.origin],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + timeout
        try:
            while True:
                try:
                    process.wait(timeout=0.1)
                    break
                except subprocess.TimeoutExpired:
                    pass
                if cancel is not None and cancel.is_set():
                    raise click.ClickException("Router sign-in cancelled.")
                if time.monotonic() >= deadline:
                    raise click.ClickException("Cloudflare Access sign-in timed out.")
        finally:
            if process.poll() is None:
                process.kill()
        _edge_tokens.pop(self.origin, None)
        if not self._access_headers():
            raise RampCLIError(
                f"Couldn't sign in to Cloudflare Access for {self.origin}.",
                code=EXIT_AUTH_REQUIRED,
            )

    def _load(self) -> dict:
        if not self.path.exists():
            return {}
        if os.name != "nt" and self.path.stat().st_mode & 0o077:
            raise RampCLIError(
                "Router login file is not private; restrict its permissions to 600."
            )
        try:
            data = json.loads(self.path.read_text())
        except (ValueError, OSError) as exc:
            raise RampCLIError("Could not read the Router login file.") from exc
        if not isinstance(data, dict) or data.get("origin") != self.origin:
            raise RampCLIError("Invalid Router login file.")
        return data

    def _save(self, data: dict) -> None:
        _atomic_write(self.path, {**data, "origin": self.origin})

    def _clear(self) -> None:
        self._session_check = None
        self.path.unlink(missing_ok=True)

    def _token_response(
        self, response: httpx.Response, *, cached_refresh_token: str | None = None
    ) -> dict:
        if response.status_code != 200:
            raise ApiError(response.status_code, response.text)
        try:
            data = response.json()
            if cached_refresh_token is not None:
                data["refresh_token"] = cached_refresh_token
            access = data["access_token"]
            refresh = data["refresh_token"]
            lifetime = data["expires_in"]
            refresh_lifetime = data["refresh_token_expires_in"]
            if (
                not isinstance(access, str)
                or not access
                or not isinstance(refresh, str)
                or not refresh
                or type(lifetime) is not int
                or lifetime <= 0
                or type(refresh_lifetime) is not int
                or refresh_lifetime <= 0
                or data.get("token_type", "").lower() != "bearer"
            ):
                raise ValueError("invalid credentials")
        except (KeyError, TypeError, ValueError) as exc:
            raise RampCLIError(
                "Router returned an incomplete token response; sign in again."
            ) from exc
        return {**data, "issued_at": int(time.time()), "origin": self.origin}

    def login(
        self,
        *,
        no_browser: bool = False,
        timeout: int = 900,
        progress: Callable[[str], None] | None = None,
        cancel: Event | None = None,
        on_callback: Callable[[oauth.PkceCallback], None] | None = None,
    ) -> dict:
        """Sign in through the browser.

        ``on_callback`` receives the pending callback, so a caller can finish
        it from a pasted address when the browser can't reach this machine.
        Without one, an interactive terminal is asked for that address.
        """
        self.ensure_edge_access(progress=progress, cancel=cancel, timeout=timeout)
        access_headers = self._access_headers()
        callback = oauth.start_pkce_callback()
        if on_callback is not None:
            on_callback(callback)
        url = (
            self.origin
            + "/api/auth/cli/authorize?"
            + urlencode(
                {
                    "redirect_uri": callback.redirect_uri,
                    "state": callback.state,
                    "code_challenge": callback.code_challenge,
                    "code_challenge_method": "S256",
                }
            )
        )
        try:
            message = "Approve the sign-in in your browser to continue..."
            if no_browser or not oauth._open_browser(url):
                # One message: a progress display replaces the last one, and
                # the URL must stay visible until sign-in completes.
                message = f"Open this URL to sign in:\n\n  {url}\n\n{message}"
            if progress is not None:
                progress(message)
            else:
                click.echo(message, err=True)
                if on_callback is None and _stdin_is_terminal():
                    _read_pasted_callback(callback)
            if cancel is not None:
                deadline = time.monotonic() + timeout
                while not callback._event.wait(timeout=0.1):
                    if cancel.is_set():
                        raise click.ClickException("Router sign-in cancelled.")
                    if time.monotonic() >= deadline:
                        raise click.ClickException("Router user login timed out.")
                if cancel.is_set():
                    raise click.ClickException("Router sign-in cancelled.")
            code = callback.wait_for_code(
                timeout, timeout_message="Router user login timed out."
            )
        finally:
            callback.shutdown()
        with httpx.Client(
            timeout=30,
            trust_env=False,
            follow_redirects=False,
            headers=access_headers,
            event_hooks=self._edge_hooks(),
        ) as http:
            data = self._token_response(
                http.post(
                    self.origin + "/api/auth/cli/token",
                    json={
                        "grant_type": "authorization_code",
                        "code": code,
                        "code_verifier": callback._verifier,
                        "redirect_uri": callback.redirect_uri,
                    },
                )
            )
            with _refresh_lock(self.namespace):
                old = self._load()
                self._save(data)
                _atomic_write(_target_path(self.profile), {"origin": self.origin})
            if old.get("refresh_token"):
                # Revoke only this machine's previous grant, not the web session.
                http.post(
                    self.origin + "/api/auth/cli/logout",
                    headers={
                        "Authorization": "Bearer " + old["refresh_token"],
                    },
                )
        return self.status()

    def status(self) -> dict:
        state = self._load()
        now = int(time.time())
        return {
            "origin": self.origin,
            "authenticated": bool(state.get("refresh_token"))
            and now
            < state.get("issued_at", 0) + state.get("refresh_token_expires_in", 0),
            "user": state.get("user"),
            "access_token_expired": now
            >= state.get("issued_at", 0) + state.get("expires_in", 0),
        }

    def logout(self) -> None:
        with _refresh_lock(self.namespace):
            state = self._load()
            if state.get("refresh_token"):
                with httpx.Client(
                    timeout=30,
                    trust_env=False,
                    follow_redirects=False,
                    headers=self._access_headers(),
                    event_hooks=self._edge_hooks(),
                ) as http:
                    response = http.post(
                        self.origin + "/api/auth/cli/logout",
                        headers={
                            "Authorization": "Bearer " + state["refresh_token"],
                        },
                    )
                    if response.status_code not in (200, 400, 401):
                        raise ApiError(response.status_code, response.text)
            self._clear()

    def access_token(
        self,
        *,
        force: bool = False,
        rejected_token: str | None = None,
        check_session: bool = False,
        recover: bool = True,
        timeout: float = 30,
        reuse_session: bool = False,
    ) -> str:
        reason: str
        with _refresh_lock(self.namespace):
            state = self._load()
            now = int(time.time())
            access = state.get("access_token")
            pending = state.get("pending_refresh_token")
            # Finish a durable pending rotation before using the old secret
            # anywhere, including /session. The response may have been lost.
            cached_valid = (
                not pending
                and access
                and (
                    (rejected_token and access != rejected_token)
                    or (
                        not force
                        and now
                        < state.get("issued_at", 0)
                        + state.get("expires_in", 0)
                        - (30 if recover else 0)
                    )
                )
            )
            if cached_valid and not check_session:
                return access
            checked = self._session_check
            if (
                cached_valid
                and reuse_session
                and not force
                and rejected_token is None
                and checked is not None
                and checked[0] == state.get("refresh_token")
                and time.monotonic() < checked[2]
            ):
                return checked[1]
            self._session_check = None
            if not recover and not cached_valid:
                raise RampCLIError("Router access token needs renewal.", code=4)
            if not state.get("refresh_token"):
                reason = "signed_out"
            else:
                http = self._pool()
                access_headers = self._access_headers()
                if cached_valid and check_session:
                    response = http.post(
                        self.origin + "/api/auth/cli/session",
                        headers={
                            **access_headers,
                            "Authorization": "Bearer " + state["refresh_token"],
                        },
                        timeout=timeout,
                    )
                else:
                    if not pending:
                        pending = (
                            state["refresh_token"].split(".")[0]
                            + "."
                            + secrets.token_urlsafe(32)
                        )
                        state["pending_refresh_token"] = pending
                        # If this write fails, do not consume the current
                        # credential on the server. The refresh lock covers
                        # preparation, HTTP, and committing its response.
                        self._save(state)
                    response = http.post(
                        self.origin + "/api/auth/cli/token",
                        json={
                            "grant_type": "refresh_token",
                            "refresh_token": state["refresh_token"],
                            "next_refresh_token": pending,
                        },
                        headers=access_headers,
                        timeout=timeout,
                    )
                if response.status_code == 200:
                    try:
                        data = self._token_response(
                            response,
                            cached_refresh_token=(
                                state["refresh_token"]
                                if cached_valid and check_session
                                else None
                            ),
                        )
                    except RampCLIError:
                        self._clear()
                        raise
                    # Keep the original access-token clock on a session check;
                    # otherwise frequent reads could postpone rotation forever.
                    if cached_valid and check_session:
                        # Reuse ends early if the token it returned would expire.
                        lifetime = min(
                            SESSION_CHECK_REUSE_SECONDS,
                            float(data.get("expires_in") or 0) - 30,
                        )
                        self._session_check = (
                            state["refresh_token"],
                            data["access_token"],
                            time.monotonic() + lifetime,
                        )
                    else:
                        self._save(data)
                    return data["access_token"]
                try:
                    error = response.json()
                except ValueError:
                    error = {}
                code = error.get("code") or error.get("error")
                if code == "RAMP_REAUTH_REQUIRED":
                    reason = "reauth"
                elif code in (
                    "invalid_grant",
                    "RAMP_ACCESS_REVOKED",
                    "RAMP_SESSION_UNLINKED",
                ):
                    self._clear()
                    reason = "expired"
                else:
                    # Timeouts, outages and concurrent refreshes keep credentials.
                    raise ApiError(response.status_code, response.text)
        # Sign-in runs outside the refresh lock, which login() takes itself.
        if not recover:
            raise RampCLIError(_RECOVERY[reason][0], code=4)
        return self.recover_login(reason)

    def recover_login(self, reason: str) -> str:
        """Sign in again when a person can approve it; otherwise explain how."""
        error, notice = _RECOVERY[reason]
        # Router-requested reauthorization has always opened the browser unless
        # prompts are off; other recoveries also need an interactive terminal.
        allowed = not self.no_input if reason == "reauth" else self.interactive
        if not allowed:
            raise RampCLIError(error, code=4)
        click.echo(f"{notice} Opening your browser to sign in...", err=True)
        self.login()
        return self.access_token()

    def get(
        self,
        path: str,
        *,
        params: dict | None = None,
        timeout: float = 30,
        refresh: bool = True,
    ) -> dict:
        return self._request(
            "GET", path, params=params, timeout=timeout, refresh=refresh
        )

    def post(
        self,
        path: str,
        *,
        json: dict,
        idempotency_key: str | None = None,
        timeout: float = 30,
    ) -> dict:
        # A 401 retry only follows a rejected request; key creation also sends
        # an idempotency key so a retry can never mint a second secret.
        return self._request(
            "POST",
            path,
            json=json,
            headers={"Idempotency-Key": idempotency_key} if idempotency_key else None,
            timeout=timeout,
        )

    def patch(self, path: str, *, json: dict, timeout: float = 30) -> dict:
        return self._request("PATCH", path, json=json, timeout=timeout)

    def delete(
        self, path: str, *, params: dict | None = None, timeout: float = 30
    ) -> dict:
        return self._request("DELETE", path, params=params, timeout=timeout)

    def _request(self, method: str, path: str, **kwargs) -> dict:
        try:
            return self._send(method, path, **kwargs)
        except (httpx.ConnectError, httpx.ConnectTimeout) as error:
            raise RampCLIError(
                f"Can't reach Router at {self.origin}. Check your connection, "
                "or that the Router URL is right."
            ) from error

    def _send(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
        headers: dict | None = None,
        timeout: float = 30,
        refresh: bool = True,
    ) -> dict:
        if (
            not path.startswith(("/client/", "/admin/"))
            or ".." in path
            or "?" in path
            or "#" in path
        ):
            raise ValueError("Expected a Router user API path")
        stored_token = self._load().get("access_token")
        expected_account = self._request_account or (
            _jwt_account(stored_token) if isinstance(stored_token, str) else None
        )
        is_write = method.upper() not in ("GET", "HEAD", "OPTIONS")
        # Reads may reuse a recent session check; writes always make their own.
        reuse_session = not is_write
        if refresh:
            token = self.access_token(
                check_session=True, timeout=timeout, reuse_session=reuse_session
            )
        else:
            state = self._load()
            if not state.get("access_token") or int(time.time()) >= state.get(
                "issued_at", 0
            ) + state.get("expires_in", 0):
                raise RampCLIError("Router access token needs renewal.", code=4)
            token = self.access_token(
                check_session=True,
                recover=False,
                timeout=timeout,
                reuse_session=reuse_session,
            )

        original_account = _jwt_account(token)
        if (is_write or self._request_account is not None) and (
            (expected_account is not None and original_account != expected_account)
            or (
                is_write
                and stored_token
                and stored_token != token
                and expected_account is None
            )
        ):
            raise RampCLIError(
                "Router account or business changed during sign-in. "
                "The request was not sent; review the account and run the command again.",
                code=4,
            )

        access_headers = self._access_headers()

        def send(http: httpx.Client, bearer: str) -> httpx.Response:
            return http.request(
                method,
                self.origin + path,
                params=params,
                json=json,
                headers={
                    **access_headers,
                    **(headers or {}),
                    "Authorization": "Bearer " + bearer,
                },
                timeout=timeout,
            )

        http = self._pool()
        response = send(http, token)
        if response.status_code == 401 and refresh:
            token = self.access_token(
                force=True,
                rejected_token=token,
                check_session=True,
                timeout=timeout,
            )
            if (is_write or self._request_account is not None) and (
                original_account is None or _jwt_account(token) != original_account
            ):
                raise RampCLIError(
                    "Router account or business changed during sign-in. "
                    "The write was not retried; review the account and run the command again.",
                    code=4,
                )
            response = send(http, token)
        if (
            response.status_code == 403
            and refresh
            and _error_code(response) == "business_access_revoked"
        ):
            # The session's Ramp business link was revoked; a fresh sign-in
            # binds a live one. Safe to retry: nothing was written.
            token = self.recover_login("business_revoked")
            if (is_write or self._request_account is not None) and (
                original_account is None or _jwt_account(token) != original_account
            ):
                raise RampCLIError(
                    "Router account or business changed during sign-in. "
                    "The write was not retried; review the account and run the command again.",
                    code=4,
                )
            response = send(http, token)
        if not response.is_success:
            raise ApiError(response.status_code, response.text)
        self._request_account = _jwt_account(token)
        if response.status_code == 204 or not response.content:
            return {}
        return response.json()
