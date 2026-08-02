"""The tick loop: the only thing in this repository that drives LinkedIn.

The architecture in one sentence
-------------------------------
The MCP server writes rows and reads rows; this loop is what turns rows into
actions. That is why no MCP tool has to open a browser, and why the whole system
keeps working with no LLM attached to it.

One tick
--------
1. **Sweep.** `recover_all_stranded` returns leads whose worker died back to
   `queue`, and :func:`~linkedin_mcp.worker.selection.reclaim_stranded_ad_hoc`
   does the same for jobs that have no lead. Both are lease-age based, so a live
   worker's work is never taken out from under it.
2. **Select.** :func:`~linkedin_mcp.worker.selection.select_due_jobs` returns
   campaign work bottom-up and bunched, plus ad-hoc work, each with its own
   budget.
3. **Execute.** Claim, ask the gate, run the executor, log to `actions_log`, land
   exactly one state machine transition.
4. **Report.** The heartbeat is written before each of those, never after.

Why the gate is asked after the claim
-------------------------------------
Claiming first means the refusal is recorded against a lead that is demonstrably
this worker's, and `refuse_step` can re-queue it with the reason's own delay.
Asking first and claiming afterwards would leave a window in which two workers
both hold permission for the same lead, which is the double send this design
exists to prevent.

Why the transaction is not held across the action
-------------------------------------------------
`claim_step` commits before the executor runs. Holding SQLite's write lock open
across a Playwright call would block every other writer for as long as LinkedIn
takes to answer, and LinkedIn sometimes takes a very long time to answer. The
lease, not the transaction, is what makes the claim exclusive.

What does and does not reach `actions_log`
------------------------------------------
Every action that reaches LinkedIn is logged. Steps in
:data:`~linkedin_mcp.sequences.steps.LOCAL_ACTIONS` are not, and that is a
deliberate correctness decision rather than an omission: `actions_log` is also
the ledger the safety gate counts, `metered_universe` excludes only
`UNMETERED_ACTIONS`, and a `success` row for a filter would therefore spend the
account's global daily and hourly ceilings on a step that touched nothing. A
campaign with a filter in front of every message would quietly halve its own
sending capacity. The filter's verdict is not lost: SEQ-01 writes it to
`campaign_leads.last_outcome`, which is where a lead's own history belongs.

The LLM is never on the path
----------------------------
When a step needs generated text, the runner parks an `ai_drafts` row through the
injected parker and refuses the step with `APPROVAL_REQUIRED`, which SEQ-01 maps
to "wait an hour, then try again". Nothing awaits a model, and a worker with no
drafts package at all still parks the lead cleanly instead of sending unapproved
content.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from linkedin_mcp.audit import Outcome, RefusalReason, log_action
from linkedin_mcp.browser.humanize import cooldown
from linkedin_mcp.safety import (
    guard_action,
    is_within_working_hours,
    working_windows,
)
from linkedin_mcp.safety.limits import local_weekday_and_minute, resolve_timezone
from linkedin_mcp.sequences import (
    DEFAULT_LEASE_SECONDS,
    FILTER_ACTION,
    LOCAL_ACTIONS,
    Campaign,
    Disposition,
    InvalidTransitionError,
    Job,
    JobState,
    SequenceError,
    Step,
    Sublist,
    apply_filter_step,
    claim_step,
    complete_step,
    current_step,
    disposition_for,
    evaluate_filter,
    fail_step,
    get_campaign,
    recover_all_stranded,
    refuse_step,
    retry_after_seconds,
    shift_timestamp,
    skip_lead,
    sublist_counts,
    transaction,
    utc_now,
)
from linkedin_mcp.sequences.jobs import lease_job, open_job_for_lead
from linkedin_mcp.worker.actions import (
    ActionContext,
    ActionRegistry,
    ActionResult,
    ActionStatus,
    BrowserSupplier,
    DraftKind,
    DraftParker,
    DraftRequest,
    Executor,
    no_browser,
    no_draft_parker,
)
from linkedin_mcp.worker.heartbeat import (
    DEFAULT_STALLED_AFTER_SECONDS,
    STATUS_CLOSED,
    STATUS_ERROR,
    STATUS_IDLE,
    STATUS_PAUSED,
    STATUS_RUNNING,
    STATUS_SELECTING,
    STATUS_STARTING,
    STATUS_STOPPED,
    STATUS_SWEEPING,
    write_heartbeat,
)
from linkedin_mcp.worker.selection import (
    Bunch,
    Selection,
    reclaim_stranded_ad_hoc,
    select_due_jobs,
)
logger = logging.getLogger(__name__)

__all__ = [
    "AD_HOC_MAX_ATTEMPTS",
    "AD_HOC_RETRY_BACKOFF_SECONDS",
    "JobReport",
    "TickReport",
    "Worker",
    "WorkerConfig",
    "build_worker",
    "campaign_funnel",
]

AD_HOC_MAX_ATTEMPTS = 3
"""Attempts an ad-hoc job gets before it is closed as failed.

Campaign work reads this from its step. An ad-hoc job has no step, so it carries
its own `max_attempts` in its payload and otherwise falls back to the number a
step would have defaulted to.
"""

AD_HOC_RETRY_BACKOFF_SECONDS = 900
"""Base wait between ad-hoc attempts, doubled per attempt like a step's."""

MISSING_STEP_REASON = "the step this lead points at no longer exists"

_DRAFT_KINDS: Mapping[str, DraftKind] = {
    "connection_request": DraftKind.CONNECTION_NOTE,
    "message": DraftKind.MESSAGE,
    "post_comment": DraftKind.COMMENT,
    FILTER_ACTION: DraftKind.ICP_EVALUATION,
}

CAMPAIGN_LANE = "campaign"
AD_HOC_LANE = "ad_hoc"


