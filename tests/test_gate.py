import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import linkedin_browser_mcp
from linkedin_mcp.audit import instrument
from linkedin_mcp.audit.log import (
    AuditLog,
    Outcome,
    RefusalReason,
    reset_audit_log,
    set_audit_log,
)
from linkedin_mcp.core.config import (
    GLOBAL_HOURLY_CEILING,
    HARD_CEILINGS,
    INVITE_ACTION,
    PENDING_INVITE_CEILING,
)
from linkedin_mcp.leads.blacklist import blacklist_lead
from linkedin_mcp.leads.store import create_lead
from linkedin_mcp.safety.gate import (
    SAFETY_EVENT_KIND,
    AccountChallenged,
    AccountCooldown,
    AccountLoggedOut,
    AccountPaused,
    ActionDisabled,
    ApprovalRequired,
    Blacklisted,
    DailyCapReached,
    DuplicateAction,
    HourlyCapReached,
    PendingInviteCeilingReached,
    SafetyError,
    SafetyGate,
    SafetyRefusal,
    UnknownAccountError,
    WarmupLimit,
    WeeklyCapReached,
    WorkingHoursClosed,
    guard_action,
    reset_gate,
    set_gate,
)
from linkedin_mcp.safety.limits import daily_jitter_fraction, shrink_for_jitter

# A Saturday at noon UTC, so weekday-sensitive checks read unambiguously.
NOON = datetime(2026, 3, 14, 12, 0, tzinfo=timezone.utc)
SATURDAY = 5
MONDAY = 0
INVITE_DAILY_CAP = HARD_CEILINGS[INVITE_ACTION].daily
INVITE_WEEKLY_CAP = HARD_CEILINGS[INVITE_ACTION].weekly
POST_URL = "https://www.linkedin.com/posts/activity-123"
PROFILE_URL = "https://www.linkedin.com/in/someone-123/"


class MockContext:
    def info(self, message):
        pass

    def error(self, message):
        pass

    def warning(self, message):
        pass

    async def report_progress(self, current, total):
        pass


class ExplodingBrowserSession:
    """Stand-in that fails the test if a refused tool still opens a browser."""

    def __init__(self, *args, **kwargs):
        raise AssertionError("the gate must refuse before a browser is opened")


class BrokenGate:
    def acquire(self, *args, **kwargs):
        raise sqlite3.OperationalError("database is locked")


@pytest.fixture
def audit(tmp_path):
    log = AuditLog.open(tmp_path / "linkedin-helper.db")
    set_audit_log(log)
    try:
        yield log
    finally:
        reset_audit_log()
        reset_gate()
        instrument.reset_account_resolver()
        log.close()


@pytest.fixture
def account_id(audit):
    resolved = audit.ensure_account("gate@example.com")
    instrument.set_account_resolver(lambda: resolved)
    return resolved


@pytest.fixture
def gate(account_id):
    """A gate frozen at NOON with jitter switched off.

    Jitter only ever shrinks a cap, so pinning it at zero makes the boundary
    tests state the exact cap rather than a range. The jittered boundary has its
    own test.
    """
    frozen = SafetyGate(clock=lambda: NOON, jitter=lambda _account, _moment: 0.0)
    set_gate(frozen)
    return frozen


def log_actions(audit, account_id, action_type, count, *, spacing=timedelta(minutes=5), lead_id=None):
    """Spread `count` successful actions backwards from NOON.

    Five minute spacing keeps a full day's worth of actions clear of the hourly
    burst cap, so a daily-cap test measures the daily cap.
    """
    for index in range(1, count + 1):
        audit.record(
            account_id,
            action_type,
            Outcome.SUCCESS,
            lead_id=lead_id,
            occurred_at=NOON - spacing * index,
        )


def set_account_state(audit, account_id, state):
    audit.connection.execute(
        "UPDATE accounts SET state = ? WHERE id = ?", (state, account_id)
    )
    audit.connection.commit()


def set_account_age(audit, account_id, days):
    audit.connection.execute(
        "UPDATE accounts SET account_age_days = ? WHERE id = ?", (days, account_id)
    )
    audit.connection.commit()


