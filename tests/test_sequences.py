"""SEQ-01: the campaign state machine, its derived job queue and its transitions.

Every test here is offline and deterministic. Clocks are passed in explicitly so
backoffs and lease expiry are exact rather than slept through, and every
connection is closed before the temporary directory goes away.
"""

import re
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from linkedin_mcp.audit import RefusalReason
from linkedin_mcp.core.db import MIGRATIONS_DIR, initialize_database
from linkedin_mcp.leads import add_tag, blacklist_lead, create_lead, ensure_tag
from linkedin_mcp.sequences import (
    ACTIVE_SUBLISTS,
    REFUSAL_DISPOSITIONS,
    SUBLIST_TRANSITIONS,
    TERMINAL_SUBLISTS,
    CampaignInFlightError,
    Disposition,
    InvalidTransitionError,
    JobState,
    StepDefinitionError,
    StepSpec,
    Sublist,
    add_step,
    can_transition,
    claim_step,
    complete_step,
    create_campaign,
    define_steps,
    enrol_lead,
    enrol_leads,
    exclude_lead,
    fail_step,
    get_campaign_lead,
    list_jobs,
    list_steps,
    mark_replied,
    open_job_for_lead,
    require_campaign_lead,
    reset_filters,
    reset_lead,
    set_campaign_status,
    sublist_counts,
    withdraw_lead,
)

BASE_TIME = datetime(2026, 5, 4, 9, 0, tzinfo=timezone.utc)


def at(seconds: float) -> datetime:
    """Return a moment `seconds` after the fixed base time."""
    return BASE_TIME + timedelta(seconds=seconds)


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


@pytest.fixture(autouse=True)
def clean_filter_registry():
    reset_filters()
    yield
    reset_filters()


def make_lead(conn, account_id: int, name: str, **fields) -> int:
    return create_lead(conn, account_id, name, **fields).id


@pytest.fixture()
def lead(conn, account):
    return make_lead(conn, account, "Ada Lovelace", public_id="ada-lovelace")


def invite_then_message() -> list[StepSpec]:
    return [
        StepSpec("connection_request", config={"priority": 5}),
        StepSpec("message", config={"delay_seconds": 3600}),
    ]


@pytest.fixture()
def campaign(conn, account):
    created = create_campaign(conn, account, "Q3 platform teams", status="active")
    define_steps(conn, created.id, invite_then_message())
    return created


# --------------------------------------------------------------------------
# The vocabulary matches the schema it is stored in
# --------------------------------------------------------------------------


def check_values(table: str, column: str) -> set[str]:
    """Pull a CHECK (col IN (...)) list straight out of the migration files."""
    schema = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(MIGRATIONS_DIR.glob("*.sql"))
    )
    table_sql = re.search(
        rf"CREATE TABLE IF NOT EXISTS {table} \((.*?)\n\);",
        schema,
        re.DOTALL,
    )
    assert table_sql is not None, f"{table} is missing from the migrations"
    constraint = re.search(
        rf"{column} TEXT NOT NULL CHECK \({column} IN \(([^)]*)\)\)",
        table_sql.group(1),
    )
    assert constraint is not None, f"{table}.{column} has no CHECK constraint"
    return {value.strip().strip("'") for value in constraint.group(1).split(",")}


def test_sublist_enum_is_exactly_the_schema_check_constraint():
    assert {member.value for member in Sublist} == check_values("campaign_leads", "sublist")


def test_job_state_enum_is_exactly_the_schema_check_constraint():
    assert {member.value for member in JobState} == check_values("jobs", "state")


def test_the_seven_sublists_split_cleanly_into_active_and_terminal():
    assert set(ACTIVE_SUBLISTS).isdisjoint(TERMINAL_SUBLISTS)
    assert set(ACTIVE_SUBLISTS) | set(TERMINAL_SUBLISTS) == set(Sublist)
    assert len(Sublist) == 7


def test_excluded_is_the_only_sublist_with_no_way_out():
    dead_ends = {
        sublist for sublist, targets in SUBLIST_TRANSITIONS.items() if not targets
    }
    assert dead_ends == {Sublist.EXCLUDED}
    assert can_transition(Sublist.SKIPPED, Sublist.QUEUE)
    assert not can_transition(Sublist.EXCLUDED, Sublist.QUEUE)


