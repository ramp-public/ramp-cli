"""Home uses real local-day analytics, never a rolling window or guessed savings."""

import json
import time as system_time
from datetime import date, datetime, timezone
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


@pytest.mark.parametrize(
    ("link", "resolved", "timezone_file", "expected"),
    [
        (
            "/var/db/timezone/zoneinfo/America/Los_Angeles",
            "/usr/share/zoneinfo.default/America/Los_Angeles",
            None,
            "America/Los_Angeles",
        ),
        (
            None,
            "/usr/share/zoneinfo.default/America/Chicago",
            None,
            "America/Chicago",
        ),
        ("../usr/share/zoneinfo/Europe/London", "", None, "Europe/London"),
        (None, "/etc/localtime", "Asia/Tokyo\n", "Asia/Tokyo"),
    ],
)
def test_local_timezone_reads_macos_and_linux_layouts(
    monkeypatch, tmp_path, link, resolved, timezone_file, expected
):
    def readlink(path):
        assert path == operations.LOCALTIME_PATH
        if link is None:
            raise OSError("not a link")
        return link

    timezone_path = tmp_path / "timezone"
    if timezone_file is not None:
        timezone_path.write_text(timezone_file, encoding="utf-8")
    monkeypatch.delenv("TZ", raising=False)
    monkeypatch.setattr(operations.os, "readlink", readlink)
    monkeypatch.setattr(operations.os.path, "realpath", lambda path: resolved)
    monkeypatch.setattr(operations, "TIMEZONE_PATH", str(timezone_path))

    assert operations.local_timezone().key == expected


def test_local_timezone_accepts_a_zoneinfo_path_in_tz(monkeypatch):
    monkeypatch.setenv("TZ", ":/usr/share/zoneinfo/posix/America/Denver")
    assert operations.local_timezone().key == "America/Denver"


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


def _frozen_week(monkeypatch):
    zone = operations.ZoneInfo("America/New_York")
    monkeypatch.setattr(operations, "local_timezone", lambda: zone)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 3, 2, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr(operations, "datetime", FrozenDatetime)


def test_usage_reports_full_cost_where_shared_key_gpt_is_unbilled(monkeypatch):
    _frozen_week(monkeypatch)
    gpt = {"group": "gpt-5", "group_label": "GPT-5", "spend_usd": "0"}
    claude = {"group": "claude-sonnet", "group_label": "Claude Sonnet"}
    usage = {
        "summary": {
            "spend_usd": "1.5",
            "estimated_usage_cost_usd": "13.5",
            "unbilled_shared_key_cost_usd": "12",
        },
        "model_stats": [
            {
                "model": "claude-sonnet",
                "model_display_name": "Claude Sonnet",
                "spend_usd": "1.5",
                "estimated_usage_cost_usd": "1.5",
                "unbilled_shared_key_cost_usd": "0",
            },
            {
                "model": "gpt-5",
                "model_display_name": "GPT-5",
                "spend_usd": "0",
                "estimated_usage_cost_usd": "12",
                "unbilled_shared_key_cost_usd": "12",
            },
            # A model Router reports without an estimate keeps only spend.
            {"model": "legacy", "spend_usd": "0.1", "estimated_usage_cost_usd": None},
        ],
        "series": [
            {
                **gpt,
                "period_start_at": "2026-10-01T04:00:00Z",
                "estimated_usage_cost_usd": "5",
                "unbilled_shared_key_cost_usd": "5",
            },
            {
                **gpt,
                "period_start_at": "2026-10-02T04:00:00Z",
                "estimated_usage_cost_usd": "7",
                "unbilled_shared_key_cost_usd": "7",
            },
            {
                **claude,
                "period_start_at": "2026-10-02T04:00:00Z",
                "spend_usd": "1.5",
                "estimated_usage_cost_usd": "1.5",
                "unbilled_shared_key_cost_usd": "0",
            },
        ],
    }
    service = RouterService({})
    service._client = SimpleNamespace(get=lambda path, **kwargs: usage)
    data = service.daily_usage(7, "model")

    assert data["summary"] == usage["summary"]
    assert [
        (m["id"], m["spend_usd"], m.get("estimated_usage_cost_usd"))
        for m in data["models"]
    ] == [
        ("gpt-5", 0, Decimal("12")),
        ("claude-sonnet", Decimal("1.5"), Decimal("1.5")),
        ("legacy", Decimal("0.1"), None),
    ]
    assert data["models"][0]["unbilled_shared_key_cost_usd"] == Decimal("12")
    assert not set(operations.COST_FIELDS) & data["models"][2].keys()

    groups = {g["id"]: g for g in data["groups"]}
    assert [g["id"] for g in data["groups"]] == ["gpt-5", "claude-sonnet"]
    assert groups["gpt-5"]["spend_usd"] == 0
    assert groups["gpt-5"]["estimated_usage_cost_usd"] == Decimal("12")
    assert groups["gpt-5"]["unbilled_shared_key_cost_usd"] == Decimal("12")
    assert groups["claude-sonnet"]["estimated_usage_cost_usd"] == Decimal("1.5")

    days = {row["date"]: row for row in data["series"]}
    today = days["2026-10-02"]
    assert today["spend_usd"] == Decimal("1.5")
    assert today["estimated_usage_cost_usd"] == Decimal("8.5")
    assert today["unbilled_shared_key_cost_usd"] == Decimal("7")
    assert today["groups"]["gpt-5"]["estimated_usage_cost_usd"] == Decimal("7")
    assert today["groups"]["claude-sonnet"]["unbilled_shared_key_cost_usd"] == 0
    yesterday = days["2026-10-01"]
    assert yesterday["estimated_usage_cost_usd"] == Decimal("5")
    assert yesterday["groups"]["claude-sonnet"]["estimated_usage_cost_usd"] == 0
    # Days without usage are zero-filled like spend.
    idle = days["2026-09-26"]
    assert idle["estimated_usage_cost_usd"] == 0
    assert idle["unbilled_shared_key_cost_usd"] == 0
    assert idle["groups"]["gpt-5"]["estimated_usage_cost_usd"] == 0