def set_account_limit(audit, account_id, action_type, **columns):
    keys = ", ".join(columns)
    placeholders = ", ".join("?" for _ in columns)
    audit.connection.execute(
        f"INSERT INTO account_limits (account_id, action_type, {keys}) "
        f"VALUES (?, ?, {placeholders})",
        (account_id, action_type, *columns.values()),
    )
    audit.connection.commit()


def set_working_hours(audit, account_id, weekday, start_minute, end_minute):
    audit.connection.execute(
        "INSERT INTO working_hours (account_id, weekday, start_minute, end_minute) "
        "VALUES (?, ?, ?, ?)",
        (account_id, weekday, start_minute, end_minute),
    )
    audit.connection.commit()


def make_lead(audit, account_id, suffix="1"):
    return create_lead(
        audit.connection,
        account_id,
        f"Lead {suffix}",
        member_id=f"urn:li:member:{suffix}",
        public_id=f"lead-{suffix}",
    )


def safety_events(audit, account_id):
    return audit.connection.execute(
        "SELECT kind, severity, detail_json FROM safety_events WHERE account_id = ?",
        (account_id,),
    ).fetchall()


def refusal_reasons(audit, account_id):
    return [
        row["detail_json"] for row in audit.refusals(account_id)
    ]


def all_refusal_classes(root=SafetyRefusal):
    found = set()
    for subclass in root.__subclasses__():
        found.add(subclass)
        found |= all_refusal_classes(subclass)
    return found


# --- VAL-01 ----------------------------------------------------------------


def test_val01_the_fifty_first_action_of_the_day_is_refused_with_the_daily_cap_reason(
    audit, account_id, gate
):
    set_account_limit(audit, account_id, "post_like", daily_cap=50)
    log_actions(audit, account_id, "post_like", 49)

    fiftieth = gate.acquire(account_id, "post_like")
    assert fiftieth.budget("daily").used == 49
    assert fiftieth.remaining("daily") == 0

    audit.record(account_id, "post_like", Outcome.SUCCESS, occurred_at=NOON)

    with pytest.raises(DailyCapReached) as refused:
        gate.acquire(account_id, "post_like")

    assert refused.value.reason is RefusalReason.DAILY_CAP_REACHED
    assert refused.value.detail["cap"] == 50
    assert refused.value.detail["used"] == 50


@pytest.mark.asyncio
async def test_val01_an_exhausted_invite_cap_is_a_normal_tool_result_and_the_server_stays_up(
    audit, account_id, gate, monkeypatch
):
    monkeypatch.setattr(linkedin_browser_mcp, "BrowserSession", ExplodingBrowserSession)
    log_actions(audit, account_id, INVITE_ACTION, INVITE_DAILY_CAP)
    ctx = MockContext()

    result = await linkedin_browser_mcp.send_connection_request(PROFILE_URL, ctx)

    assert result["status"] == "refused"
    assert result["reason"] == RefusalReason.DAILY_CAP_REACHED.value
    assert result["cap"] == INVITE_DAILY_CAP
    assert result["audit_logged"] is True

    refusals = audit.refusals(account_id)
    assert len(refusals) == 1
    assert refusals[0]["action_type"] == INVITE_ACTION
    assert len(safety_events(audit, account_id)) == 1

    # The server is still answering: the same tool refuses cleanly again and an
    # unrelated tool still runs its own validation instead of blowing up.
    again = await linkedin_browser_mcp.send_connection_request(PROFILE_URL, ctx)
    assert again["status"] == "refused"

    unrelated = await linkedin_browser_mcp.interact_with_linkedin_post(
        "https://invalid-url.com", ctx
    )
    assert unrelated["status"] == "error"
    assert "Invalid LinkedIn post URL" in unrelated["message"]


# --- leases ----------------------------------------------------------------