def test_processing_is_only_reachable_from_the_queue():
    sources = {
        sublist
        for sublist, targets in SUBLIST_TRANSITIONS.items()
        if Sublist.PROCESSING in targets
    }
    assert sources == {Sublist.QUEUE}


def test_every_refusal_reason_has_a_disposition():
    assert set(REFUSAL_DISPOSITIONS) == set(RefusalReason)
    assert set(REFUSAL_DISPOSITIONS.values()) <= set(Disposition)


# --------------------------------------------------------------------------
# Step definitions persist per campaign and survive a restart
# --------------------------------------------------------------------------


def test_define_steps_numbers_ords_from_one_in_order(conn, campaign):
    steps = list_steps(conn, campaign.id)

    assert [step.ord for step in steps] == [1, 2]
    assert [step.action_type for step in steps] == ["connection_request", "message"]
    assert steps[0].priority == 5
    assert steps[1].delay_seconds == 3600


def test_step_definitions_survive_a_restart(tmp_path):
    db_path = tmp_path / "restart.db"
    first = initialize_database(db_path)
    try:
        cursor = first.execute(
            "INSERT INTO accounts (label, timezone, state) VALUES (?, ?, ?)",
            ("primary", "UTC", "active"),
        )
        first.commit()
        account_id = int(cursor.lastrowid)
        created = create_campaign(first, account_id, "Persisted", status="active")
        define_steps(
            first,
            created.id,
            [
                StepSpec("profile_view"),
                StepSpec(
                    "filter",
                    config={"filter": "is_connected", "on_no_match": "skipped"},
                ),
                StepSpec("message", config={"delay_seconds": 7200}, on_failure="skip"),
            ],
        )
        campaign_id = created.id
    finally:
        first.close()

    second = initialize_database(db_path)
    try:
        steps = list_steps(second, campaign_id)
        assert [step.action_type for step in steps] == ["profile_view", "filter", "message"]
        assert steps[1].is_filter
        assert steps[1].filter_name == "is_connected"
        assert steps[1].no_match_sublist is Sublist.SKIPPED
        assert steps[2].delay_seconds == 7200
        assert steps[2].on_failure == "skip"
    finally:
        second.close()


def test_define_steps_refuses_while_leads_are_in_flight(conn, campaign, lead):
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)

    with pytest.raises(CampaignInFlightError):
        define_steps(conn, campaign.id, [StepSpec("message")])

    assert [step.ord for step in list_steps(conn, campaign.id)] == [1, 2]


def test_define_steps_replaces_when_the_caller_insists(conn, campaign, lead):
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)

    steps = define_steps(conn, campaign.id, [StepSpec("message")], replace=True)

    assert [step.action_type for step in steps] == ["message"]


def test_a_filter_step_must_name_a_filter(conn, campaign):
    with pytest.raises(StepDefinitionError):
        define_steps(conn, campaign.id, [StepSpec("filter")], replace=True)


def test_add_step_inserts_at_an_ord_and_shifts_the_rest(conn, campaign):
    add_step(conn, campaign.id, StepSpec("profile_view"), ord_=1)

    assert [step.action_type for step in list_steps(conn, campaign.id)] == [
        "profile_view",
        "connection_request",
        "message",
    ]


def test_campaign_status_stamps_started_once_and_paused_each_time(conn, account):
    created = create_campaign(conn, account, "Draft first")
    assert created.started_at is None

    started = set_campaign_status(conn, created.id, "active", now=at(0))
    paused = set_campaign_status(conn, created.id, "paused", now=at(60))
    resumed = set_campaign_status(conn, created.id, "active", now=at(120))

    assert started.started_at is not None
    assert paused.paused_at is not None
    assert resumed.started_at == started.started_at


# --------------------------------------------------------------------------
# Enrolment
# --------------------------------------------------------------------------


def test_enrol_lead_queues_the_first_step_with_exactly_one_job(conn, campaign, lead):
    record = enrol_lead(conn, campaign.id, lead, now=BASE_TIME)

    assert record.sublist == Sublist.QUEUE.value
    assert record.current_step_ord == 1
    job = open_job_for_lead(conn, campaign.id, lead)
    assert job is not None
    assert job.action_type == "connection_request"
    assert job.state == JobState.PENDING.value
    assert job.payload == {"step_ord": 1}
    assert job.priority == 5
    assert len(list_jobs(conn, campaign_id=campaign.id)) == 1


def test_enrol_lead_is_idempotent(conn, campaign, lead):
    first = enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    second = enrol_lead(conn, campaign.id, lead, now=at(600))

    assert second == first
    assert len(list_jobs(conn, campaign_id=campaign.id)) == 1