def test_usage_without_cost_fields_leaves_them_out(monkeypatch):
    _frozen_week(monkeypatch)
    usage = {
        "summary": {"spend_usd": "2"},
        "model_stats": [{"model": "claude-sonnet", "spend_usd": "2"}],
        "series": [
            {
                "period_start_at": "2026-10-02T04:00:00Z",
                "group": "claude-sonnet",
                "spend_usd": "2",
            }
        ],
    }
    service = RouterService({})
    service._client = SimpleNamespace(get=lambda path, **kwargs: usage)
    data = service.daily_usage(7, "model")
    rows = [
        *data["models"],
        *data["groups"],
        *data["series"],
        *(cell for row in data["series"] for cell in row["groups"].values()),
    ]
    assert rows and not any(set(operations.COST_FIELDS) & row.keys() for row in rows)
    assert data["series"][-1]["spend_usd"] == Decimal("2")


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


def _harness_service(monkeypatch):
    zone = operations.ZoneInfo("America/New_York")
    monkeypatch.setattr(operations, "local_timezone", lambda: zone)
    calls = []

    def get(path, **kwargs):
        calls.append((path, kwargs["params"] if "params" in kwargs else None))
        if path == "/client/billing":
            return {"snapshot": {"remaining_credit_usd": "87.75"}}
        return {"summary": {"spend_usd": "1.5", "total_tokens": 9}, "series": []}

    service = RouterService({})
    service._client = SimpleNamespace(
        origin="https://router.invalid", status=lambda: {"authenticated": True}, get=get
    )
    return service, calls


def test_harness_usage_filters_the_local_day_to_one_harness(monkeypatch):
    service, calls = _harness_service(monkeypatch)
    usage = service.account(1, "model", "claude-code")["usage"]
    dashboard = next(p for path, p in calls if path == "/client/usage/dashboard")
    assert json.loads(dashboard["filters"]) == {"client": ["claude_code"]}
    assert dashboard["timezone"] == "America/New_York"
    assert [path for path, _ in calls] == ["/client/billing", "/client/usage/dashboard"]
    assert usage["harness"] == "claude-code"
    assert usage["summary"]["spend_usd"] == "1.5"
    assert "strategy_savings" not in usage


@pytest.mark.parametrize("harness", ["conductor", "cowork", "cursor"])
def test_usage_is_unavailable_rather_than_a_zero_without_a_request_tag(
    monkeypatch, harness
):
    service, calls = _harness_service(monkeypatch)
    result = service.account(1, "model", harness)
    assert "usage" not in result
    assert result["usage_error"] == "Usage analytics are unavailable."
    assert [path for path, _ in calls] == ["/client/billing"]