def test_a_clean_account_gets_a_lease_carrying_its_budgets(audit, account_id, gate):
    lease = gate.acquire(account_id, INVITE_ACTION, approved=True)

    assert lease.account_id == account_id
    assert lease.action_type == INVITE_ACTION
    assert lease.granted_at == NOON
    assert lease.budget("daily").cap == INVITE_DAILY_CAP
    assert lease.budget("weekly").cap == INVITE_WEEKLY_CAP
    assert lease.remaining("daily") == INVITE_DAILY_CAP - 1
    assert lease.budget("pending_invites").cap == PENDING_INVITE_CEILING
    assert lease.to_result()["remaining_today"] == INVITE_DAILY_CAP - 1


def test_a_lease_is_advisory_so_taking_one_spends_nothing(audit, account_id, gate):
    for _ in range(5):
        gate.acquire(account_id, "post_like")

    assert gate.acquire(account_id, "post_like").budget("daily").used == 0


def test_bookkeeping_actions_never_need_a_lease(audit, gate):
    lease = gate.acquire(9999, "login")

    assert lease.budgets == ()
    assert gate.acquire(9999, "browser_close").budgets == ()


def test_the_gate_leaves_no_transaction_open(audit, account_id, gate):
    gate.acquire(account_id, "post_like")
    assert not audit.connection.in_transaction

    set_account_state(audit, account_id, "paused")
    with pytest.raises(AccountPaused):
        gate.acquire(account_id, "post_like")

    assert not audit.connection.in_transaction


def test_a_failed_safety_event_write_still_refuses_and_unwedges_the_connection(
    audit, account_id, gate
):
    audit.connection.execute(
        "CREATE TRIGGER block_safety_events AFTER INSERT ON safety_events "
        "BEGIN SELECT RAISE(ABORT, 'no safety events today'); END"
    )
    audit.connection.commit()
    set_account_state(audit, account_id, "paused")

    with pytest.raises(AccountPaused) as refused:
        gate.acquire(account_id, "post_like")

    assert "audit_error" in refused.value.detail
    assert not audit.connection.in_transaction
    assert len(audit.refusals(account_id)) == 1
    assert safety_events(audit, account_id) == []

    audit.connection.execute("DROP TRIGGER block_safety_events")
    audit.connection.commit()
    with pytest.raises(AccountPaused):
        gate.acquire(account_id, "post_like")

    assert len(safety_events(audit, account_id)) == 1


def test_a_refusal_the_log_could_not_record_is_still_a_refusal(
    audit, account_id, gate, monkeypatch
):
    def explode(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr("linkedin_mcp.safety.gate.log_refusal", explode)
    set_account_state(audit, account_id, "paused")

    with pytest.raises(AccountPaused) as refused:
        gate.acquire(account_id, "post_like")

    assert refused.value.to_result()["audit_logged"] is False
    assert refused.value.detail["audit_error"] == "database is locked"
    assert not audit.connection.in_transaction


def test_the_gate_refuses_to_decide_inside_someone_elses_transaction(
    audit, account_id, gate
):
    audit.connection.execute("BEGIN")
    audit.connection.execute(
        "UPDATE accounts SET account_age_days = 5 WHERE id = ?", (account_id,)
    )

    with pytest.raises(SafetyError):
        gate.acquire(account_id, "post_like")

    audit.connection.rollback()
    assert not audit.connection.in_transaction
    assert gate.acquire(account_id, "post_like")


def test_a_wedged_connection_fails_the_tool_closed(audit, account_id, gate, monkeypatch):
    monkeypatch.setattr(linkedin_browser_mcp, "BrowserSession", ExplodingBrowserSession)
    audit.connection.execute("BEGIN")
    try:
        result = guard_action("post_like")
    finally:
        audit.connection.rollback()

    assert result["status"] == "error"
    assert "Safety gate unavailable" in result["message"]


def test_an_unknown_account_is_a_programming_error_not_a_refusal(audit, gate):
    with pytest.raises(UnknownAccountError):
        gate.acquire(4242, "post_like")


# --- account state ---------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("paused", AccountPaused),
        ("cooldown", AccountCooldown),
        ("challenged", AccountChallenged),
        ("logged_out", AccountLoggedOut),
    ],
)
def test_an_inactive_account_refuses_every_action(audit, account_id, gate, state, expected):
    set_account_state(audit, account_id, state)

    with pytest.raises(expected) as refused:
        gate.acquire(account_id, "post_like")

    assert refused.value.detail["account_state"] == state


