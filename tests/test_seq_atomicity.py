"""SEQ-01: filter branching, safety refusals, atomicity and queue rebuilding.

The two headline claims of the issue are proved here rather than asserted in a
docstring. `test_crash_*` shows that an interrupted transition leaves nothing
half-written and that a lead which did get committed into `processing` is
recovered rather than stranded. `test_rebuild_*` deletes every row in `jobs` and
shows the queue comes back equivalent.
"""

from datetime import datetime, timedelta, timezone

import pytest

from linkedin_mcp.audit import RefusalReason
from linkedin_mcp.core.db import initialize_database
from linkedin_mcp.leads import add_tag, create_lead
from linkedin_mcp.sequences import (
    Disposition,
    FilterNotRegisteredError,
    JobState,
    StepDefinitionError,
    StepSpec,
    Sublist,
    apply_filter_step,
    claim_step,
    complete_step,
    create_campaign,
    define_steps,
    derive_jobs,
    disposition_for,
    due_jobs,
    enrol_lead,
    evaluate_filter,
    fail_step,
    get_campaign_lead,
    list_campaign_leads,
    list_jobs,
    mark_replied,
    open_job_for_lead,
    open_job_specs,
    queue_matches_state,
    rebuild_jobs,
    recover_all_stranded,
    recover_stranded,
    refuse_step,
    register_filter,
    registered_filters,
    require_campaign_lead,
    reset_filters,
    reset_lead,
    set_campaign_status,
    skip_lead,
    step_at_ord,
    sublist_counts,
    unregister_filter,
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


def filtered_campaign(conn, account_id, *, on_no_match="skipped", filter_name="always"):
    created = create_campaign(conn, account_id, f"Filtered {on_no_match}", status="active")
    define_steps(
        conn,
        created.id,
        [
            StepSpec("profile_view"),
            StepSpec(
                "filter",
                config={"filter": filter_name, "on_no_match": on_no_match},
            ),
            StepSpec("connection_request"),
        ],
    )
    return created


def advance_to_the_filter(conn, campaign_id, lead_id):
    """Enrol a lead and walk it up to the filter step at ord 2."""
    enrol_lead(conn, campaign_id, lead_id, now=BASE_TIME)
    claim_step(conn, campaign_id, lead_id, worker_id="w", now=at(10))
    complete_step(conn, campaign_id, lead_id, now=at(20))
    claim_step(conn, campaign_id, lead_id, worker_id="w", now=at(30))


# --------------------------------------------------------------------------
# Filter steps drop leads out of the flow, they never fork it
# --------------------------------------------------------------------------


def test_a_matching_filter_advances_the_lead_like_any_other_step(conn, account, lead):
    campaign = filtered_campaign(conn, account, filter_name="always")
    advance_to_the_filter(conn, campaign.id, lead)

    record = apply_filter_step(conn, campaign.id, lead, matched=True, now=at(40))

    assert record.sublist == Sublist.QUEUE.value
    assert record.current_step_ord == 3
    # The defining property: one lead in, one lead out, one job. Nothing forked.
    assert len(list_campaign_leads(conn, campaign.id)) == 1
    open_jobs = [job for job in list_jobs(conn, campaign_id=campaign.id) if job.is_open]
    assert len(open_jobs) == 1
    assert open_jobs[0].action_type == "connection_request"


def test_a_failing_filter_drops_the_lead_into_skipped_by_default(conn, account, lead):
    campaign = filtered_campaign(conn, account, filter_name="never")
    advance_to_the_filter(conn, campaign.id, lead)

    record = apply_filter_step(conn, campaign.id, lead, matched=False, now=at(40))

    assert record.sublist == Sublist.SKIPPED.value
    assert record.last_outcome == "filter_no_match: never"
    assert open_job_for_lead(conn, campaign.id, lead) is None
    assert len(list_campaign_leads(conn, campaign.id)) == 1


def test_a_filter_can_be_configured_to_exclude_instead(conn, account, lead):
    campaign = filtered_campaign(conn, account, on_no_match="excluded", filter_name="never")
    advance_to_the_filter(conn, campaign.id, lead)

    record = apply_filter_step(conn, campaign.id, lead, matched=False, now=at(40))

    assert record.sublist == Sublist.EXCLUDED.value


def test_skipped_can_come_back_and_excluded_cannot(conn, account):
    """The behavioural difference between the two exits a filter can take."""
    soft_lead = make_lead(conn, account, "Soft Exit", public_id="soft-exit")
    hard_lead = make_lead(conn, account, "Hard Exit", public_id="hard-exit")
    soft = filtered_campaign(conn, account, on_no_match="skipped", filter_name="never")
    hard = filtered_campaign(conn, account, on_no_match="excluded", filter_name="never")

    advance_to_the_filter(conn, soft.id, soft_lead)
    apply_filter_step(conn, soft.id, soft_lead, matched=False, now=at(40))
    advance_to_the_filter(conn, hard.id, hard_lead)
    apply_filter_step(conn, hard.id, hard_lead, matched=False, now=at(40))

    revived = reset_lead(conn, soft.id, soft_lead, now=at(100))
    assert revived.sublist == Sublist.QUEUE.value

    with pytest.raises(Exception) as excinfo:
        reset_lead(conn, hard.id, hard_lead, now=at(100))
    assert "excluded" in str(excinfo.value)


def test_a_filter_step_is_not_allowed_to_exit_into_an_arbitrary_sublist(conn, account):
    campaign = create_campaign(conn, account, "Bad exit", status="active")
    define_steps(
        conn,
        campaign.id,
        [StepSpec("filter", config={"filter": "always", "on_no_match": "failed"})],
    )
    step = step_at_ord(conn, campaign.id, 1)

    with pytest.raises(StepDefinitionError):
        _ = step.no_match_sublist


# --------------------------------------------------------------------------
# The filter registry is the seam SEQ-05 plugs into
# --------------------------------------------------------------------------


def test_a_registered_predicate_decides_the_step(conn, account, lead):
    seen = []

    def only_platform_people(context):
        seen.append((context.campaign_id, context.lead_id))
        return "platform" in (context.config.get("needle") or "")

    register_filter("icp_match", only_platform_people)
    campaign = create_campaign(conn, account, "ICP", status="active")
    define_steps(
        conn,
        campaign.id,
        [StepSpec("filter", config={"filter": "icp_match", "needle": "platform teams"})],
    )
    step = step_at_ord(conn, campaign.id, 1)

    assert evaluate_filter(conn, account, campaign.id, lead, step) is True
    assert seen == [(campaign.id, lead)]
    assert "icp_match" in registered_filters()
    assert unregister_filter("icp_match") is True


def test_the_built_in_tag_filter_reads_the_lead_store(conn, account, lead):
    add_tag(conn, lead, "warm")
    campaign = create_campaign(conn, account, "Tagged", status="active")
    define_steps(
        conn,
        campaign.id,
        [StepSpec("filter", config={"filter": "has_tag", "tag": "warm"})],
    )
    step = step_at_ord(conn, campaign.id, 1)

    assert evaluate_filter(conn, account, campaign.id, lead, step) is True

    define_steps(
        conn,
        campaign.id,
        [StepSpec("filter", config={"filter": "has_tag", "tag": "cold"})],
        replace=True,
    )
    assert (
        evaluate_filter(conn, account, campaign.id, lead, step_at_ord(conn, campaign.id, 1))
        is False
    )


def test_an_unregistered_filter_name_fails_loudly(conn, account, lead):
    campaign = create_campaign(conn, account, "Missing", status="active")
    define_steps(conn, campaign.id, [StepSpec("filter", config={"filter": "nope"})])
    step = step_at_ord(conn, campaign.id, 1)

    with pytest.raises(FilterNotRegisteredError):
        evaluate_filter(conn, account, campaign.id, lead, step)


def test_evaluating_a_non_filter_step_is_a_definition_error(conn, account, lead):
    campaign = create_campaign(conn, account, "Not a filter", status="active")
    define_steps(conn, campaign.id, [StepSpec("message")])
    step = step_at_ord(conn, campaign.id, 1)

    with pytest.raises(StepDefinitionError):
        evaluate_filter(conn, account, campaign.id, lead, step)


# --------------------------------------------------------------------------
# Safety refusals resolve through the shared CORE-03 vocabulary
# --------------------------------------------------------------------------


def simple_campaign(conn, account_id, name="Simple"):
    created = create_campaign(conn, account_id, name, status="active")
    define_steps(
        conn,
        created.id,
        [StepSpec("connection_request"), StepSpec("message")],
    )
    return created


def test_a_cap_refusal_requeues_later_without_burning_an_attempt(conn, account, lead):
    campaign = simple_campaign(conn, account)
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    claim_step(conn, campaign.id, lead, worker_id="w", now=at(10))

    record = refuse_step(
        conn, campaign.id, lead, reason=RefusalReason.DAILY_CAP_REACHED, now=at(20)
    )

    assert record.sublist == Sublist.QUEUE.value
    assert record.current_step_ord == 1
    assert record.attempts == 0
    assert record.next_run_at == "2026-05-04 15:00:20"
    assert record.last_outcome == "refused: daily_cap_reached"
    assert open_job_for_lead(conn, campaign.id, lead).state == JobState.PENDING.value


def test_a_blacklist_refusal_excludes_the_lead(conn, account, lead):
    campaign = simple_campaign(conn, account)
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    claim_step(conn, campaign.id, lead, worker_id="w", now=at(10))

    record = refuse_step(
        conn, campaign.id, lead, reason=RefusalReason.LEAD_BLACKLISTED, now=at(20)
    )

    assert record.sublist == Sublist.EXCLUDED.value
    assert [job.state for job in list_jobs(conn, campaign_id=campaign.id)] == ["refused"]


def test_a_disabled_action_skips_the_lead_rather_than_pinning_it(conn, account, lead):
    campaign = simple_campaign(conn, account)
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    claim_step(conn, campaign.id, lead, worker_id="w", now=at(10))

    record = refuse_step(
        conn, campaign.id, lead, reason=RefusalReason.ACTION_DISABLED, now=at(20)
    )

    assert record.sublist == Sublist.SKIPPED.value


def test_a_duplicate_action_advances_because_the_step_is_already_satisfied(
    conn, account, lead
):
    campaign = simple_campaign(conn, account)
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    claim_step(conn, campaign.id, lead, worker_id="w", now=at(10))

    record = refuse_step(
        conn, campaign.id, lead, reason=RefusalReason.DUPLICATE_ACTION, now=at(20)
    )

    assert record.sublist == Sublist.QUEUE.value
    assert record.current_step_ord == 2
    assert disposition_for(RefusalReason.DUPLICATE_ACTION) is Disposition.ADVANCE


def test_an_approval_refusal_waits_rather_than_failing(conn, account, lead):
    campaign = simple_campaign(conn, account)
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)

    record = refuse_step(
        conn, campaign.id, lead, reason=RefusalReason.APPROVAL_REQUIRED, now=at(20)
    )

    assert record.sublist == Sublist.QUEUE.value
    assert record.attempts == 0


