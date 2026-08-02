"""SEQ-01: lease fencing, nested transactions, and repair of a corrupted queue.

These cover the failure modes that only show up when something goes wrong: a
worker that stalls past its lease, a caller that catches an inner transition
failure and commits anyway, a step list edited under live work, and a database
that was already corrupt before the one-open-job-per-lead index existed.
"""

import shutil
from datetime import datetime, timedelta, timezone

import pytest

from linkedin_mcp.core.db import MIGRATIONS_DIR, initialize_database, migrate
from linkedin_mcp.leads import create_lead
from linkedin_mcp.sequences import (
    InvalidTransitionError,
    JobState,
    StepSpec,
    Sublist,
    add_step,
    claim_step,
    complete_step,
    create_campaign,
    define_steps,
    derive_jobs,
    enrol_lead,
    fail_step,
    list_jobs,
    open_job_for_lead,
    orphan_open_jobs,
    queue_matches_state,
    rebuild_jobs,
    recover_stranded,
    require_campaign_lead,
    step_at_ord,
    transaction,
)
from linkedin_mcp.sequences import jobs as jobs_module

BASE_TIME = datetime(2026, 5, 4, 9, 0, tzinfo=timezone.utc)


def at(seconds: float) -> datetime:
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
        ("primary", "UTC", "active"),
    )
    conn.commit()
    return int(cursor.lastrowid)


@pytest.fixture()
def lead(conn, account):
    return create_lead(conn, account, "Ada Lovelace", public_id="ada-lovelace").id


def simple_campaign(conn, account_id, name="Simple"):
    created = create_campaign(conn, account_id, name, status="active")
    define_steps(conn, created.id, [StepSpec("connection_request"), StepSpec("message")])
    return created


def boom(*args, **kwargs):
    raise RuntimeError("simulated crash")


# --------------------------------------------------------------------------
# A failed transition cannot survive its caller's commit
# --------------------------------------------------------------------------


def test_an_inner_failure_is_unwound_even_when_the_outer_transaction_commits(
    conn, account, lead, monkeypatch
):
    """The nesting case: a caller batches work and swallows one failure.

    Without savepoints, the failed transition's half-written rows would still be
    inside the outer transaction and would be committed along with everything
    else. This is the test that a plain "only the outermost commits" helper
    fails.
    """
    campaign = simple_campaign(conn, account)
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    claim_step(conn, campaign.id, lead, worker_id="w", now=at(10))
    before_lead = require_campaign_lead(conn, campaign.id, lead)
    before_jobs = [
        (job.id, job.state, job.step_id) for job in list_jobs(conn, campaign_id=campaign.id)
    ]

    with transaction(conn):
        conn.execute(
            "UPDATE campaigns SET name = ? WHERE id = ?", ("renamed", campaign.id)
        )
        monkeypatch.setattr(jobs_module, "insert_job", boom)
        with pytest.raises(RuntimeError):
            complete_step(conn, campaign.id, lead, now=at(20))
        monkeypatch.undo()

    # The caller's own write survived, so the outer transaction really did commit.
    assert conn.execute(
        "SELECT name FROM campaigns WHERE id = ?", (campaign.id,)
    ).fetchone()["name"] == "renamed"
    # The failed transition left nothing behind.
    assert require_campaign_lead(conn, campaign.id, lead) == before_lead
    assert [
        (job.id, job.state, job.step_id) for job in list_jobs(conn, campaign_id=campaign.id)
    ] == before_jobs


def test_two_transitions_in_one_block_commit_together(conn, account):
    """The happy nesting case: batching does not break a successful transition."""
    campaign = simple_campaign(conn, account)
    first = create_lead(conn, account, "One", public_id="one").id
    second = create_lead(conn, account, "Two", public_id="two").id

    with transaction(conn):
        enrol_lead(conn, campaign.id, first, now=BASE_TIME)
        enrol_lead(conn, campaign.id, second, now=BASE_TIME)

    assert open_job_for_lead(conn, campaign.id, first) is not None
    assert open_job_for_lead(conn, campaign.id, second) is not None


# --------------------------------------------------------------------------
# A stalled worker is fenced out
# --------------------------------------------------------------------------


def test_a_stalled_worker_cannot_finish_a_lead_the_sweep_took_back(conn, account, lead):
    campaign = simple_campaign(conn, account)
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    claim_step(conn, campaign.id, lead, worker_id="slow", now=at(10))

    assert recover_stranded(conn, campaign.id, now=at(10_000)) == (lead,)

    with pytest.raises(InvalidTransitionError):
        complete_step(conn, campaign.id, lead, now=at(10_010))

    record = require_campaign_lead(conn, campaign.id, lead)
    assert record.sublist == Sublist.QUEUE.value
    assert record.current_step_ord == 1


