#!/usr/bin/env python3
"""The SEQ-04 daemon. The only process in this repository that drives LinkedIn.

Run it and leave it running::

    python worker.py --account 1
    python worker.py --account 1 --status          # ask, do not run
    python worker.py --account 1 --once            # one tick, then exit

Why this file exists at all
--------------------------
Before this, an action happened because an MCP tool was called, which meant it
happened because a model decided to call it, which meant nothing happened
overnight and everything happened in bursts whenever somebody was talking to the
agent. That is the opposite of how safe outreach behaves.

Now the MCP server writes rows and this process reads them. It owns the clock, so
a step scheduled for Tuesday runs on Tuesday whether or not anyone is at the
keyboard. It owns Playwright, so no MCP tool needs a browser. It holds nothing in
memory that matters, so closing the editor, restarting the machine or killing this
process loses no work: the next start sweeps its own stale leases and carries on
from the database.

Executors
---------
The loop schedules; it does not know how to click anything. Browser work arrives
through ``--executors module:attribute``, where the attribute is a mapping of
`action_type` to coroutine, or a callable returning one. The browser those
executors use is built here and handed to them, which is what "the daemon owns
Playwright" means in practice.

MCP-03 (#26) made :data:`DEFAULT_EXECUTORS` the default. Until then the registry
was empty in every process, and an ad-hoc job failed with "no executor is
registered", which was honest but useless: the MCP tools were the only thing
that could act, and they acted inline where no cap could bind. Now
``linkedin_mcp.executors.linkedin`` supplies a coroutine per LinkedIn action and
this daemon is the only process that performs one. Pass ``--no-default-executors``
to get the old empty registry back, which is what a scheduling-only test wants.

Even with the default table this is still a working daemon on a machine with no
browser: filters resolve, campaigns advance through their local steps, everything
that needs the browser fails its step and lands under the step's own `on_failure`
policy, and the heartbeat keeps telling the truth. That is deliberate. A
scheduler that only runs when a browser and a model are both present is a
scheduler nobody can trust.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import logging
import signal
import sqlite3
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from linkedin_mcp.audit import AuditLog, set_audit_log
from linkedin_mcp.core.db import DEFAULT_DB_PATH
from linkedin_mcp.worker import (
    DEFAULT_STALLED_AFTER_SECONDS,
    Executor,
    Worker,
    WorkerConfig,
    worker_status,
)
from linkedin_mcp.worker.actions import ActionRegistry

logger = logging.getLogger("linkedin_mcp.worker")

__all__ = [
    "DEFAULT_EXECUTORS",
    "build_browser_supplier",
    "default_executors",
    "load_executors",
    "main",
    "open_database",
    "parse_args",
    "run",
]

DEFAULT_EXECUTORS = "linkedin_mcp.executors.linkedin:linkedin_executors"
"""The executor table every daemon registers unless told otherwise.