# --------------------------------------------------------------------------
# Atomicity: a crash mid-step cannot strand a lead in `processing`
# --------------------------------------------------------------------------


def test_a_crash_between_the_sublist_write_and_the_job_write_writes_nothing(
    conn, account, lead, monkeypatch
):
    """The exact failure the DoD names, simulated at the exact seam.

    `complete_step` closes the old job, rewrites `campaign_leads` and inserts the
    next job. Blowing up on that last insert is a crash after the sub-list write
    and before the job write. Nothing may survive it.
    """
    campaign = simple_campaign(conn, account)
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    claim_step(conn, campaign.id, lead, worker_id="worker-1", now=at(10))

    before_lead = require_campaign_lead(conn, campaign.id, lead)
    before_jobs = [
        (job.id, job.state, job.step_id) for job in list_jobs(conn, campaign_id=campaign.id)
    ]

    def crash(*args, **kwargs):
        raise RuntimeError("process died between the two writes")

    monkeypatch.setattr(jobs_module, "insert_job", crash)

    with pytest.raises(RuntimeError):
        complete_step(conn, campaign.id, lead, now=at(20))

    monkeypatch.undo()

    after_lead = require_campaign_lead(conn, campaign.id, lead)
    after_jobs = [
        (job.id, job.state, job.step_id) for job in list_jobs(conn, campaign_id=campaign.id)
    ]

    assert after_lead == before_lead
    assert after_jobs == before_jobs
    assert after_lead.sublist == Sublist.PROCESSING.value
    assert after_lead.current_step_ord == 1
    # And the lead that the crash left holding a lease is not stranded: the sweep
    # puts it back in the queue on the step it never finished.
    assert recover_stranded(conn, campaign.id, now=at(10_000)) == (lead,)
    rescued = require_campaign_lead(conn, campaign.id, lead)
    assert rescued.sublist == Sublist.QUEUE.value
    assert rescued.current_step_ord == 1
    assert open_job_for_lead(conn, campaign.id, lead).state == JobState.PENDING.value