def test_a_stalled_worker_cannot_finish_a_lead_another_worker_took_over(
    conn, account, lead
):
    campaign = simple_campaign(conn, account)
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    claim_step(conn, campaign.id, lead, worker_id="slow", now=at(10))
    recover_stranded(conn, campaign.id, now=at(10_000))
    claim_step(conn, campaign.id, lead, worker_id="fast", now=at(10_010))

    with pytest.raises(InvalidTransitionError):
        complete_step(conn, campaign.id, lead, worker_id="slow", now=at(10_020))

    # The rightful holder is unaffected.
    record = complete_step(conn, campaign.id, lead, worker_id="fast", now=at(10_030))
    assert record.current_step_ord == 2


def test_a_stalled_worker_cannot_fail_a_lead_it_no_longer_holds(conn, account, lead):
    campaign = simple_campaign(conn, account)
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    claim_step(conn, campaign.id, lead, worker_id="slow", now=at(10))
    recover_stranded(conn, campaign.id, now=at(10_000))
    claim_step(conn, campaign.id, lead, worker_id="fast", now=at(10_010))

    with pytest.raises(InvalidTransitionError):
        fail_step(conn, campaign.id, lead, error="late", worker_id="slow", now=at(10_020))

    assert require_campaign_lead(conn, campaign.id, lead).sublist == Sublist.PROCESSING.value


def test_a_step_cannot_be_completed_without_being_claimed(conn, account, lead):
    campaign = simple_campaign(conn, account)
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)

    with pytest.raises(InvalidTransitionError):
        complete_step(conn, campaign.id, lead, now=at(20))


# --------------------------------------------------------------------------
# Editing the step list does not trample live work
# --------------------------------------------------------------------------


def test_appending_a_step_leaves_a_live_lease_untouched(conn, account, lead):
    campaign = simple_campaign(conn, account)
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    claim_step(conn, campaign.id, lead, worker_id="w", now=at(10))
    before = open_job_for_lead(conn, campaign.id, lead)

    add_step(conn, campaign.id, StepSpec("post_like"))

    after = open_job_for_lead(conn, campaign.id, lead)
    assert after.id == before.id
    assert after.state == JobState.LEASED.value
    assert after.locked_by == "w"
    assert require_campaign_lead(conn, campaign.id, lead).sublist == Sublist.PROCESSING.value


def test_inserting_a_step_mid_list_keeps_leads_on_the_same_action(conn, account, lead):
    campaign = simple_campaign(conn, account)
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    claim_step(conn, campaign.id, lead, worker_id="w", now=at(10))
    complete_step(conn, campaign.id, lead, worker_id="w", now=at(20))
    message_step_id = step_at_ord(conn, campaign.id, 2).id
    assert require_campaign_lead(conn, campaign.id, lead).current_step_ord == 2

    add_step(conn, campaign.id, StepSpec("profile_view"), ord_=2)

    assert step_at_ord(conn, campaign.id, 3).id == message_step_id
    record = require_campaign_lead(conn, campaign.id, lead)
    assert record.current_step_ord == 3
    assert open_job_for_lead(conn, campaign.id, lead).action_type == "message"
    assert queue_matches_state(conn, campaign.id, now=at(50))


def test_a_definition_only_rebuild_preserves_a_live_lease(conn, account, lead):
    campaign = simple_campaign(conn, account)
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    claim_step(conn, campaign.id, lead, worker_id="w", now=at(10))
    before = open_job_for_lead(conn, campaign.id, lead)

    report = rebuild_jobs(conn, campaign.id, now=at(50), recover_processing=False)

    after = open_job_for_lead(conn, campaign.id, lead)
    assert after.id == before.id
    assert after.state == JobState.LEASED.value
    assert after.locked_by == "w"
    assert report.deleted == 0
    assert report.created == 0
    assert report.requeued == 0


# --------------------------------------------------------------------------
# Repairing a queue that is already wrong
# --------------------------------------------------------------------------


def test_a_rebuild_persists_a_due_time_so_derivation_stops_tracking_the_clock(
    conn, account, lead
):
    campaign = simple_campaign(conn, account)
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    conn.execute(
        "UPDATE campaign_leads SET next_run_at = NULL WHERE campaign_id = ?",
        (campaign.id,),
    )
    conn.commit()
    # Before the repair, two derivations moments apart disagree.
    assert derive_jobs(conn, campaign.id, now=at(0)) != derive_jobs(
        conn, campaign.id, now=at(9999)
    )

    rebuild_jobs(conn, campaign.id, now=at(50))

    assert require_campaign_lead(conn, campaign.id, lead).next_run_at == "2026-05-04 09:00:50"
    assert derive_jobs(conn, campaign.id, now=at(0)) == derive_jobs(
        conn, campaign.id, now=at(9999)
    )
    assert queue_matches_state(conn, campaign.id, now=at(99_999))


