"""The `jobs` execution queue, derived from campaign state.

`campaign_leads` is the truth. `jobs` is a projection of it: exactly one open job
per lead that is still in the flow, pointing at the step that lead will run next.
Nothing about what to do next is stored only in a job, which is why
:func:`rebuild_jobs` can throw the whole table away and reconstruct an equivalent
queue from `campaign_leads` and `campaign_steps` alone.

What is derived and what is not
-------------------------------
Derived, and therefore rebuilt: `account_id`, `campaign_id`, `lead_id`, `step_id`,
`action_type`, `payload_json`, `scheduled_for`, `priority`, and the fact that the
row is `pending`.

Not derived, and therefore never reconstructed: `attempts`, `last_error`,
`locked_by` and `locked_at`. Those describe an execution that has already
happened. A rebuilt queue is equivalent in what it will do, not in what it has
already tried; the attempt count that matters for retry limits lives on
`campaign_leads.attempts`, which survives.

The payload is a pointer, not content. It names the step ord and nothing else, so
derivation stays a pure function of stored state. Rendering a message body is
SEQ-02's job and happens when the job runs, not when it is queued.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from linkedin_mcp.sequences.campaigns import RUNNABLE_STATUSES, require_campaign
from linkedin_mcp.sequences.states import (
    CLOSED_JOB_STATES,
    OPEN_JOB_STATES,
    JobState,
    Sublist,
    coerce_job_state,
)
from linkedin_mcp.sequences.transaction import now_timestamp, transaction

__all__ = [
    "DERIVED_COLUMNS",
    "Job",
    "JobSpec",
    "RebuildReport",
    "close_open_jobs",
    "derive_jobs",
    "due_jobs",
    "insert_job",
    "job_row",
    "lease_job",
    "list_jobs",
    "open_job_for_lead",
    "open_job_specs",
    "orphan_open_jobs",
    "queue_matches_state",
    "rebuild_jobs",
    "step_payload",
]

DERIVED_COLUMNS: tuple[str, ...] = (
    "account_id",
    "campaign_id",
    "lead_id",
    "step_id",
    "action_type",
    "payload_json",
    "scheduled_for",
    "priority",
)
"""Job columns that are a pure function of campaign state."""

_OPEN_STATE_VALUES = tuple(state.value for state in OPEN_JOB_STATES)


@dataclass(frozen=True, slots=True, order=True)
class JobSpec:
    """The derivable shape of a job, with no execution history attached.

    Ordered so two queues can be compared as sorted tuples regardless of the
    `jobs.id` values, which a rebuild does not and cannot preserve.
    """

    campaign_id: int
    lead_id: int
    step_id: int
    account_id: int
    action_type: str
    payload_json: str
    scheduled_for: str
    priority: int


@dataclass(frozen=True, slots=True)
class Job:
    """A row of the execution queue."""

    id: int
    account_id: int
    campaign_id: int | None
    lead_id: int | None
    step_id: int | None
    action_type: str
    payload: dict[str, Any]
    scheduled_for: str
    priority: int
    state: str
    attempts: int
    last_error: str | None = None
    locked_by: str | None = None
    locked_at: str | None = None

    @property
    def is_open(self) -> bool:
        return self.state in _OPEN_STATE_VALUES

    def spec(self) -> JobSpec:
        """Return the derivable part of this job, for comparison with a rebuild."""
        if self.campaign_id is None or self.lead_id is None or self.step_id is None:
            raise ValueError(
                f"job {self.id} is not campaign work, so it has no derivable spec"
            )
        return JobSpec(
            campaign_id=self.campaign_id,
            lead_id=self.lead_id,
            step_id=self.step_id,
            account_id=self.account_id,
            action_type=self.action_type,
            payload_json=json.dumps(self.payload, sort_keys=True),
            scheduled_for=self.scheduled_for,
            priority=self.priority,
        )


@dataclass(frozen=True, slots=True)
class RebuildReport:
    """What :func:`rebuild_jobs` changed."""

    campaign_id: int
    deleted: int = 0
    created: int = 0
    requeued: int = 0
    completed: int = 0


def job_row(row: sqlite3.Row) -> Job:
    payload = json.loads(row["payload_json"] or "{}")
    if not isinstance(payload, dict):
        payload = {}
    return Job(
        id=row["id"],
        account_id=row["account_id"],
        campaign_id=row["campaign_id"],
        lead_id=row["lead_id"],
        step_id=row["step_id"],
        action_type=row["action_type"],
        payload=payload,
        scheduled_for=row["scheduled_for"],
        priority=row["priority"],
        state=row["state"],
        attempts=row["attempts"],
        last_error=row["last_error"],
        locked_by=row["locked_by"],
        locked_at=row["locked_at"],
    )


def step_payload(step_ord: int) -> str:
    """Return the payload a step's job carries.

    Deliberately just a pointer. Anything richer would have to be recomputed
    identically by a rebuild, and content that can drift is content the queue
    should not be the only copy of.
    """
    return json.dumps({"step_ord": int(step_ord)}, sort_keys=True)


def insert_job(
    conn: sqlite3.Connection,
    spec: JobSpec,
    *,
    state: JobState | str = JobState.PENDING,
) -> int:
    """Insert one job row and return its id.

    Must be called inside a :func:`linkedin_mcp.sequences.transaction.transaction`
    block. It does not commit, so the caller can pair it with the
    `campaign_leads` write it belongs to.
    """
    job_state = coerce_job_state(state)
    cursor = conn.execute(
        """
        INSERT INTO jobs
            (account_id, campaign_id, lead_id, step_id, action_type, payload_json,
             scheduled_for, priority, state, attempts)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        """,
        (
            spec.account_id,
            spec.campaign_id,
            spec.lead_id,
            spec.step_id,
            spec.action_type,
            spec.payload_json,
            spec.scheduled_for,
            spec.priority,
            job_state.value,
        ),
    )
    return int(cursor.lastrowid)


def close_open_jobs(
    conn: sqlite3.Connection,
    campaign_id: int,
    lead_id: int,
    state: JobState | str,
    *,
    error: str | None = None,
) -> int:
    """Move every open job for one lead into a closed state and return the count.

    Closing rather than deleting keeps the execution history, and moving the row
    out of `pending`/`leased` also releases it from the one-open-job-per-lead
    index so the next step can be enqueued in the same transaction.
    """
    closed = coerce_job_state(state)
    if closed not in CLOSED_JOB_STATES:
        raise ValueError(
            f"{closed.value!r} is not a closed job state; expected one of "
            f"{[member.value for member in CLOSED_JOB_STATES]}"
        )
    placeholders = ", ".join("?" for _ in _OPEN_STATE_VALUES)
    cursor = conn.execute(
        f"""
        UPDATE jobs
        SET state = ?, last_error = ?, locked_by = NULL, locked_at = NULL
        WHERE campaign_id = ? AND lead_id = ? AND state IN ({placeholders})
        """,
        (closed.value, error, campaign_id, lead_id, *_OPEN_STATE_VALUES),
    )
    return int(cursor.rowcount)


def lease_job(
    conn: sqlite3.Connection,
    job_id: int,
    worker_id: str,
    *,
    now: datetime | str | None = None,
) -> bool:
    """Mark one pending job as leased by a worker. Returns False if it was taken."""
    moment = now_timestamp(now)
    cursor = conn.execute(
        """
        UPDATE jobs
        SET state = ?, locked_by = ?, locked_at = ?, attempts = attempts + 1
        WHERE id = ? AND state = ?
        """,
        (JobState.LEASED.value, worker_id, moment, job_id, JobState.PENDING.value),
    )
    return cursor.rowcount > 0


def open_job_for_lead(
    conn: sqlite3.Connection,
    campaign_id: int,
    lead_id: int,
) -> Job | None:
    """Return the single open job for a lead, if it has one."""
    placeholders = ", ".join("?" for _ in _OPEN_STATE_VALUES)
    row = conn.execute(
        f"""
        SELECT * FROM jobs
        WHERE campaign_id = ? AND lead_id = ? AND state IN ({placeholders})
        ORDER BY id
        LIMIT 1
        """,
        (campaign_id, lead_id, *_OPEN_STATE_VALUES),
    ).fetchone()
    return None if row is None else job_row(row)


def list_jobs(
    conn: sqlite3.Connection,
    *,
    campaign_id: int | None = None,
    account_id: int | None = None,
    states: Iterable[JobState | str] | None = None,
) -> list[Job]:
    """Read jobs, optionally narrowed to a campaign, an account or a set of states."""
    sql = "SELECT * FROM jobs WHERE 1 = 1"
    params: list[Any] = []
    if campaign_id is not None:
        sql += " AND campaign_id = ?"
        params.append(campaign_id)
    if account_id is not None:
        sql += " AND account_id = ?"
        params.append(account_id)
    if states is not None:
        wanted = [coerce_job_state(state).value for state in states]
        if not wanted:
            return []
        sql += f" AND state IN ({', '.join('?' for _ in wanted)})"
        params.extend(wanted)
    sql += " ORDER BY priority DESC, scheduled_for, id"
    return [job_row(row) for row in conn.execute(sql, params).fetchall()]


def open_job_specs(conn: sqlite3.Connection, campaign_id: int) -> tuple[JobSpec, ...]:
    """Return the open jobs of a campaign in canonical, id-free, sorted form.

    A job whose `step_id` was nulled out by a step deletion has no derivable
    shape, so it is left out here and deleted by the next
    :func:`rebuild_jobs`. Silently dropping it from the comparison would hide the
    corruption; :func:`queue_matches_state` reports the mismatch instead.
    """
    jobs = list_jobs(conn, campaign_id=campaign_id, states=OPEN_JOB_STATES)
    return tuple(
        sorted(
            job.spec()
            for job in jobs
            if job.campaign_id is not None
            and job.lead_id is not None
            and job.step_id is not None
        )
    )


def due_jobs(
    conn: sqlite3.Connection,
    account_id: int,
    *,
    now: datetime | str | None = None,
    campaign_id: int | None = None,
    limit: int = 50,
) -> list[Job]:
    """Return pending jobs an account may run right now, most urgent first.

    This is the read SEQ-04's runner leases from. It filters on the campaign
    status and on the lead still sitting in `queue`, so a paused campaign or a
    lead that replied since the job was written yields nothing even if the queue
    was not swept first.
    """
    moment = now_timestamp(now)
    runnable = tuple(sorted(RUNNABLE_STATUSES))
    sql = f"""
        SELECT j.*
        FROM jobs AS j
        JOIN campaigns AS c ON c.id = j.campaign_id
        JOIN campaign_leads AS cl
            ON cl.campaign_id = j.campaign_id AND cl.lead_id = j.lead_id
        WHERE j.account_id = ?
          AND j.state = ?
          AND j.scheduled_for <= ?
          AND c.status IN ({", ".join("?" for _ in runnable)})
          AND cl.sublist = ?
    """
    params: list[Any] = [
        account_id,
        JobState.PENDING.value,
        moment,
        *runnable,
        Sublist.QUEUE.value,
    ]
    if campaign_id is not None:
        sql += " AND j.campaign_id = ?"
        params.append(campaign_id)
    sql += " ORDER BY j.priority DESC, j.scheduled_for, j.id LIMIT ?"
    params.append(max(0, int(limit)))
    return [job_row(row) for row in conn.execute(sql, params).fetchall()]


def _derive(
    conn: sqlite3.Connection,
    campaign_id: int,
    account_id: int,
    fallback: str,
    sublists: tuple[str, ...],
) -> tuple[JobSpec, ...]:
    placeholders = ", ".join("?" for _ in sublists)
    rows = conn.execute(
        f"""
        SELECT cl.lead_id, cl.next_run_at, s.id AS step_id, s.ord AS step_ord,
               s.action_type, s.config_json
        FROM campaign_leads AS cl
        JOIN campaign_steps AS s
            ON s.campaign_id = cl.campaign_id AND s.ord = cl.current_step_ord
        WHERE cl.campaign_id = ? AND cl.sublist IN ({placeholders})
        ORDER BY cl.lead_id
        """,
        (campaign_id, *sublists),
    ).fetchall()

    specs: list[JobSpec] = []
    for row in rows:
        config = json.loads(row["config_json"] or "{}")
        if not isinstance(config, dict):
            config = {}
        specs.append(
            JobSpec(
                campaign_id=campaign_id,
                lead_id=int(row["lead_id"]),
                step_id=int(row["step_id"]),
                account_id=account_id,
                action_type=row["action_type"],
                payload_json=step_payload(int(row["step_ord"])),
                scheduled_for=row["next_run_at"] or fallback,
                priority=int(config.get("priority", 0)),
            )
        )
    return tuple(sorted(specs))


def derive_jobs(
    conn: sqlite3.Connection,
    campaign_id: int,
    *,
    now: datetime | str | None = None,
) -> tuple[JobSpec, ...]:
    """Return the queue this campaign's state implies, sorted and id-free.

    One job per lead still in the flow, aimed at the step its `current_step_ord`
    names. Leads in a terminal sub-list derive nothing, which is what makes a
    stale job for a replied lead disappear on rebuild rather than fire.

    Pure with respect to stored state, with one caveat: an active lead whose
    `next_run_at` is NULL has no stored due time, so `now` stands in for it and
    two derivations a minute apart disagree. Nothing in this package writes such
    a row, and :func:`rebuild_jobs` persists a due time for any it finds, so the
    caveat only bites on a database corrupted from outside.
    """
    campaign = require_campaign(conn, campaign_id)
    return _derive(
        conn,
        campaign_id,
        campaign.account_id,
        now_timestamp(now),
        (Sublist.QUEUE.value, Sublist.PROCESSING.value),
    )


def _repair_ords(conn: sqlite3.Connection, campaign_id: int, moment: str) -> int:
    """Point active leads at a step that exists, or close out the ones past the end.

    Returns how many were closed out as `successful`. A lead whose ord landed in a
    gap is snapped forward to the next real step rather than being declared
    finished, because a gap means the step list was edited underneath it, not that
    the lead did the work.
    """
    last = conn.execute(
        "SELECT MAX(ord) AS ord FROM campaign_steps WHERE campaign_id = ?",
        (campaign_id,),
    ).fetchone()["ord"]

    rows = conn.execute(
        """
        SELECT cl.lead_id, cl.current_step_ord
        FROM campaign_leads AS cl
        WHERE cl.campaign_id = ?
          AND cl.sublist IN (?, ?)
          AND NOT EXISTS (
              SELECT 1 FROM campaign_steps AS s
              WHERE s.campaign_id = cl.campaign_id AND s.ord = cl.current_step_ord
          )
        ORDER BY cl.lead_id
        """,
        (campaign_id, Sublist.QUEUE.value, Sublist.PROCESSING.value),
    ).fetchall()

    completed = 0
    for row in rows:
        lead_id = int(row["lead_id"])
        current = int(row["current_step_ord"])
        following = conn.execute(
            "SELECT MIN(ord) AS ord FROM campaign_steps WHERE campaign_id = ? AND ord >= ?",
            (campaign_id, current),
        ).fetchone()["ord"]
        if following is not None:
            conn.execute(
                """
                UPDATE campaign_leads
                SET current_step_ord = ?, updated_at = ?
                WHERE campaign_id = ? AND lead_id = ?
                """,
                (int(following), moment, campaign_id, lead_id),
            )
            continue
        conn.execute(
            """
            UPDATE campaign_leads
            SET sublist = ?, current_step_ord = ?, next_run_at = NULL,
                last_outcome = ?, updated_at = ?
            WHERE campaign_id = ? AND lead_id = ?
            """,
            (
                Sublist.SUCCESSFUL.value,
                (int(last) + 1) if last is not None else current,
                "sequence_complete",
                moment,
                campaign_id,
                lead_id,
            ),
        )
        completed += 1
    return completed


def rebuild_jobs(
    conn: sqlite3.Connection,
    campaign_id: int,
    *,
    now: datetime | str | None = None,
    recover_processing: bool = True,
) -> RebuildReport:
    """Discard the open queue and re-derive it from campaign state.

    This is the corruption recovery path named in the DoD. It repairs five things
    in one transaction:

    1. Leads whose `current_step_ord` no longer names a real step are snapped
       forward to the next one, or closed out as `successful` when the list has
       run out under them.
    2. Active leads with no `next_run_at` are given one, so the derivation that
       follows is a pure function of stored state rather than of the clock.
    3. Leads stuck in `processing` return to `queue`. A lead in `processing`
       whose job was lost can never make progress on its own. Pass
       `recover_processing=False` to leave them and their live leases completely
       untouched, which is what a definition-only refresh wants.
    4. Every open job in scope is deleted and re-derived, so a queue that is
       missing rows, has duplicates, or points at the wrong step is corrected.
    5. Stale open jobs for leads that already left the flow disappear, because a
       terminal lead derives nothing.

    Closed jobs are history and are left untouched.
    """
    moment = now_timestamp(now)
    with transaction(conn):
        campaign = require_campaign(conn, campaign_id)

        completed = _repair_ords(conn, campaign_id, moment)

        conn.execute(
            """
            UPDATE campaign_leads
            SET next_run_at = ?
            WHERE campaign_id = ? AND sublist IN (?, ?) AND next_run_at IS NULL
            """,
            (moment, campaign_id, Sublist.QUEUE.value, Sublist.PROCESSING.value),
        )

        requeued = 0
        if recover_processing:
            cursor = conn.execute(
                """
                UPDATE campaign_leads
                SET sublist = ?, last_outcome = ?, updated_at = ?
                WHERE campaign_id = ? AND sublist = ?
                """,
                (
                    Sublist.QUEUE.value,
                    "requeued_by_rebuild",
                    moment,
                    campaign_id,
                    Sublist.PROCESSING.value,
                ),
            )
            requeued = int(cursor.rowcount)

        placeholders = ", ".join("?" for _ in _OPEN_STATE_VALUES)
        scope = f"DELETE FROM jobs WHERE campaign_id = ? AND state IN ({placeholders})"
        params: list[Any] = [campaign_id, *_OPEN_STATE_VALUES]
        derived_from = (Sublist.QUEUE.value, Sublist.PROCESSING.value)
        if not recover_processing:
            # Leave a live lease and the job it holds exactly as they are.
            scope += """
                AND NOT EXISTS (
                    SELECT 1 FROM campaign_leads AS cl
                    WHERE cl.campaign_id = jobs.campaign_id
                      AND cl.lead_id = jobs.lead_id
                      AND cl.sublist = ?
                )
            """
            params.append(Sublist.PROCESSING.value)
            derived_from = (Sublist.QUEUE.value,)

        deleted = int(conn.execute(scope, params).rowcount)

        specs = _derive(conn, campaign_id, campaign.account_id, moment, derived_from)
        for spec in specs:
            insert_job(conn, spec)

    return RebuildReport(
        campaign_id=campaign_id,
        deleted=deleted,
        created=len(specs),
        requeued=requeued,
        completed=completed,
    )


def orphan_open_jobs(conn: sqlite3.Connection, campaign_id: int) -> int:
    """Count open jobs of a campaign that have no derivable shape.

    A job whose `step_id` was nulled out when its step was deleted is one of
    these. It cannot be compared against a derivation, so it is corruption by
    definition and the next :func:`rebuild_jobs` deletes it.
    """
    placeholders = ", ".join("?" for _ in _OPEN_STATE_VALUES)
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS total FROM jobs
        WHERE campaign_id = ? AND state IN ({placeholders})
          AND (lead_id IS NULL OR step_id IS NULL)
        """,
        (campaign_id, *_OPEN_STATE_VALUES),
    ).fetchone()
    return int(row["total"])


def queue_matches_state(
    conn: sqlite3.Connection,
    campaign_id: int,
    *,
    now: datetime | str | None = None,
) -> bool:
    """Return True when the stored queue equals what campaign state derives.

    Useful as an assertion in a runner and as the equivalence check after a
    rebuild. An open job with no derivable shape counts as a mismatch rather than
    being quietly excluded from the comparison.
    """
    if orphan_open_jobs(conn, campaign_id):
        return False
    return open_job_specs(conn, campaign_id) == derive_jobs(conn, campaign_id, now=now)