def test_a_crash_during_enrolment_leaves_no_half_enrolled_lead(
    conn, account, lead, monkeypatch
):
    campaign = simple_campaign(conn, account)

    def crash(*args, **kwargs):
        raise RuntimeError("process died before the job was written")

    monkeypatch.setattr(jobs_module, "insert_job", crash)

    with pytest.raises(RuntimeError):
        enrol_lead(conn, campaign.id, lead, now=BASE_TIME)

    monkeypatch.undo()

    assert get_campaign_lead(conn, campaign.id, lead) is None
    assert list_jobs(conn, campaign_id=campaign.id) == []


def test_a_crash_during_a_retry_leaves_the_previous_job_open(
    conn, account, lead, monkeypatch
):
    campaign = simple_campaign(conn, account)
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    claim_step(conn, campaign.id, lead, worker_id="w", now=at(10))
    original = open_job_for_lead(conn, campaign.id, lead)

    def crash(*args, **kwargs):
        raise RuntimeError("died while re-queueing")

    monkeypatch.setattr(jobs_module, "insert_job", crash)

    with pytest.raises(RuntimeError):
        refuse_step(
            conn, campaign.id, lead, reason=RefusalReason.HOURLY_CAP_REACHED, now=at(20)
        )

    monkeypatch.undo()

    still_open = open_job_for_lead(conn, campaign.id, lead)
    assert still_open is not None
    assert still_open.id == original.id
    assert still_open.state == JobState.LEASED.value


