"""Atomic transitions: the only sanctioned way a lead changes sub-list.

Every function here is one transaction. The `campaign_leads` write and the `jobs`
write land together or neither lands, so the failure mode the DoD names, a crash
mid-step stranding a lead in `processing`, cannot be produced by an interrupted
transition. If the process dies before the commit, SQLite rolls the whole thing
back and the lead is exactly where it started.

That covers the uncommitted case. The committed case is different and needs its
own answer: a worker that successfully claims a lead into `processing` and then
dies really has left the lead there, because the claim was supposed to be
durable. :func:`recover_stranded` is the sweep for that, and it is the reason
`processing` is safe to use as a real lease rather than a hopeful flag.

The step lifecycle
------------------
    queue --claim_step--> processing --complete_step--> queue (next ord)
                                     \\--fail_step----> queue (retry) | failed | skipped
                                     \\--refuse_step--> per RefusalReason
                                     \\--apply_filter_step--> queue | skipped | excluded

`mark_replied` can fire from either active sub-list, because a reply arrives
whenever it arrives.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import datetime

from linkedin_mcp.audit import RefusalReason
from linkedin_mcp.sequences import jobs as jobs_module
from linkedin_mcp.sequences.campaigns import require_campaign
from linkedin_mcp.sequences.enrollment import (
    CampaignLead,
    require_campaign_lead,
)
from linkedin_mcp.sequences.errors import InvalidTransitionError
from linkedin_mcp.sequences.jobs import JobSpec
from linkedin_mcp.sequences.states import (
    Disposition,
    JobState,
    Sublist,
    can_transition,
    coerce_sublist,
    disposition_for,
    retry_after_seconds,
)
from linkedin_mcp.sequences.steps import (
    ON_FAILURE_FAIL,
    ON_FAILURE_SKIP,
    Step,
    find_step_at_ord,
    last_step_ord,
    next_step_ord,
    step_at_ord,
)
from linkedin_mcp.sequences.transaction import (
    now_timestamp,
    shift_timestamp,
    transaction,
)

__all__ = [
    "DEFAULT_LEASE_SECONDS",
    "RECOVERED_OUTCOME",
    "REPLIED_OUTCOME",
    "apply_filter_step",
    "claim_step",
    "complete_step",
    "current_step",
    "exclude_lead",
    "fail_step",
    "mark_replied",
    "recover_all_stranded",
    "recover_stranded",
    "refuse_step",
    "reset_lead",
    "skip_lead",
    "sublists_of",
]

DEFAULT_LEASE_SECONDS = 900
"""How long a claim on a lead stays valid before the sweep reclaims it."""

REPLIED_OUTCOME = "replied"
RECOVERED_OUTCOME = "recovered_stale_lease"


def _write_lead_state(
    conn: sqlite3.Connection,
    campaign_id: int,
    lead_id: int,
    *,
    sublist: Sublist,
    current_step_ord: int,
    next_run_at: str | None,
    attempts: int,
    last_outcome: str | None,
    updated_at: str,
) -> None:
    conn.execute(
        """
        UPDATE campaign_leads
        SET sublist = ?, current_step_ord = ?, next_run_at = ?, attempts = ?,
            last_outcome = ?, updated_at = ?
        WHERE campaign_id = ? AND lead_id = ?
        """,
        (
            sublist.value,
            current_step_ord,
            next_run_at,
            attempts,
            last_outcome,
            updated_at,
            campaign_id,
            lead_id,
        ),
    )


def _guard(record: CampaignLead, target: Sublist) -> None:
    if not can_transition(record.sublist, target):
        raise InvalidTransitionError(
            record.campaign_id, record.lead_id, record.sublist, target.value
        )


def _require_lease(
    conn: sqlite3.Connection,
    record: CampaignLead,
    worker_id: str | None,
) -> None:
    """Refuse a finalising call from a worker that no longer holds the lead.

    A worker that stalls past its lease has its lead requeued by
    `recover_stranded` and possibly re-claimed by somebody else. If it then wakes
    up and finishes, it would close a job it does not own and advance a lead
    another worker is mid-way through, which is exactly the double send the lease
    exists to prevent. Two things are checked: the lead must still be
    `processing`, and, when the caller identifies itself, the lease must still be
    in its name.
    """
    if record.sublist != Sublist.PROCESSING.value:
        raise InvalidTransitionError(
            record.campaign_id,
            record.lead_id,
            record.sublist,
            Sublist.PROCESSING.value,
        )
    if worker_id is None:
        return
    job = jobs_module.open_job_for_lead(conn, record.campaign_id, record.lead_id)
    if job is None or job.state != JobState.LEASED.value or job.locked_by != worker_id:
        holder = "nobody" if job is None else repr(job.locked_by)
        raise InvalidTransitionError(
            record.campaign_id,
            record.lead_id,
            f"processing (leased by {holder})",
            f"finalised by {worker_id!r}",
        )


def current_step(
    conn: sqlite3.Connection,
    campaign_id: int,
    lead_id: int,
) -> Step | None:
    """Return the step the lead will run next, or None when it has run out."""
    record = require_campaign_lead(conn, campaign_id, lead_id)
    return find_step_at_ord(conn, campaign_id, record.current_step_ord)


def _terminal(
    conn: sqlite3.Connection,
    campaign_id: int,
    lead_id: int,
    target: Sublist,
    *,
    outcome: str,
    job_state: JobState,
    now: datetime | str | None = None,
    attempts: int | None = None,
) -> CampaignLead:
    moment = now_timestamp(now)
    with transaction(conn):
        record = require_campaign_lead(conn, campaign_id, lead_id)
        _guard(record, target)
        jobs_module.close_open_jobs(conn, campaign_id, lead_id, job_state, error=outcome)
        _write_lead_state(
            conn,
            campaign_id,
            lead_id,
            sublist=target,
            current_step_ord=record.current_step_ord,
            next_run_at=None,
            attempts=record.attempts if attempts is None else attempts,
            last_outcome=outcome,
            updated_at=moment,
        )
        return require_campaign_lead(conn, campaign_id, lead_id)


def _requeue(
    conn: sqlite3.Connection,
    campaign_id: int,
    lead_id: int,
    *,
    step_ord: int,
    due_at: str,
    attempts: int,
    outcome: str,
    closing_state: JobState,
    updated_at: str,
) -> CampaignLead:
    """Park a lead back in `queue` on one step and give it exactly one open job.

    Called only from inside an open transaction. The old job is closed before the
    new one is written, so the one-open-job-per-lead index is never violated even
    though both statements touch the same lead.
    """
    campaign = require_campaign(conn, campaign_id)
    step = step_at_ord(conn, campaign_id, step_ord)
    jobs_module.close_open_jobs(conn, campaign_id, lead_id, closing_state, error=outcome)
    _write_lead_state(
        conn,
        campaign_id,
        lead_id,
        sublist=Sublist.QUEUE,
        current_step_ord=step_ord,
        next_run_at=due_at,
        attempts=attempts,
        last_outcome=outcome,
        updated_at=updated_at,
    )
    jobs_module.insert_job(
        conn,
        JobSpec(
            campaign_id=campaign_id,
            lead_id=lead_id,
            step_id=step.id,
            account_id=campaign.account_id,
            action_type=step.action_type,
            payload_json=jobs_module.step_payload(step.ord),
            scheduled_for=due_at,
            priority=step.priority,
        ),
    )
    return require_campaign_lead(conn, campaign_id, lead_id)


def claim_step(
    conn: sqlite3.Connection,
    campaign_id: int,
    lead_id: int,
    *,
    worker_id: str,
    now: datetime | str | None = None,
) -> CampaignLead:
    """Move a lead from `queue` to `processing` and lease its job.

    Both writes are one transaction, so there is no instant at which the lead
    looks claimed but the job looks free, or the other way round. `BEGIN
    IMMEDIATE` inside :func:`transaction` also means two workers racing for the
    same lead serialise, and the loser sees `processing` and moves on.
    """
    moment = now_timestamp(now)
    with transaction(conn):
        record = require_campaign_lead(conn, campaign_id, lead_id)
        # Claiming is the one transition that is not idempotent: a lead already in
        # `processing` is held by somebody, and letting a second worker re-claim it
        # is exactly the double-send this lease exists to prevent.
        if record.sublist != Sublist.QUEUE.value:
            raise InvalidTransitionError(
                campaign_id, lead_id, record.sublist, Sublist.PROCESSING.value
            )
        job = jobs_module.open_job_for_lead(conn, campaign_id, lead_id)
        if job is None or job.state != JobState.PENDING.value:
            raise InvalidTransitionError(
                campaign_id, lead_id, record.sublist, Sublist.PROCESSING.value
            )
        jobs_module.lease_job(conn, job.id, worker_id, now=moment)
        _write_lead_state(
            conn,
            campaign_id,
            lead_id,
            sublist=Sublist.PROCESSING,
            current_step_ord=record.current_step_ord,
            next_run_at=record.next_run_at,
            attempts=record.attempts,
            last_outcome="claimed",
            updated_at=moment,
        )
        return require_campaign_lead(conn, campaign_id, lead_id)


def _advance(
    conn: sqlite3.Connection,
    campaign_id: int,
    lead_id: int,
    *,
    now: datetime | str | None,
    outcome: str,
) -> CampaignLead:
    """Move a lead onto the next step, or finish its sequence.

    Shared by :func:`complete_step`, a matching filter and a duplicate-action
    refusal. It enforces the state machine but not the lease, because a refusal
    can legitimately resolve a step the lead was still queued for.
    """
    moment = now_timestamp(now)
    with transaction(conn):
        record = require_campaign_lead(conn, campaign_id, lead_id)
        following = next_step_ord(conn, campaign_id, record.current_step_ord)
        if following is None:
            _guard(record, Sublist.SUCCESSFUL)
            end = last_step_ord(conn, campaign_id) or record.current_step_ord
            jobs_module.close_open_jobs(
                conn, campaign_id, lead_id, JobState.DONE, error=None
            )
            _write_lead_state(
                conn,
                campaign_id,
                lead_id,
                sublist=Sublist.SUCCESSFUL,
                # Past the end, so `current_step_ord` can never be mistaken for a
                # step that is still due.
                current_step_ord=end + 1,
                next_run_at=None,
                attempts=0,
                last_outcome="sequence_complete",
                updated_at=moment,
            )
            return require_campaign_lead(conn, campaign_id, lead_id)

        _guard(record, Sublist.QUEUE)
        step = step_at_ord(conn, campaign_id, following)
        due_at = shift_timestamp(moment, step.delay_seconds) if step.delay_seconds else moment
        return _requeue(
            conn,
            campaign_id,
            lead_id,
            step_ord=following,
            due_at=due_at,
            attempts=0,
            outcome=outcome,
            closing_state=JobState.DONE,
            updated_at=moment,
        )


def complete_step(
    conn: sqlite3.Connection,
    campaign_id: int,
    lead_id: int,
    *,
    now: datetime | str | None = None,
    outcome: str = "success",
    worker_id: str | None = None,
) -> CampaignLead:
    """Finish the current step and schedule the next one, or finish the sequence.

    The lead must be `processing`, and when `worker_id` is given it must still
    hold the lease. Pass the same `worker_id` here as to :func:`claim_step`; that
    is what stops a stalled worker finishing a step somebody else has taken over.

    The next step's `delay_seconds` is applied here rather than being a step of
    its own, so waiting costs no queue row.
    """
    moment = now_timestamp(now)
    with transaction(conn):
        record = require_campaign_lead(conn, campaign_id, lead_id)
        _require_lease(conn, record, worker_id)
        return _advance(conn, campaign_id, lead_id, now=moment, outcome=outcome)


def fail_step(
    conn: sqlite3.Connection,
    campaign_id: int,
    lead_id: int,
    *,
    error: str,
    now: datetime | str | None = None,
    worker_id: str | None = None,
) -> CampaignLead:
    """Record a failed step and apply the step's `on_failure` policy.

    Fenced the same way as :func:`complete_step`: the lead must be `processing`,
    and a `worker_id` must still hold the lease.

    `retry` re-queues with an exponential backoff until `max_attempts`, then
    falls through to `failed`. `skip` and `fail` leave the flow immediately, into
    `skipped` and `failed` respectively.
    """
    moment = now_timestamp(now)
    with transaction(conn):
        record = require_campaign_lead(conn, campaign_id, lead_id)
        _require_lease(conn, record, worker_id)
        step = step_at_ord(conn, campaign_id, record.current_step_ord)
        attempts = record.attempts + 1

        if step.on_failure == ON_FAILURE_SKIP:
            return _terminal(
                conn,
                campaign_id,
                lead_id,
                Sublist.SKIPPED,
                outcome=f"step_failed: {error}",
                job_state=JobState.FAILED,
                now=moment,
                attempts=attempts,
            )
        if step.on_failure == ON_FAILURE_FAIL or attempts >= step.max_attempts:
            return _terminal(
                conn,
                campaign_id,
                lead_id,
                Sublist.FAILED,
                outcome=f"step_failed: {error}",
                job_state=JobState.FAILED,
                now=moment,
                attempts=attempts,
            )

        _guard(record, Sublist.QUEUE)
        backoff = step.retry_backoff_seconds * (2 ** (attempts - 1))
        return _requeue(
            conn,
            campaign_id,
            lead_id,
            step_ord=record.current_step_ord,
            due_at=shift_timestamp(moment, backoff),
            attempts=attempts,
            outcome=f"retry_after_failure: {error}",
            closing_state=JobState.FAILED,
            updated_at=moment,
        )


def apply_filter_step(
    conn: sqlite3.Connection,
    campaign_id: int,
    lead_id: int,
    *,
    matched: bool,
    now: datetime | str | None = None,
    worker_id: str | None = None,
) -> CampaignLead:
    """Resolve a filter step: continue the sequence, or drop out of it.

    This is the branching mechanism, and it is deliberately not a branch. A match
    is exactly the same transition as completing any other step. A no-match sends
    the lead to `skipped` or `excluded` and the sequence is over for it. Nothing
    forks, so no second path ever has to be scheduled or reconciled.

    Fenced like :func:`complete_step`, because resolving a filter advances the
    lead just as finishing any other step does.
    """
    moment = now_timestamp(now)
    with transaction(conn):
        record = require_campaign_lead(conn, campaign_id, lead_id)
        _require_lease(conn, record, worker_id)
        step = step_at_ord(conn, campaign_id, record.current_step_ord)
        if matched:
            return _advance(
                conn, campaign_id, lead_id, now=moment, outcome="filter_matched"
            )
        target = step.no_match_sublist
        return _terminal(
            conn,
            campaign_id,
            lead_id,
            target,
            outcome=f"filter_no_match: {step.filter_name}",
            job_state=JobState.CANCELLED,
            now=moment,
        )


def refuse_step(
    conn: sqlite3.Connection,
    campaign_id: int,
    lead_id: int,
    *,
    reason: RefusalReason | str,
    now: datetime | str | None = None,
    retry_after: int | None = None,
) -> CampaignLead:
    """Apply a CORE-03 safety refusal to the lead that was going to run the step.

    The reason is mapped through
    :data:`linkedin_mcp.sequences.states.REFUSAL_DISPOSITIONS` rather than
    interpreted here, so the sequence engine and the safety gate share one
    vocabulary for why an action did not happen.
    """
    typed = reason if isinstance(reason, RefusalReason) else RefusalReason(reason)
    disposition = disposition_for(typed)
    moment = now_timestamp(now)
    outcome = f"refused: {typed.value}"

    with transaction(conn):
        record = require_campaign_lead(conn, campaign_id, lead_id)

        if disposition is Disposition.EXCLUDE:
            return _terminal(
                conn,
                campaign_id,
                lead_id,
                Sublist.EXCLUDED,
                outcome=outcome,
                job_state=JobState.REFUSED,
                now=moment,
            )
        if disposition is Disposition.SKIP:
            return _terminal(
                conn,
                campaign_id,
                lead_id,
                Sublist.SKIPPED,
                outcome=outcome,
                job_state=JobState.REFUSED,
                now=moment,
            )
        if disposition is Disposition.FAIL:
            return _terminal(
                conn,
                campaign_id,
                lead_id,
                Sublist.FAILED,
                outcome=outcome,
                job_state=JobState.REFUSED,
                now=moment,
            )
        if disposition is Disposition.ADVANCE:
            return _advance(conn, campaign_id, lead_id, now=moment, outcome=outcome)

        _guard(record, Sublist.QUEUE)
        wait = retry_after if retry_after is not None else retry_after_seconds(typed)
        return _requeue(
            conn,
            campaign_id,
            lead_id,
            step_ord=record.current_step_ord,
            due_at=shift_timestamp(moment, wait),
            # A refusal is the gate declining, not the step failing, so the
            # attempt budget is untouched. Otherwise a week of cap refusals would
            # exhaust the retries of a step that never ran once.
            attempts=record.attempts,
            outcome=outcome,
            closing_state=JobState.REFUSED,
            updated_at=moment,
        )


def mark_replied(
    conn: sqlite3.Connection,
    campaign_id: int,
    lead_id: int,
    *,
    now: datetime | str | None = None,
    detail: str | None = None,
) -> CampaignLead:
    """Stop the sequence because the lead answered.

    This is the transition SEQ-03 (#21) calls when its inbox scan finds an
    inbound message. It fires from `queue` or `processing`, because a reply
    arrives whenever it arrives, and it cancels the open job so a message already
    queued for tonight does not go out on top of the reply.
    """
    outcome = REPLIED_OUTCOME if not detail else f"{REPLIED_OUTCOME}: {detail}"
    return _terminal(
        conn,
        campaign_id,
        lead_id,
        Sublist.REPLIED,
        outcome=outcome,
        job_state=JobState.CANCELLED,
        now=now,
    )


def skip_lead(
    conn: sqlite3.Connection,
    campaign_id: int,
    lead_id: int,
    *,
    reason: str,
    now: datetime | str | None = None,
) -> CampaignLead:
    """Drop a lead out of the flow softly. It may be enrolled again later."""
    return _terminal(
        conn,
        campaign_id,
        lead_id,
        Sublist.SKIPPED,
        outcome=f"skipped: {reason}",
        job_state=JobState.CANCELLED,
        now=now,
    )


def exclude_lead(
    conn: sqlite3.Connection,
    campaign_id: int,
    lead_id: int,
    *,
    reason: str,
    now: datetime | str | None = None,
) -> CampaignLead:
    """Bar a lead from this campaign permanently. There is no transition out."""
    return _terminal(
        conn,
        campaign_id,
        lead_id,
        Sublist.EXCLUDED,
        outcome=f"excluded: {reason}",
        job_state=JobState.CANCELLED,
        now=now,
    )


def reset_lead(
    conn: sqlite3.Connection,
    campaign_id: int,
    lead_id: int,
    *,
    to_ord: int | None = None,
    now: datetime | str | None = None,
) -> CampaignLead:
    """Send a terminal lead back into the flow at a step.

    Works from `successful`, `failed`, `replied` and `skipped`. It does not work
    from `excluded`, which raises :class:`InvalidTransitionError`: that is the
    behavioural difference between the two exits a filter can take.
    """
    moment = now_timestamp(now)
    with transaction(conn):
        record = require_campaign_lead(conn, campaign_id, lead_id)
        _guard(record, Sublist.QUEUE)
        target_ord = to_ord if to_ord is not None else 1
        return _requeue(
            conn,
            campaign_id,
            lead_id,
            step_ord=target_ord,
            due_at=moment,
            attempts=0,
            outcome="reset",
            closing_state=JobState.CANCELLED,
            updated_at=moment,
        )


def recover_stranded(
    conn: sqlite3.Connection,
    campaign_id: int,
    *,
    now: datetime | str | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> tuple[int, ...]:
    """Return leads whose worker died mid-step to `queue`, and say which.

    Atomic transitions guarantee a lead is never half-moved. They cannot
    guarantee anything about a lead whose move into `processing` committed and
    whose worker then died, because that write was supposed to survive. This
    sweep is the answer to that case: a lead in `processing` is stranded when its
    lease has expired, or when it holds no open job at all, and either way it goes
    back to `queue` on the same step with its attempt count intact.

    Run it on startup and on a timer. It is safe to run concurrently with live
    workers because a lease younger than `lease_seconds` is left alone.
    """
    moment = now_timestamp(now)
    cutoff = shift_timestamp(moment, -abs(lease_seconds))
    recovered: list[int] = []

    with transaction(conn):
        require_campaign(conn, campaign_id)
        rows = conn.execute(
            """
            SELECT cl.lead_id, cl.current_step_ord, cl.attempts,
                   j.id AS job_id, j.state AS job_state, j.locked_at AS locked_at
            FROM campaign_leads AS cl
            LEFT JOIN jobs AS j
                ON j.campaign_id = cl.campaign_id
               AND j.lead_id = cl.lead_id
               AND j.state IN (?, ?)
            WHERE cl.campaign_id = ? AND cl.sublist = ?
            ORDER BY cl.lead_id
            """,
            (
                JobState.PENDING.value,
                JobState.LEASED.value,
                campaign_id,
                Sublist.PROCESSING.value,
            ),
        ).fetchall()

        for row in rows:
            locked_at = row["locked_at"]
            has_open_job = row["job_id"] is not None
            lease_live = has_open_job and locked_at is not None and locked_at > cutoff
            if lease_live:
                continue
            lead_id = int(row["lead_id"])
            _requeue(
                conn,
                campaign_id,
                lead_id,
                step_ord=int(row["current_step_ord"]),
                due_at=moment,
                attempts=int(row["attempts"]),
                outcome=RECOVERED_OUTCOME,
                closing_state=JobState.CANCELLED,
                updated_at=moment,
            )
            recovered.append(lead_id)

    return tuple(recovered)


def recover_all_stranded(
    conn: sqlite3.Connection,
    *,
    now: datetime | str | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> dict[int, tuple[int, ...]]:
    """Sweep every campaign that currently has a lead in `processing`."""
    rows = conn.execute(
        "SELECT DISTINCT campaign_id FROM campaign_leads WHERE sublist = ? ORDER BY campaign_id",
        (Sublist.PROCESSING.value,),
    ).fetchall()
    swept: dict[int, tuple[int, ...]] = {}
    for row in rows:
        campaign_id = int(row["campaign_id"])
        recovered = recover_stranded(
            conn, campaign_id, now=now, lease_seconds=lease_seconds
        )
        if recovered:
            swept[campaign_id] = recovered
    return swept


def sublists_of(records: Sequence[CampaignLead]) -> dict[str, int]:
    """Count a list of state machine rows by sub-list."""
    counts = {member.value: 0 for member in Sublist}
    for record in records:
        counts[coerce_sublist(record.sublist).value] += 1
    return counts
