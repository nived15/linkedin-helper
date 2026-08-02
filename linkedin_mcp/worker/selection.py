"""What the tick loop is allowed to run next, and in what order.

Two lanes, because the queue holds two different shapes of work
--------------------------------------------------------------
:func:`linkedin_mcp.sequences.jobs.due_jobs` inner-joins `campaigns` and
`campaign_leads`. That is deliberate and correct for campaign work: a paused
campaign or a lead that replied since the job was written yields nothing even if
the queue was never swept. It also means `due_jobs` returns **only** campaign
work, and a job with no campaign is invisible to it.

MCP-02 (#25) enqueues harvests, and a harvest has no campaign, no lead and no
step. `insert_job` accepts NULL for all three and `0003`'s partial index
deliberately excludes NULLs so ad-hoc rows stay unconstrained, so those rows are
perfectly legal and `due_jobs` returns none of them. A runner that selected work
only through `due_jobs` would leave every harvest in the table forever.

So this module adds a second selection path rather than changing the first, and
the runner merges them:

``campaign_id IS NOT NULL AND lead_id IS NOT NULL``
    Campaign work. Selected by `due_jobs`, driven through the SEQ-01 state
    machine. Its lease is `claim_step` and its lock is `campaign_leads.sublist`.

``campaign_id IS NULL``
    Ad-hoc work: harvests, one-off visits, anything MCP-02 enqueues. `lead_id`
    may be set, because the safety gate's blacklist and dedupe checks want it.
    There is no state machine row, so the lease is the job row itself.

    A job with no lead at all also has nothing for the gate's dedupe window to
    key on, so a harvest that runs twice is not caught by it. That is deliberate
    and harmless: re-reading a search page costs a `profile_search` from the
    budget and nothing else. Anything whose repeat would be visible to another
    person names a lead.

``campaign_id IS NOT NULL AND lead_id IS NULL``
    Neither shape. No `campaign_leads` row exists to advance and no state machine
    can be driven, so executing it would be guessing. These are counted and
    reported by `worker_status` rather than executed or silently dropped.

Bottom-up priority
------------------
Leads deepest in their sequence go first. A lead on step four is a conversation
already started and the least polite thing an outreach tool can do is open a
hundred new ones while leaving those hanging. It is also what keeps a campaign's
funnel draining rather than widening. `due_jobs` orders by `priority DESC`, which
this layer re-sorts rather than replaces, because changing that read's ordering
would change campaign semantics that other callers depend on.

Bunching
--------
`campaign_steps.bunch_size` says how many leads of one step run back to back.
Consecutive same-step jobs are grouped into a :class:`Bunch` and the rest wait
for the next tick, so the worker performs one kind of action for a while instead
of hopping between profile pages, invites and messages at random.

A known limit
-------------
Both orderings are applied to :data:`SELECTION_WINDOW` rows per lane rather than
to the whole queue, and the runner's closed-hours filter is applied after that
window rather than inside the SQL. `due_jobs` takes no action-type parameter and
its semantics are load bearing for campaign correctness, so narrowing it is not
an option here. In practice a per-tick budget in the tens against a window of two
hundred makes the window the whole due set; a queue with two hundred metered jobs
ahead of a local one would defer that local job until the metered ones drain.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from linkedin_mcp.sequences import (
    DEFAULT_LEASE_SECONDS,
    Job,
    JobState,
    Step,
    due_jobs,
    find_step_at_ord,
    now_timestamp,
    shift_timestamp,
    transaction,
)
from linkedin_mcp.sequences.jobs import job_row

__all__ = [
    "AD_HOC_ORD",
    "SELECTION_WINDOW",
    "Bunch",
    "Selection",
    "ad_hoc_due_jobs",
    "bunch_jobs",
    "is_ad_hoc",
    "is_campaign_work",
    "is_unroutable",
    "job_step",
    "reclaim_stranded_ad_hoc",
    "select_due_jobs",
    "sort_key",
    "unroutable_open_jobs",
]

SELECTION_WINDOW = 200
"""How many due rows per lane the bottom-up sort is applied to.

Bottom-up ordering is a property of the jobs a tick considers, not of the whole
queue, and a tick that read a hundred thousand rows to pick ten would be a
different kind of problem. Two hundred is an order of magnitude above any sane
per-tick budget, so in practice the window is the whole due set.
"""

AD_HOC_ORD = 0
"""Depth a job with no step counts as for bottom-up ordering.