def test_a_challenged_account_raises_a_critical_safety_event(audit, account_id, gate):
    set_account_state(audit, account_id, "challenged")

    with pytest.raises(AccountChallenged):
        gate.acquire(account_id, "post_like")

    events = safety_events(audit, account_id)
    assert [row["severity"] for row in events] == ["critical"]
    assert events[0]["kind"] == SAFETY_EVENT_KIND


def test_a_disabled_action_is_refused(audit, account_id, gate):
    set_account_limit(audit, account_id, "post_like", daily_cap=10, enabled=0)

    with pytest.raises(ActionDisabled):
        gate.acquire(account_id, "post_like")


# --- lead level checks -----------------------------------------------------


def test_a_blacklisted_lead_is_refused(audit, account_id, gate):
    lead = make_lead(audit, account_id)
    blacklist_lead(audit.connection, lead.id, reason="asked not to be contacted")

    with pytest.raises(Blacklisted) as refused:
        gate.acquire(account_id, INVITE_ACTION, lead.id, approved=True)

    assert refused.value.reason is RefusalReason.LEAD_BLACKLISTED
    assert refused.value.lead_id == lead.id


def test_a_lead_that_does_not_resolve_fails_closed(audit, account_id, gate):
    with pytest.raises(Blacklisted) as refused:
        gate.acquire(account_id, INVITE_ACTION, 777, approved=True)

    assert refused.value.detail["requested_lead_id"] == 777
    assert refused.value.detail["unresolved_lead"] is True
    assert refused.value.logged
    assert len(audit.refusals(account_id)) == 1


def test_inviting_the_same_lead_twice_is_a_duplicate(audit, account_id, gate):
    lead = make_lead(audit, account_id)
    row_id = audit.record(
        account_id,
        INVITE_ACTION,
        Outcome.SUCCESS,
        lead_id=lead.id,
        occurred_at=NOON - timedelta(days=30),
    )

    with pytest.raises(DuplicateAction) as refused:
        gate.acquire(account_id, INVITE_ACTION, lead.id, approved=True)

    assert refused.value.detail["previous_action_id"] == row_id


def test_a_failed_attempt_still_blocks_a_repeat(audit, account_id, gate):
    lead = make_lead(audit, account_id)
    audit.record(
        account_id,
        INVITE_ACTION,
        Outcome.FAILURE,
        lead_id=lead.id,
        occurred_at=NOON - timedelta(days=1),
    )

    with pytest.raises(DuplicateAction):
        gate.acquire(account_id, INVITE_ACTION, lead.id, approved=True)


def test_a_refused_attempt_does_not_block_a_retry(audit, account_id, gate):
    lead = make_lead(audit, account_id)
    audit.record_refusal(
        account_id,
        INVITE_ACTION,
        RefusalReason.OUTSIDE_WORKING_HOURS,
        lead_id=lead.id,
        occurred_at=NOON - timedelta(hours=8),
    )

    assert gate.acquire(account_id, INVITE_ACTION, lead.id, approved=True)


def test_a_duplicate_outside_the_window_is_allowed_again(audit, account_id, gate):
    lead = make_lead(audit, account_id)
    audit.record(
        account_id,
        INVITE_ACTION,
        Outcome.SUCCESS,
        lead_id=lead.id,
        occurred_at=NOON - timedelta(days=120),
    )

    assert gate.acquire(account_id, INVITE_ACTION, lead.id, approved=True)


def test_a_duplicate_is_found_behind_a_long_run_of_later_invites(audit, account_id, gate):
    lead = make_lead(audit, account_id, "old")
    audit.record(
        account_id,
        INVITE_ACTION,
        Outcome.SUCCESS,
        lead_id=lead.id,
        occurred_at=NOON - timedelta(days=80),
    )
    for index in range(700):
        audit.record(
            account_id,
            INVITE_ACTION,
            Outcome.SUCCESS,
            occurred_at=NOON - timedelta(days=79) + timedelta(minutes=index * 60),
        )

    with pytest.raises(DuplicateAction) as refused:
        gate.acquire(account_id, INVITE_ACTION, lead.id, approved=True)

    assert refused.value.detail["previous_occurred_at"].startswith("2025-12-24")