def test_process_death_mid_transaction_rolls_the_whole_thing_back(tmp_path):
    """No Python `finally` runs when a process is killed. SQLite still rolls back."""
    db_path = tmp_path / "crash.db"
    setup = initialize_database(db_path)
    try:
        cursor = setup.execute(
            "INSERT INTO accounts (label, timezone, state) VALUES (?, ?, ?)",
            ("primary", "UTC", "active"),
        )
        setup.commit()
        account_id = int(cursor.lastrowid)
        campaign = simple_campaign(setup, account_id, name="Killed")
        lead_id = create_lead(setup, account_id, "Grace Hopper", public_id="grace").id
        enrol_lead(setup, campaign.id, lead_id, now=BASE_TIME)
    finally:
        setup.close()

    doomed = initialize_database(db_path)
    doomed.execute("BEGIN IMMEDIATE")
    doomed.execute(
        "UPDATE campaign_leads SET sublist = 'processing' WHERE campaign_id = ? AND lead_id = ?",
        (campaign.id, lead_id),
    )
    doomed.execute(
        "UPDATE jobs SET state = 'leased', locked_by = 'doomed' WHERE campaign_id = ?",
        (campaign.id,),
    )
    # The process dies here: no commit, no rollback, no cleanup.
    doomed.close()

    reopened = initialize_database(db_path)
    try:
        record = require_campaign_lead(reopened, campaign.id, lead_id)
        assert record.sublist == Sublist.QUEUE.value
        assert open_job_for_lead(reopened, campaign.id, lead_id).state == "pending"
    finally:
        reopened.close()


def test_recover_stranded_leaves_a_live_lease_alone(conn, account, lead):
    campaign = simple_campaign(conn, account)
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    claim_step(conn, campaign.id, lead, worker_id="w", now=at(10))

    assert recover_stranded(conn, campaign.id, now=at(60), lease_seconds=900) == ()
    assert require_campaign_lead(conn, campaign.id, lead).sublist == Sublist.PROCESSING.value


