"""The SEQ-04 tick loop: what it runs, what it refuses, and what it survives.

Every test here is offline. The clock is injected, the executors are fakes, the
browser supplier is either absent or an assertion that it was never called, and
the humanizer's sleep is a recorder. Nothing waits.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from linkedin_mcp.audit import Outcome, RefusalReason
from linkedin_mcp.sequences import (
    InvalidTransitionError,
    JobState,
    StepSpec,
    claim_step,
    complete_step,
    fail_step,
    list_jobs,
    mark_replied,
    recover_all_stranded,
    refuse_step,
    skip_lead,
    transaction,
)
from linkedin_mcp.sequences.jobs import lease_job
from linkedin_mcp.worker import (
    ActionRegistry,
    ActionResult,
    DraftKind,
    STATUS_STOPPED,
    Worker,
    WorkerConfig,
    build_worker,
    campaign_funnel,
    read_heartbeat,
    reclaim_stranded_ad_hoc,
    worker_status,
)
from test_worker_support import (  # noqa: F401
    BASE_TIME,
    FIVE_PM,
    NINE_AM,
    WEEKDAYS,
    CrashingExecutor,
    RecordingExecutor,
    StepClock,
    at,
    env,
)

pytestmark = [pytest.mark.usefixtures("env"), pytest.mark.asyncio]


def make_worker(env, *, executors=None, worker_id="w1", clock=None, **config):
    settings = {
        "pace_between_actions": False,
        "sweep_every_ticks": 1,
        **config,
    }
    return build_worker(
        env.conn,
        env.account_id,
        worker_id=worker_id,
        executors=executors,
        clock=clock or env.clock,
        **settings,
    )


async def exploding_browser():
    raise AssertionError("no local step may launch a browser")


# ----------------------------------------------------------------------
# The seam: campaign-less work
# ----------------------------------------------------------------------


async def test_the_worker_runs_a_job_the_state_machine_cannot_see(env):
    """A harvest has no campaign, no lead and no step, and must still run.

    `sequences.due_jobs` inner-joins `campaigns` and `campaign_leads`, so it
    returns none of these. A tick loop that leased only through it would let
    every harvest MCP-02 (#25) enqueues accumulate in `jobs` forever, and both
    that pull request and this one would still pass their own tests.
    """
    harvest = env.enqueue_ad_hoc("profile_search")
    executor = RecordingExecutor(ActionResult.ok(found=12))
    worker = make_worker(env, executors={"profile_search": executor})

    report = await worker.tick(now=BASE_TIME)

    assert len(executor) == 1
    assert [job.outcome for job in report.jobs] == ["success"]
    assert env.job(harvest)["state"] == JobState.DONE.value
    assert env.logged(action_type="profile_search", outcome=Outcome.SUCCESS.value)


async def test_both_lanes_run_in_one_tick(env):
    campaign_id = env.campaign([StepSpec("connection_request")])
    lead_id = env.lead("Ada", public_id="ada")
    env.enrol(campaign_id, [lead_id])
    env.enqueue_ad_hoc("profile_search")

    invites = RecordingExecutor()
    searches = RecordingExecutor(ActionResult.ok(found=3))
    worker = make_worker(
        env,
        executors={"connection_request": invites, "profile_search": searches},
    )

    report = await worker.tick(now=BASE_TIME)

    assert len(invites) == 1
    assert len(searches) == 1
    assert sorted(job.lane for job in report.jobs) == ["ad_hoc", "campaign"]


async def test_an_unroutable_job_is_reported_and_left_alone(env):
    campaign_id = env.campaign([StepSpec("message")])
    orphan = env.enqueue_ad_hoc("message", campaign_id=campaign_id)
    executor = RecordingExecutor()
    worker = make_worker(env, executors={"message": executor})

    report = await worker.tick(now=BASE_TIME)

    assert len(executor) == 0
    assert report.unroutable == (orphan,)
    assert env.job(orphan)["state"] == JobState.PENDING.value


# ----------------------------------------------------------------------
# The campaign lane
# ----------------------------------------------------------------------


async def test_a_successful_step_is_logged_and_the_lead_advances(env):
    campaign_id = env.campaign([StepSpec("connection_request"), StepSpec("message")])
    lead_id = env.lead("Ada", public_id="ada")
    env.enrol(campaign_id, [lead_id])
    worker = make_worker(env, executors={"connection_request": RecordingExecutor()})

    await worker.tick(now=BASE_TIME)

    state = env.lead_state(campaign_id, lead_id)
    assert state["sublist"] == "queue"
    assert state["current_step_ord"] == 2
    logged = env.logged(action_type="connection_request")
    assert [row["outcome"] for row in logged] == [Outcome.SUCCESS.value]
    assert logged[0]["campaign_id"] == campaign_id


async def test_a_failed_step_falls_under_the_steps_own_on_failure_policy(env):
    campaign_id = env.campaign(
        [StepSpec("connection_request", on_failure="skip"), StepSpec("message")]
    )
    lead_id = env.lead("Ada", public_id="ada")
    env.enrol(campaign_id, [lead_id])
    executor = RecordingExecutor(ActionResult.failed("the Connect button was gone"))
    worker = make_worker(env, executors={"connection_request": executor})

    report = await worker.tick(now=BASE_TIME)

    assert [job.outcome for job in report.jobs] == ["failed"]
    assert env.lead_state(campaign_id, lead_id)["sublist"] == "skipped"
    assert env.logged(action_type="connection_request", outcome=Outcome.FAILURE.value)


async def test_a_step_nothing_can_run_does_not_spend_the_accounts_budget(env):
    """A failure that never reached LinkedIn must not count against the caps.

    `ATTEMPTED_OUTCOMES` is `success` and `failure`, and those two are what the
    safety gate counts. Logging "no executor is registered" as a `failure` would
    make an unattended worker with no browser drivers burn its entire daily
    invitation budget on actions that did not happen, and the second lead here
    would be refused for a cap the first lead never actually spent.
    """
    campaign_id = env.campaign([StepSpec("connection_request", on_failure="fail")])
    env.enrol(campaign_id, env.leads(3))
    env.set_cap("connection_request", daily_cap=1)
    worker = make_worker(env)

    report = await worker.tick(now=BASE_TIME)

    assert [job.outcome for job in report.jobs] == ["failed", "failed", "failed"]
    assert env.logged(outcome=Outcome.REFUSED.value) == []
    assert env.logged(outcome=Outcome.FAILURE.value) == []
    assert len(env.logged(outcome=Outcome.SKIPPED.value)) == 3


async def test_a_failure_that_did_reach_linkedin_still_counts(env):
    """The other side of the same rule. An attempt that happened is an attempt.

    A `connection_request` whose click failed still loaded a profile and still
    told LinkedIn something, so it spends budget exactly like a success does.
    """
    campaign_id = env.campaign([StepSpec("connection_request", on_failure="fail")])
    env.enrol(campaign_id, env.leads(2))
    env.set_cap("connection_request", daily_cap=1)
    worker = make_worker(
        env,
        executors={
            "connection_request": RecordingExecutor(
                ActionResult.failed("the Connect button was gone")
            )
        },
    )

    report = await worker.tick(now=BASE_TIME)

    assert len(env.logged(outcome=Outcome.FAILURE.value)) == 1
    assert [job.reason for job in report.jobs][1] == (
        RefusalReason.DAILY_CAP_REACHED.value
    )


async def test_an_executor_that_raises_is_a_step_failure_not_a_dead_tick(env):
    """One bad step must not end the tick for every other lead in the bunch."""
    campaign_id = env.campaign([StepSpec("connection_request", on_failure="skip")])
    first, second = env.leads(2)
    env.enrol(campaign_id, [first, second])

    calls: list[int] = []

    async def flaky(context):
        calls.append(context.lead_id)
        if context.lead_id == first:
            raise RuntimeError("selector timed out")
        return ActionResult.ok()

    worker = make_worker(env, executors={"connection_request": flaky})
    await worker.tick(now=BASE_TIME)

    assert calls == [first, second]
    assert env.lead_state(campaign_id, first)["sublist"] == "skipped"
    assert env.lead_state(campaign_id, second)["sublist"] == "successful"


async def test_an_executor_may_skip_a_lead_softly(env):
    campaign_id = env.campaign([StepSpec("connection_request"), StepSpec("message")])
    lead_id = env.lead("Ada", public_id="ada")
    env.enrol(campaign_id, [lead_id])
    executor = RecordingExecutor(ActionResult.skipped("already a first degree contact"))
    worker = make_worker(env, executors={"connection_request": executor})

    await worker.tick(now=BASE_TIME)

    assert env.lead_state(campaign_id, lead_id)["sublist"] == "skipped"
    assert env.logged(action_type="connection_request", outcome=Outcome.SKIPPED.value)


async def test_a_lead_that_replied_receives_no_further_step(env):
    """SEQ-03 (#21) writes `replied`; the runner must simply never see the lead.

    `due_jobs` filters on the sub-list, so a message queued for tonight does not
    go out on top of this morning's reply.
    """
    campaign_id = env.campaign([StepSpec("connection_request"), StepSpec("message")])
    lead_id = env.lead("Ada", public_id="ada")
    env.enrol(campaign_id, [lead_id])
    messages = RecordingExecutor()
    worker = make_worker(
        env,
        executors={"connection_request": RecordingExecutor(), "message": messages},
    )

    await worker.tick(now=BASE_TIME)
    mark_replied(env.conn, campaign_id, lead_id, now=at(minutes=1))
    await worker.tick(now=at(minutes=2))

    assert len(messages) == 0
    assert env.lead_state(campaign_id, lead_id)["sublist"] == "replied"


# ----------------------------------------------------------------------
# Running with nothing attached
# ----------------------------------------------------------------------


async def test_a_filter_step_resolves_with_nothing_registered_at_all(env):
    """No executors, no browser, no model, and the campaign still moves."""
    campaign_id = env.campaign(
        [
            StepSpec("filter", config={"filter": "headline_contains", "contains": ["VP"]}),
            StepSpec("message"),
        ]
    )
    match = env.lead("Ada", public_id="ada", headline="VP Engineering")
    miss = env.lead("Bob", public_id="bob", headline="Student")
    env.enrol(campaign_id, [match, miss])
    worker = Worker(
        env.conn,
        WorkerConfig(
            account_id=env.account_id,
            worker_id="w1",
            pace_between_actions=False,
            sweep_every_ticks=1,
        ),
        clock=env.clock,
        browser_supplier=exploding_browser,
    )

    report = await worker.tick(now=BASE_TIME)

    assert sorted(job.outcome for job in report.jobs) == [
        "filter_matched",
        "filter_no_match",
    ]
    assert env.lead_state(campaign_id, match)["current_step_ord"] == 2
    assert env.lead_state(campaign_id, miss)["sublist"] == "skipped"


async def test_a_local_step_writes_no_actions_log_row(env):
    """Deliberate, and load bearing rather than an omission.

    `actions_log` is the ledger the safety gate counts, and `metered_universe`
    excludes only `UNMETERED_ACTIONS`. A `success` row for a filter would spend
    the account's global daily and hourly ceilings on a step that touched
    nothing, so a campaign with a filter in front of every message would quietly
    halve its own sending capacity. The verdict is not lost: SEQ-01 writes it to
    `campaign_leads.last_outcome`.
    """
    campaign_id = env.campaign(
        [StepSpec("filter", config={"filter": "always"}), StepSpec("message")]
    )
    lead_id = env.lead("Ada", public_id="ada")
    env.enrol(campaign_id, [lead_id])
    worker = make_worker(env)

    await worker.tick(now=BASE_TIME)

    assert env.logged(action_type="filter") == []
    assert env.lead_state(campaign_id, lead_id)["last_outcome"] == "filter_matched"


async def test_a_campaign_parks_cleanly_when_no_executor_can_run_its_step(env):
    """Unattended, with no browser drivers registered, is a supported state.

    The step cannot run, so it fails, and the step's own `on_failure` decides
    what that means for the lead. What must not happen is a lead spinning in
    `processing`, an exception escaping the tick, or the loop stopping.
    """
    campaign_id = env.campaign(
        [
            StepSpec("filter", config={"filter": "always"}),
            StepSpec("connection_request", on_failure="fail"),
        ]
    )
    lead_id = env.lead("Ada", public_id="ada")
    env.enrol(campaign_id, [lead_id])
    worker = make_worker(env)

    await worker.tick(now=BASE_TIME)
    report = await worker.tick(now=at(minutes=1))

    assert [job.outcome for job in report.jobs] == ["failed"]
    assert env.lead_state(campaign_id, lead_id)["sublist"] == "failed"
    assert campaign_funnel(env.conn, campaign_id)["in_flight"] == 0
    assert list_jobs(env.conn, campaign_id=campaign_id, states=[JobState.PENDING]) == []


async def test_the_browser_is_never_built_for_a_local_step(env):
    campaign_id = env.campaign([StepSpec("filter", config={"filter": "always"})])
    env.enrol(campaign_id, [env.lead("Ada", public_id="ada")])
    worker = Worker(
        env.conn,
        WorkerConfig(account_id=env.account_id, worker_id="w1", sweep_every_ticks=1),
        clock=env.clock,
        browser_supplier=exploding_browser,
    )

    await worker.tick(now=BASE_TIME)

    assert env.lead_state(campaign_id, 1)["sublist"] == "successful"


# ----------------------------------------------------------------------
# The safety gate
# ----------------------------------------------------------------------


async def test_a_refusal_requeues_the_lead_with_the_reasons_own_delay(env):
    """The gate says no, the lead waits, and the row explaining why is written."""
    campaign_id = env.campaign([StepSpec("connection_request")])
    lead_id = env.lead("Ada", public_id="ada")
    env.enrol(campaign_id, [lead_id])
    env.set_cap("connection_request", daily_cap=0)
    executor = RecordingExecutor()
    worker = make_worker(env, executors={"connection_request": executor})

    report = await worker.tick(now=BASE_TIME)

    assert len(executor) == 0
    assert [job.reason for job in report.jobs] == [
        RefusalReason.DAILY_CAP_REACHED.value
    ]
    state = env.lead_state(campaign_id, lead_id)
    assert state["sublist"] == "queue"
    assert state["next_run_at"] > "2026-03-09 09:30:00"
    # The gate logs its own refusal. A second row here would be a duplicate in an
    # append-only table, and there is no way to take one back.
    assert len(env.logged(outcome=Outcome.REFUSED.value)) == 1


async def test_a_disabled_action_takes_the_lead_out_of_the_flow(env):
    """`ACTION_DISABLED` maps to `skip`, so the lead does not pin a step forever."""
    campaign_id = env.campaign([StepSpec("connection_request")])
    lead_id = env.lead("Ada", public_id="ada")
    env.enrol(campaign_id, [lead_id])
    env.set_cap("connection_request", enabled=False)
    worker = make_worker(env, executors={"connection_request": RecordingExecutor()})

    await worker.tick(now=BASE_TIME)

    assert env.lead_state(campaign_id, lead_id)["sublist"] == "skipped"


async def test_a_paused_account_stops_the_worker_without_stopping_the_campaign(env):
    campaign_id = env.campaign([StepSpec("connection_request")])
    lead_id = env.lead("Ada", public_id="ada")
    env.enrol(campaign_id, [lead_id])
    env.set_account_state("paused")
    executor = RecordingExecutor()
    worker = make_worker(env, executors={"connection_request": executor})

    report = await worker.tick(now=BASE_TIME)

    assert len(executor) == 0
    assert [job.reason for job in report.jobs] == [RefusalReason.ACCOUNT_PAUSED.value]
    assert env.lead_state(campaign_id, lead_id)["sublist"] == "queue"


# ----------------------------------------------------------------------
# Working hours
# ----------------------------------------------------------------------


async def test_outside_working_hours_only_local_steps_run(env):
    env.set_working_hours()
    campaign_id = env.campaign(
        [
            StepSpec("filter", config={"filter": "always"}),
            StepSpec("connection_request"),
        ]
    )
    lead_id = env.lead("Ada", public_id="ada")
    env.enrol(campaign_id, [lead_id])
    executor = RecordingExecutor()
    worker = make_worker(env, executors={"connection_request": executor})

    # Monday 21:30, well after the account closes.
    night = await worker.tick(now=at(hours=12))
    assert night.within_working_hours is False
    assert [job.outcome for job in night.jobs] == ["filter_matched"]

    later = await worker.tick(now=at(hours=13))
    assert later.jobs == ()
    assert len(later.deferred_metered) == 1
    assert len(executor) == 0


async def test_deferred_work_goes_first_when_the_account_opens(env):
    """Work bunches at the start of the working day, as a person's would.

    The job's due time stayed in the past all night, so it is the most overdue
    thing in the queue the moment the window opens.
    """
    env.set_working_hours()
    campaign_id = env.campaign([StepSpec("connection_request")])
    lead_id = env.lead("Ada", public_id="ada")
    env.enrol(campaign_id, [lead_id])
    executor = RecordingExecutor()
    worker = make_worker(env, executors={"connection_request": executor})

    await worker.tick(now=at(hours=12))
    assert len(executor) == 0

    # Tuesday 09:30.
    await worker.tick(now=at(hours=24))

    assert executor.lead_ids == [lead_id]


# ----------------------------------------------------------------------
# Drafts, and never waiting on a model
# ----------------------------------------------------------------------


async def test_a_manual_drafts_campaign_parks_a_draft_instead_of_sending(env):
    """The unattended path CORE-03's approval flag exists for.

    The worker passes the campaign's real approval mode, so a sequence nobody
    signed off refuses here instead of quietly inviting people overnight.
    """
    campaign_id = env.campaign(
        [StepSpec("connection_request")], approval_mode="manual_drafts"
    )
    lead_id = env.lead("Ada", public_id="ada")
    env.enrol(campaign_id, [lead_id])
    parked: list = []
    executor = RecordingExecutor()
    worker = build_worker(
        env.conn,
        env.account_id,
        worker_id="w1",
        executors={"connection_request": executor},
        clock=env.clock,
        draft_parker=lambda request: parked.append(request) or 99,
        pace_between_actions=False,
        sweep_every_ticks=1,
    )

    report = await worker.tick(now=BASE_TIME)

    assert len(executor) == 0
    assert [job.reason for job in report.jobs] == [
        RefusalReason.APPROVAL_REQUIRED.value
    ]
    assert [request.kind for request in parked] == [DraftKind.CONNECTION_NOTE]
    assert parked[0].lead_id == lead_id
    assert env.lead_state(campaign_id, lead_id)["sublist"] == "queue"


async def test_the_default_parker_still_parks_the_lead_cleanly(env):
    """No drafts package is installed, and nothing hangs or sends.

    SEQ-05 (#23) is not merged, so this is the state the daemon actually ships
    in. The lead waits for an approval that has nowhere to come from yet, which
    is correct: waiting is not sending.
    """
    campaign_id = env.campaign(
        [StepSpec("connection_request")], approval_mode="manual_drafts"
    )
    lead_id = env.lead("Ada", public_id="ada")
    env.enrol(campaign_id, [lead_id])
    worker = make_worker(env, executors={"connection_request": RecordingExecutor()})

    first = await worker.tick(now=BASE_TIME)
    second = await worker.tick(now=at(hours=2))

    assert [job.outcome for job in first.jobs] == ["refused"]
    assert [job.outcome for job in second.jobs] == ["refused"]
    assert env.lead_state(campaign_id, lead_id)["sublist"] == "queue"
    # A refusal is the gate declining, not the step failing, so the attempt
    # budget is untouched and a week of refusals cannot exhaust the retries.
    assert env.lead_state(campaign_id, lead_id)["attempts"] == 0


async def test_an_executor_asking_for_a_draft_parks_and_moves_on(env):
    campaign_id = env.campaign([StepSpec("message"), StepSpec("profile_view")])
    lead_id = env.lead("Ada", public_id="ada")
    env.enrol(campaign_id, [lead_id])
    parked: list = []
    worker = build_worker(
        env.conn,
        env.account_id,
        worker_id="w1",
        executors={
            "message": RecordingExecutor(
                ActionResult.needs_draft(DraftKind.MESSAGE, thread="new")
            )
        },
        clock=env.clock,
        draft_parker=lambda request: parked.append(request) or 5,
        pace_between_actions=False,
        sweep_every_ticks=1,
    )

    report = await worker.tick(now=BASE_TIME)

    assert [job.outcome for job in report.jobs] == ["parked_for_draft"]
    assert parked[0].context["thread"] == "new"
    assert parked[0].step_id is not None
    assert env.lead_state(campaign_id, lead_id)["current_step_ord"] == 1


async def test_a_broken_draft_parker_does_not_stop_the_loop(env):
    campaign_id = env.campaign(
        [StepSpec("connection_request")], approval_mode="manual_drafts"
    )
    env.enrol(campaign_id, [env.lead("Ada", public_id="ada")])

    def explode(request):
        raise RuntimeError("the drafts table is missing")

    worker = build_worker(
        env.conn,
        env.account_id,
        worker_id="w1",
        clock=env.clock,
        draft_parker=explode,
        pace_between_actions=False,
        sweep_every_ticks=1,
    )

    report = await worker.tick(now=BASE_TIME)

    assert [job.outcome for job in report.jobs] == ["refused"]


# ----------------------------------------------------------------------
# Crash, lease and recovery
# ----------------------------------------------------------------------


async def test_a_crashed_worker_leaves_a_committed_claim_behind(env):
    campaign_id = env.campaign([StepSpec("connection_request"), StepSpec("message")])
    lead_id = env.lead("Ada", public_id="ada")
    env.enrol(campaign_id, [lead_id])
    dying = CrashingExecutor()
    worker_a = make_worker(
        env, executors={"connection_request": dying}, worker_id="worker-a"
    )

    with pytest.raises(CrashingExecutor.Death):
        await worker_a.tick(now=BASE_TIME)

    state = env.lead_state(campaign_id, lead_id)
    assert state["sublist"] == "processing"
    job = list_jobs(env.conn, campaign_id=campaign_id, states=[JobState.LEASED])[0]
    # The lease carries a token unique to this process, not just the operator's
    # stable worker id, so a replacement started under the same `--worker-id`
    # cannot inherit the dead process's claim.
    assert job.locked_by == worker_a.lease_id
    assert job.locked_by.startswith("worker-a#")
    assert worker_a.worker_id == "worker-a"


async def test_a_live_peer_does_not_touch_a_lead_whose_lease_is_still_valid(env):
    """The half of "not double-executed" that matters most.

    Worker A is dead but its lease has not expired. If worker B could take the
    lead now, the same invitation would go out twice, which is the failure the
    lease exists to prevent. B must find nothing at all.
    """
    campaign_id = env.campaign([StepSpec("connection_request"), StepSpec("message")])
    lead_id = env.lead("Ada", public_id="ada")
    env.enrol(campaign_id, [lead_id])
    worker_a = make_worker(
        env,
        executors={"connection_request": CrashingExecutor()},
        worker_id="worker-a",
    )
    with pytest.raises(CrashingExecutor.Death):
        await worker_a.tick(now=BASE_TIME)

    survivor = RecordingExecutor()
    worker_b = make_worker(
        env, executors={"connection_request": survivor}, worker_id="worker-b"
    )
    report = await worker_b.tick(now=at(minutes=5))

    assert len(survivor) == 0
    assert report.jobs == ()
    assert report.recovered_leads == {}
    assert env.lead_state(campaign_id, lead_id)["sublist"] == "processing"


async def test_the_reclaimed_lead_runs_exactly_once_once_the_lease_expires(env):
    campaign_id = env.campaign([StepSpec("connection_request"), StepSpec("message")])
    lead_id = env.lead("Ada", public_id="ada")
    env.enrol(campaign_id, [lead_id])
    dying = CrashingExecutor()
    worker_a = make_worker(
        env, executors={"connection_request": dying}, worker_id="worker-a"
    )
    with pytest.raises(CrashingExecutor.Death):
        await worker_a.tick(now=BASE_TIME)

    survivor = RecordingExecutor()
    worker_b = make_worker(
        env, executors={"connection_request": survivor}, worker_id="worker-b"
    )
    report = await worker_b.tick(now=at(minutes=30))

    assert report.recovered_leads == {campaign_id: (lead_id,)}
    assert len(survivor) == 1
    assert env.lead_state(campaign_id, lead_id)["current_step_ord"] == 2
    # The crashed attempt reached the executor but never logged, and the
    # reclaimed run logged exactly once. Two rows here would be the double send.
    assert len(env.logged(action_type="connection_request")) == 1

    again = await worker_b.tick(now=at(minutes=31))
    assert len(survivor) == 1
    assert again.by_outcome("success") == ()


async def test_the_zombie_worker_cannot_finalise_the_lead_it_lost(env):
    """The fence that makes reclamation safe, tested at the dangerous instant.

    The check has to happen while worker B is *holding* the lead in `processing`,
    not after B has already finished and returned it to `queue`. Finalising a
    lead that has left `processing` would be refused whether or not the worker id
    were checked at all, so testing it there proves nothing. Here worker A wakes
    up in the middle of B's action, which is exactly the double send the lease
    exists to prevent.
    """
    campaign_id = env.campaign([StepSpec("connection_request"), StepSpec("message")])
    lead_id = env.lead("Ada", public_id="ada")
    env.enrol(campaign_id, [lead_id])
    worker_a = make_worker(
        env,
        executors={"connection_request": CrashingExecutor()},
        worker_id="worker-a",
    )
    with pytest.raises(CrashingExecutor.Death):
        await worker_a.tick(now=BASE_TIME)

    caught: list[Exception] = []

    async def wake_the_zombie(context):
        # Worker B holds the lease right now and the lead is `processing`.
        assert env.lead_state(campaign_id, lead_id)["sublist"] == "processing"
        for finalise in (
            lambda: complete_step(
                env.conn,
                campaign_id,
                lead_id,
                now=at(minutes=30),
                worker_id=worker_a.lease_id,
            ),
            lambda: fail_step(
                env.conn,
                campaign_id,
                lead_id,
                error="zombie",
                now=at(minutes=30),
                worker_id=worker_a.lease_id,
            ),
        ):
            try:
                finalise()
            except InvalidTransitionError as exc:
                caught.append(exc)
        return ActionResult.ok()

    worker_b = make_worker(
        env, executors={"connection_request": wake_the_zombie}, worker_id="worker-b"
    )
    await worker_b.tick(now=at(minutes=30))

    assert len(caught) == 2, "the zombie finalised work it no longer owned"
    # Worker B's own outcome still landed, so the fence blocked the zombie rather
    # than the whole lead.
    assert env.lead_state(campaign_id, lead_id)["current_step_ord"] == 2
    assert len(env.logged(action_type="connection_request")) == 1


async def test_a_zombie_cannot_refuse_or_skip_a_lead_it_no_longer_holds(env):
    """`refuse_step` and `skip_lead` take no worker id, so the runner fences them.

    SEQ-01 cannot fence them itself: a refusal legitimately resolves a step a
    lead was only queued for. This runner has claimed the lead, so it can check,
    and it must, because a stalled worker re-queueing a lead somebody else is
    part way through would push that lead's next attempt an hour into the future
    on the strength of a decision made before the lease expired.
    """
    campaign_id = env.campaign([StepSpec("connection_request"), StepSpec("message")])
    lead_id = env.lead("Ada", public_id="ada")
    env.enrol(campaign_id, [lead_id])
    zombie = make_worker(env, worker_id="zombie")
    live = make_worker(env, worker_id="live")

    # The zombie claims, then loses the lead to the sweep and to `live`.
    claim_step(env.conn, campaign_id, lead_id, worker_id=zombie.lease_id, now=BASE_TIME)
    recover_all_stranded(env.conn, now=at(minutes=30), lease_seconds=900)
    claim_step(
        env.conn, campaign_id, lead_id, worker_id=live.lease_id, now=at(minutes=31)
    )
    before = env.lead_state(campaign_id, lead_id)

    refused = zombie._fenced(
        campaign_id,
        lead_id,
        lambda: refuse_step(
            env.conn,
            campaign_id,
            lead_id,
            reason=RefusalReason.DAILY_CAP_REACHED,
            now=at(minutes=32),
        ),
    )
    skipped = zombie._fenced(
        campaign_id,
        lead_id,
        lambda: skip_lead(
            env.conn, campaign_id, lead_id, reason="zombie", now=at(minutes=32)
        ),
    )

    assert refused is False
    assert skipped is False
    assert env.lead_state(campaign_id, lead_id) == before
    # The live worker is unaffected and can still finish normally.
    assert (
        live._fenced(
            campaign_id,
            lead_id,
            lambda: skip_lead(
                env.conn, campaign_id, lead_id, reason="done", now=at(minutes=33)
            ),
        )
        is True
    )


async def test_an_ad_hoc_job_cannot_be_closed_by_a_worker_that_lost_its_lease(env):
    """The same fence in the lane that has no state machine to lean on."""
    harvest = env.enqueue_ad_hoc("profile_search")
    zombie = make_worker(env, worker_id="zombie")
    live = make_worker(env, worker_id="live")
    job = list_jobs(env.conn, account_id=env.account_id)[0]

    with transaction(env.conn):
        lease_job(env.conn, harvest, zombie.lease_id, now=BASE_TIME)
    reclaim_stranded_ad_hoc(env.conn, now=at(minutes=30), lease_seconds=900)
    with transaction(env.conn):
        lease_job(env.conn, harvest, live.lease_id, now=at(minutes=31))

    assert zombie._close_ad_hoc(job, JobState.DONE, error="zombie") is False
    assert env.job(harvest)["state"] == JobState.LEASED.value
    assert env.job(harvest)["locked_by"] == live.lease_id
    assert live._close_ad_hoc(job, JobState.DONE) is True


async def test_a_crashed_ad_hoc_job_is_reclaimed_and_run_once(env):
    """`recover_stranded` sweeps `campaign_leads` and cannot see this job at all."""
    harvest = env.enqueue_ad_hoc("profile_search")
    worker_a = make_worker(
        env, executors={"profile_search": CrashingExecutor()}, worker_id="worker-a"
    )
    with pytest.raises(CrashingExecutor.Death):
        await worker_a.tick(now=BASE_TIME)
    assert env.job(harvest)["state"] == JobState.LEASED.value

    survivor = RecordingExecutor(ActionResult.ok(found=4))
    worker_b = make_worker(
        env, executors={"profile_search": survivor}, worker_id="worker-b"
    )

    early = await worker_b.tick(now=at(minutes=5))
    assert early.jobs == ()
    assert len(survivor) == 0

    late = await worker_b.tick(now=at(minutes=30))

    assert late.reclaimed_ad_hoc == (harvest,)
    assert len(survivor) == 1
    assert env.job(harvest)["state"] == JobState.DONE.value


async def test_an_ad_hoc_failure_retries_before_it_gives_up(env):
    harvest = env.enqueue_ad_hoc(
        "profile_search",
        payload='{"max_attempts": 2, "retry_backoff_seconds": 60}',
    )
    executor = RecordingExecutor(ActionResult.failed("LinkedIn returned nothing"))
    worker = make_worker(env, executors={"profile_search": executor})

    first = await worker.tick(now=BASE_TIME)
    assert [job.outcome for job in first.jobs] == ["retry_scheduled"]
    assert env.job(harvest)["state"] == JobState.PENDING.value

    second = await worker.tick(now=at(minutes=5))

    assert [job.outcome for job in second.jobs] == ["failed"]
    assert env.job(harvest)["state"] == JobState.FAILED.value
    assert len(executor) == 2


async def test_an_ad_hoc_refusal_that_can_succeed_later_is_rescheduled(env):
    harvest = env.enqueue_ad_hoc("profile_search")
    env.set_cap("profile_search", daily_cap=0)
    executor = RecordingExecutor()
    worker = make_worker(env, executors={"profile_search": executor})

    report = await worker.tick(now=BASE_TIME)

    assert len(executor) == 0
    assert [job.outcome for job in report.jobs] == ["retry_scheduled"]
    row = env.job(harvest)
    assert row["state"] == JobState.PENDING.value
    assert row["scheduled_for"] > "2026-03-09 09:30:00"


async def test_an_ad_hoc_invite_refuses_unless_the_payload_says_it_was_approved(env):
    """Silence is not approval.

    An ad-hoc job has no campaign, so there is no `approval_mode` to read. The
    safe reading of a missing flag is that nobody signed this off.
    """
    lead_id = env.lead("Ada", public_id="ada")
    env.enqueue_ad_hoc("connection_request", lead_id=lead_id, payload="{}")
    executor = RecordingExecutor()
    worker = make_worker(env, executors={"connection_request": executor})

    report = await worker.tick(now=BASE_TIME)

    assert len(executor) == 0
    assert [job.reason for job in report.jobs] == [
        RefusalReason.APPROVAL_REQUIRED.value
    ]


# ----------------------------------------------------------------------
# Restart, heartbeat and the daemon loop
# ----------------------------------------------------------------------


async def test_an_ad_hoc_refusal_does_not_spend_an_attempt(env):
    """A refusal is the gate declining, not the job failing.

    `lease_job` increments the row's counter on every lease, so without this a
    harvest refused three nights running would be marked failed the first time it
    actually ran and failed. The campaign lane makes the same distinction inside
    `refuse_step`.
    """
    harvest = env.enqueue_ad_hoc(
        "profile_search", payload='{"max_attempts": 2, "retry_backoff_seconds": 60}'
    )
    env.set_cap("profile_search", daily_cap=0)
    executor = RecordingExecutor()
    worker = make_worker(env, executors={"profile_search": executor})

    for hour in (0, 6, 12, 18):
        await worker.tick(now=at(hours=hour))

    row = env.job(harvest)
    assert row["state"] == JobState.PENDING.value
    assert row["attempts"] == 0, "cap refusals must not consume the retry budget"
    assert len(executor) == 0


async def test_a_job_that_keeps_killing_its_worker_is_eventually_given_up_on(env):
    """Reclaiming forever is a stranded job wearing a costume.

    Each crash leaves the job leased, each sweep hands it back, and no transition
    ever runs to notice. The lease counter is the only evidence, so it is what
    the budget is enforced against.
    """
    harvest = env.enqueue_ad_hoc(
        "profile_search", payload='{"max_attempts": 2, "retry_backoff_seconds": 0}'
    )
    dying = CrashingExecutor()
    worker = make_worker(env, executors={"profile_search": dying})

    for attempt in range(2):
        with pytest.raises(CrashingExecutor.Death):
            await worker.tick(now=at(hours=attempt))

    final = await worker.tick(now=at(hours=2))

    assert len(dying) == 2
    assert [job.outcome for job in final.jobs] == ["failed"]
    assert env.job(harvest)["state"] == JobState.FAILED.value


async def test_a_local_ad_hoc_action_never_asks_the_gate(env):
    """Consistent with the campaign lane, and with SEQ-01's rule about it.

    A local action consumes no LinkedIn budget, so spending a safety-gate lease
    on it would be wrong. It also means a closed account can still run one, which
    is the whole reason the closed-hours lane allows local actions through.
    """
    env.set_working_hours()
    env.set_account_state("paused")
    tags = env.enqueue_ad_hoc("tag", payload='{"tag": "warm"}')
    executor = RecordingExecutor(ActionResult.ok(tagged=True))
    worker = make_worker(env, executors={"tag": executor})

    report = await worker.tick(now=at(hours=12))

    assert report.within_working_hours is False
    assert len(executor) == 1
    assert [job.outcome for job in report.jobs] == ["success"]
    assert env.job(tags)["state"] == JobState.DONE.value
    # Nothing reached LinkedIn, so nothing was written to the ledger the caps
    # are counted from.
    assert env.logged(action_type="tag") == []


async def test_a_browser_that_will_not_start_is_a_step_failure_not_a_stuck_lease(env):
    """Outside the executor's handler this would leave the lead leased forever.

    A deterministic Playwright startup failure would then repeat every sweep,
    never spend an attempt, and never reach a terminal state.
    """
    campaign_id = env.campaign([StepSpec("connection_request", on_failure="fail")])
    lead_id = env.lead("Ada", public_id="ada")
    env.enrol(campaign_id, [lead_id])

    async def broken_browser():
        raise RuntimeError("playwright is not installed")

    worker = Worker(
        env.conn,
        WorkerConfig(
            account_id=env.account_id,
            worker_id="w1",
            pace_between_actions=False,
            sweep_every_ticks=1,
        ),
        registry=ActionRegistry({"connection_request": RecordingExecutor()}),
        clock=env.clock,
        browser_supplier=broken_browser,
    )

    report = await worker.tick(now=BASE_TIME)

    assert [job.outcome for job in report.jobs] == ["failed"]
    assert env.lead_state(campaign_id, lead_id)["sublist"] == "failed"
    assert list_jobs(env.conn, campaign_id=campaign_id, states=[JobState.LEASED]) == []
    # Nothing reached LinkedIn, so the failure does not spend the daily budget.
    assert env.logged(outcome=Outcome.FAILURE.value) == []


async def test_a_bunch_stops_when_the_window_closes_part_way_through(env):
    """A tick that began at 16:55 must not still be sending at ten past five.

    The daemon re-reads its clock before every job, because a bunch of humanised
    actions takes minutes. A test that pins the tick to one instant cannot see
    this, so this one lets the clock move exactly as the daemon's does.
    """
    env.set_working_hours(WEEKDAYS, NINE_AM, FIVE_PM)
    campaign_id = env.campaign([StepSpec("connection_request")])
    env.enrol(campaign_id, env.leads(4))

    clock = StepClock(datetime(2026, 3, 9, 16, 55, tzinfo=timezone.utc))
    executor = RecordingExecutor()

    async def slow(context):
        clock.advance(minutes=4)
        return await executor(context)

    worker = make_worker(env, executors={"connection_request": slow}, clock=clock)

    report = await worker.tick()

    assert report.within_working_hours is True
    # 16:55 and 16:59 are inside the window; 17:03 is not, so the bunch stops.
    assert len(executor) == 2
    assert len(report.jobs) == 2
    assert env.logged(action_type="connection_request", outcome=Outcome.REFUSED.value) == []


async def test_a_brand_new_worker_object_resumes_from_the_database(env):
    """"Survives a VS Code restart", stated as what it actually means.

    The first worker is discarded entirely, which is what closing the editor
    does. Nothing about where the campaign had reached lived in it.
    """
    campaign_id = env.campaign(
        [StepSpec("connection_request"), StepSpec("profile_view"), StepSpec("message")]
    )
    lead_id = env.lead("Ada", public_id="ada")
    env.enrol(campaign_id, [lead_id])
    executors = {
        "connection_request": RecordingExecutor(),
        "profile_view": RecordingExecutor(),
        "message": RecordingExecutor(),
    }

    before = make_worker(env, executors=executors, worker_id="before-restart")
    await before.tick(now=BASE_TIME)
    before.stop(now=at(minutes=1))
    del before

    after = make_worker(env, executors=executors, worker_id="after-restart")
    await after.tick(now=at(minutes=2))
    await after.tick(now=at(minutes=3))

    assert env.lead_state(campaign_id, lead_id)["sublist"] == "successful"
    assert read_heartbeat(env.conn, "before-restart").status == STATUS_STOPPED
    assert campaign_funnel(env.conn, campaign_id)["successful"] == 1


async def test_the_heartbeat_names_the_job_the_worker_is_on(env):
    campaign_id = env.campaign([StepSpec("connection_request")])
    env.enrol(campaign_id, [env.lead("Ada", public_id="ada")])
    seen: list[int | None] = []

    async def watchful(context):
        seen.append(read_heartbeat(env.conn, "w1").current_job_id)
        return ActionResult.ok()

    worker = make_worker(env, executors={"connection_request": watchful})
    await worker.tick(now=BASE_TIME)

    assert seen == [1]
    assert read_heartbeat(env.conn, "w1").status == "idle"


async def test_a_worker_that_never_ticks_again_is_reported_stalled(env):
    """The heartbeat is written before the work, so a wedge is visible.

    Here the worker gets as far as running a job and then the process is gone.
    Two hours later the report says stalled rather than running, and it says the
    campaign is not running even though every row about that campaign still
    claims it is.
    """
    campaign_id = env.campaign([StepSpec("connection_request")])
    env.enrol(campaign_id, [env.lead("Ada", public_id="ada")])
    worker = make_worker(
        env,
        executors={"connection_request": CrashingExecutor()},
        worker_id="wedged",
    )
    with pytest.raises(CrashingExecutor.Death):
        await worker.tick(now=BASE_TIME)

    report = worker_status(
        env.conn, account_id=env.account_id, now=at(hours=2), stalled_after_seconds=180
    )

    assert report["stalled_workers"] == 1
    assert report["campaigns_running"] is False
    assert report["workers"][0]["status"] == "running"


async def test_pacing_between_actions_routes_through_the_humanizer(env):
    """CORE-04 owns every delay. The tick loop does not get its own.

    `tests/test_humanize.py` forbids a raw sleep outside that layer, and the loop
    between two invitations is exactly where one is tempting.
    """
    campaign_id = env.campaign([StepSpec("connection_request")])
    env.enrol(campaign_id, env.leads(3))
    worker = build_worker(
        env.conn,
        env.account_id,
        worker_id="w1",
        executors={"connection_request": RecordingExecutor()},
        clock=env.clock,
        pace_between_actions=True,
        sweep_every_ticks=1,
    )

    await worker.tick(now=BASE_TIME)

    assert len(env.sleeper.calls) >= 2
    assert all(seconds > 0 for seconds in env.sleeper.calls)


async def test_run_forever_stops_when_asked_and_records_it(env):
    campaign_id = env.campaign([StepSpec("connection_request")])
    env.enrol(campaign_id, [env.lead("Ada", public_id="ada")])
    worker = make_worker(
        env,
        executors={"connection_request": RecordingExecutor()},
        tick_seconds=0.001,
    )

    reports = await worker.run_forever(max_ticks=3)

    assert len(reports) == 3
    assert read_heartbeat(env.conn, "w1").status == STATUS_STOPPED
    assert worker_status(env.conn, account_id=env.account_id)["campaigns_running"] is False


async def test_a_bounded_run_does_not_wait_out_an_interval_it_will_never_use(env):
    """`--once` must exit at once, not thirty seconds later.

    The tick interval here is an hour. If the loop waited before re-checking its
    own bound, this would take an hour; the five second guard turns that into a
    failure instead of a hang.
    """
    campaign_id = env.campaign([StepSpec("filter", config={"filter": "always"})])
    env.enrol(campaign_id, [env.lead("Ada", public_id="ada")])
    worker = make_worker(env, tick_seconds=3600)

    reports = await asyncio.wait_for(worker.run_forever(max_ticks=1), timeout=5)

    assert len(reports) == 1
    assert read_heartbeat(env.conn, "w1").status == STATUS_STOPPED