def test_a_different_lead_is_not_a_duplicate(audit, account_id, gate):
    first = make_lead(audit, account_id, "1")
    second = make_lead(audit, account_id, "2")
    audit.record(
        account_id,
        INVITE_ACTION,
        Outcome.SUCCESS,
        lead_id=first.id,
        occurred_at=NOON - timedelta(days=1),
    )

    assert gate.acquire(account_id, INVITE_ACTION, second.id, approved=True)


def test_an_action_type_with_no_dedupe_window_may_repeat(audit, account_id, gate):
    lead = make_lead(audit, account_id)
    audit.record(
        account_id,
        "profile_view",
        Outcome.SUCCESS,
        lead_id=lead.id,
        occurred_at=NOON - timedelta(hours=2),
    )

    assert gate.acquire(account_id, "profile_view", lead.id)


# --- human approval --------------------------------------------------------


def test_outreach_without_human_approval_is_refused(audit, account_id, gate):
    with pytest.raises(ApprovalRequired) as refused:
        gate.acquire(account_id, "post_comment")

    assert refused.value.reason is RefusalReason.APPROVAL_REQUIRED


def test_approved_outreach_runs(audit, account_id, gate):
    assert gate.acquire(account_id, "post_comment", approved=True)


def test_a_read_action_needs_no_approval(audit, account_id, gate):
    assert gate.acquire(account_id, "profile_search")


# --- working hours ---------------------------------------------------------


def test_an_action_outside_working_hours_is_refused(audit, account_id, gate):
    set_working_hours(audit, account_id, MONDAY, 9 * 60, 18 * 60)

    with pytest.raises(WorkingHoursClosed) as refused:
        gate.acquire(account_id, "post_like")

    assert refused.value.detail["local_weekday"] == SATURDAY
    assert refused.value.detail["local_minute"] == 12 * 60


def test_an_action_inside_working_hours_runs(audit, account_id, gate):
    set_working_hours(audit, account_id, SATURDAY, 9 * 60, 18 * 60)

    assert gate.acquire(account_id, "post_like")


def test_the_closing_minute_is_already_closed(audit, account_id, gate):
    set_working_hours(audit, account_id, SATURDAY, 9 * 60, 12 * 60)

    with pytest.raises(WorkingHoursClosed):
        gate.acquire(account_id, "post_like")


def test_an_account_with_no_schedule_is_always_open(audit, account_id, gate):
    assert gate.acquire(account_id, "post_like")


# --- caps ------------------------------------------------------------------


def test_the_hourly_burst_cap_is_enforced(audit, account_id, gate):
    log_actions(audit, account_id, "profile_view", GLOBAL_HOURLY_CEILING, spacing=timedelta(seconds=30))

    with pytest.raises(HourlyCapReached) as refused:
        gate.acquire(account_id, "post_like")

    assert refused.value.detail["cap"] == GLOBAL_HOURLY_CEILING


def test_the_global_daily_cap_catches_a_spread_of_action_types(audit, account_id, gate):
    for action_type in ("profile_view", "post_read"):
        log_actions(audit, account_id, action_type, 60, spacing=timedelta(minutes=10))
    log_actions(audit, account_id, "post_like", 30, spacing=timedelta(minutes=20))

    with pytest.raises(DailyCapReached) as refused:
        gate.acquire(account_id, "profile_search")

    assert refused.value.detail["limit"] == "global_daily"


def test_an_unconfigured_action_cannot_burst_past_the_global_ceiling(
    audit, account_id, gate
):
    log_actions(audit, account_id, "post_repost", 50, spacing=timedelta(minutes=10))
    log_actions(audit, account_id, "profile_view", 100, spacing=timedelta(minutes=10))

    with pytest.raises(DailyCapReached) as refused:
        gate.acquire(account_id, "post_read")

    assert refused.value.detail["limit"] == "global_daily"
    assert refused.value.detail["used"] == 150


