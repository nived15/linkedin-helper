"""VAL-02: a reply must stop the queued follow-up before it can ever be sent.

This is the acceptance test named in SEQ-03's definition of done, and it is the
only thing in the issue that a user would ever notice. Everything else is
plumbing that supports it.

The scenario the DoD describes is "send an invite, reply from the target
account, and confirm the queued follow-up never sends". A live LinkedIn account
cannot be driven from a test, so it is reproduced end to end with fakes: a lead
is enrolled, the invite step is claimed and completed exactly as SEQ-04's runner
will do it, the follow-up message is left sitting in the queue and allowed to
become due, and then a reply is delivered through the real scanner reading a
fake messaging page.

The assertion that matters is not the sub-list. A lead marked `replied` while a
`pending` job for it still sits in the queue is precisely the failure this issue
exists to prevent, because the queue is what actually reaches a human. So this
asserts on all three: the lead moved, no open job survives, and a worker-style
selection at any later time offers nothing for that lead.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from test_inbox_fakes import InboxPage, Thread, conversation
from test_scrape_fakes import FakeGate, FakeRecorder, RecordingSleep

from linkedin_mcp.browser.humanize import FAST, Humanizer
from linkedin_mcp.core.db import initialize_database
from linkedin_mcp.inbox import run_inbox_scan
from linkedin_mcp.leads import create_lead
from linkedin_mcp.sequences import (
    JobState,
    StepSpec,
    Sublist,
    claim_step,
    complete_step,
    create_campaign,
    define_steps,
    due_jobs,
    enrol_lead,
    get_campaign_lead,
    list_jobs,
    open_job_for_lead,
)

NOW = datetime(2026, 8, 2, 9, 0, tzinfo=timezone.utc)
FOLLOW_UP_DELAY = 86400
LEAD_SLUG = "ada-lovelace"
LEAD_NAME = "Ada Lovelace"
WORKER = "worker-1"


def at(seconds: float) -> datetime:
    return NOW + timedelta(seconds=seconds)


@pytest.fixture()
def conn(tmp_path):
    connection = initialize_database(tmp_path / "linkedin-helper.db")
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture()
def account(conn):
    cursor = conn.execute(
        "INSERT INTO accounts (label, timezone, state) VALUES (?, ?, ?)",
        ("primary", "Asia/Kolkata", "active"),
    )
    conn.commit()
    return int(cursor.lastrowid)


@pytest.fixture()
def invited(conn, account):
    """A lead who has been sent an invite and has a follow-up message queued.

    This is the exact state SEQ-04's runner leaves a lead in after the first
    step of an invite-then-message campaign succeeds.
    """
    lead_id = create_lead(conn, account, LEAD_NAME, public_id=LEAD_SLUG).id
    campaign = create_campaign(conn, account, "Q3 platform teams", status="active")
    define_steps(
        conn,
        campaign.id,
        [
            StepSpec("connection_request", config={"priority": 5}),
            StepSpec("message", config={"delay_seconds": FOLLOW_UP_DELAY}),
        ],
    )
    enrol_lead(conn, campaign.id, lead_id, now=NOW)

    # Send the invite, the way the runner will: lease it, then land it.
    claim_step(conn, campaign.id, lead_id, worker_id=WORKER, now=NOW)
    complete_step(conn, campaign.id, lead_id, worker_id=WORKER, now=NOW)
    return campaign, lead_id


def answered_inbox() -> InboxPage:
    return InboxPage(
        [
            Thread(
                "2-ada",
                slug=LEAD_SLUG,
                name=LEAD_NAME,
                preview="Yes, happy to chat. Thursday works.",
                unread=True,
                messages=conversation(lead_slug=LEAD_SLUG, lead_name=LEAD_NAME),
            )
        ],
        per_slice=10,
    )


def deliver_reply(conn, account_id, *, moment):
    return asyncio.run(
        run_inbox_scan(
            answered_inbox(),
            conn,
            account_id,
            humanizer=Humanizer(FAST, seed=11, sleep=RecordingSleep()),
            guard=FakeGate(),
            record=FakeRecorder(),
            clock=lambda: moment,
        )
    )


def test_val_02_the_queued_follow_up_never_sends_once_the_lead_replies(
    conn, account, invited
):
    campaign, lead_id = invited

    # The invite went out, so the follow-up message is queued and, a day later,
    # due. A worker asked at that moment would send it.
    queued = open_job_for_lead(conn, campaign.id, lead_id)
    assert queued is not None
    assert queued.action_type == "message"
    assert queued.state == JobState.PENDING.value
    assert [job.id for job in due_jobs(conn, account, now=at(FOLLOW_UP_DELAY))] == [
        queued.id
    ]

    # The lead replies, an hour before the follow-up was due to go out.
    report = deliver_reply(conn, account, moment=at(FOLLOW_UP_DELAY - 3600))

    assert report.replies_detected == 1
    assert report.leads_replied == (lead_id,)
    assert report.campaigns_stopped == ((campaign.id, lead_id),)
    assert report.sequences_terminated == 1

    # 1. The lead moved to Replied.
    record = get_campaign_lead(conn, campaign.id, lead_id)
    assert record.sublist == Sublist.REPLIED.value
    assert record.next_run_at is None

    # 2. No open job survives. This is the assertion that matters.
    assert open_job_for_lead(conn, campaign.id, lead_id) is None
    cancelled = [job for job in list_jobs(conn, campaign_id=campaign.id) if job.id == queued.id]
    assert cancelled[0].state == JobState.CANCELLED.value
    assert cancelled[0].locked_by is None

    # 3. A worker-style selection offers nothing for this lead, at the moment
    #    the follow-up was due and at any point after it.
    for offset in (FOLLOW_UP_DELAY, FOLLOW_UP_DELAY + 3600, FOLLOW_UP_DELAY * 30):
        assert due_jobs(conn, account, now=at(offset)) == []
    assert due_jobs(conn, account, campaign_id=campaign.id, now=at(FOLLOW_UP_DELAY)) == []


def test_val_02_archives_both_directions_of_the_conversation(conn, account, invited):
    campaign, lead_id = invited

    report = deliver_reply(conn, account, moment=at(FOLLOW_UP_DELAY - 3600))

    rows = conn.execute(
        "SELECT direction, body, thread_urn, sent_at, detected_at FROM messages"
        " WHERE account_id = ? AND lead_id = ? ORDER BY id",
        (account, lead_id),
    ).fetchall()

    assert report.messages_archived == 2
    assert [row["direction"] for row in rows] == ["outbound", "inbound"]
    assert rows[0]["body"].startswith("Hi Ada")
    assert rows[1]["body"].startswith("Yes, happy to chat")
    assert {row["thread_urn"] for row in rows} == {"2-ada"}
    assert rows[1]["sent_at"] == "2026-08-01 10:04:00"
    assert {row["detected_at"] for row in rows} == {"2026-08-03 08:00:00"}


def test_val_02_survives_the_scan_running_again(conn, account, invited):
    """The runner will call this every three hours forever. Twice must be safe."""
    campaign, lead_id = invited
    deliver_reply(conn, account, moment=at(FOLLOW_UP_DELAY - 3600))

    second = deliver_reply(conn, account, moment=at(FOLLOW_UP_DELAY * 2))

    assert second.threads_opened == 0
    assert second.messages_archived == 0
    assert second.campaigns_stopped == ()
    assert get_campaign_lead(conn, campaign.id, lead_id).sublist == Sublist.REPLIED.value
    assert open_job_for_lead(conn, campaign.id, lead_id) is None
    assert due_jobs(conn, account, now=at(FOLLOW_UP_DELAY * 3)) == []
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM messages WHERE lead_id = ?", (lead_id,)
        ).fetchone()[0]
        == 2
    )


def test_val_02_a_reply_that_lands_mid_step_still_stops_the_sequence(
    conn, account, invited
):
    """A worker holding the lease is the awkward case: the reply arrives while
    the follow-up is being processed. `mark_replied` fires from `processing` too,
    and cancels the leased job."""
    campaign, lead_id = invited
    claim_step(conn, campaign.id, lead_id, worker_id=WORKER, now=at(FOLLOW_UP_DELAY))
    leased = open_job_for_lead(conn, campaign.id, lead_id)
    assert leased.state == JobState.LEASED.value

    deliver_reply(conn, account, moment=at(FOLLOW_UP_DELAY + 60))

    assert get_campaign_lead(conn, campaign.id, lead_id).sublist == Sublist.REPLIED.value
    assert open_job_for_lead(conn, campaign.id, lead_id) is None
    assert due_jobs(conn, account, now=at(FOLLOW_UP_DELAY * 2)) == []


def test_val_02_leaves_a_lead_who_has_not_replied_alone(conn, account, invited):
    """The negative half. Without it, a scanner that cancelled everything would
    pass every assertion above."""
    campaign, lead_id = invited
    silent = InboxPage(
        [
            Thread(
                "2-ada",
                slug=LEAD_SLUG,
                name=LEAD_NAME,
                preview="You sent an invitation",
                messages=conversation(
                    lead_slug=LEAD_SLUG, lead_name=LEAD_NAME, reply=None
                ),
            )
        ],
        per_slice=10,
    )

    report = asyncio.run(
        run_inbox_scan(
            silent,
            conn,
            account,
            humanizer=Humanizer(FAST, seed=11, sleep=RecordingSleep()),
            guard=FakeGate(),
            record=FakeRecorder(),
            clock=lambda: at(FOLLOW_UP_DELAY - 3600),
        )
    )

    assert report.replies_detected == 0
    assert report.campaigns_stopped == ()
    assert get_campaign_lead(conn, campaign.id, lead_id).sublist == Sublist.QUEUE.value
    surviving = open_job_for_lead(conn, campaign.id, lead_id)
    assert surviving is not None
    assert surviving.state == JobState.PENDING.value
    assert [job.id for job in due_jobs(conn, account, now=at(FOLLOW_UP_DELAY))] == [
        surviving.id
    ]
