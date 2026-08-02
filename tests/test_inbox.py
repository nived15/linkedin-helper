"""SEQ-03: the inbox scanner, its poll policy, its delta path and its archive.

Every test is offline and deterministic. There is no browser, no LinkedIn
session and no wall-clock sleep: the clock is injected the way the sequences API
does it with `now=`, the humanizer records delays instead of taking them, and
every connection is closed before the temporary directory goes away.

`tests/test_inbox_val02.py` holds VAL-02, the acceptance test.
"""

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from test_inbox_fakes import (
    InboxPage,
    Thread,
    conversation,
    quiet_threads,
)
from test_scrape_fakes import FakeGate, FakeRecorder, RecordingSleep

from linkedin_mcp.browser.humanize import FAST, Humanizer
from linkedin_mcp.core.config import METERED_ACTIONS, UNMETERED_ACTIONS
from linkedin_mcp.core.db import initialize_database
from linkedin_mcp.inbox import (
    DEFAULT_POLL_SECONDS,
    DELTA_THREAD_LIMIT,
    FIRST_RUN_THREAD_LIMIT,
    INBOUND,
    INBOX_ACTION,
    INBOX_SOURCE,
    INBOX_URL,
    OUTBOUND,
    PARTICIPANT_PREFIX,
    WATERMARK_LIMIT,
    InboxScanState,
    InboxThread,
    PollIntervalTooShortError,
    ThreadMessage,
    active_campaign_leads,
    archive_thread,
    extract_threads,
    match_thread,
    next_scan_at,
    participant_alias,
    read_scan_state,
    read_thread_messages,
    resolve_lead,
    resolve_poll_seconds,
    run_inbox_scan,
    scan_due,
    terminate_sequences,
    thread_identities,
    thread_key,
    thread_urn_from,
    trim_watermark,
)
from linkedin_mcp.leads import create_lead
from linkedin_mcp.scrape.paginate import SearchCursor, StopReason
from linkedin_mcp.sequences import (
    JobState,
    StepSpec,
    Sublist,
    create_campaign,
    define_steps,
    due_jobs,
    enrol_lead,
    get_campaign_lead,
    list_jobs,
    open_job_for_lead,
    set_campaign_status,
)

NOW = datetime(2026, 8, 2, 9, 0, tzinfo=timezone.utc)
LEAD_SLUG = "ada-lovelace"
LEAD_NAME = "Ada Lovelace"


def run(coroutine):
    return asyncio.run(coroutine)


def pacer():
    return Humanizer(FAST, seed=7, sleep=RecordingSleep())


def at(seconds: float) -> datetime:
    return NOW + timedelta(seconds=seconds)


def clock(moment: datetime = NOW):
    return lambda: moment


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
def lead(conn, account):
    return create_lead(conn, account, LEAD_NAME, public_id=LEAD_SLUG).id


def invite_then_message() -> list[StepSpec]:
    return [
        StepSpec("connection_request", config={"priority": 5}),
        StepSpec("message", config={"delay_seconds": 86400}),
    ]


@pytest.fixture()
def campaign(conn, account):
    created = create_campaign(conn, account, "Q3 platform teams", status="active")
    define_steps(conn, created.id, invite_then_message())
    return created


def replied_thread(thread_id: str = "thread-ada", **overrides) -> Thread:
    fields = {
        "slug": LEAD_SLUG,
        "name": LEAD_NAME,
        "preview": "Yes, happy to chat. Thursday works.",
        "messages": conversation(lead_slug=LEAD_SLUG, lead_name=LEAD_NAME),
    }
    fields.update(overrides)
    return Thread(thread_id, **fields)


def scan(page, conn, account_id, *, gate=None, recorder=None, moment=NOW, **kwargs):
    return run(
        run_inbox_scan(
            page,
            conn,
            account_id,
            humanizer=pacer(),
            guard=gate or FakeGate(),
            record=recorder or FakeRecorder(),
            clock=clock(moment),
            **kwargs,
        )
    )


def run_row(conn, account_id):
    row = conn.execute(
        "SELECT * FROM harvest_runs WHERE account_id = ? AND source_type = ?"
        " ORDER BY id DESC LIMIT 1",
        (account_id, INBOX_SOURCE),
    ).fetchone()
    return None if row is None else dict(row)


def stored_messages(conn, account_id):
    return [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM messages WHERE account_id = ? ORDER BY id", (account_id,)
        ).fetchall()
    ]


# --------------------------------------------------------------------------
# The poll interval: one hour is a floor, three hours is the default
# --------------------------------------------------------------------------


def test_the_default_interval_is_three_hours_and_the_floor_is_one():
    assert DEFAULT_POLL_SECONDS == 3 * 3600
    assert resolve_poll_seconds(None) == 3 * 3600


@pytest.mark.parametrize("requested", [1, 60, 300, 3599])
def test_asking_to_poll_faster_than_the_floor_is_clamped_not_obeyed(requested):
    assert resolve_poll_seconds(requested) == 3600


@pytest.mark.parametrize("requested", [1, 300, 3599])
def test_a_strict_caller_is_refused_rather_than_quietly_slowed(requested):
    with pytest.raises(PollIntervalTooShortError) as error:
        resolve_poll_seconds(requested, strict=True)

    assert error.value.requested == requested
    assert error.value.minimum == 3600


def test_an_interval_at_or_above_the_floor_is_honoured():
    assert resolve_poll_seconds(3600) == 3600
    assert resolve_poll_seconds(6 * 3600) == 6 * 3600


def test_an_interval_that_is_not_a_number_is_refused():
    with pytest.raises(ValueError):
        resolve_poll_seconds("whenever")


# --------------------------------------------------------------------------
# Due checks. A policy value and a comparison, never a timer
# --------------------------------------------------------------------------


def test_an_account_that_has_never_been_scanned_is_always_due(conn, account):
    assert scan_due(conn, account, now=NOW) is True
    assert next_scan_at(conn, account) is None
    assert read_scan_state(conn, account).first_run is True