def test_the_weekly_invite_cap_is_enforced(audit, account_id, gate):
    set_account_limit(audit, account_id, INVITE_ACTION, daily_cap=5, weekly_cap=20)
    for day in range(1, 6):
        for index in range(4):
            audit.record(
                account_id,
                INVITE_ACTION,
                Outcome.SUCCESS,
                occurred_at=NOON - timedelta(days=day, minutes=index * 5),
            )

    with pytest.raises(WeeklyCapReached) as refused:
        gate.acquire(account_id, INVITE_ACTION, approved=True)

    assert refused.value.detail["limit"] == "weekly"
    assert refused.value.detail["used"] == 20


def test_the_pending_invite_ceiling_stops_a_backlog_growing(audit, account_id, gate):
    set_account_limit(audit, account_id, INVITE_ACTION, daily_cap=30, weekly_cap=100)
    for index in range(PENDING_INVITE_CEILING):
        audit.record(
            account_id,
            INVITE_ACTION,
            Outcome.SUCCESS,
            occurred_at=NOON - timedelta(days=10, seconds=index * 60),
        )

    with pytest.raises(PendingInviteCeilingReached) as refused:
        gate.acquire(account_id, INVITE_ACTION, approved=True)

    assert refused.value.reason is RefusalReason.WEEKLY_CAP_REACHED
    assert refused.value.detail["limit"] == "pending_invites"
    assert refused.value.detail["used"] == PENDING_INVITE_CEILING


def test_accepted_invitations_free_up_the_pending_ceiling(audit, account_id, gate):
    for index in range(PENDING_INVITE_CEILING):
        audit.record(
            account_id,
            INVITE_ACTION,
            Outcome.SUCCESS,
            occurred_at=NOON - timedelta(days=10, seconds=index * 60),
        )
    audit.record(
        account_id,
        "connection_accepted",
        Outcome.SUCCESS,
        occurred_at=NOON - timedelta(days=9),
    )

    assert gate.acquire(account_id, INVITE_ACTION, approved=True)


def test_a_warming_account_is_refused_for_warming_up_not_for_the_cap(audit, account_id, gate):
    set_account_age(audit, account_id, 1)
    log_actions(audit, account_id, "post_like", 12)

    with pytest.raises(WarmupLimit) as refused:
        gate.acquire(account_id, "post_like")

    assert refused.value.reason is RefusalReason.WARMUP_LIMIT
    assert refused.value.detail["warmup_cap"] == 12
    assert refused.value.detail["configured_cap"] == HARD_CEILINGS["post_like"].daily


def test_a_configured_cap_cannot_be_raised_above_the_hard_ceiling(audit, account_id, gate):
    set_account_limit(audit, account_id, INVITE_ACTION, daily_cap=10_000, weekly_cap=10_000)
    log_actions(audit, account_id, INVITE_ACTION, INVITE_DAILY_CAP)

    with pytest.raises(DailyCapReached) as refused:
        gate.acquire(account_id, INVITE_ACTION, approved=True)

    assert refused.value.detail["cap"] == INVITE_DAILY_CAP


def test_a_stricter_configured_cap_is_honoured(audit, account_id, gate):
    set_account_limit(audit, account_id, INVITE_ACTION, daily_cap=3)
    log_actions(audit, account_id, INVITE_ACTION, 3)

    with pytest.raises(DailyCapReached) as refused:
        gate.acquire(account_id, INVITE_ACTION, approved=True)

    assert refused.value.detail["cap"] == 3


def test_yesterdays_actions_fall_out_of_the_rolling_day(audit, account_id, gate):
    for index in range(INVITE_DAILY_CAP):
        audit.record(
            account_id,
            INVITE_ACTION,
            Outcome.SUCCESS,
            occurred_at=NOON - timedelta(days=1, minutes=index + 1),
        )

    assert gate.acquire(account_id, INVITE_ACTION, approved=True)


# --- jitter ----------------------------------------------------------------