@dataclass(frozen=True, slots=True)
class JobReport:
    """What one job did, in terms a funnel can be built from."""

    job_id: int
    action_type: str
    lane: str
    outcome: str
    campaign_id: int | None = None
    lead_id: int | None = None
    reason: str | None = None
    error: str | None = None
    detail: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TickReport:
    """Everything one tick did. Returned rather than logged, so tests can assert."""

    at: str
    worker_id: str
    account_id: int
    within_working_hours: bool
    jobs: tuple[JobReport, ...] = ()
    recovered_leads: Mapping[int, tuple[int, ...]] = field(default_factory=dict)
    reclaimed_ad_hoc: tuple[int, ...] = ()
    unroutable: tuple[int, ...] = ()
    deferred_metered: tuple[int, ...] = ()
    paused: bool = False
    """MCP-05 (#28): True when the worker pause stopped this tick selecting work.

    A tick that ran nothing because it was paused and a tick that ran nothing
    because nothing was due are both `idle`, and only this tells them apart.
    """

    @property
    def executed(self) -> int:
        return len(self.jobs)

    @property
    def idle(self) -> bool:
        return not self.jobs

    def by_outcome(self, outcome: str) -> tuple[JobReport, ...]:
        return tuple(report for report in self.jobs if report.outcome == outcome)


@dataclass(frozen=True, slots=True)
class WorkerConfig:
    """Everything the loop needs that is not code.

    The two per-tick budgets are separate on purpose. See
    :func:`~linkedin_mcp.worker.selection.select_due_jobs` for why sharing one
    starves whichever lane loses.
    """

    account_id: int
    worker_id: str = ""
    campaign_actions_per_tick: int = 10
    ad_hoc_actions_per_tick: int = 5
    lease_seconds: int = DEFAULT_LEASE_SECONDS
    stalled_after_seconds: int = DEFAULT_STALLED_AFTER_SECONDS
    tick_seconds: float = 30.0
    sweep_every_ticks: int = 10
    campaign_id: int | None = None
    pace_between_actions: bool = True

    def resolved_worker_id(self) -> str:
        return self.worker_id or f"worker-{uuid.uuid4().hex[:12]}"


def campaign_funnel(conn: sqlite3.Connection, campaign_id: int) -> dict[str, int]:
    """Return a campaign's sub-list counts plus the two totals people ask for.

    Thin on purpose. `sublist_counts` is SEQ-01's and already returns all seven
    keys every time; what this adds is the arithmetic nobody should have to redo,
    namely how many leads are still moving and how many have left the flow.
    """
    counts = sublist_counts(conn, campaign_id)
    in_flight = counts[Sublist.QUEUE.value] + counts[Sublist.PROCESSING.value]
    return {
        **counts,
        "in_flight": in_flight,
        "finished": sum(counts.values()) - in_flight,
        "total": sum(counts.values()),
    }