def test_a_scanned_account_is_not_due_again_until_the_interval_has_passed(
    conn, account, lead
):
    page = InboxPage([replied_thread()], per_slice=10)
    scan(page, conn, account, moment=NOW)

    assert scan_due(conn, account, now=NOW) is False
    assert scan_due(conn, account, now=at(DEFAULT_POLL_SECONDS - 1)) is False
    assert scan_due(conn, account, now=at(DEFAULT_POLL_SECONDS)) is True


def test_the_stored_interval_is_what_the_next_due_check_uses(conn, account, lead):
    page = InboxPage([replied_thread()], per_slice=10)
    scan(page, conn, account, moment=NOW, poll_seconds=6 * 3600)

    assert read_scan_state(conn, account).poll_seconds == 6 * 3600
    assert scan_due(conn, account, now=at(3 * 3600)) is False
    assert scan_due(conn, account, now=at(6 * 3600)) is True


def test_a_due_check_override_below_the_floor_is_clamped(conn, account, lead):
    page = InboxPage([replied_thread()], per_slice=10)
    scan(page, conn, account, moment=NOW)

    # Asking "is it due after five minutes" must not answer yes.
    assert scan_due(conn, account, now=at(300), poll_seconds=300) is False
    assert scan_due(conn, account, now=at(3600), poll_seconds=300) is True


def test_a_strict_due_check_refuses_an_interval_below_the_floor(conn, account):
    with pytest.raises(PollIntervalTooShortError):
        scan_due(conn, account, now=NOW, poll_seconds=300, strict=True)


def test_a_scan_that_never_finished_leaves_the_account_still_on_its_first_run(
    conn, account
):
    conn.execute(
        "INSERT INTO harvest_runs (account_id, source_type, params_json, started_at)"
        " VALUES (?, ?, '{}', ?)",
        (account, INBOX_SOURCE, "2026-08-02 08:00:00"),
    )
    conn.commit()

    assert read_scan_state(conn, account).first_run is True
    assert scan_due(conn, account, now=NOW) is True


def test_a_stored_interval_below_the_floor_is_clamped_when_it_is_read_back(
    conn, account
):
    conn.execute(
        "INSERT INTO harvest_runs (account_id, source_type, params_json, started_at,"
        " finished_at) VALUES (?, ?, ?, ?, ?)",
        (
            account,
            INBOX_SOURCE,
            json.dumps({"filters": {"poll_seconds": 60}, "cursor": {}}),
            "2026-08-02 08:00:00",
            "2026-08-02 08:00:00",
        ),
    )
    conn.commit()

    assert read_scan_state(conn, account).poll_seconds == 3600


def test_the_state_object_answers_due_without_touching_the_database():
    state = InboxScanState(account_id=1, last_scan_at="2026-08-02 09:00:00")

    assert state.is_due(now=at(3600)) is False
    assert state.is_due(now=at(DEFAULT_POLL_SECONDS)) is True
    assert state.due_at() == "2026-08-02 12:00:00"


# --------------------------------------------------------------------------
# Reading the list and the thread
# --------------------------------------------------------------------------


def test_a_thread_urn_is_read_from_a_messaging_url_or_a_bare_urn():
    assert (
        thread_urn_from("https://www.linkedin.com/messaging/thread/2-abc123==/")
        == "2-abc123=="
    )
    assert thread_urn_from("urn:li:msg_conversation:99") == "urn:li:msg_conversation:99"
    assert thread_urn_from("https://www.linkedin.com/feed/") is None
    assert thread_urn_from(None) is None


def test_a_full_row_reads_into_a_thread():
    page = InboxPage([replied_thread("2-abc")], per_slice=10)

    threads = run(extract_threads(page))

    assert len(threads) == 1
    thread = threads[0]
    assert thread.thread_urn == "2-abc"
    assert thread.participant_public_id == LEAD_SLUG
    assert thread.participant_name == LEAD_NAME
    assert thread.participant_profile_url == f"https://www.linkedin.com/in/{LEAD_SLUG}/"
    assert thread.preview == "Yes, happy to chat. Thursday works."
    assert thread.unread is False


def test_every_optional_field_may_be_missing_without_raising():
    page = InboxPage(
        [
            Thread(
                "2-bare",
                slug="ada-lovelace",
                name=None,
                preview="",
                timestamp="",
                messages=[],
            )
        ],
        per_slice=10,
    )

    threads = run(extract_threads(page))

    assert len(threads) == 1
    thread = threads[0]
    # The name is rebuilt from the slug rather than the row being dropped.
    assert thread.participant_name == "Ada Lovelace"
    assert thread.preview is None
    assert thread.last_activity_text is None
    assert thread.unread is False


def test_a_row_with_neither_a_thread_address_nor_a_participant_is_dropped():
    page = InboxPage(
        [Thread("ignored", slug=None, name=None, thread_href="", messages=[])],
        per_slice=10,
    )

    assert run(extract_threads(page)) == []


def test_an_unread_badge_is_noticed():
    page = InboxPage([replied_thread("2-abc", unread=True)], per_slice=10)

    assert run(extract_threads(page))[0].unread is True


def test_the_signature_changes_when_the_conversation_does():
    quiet = replied_thread("2-abc", preview="Sent an invitation")
    noisy = quiet.touch("Yes, happy to chat.")
    page = InboxPage([quiet], per_slice=10)
    before = run(extract_threads(page))[0]
    page.threads = [noisy]
    after = run(extract_threads(page))[0]

    assert before.thread_urn == after.thread_urn
    assert before.signature != after.signature
    assert before.dedupe_key != after.dedupe_key
    assert after.dedupe_key == thread_key(after.thread_urn, after.signature)


def test_both_directions_are_read_out_of_an_open_thread():
    page = InboxPage([replied_thread("2-abc")], per_slice=10)
    thread = run(extract_threads(page))[0]
    page.open("2-abc")

    messages = run(read_thread_messages(page, thread))

    assert [message.direction for message in messages] == [OUTBOUND, INBOUND]
    assert messages[0].body.startswith("Hi Ada")
    assert messages[1].body.startswith("Yes, happy to chat")
    assert messages[1].sent_at == "2026-08-01 10:04:00"
    assert messages[1].sent_at_text == "10:04 AM"
    assert thread.with_messages(messages).has_reply is True


