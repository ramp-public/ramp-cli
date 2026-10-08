"""Router reads are reused for a TTL so switching tabs doesn't refetch them."""

import asyncio
import runpy
import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from ramp_cli.router_ui import cache as read_cache
from ramp_cli.router_ui import service as operations
from ramp_cli.router_ui.app import RouterApp
from ramp_cli.router_ui.service import RouterService

# Shared UI doubles, found next to this file wherever pytest runs from.
TEST_UI = str(Path(__file__).with_name("test_router_ui.py"))


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(read_cache, "monotonic", lambda: now[0])
    return now


def service_with(get, **client):
    service = RouterService({})
    service.client = SimpleNamespace(
        origin="https://router.invalid",
        status=lambda: {"authenticated": True},
        get=get,
        **client,
    )
    return service


def test_reads_are_reused_until_the_ttl_passes(clock):
    calls = []

    def get(path, **kwargs):
        calls.append(path)
        return {"snapshot": {"remaining_credit_usd": str(len(calls))}}

    service = service_with(get)
    assert service.cache_ttl == 60
    first = service.billing()
    clock[0] += 59
    assert service.billing() == first
    assert calls == ["/client/billing"]
    clock[0] += 1
    assert service.billing()["snapshot"]["remaining_credit_usd"] == "2"
    assert len(calls) == 2


def test_arguments_key_the_cache_and_defaults_match(clock, monkeypatch):
    zone = operations.ZoneInfo("UTC")
    monkeypatch.setattr(operations, "local_timezone", lambda: zone)
    calls = []

    def get(path, **kwargs):
        calls.append(kwargs["params"]["group_by"])
        return {}

    service = service_with(get)
    service.daily_usage()
    service.daily_usage(7, "model")
    service.daily_usage(days=7, group_by="model")
    assert calls == ["model"]
    service.daily_usage(7, "key")
    service.daily_usage(3)
    assert calls == ["model", "key", "model"]


def test_callers_cannot_edit_the_cached_copy(clock):
    service = service_with(lambda path, **kwargs: {"snapshot": {"value": 1}})
    service.billing()["snapshot"]["value"] = 2
    assert service.billing()["snapshot"]["value"] == 1


def test_failures_are_not_cached(clock):
    calls = []

    def get(path, **kwargs):
        calls.append(path)
        if len(calls) == 1:
            raise httpx.ReadTimeout("offline")
        return {"snapshot": {}}

    service = service_with(get)
    with pytest.raises(httpx.ReadTimeout):
        service.billing()
    assert service.billing() == {"snapshot": {}}
    assert len(calls) == 2


def test_writes_and_invalidate_clear_every_read(clock):
    calls = []

    def get(path, **kwargs):
        calls.append(path)
        return {}

    service = service_with(get, logout=lambda: None)
    service.billing()
    service.logout()
    service.billing()
    service.invalidate()
    service.billing()
    assert len(calls) == 3


def test_a_read_that_overlaps_a_write_is_not_kept(clock):
    service = None
    calls = []

    def get(path, **kwargs):
        calls.append(path)
        if len(calls) == 1:
            # A write lands while the first read is still in flight.
            service.invalidate()
        return {}

    service = service_with(get)
    service.billing()
    service.billing()
    assert len(calls) == 2


def test_reload_asks_the_service_for_fresh_data():
    test_ui = runpy.run_path(TEST_UI)
    service = test_ui["FakeService"]()

    async def run():
        app = RouterApp(service, page="keys")
        async with app.run_test(size=(100, 35)) as pilot:
            await test_ui["settle"](app, pilot)
            assert service.invalidations == 0
            await pilot.press("r")
            await test_ui["settle"](app, pilot)
            assert service.invalidations == 1

    asyncio.run(run())


@pytest.fixture
def joined(monkeypatch):
    """Set once a second caller waits on the first caller's request."""
    event = threading.Event()

    class Flight(read_cache.Future):
        def result(self, timeout=None):
            event.set()
            return super().result(timeout)

    monkeypatch.setattr(read_cache, "Future", Flight)
    return event


def run_both(read, release, joined):
    first = threading.Thread(target=read)
    first.start()
    second = threading.Thread(target=read)
    second.start()
    assert joined.wait(5), "The second read never joined the first"
    release.set()
    for thread in (first, second):
        thread.join(5)
        assert not thread.is_alive()