def test_recover_stranded_rescues_a_lead_whose_job_vanished(conn, account, lead):
    campaign = simple_campaign(conn, account)
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    claim_step(conn, campaign.id, lead, worker_id="w", now=at(10))
    conn.execute("DELETE FROM jobs WHERE campaign_id = ?", (campaign.id,))
    conn.commit()

    assert recover_stranded(conn, campaign.id, now=at(20)) == (lead,)

    record = require_campaign_lead(conn, campaign.id, lead)
    assert record.sublist == Sublist.QUEUE.value
    assert open_job_for_lead(conn, campaign.id, lead) is not None


def test_recover_stranded_preserves_the_attempt_count(conn, account, lead):
    campaign = create_campaign(conn, account, "Attempts", status="active")
    define_steps(
        conn,
        campaign.id,
        [StepSpec("message", config={"max_attempts": 5, "retry_backoff_seconds": 10})],
    )
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    claim_step(conn, campaign.id, lead, worker_id="w", now=at(10))
    fail_step(conn, campaign.id, lead, error="boom", now=at(20))
    claim_step(conn, campaign.id, lead, worker_id="w", now=at(100))

    recover_stranded(conn, campaign.id, now=at(10_000))

    assert require_campaign_lead(conn, campaign.id, lead).attempts == 1


def test_recover_all_stranded_sweeps_every_campaign(conn, account):
    first_lead = make_lead(conn, account, "One", public_id="one")
    second_lead = make_lead(conn, account, "Two", public_id="two")
    first = simple_campaign(conn, account, name="First")
    second = simple_campaign(conn, account, name="Second")
    enrol_lead(conn, first.id, first_lead, now=BASE_TIME)
    enrol_lead(conn, second.id, second_lead, now=BASE_TIME)
    claim_step(conn, first.id, first_lead, worker_id="w", now=at(10))
    claim_step(conn, second.id, second_lead, worker_id="w", now=at(10))

    swept = recover_all_stranded(conn, now=at(10_000))

    assert swept == {first.id: (first_lead,), second.id: (second_lead,)}
    assert sublist_counts(conn, first.id)["queue"] == 1
    assert sublist_counts(conn, second.id)["queue"] == 1


# --------------------------------------------------------------------------
# The jobs queue is genuinely rebuildable from campaign state
# --------------------------------------------------------------------------


def populated_campaign(conn, account_id):
    """A campaign whose leads sit across the queue and several terminal sub-lists."""
    campaign = create_campaign(conn, account_id, "Mixed", status="active")
    define_steps(
        conn,
        campaign.id,
        [
            StepSpec("connection_request", config={"priority": 3}),
            StepSpec("message", config={"delay_seconds": 60, "priority": 1}),
            StepSpec("message"),
        ],
    )
    leads = {
        name: make_lead(conn, account_id, name.title(), public_id=name)
        for name in ("queued", "midway", "replied", "skipped")
    }
    for lead_id in leads.values():
        enrol_lead(conn, campaign.id, lead_id, now=BASE_TIME)

    claim_step(conn, campaign.id, leads["midway"], worker_id="w", now=at(10))
    complete_step(conn, campaign.id, leads["midway"], now=at(20))
    mark_replied(conn, campaign.id, leads["replied"], now=at(30))
    skip_lead(conn, campaign.id, leads["skipped"], reason="not a fit", now=at(40))
    return campaign, leads


def test_the_queue_can_be_deleted_whole_and_rebuilt_equivalent(conn, account):
    campaign, _leads = populated_campaign(conn, account)
    before = open_job_specs(conn, campaign.id)
    assert before == derive_jobs(conn, campaign.id, now=at(50))
    assert len(before) == 2

    conn.execute("DELETE FROM jobs")
    conn.commit()
    assert open_job_specs(conn, campaign.id) == ()

    report = rebuild_jobs(conn, campaign.id, now=at(50))

    assert open_job_specs(conn, campaign.id) == before
    assert report.created == 2
    assert report.deleted == 0
    assert queue_matches_state(conn, campaign.id, now=at(50))