def test_a_message_with_no_sender_of_its_own_inherits_the_one_above_it():
    """LinkedIn renders a run of messages from one person with one name on top."""
    page = InboxPage(
        [
            replied_thread(
                "2-abc",
                messages=[
                    {
                        "body": "Hi Ada",
                        "sender_slug": "nived-velayudhan",
                        "sender_name": "Nived Velayudhan",
                    },
                    {"body": "One more thought"},
                    {
                        "body": "Yes, happy to chat",
                        "sender_slug": LEAD_SLUG,
                        "sender_name": LEAD_NAME,
                    },
                    {"body": "Thursday works"},
                ],
            )
        ],
        per_slice=10,
    )
    thread = run(extract_threads(page))[0]
    page.open("2-abc")

    directions = [m.direction for m in run(read_thread_messages(page, thread))]

    assert directions == [OUTBOUND, OUTBOUND, INBOUND, INBOUND]


def test_a_message_with_no_attributable_sender_at_all_is_treated_as_outbound():
    """The safe error. A false inbound would cancel every sequence on its own first
    message, which is far worse than failing to notice one reply."""
    page = InboxPage(
        [replied_thread("2-abc", messages=[{"body": "who said this"}])],
        per_slice=10,
    )
    thread = run(extract_threads(page))[0]
    page.open("2-abc")

    messages = run(read_thread_messages(page, thread))

    assert [message.direction for message in messages] == [OUTBOUND]
    assert thread.with_messages(messages).has_reply is False


def test_a_relative_timestamp_is_not_invented_into_an_instant():
    page = InboxPage(
        [
            replied_thread(
                "2-abc",
                messages=[
                    {
                        "body": "Yes, happy to chat",
                        "sender_slug": LEAD_SLUG,
                        "sender_name": LEAD_NAME,
                        "timestamp": "2h",
                    }
                ],
            )
        ],
        per_slice=10,
    )
    thread = run(extract_threads(page))[0]
    page.open("2-abc")

    message = run(read_thread_messages(page, thread))[0]

    assert message.sent_at is None
    assert message.sent_at_text == "2h"


def test_a_message_with_no_body_is_not_archived():
    page = InboxPage(
        [replied_thread("2-abc", messages=[{"body": "", "sender_slug": LEAD_SLUG}])],
        per_slice=10,
    )
    thread = run(extract_threads(page))[0]
    page.open("2-abc")

    assert run(read_thread_messages(page, thread)) == ()


# --------------------------------------------------------------------------
# Archiving. Both directions, and never the same row twice
# --------------------------------------------------------------------------


def sample_thread(messages=None) -> InboxThread:
    if messages is None:
        messages = (
            ThreadMessage(OUTBOUND, "Hi Ada", sent_at="2026-08-01 09:00:00"),
            ThreadMessage(INBOUND, "Yes please", sent_at="2026-08-01 10:04:00"),
        )
    return InboxThread(
        thread_urn="2-abc",
        participant_public_id=LEAD_SLUG,
        participant_name=LEAD_NAME,
        messages=tuple(messages),
    )


def test_both_directions_land_in_messages(conn, account, lead):
    result = archive_thread(conn, account, lead, sample_thread(), detected_at=NOW)

    rows = stored_messages(conn, account)
    assert result.inserted == 2
    assert result.inbound == 1
    assert result.outbound == 1
    assert [row["direction"] for row in rows] == [OUTBOUND, INBOUND]
    assert [row["thread_urn"] for row in rows] == ["2-abc", "2-abc"]
    assert [row["sent_at"] for row in rows] == [
        "2026-08-01 09:00:00",
        "2026-08-01 10:04:00",
    ]
    assert {row["detected_at"] for row in rows} == {"2026-08-02 09:00:00"}


def test_archiving_the_same_thread_twice_writes_nothing_the_second_time(
    conn, account, lead
):
    archive_thread(conn, account, lead, sample_thread(), detected_at=NOW)
    again = archive_thread(conn, account, lead, sample_thread(), detected_at=at(3600))

    assert again.inserted == 0
    assert again.skipped == 2
    assert len(stored_messages(conn, account)) == 2


def test_a_reply_added_later_is_the_only_thing_the_next_archive_writes(
    conn, account, lead
):
    archive_thread(conn, account, lead, sample_thread(), detected_at=NOW)
    grown = sample_thread(
        (
            ThreadMessage(OUTBOUND, "Hi Ada", sent_at="2026-08-01 09:00:00"),
            ThreadMessage(INBOUND, "Yes please", sent_at="2026-08-01 10:04:00"),
            ThreadMessage(INBOUND, "Thursday works", sent_at="2026-08-01 10:05:00"),
        )
    )

    result = archive_thread(conn, account, lead, grown, detected_at=at(3600))

    assert result.inserted == 1
    assert result.skipped == 2
    assert len(stored_messages(conn, account)) == 3


def test_the_same_words_sent_twice_are_two_messages_not_one(conn, account, lead):
    """A set would lose the second 'ok'. The identity is a multiset for this."""
    twice = sample_thread(
        (
            ThreadMessage(INBOUND, "ok"),
            ThreadMessage(INBOUND, "ok"),
        )
    )

    result = archive_thread(conn, account, lead, twice, detected_at=NOW)

    assert result.inserted == 2
    assert len(stored_messages(conn, account)) == 2

    # And re-scanning that thread still writes nothing.
    assert archive_thread(conn, account, lead, twice, detected_at=at(60)).inserted == 0
    assert len(stored_messages(conn, account)) == 2