def test_usage_by_harness_answers_for_every_tagged_harness_in_one_read(monkeypatch):
    zone = operations.ZoneInfo("America/New_York")
    monkeypatch.setattr(operations, "local_timezone", lambda: zone)
    today = datetime.now(zone).replace(hour=12).astimezone(timezone.utc).isoformat()
    calls = []

    def get(path, **kwargs):
        calls.append((path, kwargs.get("params")))
        if path == "/client/billing":
            return {}
        point = {"period_start_at": today, "request_count": 2, "total_tokens": 10}
        return {
            "summary": {"spend_usd": "3.5"},
            "series": [
                {**point, "group": "claude_code", "spend_usd": "1.25"},
                {**point, "group": "codex", "spend_usd": "2"},
                {**point, "group": "claude_code", "spend_usd": "0.25"},
            ],
        }

    service = RouterService({})
    service._client = SimpleNamespace(
        origin="https://router.invalid", status=lambda: {"authenticated": True}, get=get
    )
    usage = service.account(1, "harness")["usage"]
    dashboard = [params for path, params in calls if path == "/client/usage/dashboard"]
    assert len(dashboard) == 1 and dashboard[0]["group_by"] == "client"
    totals = {row["harness"]: row for row in usage["harnesses"]}
    assert totals["claude-code"]["spend_usd"] == Decimal("1.5")
    assert totals["claude-code"]["request_count"] == 4
    assert totals["codex"]["spend_usd"] == Decimal("2")
    assert not set(operations.COST_FIELDS) & totals["codex"].keys()
    # Tagged but idle reads as zero; untagged harnesses are left out, not zeroed.
    assert totals["pi"]["spend_usd"] == 0 and totals["pi"]["total_tokens"] == 0
    assert not {"conductor", "cowork", "cursor"} & totals.keys()


def test_usage_by_harness_carries_full_cost_and_zero_fills_idle_harnesses():
    cost = {"estimated_usage_cost_usd": Decimal("9")}
    groups = [
        {"id": "codex", "spend_usd": Decimal(0), **cost, "request_count": 3},
        {"id": "claude_code", "spend_usd": Decimal("1"), **cost},
    ]
    totals = {row["harness"]: row for row in operations.harness_totals(groups)}
    assert totals["codex"]["estimated_usage_cost_usd"] == Decimal("9")
    assert totals["pi"]["estimated_usage_cost_usd"] == 0
    assert "unbilled_shared_key_cost_usd" not in totals["codex"]


def test_account_usage_without_a_harness_is_unchanged(monkeypatch):
    service, calls = _harness_service(monkeypatch)
    usage = service.account(1)["usage"]
    assert [path for path, _ in calls] == ["/client/billing", "/client/usage/dashboard"]
    assert "filters" not in calls[1][1]
    assert "harness" not in usage
    assert "strategy_savings" not in usage


def _account(billing, extra=None):
    return {
        "session": {"authenticated": True},
        "origin": "https://router.invalid",
        "billing": billing,
        "usage": {"days": 1, "summary": {}, "models": [], **(extra or {})},
    }