def test_rebuilding_is_idempotent(conn, account):
    campaign, _leads = populated_campaign(conn, account)
    expected = open_job_specs(conn, campaign.id)

    rebuild_jobs(conn, campaign.id, now=at(50))
    rebuild_jobs(conn, campaign.id, now=at(60))

    assert open_job_specs(conn, campaign.id) == expected


def test_a_rebuild_drops_a_stale_job_for_a_lead_that_left_the_flow(conn, account, lead):
    campaign = simple_campaign(conn, account)
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    # Corrupt the queue the way a partial write would: the lead replied but its
    # job was never closed, so tonight's run would message somebody who answered.
    conn.execute(
        "UPDATE campaign_leads SET sublist = 'replied' WHERE campaign_id = ? AND lead_id = ?",
        (campaign.id, lead),
    )
    conn.commit()
    assert open_job_for_lead(conn, campaign.id, lead) is not None
    assert not queue_matches_state(conn, campaign.id, now=at(50))

    report = rebuild_jobs(conn, campaign.id, now=at(50))

    assert open_job_for_lead(conn, campaign.id, lead) is None
    assert report.deleted == 1
    assert report.created == 0


def test_a_rebuild_returns_processing_leads_to_the_queue(conn, account, lead):
    campaign = simple_campaign(conn, account)
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    claim_step(conn, campaign.id, lead, worker_id="w", now=at(10))

    report = rebuild_jobs(conn, campaign.id, now=at(50))

    assert report.requeued == 1
    record = require_campaign_lead(conn, campaign.id, lead)
    assert record.sublist == Sublist.QUEUE.value
    assert record.last_outcome == "requeued_by_rebuild"
    assert open_job_for_lead(conn, campaign.id, lead).state == JobState.PENDING.value


def test_a_rebuild_can_be_told_to_leave_processing_leads_alone(conn, account, lead):
    campaign = simple_campaign(conn, account)
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    claim_step(conn, campaign.id, lead, worker_id="w", now=at(10))

    rebuild_jobs(conn, campaign.id, now=at(50), recover_processing=False)

    assert require_campaign_lead(conn, campaign.id, lead).sublist == Sublist.PROCESSING.value


def test_a_rebuild_closes_out_a_lead_that_ran_off_the_end_of_the_steps(
    conn, account, lead
):
    campaign = simple_campaign(conn, account)
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    conn.execute(
        "UPDATE campaign_leads SET current_step_ord = 99 WHERE campaign_id = ? AND lead_id = ?",
        (campaign.id, lead),
    )
    conn.commit()

    report = rebuild_jobs(conn, campaign.id, now=at(50))

    assert report.completed == 1
    assert require_campaign_lead(conn, campaign.id, lead).sublist == Sublist.SUCCESSFUL.value
    assert open_job_for_lead(conn, campaign.id, lead) is None


def test_a_rebuild_leaves_closed_jobs_as_history(conn, account):
    campaign, _leads = populated_campaign(conn, account)
    closed_before = sorted(
        job.id for job in list_jobs(conn, campaign_id=campaign.id) if not job.is_open
    )
    assert closed_before

    rebuild_jobs(conn, campaign.id, now=at(50))

    closed_after = sorted(
        job.id for job in list_jobs(conn, campaign_id=campaign.id) if not job.is_open
    )
    assert closed_after == closed_before


def test_derive_jobs_ignores_every_terminal_sublist(conn, account):
    campaign, leads = populated_campaign(conn, account)

    derived_leads = {spec.lead_id for spec in derive_jobs(conn, campaign.id, now=at(50))}

    assert derived_leads == {leads["queued"], leads["midway"]}


def test_a_duplicated_queue_row_is_corrected_by_a_rebuild(conn, account, lead):
    campaign = simple_campaign(conn, account)
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    original = open_job_for_lead(conn, campaign.id, lead)
    # The unique index blocks a genuine duplicate, so corrupt the queue the only
    # other way it can be wrong: an open job aimed at the wrong step.
    conn.execute(
        "UPDATE jobs SET step_id = ?, action_type = 'message' WHERE id = ?",
        (step_at_ord(conn, campaign.id, 2).id, original.id),
    )
    conn.commit()
    assert not queue_matches_state(conn, campaign.id, now=at(50))

    rebuild_jobs(conn, campaign.id, now=at(50))

    assert queue_matches_state(conn, campaign.id, now=at(50))
    assert open_job_for_lead(conn, campaign.id, lead).action_type == "connection_request"