def test_a_different_thread_with_the_same_words_is_kept_apart(conn, account, lead):
    archive_thread(conn, account, lead, sample_thread(), detected_at=NOW)
    other = InboxThread(
        thread_urn="2-other",
        participant_public_id=LEAD_SLUG,
        messages=(ThreadMessage(INBOUND, "Yes please", sent_at="2026-08-01 10:04:00"),),
    )

    assert archive_thread(conn, account, lead, other, detected_at=NOW).inserted == 1
    assert len(stored_messages(conn, account)) == 3


def test_archiving_a_thread_with_nothing_in_it_is_a_no_op(conn, account, lead):
    result = archive_thread(conn, account, lead, sample_thread(()), detected_at=NOW)

    assert result.inserted == 0
    assert stored_messages(conn, account) == []


# --------------------------------------------------------------------------
# Matching a thread to a lead, and a lead to the queues it is in
# --------------------------------------------------------------------------


def test_a_thread_resolves_to_a_lead_by_slug(conn, account, lead):
    assert resolve_lead(conn, account, sample_thread()).id == lead


def test_a_thread_resolves_to_a_lead_by_member_urn_first(conn, account):
    renamed = create_lead(
        conn, account, LEAD_NAME, member_id="urn:li:member:42", public_id="old-slug"
    ).id
    thread = InboxThread(
        thread_urn="2-abc",
        participant_member_id="urn:li:member:42",
        participant_public_id="new-slug",
    )

    assert resolve_lead(conn, account, thread).id == renamed


def test_a_stranger_resolves_to_nothing_rather_than_raising(conn, account):
    thread = InboxThread(thread_urn="2-abc", participant_public_id="a-recruiter")

    assert resolve_lead(conn, account, thread) is None
    assert match_thread(conn, account, thread).resolved is False
    assert match_thread(conn, account, thread).in_campaign is False


def test_only_runnable_campaigns_and_active_sublists_count_as_queues(
    conn, account, lead, campaign
):
    enrol_lead(conn, campaign.id, lead, now=NOW)
    assert [record.campaign_id for record in active_campaign_leads(conn, account, lead)] == [
        campaign.id
    ]

    set_campaign_status(conn, campaign.id, "paused", now=NOW)
    assert active_campaign_leads(conn, account, lead) == []

    set_campaign_status(conn, campaign.id, "active", now=NOW)
    terminate_sequences(conn, account, lead, now=NOW)
    assert active_campaign_leads(conn, account, lead) == []


def test_a_lead_in_two_campaigns_has_both_stopped(conn, account, lead):
    first = create_campaign(conn, account, "one", status="active")
    define_steps(conn, first.id, invite_then_message())
    second = create_campaign(conn, account, "two", status="active")
    define_steps(conn, second.id, invite_then_message())
    enrol_lead(conn, first.id, lead, now=NOW)
    enrol_lead(conn, second.id, lead, now=NOW)

    stopped = terminate_sequences(conn, account, lead, now=NOW)

    assert stopped == (first.id, second.id)
    for created in (first, second):
        assert get_campaign_lead(conn, created.id, lead).sublist == Sublist.REPLIED.value
        assert open_job_for_lead(conn, created.id, lead) is None


# --------------------------------------------------------------------------
# The scan: budget, first run, and what it writes
# --------------------------------------------------------------------------


def test_the_scan_spends_a_configured_budget_and_invents_no_action_type():
    assert INBOX_ACTION in METERED_ACTIONS
    assert INBOX_ACTION not in UNMETERED_ACTIONS
    assert "inbox_read" not in METERED_ACTIONS | UNMETERED_ACTIONS


def test_the_gate_is_asked_before_every_slice(conn, account, lead):
    page = InboxPage(quiet_threads(30), per_slice=10)
    gate = FakeGate()

    report = scan(page, conn, account, gate=gate, limit=30)

    assert report.slices_fetched == 3
    assert len(gate.calls) == 3
    assert {call["action_type"] for call in gate.calls} == {INBOX_ACTION}
    assert {call["account_id"] for call in gate.calls} == {account}


def test_a_refused_gate_stops_the_run_without_raising(conn, account, lead):
    page = InboxPage(quiet_threads(30), per_slice=10)

    report = scan(page, conn, account, gate=FakeGate(allow=1), limit=30)

    assert report.refused is True
    assert report.stop_reason is StopReason.GATE_REFUSED
    assert report.slices_fetched == 1
    assert report.gate_refusal["reason"] == "daily_cap_reached"


def test_every_slice_is_written_to_the_audit_log(conn, account, lead):
    page = InboxPage(quiet_threads(20), per_slice=10)
    recorder = FakeRecorder()

    scan(page, conn, account, recorder=recorder, limit=20)

    assert [row["action_type"] for row in recorder.rows] == [INBOX_ACTION] * 2
    assert [row["detail"]["source"] for row in recorder.rows] == [INBOX_SOURCE] * 2


def test_the_first_run_walks_back_through_two_hundred_threads(conn, account):
    page = InboxPage(quiet_threads(FIRST_RUN_THREAD_LIMIT + 40), per_slice=10)

    report = scan(page, conn, account, gate=FakeGate(allow=1000))

    assert report.first_run is True
    assert report.threads_changed == FIRST_RUN_THREAD_LIMIT
    assert report.threads_opened == FIRST_RUN_THREAD_LIMIT
    assert report.stop_reason is StopReason.COUNT_REACHED
    assert page.goto_urls == [INBOX_URL]


def test_the_run_row_carries_the_watermark_and_the_interval(conn, account, lead):
    page = InboxPage([replied_thread("2-abc")], per_slice=10)

    report = scan(page, conn, account, poll_seconds=4 * 3600)

    row = run_row(conn, account)
    params = json.loads(row["params_json"])["filters"]
    assert row["source_type"] == INBOX_SOURCE
    assert row["finished_at"] == "2026-08-02 09:00:00"
    assert params["poll_seconds"] == 4 * 3600
    assert params["url"] == INBOX_URL
    assert params["watermark"]["2-abc"]
    assert params["threads_opened"] == 1
    assert report.harvest_run_id == row["id"]
    assert report.scanned_at == "2026-08-02 09:00:00"