class Worker:
    """One account's tick loop. Owns the browser, the clock and nothing else."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        config: WorkerConfig,
        *,
        registry: ActionRegistry | None = None,
        clock: Callable[[], datetime] | None = None,
        browser_supplier: BrowserSupplier = no_browser,
        draft_parker: DraftParker = no_draft_parker,
    ) -> None:
        self.conn = conn
        self.config = config
        self.worker_id = config.resolved_worker_id()
        # The heartbeat is keyed by the stable `worker_id` an operator chose, so
        # `--status` reads the same name across restarts. Leases are taken under
        # a token unique to this *process*, because a supervisor that restarts a
        # worker with the same `--worker-id` would otherwise give the replacement
        # the dead process's identity: the old process could wake up, match the
        # `locked_by` fence against a lease it never took, and finalise a lead
        # somebody else is mid-way through. That is the double send the fence
        # exists to stop, reintroduced by a naming coincidence.
        self.lease_id = f"{self.worker_id}#{uuid.uuid4().hex[:8]}"
        self.account_id = config.account_id
        self.registry = registry if registry is not None else ActionRegistry()
        self.clock = clock or utc_now
        self._browser_supplier = browser_supplier
        self._draft_parker = draft_parker
        self._browser: Any | None = None
        self._browser_opened = False
        self._ticks = 0
        self._stop = asyncio.Event()

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def now(self) -> datetime:
        """The worker's clock. Injectable, because a weekend cannot be waited for."""
        return self.clock()

    def start(self, *, now: datetime | None = None) -> None:
        """Announce the worker before it does anything.

        Written first so a worker that dies during its very first sweep is still
        visible as one that started and went quiet, rather than as one that never
        existed at all.
        """
        self._heartbeat(STATUS_STARTING, now=now or self.now())

    def stop(self, *, now: datetime | None = None) -> None:
        """Record a clean shutdown, the one silence that is not a fault."""
        self._stop.set()
        self._heartbeat(STATUS_STOPPED, now=now or self.now())

    def request_stop(self) -> None:
        """Ask the loop to finish the job in hand and exit."""
        self._stop.set()

    async def browser(self) -> Any | None:
        """Return the shared browser, building it at most once.

        A worker whose steps are all local never calls this, so running a filter
        never launches Chromium.
        """
        if not self._browser_opened:
            self._browser = await self._browser_supplier()
            self._browser_opened = True
        return self._browser

    def _heartbeat(
        self,
        status: str,
        *,
        now: datetime,
        current_job_id: int | None = None,
    ) -> None:
        write_heartbeat(
            self.conn,
            self.worker_id,
            self.account_id,
            status,
            current_job_id=current_job_id,
            now=now,
        )

    # ------------------------------------------------------------------
    # working hours
    # ------------------------------------------------------------------

    def within_working_hours(self, moment: datetime) -> bool:
        """Ask CORE-03 whether the account is open, in the account's own timezone.

        Not reimplemented here. The gate asks the same question of the same rows
        when it grants a lease, so a disagreement between this and the gate is
        impossible by construction.
        """
        row = self.conn.execute(
            "SELECT timezone FROM accounts WHERE id = ?", (self.account_id,)
        ).fetchone()
        zone = resolve_timezone(None if row is None else row["timezone"])
        weekday, minute = local_weekday_and_minute(moment, zone)
        return is_within_working_hours(
            working_windows(self.conn, self.account_id), weekday, minute
        )

    # ------------------------------------------------------------------
    # the tick
    # ------------------------------------------------------------------

    def _sweep_due(self) -> bool:
        every = max(1, int(self.config.sweep_every_ticks))
        return self._ticks % every == 1 % every

    async def tick(self, *, now: datetime | None = None) -> TickReport:
        """Run one pass and return exactly what it did.

        Passing `now` pins the whole tick to one instant, which is what makes a
        simulated week reproducible. Leaving it out, which is what the daemon
        does, re-reads the clock before every job instead: a bunch of ten
        humanised actions can take minutes, and a tick that started at 16:55
        must not still be handing the safety gate 16:55 at ten past five.
        """
        pinned = now is not None
        moment = now or self.now()
        self._ticks += 1

        recovered: Mapping[int, tuple[int, ...]] = {}
        reclaimed: tuple[int, ...] = ()
        if self._sweep_due():
            self._heartbeat(STATUS_SWEEPING, now=moment)
            recovered = recover_all_stranded(
                self.conn, now=moment, lease_seconds=self.config.lease_seconds
            )
            reclaimed = reclaim_stranded_ad_hoc(
                self.conn,
                account_id=self.account_id,
                now=moment,
                lease_seconds=self.config.lease_seconds,
            )

        open_now = self.within_working_hours(moment)
        self._heartbeat(
            STATUS_SELECTING if open_now else STATUS_CLOSED, now=moment
        )

        # Outside working hours only local steps run. They reach nothing on
        # LinkedIn, so refusing them would stall a sequence on a filter for no
        # safety benefit. Everything else stays in `queue` with its due time
        # already past, so it goes first the moment the account opens: work
        # bunches at the start of the working day, exactly as a person's would.
        selection = select_due_jobs(
            self.conn,
            self.account_id,
            now=moment,
            campaign_limit=self.config.campaign_actions_per_tick,
            ad_hoc_limit=self.config.ad_hoc_actions_per_tick,
            campaign_id=self.config.campaign_id,
            runnable=None if open_now else sorted(LOCAL_ACTIONS),
        )

        reports = await self._run_selection(selection, moment, pinned=pinned)
        self._heartbeat(STATUS_PAUSED if selection.paused else STATUS_IDLE, now=moment)

        return TickReport(
            at=str(moment),
            worker_id=self.worker_id,
            account_id=self.account_id,
            within_working_hours=open_now,
            jobs=tuple(reports),
            recovered_leads=dict(recovered),
            reclaimed_ad_hoc=reclaimed,
            unroutable=tuple(job.id for job in selection.unroutable),
            deferred_metered=tuple(job.id for job in selection.skipped_metered),
            paused=selection.paused,
        )

    async def _run_selection(
        self,
        selection: Selection,
        moment: datetime,
        *,
        pinned: bool,
    ) -> list[JobReport]:
        if selection.unroutable:
            logger.warning(
                "%d open jobs name a campaign but no lead, so no state machine "
                "can be driven for them and they were not executed: %s",
                len(selection.unroutable),
                [job.id for job in selection.unroutable],
            )

        reports: list[JobReport] = []
        acted = False
        for bunch in selection.bunches:
            for job in bunch.jobs:
                if self._stop.is_set():
                    return reports
                reaches_linkedin = job.action_type not in LOCAL_ACTIONS
                if acted and reaches_linkedin and self.config.pace_between_actions:
                    # The only delay in this loop that is about looking human
                    # rather than about scheduling, so it routes through CORE-04
                    # like every other one.
                    await cooldown()
                at = moment if pinned else self.now()
                if reaches_linkedin and not pinned and not self.within_working_hours(at):
                    # The window closed part way through the bunch. The rest of
                    # the queue keeps its already-past due time and goes first
                    # tomorrow, which is better than pushing three more
                    # invitations out after hours because the tick began before
                    # five.
                    logger.info(
                        "stopping the bunch at job %s: the account closed mid-tick",
                        job.id,
                    )
                    return reports
                self._heartbeat(STATUS_RUNNING, now=at, current_job_id=job.id)
                report = await self._run_job(job, bunch, at)
                if report is not None:
                    reports.append(report)
                    acted = acted or reaches_linkedin
        return reports

    async def _run_job(
        self,
        job: Job,
        bunch: Bunch,
        moment: datetime,
    ) -> JobReport | None:
        lane = CAMPAIGN_LANE if job.campaign_id is not None else AD_HOC_LANE
        try:
            if job.campaign_id is not None and job.lead_id is not None:
                return await self._run_campaign_job(job, moment)
            return await self._run_ad_hoc_job(job, moment)
        except Exception as exc:  # noqa: BLE001 - a tick must survive one bad job
            logger.exception("job %s raised outside its own handler", job.id)
            self._heartbeat(STATUS_ERROR, now=moment, current_job_id=job.id)
            return JobReport(
                job_id=job.id,
                action_type=job.action_type,
                lane=lane,
                outcome="error",
                campaign_id=job.campaign_id,
                lead_id=job.lead_id,
                error=str(exc),
            )

    # ------------------------------------------------------------------
    # audit
    # ------------------------------------------------------------------

    def _log(
        self,
        action_type: str,
        outcome: Outcome,
        *,
        moment: datetime,
        lead_id: int | None = None,
        campaign_id: int | None = None,
        step_id: int | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        """Append one `actions_log` row, unless the action never left the machine.

        See the module docstring: a local step's `success` row would spend the
        account's global ceilings on work that touched nothing.
        """
        if action_type in LOCAL_ACTIONS:
            return
        log_action(
            self.account_id,
            action_type,
            outcome,
            lead_id=lead_id,
            campaign_id=campaign_id,
            step_id=step_id,
            detail=dict(detail or {}),
            occurred_at=moment,
        )

    @staticmethod
    def _failure_outcome(reached_linkedin: bool) -> Outcome:
        """Pick the outcome a failed step is logged under.

        `ATTEMPTED_OUTCOMES` is `success` and `failure`, and those are the only
        two the safety caps count. A step that failed because nothing was
        registered to run it, or because the gate could not be reached, never
        touched LinkedIn, so logging it as `failure` would spend the account's
        daily budget on an action that did not happen. An unattended worker with
        no browser drivers would burn thirty invitations a day on nothing.
        `skipped` keeps the row, and therefore the explanation, without the
        arithmetic.
        """
        return Outcome.FAILURE if reached_linkedin else Outcome.SKIPPED

    # ------------------------------------------------------------------
    # campaign lane
    # ------------------------------------------------------------------

    async def _run_campaign_job(self, job: Job, moment: datetime) -> JobReport | None:
        campaign_id = int(job.campaign_id)
        lead_id = int(job.lead_id)

        try:
            claim_step(
                self.conn,
                campaign_id,
                lead_id,
                worker_id=self.lease_id,
                now=moment,
            )
        except (InvalidTransitionError, SequenceError):
            # Somebody else took the lead between the select and the claim. That
            # is the race the lease exists to lose gracefully, not an error.
            logger.debug(
                "lead %s of campaign %s was taken by another worker", lead_id, campaign_id
            )
            return None

        campaign = get_campaign(self.conn, campaign_id)
        # Read the step from the lead rather than from the job payload. The
        # payload is a pointer written when the job was queued; `current_step_ord`
        # is where the lead actually is now, and a rebuild or a step edit can move
        # one without the other.
        step = current_step(self.conn, campaign_id, lead_id)
        if step is None:
            return self._campaign_step_missing(job, campaign_id, lead_id, moment)

        if step.action_type == FILTER_ACTION:
            return self._run_filter(job, campaign_id, lead_id, step, moment)

        if not step.is_local:
            refusal = self._ask_gate(
                step.action_type,
                lead_id=lead_id,
                approved=self._campaign_is_approved(campaign),
                moment=moment,
            )
            if refusal is not None:
                return self._apply_campaign_refusal(
                    job, campaign_id, lead_id, step, refusal, moment
                )

        return await self._execute_campaign_action(
            job, campaign, step, campaign_id, lead_id, moment
        )

    @staticmethod
    def _campaign_is_approved(campaign: Campaign | None) -> bool:
        """Turn a campaign's approval mode into the flag the gate enforces.

        `manual_drafts` means a human signs each message off, and the worker has
        no way to know they did, so it says False and the gate refuses with
        `APPROVAL_REQUIRED`. That refusal is what parks the draft. SEQ-05 (#23)
        is what eventually turns it into a send.
        """
        return campaign is not None and campaign.approval_mode == "auto"

    def _campaign_step_missing(
        self,
        job: Job,
        campaign_id: int,
        lead_id: int,
        moment: datetime,
    ) -> JobReport:
        """Retire a lead whose step was deleted under it.

        `fail_step` and `refuse_step` both look the step up, so neither can be
        used here. `skip_lead` is terminal without touching `campaign_steps`, and
        `skipped` is the honest verdict: the lead did nothing wrong and may be
        enrolled again once the campaign has steps that make sense.
        """
        self._log(
            job.action_type,
            Outcome.SKIPPED,
            moment=moment,
            lead_id=lead_id,
            campaign_id=campaign_id,
            detail={"error": MISSING_STEP_REASON},
        )
        self._fenced(
            campaign_id,
            lead_id,
            lambda: skip_lead(
                self.conn,
                campaign_id,
                lead_id,
                reason=MISSING_STEP_REASON,
                now=moment,
            ),
        )
        return JobReport(
            job_id=job.id,
            action_type=job.action_type,
            lane=CAMPAIGN_LANE,
            outcome="skipped",            campaign_id=campaign_id,
            lead_id=lead_id,
            reason=MISSING_STEP_REASON,
        )

    def _run_filter(
        self,
        job: Job,
        campaign_id: int,
        lead_id: int,
        step: Step,
        moment: datetime,
    ) -> JobReport:
        """Resolve a filter step. Entirely local: no gate, no browser, no model.

        This is why a worker with nothing registered still makes progress, and
        why the ICP step of a campaign resolves at three in the morning without
        touching LinkedIn.
        """
        try:
            matched = evaluate_filter(
                self.conn, self.account_id, campaign_id, lead_id, step
            )
        except Exception as exc:  # noqa: BLE001 - a broken predicate is a step failure
            return self._campaign_failure(
                job, campaign_id, lead_id, step, f"filter raised: {exc}", moment,
                reached_linkedin=False,
            )

        apply_filter_step(
            self.conn,
            campaign_id,
            lead_id,
            matched=matched,
            now=moment,
            worker_id=self.lease_id,
        )
        return JobReport(
            job_id=job.id,
            action_type=job.action_type,
            lane=CAMPAIGN_LANE,
            outcome="filter_matched" if matched else "filter_no_match",
            campaign_id=campaign_id,
            lead_id=lead_id,
            detail={"filter": step.filter_name, "matched": matched},
        )

    async def _execute_campaign_action(
        self,
        job: Job,
        campaign: Campaign | None,
        step: Step,
        campaign_id: int,
        lead_id: int,
        moment: datetime,
    ) -> JobReport:
        if bool(step.config.get("requires_draft", False)):
            return self._park_campaign_draft(
                job, campaign_id, lead_id, step, moment, source="step_config"
            )

        executor = self.registry.get(step.action_type)
        if executor is None:
            # Not an outage and not a crash: this worker simply cannot do this
            # action. The step's own `on_failure` policy decides what that means
            # for the lead, which is how an unattended worker with no browser
            # drivers registered parks a campaign instead of spinning on it.
            return self._campaign_failure(
                job,
                campaign_id,
                lead_id,
                step,
                f"no executor is registered for {step.action_type!r}",
                moment,
                reached_linkedin=False,
            )

        try:
            browser = None if step.is_local else await self.browser()
        except Exception as exc:  # noqa: BLE001 - the browser may fail to start
            # Outside the executor's own handler this would escape into the
            # tick's catch-all, leaving the lead in `processing` until the sweep
            # reclaimed it and never spending an attempt. A browser that fails to
            # start deterministically would repeat that forever.
            logger.exception("the browser could not be started for job %s", job.id)
            return self._campaign_failure(
                job,
                campaign_id,
                lead_id,
                step,
                f"the browser could not be started: {exc}",
                moment,
                reached_linkedin=False,
            )

        context = ActionContext(
            conn=self.conn,
            account_id=self.account_id,
            worker_id=self.worker_id,
            job=job,
            now=moment,
            campaign=campaign,
            step=step,
            lead_id=lead_id,
            browser=browser,
            payload=job.payload,
        )
        try:
            result = await executor(context)
        except Exception as exc:  # noqa: BLE001 - an executor may simply blow up
            logger.exception("executor for %s failed", step.action_type)
            return self._campaign_failure(
                job, campaign_id, lead_id, step, str(exc), moment
            )

        return self._land_campaign_result(
            job, campaign_id, lead_id, step, result, moment
        )

    def _land_campaign_result(
        self,
        job: Job,
        campaign_id: int,
        lead_id: int,
        step: Step,
        result: ActionResult,
        moment: datetime,
    ) -> JobReport:
        if result.status is ActionStatus.NEEDS_DRAFT:
            return self._park_campaign_draft(
                job,
                campaign_id,
                lead_id,
                step,
                moment,
                source="executor",
                kind=result.draft_kind,
                context=result.detail,
            )

        if result.status is ActionStatus.FAILED:
            return self._campaign_failure(
                job,
                campaign_id,
                lead_id,
                step,
                result.error or "the action failed without saying why",
                moment,
                detail=result.detail,
            )

        if result.status is ActionStatus.SKIPPED:
            reason = result.outcome or "the executor skipped this lead"
            self._log(
                step.action_type,
                Outcome.SKIPPED,
                moment=moment,
                lead_id=lead_id,
                campaign_id=campaign_id,
                step_id=step.id,
                detail={**dict(result.detail), "reason": reason},
            )
            self._fenced(
                campaign_id,
                lead_id,
                lambda: skip_lead(
                    self.conn, campaign_id, lead_id, reason=reason, now=moment
                ),
            )
            return JobReport(
                job_id=job.id,
                action_type=job.action_type,
                lane=CAMPAIGN_LANE,
                outcome="skipped",
                campaign_id=campaign_id,
                lead_id=lead_id,
                reason=reason,
                detail=result.detail,
            )

        self._log(
            step.action_type,
            Outcome.SUCCESS,
            moment=moment,
            lead_id=lead_id,
            campaign_id=campaign_id,
            step_id=step.id,
            detail=result.detail,
        )
        # Logged before the transition, and the order is not arbitrary. Dying
        # between the two leaves an action that happened and a lead that did not
        # advance, so the sweep requeues it and the gate's dedupe window refuses
        # the repeat as `DUPLICATE_ACTION`, which advances the lead without a
        # second send. The other order would leave an action that happened and no
        # row saying so, which is invisible to every cap in the system.
        complete_step(
            self.conn,
            campaign_id,
            lead_id,
            now=moment,
            worker_id=self.lease_id,
        )
        return JobReport(
            job_id=job.id,
            action_type=job.action_type,
            lane=CAMPAIGN_LANE,
            outcome="success",
            campaign_id=campaign_id,
            lead_id=lead_id,
            detail=result.detail,
        )

    def _still_holds(self, campaign_id: int, lead_id: int) -> bool:
        """True when this process still owns the lead's open job.

        `complete_step`, `fail_step` and `apply_filter_step` take a `worker_id`
        and fence themselves. `refuse_step` and `skip_lead` do not, because a
        refusal can legitimately resolve a step a lead was merely queued for, so
        SEQ-01 cannot fence them without breaking that. This runner *has* claimed
        the lead, so it can and must check: a worker that stalled past its lease
        would otherwise cancel or re-queue work another worker had already taken
        over.

        Call it inside the same :func:`transaction` block as the finalising call,
        so the write lock is held across both and the answer cannot go stale
        between the check and the act.
        """
        job = open_job_for_lead(self.conn, campaign_id, lead_id)
        return (
            job is not None
            and job.state == JobState.LEASED.value
            and job.locked_by == self.lease_id
        )

    def _fenced(
        self,
        campaign_id: int,
        lead_id: int,
        finalise: Callable[[], Any],
    ) -> bool:
        """Run a finalising transition only if this process still owns the lead."""
        with transaction(self.conn):
            if not self._still_holds(campaign_id, lead_id):
                logger.warning(
                    "declining to finalise lead %s of campaign %s: the lease is no "
                    "longer this worker's",
                    lead_id,
                    campaign_id,
                )
                return False
            finalise()
            return True

    def _campaign_failure(
        self,
        job: Job,
        campaign_id: int,
        lead_id: int,
        step: Step,
        error: str,
        moment: datetime,
        *,
        detail: Mapping[str, Any] | None = None,
        reached_linkedin: bool = True,
    ) -> JobReport:
        self._log(
            step.action_type,
            self._failure_outcome(reached_linkedin),
            moment=moment,
            lead_id=lead_id,
            campaign_id=campaign_id,
            step_id=step.id,
            detail={**dict(detail or {}), "error": error},
        )
        fail_step(
            self.conn,
            campaign_id,
            lead_id,
            error=error,
            now=moment,
            worker_id=self.lease_id,
        )
        return JobReport(
            job_id=job.id,
            action_type=job.action_type,
            lane=CAMPAIGN_LANE,
            outcome="failed",
            campaign_id=campaign_id,
            lead_id=lead_id,
            error=error,
        )

    def _apply_campaign_refusal(
        self,
        job: Job,
        campaign_id: int,
        lead_id: int,
        step: Step,
        refusal: Mapping[str, Any],
        moment: datetime,
    ) -> JobReport:
        raw_reason = refusal.get("reason")
        if raw_reason is None:
            # The gate could not reach its own database, so it failed closed. So
            # does this: the action does not run, and the step's retry policy
            # decides what that means for the lead.
            return self._campaign_failure(
                job,
                campaign_id,
                lead_id,
                step,
                str(refusal.get("message", "the safety gate was unavailable")),
                moment,
                reached_linkedin=False,
            )

        reason = RefusalReason(raw_reason)
        draft_id: Any = None
        if reason is RefusalReason.APPROVAL_REQUIRED:
            draft_id = self._park_draft(
                job, campaign_id, lead_id, step, moment, source="gate"
            )

        if not refusal.get("audit_logged", False):
            # The gate logs its own refusals. Logging again would put two rows in
            # an append-only table for one decision, and there is no way to take
            # one of them back.
            self._log(
                step.action_type,
                Outcome.REFUSED,
                moment=moment,
                lead_id=lead_id,
                campaign_id=campaign_id,
                step_id=step.id,
                detail={"reason": reason.value},
            )

        self._fenced(
            campaign_id,
            lead_id,
            lambda: refuse_step(
                self.conn, campaign_id, lead_id, reason=reason, now=moment
            ),
        )
        return JobReport(
            job_id=job.id,
            action_type=job.action_type,
            lane=CAMPAIGN_LANE,
            outcome="refused",
            campaign_id=campaign_id,
            lead_id=lead_id,
            reason=reason.value,
            detail={"draft_id": draft_id} if draft_id is not None else {},
        )

    # ------------------------------------------------------------------
    # drafts
    # ------------------------------------------------------------------

    def _draft_kind(
        self,
        action_type: str,
        override: DraftKind | None = None,
    ) -> DraftKind:
        if override is not None:
            return override
        return _DRAFT_KINDS.get(action_type, DraftKind.MESSAGE)

    def _park_draft(
        self,
        job: Job,
        campaign_id: int | None,
        lead_id: int | None,
        step: Step | None,
        moment: datetime,
        *,
        source: str,
        kind: DraftKind | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> Any:
        action_type = job.action_type if step is None else step.action_type
        request = DraftRequest(
            conn=self.conn,
            account_id=self.account_id,
            kind=self._draft_kind(action_type, kind),
            campaign_id=campaign_id,
            lead_id=lead_id,
            step_id=None if step is None else step.id,
            context={
                "action_type": action_type,
                "job_id": job.id,
                "source": source,
                **dict(context or {}),
            },
            now=moment,
        )
        try:
            return self._draft_parker(request)
        except Exception:  # noqa: BLE001 - a broken parker must not stop the loop
            logger.exception("the draft parker raised for job %s", job.id)
            return None

    def _park_campaign_draft(
        self,
        job: Job,
        campaign_id: int,
        lead_id: int,
        step: Step,
        moment: datetime,
        *,
        source: str,
        kind: DraftKind | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> JobReport:
        draft_id = self._park_draft(
            job,
            campaign_id,
            lead_id,
            step,
            moment,
            source=source,
            kind=kind,
            context=context,
        )
        self._log(
            step.action_type,
            Outcome.REFUSED,
            moment=moment,
            lead_id=lead_id,
            campaign_id=campaign_id,
            step_id=step.id,
            detail={
                "reason": RefusalReason.APPROVAL_REQUIRED.value,
                "draft_id": draft_id,
                "source": source,
            },
        )
        self._fenced(
            campaign_id,
            lead_id,
            lambda: refuse_step(
                self.conn,
                campaign_id,
                lead_id,
                reason=RefusalReason.APPROVAL_REQUIRED,
                now=moment,
            ),
        )
        return JobReport(
            job_id=job.id,
            action_type=job.action_type,
            lane=CAMPAIGN_LANE,
            outcome="parked_for_draft",
            campaign_id=campaign_id,
            lead_id=lead_id,
            reason=RefusalReason.APPROVAL_REQUIRED.value,
            detail={"draft_id": draft_id, "source": source},
        )

    # ------------------------------------------------------------------
    # the gate
    # ------------------------------------------------------------------

    def _ask_gate(
        self,
        action_type: str,
        *,
        lead_id: int | None,
        approved: bool,
        moment: datetime,
    ) -> Mapping[str, Any] | None:
        """Ask CORE-03 whether this action may run. None means yes.

        Called with no transaction open, because the gate reads its own
        consistent snapshot and refuses outright if another writer left one in
        flight.
        """
        return guard_action(
            action_type,
            lead_id=lead_id,
            account_id=self.account_id,
            approved=approved,
            now=moment,
        )

    # ------------------------------------------------------------------
    # ad-hoc lane
    # ------------------------------------------------------------------

    async def _run_ad_hoc_job(self, job: Job, moment: datetime) -> JobReport | None:
        with transaction(self.conn):
            if not lease_job(self.conn, job.id, self.lease_id, now=moment):
                return None

        local = job.action_type in LOCAL_ACTIONS
        max_attempts, _ = self._ad_hoc_attempt_budget(job)
        if job.attempts >= max_attempts:
            # Every lease increments `attempts`, so a job that keeps killing its
            # worker arrives back here with a higher count each time and no
            # transition ever runs to notice. Without this it would be reclaimed
            # and re-leased forever, which is a stranded job wearing a costume.
            error = (
                f"gave up after {job.attempts} leases that never reported an outcome"
            )
            self._log(
                job.action_type,
                Outcome.SKIPPED,
                moment=moment,
                lead_id=job.lead_id,
                detail={"error": error},
            )
            self._close_ad_hoc(job, JobState.FAILED, error=error)
            return JobReport(
                job_id=job.id,
                action_type=job.action_type,
                lane=AD_HOC_LANE,
                outcome="failed",
                lead_id=job.lead_id,
                error=error,
            )

        if not local:
            # An ad-hoc job has no campaign, so there is no `approval_mode` to
            # read and the safe reading of silence is "nobody approved this".
            # Anything in `APPROVAL_REQUIRED_ACTIONS` therefore refuses unless
            # the enqueuer said so explicitly in the payload.
            approved = bool(job.payload.get("approved", False))
            refusal = self._ask_gate(
                job.action_type,
                lead_id=job.lead_id,
                approved=approved,
                moment=moment,
            )
            if refusal is not None:
                return self._apply_ad_hoc_refusal(job, refusal, moment)

        executor = self.registry.get(job.action_type)
        if executor is None:
            return self._ad_hoc_failure(
                job,
                f"no executor is registered for {job.action_type!r}",
                moment,
                reached_linkedin=False,
            )

        try:
            browser = None if local else await self.browser()
        except Exception as exc:  # noqa: BLE001 - the browser may fail to start
            # Outside the executor's own handler, this would leave the job leased
            # and only recoverable by the sweep, and a browser that fails to
            # start deterministically would repeat that forever.
            logger.exception("the browser could not be started for job %s", job.id)
            return self._ad_hoc_failure(
                job, f"the browser could not be started: {exc}", moment,
                reached_linkedin=False,
            )

        context = ActionContext(
            conn=self.conn,
            account_id=self.account_id,
            worker_id=self.worker_id,
            job=job,
            now=moment,
            lead_id=job.lead_id,
            browser=browser,
            payload=job.payload,
        )
        try:
            result = await executor(context)
        except Exception as exc:  # noqa: BLE001 - an executor may simply blow up
            logger.exception("ad-hoc executor for %s failed", job.action_type)
            return self._ad_hoc_failure(job, str(exc), moment)

        return self._land_ad_hoc_result(job, result, moment)

    def _land_ad_hoc_result(
        self,
        job: Job,
        result: ActionResult,
        moment: datetime,
    ) -> JobReport:
        if result.status is ActionStatus.NEEDS_DRAFT:
            draft_id = self._park_draft(
                job,
                None,
                job.lead_id,
                None,
                moment,
                source="executor",
                kind=result.draft_kind,
                context=result.detail,
            )
            self._reschedule_ad_hoc(
                job,
                moment,
                seconds=retry_after_seconds(RefusalReason.APPROVAL_REQUIRED),
                error=f"refused: {RefusalReason.APPROVAL_REQUIRED.value}",
                attempts=job.attempts,
            )
            return JobReport(
                job_id=job.id,
                action_type=job.action_type,
                lane=AD_HOC_LANE,
                outcome="parked_for_draft",
                lead_id=job.lead_id,
                reason=RefusalReason.APPROVAL_REQUIRED.value,
                detail={"draft_id": draft_id},
            )

        if result.status is ActionStatus.FAILED:
            return self._ad_hoc_failure(
                job,
                result.error or "the action failed without saying why",
                moment,
                detail=result.detail,
            )

        skipped = result.status is ActionStatus.SKIPPED
        self._log(
            job.action_type,
            Outcome.SKIPPED if skipped else Outcome.SUCCESS,
            moment=moment,
            lead_id=job.lead_id,
            detail=result.detail,
        )
        self._close_ad_hoc(
            job,
            JobState.CANCELLED if skipped else JobState.DONE,
            error=result.outcome,
        )
        return JobReport(
            job_id=job.id,
            action_type=job.action_type,
            lane=AD_HOC_LANE,
            outcome="skipped" if skipped else "success",
            lead_id=job.lead_id,
            detail=result.detail,
        )

    def _ad_hoc_attempt_budget(self, job: Job) -> tuple[int, int]:
        payload = job.payload
        attempts = max(1, int(payload.get("max_attempts", AD_HOC_MAX_ATTEMPTS)))
        backoff = max(
            0, int(payload.get("retry_backoff_seconds", AD_HOC_RETRY_BACKOFF_SECONDS))
        )
        return attempts, backoff

    def _ad_hoc_failure(
        self,
        job: Job,
        error: str,
        moment: datetime,
        *,
        detail: Mapping[str, Any] | None = None,
        reached_linkedin: bool = True,
    ) -> JobReport:
        self._log(
            job.action_type,
            self._failure_outcome(reached_linkedin),
            moment=moment,
            lead_id=job.lead_id,
            detail={**dict(detail or {}), "error": error},
        )
        max_attempts, backoff = self._ad_hoc_attempt_budget(job)
        # `lease_job` already incremented the row's counter, so this attempt is
        # `job.attempts + 1`. An ad-hoc job has no `campaign_leads` row to keep an
        # attempt count in, which is why the job row keeps its own.
        attempts = job.attempts + 1
        if attempts >= max_attempts:
            self._close_ad_hoc(job, JobState.FAILED, error=error)
            outcome = "failed"
        else:
            self._reschedule_ad_hoc(
                job, moment, seconds=backoff * (2 ** (attempts - 1)), error=error
            )
            outcome = "retry_scheduled"
        return JobReport(
            job_id=job.id,
            action_type=job.action_type,
            lane=AD_HOC_LANE,
            outcome=outcome,
            lead_id=job.lead_id,
            error=error,
        )

    def _apply_ad_hoc_refusal(
        self,
        job: Job,
        refusal: Mapping[str, Any],
        moment: datetime,
    ) -> JobReport:
        raw_reason = refusal.get("reason")
        if raw_reason is None:
            return self._ad_hoc_failure(
                job,
                str(refusal.get("message", "the safety gate was unavailable")),
                moment,
                reached_linkedin=False,
            )

        reason = RefusalReason(raw_reason)
        if reason is RefusalReason.APPROVAL_REQUIRED:
            self._park_draft(job, None, job.lead_id, None, moment, source="gate")
        if not refusal.get("audit_logged", False):
            self._log(
                job.action_type,
                Outcome.REFUSED,
                moment=moment,
                lead_id=job.lead_id,
                detail={"reason": reason.value},
            )

        disposition = disposition_for(reason)
        if disposition is Disposition.RETRY_LATER:
            self._reschedule_ad_hoc(
                job,
                moment,
                seconds=retry_after_seconds(reason),
                error=f"refused: {reason.value}",
                # A refusal is the gate declining, not the job failing, so the
                # attempt this lease consumed is handed back. Otherwise three
                # nights of "the daily cap is spent" would exhaust the retries of
                # a harvest that has not run once.
                attempts=job.attempts,
            )
            outcome = "retry_scheduled"
        elif disposition is Disposition.ADVANCE:
            # The same action already happened inside its dedupe window, so this
            # job is satisfied rather than refused.
            self._close_ad_hoc(job, JobState.DONE, error=f"refused: {reason.value}")
            outcome = "already_done"
        else:
            self._close_ad_hoc(job, JobState.REFUSED, error=f"refused: {reason.value}")
            outcome = "refused"

        return JobReport(
            job_id=job.id,
            action_type=job.action_type,
            lane=AD_HOC_LANE,
            outcome=outcome,
            lead_id=job.lead_id,
            reason=reason.value,
        )

    def _close_ad_hoc(
        self,
        job: Job,
        state: JobState,
        *,
        error: str | None = None,
    ) -> bool:
        """Close an ad-hoc job, but only while this process still holds its lease.

        The predicate is the whole point. A worker that stalled past its lease
        and then woke up would otherwise close a job the sweep had already handed
        back and another worker had already re-leased, which is the ad-hoc
        version of finalising somebody else's work.
        """
        with transaction(self.conn):
            cursor = self.conn.execute(
                """
                UPDATE jobs
                SET state = ?, last_error = ?, locked_by = NULL, locked_at = NULL
                WHERE id = ? AND state = ? AND locked_by = ?
                """,
                (
                    state.value,
                    error,
                    job.id,
                    JobState.LEASED.value,
                    self.lease_id,
                ),
            )
        return cursor.rowcount > 0

    def _reschedule_ad_hoc(
        self,
        job: Job,
        moment: datetime,
        *,
        seconds: int,
        error: str | None,
        attempts: int | None = None,
    ) -> bool:
        """Put an ad-hoc job back in the queue, under the same lease predicate.

        `attempts` exists because `lease_job` increments the counter on every
        lease, including one that ends in a cap refusal or a wait for a draft.
        Left alone, three nights of "the daily cap is spent" would exhaust the
        retry budget of a job that has not been attempted once. The campaign lane
        makes the same distinction in `refuse_step`, which deliberately leaves
        `campaign_leads.attempts` untouched.
        """
        due = shift_timestamp(moment, max(0, int(seconds)))
        with transaction(self.conn):
            if attempts is None:
                cursor = self.conn.execute(
                    """
                    UPDATE jobs
                    SET state = ?, scheduled_for = ?, last_error = ?,
                        locked_by = NULL, locked_at = NULL
                    WHERE id = ? AND state = ? AND locked_by = ?
                    """,
                    (
                        JobState.PENDING.value,
                        due,
                        error,
                        job.id,
                        JobState.LEASED.value,
                        self.lease_id,
                    ),
                )
            else:
                cursor = self.conn.execute(
                    """
                    UPDATE jobs
                    SET state = ?, scheduled_for = ?, last_error = ?, attempts = ?,
                        locked_by = NULL, locked_at = NULL
                    WHERE id = ? AND state = ? AND locked_by = ?
                    """,
                    (
                        JobState.PENDING.value,
                        due,
                        error,
                        max(0, int(attempts)),
                        job.id,
                        JobState.LEASED.value,
                        self.lease_id,
                    ),
                )
        return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # the daemon
    # ------------------------------------------------------------------

    async def run_forever(
        self,
        *,
        max_ticks: int | None = None,
        on_tick: Callable[[TickReport], None] | None = None,
    ) -> list[TickReport]:
        """Tick until asked to stop. This is what `worker.py` runs.

        The wait between ticks is `Event.wait` under a timeout rather than a
        sleep. It is scheduling rather than pacing, so it does not belong to
        CORE-04's humanizer, and waiting on the stop event means a shutdown
        signal is answered at once instead of at the end of the interval.
        """
        self.start()
        reports: list[TickReport] = []
        try:
            while not self._stop.is_set():
                if max_ticks is not None and len(reports) >= max_ticks:
                    break
                try:
                    report = await self.tick()
                except Exception:  # noqa: BLE001 - the daemon outlives a bad tick
                    logger.exception("tick failed")
                    moment = self.now()
                    self._heartbeat(STATUS_ERROR, now=moment)
                    report = TickReport(
                        at=str(moment),
                        worker_id=self.worker_id,
                        account_id=self.account_id,
                        within_working_hours=False,
                    )
                reports.append(report)
                if on_tick is not None:
                    on_tick(report)
                # Checked here rather than only at the top of the loop, so the
                # last tick of a bounded run is not followed by a pointless wait
                # for an interval nobody is going to use.
                if self._stop.is_set() or (
                    max_ticks is not None and len(reports) >= max_ticks
                ):
                    break
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self.config.tick_seconds
                    )
                except (asyncio.TimeoutError, TimeoutError):
                    continue
        finally:
            self.stop()
        return reports


def build_worker(
    conn: sqlite3.Connection,
    account_id: int,
    *,
    worker_id: str = "",
    executors: Mapping[str, Executor] | None = None,
    clock: Callable[[], datetime] | None = None,
    browser_supplier: BrowserSupplier = no_browser,
    draft_parker: DraftParker = no_draft_parker,
    **config: Any,
) -> Worker:
    """Convenience constructor, used by `worker.py` and by the tests."""
    return Worker(
        conn,
        WorkerConfig(account_id=account_id, worker_id=worker_id, **config),
        registry=ActionRegistry(executors),
        clock=clock,
        browser_supplier=browser_supplier,
        draft_parker=draft_parker,
    )