def test_a_blacklisted_lead_enrols_into_excluded_with_no_job(conn, account, campaign):
    barred = make_lead(conn, account, "Blocked Person", public_id="blocked-person")
    blacklist_lead(conn, barred, reason="asked not to be contacted")

    record = enrol_lead(conn, campaign.id, barred, now=BASE_TIME)

    assert record.sublist == Sublist.EXCLUDED.value
    assert record.last_outcome == "blacklisted"
    assert open_job_for_lead(conn, campaign.id, barred) is None


def test_the_campaign_exclude_list_lands_a_lead_in_excluded(conn, account, lead):
    tag = ensure_tag(conn, account, "do-not-contact")
    add_tag(conn, lead, "do-not-contact")
    created = create_campaign(
        conn, account, "Guarded", status="active", exclude_list_id=tag.id
    )
    define_steps(conn, created.id, invite_then_message())

    record = enrol_lead(conn, created.id, lead, now=BASE_TIME)

    assert record.sublist == Sublist.EXCLUDED.value
    assert record.last_outcome == "campaign_exclude_list"


def test_enrol_leads_reports_each_outcome(conn, account, campaign):
    ok = make_lead(conn, account, "Fine Person", public_id="fine-person")
    barred = make_lead(conn, account, "Barred Person", public_id="barred-person")
    blacklist_lead(conn, barred, reason="opt out")
    already = make_lead(conn, account, "Early Person", public_id="early-person")
    enrol_lead(conn, campaign.id, already, now=BASE_TIME)

    summary = enrol_leads(conn, campaign.id, [ok, barred, already], now=BASE_TIME)

    assert summary.enrolled == (ok,)
    assert summary.excluded == (barred,)
    assert summary.already_enrolled == (already,)
    assert summary.total == 3


def test_sublist_counts_always_reports_all_seven_keys(conn, campaign, lead):
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)

    counts = sublist_counts(conn, campaign.id)

    assert set(counts) == {member.value for member in Sublist}
    assert counts["queue"] == 1
    assert counts["replied"] == 0


def test_withdraw_lead_removes_the_row_and_cancels_the_job(conn, campaign, lead):
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)

    assert withdraw_lead(conn, campaign.id, lead) is True

    assert get_campaign_lead(conn, campaign.id, lead) is None
    assert open_job_for_lead(conn, campaign.id, lead) is None
    assert [job.state for job in list_jobs(conn, campaign_id=campaign.id)] == ["cancelled"]


# --------------------------------------------------------------------------
# The step lifecycle
# --------------------------------------------------------------------------


def test_claim_step_leases_the_job_and_moves_the_lead(conn, campaign, lead):
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)

    record = claim_step(conn, campaign.id, lead, worker_id="worker-1", now=at(10))

    assert record.sublist == Sublist.PROCESSING.value
    job = open_job_for_lead(conn, campaign.id, lead)
    assert job.state == JobState.LEASED.value
    assert job.locked_by == "worker-1"
    assert job.attempts == 1


def test_a_second_worker_cannot_claim_a_lead_already_processing(conn, campaign, lead):
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    claim_step(conn, campaign.id, lead, worker_id="worker-1", now=at(10))

    with pytest.raises(InvalidTransitionError):
        claim_step(conn, campaign.id, lead, worker_id="worker-2", now=at(11))


def test_complete_step_advances_and_applies_the_next_steps_delay(conn, campaign, lead):
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    claim_step(conn, campaign.id, lead, worker_id="worker-1", now=at(10))

    record = complete_step(conn, campaign.id, lead, now=at(20))

    assert record.sublist == Sublist.QUEUE.value
    assert record.current_step_ord == 2
    assert record.attempts == 0
    assert record.next_run_at == "2026-05-04 10:00:20"
    job = open_job_for_lead(conn, campaign.id, lead)
    assert job.action_type == "message"
    assert job.payload == {"step_ord": 2}
    states = sorted(job.state for job in list_jobs(conn, campaign_id=campaign.id))
    assert states == ["done", "pending"]