def test_redefining_steps_refreshes_the_derived_queue(conn, account, lead):
    campaign = simple_campaign(conn, account)
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    old_step_id = step_at_ord(conn, campaign.id, 1).id

    define_steps(conn, campaign.id, [StepSpec("profile_view")], replace=True)

    job = open_job_for_lead(conn, campaign.id, lead)
    assert job.step_id != old_step_id
    assert job.action_type == "profile_view"
    assert queue_matches_state(conn, campaign.id, now=at(50))


def test_redefining_a_shorter_list_closes_out_leads_past_the_new_end(
    conn, account, lead
):
    campaign = simple_campaign(conn, account)
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    claim_step(conn, campaign.id, lead, worker_id="w", now=at(10))
    complete_step(conn, campaign.id, lead, now=at(20))
    assert require_campaign_lead(conn, campaign.id, lead).current_step_ord == 2

    define_steps(conn, campaign.id, [StepSpec("profile_view")], replace=True)

    record = require_campaign_lead(conn, campaign.id, lead)
    assert record.sublist == Sublist.SUCCESSFUL.value
    assert open_job_for_lead(conn, campaign.id, lead) is None


def test_no_open_job_ever_points_at_a_deleted_step(conn, account, lead):
    campaign = simple_campaign(conn, account)
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)

    define_steps(conn, campaign.id, [StepSpec("message")], replace=True)

    dangling = conn.execute(
        """
        SELECT COUNT(*) AS total FROM jobs
        WHERE campaign_id = ? AND state IN ('pending', 'leased') AND step_id IS NULL
        """,
        (campaign.id,),
    ).fetchone()
    assert dangling["total"] == 0


# --------------------------------------------------------------------------
# What the runner reads
# --------------------------------------------------------------------------


def test_due_jobs_ignores_a_paused_campaign(conn, account, lead):
    campaign = simple_campaign(conn, account)
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    assert len(due_jobs(conn, account, now=at(10))) == 1

    set_campaign_status(conn, campaign.id, "paused", now=at(20))

    assert due_jobs(conn, account, now=at(30)) == []
    # Pausing does not throw the queue away; it just stops it being served.
    assert open_job_for_lead(conn, campaign.id, lead) is not None


def test_due_jobs_ignores_a_job_that_is_not_due_yet(conn, account, lead):
    campaign = create_campaign(conn, account, "Delayed", status="active")
    define_steps(
        conn,
        campaign.id,
        [StepSpec("connection_request"), StepSpec("message", config={"delay_seconds": 3600})],
    )
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    claim_step(conn, campaign.id, lead, worker_id="w", now=at(10))
    complete_step(conn, campaign.id, lead, now=at(20))

    assert due_jobs(conn, account, now=at(21)) == []
    assert len(due_jobs(conn, account, now=at(3700))) == 1


def test_due_jobs_ignores_a_lead_that_already_left_the_queue(conn, account, lead):
    campaign = simple_campaign(conn, account)
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    claim_step(conn, campaign.id, lead, worker_id="w", now=at(10))

    assert due_jobs(conn, account, now=at(20)) == []


def test_due_jobs_serves_the_highest_priority_first(conn, account):
    urgent_lead = make_lead(conn, account, "Urgent", public_id="urgent")
    normal_lead = make_lead(conn, account, "Normal", public_id="normal")
    urgent = create_campaign(conn, account, "Urgent", status="active")
    define_steps(conn, urgent.id, [StepSpec("message", config={"priority": 9})])
    normal = create_campaign(conn, account, "Normal", status="active")
    define_steps(conn, normal.id, [StepSpec("message")])
    enrol_lead(conn, normal.id, normal_lead, now=BASE_TIME)
    enrol_lead(conn, urgent.id, urgent_lead, now=BASE_TIME)

    served = due_jobs(conn, account, now=at(10))

    assert [job.lead_id for job in served] == [urgent_lead, normal_lead]