def test_a_lead_stranded_in_a_gap_is_moved_forward_not_declared_finished(
    conn, account, lead
):
    campaign = create_campaign(conn, account, "Gapped", status="active")
    define_steps(
        conn,
        campaign.id,
        [StepSpec("profile_view"), StepSpec("connection_request"), StepSpec("message")],
    )
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    conn.execute(
        "UPDATE campaign_leads SET current_step_ord = 2 WHERE campaign_id = ?",
        (campaign.id,),
    )
    # A hole in the middle of the list, which is not the same thing as the end.
    conn.execute("DELETE FROM campaign_steps WHERE campaign_id = ? AND ord = 2", (campaign.id,))
    conn.commit()

    report = rebuild_jobs(conn, campaign.id, now=at(50))

    record = require_campaign_lead(conn, campaign.id, lead)
    assert record.sublist == Sublist.QUEUE.value
    assert record.current_step_ord == 3
    assert report.completed == 0
    assert open_job_for_lead(conn, campaign.id, lead).action_type == "message"


def test_a_lead_past_the_last_step_is_parked_exactly_one_past_the_end(
    conn, account, lead
):
    campaign = simple_campaign(conn, account)
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    conn.execute(
        "UPDATE campaign_leads SET current_step_ord = 47 WHERE campaign_id = ?",
        (campaign.id,),
    )
    conn.commit()

    report = rebuild_jobs(conn, campaign.id, now=at(50))

    record = require_campaign_lead(conn, campaign.id, lead)
    assert record.sublist == Sublist.SUCCESSFUL.value
    assert record.current_step_ord == 3
    assert report.completed == 1


def test_an_open_job_with_no_derivable_shape_is_a_reported_mismatch(conn, account, lead):
    campaign = simple_campaign(conn, account)
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    conn.execute("UPDATE jobs SET step_id = NULL WHERE campaign_id = ?", (campaign.id,))
    conn.commit()

    assert orphan_open_jobs(conn, campaign.id) == 1
    assert not queue_matches_state(conn, campaign.id, now=at(50))

    rebuild_jobs(conn, campaign.id, now=at(50))

    assert orphan_open_jobs(conn, campaign.id) == 0
    assert queue_matches_state(conn, campaign.id, now=at(50))


# --------------------------------------------------------------------------
# The 0003 migration has to survive the corruption it is there to prevent
# --------------------------------------------------------------------------


def test_migration_0003_quarantines_duplicate_open_jobs_it_finds(tmp_path):
    """A database written before the index existed may already hold duplicates.

    If `CREATE UNIQUE INDEX` ran first it would fail on exactly those databases,
    and initialization would abort before anything could repair them.
    """
    older = tmp_path / "older"
    older.mkdir()
    for name in ("0001_init.sql", "0002_lead_dedupe.sql"):
        shutil.copy(MIGRATIONS_DIR / name, older / name)

    db_path = tmp_path / "legacy.db"
    conn = initialize_database(db_path, migrations_dir=older)
    try:
        applied = [
            row["version"]
            for row in conn.execute("SELECT version FROM schema_migrations").fetchall()
        ]
        assert applied == ["0001_init", "0002_lead_dedupe"]

        account_id = int(
            conn.execute(
                "INSERT INTO accounts (label, timezone, state) VALUES (?, ?, ?)",
                ("primary", "UTC", "active"),
            ).lastrowid
        )
        campaign_id = int(
            conn.execute(
                """
                INSERT INTO campaigns (account_id, name, status, approval_mode)
                VALUES (?, 'Legacy', 'active', 'auto')
                """,
                (account_id,),
            ).lastrowid
        )
        lead_id = int(
            conn.execute(
                "INSERT INTO leads (account_id, full_name) VALUES (?, 'Legacy Lead')",
                (account_id,),
            ).lastrowid
        )
        for _ in range(3):
            conn.execute(
                """
                INSERT INTO jobs
                    (account_id, campaign_id, lead_id, action_type, scheduled_for, state)
                VALUES (?, ?, ?, 'message', '2026-05-04 09:00:00', 'pending')
                """,
                (account_id, campaign_id, lead_id),
            )
        conn.commit()

        assert migrate(conn) == ["0003_sequence_jobs"]

        states = [
            row["state"]
            for row in conn.execute("SELECT state FROM jobs ORDER BY id").fetchall()
        ]
        assert states == ["pending", "cancelled", "cancelled"]
        index = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name = ?",
            ("idx_jobs_one_open_per_lead",),
        ).fetchone()
        assert index is not None
    finally:
        conn.close()
