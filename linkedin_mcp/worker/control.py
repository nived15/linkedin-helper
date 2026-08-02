"""MCP-05 (#28): a pause the whole worker honours, not one campaign at a time.

The gap this closes
-------------------
Before this module the only pause-shaped tools were `campaign_pause` and
`campaign_resume`, and each pauses exactly one campaign. That is enough for the
campaign lane, because
:func:`linkedin_mcp.sequences.jobs.due_jobs` inner-joins `campaigns` on
:data:`~linkedin_mcp.sequences.campaigns.RUNNABLE_STATUSES`, so a paused campaign
selects nothing.

It is not enough for the worker. The ad-hoc lane in
:func:`linkedin_mcp.worker.selection.ad_hoc_due_jobs` keys on
``campaign_id IS NULL`` and never consults a campaign, so it has no off switch at
all. Measured on `2a34682`: build a campaign, start it, enqueue one ad-hoc
`profile_view`, pause the campaign, and `select_due_jobs` still returns the
ad-hoc job. A client told "pause the worker" by pausing every campaign would keep
sending.

So the pause lives here, one row per account, and
:func:`linkedin_mcp.worker.selection.select_due_jobs` reads it before either lane
is queried. Both lanes stop or neither does.

Why not `accounts.state = 'paused'`
-----------------------------------
That column belongs to detection. `linkedin_mcp.safety.detect` writes
`challenged` and `logged_out` into it and ranks the transitions between them, so
an operator pause written there would race challenge escalation and an operator
resume could clear a challenge nobody resolved.

It is also the wrong mechanism. The safety gate reads `accounts.state` only for
metered actions, so a local step would still run, and nothing in job selection
reads it at all. A job that is selected, leased, attempted and then refused is
not paused work: it is churn that burns attempts and writes a refusal row and a
`safety_events` alert per job. A pause has to happen before selection, and that
is what this does.

What a pause does not do
------------------------
It does not cancel anything. Every queued job keeps its state and its due time,
so a resume picks the queue up where it stopped rather than re-planning it. It
also does not stop a job already in flight: the worker finishes the action it is
mid-way through, because abandoning a half-sent invitation is not something a
flag can undo.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from linkedin_mcp.sequences import now_timestamp, transaction

__all__ = [
    "PauseState",
    "is_worker_paused",
    "pause_worker",
    "resume_worker",
    "worker_pause_state",
]


@dataclass(frozen=True, slots=True)
class PauseState:
    """Whether this account's worker is paused, and the provenance of that.

    `paused` is the only field job selection reads. The rest exists so a client
    reading `linkedin://worker/status` can say who stopped the worker and why
    rather than only that it is stopped, which is the difference between an
    operator pausing for a template rewrite and a worker that was never started.
    """

    account_id: int
    paused: bool = False
    reason: str | None = None
    paused_by: str | None = None
    paused_at: str | None = None
    resumed_at: str | None = None
    updated_at: str | None = None

    def to_result(self) -> dict[str, Any]:
        """The payload shape both the tools and the worker resource return."""
        return {
            "account_id": self.account_id,
            "paused": self.paused,
            "reason": self.reason,
            "paused_by": self.paused_by,
            "paused_at": self.paused_at,
            "resumed_at": self.resumed_at,
            "updated_at": self.updated_at,
        }


def worker_pause_state(conn: sqlite3.Connection, account_id: int) -> PauseState:
    """Return the pause state for one account.

    An account with no `worker_control` row has never been paused, which reads
    as running. Absence is the default rather than an error, so a database that
    predates the pause behaves exactly as it did.
    """
    row = conn.execute(
        "SELECT * FROM worker_control WHERE account_id = ?",
        (int(account_id),),
    ).fetchone()
    if row is None:
        return PauseState(account_id=int(account_id))
    return PauseState(
        account_id=int(row["account_id"]),
        paused=bool(row["paused"]),
        reason=row["reason"],
        paused_by=row["paused_by"],
        paused_at=row["paused_at"],
        resumed_at=row["resumed_at"],
        updated_at=row["updated_at"],
    )


def is_worker_paused(conn: sqlite3.Connection, account_id: int) -> bool:
    """True when no lane may select work for this account."""
    return worker_pause_state(conn, account_id).paused


def _write(
    conn: sqlite3.Connection,
    account_id: int,
    *,
    paused: bool,
    reason: str | None,
    paused_by: str | None,
    paused_at: str | None,
    resumed_at: str | None,
    moment: str,
) -> PauseState:
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO worker_control
                (account_id, paused, reason, paused_by, paused_at, resumed_at,
                 updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (account_id) DO UPDATE SET
                paused = excluded.paused,
                reason = excluded.reason,
                paused_by = excluded.paused_by,
                paused_at = excluded.paused_at,
                resumed_at = excluded.resumed_at,
                updated_at = excluded.updated_at
            """,
            (
                int(account_id),
                1 if paused else 0,
                reason,
                paused_by,
                paused_at,
                resumed_at,
                moment,
            ),
        )
    return worker_pause_state(conn, account_id)


def pause_worker(
    conn: sqlite3.Connection,
    account_id: int,
    *,
    reason: str | None = None,
    paused_by: str | None = None,
    now: datetime | str | None = None,
) -> PauseState:
    """Stop both job lanes for this account and record who did it.

    Idempotent. Pausing an already-paused worker refreshes the reason and the
    timestamp rather than failing, because a client that cannot tell whether its
    first call landed must be able to call again.
    """
    moment = now_timestamp(now)
    return _write(
        conn,
        account_id,
        paused=True,
        reason=(reason or None),
        paused_by=(paused_by or None),
        paused_at=moment,
        resumed_at=None,
        moment=moment,
    )


def resume_worker(
    conn: sqlite3.Connection,
    account_id: int,
    *,
    now: datetime | str | None = None,
) -> PauseState:
    """Let both lanes select work again, keeping why it was stopped.

    `reason` and `paused_by` survive the resume on purpose. "Paused at 14:02 by
    nived because the invite template was wrong, resumed at 14:40" is a more
    useful row than a blank one, and nothing reads either field to decide
    anything.
    """
    moment = now_timestamp(now)
    current = worker_pause_state(conn, account_id)
    return _write(
        conn,
        account_id,
        paused=False,
        reason=current.reason,
        paused_by=current.paused_by,
        paused_at=current.paused_at,
        resumed_at=moment,
        moment=moment,
    )