def test_the_watermark_is_bounded(conn, account):
    watermark = {f"thread-{index}": "sig" for index in range(WATERMARK_LIMIT + 50)}

    assert len(trim_watermark(watermark)) == WATERMARK_LIMIT
    assert "thread-0" in trim_watermark(watermark)


def test_a_scan_report_serialises_for_a_tool_result(conn, account, lead):
    page = InboxPage([replied_thread("2-abc")], per_slice=10)

    payload = scan(page, conn, account).as_dict()

    assert payload["status"] == "success"
    assert payload["action_type"] == INBOX_ACTION
    assert payload["source"] == INBOX_SOURCE
    assert payload["first_run"] is True
    assert payload["replies_detected"] == 1
    assert json.dumps(payload)


# --------------------------------------------------------------------------
# The delta path, proven by counting fetches rather than by counting rows
# --------------------------------------------------------------------------


def test_the_second_run_does_not_re_read_the_whole_inbox(conn, account):
    """Deltas only, and counted. Message counts alone would not catch a rescan,
    because archiving deduplicates and would hide it."""
    threads = quiet_threads(FIRST_RUN_THREAD_LIMIT)
    page = InboxPage(threads, per_slice=10)

    first = scan(page, conn, account, gate=FakeGate(allow=1000))
    assert first.slices_fetched == 20
    assert first.threads_opened == 200

    second_page = InboxPage(threads, per_slice=10)
    second = scan(second_page, conn, account, moment=at(DEFAULT_POLL_SECONDS))

    assert second.first_run is False
    assert second.slices_fetched == 1
    assert second.threads_opened == 0
    assert second.threads_changed == 0
    assert second.stop_reason is StopReason.NO_NEW_RESULTS
    assert second_page.opens == []


def test_a_thread_with_new_activity_is_the_only_one_the_delta_opens(conn, account, lead):
    threads = quiet_threads(50)
    threads[0] = replied_thread("thread-ada", preview="Sent an invitation", messages=[])
    page = InboxPage(list(threads), per_slice=10)
    scan(page, conn, account, gate=FakeGate(allow=1000))

    answered = replied_thread("thread-ada")
    delta_threads = [answered, *threads[1:]]
    delta_page = InboxPage(delta_threads, per_slice=10)
    delta = scan(delta_page, conn, account, moment=at(DEFAULT_POLL_SECONDS))

    assert delta.threads_changed == 1
    assert delta.threads_opened == 1
    assert delta.slices_fetched <= 2
    assert delta_page.opens == ["thread-ada"]


def test_a_delta_run_is_capped_even_when_everything_changed(conn, account):
    threads = quiet_threads(60)
    page = InboxPage(list(threads), per_slice=10)
    scan(page, conn, account, gate=FakeGate(allow=1000))

    churned = [thread.touch(f"new activity {index}") for index, thread in enumerate(threads)]
    delta_page = InboxPage(churned, per_slice=10)
    delta = scan(
        delta_page, conn, account, gate=FakeGate(allow=1000), moment=at(DEFAULT_POLL_SECONDS)
    )

    assert delta.threads_changed == DELTA_THREAD_LIMIT
    assert delta.stop_reason is StopReason.COUNT_REACHED


def test_re_scanning_an_unchanged_thread_never_duplicates_a_message(conn, account, lead):
    page = InboxPage([replied_thread("2-abc")], per_slice=10)
    first = scan(page, conn, account)

    # Force the same thread to be walked again by resuming with an empty cursor,
    # which is the strongest form of the re-scan: the loop believes it is new.
    again = scan(
        InboxPage([replied_thread("2-abc")], per_slice=10),
        conn,
        account,
        cursor=SearchCursor(),
        moment=at(DEFAULT_POLL_SECONDS),
    )

    assert first.messages_archived == 2
    assert again.threads_opened == 1
    assert again.messages_archived == 0
    assert again.messages_already_stored == 2
    assert len(stored_messages(conn, account)) == 2


def test_an_explicit_cursor_resumes_where_the_caller_says(conn, account):
    threads = quiet_threads(30)
    page = InboxPage(threads, per_slice=10)
    first = scan(page, conn, account, limit=10, gate=FakeGate(allow=1000))

    assert first.cursor.page == 2
    assert len(first.cursor.seen_keys) == 10

    resumed = scan(
        InboxPage(threads, per_slice=10),
        conn,
        account,
        cursor=first.cursor,
        limit=10,
        gate=FakeGate(allow=1000),
        moment=at(DEFAULT_POLL_SECONDS),
    )

    assert resumed.threads_changed == 10
    assert resumed.cursor.collected == 20


def test_a_backfill_the_gate_cut_short_carries_on_instead_of_truncating(conn, account):
    """The dangerous interaction between "deltas only" and "the gate said stop".

    A refused first run has archived the top of the inbox and nothing below it.
    If the next run started from the top it would meet only threads the
    watermark already knows, stop on the first slice, and abandon the other
    hundred and fifty for good. It resumes instead.
    """
    threads = quiet_threads(FIRST_RUN_THREAD_LIMIT)
    page = InboxPage(threads, per_slice=10)

    cut_short = scan(page, conn, account, gate=FakeGate(allow=5))

    assert cut_short.refused is True
    assert cut_short.slices_fetched == 5
    assert cut_short.threads_opened == 50
    assert cut_short.backfill is True

    state = read_scan_state(conn, account)
    assert state.resuming is True
    assert state.backfilling is True
    assert state.thread_limit == FIRST_RUN_THREAD_LIMIT

    carried_on = scan(
        InboxPage(threads, per_slice=10),
        conn,
        account,
        gate=FakeGate(allow=1000),
        moment=at(DEFAULT_POLL_SECONDS),
    )

    assert carried_on.resumed is True
    assert carried_on.backfill is True
    # The rest of the two hundred, and none of the fifty already archived.
    assert carried_on.threads_changed == FIRST_RUN_THREAD_LIMIT - 50
    assert carried_on.threads_opened == FIRST_RUN_THREAD_LIMIT - 50
    assert cut_short.threads_opened + carried_on.threads_opened == FIRST_RUN_THREAD_LIMIT
    watermark = json.loads(run_row(conn, account)["params_json"])["filters"]["watermark"]
    assert len(watermark) == FIRST_RUN_THREAD_LIMIT

    # Once the backfill is done the next run is an ordinary delta again.
    finished = read_scan_state(conn, account)
    assert finished.resuming is False
    assert finished.thread_limit == DELTA_THREAD_LIMIT
    quiet_page = InboxPage(threads, per_slice=10)
    quiet = scan(quiet_page, conn, account, moment=at(DEFAULT_POLL_SECONDS * 2))
    assert quiet.slices_fetched == 1
    assert quiet.threads_opened == 0