A `module:attribute` string rather than a direct import so the browser package
is not pulled in by ``--status``, which must work on a machine with no
Playwright installed.
"""


def default_executors() -> dict[str, Executor]:
    """Return the built-in LinkedIn executors, importing them on demand."""
    return load_executors([DEFAULT_EXECUTORS])


def open_database(db_path: str | Path) -> sqlite3.Connection:
    """Open one connection and make it the process-wide audit log's connection too.

    One connection rather than two on purpose. The safety gate reads its
    consistent snapshot from the audit log's connection and refuses outright if
    another writer has a transaction in flight, so a second connection would turn
    every overlapping write into a refusal to run instead of a wait. Sharing one
    also means the runner can never deadlock against itself.
    """
    log = AuditLog.open(db_path)
    set_audit_log(log)
    return log.connection


def load_executors(specs: list[str]) -> dict[str, Executor]:
    """Import ``module:attribute`` executor tables and merge them.

    The attribute may be a mapping, or a callable returning one. Anything else is
    a configuration error and says so rather than starting a daemon that will
    quietly fail every step.
    """
    executors: dict[str, Executor] = {}
    for spec in specs:
        module_name, _, attribute = spec.partition(":")
        if not module_name or not attribute:
            raise ValueError(
                f"executor spec {spec!r} is not in module:attribute form"
            )
        module = importlib.import_module(module_name)
        table = getattr(module, attribute)
        if callable(table) and not isinstance(table, Mapping):
            table = table()
        if not isinstance(table, Mapping):
            raise TypeError(
                f"{spec} is a {type(table).__name__}, not a mapping of "
                "action_type to executor"
            )
        executors.update(table)
    return executors


def build_browser_supplier(
    *,
    headless: bool,
    account_seed: str | None,
    enabled: bool,
):
    """Return the coroutine the worker calls to get its browser.

    Called at most once per process, and only when a step actually needs it, so a
    night of filters and delays never launches Chromium. The import is inside the
    function because Playwright is a heavy dependency and a `--no-browser` run
    should not need it installed.
    """

    async def supplier() -> Any | None:
        if not enabled:
            return None
        from linkedin_mcp.browser.session import BrowserSession

        session = BrowserSession(headless=headless, account_seed=account_seed)
        return await session.__aenter__()

    return supplier


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="worker.py",
        description="Run the LinkedIn campaign worker, or report on one.",
    )
    parser.add_argument(
        "--account",
        type=int,
        required=True,
        help="accounts.id this worker runs as",
    )
    parser.add_argument(
        "--db",
        default=str(DEFAULT_DB_PATH),
        help="path to the SQLite database (default: %(default)s)",
    )
    parser.add_argument(
        "--worker-id",
        default="",
        help="stable id for this worker; generated when omitted",
    )
    parser.add_argument("--tick-seconds", type=float, default=30.0)
    parser.add_argument("--campaign-actions-per-tick", type=int, default=10)
    parser.add_argument("--ad-hoc-actions-per-tick", type=int, default=5)
    parser.add_argument("--sweep-every-ticks", type=int, default=10)
    parser.add_argument(
        "--stalled-after-seconds",
        type=int,
        default=DEFAULT_STALLED_AFTER_SECONDS,
        help="how old a heartbeat may get before --status calls it stalled",
    )
    parser.add_argument(
        "--campaign",
        type=int,
        default=None,
        help="restrict this worker to one campaign",
    )
    parser.add_argument(
        "--executors",
        action="append",
        default=[],
        metavar="MODULE:ATTRIBUTE",
        help="import an action_type -> coroutine mapping; may be repeated",
    )
    parser.add_argument(
        "--no-default-executors",
        action="store_true",
        help=(
            "start with an empty registry instead of the built-in LinkedIn "
            "executors; every action then fails as unregistered"
        ),
    )
    parser.add_argument("--headless", action="store_true")
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="never launch Playwright; executors receive None",
    )
    parser.add_argument("--once", action="store_true", help="run one tick and exit")
    parser.add_argument("--max-ticks", type=int, default=None)
    parser.add_argument(
        "--status",
        action="store_true",
        help="print worker_status as JSON and exit without running anything",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser.parse_args(argv)


async def run(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    """Build the worker, wire the signals, and tick until told to stop."""
    specs = list(args.executors)
    if not getattr(args, "no_default_executors", False):
        # Ahead of anything given on the command line, so an operator who names
        # their own table for one action type replaces the built-in rather than
        # being silently overridden by it.
        specs.insert(0, DEFAULT_EXECUTORS)
    executors = load_executors(specs)
    if not executors:
        logger.warning(
            "no executors registered: local steps will run and every step that "
            "needs the browser will fail under its own on_failure policy"
        )
    else:
        logger.info(
            "registered %d executor(s): %s",
            len(executors),
            ", ".join(sorted(executors)),
        )

    # Derive the browser profile seed from the account's label (the email /
    # username stored at account creation time), falling back to the env var and
    # then to the string account-id only as a last resort. Using the integer id
    # would land in a different profile directory than the login tool, which
    # resolves its seed from LINKEDIN_USERNAME, leaving the worker with an empty
    # profile and no session on every start.
    account_row = conn.execute(
        "SELECT label FROM accounts WHERE id = ?", (int(args.account),)
    ).fetchone()
    account_label = None
    if account_row is not None:
        raw = account_row[0] if isinstance(account_row, tuple) else account_row["label"]
        account_label = (raw or "").strip() or None
    account_seed = account_label or os.getenv("LINKEDIN_USERNAME", "").strip() or str(args.account)

    worker = Worker(
        conn,
        WorkerConfig(
            account_id=args.account,
            worker_id=args.worker_id,
            campaign_actions_per_tick=args.campaign_actions_per_tick,
            ad_hoc_actions_per_tick=args.ad_hoc_actions_per_tick,
            stalled_after_seconds=args.stalled_after_seconds,
            tick_seconds=args.tick_seconds,
            sweep_every_ticks=args.sweep_every_ticks,
            campaign_id=args.campaign,
        ),
        registry=ActionRegistry(executors),
        browser_supplier=build_browser_supplier(
            headless=args.headless,
            account_seed=account_seed,
            enabled=not args.no_browser,
        ),
    )

    loop = asyncio.get_running_loop()
    for name in ("SIGINT", "SIGTERM"):
        handler = getattr(signal, name, None)
        if handler is None:
            continue
        try:
            loop.add_signal_handler(handler, worker.request_stop)
        except NotImplementedError:
            # Windows has no add_signal_handler for the proactor loop. Ctrl-C
            # still raises KeyboardInterrupt, which the caller turns into a clean
            # stop, so this is a downgrade rather than a failure.
            signal.signal(handler, lambda *_: worker.request_stop())

    max_ticks = 1 if args.once else args.max_ticks
    logger.info(
        "worker %s starting for account %s against %s",
        worker.worker_id,
        args.account,
        args.db,
    )
    try:
        reports = await worker.run_forever(max_ticks=max_ticks)
    except KeyboardInterrupt:
        worker.stop()
        return 130
    logger.info(
        "worker %s stopped after %d ticks and %d jobs",
        worker.worker_id,
        len(reports),
        sum(report.executed for report in reports),
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    conn = open_database(args.db)
    try:
        if args.status:
            print(
                json.dumps(
                    worker_status(
                        conn,
                        account_id=args.account,
                        stalled_after_seconds=args.stalled_after_seconds,
                    ),
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        return asyncio.run(run(args, conn))
    finally:
        conn.close()


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
