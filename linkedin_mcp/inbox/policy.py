"""Poll policy and delta state for the inbox scanner.

There is no timer in this package
---------------------------------
SEQ-04 (#22) owns the tick loop, so a "configurable poll interval" here is a
policy value plus a due check, not a scheduler. A runner asks
:func:`scan_due` whether enough time has passed and, if it has, calls
:func:`linkedin_mcp.inbox.scan.run_inbox_scan`. Nothing here sleeps, spawns a
thread or holds a loop.

The floor is a floor
--------------------
One hour is the minimum and three hours is the default. A caller asking for five
minutes is clamped to the floor by default, or refused outright with
:class:`~linkedin_mcp.inbox.errors.PollIntervalTooShortError` when it opts into
strict handling. Reading LinkedIn's inbox every five minutes is exactly the
pattern that gets an account challenged, so neither path lets the request
through as written.

Where the state lives
---------------------
No new table. `harvest_runs` is already the bookkeeping row for one extraction
run, already carries a resumable cursor, and already belongs to an account, so
an inbox scan gets a row there with `source_type = 'inbox_scan'`. Its
`params_json` carries three things the next run needs: the poll interval the
operator configured, the watermark of threads already archived, and the counts
that make the delta path auditable. `finished_at` is the last scan time
:func:`scan_due` measures against.

A run the gate refused still records a finish time. That is deliberate: the gate
refusing is the account asking to be left alone, and retrying in five minutes
because "nothing was read" would argue with it.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from linkedin_mcp.inbox.errors import PollIntervalTooShortError
from linkedin_mcp.scrape.paginate import SearchCursor
from linkedin_mcp.sequences.transaction import now_timestamp, shift_timestamp

logger = logging.getLogger(__name__)

__all__ = [
    "DELTA_THREAD_LIMIT",
    "DEFAULT_POLL_SECONDS",
    "FIRST_RUN_THREAD_LIMIT",
    "INBOX_SOURCE",
    "MIN_POLL_SECONDS",
    "WATERMARK_LIMIT",
    "InboxScanState",
    "next_scan_at",
    "read_scan_state",
    "resolve_poll_seconds",
    "scan_due",
    "thread_key",
    "trim_watermark",
]

MIN_POLL_SECONDS = 3600
"""Fastest the inbox may be polled, in seconds. One hour, and it is a floor."""

DEFAULT_POLL_SECONDS = 3 * 3600
"""Interval used when nobody has configured one. Three hours."""

FIRST_RUN_THREAD_LIMIT = 200
"""Threads the first run walks back through. Every later run is a delta."""

DELTA_THREAD_LIMIT = 25
"""Threads a delta run will look at before it stops.

A delta normally stops on its own after one slice, because every thread it sees
is already in the watermark. This is the ceiling for the case where a lot has
changed since the last scan, so a backlog does not turn into a 200 thread walk
every three hours.
"""

WATERMARK_LIMIT = 500
"""Threads remembered between runs, most recently seen first.

Bounded so the run row cannot grow without limit. Anything evicted is simply
re-read once, which costs a slice and never costs a duplicate `messages` row,
because archiving deduplicates against the database rather than against this.
"""

INBOX_SOURCE = "inbox_scan"
"""`harvest_runs.source_type` for an inbox scan."""


def thread_key(thread_urn: str, signature: str) -> str:
    """Return the dedupe key one thread presents to the paged loop.

    The key carries a change signature as well as the thread's identity, so a
    thread with a new message is a new key and is walked again, while an
    untouched thread reads as a duplicate and stops the delta run.
    """
    return f"{thread_urn}|{signature}"


def resolve_poll_seconds(
    requested: int | float | None,
    *,
    strict: bool = False,
) -> int:
    """Return a legal poll interval, clamping or refusing anything below the floor.

    Args:
        requested: Interval the caller wants, in seconds. None takes the default.
        strict: Raise instead of clamping when the request is below the floor.

    Raises:
        PollIntervalTooShortError: When `strict` and the request is too fast.
    """
    if requested is None:
        return DEFAULT_POLL_SECONDS
    try:
        seconds = int(requested)
    except (TypeError, ValueError):
        raise ValueError(
            f"a poll interval must be a number of seconds, got {requested!r}"
        ) from None
    if seconds >= MIN_POLL_SECONDS:
        return seconds
    if strict:
        raise PollIntervalTooShortError(seconds, MIN_POLL_SECONDS)
    logger.warning(
        "Clamping the requested inbox poll interval of %ds up to the %ds floor",
        seconds,
        MIN_POLL_SECONDS,
    )
    return MIN_POLL_SECONDS


def trim_watermark(watermark: Mapping[str, str]) -> dict[str, str]:
    """Keep the most recently seen threads and drop the tail."""
    items = list(watermark.items())
    return dict(items[:WATERMARK_LIMIT])


@dataclass(frozen=True, slots=True)
class InboxScanState:
    """What the previous inbox scan left behind for the next one."""

    account_id: int
    run_id: int | None = None
    last_scan_at: str | None = None
    poll_seconds: int = DEFAULT_POLL_SECONDS
    watermark: Mapping[str, str] = field(default_factory=dict)
    cursor: SearchCursor = field(default_factory=SearchCursor)
    threads_seen: int = 0
    stop_reason: str | None = None

    @property
    def first_run(self) -> bool:
        """True when this account's inbox has never been scanned to completion."""
        return self.last_scan_at is None

    @property
    def thread_limit(self) -> int:
        """Threads the next run should look at."""
        return FIRST_RUN_THREAD_LIMIT if self.first_run else DELTA_THREAD_LIMIT

    @property
    def seen_keys(self) -> tuple[str, ...]:
        """The watermark in the form the paged loop deduplicates against."""
        return tuple(
            thread_key(urn, signature) for urn, signature in self.watermark.items()
        )

    def due_at(self, *, poll_seconds: int | None = None) -> str | None:
        """Return when the next scan becomes due, or None when it is due now."""
        if self.last_scan_at is None:
            return None
        interval = resolve_poll_seconds(
            self.poll_seconds if poll_seconds is None else poll_seconds
        )
        return shift_timestamp(self.last_scan_at, interval)

    def is_due(
        self,
        *,
        now: datetime | str | None = None,
        poll_seconds: int | None = None,
    ) -> bool:
        """Return True when enough time has passed since the last scan."""
        due = self.due_at(poll_seconds=poll_seconds)
        return True if due is None else now_timestamp(now) >= due


