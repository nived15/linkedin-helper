"""SEQ-04 job selection: the two lanes, the ordering and the reclaim.

The measurement these tests exist to pin down
---------------------------------------------
`sequences.due_jobs` inner-joins `campaigns` and `campaign_leads`. Run against a
queue that holds a harvest, it returns nothing, and a runner that leased only
through it would leave every harvest MCP-02 (#25) enqueues sitting in the table
forever. `test_due_jobs_is_blind_to_campaign_less_work` is that measurement
written down, so a later change to either side cannot quietly reintroduce it.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from linkedin_mcp.sequences import (
    JobState,
    StepSpec,
    claim_step,
    complete_step,
    due_jobs,
    list_jobs,
    set_campaign_status,
)
from linkedin_mcp.worker import (
    AD_HOC_ORD,
    ad_hoc_due_jobs,
    bunch_jobs,
    is_ad_hoc,
    is_campaign_work,
    is_unroutable,
    reclaim_stranded_ad_hoc,
    select_due_jobs,
    unroutable_open_jobs,
)
from test_worker_support import BASE_TIME, at, env  # noqa: F401

pytestmark = pytest.mark.usefixtures("env")


def three_step_campaign(env, *, bunch_size: int = 1):
    return env.campaign(
        [
            StepSpec("connection_request", bunch_size=bunch_size),
            StepSpec("profile_view", bunch_size=bunch_size),
            StepSpec("message", bunch_size=bunch_size),
        ]
    )


def advance(env, campaign_id: int, lead_id: int, steps: int) -> None:
    """Walk a lead forward without a worker, so its depth is known exactly."""
    for index in range(steps):
        moment = BASE_TIME + timedelta(seconds=index + 1)
        claim_step(env.conn, campaign_id, lead_id, worker_id="setup", now=moment)
        complete_step(
            env.conn, campaign_id, lead_id, now=moment, worker_id="setup"
        )


# ----------------------------------------------------------------------
# The seam
# ----------------------------------------------------------------------


def test_due_jobs_is_blind_to_campaign_less_work(env):
    """The reason this module exists, asserted rather than assumed.

    Nothing is wrong with `due_jobs`: filtering on campaign status and on the
    lead's sub-list is what stops a paused campaign or a lead that replied from
    running. It simply cannot see a job that has no campaign, and the queue is
    allowed to hold those.
    """
    harvest = env.enqueue_ad_hoc("profile_search")

    assert due_jobs(env.conn, env.account_id, now=BASE_TIME) == []
    assert [job.id for job in ad_hoc_due_jobs(env.conn, env.account_id, now=BASE_TIME)] == [
        harvest
    ]


def test_two_campaign_less_jobs_may_sit_in_the_queue_at_once(env):
    """0003's partial index excludes NULLs, so ad-hoc work is unconstrained.

    Campaign work is limited to one open job per lead. Harvests are not leads and
    two of them queued together is normal, so the selection layer has to be able
    to return both.
    """
    first = env.enqueue_ad_hoc("profile_search")
    second = env.enqueue_ad_hoc("post_search")

    found = ad_hoc_due_jobs(env.conn, env.account_id, now=BASE_TIME)

    assert sorted(job.id for job in found) == sorted([first, second])


def test_selection_returns_both_lanes(env):
    campaign_id = three_step_campaign(env)
    lead_id = env.lead("Ada", public_id="ada")
    env.enrol(campaign_id, [lead_id])
    harvest = env.enqueue_ad_hoc("profile_search")

    selection = select_due_jobs(env.conn, env.account_id, now=BASE_TIME)

    assert [job.lead_id for job in selection.campaign[0].jobs] == [lead_id]
    assert [job.id for job in selection.ad_hoc[0].jobs] == [harvest]
    assert len(selection) == 2


def test_a_campaign_backlog_cannot_starve_the_harvest_lane(env):
    """Separate budgets, not one shared one.

    With a single budget the campaign wins every slot for as long as it has work,
    and a harvest queued on Monday runs whenever the campaign happens to run dry.
    That is indistinguishable from the bug this module fixes.
    """
    campaign_id = three_step_campaign(env)
    env.enrol(campaign_id, env.leads(20))
    harvest = env.enqueue_ad_hoc("profile_search")

    selection = select_due_jobs(
        env.conn,
        env.account_id,
        now=BASE_TIME,
        campaign_limit=5,
        ad_hoc_limit=2,
    )

    assert len(selection.campaign) == 5
    assert [job.id for bunch in selection.ad_hoc for job in bunch.jobs] == [harvest]


def test_ad_hoc_work_ignores_campaign_status_because_it_has_no_campaign(env):
    campaign_id = three_step_campaign(env)
    env.enrol(campaign_id, [env.lead("Ada", public_id="ada")])
    set_campaign_status(env.conn, campaign_id, "paused")
    harvest = env.enqueue_ad_hoc("profile_search")

    selection = select_due_jobs(env.conn, env.account_id, now=BASE_TIME)

    assert selection.campaign == ()
    assert [job.id for bunch in selection.ad_hoc for job in bunch.jobs] == [harvest]


def test_work_scheduled_for_later_is_not_due_in_either_lane(env):
    campaign_id = env.campaign([StepSpec("connection_request", config={"delay_seconds": 600})])
    env.enrol(campaign_id, [env.lead("Ada", public_id="ada")], now=BASE_TIME)
    env.enqueue_ad_hoc("profile_search", scheduled_for=at(hours=2))

    assert len(select_due_jobs(env.conn, env.account_id, now=BASE_TIME)) == 0
    assert len(select_due_jobs(env.conn, env.account_id, now=at(hours=3))) == 2


# ----------------------------------------------------------------------
# Shapes
# ----------------------------------------------------------------------


def test_job_shapes_are_classified_by_what_can_drive_them(env):
    campaign_id = three_step_campaign(env)
    lead_id = env.lead("Ada", public_id="ada")
    env.enrol(campaign_id, [lead_id])
    env.enqueue_ad_hoc("profile_search")
    env.enqueue_ad_hoc("profile_view", lead_id=env.lead("Bob", public_id="bob"))

    jobs = {job.id: job for job in list_jobs(env.conn, account_id=env.account_id)}
    campaign_job = next(job for job in jobs.values() if job.campaign_id is not None)
    harvest = next(
        job for job in jobs.values() if job.action_type == "profile_search"
    )
    ad_hoc_visit = next(
        job for job in jobs.values() if job.action_type == "profile_view"
    )

    assert is_campaign_work(campaign_job) and not is_ad_hoc(campaign_job)
    assert is_ad_hoc(harvest) and not is_campaign_work(harvest)
    # An ad-hoc job may still name a lead: the gate's blacklist and dedupe checks
    # want one, and there is still no state machine row to drive.
    assert is_ad_hoc(ad_hoc_visit) and ad_hoc_visit.lead_id is not None


def test_a_campaign_without_a_lead_is_reported_rather_than_run(env):
    """The shape neither lane can execute, made visible instead of silent.

    A job naming a campaign but no lead points at a `campaign_leads` row that
    does not exist. Running it would mean inventing a lead; dropping it would
    recreate the invisible backlog this module exists to prevent.
    """
    campaign_id = three_step_campaign(env)
    orphan = env.enqueue_ad_hoc("message", campaign_id=campaign_id)

    selection = select_due_jobs(env.conn, env.account_id, now=BASE_TIME)

    assert len(selection) == 0
    assert [job.id for job in selection.unroutable] == [orphan]
    assert [job.id for job in unroutable_open_jobs(env.conn, env.account_id)] == [orphan]


# ----------------------------------------------------------------------
# Bottom-up ordering and bunching
# ----------------------------------------------------------------------


def test_leads_deeper_in_the_sequence_go_first(env):
    """Bottom-up: finish the conversations already started.

    Top-down would open a hundred new threads a night while the people who
    answered last week wait for their follow-up, which is both rude and the
    fastest way to build a backlog no cap can drain.
    """
    campaign_id = three_step_campaign(env)
    shallow, middle, deep = env.leads(3)
    env.enrol(campaign_id, [shallow, middle, deep])
    advance(env, campaign_id, middle, 1)
    advance(env, campaign_id, deep, 2)

    selection = select_due_jobs(env.conn, env.account_id, now=at(minutes=5))
    order = [job.lead_id for bunch in selection.campaign for job in bunch.jobs]

    assert order == [deep, middle, shallow]


def test_an_ad_hoc_job_sorts_last_within_its_own_lane(env):
    """It has no step, so it has no depth. Zero is above every real ord."""
    first = env.enqueue_ad_hoc("profile_search", priority=5)
    second = env.enqueue_ad_hoc("post_search", priority=1)

    bunches = bunch_jobs(
        env.conn, ad_hoc_due_jobs(env.conn, env.account_id, now=BASE_TIME)
    )

    assert AD_HOC_ORD == 0
    assert [bunch.jobs[0].id for bunch in bunches] == [first, second]


def test_same_step_jobs_are_bunched_up_to_the_step_bunch_size(env):
    """One kind of action for a while, rather than hopping between three."""
    campaign_id = three_step_campaign(env, bunch_size=3)
    env.enrol(campaign_id, env.leads(5))

    selection = select_due_jobs(env.conn, env.account_id, now=BASE_TIME, campaign_limit=5)

    assert [len(bunch) for bunch in selection.campaign] == [3, 2]
    assert all(bunch.action_type == "connection_request" for bunch in selection.campaign)


def test_a_bunch_never_spends_more_than_the_tick_budget(env):
    campaign_id = three_step_campaign(env, bunch_size=10)
    env.enrol(campaign_id, env.leads(8))

    selection = select_due_jobs(env.conn, env.account_id, now=BASE_TIME, campaign_limit=3)

    assert len(selection) == 3
    assert [len(bunch) for bunch in selection.campaign] == [3]


def test_bunching_does_not_mix_two_steps_into_one_batch(env):
    campaign_id = three_step_campaign(env, bunch_size=5)
    shallow_a, shallow_b, deep = env.leads(3)
    env.enrol(campaign_id, [shallow_a, shallow_b, deep])
    advance(env, campaign_id, deep, 1)

    selection = select_due_jobs(env.conn, env.account_id, now=at(minutes=5))

    assert [bunch.action_type for bunch in selection.campaign] == [
        "profile_view",
        "connection_request",
    ]
    assert [len(bunch) for bunch in selection.campaign] == [1, 2]
    assert sorted(selection.campaign[1].jobs[index].lead_id for index in range(2)) == sorted(
        [shallow_a, shallow_b]
    )


# ----------------------------------------------------------------------
# Reclaiming an ad-hoc lease
# ----------------------------------------------------------------------


def test_a_dead_workers_ad_hoc_job_is_reclaimed_once_its_lease_expires(env):
    """`recover_stranded` sweeps `campaign_leads`, so it cannot see this job.

    Without a second sweep an ad-hoc job leased by a worker that then died would
    stay `leased` forever, which is exactly the stranding a lease is supposed to
    make impossible.
    """
    harvest = env.enqueue_ad_hoc("profile_search")
    with env.conn:
        env.conn.execute(
            "UPDATE jobs SET state = ?, locked_by = ?, locked_at = ?, attempts = 1 WHERE id = ?",
            (JobState.LEASED.value, "dead-worker", "2026-03-09 09:30:00", harvest),
        )

    still_fresh = reclaim_stranded_ad_hoc(
        env.conn, account_id=env.account_id, now=at(minutes=5), lease_seconds=900
    )
    assert still_fresh == ()
    assert env.job(harvest)["state"] == JobState.LEASED.value

    reclaimed = reclaim_stranded_ad_hoc(
        env.conn, account_id=env.account_id, now=at(minutes=30), lease_seconds=900
    )

    assert reclaimed == (harvest,)
    row = env.job(harvest)
    assert row["state"] == JobState.PENDING.value
    assert row["locked_by"] is None
    assert row["last_error"] == "reclaimed_stale_lease"
    # The attempt the dead worker spent is kept. A harvest that keeps killing its
    # worker must not retry forever with a clean record.
    assert row["attempts"] == 1


def test_reclaiming_leaves_campaign_jobs_to_the_sequence_engine(env):
    """One sweep per shape, and neither reaches into the other's rows.

    A campaign job's lease is `campaign_leads.sublist`, and moving the job
    without moving the lead would leave the state machine describing something
    that is not true.
    """
    campaign_id = three_step_campaign(env)
    lead_id = env.lead("Ada", public_id="ada")
    env.enrol(campaign_id, [lead_id])
    claim_step(env.conn, campaign_id, lead_id, worker_id="dead-worker", now=BASE_TIME)

    reclaimed = reclaim_stranded_ad_hoc(
        env.conn, account_id=env.account_id, now=at(hours=5), lease_seconds=900
    )

    assert reclaimed == ()
    assert env.lead_state(campaign_id, lead_id)["sublist"] == "processing"
