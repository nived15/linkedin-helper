"""`worker_heartbeat`, and what it takes for `worker_status` to be honest.

The failure this table exists to prevent
----------------------------------------
A dashboard that reports "campaign running" because a campaign row says `active`
and jobs are `pending` is telling you about the database, not about the world. If
the worker died on Friday, nothing has run since Friday and every one of those
statements is still true. The only thing that can distinguish a running campaign
from a stalled one is evidence that something is still ticking, and this table is
that evidence.

What "stalled" means here
-------------------------
A worker is **stalled** when the gap between now and its `last_tick_at` exceeds
`stalled_after_seconds`. That number is a property of the deployment, not of this
module, so it is a parameter with a conservative default rather than a constant
somebody has to guess at.

The definition is only worth anything because of *when* the row is written. The
heartbeat is written at the **start** of every phase, before the work of that
phase is attempted, and it names the job it is about to run. A heartbeat written
only after a successful tick would report a healthy worker right up until the
moment it hung, and then report a healthy worker for as long as it stayed hung,
because a worker wedged inside a Playwright call never reaches its own success
path. Writing first inverts that: a wedged worker's last row is the one it wrote
before it wedged, its age grows, and `current_job_id` names exactly the job it is
stuck on.

Statuses
--------
`starting`, `sweeping`, `selecting`, `running`, `idle`, `outside_working_hours`,
`error` and `stopped`. Only `stopped` means a clean exit; everything else is a
worker that intended to keep going, and therefore a worker whose silence is a
fault rather than a shutdown.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from linkedin_mcp.sequences import JobState, now_timestamp, transaction

__all__ = [
    "DEFAULT_STALLED_AFTER_SECONDS",
    "LIVE_STATUSES",
    "STATUSES",
    "WorkerHeartbeat",
    "clear_heartbeat",
    "list_heartbeats",
    "read_heartbeat",
    "seconds_since",
    "worker_status",
    "write_heartbeat",
]

STATUS_STARTING = "starting"
STATUS_SWEEPING = "sweeping"
STATUS_SELECTING = "selecting"
STATUS_RUNNING = "running"
STATUS_IDLE = "idle"
STATUS_CLOSED = "outside_working_hours"
STATUS_ERROR = "error"
STATUS_STOPPED = "stopped"

STATUSES: tuple[str, ...] = (
    STATUS_STARTING,
    STATUS_SWEEPING,
    STATUS_SELECTING,
    STATUS_RUNNING,
    STATUS_IDLE,
    STATUS_CLOSED,
    STATUS_ERROR,
    STATUS_STOPPED,
)
"""Every status the runner writes. `worker_heartbeat.status` has no CHECK, so
this tuple is the vocabulary and :func:`write_heartbeat` enforces it."""

LIVE_STATUSES: frozenset[str] = frozenset(
    {
        STATUS_STARTING,
        STATUS_SWEEPING,
        STATUS_SELECTING,
        STATUS_RUNNING,
        STATUS_IDLE,
        STATUS_CLOSED,
        STATUS_ERROR,
    }
)
"""Statuses that claim the worker is still going, and can therefore go stale.

`error` is here on purpose. A worker that logged an error and kept ticking is
alive; one that logged an error and died is not, and only the age of the row can
tell those two apart.
"""

DEFAULT_STALLED_AFTER_SECONDS = 180
"""How old a heartbeat may get before the worker is presumed wedged.

Three minutes is several ticks at any sane tick interval, so an ordinary slow
tick does not raise a false alarm, and it is far shorter than the default
900 second job lease, so a wedged worker is visible long before its work is
reclaimed from under it.
"""

_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

HEALTH_RUNNING = "running"
HEALTH_STALLED = "stalled"
HEALTH_STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class WorkerHeartbeat:
    """One row of `worker_heartbeat`, plus what its age means."""

    worker_id: str
    account_id: int
    last_tick_at: str
    status: str
    current_job_id: int | None = None
    age_seconds: float | None = None

    @property
    def stopped(self) -> bool:
        return self.status == STATUS_STOPPED

    def health(self, stalled_after_seconds: int) -> str:
        """Return `running`, `stalled` or `stopped` for this row's age.

        An age that could not be computed reads as `stalled`, not as `running`.
        A corrupt timestamp is a worker nobody can vouch for, and the whole point
        of this table is that it refuses to vouch for a worker it cannot see.
        """
        if self.stopped:
            return HEALTH_STOPPED
        if self.age_seconds is None:
            return HEALTH_STALLED
        return (
            HEALTH_STALLED
            if self.age_seconds > stalled_after_seconds
            else HEALTH_RUNNING
        )

    def to_result(self, stalled_after_seconds: int) -> dict[str, Any]:
        health = self.health(stalled_after_seconds)
        return {
            "worker_id": self.worker_id,
            "account_id": self.account_id,
            "status": self.status,
            "last_tick_at": self.last_tick_at,
            "current_job_id": self.current_job_id,
            "age_seconds": self.age_seconds,
            "health": health,
            "stalled": health == HEALTH_STALLED,
        }


def _parse(moment: str) -> datetime | None:
    try:
        return datetime.strptime(moment, _TIMESTAMP_FORMAT).replace(
            tzinfo=timezone.utc
        )
    except (TypeError, ValueError):
        return None


def seconds_since(moment: str, now: datetime | str | None = None) -> float | None:
    """Return how many seconds have passed since a stored timestamp.

    None when either end is unparseable, because a heartbeat whose age cannot be
    computed must not be reported as fresh.
    """
    then = _parse(moment)
    reference = _parse(now_timestamp(now))
    if then is None or reference is None:
        return None
    return (reference - then).total_seconds()


def _row(row: sqlite3.Row, now: datetime | str | None) -> WorkerHeartbeat:
    last_tick_at = row["last_tick_at"]
    return WorkerHeartbeat(
        worker_id=row["worker_id"],
        account_id=int(row["account_id"]),
        last_tick_at=last_tick_at,
        status=row["status"],
        current_job_id=(
            None if row["current_job_id"] is None else int(row["current_job_id"])
        ),
        age_seconds=seconds_since(last_tick_at, now),
    )


def write_heartbeat(
    conn: sqlite3.Connection,
    worker_id: str,
    account_id: int,
    status: str,
    *,
    current_job_id: int | None = None,
    now: datetime | str | None = None,
) -> str:
    """Record what a worker is about to do, and return the timestamp written.

    Called before the work, never after it. See the module docstring: a
    heartbeat written on success reports health it cannot know about.
    """
    if status not in STATUSES:
        raise ValueError(
            f"unknown worker status {status!r}; expected one of {list(STATUSES)}"
        )
    moment = now_timestamp(now)
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO worker_heartbeat
                (worker_id, account_id, last_tick_at, status, current_job_id)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (worker_id) DO UPDATE SET
                account_id = excluded.account_id,
                last_tick_at = excluded.last_tick_at,
                status = excluded.status,
                current_job_id = excluded.current_job_id
            """,
            (worker_id, account_id, moment, status, current_job_id),
        )
    return moment