def test_concurrent_reads_share_one_request(clock, joined):
    calls = []
    release = threading.Event()

    def get(path, **kwargs):
        calls.append(path)
        release.wait(5)
        return {"snapshot": {"value": 1}}

    service = service_with(get)
    results = []
    run_both(lambda: results.append(service.billing()), release, joined)
    assert calls == ["/client/billing"]
    assert results == [{"snapshot": {"value": 1}}] * 2
    # Each caller still owns its copy.
    assert results[0] is not results[1]


def test_a_shared_failure_reaches_every_caller_and_is_not_kept(clock, joined):
    calls = []
    release = threading.Event()

    def get(path, **kwargs):
        calls.append(path)
        if len(calls) == 1:
            release.wait(5)
            raise httpx.ReadTimeout("offline")
        return {}

    service = service_with(get)
    errors = []

    def read():
        try:
            service.billing()
        except httpx.ReadTimeout as error:
            errors.append(error)

    run_both(read, release, joined)
    assert len(errors) == 2
    assert service.billing() == {}
    assert len(calls) == 2


def finish_prefetches():
    for thread in threading.enumerate():
        if thread.name == "router-prefetch":
            thread.join(5)
            assert not thread.is_alive()


def test_prefetch_warms_reads_so_a_tab_sends_nothing(clock):
    calls = []
    service = service_with(lambda path, **kwargs: calls.append(path) or {})
    service.cache.prefetch(service.billing)
    finish_prefetches()
    assert calls == ["/client/billing"]
    service.billing()
    assert calls == ["/client/billing"]


def test_prefetch_never_raises_and_failures_load_normally(clock):
    calls = []

    def get(path, **kwargs):
        calls.append(path)
        if len(calls) == 1:
            raise httpx.ConnectError("offline")
        return {}

    service = service_with(get)
    service.cache.prefetch(service.billing)
    finish_prefetches()
    assert service.billing() == {}
    assert len(calls) == 2


def test_prefetch_tabs_warms_harnesses_keys_and_strategies(monkeypatch):
    service = RouterService({})
    warmed = []
    monkeypatch.setattr(service.cache, "prefetch", lambda *reads: warmed.extend(reads))
    service.prefetch_tabs()
    assert [read.__name__ for read in warmed] == [
        "harnesses",
        "keys",
        "key_grants",
        "profiles",
        "experiment_settings",
    ]


def test_auto_routing_default_is_cached_but_failures_are_not(clock):
    calls = []

    def get(path, **kwargs):
        calls.append(path)
        if len(calls) == 1:
            raise httpx.ReadTimeout("offline")
        return {"auto_routing_enabled": True}

    service = service_with(get)
    assert service.auto_routing_default() is None
    assert service.auto_routing_default() is True
    assert service.auto_routing_default() is True
    assert calls == ["/admin/me/experiment-settings"] * 2


def test_home_warms_the_other_tabs_only_after_its_own_data():
    test_ui = runpy.run_path(TEST_UI)
    service = test_ui["FakeService"]()

    def account(days=None, group_by="model"):
        service.calls.append(("account",))
        return {
            "session": {"authenticated": True, "email": "owner@example.com"},
            "origin": service.client.origin,
        }

    service.account = account

    async def run():
        app = RouterApp(service, page="home")
        async with app.run_test(size=(100, 35)) as pilot:
            await test_ui["settle"](app, pilot)

    asyncio.run(run())
    assert ("prefetch",) in service.calls
    assert service.calls.index(("account",)) < service.calls.index(("prefetch",))


def test_signed_out_home_warms_nothing():
    test_ui = runpy.run_path(TEST_UI)
    service = test_ui["FakeService"]()

    async def run():
        app = RouterApp(service, page="home")
        async with app.run_test(size=(100, 35)) as pilot:
            await test_ui["settle"](app, pilot)

    asyncio.run(run())
    assert ("prefetch",) not in service.calls


def test_close_stops_prefetching_and_closes_the_connection_pool():
    service = RouterService({})
    closed = []
    service.client = SimpleNamespace(
        origin="https://router.invalid", close=lambda: closed.append("pool")
    )
    service.close()
    assert closed == ["pool"]
    ran = []
    service.cache.prefetch(lambda: ran.append("read"))
    finish_prefetches()
    assert ran == []


def test_switching_routers_closes_the_previous_connection_pool():
    service = RouterService({})
    closed = []
    first = SimpleNamespace(
        origin="https://one.invalid", close=lambda: closed.append(1)
    )
    service.client = first
    service.client = first
    assert closed == []
    service.client = SimpleNamespace(origin="https://two.invalid", close=lambda: None)
    assert closed == [1]