def test_a_refused_delta_resumes_at_its_own_limit_not_the_backfill_one(conn, account):
    threads = quiet_threads(FIRST_RUN_THREAD_LIMIT)
    scan(InboxPage(threads, per_slice=10), conn, account, gate=FakeGate(allow=1000))

    churned = [thread.touch(f"new activity {index}") for index, thread in enumerate(threads)]
    scan(
        InboxPage(churned, per_slice=10),
        conn,
        account,
        gate=FakeGate(allow=1),
        moment=at(DEFAULT_POLL_SECONDS),
    )

    state = read_scan_state(conn, account)

    assert state.resuming is True
    assert state.backfilling is False
    assert state.thread_limit == DELTA_THREAD_LIMIT


# --------------------------------------------------------------------------
# Replies, strangers and the queue
# --------------------------------------------------------------------------


def test_a_reply_moves_the_lead_to_replied_and_cancels_its_job(
    conn, account, lead, campaign
):
    enrol_lead(conn, campaign.id, lead, now=NOW)
    assert open_job_for_lead(conn, campaign.id, lead) is not None

    report = scan(InboxPage([replied_thread()], per_slice=10), conn, account)

    assert report.replies_detected == 1
    assert report.leads_replied == (lead,)
    assert report.campaigns_stopped == ((campaign.id, lead),)
    assert report.sequences_terminated == 1
    assert get_campaign_lead(conn, campaign.id, lead).sublist == Sublist.REPLIED.value
    assert open_job_for_lead(conn, campaign.id, lead) is None
    assert due_jobs(conn, account, now=at(86400 * 7)) == []


def test_a_thread_with_no_reply_leaves_the_sequence_alone(conn, account, lead, campaign):
    enrol_lead(conn, campaign.id, lead, now=NOW)
    quiet = replied_thread(
        preview="Sent an invitation",
        messages=conversation(lead_slug=LEAD_SLUG, lead_name=LEAD_NAME, reply=None),
    )

    report = scan(InboxPage([quiet], per_slice=10), conn, account)

    assert report.replies_detected == 0
    assert report.campaigns_stopped == ()
    assert get_campaign_lead(conn, campaign.id, lead).sublist == Sublist.QUEUE.value
    assert open_job_for_lead(conn, campaign.id, lead) is not None
    assert report.messages_archived == 1


def test_a_reply_from_somebody_in_no_campaign_is_archived_and_nothing_breaks(
    conn, account, lead
):
    report = scan(InboxPage([replied_thread()], per_slice=10), conn, account)

    assert report.replies_detected == 1
    assert report.leads_replied == (lead,)
    assert report.campaigns_stopped == ()
    assert report.messages_archived == 2
    assert report.threads_unresolved == ()


def test_a_reply_from_a_total_stranger_is_reported_not_forced_into_anything(
    conn, account, campaign
):
    stranger = Thread(
        "2-recruiter",
        slug="a-recruiter",
        name="A Recruiter",
        preview="Are you open to new roles?",
        messages=conversation(lead_slug="a-recruiter", lead_name="A Recruiter"),
    )

    report = scan(InboxPage([stranger], per_slice=10), conn, account)

    assert report.threads_unresolved == ("2-recruiter",)
    assert report.replies_detected == 0
    assert report.campaigns_stopped == ()
    assert report.messages_archived == 0
    assert stored_messages(conn, account) == []
    # And no lead was invented for them.
    assert conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 0


def test_a_stranger_thread_still_enters_the_watermark(conn, account):
    stranger = Thread(
        "2-recruiter",
        slug="a-recruiter",
        name="A Recruiter",
        messages=conversation(lead_slug="a-recruiter", lead_name="A Recruiter"),
    )
    scan(InboxPage([stranger], per_slice=10), conn, account)

    delta = scan(
        InboxPage([stranger], per_slice=10),
        conn,
        account,
        moment=at(DEFAULT_POLL_SECONDS),
    )

    assert delta.threads_opened == 0


def test_a_thread_that_will_not_open_is_reported_and_retried_next_time(conn, account, lead):
    stuck = replied_thread("2-stuck", openable=False)
    first = scan(InboxPage([stuck], per_slice=10), conn, account)

    assert first.threads_unreadable == ("2-stuck",)
    assert first.threads_opened == 0
    assert first.messages_archived == 0

    # Not watermarked, so the next run tries it again rather than retiring it.
    page = InboxPage([replied_thread("2-stuck")], per_slice=10)
    second = scan(page, conn, account, moment=at(DEFAULT_POLL_SECONDS))

    assert second.threads_opened == 1
    assert second.messages_archived == 2


def test_a_paused_campaign_is_not_stopped_by_a_reply(conn, account, lead, campaign):
    enrol_lead(conn, campaign.id, lead, now=NOW)
    set_campaign_status(conn, campaign.id, "paused", now=NOW)

    report = scan(InboxPage([replied_thread()], per_slice=10), conn, account)

    assert report.campaigns_stopped == ()
    assert get_campaign_lead(conn, campaign.id, lead).sublist == Sublist.QUEUE.value
    assert (
        list_jobs(conn, campaign_id=campaign.id, states=[JobState.PENDING])[0].state
        == JobState.PENDING.value
    )


