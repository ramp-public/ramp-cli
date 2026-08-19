import socket
import threading
import urllib.error
import urllib.parse
import urllib.request

import click
import pytest

import ramp_cli.router_setup as router_setup
from ramp_cli.router_setup import start_router_key_callback

ROUTER_UI_URL = "https://app.router.com"


def _post(callback, fields, *, content_type="application/x-www-form-urlencoded"):
    body = urllib.parse.urlencode(fields).encode()
    request = urllib.request.Request(
        callback.redirect_uri,
        data=body,
        headers={"Content-Type": content_type},
    )
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def test_callback_accepts_a_state_bound_form_post_without_echoing_the_key():
    callback = start_router_key_callback(ROUTER_UI_URL)
    try:
        status, page = _post(
            callback,
            {"state": callback.state, "api_key": "ramp-secret-key"},
        )

        assert status == 200
        assert "Ramp Router connected" in page
        assert "ramp-secret-key" not in page
        assert callback.wait_for_key(timeout=1) == "ramp-secret-key"
    finally:
        callback.shutdown()


def test_a_completed_callback_closes_the_window_router_opened():
    # Setup is finished by the time this renders, so leaving the popup up only
    # asks the user to clean up after us.
    callback = start_router_key_callback(ROUTER_UI_URL)
    try:
        _, page = _post(
            callback,
            {"state": callback.state, "api_key": "ramp-secret-key"},
        )

        # Only a window opened from script may be closed from script.
        assert "if (window.opener)" in page
        assert "window.close()" in page
        # Router has to hear the acceptance before the window goes away.
        assert page.index("postMessage") < page.index("window.close()")
        # A browser that refuses to close leaves this page up, so it still has
        # to say what to do with it.
        assert "You can close this window" in page
    finally:
        callback.shutdown()


def test_a_failed_callback_stays_open_to_be_read():
    callback = start_router_key_callback(ROUTER_UI_URL)
    try:
        _, page = _post(callback, {"state": "wrong", "api_key": "not-accepted"})

        assert "State mismatch" in page
        assert "window.close()" not in page
    finally:
        callback.shutdown()


def test_callback_rejects_a_state_mismatch():
    callback = start_router_key_callback(ROUTER_UI_URL)
    try:
        status, page = _post(
            callback,
            {"state": "wrong", "api_key": "must-not-be-accepted"},
        )

        assert status == 400
        assert "State mismatch" in page
        assert "must-not-be-accepted" not in page
        with pytest.raises(click.ClickException, match="State mismatch"):
            callback.wait_for_key(timeout=1)
    finally:
        callback.shutdown()


def test_callback_rejects_non_form_content():
    callback = start_router_key_callback(ROUTER_UI_URL)
    try:
        status, _ = _post(
            callback,
            {"state": callback.state, "api_key": "secret"},
            content_type="application/json",
        )

        assert status == 415
        with pytest.raises(click.ClickException, match="form POST"):
            callback.wait_for_key(timeout=1)
    finally:
        callback.shutdown()


def test_callback_times_out_cleanly():
    callback = start_router_key_callback(ROUTER_UI_URL)
    try:
        with pytest.raises(click.ClickException, match="timed out"):
            callback.wait_for_key(timeout=0)
    finally:
        callback.shutdown()


def test_shutdown_unblocks_the_server_thread():
    callback = start_router_key_callback(ROUTER_UI_URL)
    thread: threading.Thread = callback._thread
    callback.shutdown()
    assert not thread.is_alive()


def test_handshake_proves_the_listener_holds_the_pkce_verifier():
    callback = start_router_key_callback(ROUTER_UI_URL)
    try:
        handshake_url = callback.redirect_uri.replace(
            "/callback", f"/handshake?state={callback.state}"
        )
        with urllib.request.urlopen(handshake_url) as response:
            page = response.read().decode()

        assert response.status == 200
        assert '"type": "ramp-cli-ready"' in page
        assert '"code_verifier"' in page
        assert callback.code_challenge not in page
        assert "https://app.router.com" in page
        assert "postMessage(" in page
        assert '"https://app.router.com"' in page
        # The verification step is visible while Router creates the key, so it
        # uses the same dark terminal chrome as the final callback instead of
        # flashing a bare white page.
        assert "Connecting to Ramp CLI\u2026" in page
        assert 'class="w"' in page
        assert "background:#090909" in page
        assert "<body><p>" not in page
        assert "window.resizeTo(560, 360)" in page
        assert page.index("resizeTo") < page.index("postMessage")
    finally:
        callback.shutdown()


def test_callback_consumes_state_before_a_replay_can_replace_the_key():
    callback = start_router_key_callback(ROUTER_UI_URL)
    try:
        first_status, _ = _post(
            callback, {"state": callback.state, "api_key": "first-key"}
        )
        replay_status, _ = _post(
            callback, {"state": callback.state, "api_key": "attacker-key"}
        )

        assert first_status == 200
        assert replay_status == 409
        assert callback.wait_for_key(timeout=1) == "first-key"
    finally:
        callback.shutdown()


def test_incomplete_callback_body_is_bounded(monkeypatch):
    monkeypatch.setattr(router_setup, "CALLBACK_READ_TIMEOUT_SECONDS", 0.05)
    callback = start_router_key_callback(ROUTER_UI_URL)
    parsed = urllib.parse.urlparse(callback.redirect_uri)
    connection = socket.create_connection((parsed.hostname, parsed.port))
    try:
        connection.sendall(
            b"POST /callback HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Content-Type: application/x-www-form-urlencoded\r\n"
            b"Content-Length: 100\r\n\r\n"
        )

        with pytest.raises(click.ClickException, match="could not be read"):
            callback.wait_for_key(timeout=1)
    finally:
        connection.close()
        callback.shutdown()


def test_browser_disconnect_does_not_lose_an_accepted_key():
    callback = start_router_key_callback(ROUTER_UI_URL)
    parsed = urllib.parse.urlparse(callback.redirect_uri)
    body = urllib.parse.urlencode(
        {"state": callback.state, "api_key": "accepted-key"}
    ).encode()
    connection = socket.create_connection((parsed.hostname, parsed.port))
    try:
        connection.sendall(
            b"POST /callback HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Content-Type: application/x-www-form-urlencoded\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode()
            + body
        )
        connection.close()

        assert callback.wait_for_key(timeout=1) == "accepted-key"
    finally:
        connection.close()
        callback.shutdown()