def test_the_daily_boundary_lands_on_the_jittered_cap(audit, account_id):
    jittered = SafetyGate(clock=lambda: NOON)
    set_gate(jittered)
    expected = shrink_for_jitter(INVITE_DAILY_CAP, daily_jitter_fraction(account_id, NOON))
    log_actions(audit, account_id, INVITE_ACTION, expected - 1)

    assert jittered.acquire(account_id, INVITE_ACTION, approved=True).remaining("daily") == 0

    audit.record(account_id, INVITE_ACTION, Outcome.SUCCESS, occurred_at=NOON)

    with pytest.raises(DailyCapReached):
        jittered.acquire(account_id, INVITE_ACTION, approved=True)

    assert INVITE_DAILY_CAP * 0.9 <= expected <= INVITE_DAILY_CAP


def test_the_same_day_always_yields_the_same_cap(audit, account_id):
    morning = SafetyGate(clock=lambda: NOON.replace(hour=8))
    evening = SafetyGate(clock=lambda: NOON.replace(hour=21))
    set_gate(morning)

    first = morning.acquire(account_id, "post_like").budget("daily").cap
    second = evening.acquire(account_id, "post_like").budget("daily").cap

    assert first == second


# --- refusal bookkeeping ---------------------------------------------------


def test_every_refusal_reason_has_a_typed_exception():
    assert {cls.reason for cls in all_refusal_classes()} == set(RefusalReason)


def test_a_refusal_is_written_to_the_audit_log_with_its_typed_reason(audit, account_id, gate):
    set_account_limit(audit, account_id, "post_like", daily_cap=1)
    log_actions(audit, account_id, "post_like", 1)

    with pytest.raises(DailyCapReached):
        gate.acquire(account_id, "post_like")

    rows = audit.refusals(account_id)
    assert len(rows) == 1
    assert rows[0]["outcome"] == Outcome.REFUSED.value
    assert RefusalReason.DAILY_CAP_REACHED.value in rows[0]["detail_json"]
    assert "post_like" in rows[0]["detail_json"]


def test_a_refusal_does_not_consume_cap(audit, account_id, gate):
    log_actions(audit, account_id, "post_comment", 1)

    with pytest.raises(ApprovalRequired):
        gate.acquire(account_id, "post_comment")
    with pytest.raises(ApprovalRequired):
        gate.acquire(account_id, "post_comment")

    lease = gate.acquire(account_id, "post_comment", approved=True)

    assert len(audit.refusals(account_id)) == 2
    assert lease.budget("daily").used == 1
    assert lease.budget("global_daily").used == 1


def test_a_per_lead_refusal_stays_out_of_the_safety_timeline(audit, account_id, gate):
    lead = make_lead(audit, account_id)
    blacklist_lead(audit.connection, lead.id)

    with pytest.raises(Blacklisted):
        gate.acquire(account_id, INVITE_ACTION, lead.id, approved=True)

    assert safety_events(audit, account_id) == []
    assert len(audit.refusals(account_id)) == 1


def test_a_cap_refusal_lands_on_the_safety_timeline(audit, account_id, gate):
    set_account_limit(audit, account_id, "post_like", daily_cap=1)
    log_actions(audit, account_id, "post_like", 1)

    with pytest.raises(DailyCapReached):
        gate.acquire(account_id, "post_like")

    events = safety_events(audit, account_id)
    assert len(events) == 1
    assert events[0]["kind"] == SAFETY_EVENT_KIND
    assert RefusalReason.DAILY_CAP_REACHED.value in events[0]["detail_json"]


# --- guard_action ----------------------------------------------------------


def test_guard_action_returns_nothing_when_the_action_may_run(audit, account_id, gate):
    assert guard_action("post_like") is None


def test_guard_action_returns_a_refusal_payload(audit, account_id, gate):
    set_account_state(audit, account_id, "paused")

    result = guard_action("post_like")

    assert result["status"] == "refused"
    assert result["reason"] == RefusalReason.ACCOUNT_PAUSED.value
    assert result["audit_logged"] is True
    assert result["account_id"] == account_id