def test_a_scan_asking_for_no_threads_is_refused(conn, account):
    with pytest.raises(ValueError):
        scan(InboxPage([], per_slice=10), conn, account, limit=0)


# --------------------------------------------------------------------------
# Failure modes that would each break the delta or corrupt the archive
# --------------------------------------------------------------------------


def test_time_passing_is_not_activity(conn, account, lead):
    """The bug that would silently undo the whole delta path.

    Every thread's rendered label creeps from "2h" to "5h" between two scans
    without anything happening in it. If that label fed the change signature,
    every thread would look changed on every scan and each delta would re-read
    and re-open the entire inbox while still producing the right message counts.
    Only a fetch and open count catches it, so that is what is asserted.
    """
    threads = quiet_threads(30)
    scan(InboxPage(list(threads), per_slice=10), conn, account, gate=FakeGate(allow=99))

    later = [thread.restamp("5h") for thread in threads]
    delta_page = InboxPage(later, per_slice=10)
    delta = scan(delta_page, conn, account, moment=at(DEFAULT_POLL_SECONDS))

    assert delta.threads_changed == 0
    assert delta.threads_opened == 0
    assert delta.slices_fetched == 1
    assert delta_page.opens == []


def test_a_pane_still_showing_the_previous_conversation_is_not_archived(
    conn, account, lead
):
    """Messaging is a single page application, so a click can return before the
    right hand pane has caught up. Reading it then would archive one person's
    messages against another person's lead."""
    lagging = replied_thread("2-ada", opens_as="2-somebody-else")
    other = Thread(
        "2-somebody-else",
        slug="somebody-else",
        name="Somebody Else",
        messages=conversation(lead_slug="somebody-else", lead_name="Somebody Else"),
    )
    page = InboxPage([lagging, other], per_slice=10)

    report = scan(page, conn, account)

    assert "2-ada" in report.threads_unreadable
    assert page.opens[0] == "2-ada"
    assert page.showing[0] == "2-somebody-else"
    # Nothing was archived against Ada from a pane that was not hers.
    assert stored_messages(conn, account) == []
    assert report.replies_detected == 0

    # And because it was never watermarked, the next scan tries it again.
    settled = InboxPage([replied_thread("2-ada")], per_slice=10)
    second = scan(settled, conn, account, moment=at(DEFAULT_POLL_SECONDS))
    assert second.threads_opened == 1
    assert second.messages_archived == 2


def test_a_conversation_addressed_two_ways_is_one_conversation():
    """An encoded URN and a bare one are the same thread. Archiving is keyed on
    this value, so letting them differ would duplicate every message in it."""
    encoded = "https://www.linkedin.com/messaging/thread/urn%3Ali%3Amsg_conversation%3A7/"
    plain = "https://www.linkedin.com/messaging/thread/urn:li:msg_conversation:7/"

    assert thread_urn_from(encoded) == thread_urn_from(plain)
    assert thread_urn_from(encoded) == "urn:li:msg_conversation:7"


def test_a_thread_that_gains_a_real_address_does_not_duplicate_its_history(
    conn, account, lead
):
    """A row rendered without a messaging link is archived under a participant
    alias. When a later render supplies the real address the conversation must
    still be recognised, not archived a second time."""
    aliased = InboxThread(
        thread_urn=participant_alias(LEAD_SLUG),
        participant_public_id=LEAD_SLUG,
        participant_name=LEAD_NAME,
        messages=(
            ThreadMessage(OUTBOUND, "Hi Ada", sent_at="2026-08-01 09:00:00"),
            ThreadMessage(INBOUND, "Yes please", sent_at="2026-08-01 10:04:00"),
        ),
    )
    archive_thread(conn, account, lead, aliased, detected_at=NOW)

    addressed = InboxThread(
        thread_urn="urn:li:msg_conversation:7",
        participant_public_id=LEAD_SLUG,
        participant_name=LEAD_NAME,
        messages=aliased.messages,
    )
    result = archive_thread(conn, account, lead, addressed, detected_at=at(3600))

    assert participant_alias(LEAD_SLUG) == f"{PARTICIPANT_PREFIX}{LEAD_SLUG}"
    assert thread_identities(addressed) == (
        "urn:li:msg_conversation:7",
        f"{PARTICIPANT_PREFIX}{LEAD_SLUG}",
    )
    assert result.inserted == 0
    assert result.skipped == 2
    assert len(stored_messages(conn, account)) == 2


def test_a_known_lead_whose_row_has_no_profile_link_is_still_matched(
    conn, account, lead, campaign
):
    """The identity is on the message, not on the list row. Discarding it would
    report a known lead as a stranger and let the follow-up survive."""
    enrol_lead(conn, campaign.id, lead, now=NOW)
    linkless = replied_thread("2-ada", slug=None, name=LEAD_NAME)
    page = InboxPage([linkless], per_slice=10)

    threads = run(extract_threads(page))
    assert threads[0].participant_public_id is None

    report = scan(page, conn, account)

    assert report.threads_unresolved == ()
    assert report.leads_replied == (lead,)
    assert report.campaigns_stopped == ((campaign.id, lead),)
    assert get_campaign_lead(conn, campaign.id, lead).sublist == Sublist.REPLIED.value
    assert open_job_for_lead(conn, campaign.id, lead) is None


def test_a_sender_whose_name_does_not_match_is_not_borrowed_as_the_participant(
    conn, account
):
    """Enrichment matches on the name only. Taking any sender's slug would risk
    picking up Nived's own and stopping his own sequences."""
    thread = InboxThread(
        thread_urn="2-abc",
        participant_name="Ada Lovelace",
        messages=(),
    )

    enriched = thread.with_messages(
        (
            ThreadMessage(
                OUTBOUND,
                "Hi Ada",
                sender_name="Nived Velayudhan",
                sender_public_id="nived-velayudhan",
            ),
        )
    )

    assert enriched.participant_public_id is None