@pytest.mark.parametrize(
    ("billing", "expected"),
    [
        (
            {"snapshot": {"remaining_credit_usd": "87.75"}},
            ("$87.75", "87.75", "personal", False),
        ),
        (
            {
                "billed_to_business": True,
                "business_wallet": {"remaining_credit_usd": "1200"},
            },
            ("$1,200.00", "1200", "business", False),
        ),
        (
            {"ramp_employee_session_unlimited": True},
            ("Unlimited", None, "personal", True),
        ),
        (None, ("Unavailable", None, "personal", False)),
    ],
)
def test_account_usage_json_exposes_credits_as_data(monkeypatch, billing, expected):
    monkeypatch.setattr(RouterService, "account", lambda self, *a: _account(billing))
    result = CliRunner().invoke(cli, ["--agent", "router", "account", "usage"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"][0]
    assert (
        data["credits"],
        data["credits_usd"],
        data["credits_scope"],
        data["credits_unlimited"],
    ) == expected


def test_account_usage_harness_option_reaches_the_service(monkeypatch):
    seen = []

    def account(self, *args):
        seen.append(args)
        return _account(None, {"harness": "codex"})

    monkeypatch.setattr(RouterService, "account", account)
    result = CliRunner().invoke(
        cli,
        ["--agent", "router", "account", "usage", "--days", "1", "--harness", "Codex"],
    )
    assert result.exit_code == 0, result.output
    assert seen == [(1, "model", "codex", None)]
    assert json.loads(result.output)["data"][0]["harness"] == "codex"
    desktop = CliRunner().invoke(
        cli, ["router", "account", "usage", "--harness", "Desktop"]
    )
    assert "Invalid value" not in desktop.output
    assert seen[-1] == (None, "model", "cowork", None)
    bad = CliRunner().invoke(
        cli, ["router", "account", "usage", "--harness", "notepad"]
    )
    assert bad.exit_code == 2


def test_daily_usage_reads_long_windows_in_chunks_and_merges_them(monkeypatch):
    zone = operations.ZoneInfo("America/New_York")
    monkeypatch.setattr(operations, "local_timezone", lambda: zone)
    calls = []

    def get(path, **kwargs):
        start = datetime.fromisoformat(kwargs["params"]["start_at"])
        end = datetime.fromisoformat(kwargs["params"]["end_at"])
        calls.append((end - start).days)
        if len(calls) == 1:
            # The oldest chunk predates Router's usage summaries.
            raise operations.ApiError(503, '{"code": "analytics_not_ready"}')
        return {
            "summary": {"spend_usd": 2, "request_count": 4, "p95_latency_ms": 9},
            "model_stats": [{"model": "big", "spend_usd": "2", "request_count": 4}],
            "series": [
                {"period_start_at": start.isoformat(), "group": "big", "spend_usd": 2}
            ],
        }

    service = RouterService({})
    service._client = SimpleNamespace(get=get)
    data = service.daily_usage(200, "model", last=operations.date(2026, 10, 31))
    assert sorted(calls, reverse=True) == [92, 92, 16]
    assert len(data["series"]) == 200
    assert data["summary"] == {
        "spend_usd": 4,
        "request_count": 8,
        "average_cost_per_request": 0.5,
    }
    assert [(m["id"], m["spend_usd"]) for m in data["models"]] == [("big", Decimal(4))]
    assert sum(row["spend_usd"] for row in data["series"]) == 4


def test_merged_reads_keep_text_money_later_costs_and_the_same_groups():
    def part(day, groups, cost):
        return {
            "summary": {"spend_usd": "1.25", "request_count": 2},
            "model_stats": [
                {"model": "big", "spend_usd": "1", **cost},
                {"model": "new", "spend_usd": "0.25"},
            ],
            "series": [
                {"period_start_at": day, "group": group, "spend_usd": 1}
                for group in groups
            ],
        }

    merged = operations.merge_dashboards(
        [
            part("2026-07-01T04:00:00+00:00", ["big", "Other models"], {}),
            part(
                "2026-10-01T04:00:00+00:00",
                ["big", "new"],
                {"estimated_usage_cost_usd": "3"},
            ),
        ]
    )
    assert merged["summary"]["spend_usd"] == Decimal("2.5")
    assert merged["summary"]["average_cost_per_request"] == Decimal("0.625")
    big = next(m for m in merged["model_stats"] if m["model"] == "big")
    assert big["estimated_usage_cost_usd"] == Decimal(3)
    new = next(m for m in merged["model_stats"] if m["model"] == "new")
    assert "estimated_usage_cost_usd" not in new
    # "new" is named only in the later window, and the earlier one has an Other
    # that may hold it, so it is Other in both.
    assert [p["group"] for p in merged["series"]] == ["big", "Other models"] * 2
    # A window with no Other names all it had: a group missing there had no usage.
    idle = operations.merge_dashboards(
        [
            part("2026-07-01T04:00:00+00:00", [], {}),
            part("2026-10-01T04:00:00+00:00", ["big"], {}),
        ]
    )
    assert [p["group"] for p in idle["series"]] == ["big"]


def test_daily_series_forwards_input_and_cached_tokens():
    zone = timezone.utc
    dates = [date(2026, 11, 20), date(2026, 11, 21)]
    with_cache = operations.daily_series(
        {
            "series": [
                {
                    "period_start_at": "2026-11-20T00:00:00+00:00",
                    "group": "claude-opus-5-5",
                    "spend_usd": "2.50",
                    "request_count": 3,
                    "total_tokens": 1000,
                    "input_tokens": 800,
                    "cached_input_tokens": 600,
                },
                {
                    "period_start_at": "2026-11-20T00:00:00+00:00",
                    "group": "gpt-6.1",
                    "spend_usd": "1.00",
                    "request_count": 2,
                    "total_tokens": 500,
                    "input_tokens": 400,
                    "cached_input_tokens": 100,
                },
            ]
        },
        dates,
        zone,
    )
    day0, day1 = with_cache["series"]
    assert day0["input_tokens"] == 1200
    assert day0["cached_input_tokens"] == 700
    assert day0["groups"]["claude-opus-5-5"]["input_tokens"] == 800
    assert day0["groups"]["claude-opus-5-5"]["cached_input_tokens"] == 600
    assert day1["input_tokens"] == 0
    assert day1["cached_input_tokens"] == 0
    assert with_cache["groups"][0]["input_tokens"] == 800
    assert with_cache["groups"][0]["cached_input_tokens"] == 600

    without_cache = operations.daily_series(
        {
            "series": [
                {
                    "period_start_at": "2026-11-20T00:00:00+00:00",
                    "group": "claude-opus-5-5",
                    "spend_usd": "2.50",
                    "request_count": 3,
                    "total_tokens": 1000,
                }
            ]
        },
        dates,
        zone,
    )
    assert "input_tokens" not in without_cache["series"][0]
    assert "cached_input_tokens" not in without_cache["series"][0]
    assert "input_tokens" not in without_cache["groups"][0]
