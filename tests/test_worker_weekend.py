"""The Phase 3 exit criterion, driven by a clock instead of by a weekend.

The criterion, verbatim: a four-step campaign (ICP filter, Invite, wait 3 days +
filter to 1st, Message) runs across a weekend, respects working hours and caps,
stops for repliers, and reports a correct funnel.

A real weekend cannot be waited for, so the worker's clock is injected and the
test ticks it forward an hour at a time from Friday afternoon to the following
Friday. Everything else is real: the same state machine, the same safety gate,
the same `actions_log`, the same selection. Only the browser is a fake, and the
only thing it fakes is whether the click worked.

Timeline this test walks through
--------------------------------
====================  ==================================================
Fri 14:00             ICP filter resolves for all six leads
Fri 15:00             two invitations; the daily cap of two is now spent
Fri 21:00 - Mon 09:00 account closed, nothing at all happens
Mon 08:00             two invitees accept
Mon 09:00             the rolling day has rolled; the last two invitations
Mon 15:00 - 16:00     the two accepted leads pass the 1st-degree filter and
                      are messaged
Tue 08:00             one invitee replies, which ends its sequence
Thu 09:00             the never-accepted lead fails the filter and is skipped
====================  ==================================================
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone

import pytest

from linkedin_mcp.audit import Outcome
from linkedin_mcp.core.config import INVITE_ACTION
from linkedin_mcp.sequences import (
    JobState,
    StepSpec,
    list_jobs,
    mark_replied,
    transaction,
)
from linkedin_mcp.worker import (
    ActionResult,
    build_worker,
    campaign_funnel,
    worker_status,
)
from test_worker_support import (  # noqa: F401
    FIVE_PM,
    NINE_AM,
    RecordingExecutor,
    WEEKDAYS,
    env,
)

pytestmark = [pytest.mark.usefixtures("env"), pytest.mark.asyncio]

FRIDAY_AFTERNOON = datetime(2026, 3, 13, 14, 0, tzinfo=timezone.utc)
RUN_HOURS = 7 * 24
INVITES_PER_DAY = 2
THREE_DAYS = 3 * 24 * 3600

ACCEPTANCES_AT = datetime(2026, 3, 16, 8, 0, tzinfo=timezone.utc)  # Monday
REPLY_AT = datetime(2026, 3, 17, 8, 0, tzinfo=timezone.utc)  # Tuesday

STORED = "%Y-%m-%d %H:%M:%S"


def four_step_campaign(env) -> int:
    return env.campaign(
        [
            StepSpec(
                "filter",
                config={"filter": "headline_contains", "contains": ["Engineering"]},
            ),
            StepSpec("connection_request"),
            StepSpec(
                "filter",
                config={"filter": "is_connected", "delay_seconds": THREE_DAYS},
            ),
            StepSpec("message"),
        ],
        name="Weekend run",
        approval_mode="auto",
    )


def parse(moment: str) -> datetime:
    return datetime.strptime(moment, STORED).replace(tzinfo=timezone.utc)


def accept(env, lead_id: int) -> None:
    """Mark an invitation as accepted, the way the inbox scanner eventually will."""
    with transaction(env.conn):
        env.conn.execute(
            "UPDATE leads SET member_distance = '1st' WHERE id = ?", (lead_id,)
        )


async def test_a_four_step_campaign_runs_across_a_weekend(env):
    env.set_working_hours(WEEKDAYS, NINE_AM, FIVE_PM)
    env.set_cap(INVITE_ACTION, daily_cap=INVITES_PER_DAY)

    campaign_id = four_step_campaign(env)
    accepts_early = env.lead("Ada", public_id="ada", headline="VP Engineering")
    accepts_late = env.lead("Ben", public_id="ben", headline="Head of Engineering")
    replies = env.lead("Cleo", public_id="cleo", headline="Engineering Manager")
    never_accepts = env.lead("Dev", public_id="dev", headline="Engineering Director")
    off_profile_a = env.lead("Eve", public_id="eve", headline="Student")
    off_profile_b = env.lead("Fay", public_id="fay", headline="Chef")
    everyone = [
        accepts_early,
        accepts_late,
        replies,
        never_accepts,
        off_profile_a,
        off_profile_b,
    ]
    env.enrol(campaign_id, everyone, now=FRIDAY_AFTERNOON)

    invites = RecordingExecutor(ActionResult.ok(invited=True))
    messages = RecordingExecutor(ActionResult.ok(sent=True))
    worker = build_worker(
        env.conn,
        env.account_id,
        worker_id="weekend",
        executors={"connection_request": invites, "message": messages},
        clock=lambda: FRIDAY_AFTERNOON,
        pace_between_actions=False,
        sweep_every_ticks=4,
    )

    accepted = False
    replied = False
    reports = []
    for hour in range(RUN_HOURS):
        moment = FRIDAY_AFTERNOON + timedelta(hours=hour)
        if not accepted and moment >= ACCEPTANCES_AT:
            accept(env, accepts_early)
            accept(env, accepts_late)
            accepted = True
        if not replied and moment >= REPLY_AT:
            mark_replied(env.conn, campaign_id, replies, now=moment)
            replied = True
        reports.append((moment, await worker.tick(now=moment)))

    # ------------------------------------------------------------------
    # The weekend was crossed rather than skipped over
    # ------------------------------------------------------------------
    weekend = [
        (moment, report)
        for moment, report in reports
        if moment.weekday() not in WEEKDAYS
    ]
    assert len(weekend) == 48, "two full closed days should have been ticked through"
    assert all(report.within_working_hours is False for _, report in weekend)
    assert all(report.jobs == () for _, report in weekend)
    # Work really was waiting the whole time, so the quiet weekend is the gate
    # holding rather than an empty queue.
    assert any(report.deferred_metered for _, report in weekend)

    # ------------------------------------------------------------------
    # Working hours
    # ------------------------------------------------------------------
    rows = env.logged()
    assert rows, "the run produced no audit trail at all"
    for row in rows:
        occurred = parse(row["occurred_at"])
        assert occurred.weekday() in WEEKDAYS, f"{row['action_type']} ran at a weekend"
        minute = occurred.hour * 60 + occurred.minute
        assert NINE_AM <= minute < FIVE_PM, (
            f"{row['action_type']} ran at {row['occurred_at']}, outside the window"
        )

    # ------------------------------------------------------------------
    # Caps
    # ------------------------------------------------------------------
    sent_invites = [
        parse(row["occurred_at"])
        for row in env.logged(action_type=INVITE_ACTION, outcome=Outcome.SUCCESS.value)
    ]
    assert len(sent_invites) == 4
    per_day = Counter(moment.date() for moment in sent_invites)
    assert max(per_day.values()) <= INVITES_PER_DAY
    for moment in sent_invites:
        window = [
            other
            for other in sent_invites
            if moment - timedelta(hours=24) < other <= moment
        ]
        assert len(window) <= INVITES_PER_DAY, (
            f"{len(window)} invitations landed in the 24 hours ending {moment}"
        )
    # The weekend really was crossed, rather than everything happening on Friday.
    assert {moment.date() for moment in sent_invites} == {
        FRIDAY_AFTERNOON.date(),
        ACCEPTANCES_AT.date(),
    }

    # ------------------------------------------------------------------
    # Repliers
    # ------------------------------------------------------------------
    messaged = {
        row["lead_id"]
        for row in env.logged(action_type="message", outcome=Outcome.SUCCESS.value)
    }
    assert replies not in messaged
    assert env.lead_state(campaign_id, replies)["sublist"] == "replied"
    assert messages.lead_ids == [accepts_early, accepts_late]

    # ------------------------------------------------------------------
    # The funnel
    # ------------------------------------------------------------------
    funnel = campaign_funnel(env.conn, campaign_id)
    assert funnel == {
        "queue": 0,
        "processing": 0,
        "successful": 2,
        "failed": 0,
        "replied": 1,
        "skipped": 3,
        "excluded": 0,
        "in_flight": 0,
        "finished": 6,
        "total": 6,
    }
    assert env.lead_state(campaign_id, off_profile_a)["sublist"] == "skipped"
    assert env.lead_state(campaign_id, off_profile_b)["sublist"] == "skipped"
    assert env.lead_state(campaign_id, never_accepts)["sublist"] == "skipped"
    assert env.lead_state(campaign_id, accepts_early)["sublist"] == "successful"

    # Nothing is left holding a job, so the queue and the state machine agree.
    assert list_jobs(env.conn, campaign_id=campaign_id, states=[JobState.PENDING]) == []
    assert list_jobs(env.conn, campaign_id=campaign_id, states=[JobState.LEASED]) == []

    # ------------------------------------------------------------------
    # And the report says so
    # ------------------------------------------------------------------
    final_moment = FRIDAY_AFTERNOON + timedelta(hours=RUN_HOURS - 1)
    status = worker_status(
        env.conn, account_id=env.account_id, now=final_moment, stalled_after_seconds=3600
    )
    assert status["campaigns_running"] is True
    assert status["due_jobs"] == 0


async def test_the_same_run_never_invites_the_same_person_twice(env):
    """The dedupe window is the second line of defence behind the lease.

    A lead re-enrolled after finishing gets its invitation refused as a duplicate
    rather than sent again, and `DUPLICATE_ACTION` advances it rather than
    stalling it, because the step is satisfied even though nothing happened.
    """
    env.set_cap(INVITE_ACTION, daily_cap=10)
    campaign_id = env.campaign(
        [StepSpec("connection_request"), StepSpec("profile_view")]
    )
    lead_id = env.lead("Ada", public_id="ada")
    env.enrol(campaign_id, [lead_id], now=FRIDAY_AFTERNOON)
    invites = RecordingExecutor(ActionResult.ok(invited=True))
    worker = build_worker(
        env.conn,
        env.account_id,
        worker_id="dedupe",
        executors={
            "connection_request": invites,
            "profile_view": RecordingExecutor(),
        },
        clock=lambda: FRIDAY_AFTERNOON,
        pace_between_actions=False,
        sweep_every_ticks=1,
    )

    await worker.tick(now=FRIDAY_AFTERNOON)
    assert len(invites) == 1

    from linkedin_mcp.sequences import reset_lead

    reset_lead(env.conn, campaign_id, lead_id, to_ord=1, now=FRIDAY_AFTERNOON)
    report = await worker.tick(now=FRIDAY_AFTERNOON + timedelta(minutes=5))

    assert len(invites) == 1
    assert [job.reason for job in report.jobs] == ["duplicate_action"]
    assert env.lead_state(campaign_id, lead_id)["current_step_ord"] == 2