def test_one_campaign_failing_to_stop_does_not_leave_the_others_running(
    conn, account, lead, monkeypatch
):
    """A raise mid-loop would abandon every campaign after it, and each of those
    is a follow-up that goes out tonight."""
    first = create_campaign(conn, account, "one", status="active")
    define_steps(conn, first.id, invite_then_message())
    second = create_campaign(conn, account, "two", status="active")
    define_steps(conn, second.id, invite_then_message())
    enrol_lead(conn, first.id, lead, now=NOW)
    enrol_lead(conn, second.id, lead, now=NOW)

    import linkedin_mcp.inbox.matching as matching

    real = matching.mark_replied

    def explode(connection, campaign_id, lead_id, **kwargs):
        if campaign_id == first.id:
            raise sqlite3.OperationalError("database is locked")
        return real(connection, campaign_id, lead_id, **kwargs)

    monkeypatch.setattr(matching, "mark_replied", explode)

    stopped = terminate_sequences(conn, account, lead, now=NOW)

    assert stopped == (second.id,)
    assert get_campaign_lead(conn, first.id, lead).sublist == Sublist.QUEUE.value
    assert get_campaign_lead(conn, second.id, lead).sublist == Sublist.REPLIED.value
    assert open_job_for_lead(conn, second.id, lead) is None


def test_a_delta_that_hit_its_limit_carries_on_instead_of_stranding_the_backlog(
    conn, account
):
    """More churn than one delta may handle leaves threads underneath the ones
    it took. Starting the next run from the top would meet the watermark on the
    first slice and lose them."""
    threads = quiet_threads(60)
    scan(InboxPage(list(threads), per_slice=10), conn, account, gate=FakeGate(allow=99))

    churned = [thread.touch(f"new activity {index}") for index, thread in enumerate(threads)]
    drained: list[str] = []
    for cycle in range(1, 5):
        page = InboxPage(list(churned), per_slice=10)
        report = scan(
            page,
            conn,
            account,
            gate=FakeGate(allow=99),
            moment=at(DEFAULT_POLL_SECONDS * cycle),
        )
        drained.extend(page.opens)
        if cycle == 1:
            # The first delta takes its limit and leaves a backlog underneath.
            assert report.threads_changed == DELTA_THREAD_LIMIT
            state = read_scan_state(conn, account)
            assert state.resuming is True
            assert state.backfilling is False
            assert state.resume_page > 1
        elif report.threads_changed:
            assert report.resumed is True

    # The backlog drains at the delta rate, and every thread is handled exactly
    # once. Without the resume the second cycle would have found nothing and the
    # last thirty five would have been abandoned.
    assert len(drained) == 60
    assert set(drained) == {thread.thread_id for thread in churned}
    assert read_scan_state(conn, account).resuming is False


def test_a_finished_backfill_is_not_resumed_past_its_two_hundred(conn, account):
    """COUNT_REACHED means something different for a backfill: the walk finished."""
    page = InboxPage(quiet_threads(FIRST_RUN_THREAD_LIMIT + 40), per_slice=10)
    done = scan(page, conn, account, gate=FakeGate(allow=1000))

    assert done.stop_reason is StopReason.COUNT_REACHED
    state = read_scan_state(conn, account)

    assert state.backfill is True
    assert state.resuming is False
    assert state.resume_page == 1
    assert state.thread_limit == DELTA_THREAD_LIMIT


# --------------------------------------------------------------------------
# The backfill lever, which exists because any reply stops the sequence
# --------------------------------------------------------------------------


def test_an_old_reply_found_on_the_first_run_still_stops_the_sequence(
    conn, account, lead, campaign
):
    """Pinned on purpose rather than left to chance.

    The first run walks two hundred conversations back, so a lead who answered
    long before this campaign existed is stopped by it. That is what the issue
    asks for and it is defensible, but it is a surprise, so it is asserted here
    rather than discovered in production.
    """
    enrol_lead(conn, campaign.id, lead, now=NOW)

    report = scan(InboxPage([replied_thread()], per_slice=10), conn, account)

    assert report.first_run is True
    assert report.campaigns_stopped == ((campaign.id, lead),)
    assert get_campaign_lead(conn, campaign.id, lead).sublist == Sublist.REPLIED.value


def test_a_backfill_archives_the_history_without_moving_anybody(
    conn, account, lead, campaign
):
    enrol_lead(conn, campaign.id, lead, now=NOW)

    backfill = scan(
        InboxPage([replied_thread()], per_slice=10),
        conn,
        account,
        terminate_on_reply=False,
    )

    assert backfill.replies_detected == 1
    assert backfill.leads_replied == (lead,)
    assert backfill.campaigns_stopped == ()
    assert backfill.messages_archived == 2
    assert get_campaign_lead(conn, campaign.id, lead).sublist == Sublist.QUEUE.value
    assert open_job_for_lead(conn, campaign.id, lead) is not None

    # The history is now the watermark, so the next scan only acts on what
    # changes from here.
    quiet = scan(
        InboxPage([replied_thread()], per_slice=10),
        conn,
        account,
        moment=at(DEFAULT_POLL_SECONDS),
    )
    assert quiet.threads_opened == 0
    assert get_campaign_lead(conn, campaign.id, lead).sublist == Sublist.QUEUE.value

    # And a genuinely new message does stop it.
    answered = replied_thread(preview="One more thing, are you free Friday?")
    live = scan(
        InboxPage([answered], per_slice=10),
        conn,
        account,
        moment=at(DEFAULT_POLL_SECONDS * 2),
    )
    assert live.campaigns_stopped == ((campaign.id, lead),)
    assert get_campaign_lead(conn, campaign.id, lead).sublist == Sublist.REPLIED.value


def test_the_backfill_choice_is_recorded_on_the_run_row(conn, account, lead):
    scan(
        InboxPage([replied_thread()], per_slice=10),
        conn,
        account,
        terminate_on_reply=False,
    )

    params = json.loads(run_row(conn, account)["params_json"])["filters"]

    assert params["terminate_on_reply"] is False