def _filters_of(params: Mapping[str, Any]) -> dict[str, Any]:
    filters = params.get("filters")
    return dict(filters) if isinstance(filters, Mapping) else {}


def read_scan_state(
    conn: sqlite3.Connection,
    account_id: int,
    *,
    source: str = INBOX_SOURCE,
) -> InboxScanState:
    """Read the state the last completed inbox scan left for this account.

    An account that has never been scanned, or whose only scan is still open,
    comes back as a first run. That is the honest answer: a scan that never
    finished cannot be trusted to have archived what it saw.
    """
    row = conn.execute(
        """
        SELECT id, params_json, found_count, finished_at
        FROM harvest_runs
        WHERE account_id = ? AND source_type = ? AND finished_at IS NOT NULL
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """,
        (account_id, source),
    ).fetchone()
    if row is None:
        return InboxScanState(account_id=account_id)

    try:
        params = json.loads(row["params_json"] or "{}")
    except json.JSONDecodeError:
        params = {}
    if not isinstance(params, Mapping):
        params = {}

    filters = _filters_of(params)
    raw_watermark = filters.get("watermark")
    watermark = (
        {str(key): str(value) for key, value in raw_watermark.items()}
        if isinstance(raw_watermark, Mapping)
        else {}
    )
    try:
        poll_seconds = resolve_poll_seconds(filters.get("poll_seconds"))
    except ValueError:
        logger.warning(
            "Inbox run %s stored an unusable poll interval %r; falling back to the default",
            row["id"],
            filters.get("poll_seconds"),
        )
        poll_seconds = DEFAULT_POLL_SECONDS

    return InboxScanState(
        account_id=account_id,
        run_id=int(row["id"]),
        last_scan_at=row["finished_at"],
        poll_seconds=poll_seconds,
        watermark=trim_watermark(watermark),
        cursor=SearchCursor.from_dict(params.get("cursor")),
        threads_seen=int(row["found_count"] or 0),
        stop_reason=filters.get("stop_reason"),
    )


def scan_due(
    conn: sqlite3.Connection,
    account_id: int,
    *,
    now: datetime | str | None = None,
    poll_seconds: int | None = None,
    strict: bool = False,
) -> bool:
    """Return True when this account's inbox is due for a scan.

    This is the whole of SEQ-03's scheduling surface. SEQ-04's tick loop calls
    it, and calls :func:`~linkedin_mcp.inbox.scan.run_inbox_scan` when it says
    yes. An account that has never been scanned is always due.

    Args:
        conn: Open connection to the MCP database.
        account_id: Account whose inbox is in question.
        now: Decision time, injected so a runner stays deterministic.
        poll_seconds: Override the stored interval for this check.
        strict: Refuse an override below the floor instead of clamping it.
    """
    interval = (
        None if poll_seconds is None else resolve_poll_seconds(poll_seconds, strict=strict)
    )
    state = read_scan_state(conn, account_id)
    return state.is_due(now=now, poll_seconds=interval)


def next_scan_at(
    conn: sqlite3.Connection,
    account_id: int,
    *,
    poll_seconds: int | None = None,
    strict: bool = False,
) -> str | None:
    """Return the timestamp the next scan becomes due, or None when it is due now."""
    interval = (
        None if poll_seconds is None else resolve_poll_seconds(poll_seconds, strict=strict)
    )
    return read_scan_state(conn, account_id).due_at(poll_seconds=interval)