def read_heartbeat(
    conn: sqlite3.Connection,
    worker_id: str,
    *,
    now: datetime | str | None = None,
) -> WorkerHeartbeat | None:
    """Return one worker's heartbeat, or None when it never wrote one."""
    row = conn.execute(
        "SELECT * FROM worker_heartbeat WHERE worker_id = ?", (worker_id,)
    ).fetchone()
    return None if row is None else _row(row, now)


def list_heartbeats(
    conn: sqlite3.Connection,
    *,
    account_id: int | None = None,
    now: datetime | str | None = None,
) -> list[WorkerHeartbeat]:
    """Return every heartbeat, newest tick first."""
    sql = "SELECT * FROM worker_heartbeat"
    params: list[Any] = []
    if account_id is not None:
        sql += " WHERE account_id = ?"
        params.append(account_id)
    sql += " ORDER BY last_tick_at DESC, worker_id"
    return [_row(row, now) for row in conn.execute(sql, params).fetchall()]


def clear_heartbeat(conn: sqlite3.Connection, worker_id: str) -> bool:
    """Forget a worker entirely. For decommissioning, not for shutdown.

    A stopped worker keeps its row, because "stopped cleanly at 18:04" is worth
    knowing and an absent row is indistinguishable from a worker that never ran.
    """
    with transaction(conn):
        cursor = conn.execute(
            "DELETE FROM worker_heartbeat WHERE worker_id = ?", (worker_id,)
        )
    return cursor.rowcount > 0


def _queue_depths(
    conn: sqlite3.Connection,
    account_id: int | None,
    moment: str,
) -> dict[str, int]:
    where = "" if account_id is None else " AND account_id = ?"
    tail: Sequence[Any] = () if account_id is None else (account_id,)

    pending = conn.execute(
        f"SELECT COUNT(*) AS total FROM jobs WHERE state = ?{where}",
        (JobState.PENDING.value, *tail),
    ).fetchone()["total"]
    leased = conn.execute(
        f"SELECT COUNT(*) AS total FROM jobs WHERE state = ?{where}",
        (JobState.LEASED.value, *tail),
    ).fetchone()["total"]
    overdue = conn.execute(
        f"SELECT COUNT(*) AS total FROM jobs WHERE state = ? AND scheduled_for <= ?{where}",
        (JobState.PENDING.value, moment, *tail),
    ).fetchone()["total"]
    # Neither lane can execute a job that names a campaign but no lead, so it is
    # counted here rather than left to accumulate where only a log line mentions
    # it. A number that will not go down is the signal somebody needs.
    unroutable = conn.execute(
        f"""
        SELECT COUNT(*) AS total FROM jobs
        WHERE state IN (?, ?) AND campaign_id IS NOT NULL AND lead_id IS NULL{where}
        """,
        (JobState.PENDING.value, JobState.LEASED.value, *tail),
    ).fetchone()["total"]
    return {
        "pending_jobs": int(pending),
        "leased_jobs": int(leased),
        "due_jobs": int(overdue),
        "unroutable_jobs": int(unroutable),
    }


def worker_status(
    conn: sqlite3.Connection,
    *,
    account_id: int | None = None,
    now: datetime | str | None = None,
    stalled_after_seconds: int = DEFAULT_STALLED_AFTER_SECONDS,
) -> dict[str, Any]:
    """Report whether anything is actually running, and say so plainly.

    `campaigns_running` is the answer to the question people really ask, and it
    is False whenever no worker is live, however many jobs are queued and however
    many campaigns are `active`. `queue_is_moving` separates the two honest kinds
    of quiet: nothing to do, versus plenty to do and nobody doing it.
    """
    moment = now_timestamp(now)
    workers = list_heartbeats(conn, account_id=account_id, now=moment)
    reports = [worker.to_result(stalled_after_seconds) for worker in workers]

    live = [report for report in reports if report["health"] == HEALTH_RUNNING]
    stalled = [report for report in reports if report["stalled"]]
    depths = _queue_depths(conn, account_id, moment)

    return {
        "as_of": moment,
        "account_id": account_id,
        "stalled_after_seconds": stalled_after_seconds,
        "workers": reports,
        "live_workers": len(live),
        "stalled_workers": len(stalled),
        "stopped_workers": sum(
            1 for report in reports if report["health"] == HEALTH_STOPPED
        ),
        "campaigns_running": bool(live),
        "queue_is_moving": bool(live) or depths["due_jobs"] == 0,
        **depths,
    }
