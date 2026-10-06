"""Home uses real local-day analytics, never a rolling window or guessed savings."""

import time as system_time
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from click.testing import CliRunner

from ramp_cli.main import cli
from ramp_cli.router_ui import service as operations
from ramp_cli.router_ui.service import RouterService


@pytest.mark.parametrize("unavailable", [False, True])
def test_home_usage_is_self_scoped_and_does_not_hide_credits_on_failure(
    monkeypatch, unavailable
):
    zone = operations.ZoneInfo("America/New_York")
    monkeypatch.setattr(operations, "local_timezone", lambda: zone)
    calls = []

    def get(path, **kwargs):
        calls.append((path, kwargs))
        if path == "/client/billing":
            return {"snapshot": {"remaining_credit_usd": "87.75"}}
        assert path == "/client/usage/dashboard"
        assert kwargs["params"]["group_by"] == "key"
        assert kwargs["params"]["include_summary_stats"] == "true"
        if unavailable:
            raise httpx.ReadTimeout("Analytics offline")
        return {
            "summary": {
                "spend_usd": "4.121",
                "cost_savings_usd": "7.5399",
                "byok_spend_usd": "0.5",
            },
            "model_stats": [
                {"model": "small", "spend_usd": "0.1", "request_count": 4},
                {
                    "model": "big",
                    "model_display_name": "Big Model",
                    "spend_usd": "4",
                    "request_count": "2",
                },
            ],
            "series": [{"input_tokens": 100, "cached_input_tokens": 40}],
        }

    service = RouterService({})
    service._client = SimpleNamespace(
        origin="https://router.invalid", status=lambda: {"authenticated": True}, get=get
    )
    data = service.account(3, "key")
    assert data["billing"]["snapshot"]["remaining_credit_usd"] == "87.75"
    assert len(calls) == 2
    if unavailable:
        assert "usage" not in data
        assert data["usage_error"]
    else:
        usage = data["usage"]
        assert usage["days"] == 3
        assert usage["summary"]["cost_savings_usd"] == "7.5399"
        assert usage["byok_spend_usd"] == Decimal("0.5")
        assert (usage["input_tokens"], usage["cached_input_tokens"]) == (100, 40)
        assert [(m["label"], m["request_count"]) for m in usage["models"]] == [
            ("Big Model", 2),
            ("small", 4),
        ]


def test_signed_out_home_does_not_request_private_analytics():
    service = RouterService({})
    service._client = SimpleNamespace(
        origin="https://router.invalid",
        status=lambda: {"authenticated": False},
        get=lambda *args, **kwargs: pytest.fail(
            "signed-out Home must not request analytics"
        ),
    )
    assert "usage" not in service.account()


@pytest.mark.skipif(
    not hasattr(system_time, "tzset"), reason="requires a process-local timezone"
)
def test_local_midnight_keeps_its_own_dst_offset(monkeypatch):
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 3, 8, 16, tzinfo=timezone.utc).astimezone(tz)

    try:
        with monkeypatch.context() as patch:
            patch.setenv("TZ", "America/New_York")
            system_time.tzset()
            patch.setattr(operations, "datetime", FrozenDatetime)
            zone = operations.local_timezone()
            assert zone.key == "America/New_York"
            start, end, dates = operations.local_days_window(1, zone)
            assert start == datetime(2026, 3, 8, 5, tzinfo=timezone.utc)
            # The day is 23 hours long, so its end uses the post-DST offset.
            assert end == datetime(2026, 3, 9, 4, tzinfo=timezone.utc)
            assert [day.isoformat() for day in dates] == ["2026-03-08"]
    finally:
        system_time.tzset()


def test_daily_usage_zero_fills_local_days_and_orders_other_last(monkeypatch):
    zone = operations.ZoneInfo("America/New_York")
    monkeypatch.setattr(operations, "local_timezone", lambda: zone)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 3, 2, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr(operations, "datetime", FrozenDatetime)
    calls = []

    def get(path, **kwargs):
        calls.append((path, kwargs["params"]))
        return {
            "summary": {"spend_usd": 6},
            "series": [
                {
                    "period_start_at": "2026-09-30T04:00:00Z",
                    "group": "__other_keys__",
                    "group_label": "Other API keys",
                    "spend_usd": 5,
                    "request_count": 9,
                },
                {
                    "period_start_at": "2026-10-02T04:00:00+00:00",
                    "group": "key-1",
                    "group_label": "Coding",
                    "spend_usd": "0.75",
                    "request_count": 2,
                    "total_tokens": 40,
                },
                {
                    "period_start_at": "2026-10-02T04:00:00+00:00",
                    "group": "key-1",
                    "group_label": "Coding",
                    "spend_usd": "0.25",
                },
            ],
        }

    service = RouterService({})
    service._client = SimpleNamespace(get=get)
    data = service.daily_usage(7, "key")
    assert calls == [
        (
            "/client/usage/dashboard",
            {
                "start_at": "2026-09-26T04:00:00+00:00",
                "end_at": "2026-10-03T04:00:00+00:00",
                "group_by": "key",
                "timezone": "America/New_York",
                "include_summary_stats": "true",
            },
        )
    ]
    assert [day["date"] for day in data["series"]] == [
        f"2026-09-{day}" for day in range(26, 31)
    ] + ["2026-10-01", "2026-10-02"]
    assert [group["id"] for group in data["groups"]] == ["key-1", "__other_keys__"]
    assert data["series"][-1]["groups"]["key-1"] == {
        "spend_usd": Decimal("1.00"),
        "request_count": 2,
        "total_tokens": 40,
    }
    assert data["series"][0]["spend_usd"] == 0
    assert data["series"][4]["spend_usd"] == 5


@pytest.mark.parametrize(
    ("account", "message"),
    [
        ({"session": {"authenticated": False}}, "ramp router login"),
        (
            {"session": {"authenticated": True}, "usage_error": "Usage down."},
            "Usage down.",
        ),
    ],
)
def test_account_usage_fails_instead_of_printing_an_empty_dashboard(
    monkeypatch, account, message
):
    monkeypatch.setattr(RouterService, "account", lambda self, *a: account)
    result = CliRunner().invoke(cli, ["--agent", "router", "account", "usage"])
    assert result.exit_code != 0
    assert message in str(result.exception)
