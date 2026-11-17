"""`ramp router app snapshot`: one menu bar refresh, read in one process and streamed.

The Mac app used to start a CLI process for every read in a refresh, a dozen or more at once.
This reads them concurrently through one `RouterService` (one sign-in, one connection pool,
shared billing and usage reads) and prints each answer as a JSON line the moment it is ready,
so the app shows it at once instead of waiting for the slowest read.

Each line is `{"section": ..., <its key>, "data": <what the single command prints>}`, or
`"error": {"message": ...}` in place of `data`. The last line is `{"section": "done"}`.
"""

from __future__ import annotations

import json
import sys
from concurrent.futures import Future, ThreadPoolExecutor, wait
from threading import Lock

import click

# Enough for one refresh's network reads to run side by side, few enough to stay polite.
_WORKERS = 8
_write = Lock()


def _emit(
    section: str, *, data: object = None, error: str | None = None, **key: object
) -> None:
    line: dict[str, object] = {"section": section, **key}
    if error is not None:
        line["error"] = {"message": error}
    elif section != "done":
        line["data"] = data
    with _write:
        sys.stdout.write(json.dumps(line, default=str) + "\n")
        sys.stdout.flush()


def _message(error: BaseException) -> str:
    return getattr(error, "message", None) or str(error) or type(error).__name__


def _read(section: str, read, *args: object, **key: object) -> None:
    try:
        data = read(*args)
    except Exception as error:  # noqa: BLE001 - each section reports its own failure
        _emit(section, error=_message(error), **key)
    else:
        _emit(section, data=data, **key)


def register(group: click.Group) -> None:
    @group.group("app", hidden=True, help="Reads for the Ramp Router Mac app.")
    def app_group():
        pass

    @app_group.command(
        "snapshot",
        help="Stream one refresh's reads as JSON lines, each as soon as it is ready.",
    )
    @click.option(
        "--usage-days",
        "usage_days",
        type=click.IntRange(1),
        multiple=True,
        help="Account usage windows to read. Default: 1.",
    )
    @click.option(
        "--totals-days",
        "totals_days",
        type=click.IntRange(1),
        multiple=True,
        help="Windows to read every harness's totals for (usage --by harness).",
    )
    @click.option(
        "--harness-days",
        "harness_days",
        type=click.IntRange(1),
        multiple=True,
        help="Windows to read each installed or connected harness's own usage for.",
    )
    @click.option(
        "--workloads",
        "with_workloads",
        is_flag=True,
        help="Read the coding workloads of each connected harness that can pick one.",
    )
    @click.option(
        "--workload-details",
        "with_details",
        is_flag=True,
        help="With --workloads, also read each listed workload's settings.",
    )
    @click.pass_context
    def snapshot(
        ctx, usage_days, totals_days, harness_days, with_workloads, with_details
    ):
        # These import router commands, so they can't load with this module.
        from ramp_cli.commands.router import (  # noqa: PLC0415
            _same_router,
            router_base_url_for,
        )
        from ramp_cli.commands.router_user import (  # noqa: PLC0415
            _CODING_ROUTING_CLIENTS,
            _WORKLOADS_PATH,
            _harness_workloads,
            usage_payload,
        )
        from ramp_cli.router_ui.harness import connected, summaries  # noqa: PLC0415
        from ramp_cli.router_ui.service import RouterService  # noqa: PLC0415

        service = RouterService(dict(ctx.obj))
        # Local reads first: the app can draw its rows before any request returns.
        session = service.client.status()
        _emit("status", data=session)
        try:
            harnesses = summaries()
        except Exception as error:  # noqa: BLE001 - reported like any other section
            harnesses = []
            _emit("harnesses", error=_message(error))
        else:
            _emit("harnesses", data=harnesses)
        if not session.get("authenticated"):
            _emit("done")
            return
        if session.get("access_token_expired"):
            # Renew once here rather than in every concurrent read.
            try:
                service.client.access_token()
            except Exception:  # noqa: BLE001 - the reads below report the failure
                pass

        pool = ThreadPoolExecutor(max_workers=_WORKERS)
        futures: list[Future] = []
        lock = Lock()
        asked: set[str] = set()
        company: dict[str, object] = {}

        def submit(read, *args, **key) -> None:
            with lock:
                futures.append(pool.submit(read, *args, **key))

        def usage(section: str, days: int, group_by: str, harness: str | None = None):
            key: dict[str, object] = {"days": days}
            if harness is not None:
                key["harness"] = harness
            submit(
                _read,
                section,
                lambda: usage_payload(service, days, group_by, harness),
                **key,
            )

        def company_workload(workload_id: str) -> dict:
            # One list read answers every company workload; members can't read them one by one.
            with lock:
                listing = company.get("list")
                if listing is None:
                    listing = company["list"] = Future()
                    owner = True
                else:
                    owner = False
            if owner:
                try:
                    listing.set_result(service.client.get("/client/coding-workloads"))
                except Exception as error:  # noqa: BLE001 - every waiter reports it
                    listing.set_exception(error)
            for workload in listing.result().get("data") or []:
                if str(workload.get("id")) == workload_id:
                    return workload
            raise click.ClickException(f"No company workload {workload_id}.")

        # The data plane of the Router the details are read through: the login's
        # own deployment, never the API-key default an environment may point at.
        login_router = router_base_url_for(service.client.origin)

        def same_router(harness: str) -> bool:
            # Options come from the harness's own Router and key; details are read
            # with the CLI's Router login, so only when that is the same deployment
            # (the legacy production URL counts as production).
            try:
                return _same_router(connected(harness).base_url, login_router)
            except Exception:  # noqa: BLE001 - no connection: no details either
                return False

        def workloads(harness: str) -> None:
            try:
                routing = _harness_workloads(harness)
            except Exception as error:  # noqa: BLE001
                _emit("workloads", error=_message(error), harness=harness)
                return
            _emit("workloads", data=routing, harness=harness)
            if not with_details:
                return
            matched = same_router(harness)
            for option in routing.get("options") or []:
                workload_id, kind = str(option.get("id") or ""), option.get("kind")
                if not workload_id or kind not in ("workload", "company_workload"):
                    continue
                with lock:
                    if workload_id in asked:
                        continue
                    asked.add(workload_id)
                if not matched:
                    _emit(
                        "workload",
                        error="This harness uses a different Router than your Router login.",
                        id=workload_id,
                    )
                    continue
                if kind == "company_workload":
                    submit(
                        _read, "workload", company_workload, workload_id, id=workload_id
                    )
                else:
                    path = f"{_WORKLOADS_PATH}/{workload_id}"
                    submit(_read, "workload", service.client.get, path, id=workload_id)

        # Submitted in the order the menu shows them, so the first rows land first.
        for d in dict.fromkeys(usage_days or (1,)):
            usage("usage", d, "model")
        for d in dict.fromkeys(totals_days):
            usage("harness_totals", d, "harness")
        if with_workloads:
            for row in harnesses:
                if (
                    row.get("connected")
                    and row.get("harness") in _CODING_ROUTING_CLIENTS
                ):
                    submit(workloads, row["harness"])
        for d in dict.fromkeys(harness_days):
            for row in harnesses:
                if row.get("installed") or row.get("connected"):
                    usage("harness_usage", d, "model", row["harness"])
        # Workload reads add their details as they finish, so wait until nothing is left.
        while True:
            with lock:
                running = [future for future in futures if not future.done()]
            if not running:
                break
            wait(running)
        pool.shutdown()
        _emit("done")