def test_completing_the_last_step_parks_the_lead_past_the_end(conn, campaign, lead):
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    claim_step(conn, campaign.id, lead, worker_id="w", now=at(10))
    complete_step(conn, campaign.id, lead, now=at(20))
    claim_step(conn, campaign.id, lead, worker_id="w", now=at(4000))

    record = complete_step(conn, campaign.id, lead, now=at(4010))

    assert record.sublist == Sublist.SUCCESSFUL.value
    assert record.current_step_ord == 3
    assert record.next_run_at is None
    assert open_job_for_lead(conn, campaign.id, lead) is None


def test_fail_step_retries_with_backoff_then_gives_up(conn, account, lead):
    created = create_campaign(conn, account, "Flaky", status="active")
    define_steps(
        conn,
        created.id,
        [StepSpec("message", config={"max_attempts": 2, "retry_backoff_seconds": 60})],
    )
    enrol_lead(conn, created.id, lead, now=BASE_TIME)

    claim_step(conn, created.id, lead, worker_id="w", now=at(10))
    first = fail_step(conn, created.id, lead, error="timeout", now=at(20))

    assert first.sublist == Sublist.QUEUE.value
    assert first.attempts == 1
    assert first.next_run_at == "2026-05-04 09:01:20"

    claim_step(conn, created.id, lead, worker_id="w", now=at(100))
    second = fail_step(conn, created.id, lead, error="timeout", now=at(110))

    assert second.sublist == Sublist.FAILED.value
    assert second.attempts == 2
    assert open_job_for_lead(conn, created.id, lead) is None


def test_on_failure_skip_drops_the_lead_out_on_the_first_failure(conn, account, lead):
    created = create_campaign(conn, account, "Soft", status="active")
    define_steps(conn, created.id, [StepSpec("message", on_failure="skip")])
    enrol_lead(conn, created.id, lead, now=BASE_TIME)
    claim_step(conn, created.id, lead, worker_id="w", now=at(10))

    record = fail_step(conn, created.id, lead, error="no thread", now=at(20))

    assert record.sublist == Sublist.SKIPPED.value


def test_on_failure_fail_stops_immediately(conn, account, lead):
    created = create_campaign(conn, account, "Strict", status="active")
    define_steps(conn, created.id, [StepSpec("message", on_failure="fail")])
    enrol_lead(conn, created.id, lead, now=BASE_TIME)
    claim_step(conn, created.id, lead, worker_id="w", now=at(10))

    record = fail_step(conn, created.id, lead, error="hard stop", now=at(20))

    assert record.sublist == Sublist.FAILED.value


def test_mark_replied_stops_the_sequence_from_the_queue(conn, campaign, lead):
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)

    record = mark_replied(conn, campaign.id, lead, now=at(30), detail="thanks!")

    assert record.sublist == Sublist.REPLIED.value
    assert record.last_outcome == "replied: thanks!"
    assert open_job_for_lead(conn, campaign.id, lead) is None
    assert [job.state for job in list_jobs(conn, campaign_id=campaign.id)] == ["cancelled"]


def test_mark_replied_also_works_mid_step(conn, campaign, lead):
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    claim_step(conn, campaign.id, lead, worker_id="w", now=at(10))

    record = mark_replied(conn, campaign.id, lead, now=at(20))

    assert record.sublist == Sublist.REPLIED.value


def test_reset_lead_returns_a_terminal_lead_to_the_queue(conn, campaign, lead):
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    mark_replied(conn, campaign.id, lead, now=at(30))

    record = reset_lead(conn, campaign.id, lead, now=at(60))

    assert record.sublist == Sublist.QUEUE.value
    assert record.current_step_ord == 1
    assert open_job_for_lead(conn, campaign.id, lead) is not None


def test_an_excluded_lead_can_never_be_reset(conn, campaign, lead):
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    exclude_lead(conn, campaign.id, lead, reason="wrong company", now=at(30))

    with pytest.raises(InvalidTransitionError):
        reset_lead(conn, campaign.id, lead, now=at(60))

    assert require_campaign_lead(conn, campaign.id, lead).sublist == Sublist.EXCLUDED.value


def test_a_lead_can_never_hold_two_open_jobs(conn, campaign, lead):
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    existing = open_job_for_lead(conn, campaign.id, lead)

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO jobs
                (account_id, campaign_id, lead_id, step_id, action_type, payload_json,
                 scheduled_for, priority, state, attempts)
            VALUES (?, ?, ?, ?, ?, '{}', ?, 0, 'pending', 0)
            """,
            (
                existing.account_id,
                campaign.id,
                lead,
                existing.step_id,
                "message",
                existing.scheduled_for,
            ),
        )
    conn.rollback()