def test_guard_action_fails_closed_when_the_gate_itself_breaks(audit, account_id):
    set_gate(BrokenGate())

    result = guard_action("post_like")

    assert result["status"] == "error"
    assert "Safety gate unavailable" in result["message"]


# --- MCP tool routing ------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "call_tool",
    [
        pytest.param(
            lambda ctx: linkedin_browser_mcp.get_linkedin_profile("someone", ctx),
            id="get_linkedin_profile",
        ),
        pytest.param(
            lambda ctx: linkedin_browser_mcp.browse_linkedin_feed(ctx),
            id="browse_linkedin_feed",
        ),
        pytest.param(
            lambda ctx: linkedin_browser_mcp.search_linkedin_profiles("ai engineer", ctx),
            id="search_linkedin_profiles",
        ),
        pytest.param(
            lambda ctx: linkedin_browser_mcp.view_linkedin_profile(PROFILE_URL, ctx),
            id="view_linkedin_profile",
        ),
        pytest.param(
            lambda ctx: linkedin_browser_mcp.interact_with_linkedin_post(POST_URL, ctx),
            id="interact_with_linkedin_post",
        ),
        pytest.param(
            lambda ctx: linkedin_browser_mcp.send_connection_request(PROFILE_URL, ctx),
            id="send_connection_request",
        ),
        pytest.param(
            lambda ctx: linkedin_browser_mcp.search_linkedin_posts("copilot", ctx),
            id="search_linkedin_posts",
        ),
        pytest.param(
            lambda ctx: linkedin_browser_mcp.comment_on_approved_posts(
                [{"post_url": POST_URL, "comment": "hello"}], ctx
            ),
            id="comment_on_approved_posts",
        ),
    ],
)
async def test_every_action_tool_refuses_before_opening_a_browser(
    audit, account_id, gate, monkeypatch, call_tool
):
    monkeypatch.setattr(linkedin_browser_mcp, "BrowserSession", ExplodingBrowserSession)
    set_account_state(audit, account_id, "paused")

    result = await call_tool(MockContext())

    assert result["status"] == "refused"
    assert result["reason"] == RefusalReason.ACCOUNT_PAUSED.value


@pytest.mark.asyncio
async def test_a_direct_profile_load_spends_its_own_smaller_budget(
    audit, account_id, gate, monkeypatch
):
    monkeypatch.setattr(linkedin_browser_mcp, "BrowserSession", ExplodingBrowserSession)
    log_actions(audit, account_id, "profile_view_direct", HARD_CEILINGS["profile_view_direct"].daily)
    ctx = MockContext()

    refused = await linkedin_browser_mcp.view_linkedin_profile(PROFILE_URL, ctx, direct=True)

    assert refused["status"] == "refused"
    assert refused["reason"] == RefusalReason.DAILY_CAP_REACHED.value
    assert refused["action_type"] == "profile_view_direct"
    assert guard_action("profile_view") is None


@pytest.mark.asyncio
async def test_logging_in_is_never_gated(audit, account_id, gate, monkeypatch):
    monkeypatch.setattr(linkedin_browser_mcp, "BrowserSession", ExplodingBrowserSession)
    monkeypatch.delenv("LINKEDIN_USERNAME", raising=False)
    monkeypatch.delenv("LINKEDIN_PASSWORD", raising=False)
    set_account_state(audit, account_id, "challenged")

    result = await linkedin_browser_mcp.login_linkedin_secure(MockContext())

    assert result["status"] == "error"
    assert "Missing LinkedIn credentials" in result["message"]


@pytest.mark.asyncio
async def test_a_refused_tool_call_writes_exactly_one_audit_row(
    audit, account_id, gate, monkeypatch
):
    monkeypatch.setattr(linkedin_browser_mcp, "BrowserSession", ExplodingBrowserSession)
    set_account_state(audit, account_id, "cooldown")

    await linkedin_browser_mcp.search_linkedin_posts("copilot", MockContext())

    rows = audit.entries(account_id, action_type="post_search")
    assert len(rows) == 1
    assert rows[0]["outcome"] == Outcome.REFUSED.value