Zero is above every real ord, so ad-hoc work sorts last within its own lane and
never displaces a lead that is mid-sequence. The two lanes have separate budgets
anyway, so this only orders ad-hoc jobs among themselves.
"""


def is_campaign_work(job: Job) -> bool:
    """True when this job drives a `campaign_leads` state machine row."""
    return job.campaign_id is not None and job.lead_id is not None


def is_ad_hoc(job: Job) -> bool:
    """True when this job belongs to no campaign and runs on its own."""
    return job.campaign_id is None


def is_unroutable(job: Job) -> bool:
    """True when the job is neither campaign work nor ad-hoc work.

    A campaign id without a lead id names a state machine row that does not
    exist. Executing it would mean inventing a lead, so the runner reports it
    instead.
    """
    return job.campaign_id is not None and job.lead_id is None


def ad_hoc_due_jobs(
    conn: sqlite3.Connection,
    account_id: int,
    *,
    now: datetime | str | None = None,
    limit: int = 50,
) -> list[Job]:
    """Return pending campaign-less jobs an account may run right now.

    The complement of :func:`linkedin_mcp.sequences.jobs.due_jobs`, and the
    reason a harvest enqueued by MCP-02 (#25) ever runs. There is no campaign to
    check the status of and no lead sub-list to check, so the only conditions are
    the account, the state and the clock. The safety gate still decides whether
    the action may happen; this only decides that the row is a candidate.
    """
    moment = now_timestamp(now)
    rows = conn.execute(
        """
        SELECT * FROM jobs
        WHERE account_id = ?
          AND state = ?
          AND scheduled_for <= ?
          AND campaign_id IS NULL
        ORDER BY priority DESC, scheduled_for, id
        LIMIT ?
        """,
        (account_id, JobState.PENDING.value, moment, max(0, int(limit))),
    ).fetchall()
    return [job_row(row) for row in rows]


def unroutable_open_jobs(
    conn: sqlite3.Connection,
    account_id: int | None = None,
) -> list[Job]:
    """Return open jobs whose shape neither lane can execute.

    Reported rather than executed. Silence here is how a queue quietly grows a
    pile of work nobody runs, which is the exact failure this module exists to
    prevent for harvests.
    """
    sql = """
        SELECT * FROM jobs
        WHERE state IN (?, ?)
          AND campaign_id IS NOT NULL
          AND lead_id IS NULL
    """
    params: list[Any] = [JobState.PENDING.value, JobState.LEASED.value]
    if account_id is not None:
        sql += " AND account_id = ?"
        params.append(account_id)
    sql += " ORDER BY id"
    return [job_row(row) for row in conn.execute(sql, params).fetchall()]


def reclaim_stranded_ad_hoc(
    conn: sqlite3.Connection,
    *,
    account_id: int | None = None,
    now: datetime | str | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> tuple[int, ...]:
    """Return ad-hoc jobs whose worker died back to `pending`, and say which.

    `recover_stranded` sweeps `campaign_leads`, so it cannot see a job that has
    no lead. An ad-hoc job leased by a worker that then died would otherwise stay
    `leased` forever, which is precisely the stranding the lease was supposed to
    make impossible. A lease younger than `lease_seconds` is left alone, so this
    is safe to run beside live workers.

    `attempts` is left as the dead worker incremented it. A harvest that keeps
    killing its worker should not retry forever unnoticed, and the attempt count
    is the only evidence of that.
    """
    moment = now_timestamp(now)
    cutoff = shift_timestamp(moment, -abs(lease_seconds))
    sql = """
        SELECT id FROM jobs
        WHERE state = ?
          AND campaign_id IS NULL
          AND (locked_at IS NULL OR locked_at <= ?)
    """
    params: list[Any] = [JobState.LEASED.value, cutoff]
    if account_id is not None:
        sql += " AND account_id = ?"
        params.append(account_id)
    sql += " ORDER BY id"

    with transaction(conn):
        stranded = [int(row["id"]) for row in conn.execute(sql, params).fetchall()]
        for job_id in stranded:
            conn.execute(
                """
                UPDATE jobs
                SET state = ?, locked_by = NULL, locked_at = NULL,
                    last_error = ?
                WHERE id = ? AND state = ?
                """,
                (
                    JobState.PENDING.value,
                    "reclaimed_stale_lease",
                    job_id,
                    JobState.LEASED.value,
                ),
            )
    return tuple(stranded)


def job_step(conn: sqlite3.Connection, job: Job) -> Step | None:
    """Return the step a job points at, or None when it has none.

    Ad-hoc jobs have no step. A campaign job whose step was deleted under it also
    has none, and that is a real state: `rebuild_jobs` clears the pointer rather
    than deleting history.
    """
    if job.campaign_id is None:
        return None
    step_ord = job.payload.get("step_ord")
    if step_ord is None:
        return None
    return find_step_at_ord(conn, job.campaign_id, int(step_ord))


def sort_key(job: Job, step: Step | None) -> tuple[int, int, str, int]:
    """Return the bottom-up ordering key for one job.

    Deepest step first, then the step's own priority, then the longest overdue,
    then insertion order so the sort is total and therefore deterministic.
    """
    ord_value = AD_HOC_ORD if step is None else step.ord
    return (-ord_value, -job.priority, job.scheduled_for, job.id)


@dataclass(frozen=True, slots=True)
class Bunch:
    """Consecutive jobs of one step, run back to back.

    Ad-hoc jobs are bunches of one: they share no step, so there is nothing to
    batch them by.
    """

    action_type: str
    jobs: tuple[Job, ...]
    campaign_id: int | None = None
    step_id: int | None = None
    step: Step | None = None

    def __len__(self) -> int:
        return len(self.jobs)


def bunch_jobs(
    conn: sqlite3.Connection,
    jobs: Iterable[Job],
    *,
    limit: int | None = None,
) -> list[Bunch]:
    """Order jobs bottom-up and group consecutive same-step runs into bunches.

    `limit` caps the total number of jobs returned, not the number of bunches, so
    a caller with a per-tick action budget gets exactly that many actions.
    """
    decorated = [(job, job_step(conn, job)) for job in jobs]
    decorated.sort(key=lambda pair: sort_key(pair[0], pair[1]))

    bunches: list[Bunch] = []
    taken = 0
    index = 0
    while index < len(decorated):
        if limit is not None and taken >= limit:
            break
        job, step = decorated[index]
        key = (job.campaign_id, job.step_id, job.action_type)
        # A step with no bunch_size, or an ad-hoc job with no step at all, runs
        # one at a time. That is the conservative reading: bunching is an
        # optimisation and guessing a larger batch would be a behaviour change.
        capacity = 1 if step is None else max(1, int(step.bunch_size))
        if limit is not None:
            capacity = min(capacity, limit - taken)

        members: list[Job] = []
        while index < len(decorated) and len(members) < capacity:
            candidate, candidate_step = decorated[index]
            if (
                candidate.campaign_id,
                candidate.step_id,
                candidate.action_type,
            ) != key:
                break
            members.append(candidate)
            index += 1

        taken += len(members)
        bunches.append(
            Bunch(
                action_type=job.action_type,
                jobs=tuple(members),
                campaign_id=job.campaign_id,
                step_id=job.step_id,
                step=step,
            )
        )
    return bunches


@dataclass(frozen=True, slots=True)
class Selection:
    """What one tick may run, split by lane so neither can starve the other."""

    campaign: tuple[Bunch, ...] = ()
    ad_hoc: tuple[Bunch, ...] = ()
    unroutable: tuple[Job, ...] = ()
    skipped_metered: tuple[Job, ...] = ()

    @property
    def bunches(self) -> tuple[Bunch, ...]:
        """Every bunch in execution order: campaign work first, then ad-hoc."""
        return self.campaign + self.ad_hoc

    def jobs(self) -> tuple[Job, ...]:
        return tuple(job for bunch in self.bunches for job in bunch.jobs)

    def __len__(self) -> int:
        return len(self.jobs())


def select_due_jobs(
    conn: sqlite3.Connection,
    account_id: int,
    *,
    now: datetime | str | None = None,
    campaign_limit: int = 10,
    ad_hoc_limit: int = 5,
    campaign_id: int | None = None,
    runnable: Sequence[str] | None = None,
) -> Selection:
    """Return the work one tick may run, from both lanes, bottom-up and bunched.

    The two lanes carry separate budgets rather than competing for one. A
    campaign with a thousand due leads would otherwise use every slot for weeks
    and no harvest would ever run, and a huge harvest backlog would otherwise
    stall live outreach. Neither is acceptable, and a shared budget makes one of
    them inevitable.

    `runnable` narrows both lanes to a set of action types, which is how the
    runner drops metered work outside working hours while still letting local
    steps such as filters proceed.
    """
    moment = now_timestamp(now)
    allowed = None if runnable is None else set(runnable)
    skipped: list[Job] = []

    def permitted(candidates: list[Job]) -> list[Job]:
        if allowed is None:
            return candidates
        kept = []
        for job in candidates:
            if job.action_type in allowed:
                kept.append(job)
            else:
                skipped.append(job)
        return kept

    # Over-fetch, because bottom-up is not `due_jobs`'s ordering: taking the
    # first N rows in queue order and re-sorting them would sort the wrong N.
    # The window is bounded rather than unlimited, so with more due jobs than
    # `SELECTION_WINDOW` the ordering is bottom-up within a window rather than
    # across the whole queue. At a per-tick budget in the tens against a window
    # of two hundred that costs nothing, and an unbounded read on a queue of a
    # hundred thousand rows would cost a great deal.
    campaign_pool = permitted(
        due_jobs(
            conn,
            account_id,
            now=moment,
            campaign_id=campaign_id,
            limit=max(SELECTION_WINDOW, campaign_limit),
        )
    )
    ad_hoc_pool: list[Job] = []
    if campaign_id is None:
        ad_hoc_pool = permitted(
            ad_hoc_due_jobs(
                conn,
                account_id,
                now=moment,
                limit=max(SELECTION_WINDOW, ad_hoc_limit),
            )
        )

    return Selection(
        campaign=tuple(bunch_jobs(conn, campaign_pool, limit=campaign_limit)),
        ad_hoc=tuple(bunch_jobs(conn, ad_hoc_pool, limit=ad_hoc_limit)),
        unroutable=tuple(unroutable_open_jobs(conn, account_id)),
        skipped_metered=tuple(skipped),
    )
