import os
import re
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from linkedin_mcp.audit import instrument
from linkedin_mcp.audit.log import AuditLog, Outcome, reset_audit_log, set_audit_log
from linkedin_mcp.core.config import (
    APPROVAL_REQUIRED_ACTIONS,
    DEFAULT_CEILING,
    GLOBAL_DAILY_CEILING,
    GLOBAL_HOURLY_CEILING,
    HARD_CEILINGS,
    INVITE_ACTION,
    JITTER_MAX_SHRINK,
    METERED_ACTIONS,
    PENDING_INVITE_CEILING,
    RAMP_UP_DAYS,
    RAMP_UP_START_FRACTION,
    UNMETERED_ACTIONS,
    ActionCeiling,
    ceiling_for,
    dedupe_window_days,
    is_metered,
    profile_view_action,
)
from linkedin_mcp.safety.limits import (
    DAY,
    HOUR,
    WEEK,
    WorkingWindow,
    account_limit,
    actions_in_window,
    daily_budget,
    daily_jitter_fraction,
    global_actions_in_window,
    global_daily_budget,
    hourly_budget,
    is_within_working_hours,
    local_weekday_and_minute,
    metered_universe,
    observed_action_types,
    pending_invite_budget,
    pending_invites,
    ramp_up_cap,
    ramp_up_fraction,
    resolve_timezone,
    shrink_for_jitter,
    weekly_budget,
    working_windows,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

# A Saturday, so weekday arithmetic in the working-hours tests is unambiguous.
NOON = datetime(2026, 3, 14, 12, 0, tzinfo=timezone.utc)
MONDAY = 0
SATURDAY = 5
NINE_AM = 9 * 60
SIX_PM = 18 * 60


@pytest.fixture
def audit(tmp_path):
    log = AuditLog.open(tmp_path / "linkedin-helper.db")
    set_audit_log(log)
    try:
        yield log
    finally:
        reset_audit_log()
        instrument.reset_account_resolver()
        log.close()


@pytest.fixture
def account_id(audit):
    return audit.ensure_account("limits@example.com")


def log_actions(audit, account_id, action_type, count, *, first_at, spacing=timedelta(minutes=1)):
    for index in range(count):
        audit.record(
            account_id,
            action_type,
            Outcome.SUCCESS,
            occurred_at=first_at - spacing * index,
        )


def set_account_limit(audit, account_id, action_type, **columns):
    keys = ", ".join(columns)
    placeholders = ", ".join("?" for _ in columns)
    audit.connection.execute(
        f"INSERT INTO account_limits (account_id, action_type, {keys}) "
        f"VALUES (?, ?, {placeholders})",
        (account_id, action_type, *columns.values()),
    )
    audit.connection.commit()


def test_hard_ceilings_carry_the_numbers_that_used_to_live_in_prose():
    assert HARD_CEILINGS[INVITE_ACTION] == ActionCeiling(daily=30, weekly=100)
    assert HARD_CEILINGS["profile_view_direct"].daily == 40
    assert GLOBAL_DAILY_CEILING == 150
    assert GLOBAL_HOURLY_CEILING == 50
    assert PENDING_INVITE_CEILING > 0


def test_an_unknown_action_type_falls_back_to_the_default_ceiling():
    assert ceiling_for("some_future_action") is DEFAULT_CEILING
    assert DEFAULT_CEILING.daily <= GLOBAL_DAILY_CEILING


def test_metered_and_unmetered_action_sets_do_not_overlap():
    assert METERED_ACTIONS.isdisjoint(UNMETERED_ACTIONS)
    assert is_metered(INVITE_ACTION)
    assert not is_metered("login")
    assert not is_metered("browser_close")


def test_every_approval_gated_action_is_also_metered():
    assert APPROVAL_REQUIRED_ACTIONS <= METERED_ACTIONS


def test_profile_view_action_separates_direct_loads_from_navigated_ones():
    assert profile_view_action(False) == "profile_view"
    assert profile_view_action(True) == "profile_view_direct"
    assert HARD_CEILINGS["profile_view_direct"].daily < HARD_CEILINGS["profile_view"].daily


def test_dedupe_windows_cover_outreach_and_leave_reads_alone():
    assert dedupe_window_days(INVITE_ACTION) == 90
    assert dedupe_window_days("post_comment") == 30
    assert dedupe_window_days("profile_view") is None


@pytest.mark.parametrize(
    ("requested", "expected"),
    [(None, 30), (10, 10), (30, 30), (1000, 30), (0, 0), (-5, 0)],
)
def test_a_ceiling_clamps_a_configured_daily_cap(requested, expected):
    assert HARD_CEILINGS[INVITE_ACTION].clamp_daily(requested) == expected


@pytest.mark.parametrize(
    ("requested", "expected"),
    [(None, 100), (40, 40), (100, 100), (5000, 100)],
)
def test_a_ceiling_clamps_a_configured_weekly_cap(requested, expected):
    assert HARD_CEILINGS[INVITE_ACTION].clamp_weekly(requested) == expected


def test_a_ceiling_without_a_weekly_number_accepts_the_configured_one():
    assert HARD_CEILINGS["post_like"].clamp_weekly(None) is None
    assert HARD_CEILINGS["post_like"].clamp_weekly(200) == 200


@pytest.mark.parametrize(
    "kwargs",
    [
        {"daily": -1},
        {"daily": 10, "weekly": -1},
        {"daily": 10, "hourly": -1},
        {"daily": 30, "weekly": 10},
    ],
)
def test_an_incoherent_ceiling_is_rejected(kwargs):
    with pytest.raises(ValueError):
        ActionCeiling(**kwargs)


def test_jitter_is_stable_for_one_account_day():
    first = daily_jitter_fraction(7, date(2026, 3, 14))
    repeated = daily_jitter_fraction(7, date(2026, 3, 14))

    assert first == repeated
    assert first == daily_jitter_fraction(7, datetime(2026, 3, 14, 23, 59, tzinfo=timezone.utc))
    assert first == daily_jitter_fraction(7, "2026-03-14 08:15:00")


def test_jitter_survives_a_process_restart_with_a_different_hash_seed():
    script = (
        "from datetime import date;"
        "from linkedin_mcp.safety.limits import daily_jitter_fraction;"
        "print(repr(daily_jitter_fraction(7, date(2026, 3, 14))))"
    )
    env = {**os.environ, "PYTHONHASHSEED": "1"}

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    assert float(completed.stdout.strip()) == daily_jitter_fraction(7, date(2026, 3, 14))


def test_jitter_never_shrinks_a_cap_by_more_than_ten_percent():
    fractions = [
        daily_jitter_fraction(account, date(2026, 3, 1) + timedelta(days=offset))
        for account in range(1, 12)
        for offset in range(30)
    ]

    assert all(0.0 <= fraction <= JITTER_MAX_SHRINK for fraction in fractions)
    assert len(set(fractions)) > 1


def test_jitter_differs_across_days_and_accounts():
    days = {daily_jitter_fraction(1, date(2026, 3, 1) + timedelta(days=n)) for n in range(14)}
    accounts = {daily_jitter_fraction(n, date(2026, 3, 14)) for n in range(1, 15)}

    assert len(days) > 1
    assert len(accounts) > 1


@pytest.mark.parametrize(
    ("cap", "fraction", "expected"),
    [(50, 0.0, 50), (50, 0.1, 45), (30, 0.1, 27), (30, 0.05, 28), (1, 0.1, 1), (0, 0.1, 0)],
)
def test_jitter_shrinks_a_cap_without_ever_zeroing_it(cap, fraction, expected):
    assert shrink_for_jitter(cap, fraction) == expected


def test_an_out_of_range_jitter_fraction_is_rejected():
    with pytest.raises(ValueError):
        shrink_for_jitter(10, 1.0)
    with pytest.raises(ValueError):
        shrink_for_jitter(10, -0.01)


def test_an_account_with_no_recorded_age_is_treated_as_established():
    assert ramp_up_fraction(0) == 1.0
    assert ramp_up_cap(30, 0) == 30


def test_warmup_starts_on_day_one_and_finishes_on_the_last_day():
    assert ramp_up_fraction(1) == pytest.approx(RAMP_UP_START_FRACTION)
    assert ramp_up_fraction(RAMP_UP_DAYS) == 1.0
    assert ramp_up_fraction(RAMP_UP_DAYS + 40) == 1.0

    curve = [ramp_up_fraction(age) for age in range(1, RAMP_UP_DAYS + 1)]
    assert curve == sorted(curve)


def test_warmup_never_scales_a_cap_below_one():
    assert ramp_up_cap(2, 1) == 1
    assert ramp_up_cap(0, 1) == 0
    assert ramp_up_cap(30, 1) == 6


def test_a_negative_account_age_is_rejected():
    with pytest.raises(ValueError):
        ramp_up_fraction(-1)


def test_an_account_with_no_limit_row_runs_at_the_ceiling(audit, account_id):
    limit = account_limit(audit.connection, account_id, INVITE_ACTION)

    assert limit.daily_cap == 30
    assert limit.weekly_cap == 100
    assert limit.enabled


def test_a_configured_limit_can_tighten_but_never_loosen_a_ceiling(audit, account_id):
    set_account_limit(audit, account_id, INVITE_ACTION, daily_cap=5000, weekly_cap=9000)

    limit = account_limit(audit.connection, account_id, INVITE_ACTION)

    assert limit.daily_cap == 30
    assert limit.weekly_cap == 100


def test_a_configured_limit_below_the_ceiling_is_honoured(audit, account_id):
    set_account_limit(audit, account_id, INVITE_ACTION, daily_cap=4, weekly_cap=12)

    limit = account_limit(audit.connection, account_id, INVITE_ACTION)

    assert limit.daily_cap == 4
    assert limit.weekly_cap == 12


def test_a_disabled_action_is_reported_as_disabled(audit, account_id):
    set_account_limit(audit, account_id, "post_like", daily_cap=10, enabled=0)

    assert not account_limit(audit.connection, account_id, "post_like").enabled


def test_usage_counts_only_what_actually_reached_linkedin(audit, account_id):
    audit.record(account_id, "post_like", Outcome.SUCCESS, occurred_at=NOON - timedelta(hours=1))
    audit.record(account_id, "post_like", Outcome.FAILURE, occurred_at=NOON - timedelta(hours=2))
    audit.record_refusal(account_id, "post_like", "daily_cap_reached", occurred_at=NOON - timedelta(hours=3))
    audit.record(account_id, "post_like", Outcome.SKIPPED, occurred_at=NOON - timedelta(hours=4))

    assert actions_in_window(account_id, "post_like", window=DAY, now=NOON) == 2


def test_the_window_edge_is_exact(audit, account_id):
    audit.record(account_id, "post_like", Outcome.SUCCESS, occurred_at=NOON - DAY + timedelta(seconds=1))
    audit.record(account_id, "post_like", Outcome.SUCCESS, occurred_at=NOON - DAY - timedelta(seconds=1))
    audit.record(account_id, "post_like", Outcome.SUCCESS, occurred_at=NOON)

    assert actions_in_window(account_id, "post_like", window=DAY, now=NOON) == 2


def test_a_clock_skewed_future_row_cannot_inflate_a_window(audit, account_id):
    audit.record(account_id, "post_like", Outcome.SUCCESS, occurred_at=NOON + timedelta(minutes=5))

    assert actions_in_window(account_id, "post_like", window=DAY, now=NOON) == 0


def test_the_global_window_sums_every_metered_action_type(audit, account_id):
    log_actions(audit, account_id, "post_like", 3, first_at=NOON - timedelta(minutes=1))
    log_actions(audit, account_id, INVITE_ACTION, 2, first_at=NOON - timedelta(minutes=10))
    log_actions(audit, account_id, "profile_view", 4, first_at=NOON - timedelta(hours=5))

    assert global_actions_in_window(account_id, window=DAY, now=NOON) == 9


def test_unmetered_bookkeeping_stays_out_of_the_global_window(audit, account_id):
    log_actions(audit, account_id, "login", 6, first_at=NOON - timedelta(minutes=1))
    log_actions(audit, account_id, "browser_close", 6, first_at=NOON - timedelta(minutes=2))
    audit.record(account_id, "post_like", Outcome.SUCCESS, occurred_at=NOON - timedelta(minutes=3))

    assert global_actions_in_window(account_id, window=DAY, now=NOON) == 1


def test_an_action_type_without_a_ceiling_still_counts_against_the_global_budget(
    audit, account_id
):
    log_actions(
        audit,
        account_id,
        "post_repost",
        80,
        first_at=NOON - timedelta(seconds=30),
        spacing=timedelta(seconds=30),
    )
    universe = metered_universe("post_repost", observed_action_types(audit.connection, account_id))

    assert global_actions_in_window(account_id, window=DAY, now=NOON) == 0
    assert global_actions_in_window(account_id, window=DAY, now=NOON, action_types=universe) == 80
    assert global_daily_budget(account_id, now=NOON, action_types=universe).used == 80
    assert hourly_budget(account_id, now=NOON, action_types=universe).used == 80


def test_an_unconfigured_action_already_in_the_log_counts_while_gating_another(
    audit, account_id
):
    log_actions(
        audit,
        account_id,
        "post_repost",
        40,
        first_at=NOON - timedelta(seconds=30),
        spacing=timedelta(seconds=30),
    )
    log_actions(
        audit,
        account_id,
        "post_like",
        10,
        first_at=NOON - timedelta(minutes=25),
        spacing=timedelta(seconds=30),
    )
    universe = metered_universe(
        "post_like", observed_action_types(audit.connection, account_id)
    )

    assert global_actions_in_window(account_id, window=DAY, now=NOON, action_types=universe) == 50


def test_the_metered_universe_is_closed_by_exclusion(audit, account_id):
    log_actions(audit, account_id, "login", 3, first_at=NOON - timedelta(minutes=1))
    log_actions(audit, account_id, "post_repost", 3, first_at=NOON - timedelta(minutes=2))
    observed = observed_action_types(audit.connection, account_id)

    assert observed == {"login", "post_repost"}
    assert metered_universe() == METERED_ACTIONS
    assert metered_universe("post_like") == METERED_ACTIONS
    assert metered_universe(None, observed) == METERED_ACTIONS | {"post_repost"}
    assert metered_universe(None, observed).isdisjoint(UNMETERED_ACTIONS)


def test_no_tool_gates_an_action_type_the_config_has_never_heard_of():
    source = (REPO_ROOT / "linkedin_browser_mcp.py").read_text(encoding="utf-8")
    named = set(re.findall(r'guard_action\(\s*"([a-z_]+)"', source))
    named |= set(re.findall(r'audit_linkedin_action\(\s*"([a-z_]+)"', source))

    assert named
    assert named <= (METERED_ACTIONS | UNMETERED_ACTIONS)


def test_every_post_interaction_the_server_offers_has_a_configured_ceiling():
    """The `post_{action}` names are built at runtime, so read them from source.

    A new entry in `valid_actions` is how the next post interaction will arrive,
    and it must arrive with a ceiling rather than with the fifty a day default.
    """
    source = (REPO_ROOT / "linkedin_browser_mcp.py").read_text(encoding="utf-8")
    listed = re.search(r"valid_actions\s*=\s*\[([^\]]*)\]", source)

    assert listed
    offered = {f"post_{name}" for name in re.findall(r'"([a-z_]+)"', listed.group(1))}
    assert offered
    assert offered <= METERED_ACTIONS


def test_every_profile_view_action_the_server_can_produce_has_a_ceiling():
    assert {profile_view_action(False), profile_view_action(True)} <= METERED_ACTIONS


def test_the_hourly_budget_is_a_burst_guard_across_every_action(audit, account_id):
    log_actions(audit, account_id, "post_like", 20, first_at=NOON - timedelta(minutes=1))
    log_actions(audit, account_id, "profile_view", 5, first_at=NOON - timedelta(minutes=30))
    log_actions(audit, account_id, "post_like", 10, first_at=NOON - timedelta(hours=3))

    budget = hourly_budget(account_id, now=NOON)

    assert budget.cap == GLOBAL_HOURLY_CEILING
    assert budget.used == 25
    assert budget.jitter_fraction == 0.0
    assert not budget.exhausted


def test_the_daily_budget_reports_the_cap_the_jitter_left(audit, account_id):
    log_actions(audit, account_id, "post_like", 4, first_at=NOON - timedelta(hours=1))

    budget = daily_budget(
        account_id, "post_like", configured_cap=50, now=NOON, jitter_fraction=0.1
    )

    assert budget.configured == 50
    assert budget.cap == 45
    assert budget.used == 4
    assert budget.remaining == 41
    assert not budget.exhausted
    assert not budget.warmup_bound


def test_a_warming_account_reports_the_warmup_cap_as_the_binding_one(audit, account_id):
    log_actions(audit, account_id, "post_like", 6, first_at=NOON - timedelta(hours=1))

    budget = daily_budget(
        account_id,
        "post_like",
        configured_cap=30,
        now=NOON,
        account_age_days=1,
        jitter_fraction=0.0,
    )

    assert budget.configured == 30
    assert budget.after_ramp == 6
    assert budget.cap == 6
    assert budget.exhausted
    assert budget.warmup_bound
    assert budget.as_detail()["warmup_cap"] == 6


def test_an_exhausted_established_account_is_not_reported_as_warming(audit, account_id):
    log_actions(audit, account_id, "post_like", 10, first_at=NOON - timedelta(hours=1))

    budget = daily_budget(
        account_id, "post_like", configured_cap=10, now=NOON, jitter_fraction=0.0
    )

    assert budget.exhausted
    assert not budget.warmup_bound


def test_the_weekly_budget_spans_seven_days(audit, account_id):
    audit.record(account_id, INVITE_ACTION, Outcome.SUCCESS, occurred_at=NOON - timedelta(days=6))
    audit.record(account_id, INVITE_ACTION, Outcome.SUCCESS, occurred_at=NOON - timedelta(days=8))

    budget = weekly_budget(
        account_id, INVITE_ACTION, configured_cap=100, now=NOON, jitter_fraction=0.0
    )

    assert budget.used == 1
    assert budget.cap == 100
    assert actions_in_window(account_id, INVITE_ACTION, window=WEEK, now=NOON) == 1


def test_the_global_daily_budget_warms_up_with_the_account(audit, account_id):
    budget = global_daily_budget(
        account_id, now=NOON, account_age_days=1, jitter_fraction=0.0
    )

    assert budget.configured == GLOBAL_DAILY_CEILING
    assert budget.after_ramp == 30
    assert budget.cap == 30


def test_pending_invites_are_sent_minus_accepted(audit, account_id):
    log_actions(audit, account_id, INVITE_ACTION, 12, first_at=NOON - timedelta(days=2))
    log_actions(audit, account_id, "connection_accepted", 5, first_at=NOON - timedelta(days=1))

    assert pending_invites(account_id, now=NOON) == 7


def test_pending_invites_ignore_invitations_older_than_the_window(audit, account_id):
    log_actions(audit, account_id, INVITE_ACTION, 3, first_at=NOON - timedelta(days=40))
    log_actions(audit, account_id, INVITE_ACTION, 2, first_at=NOON - timedelta(days=1))

    assert pending_invites(account_id, now=NOON) == 2


def test_pending_invites_never_go_negative(audit, account_id):
    log_actions(audit, account_id, "connection_accepted", 4, first_at=NOON - timedelta(days=1))

    assert pending_invites(account_id, now=NOON) == 0
    assert pending_invite_budget(account_id, now=NOON).used == 0


def test_no_working_hours_configured_means_always_open():
    assert is_within_working_hours((), MONDAY, 0)
    assert is_within_working_hours((), SATURDAY, 3 * 60)


@pytest.mark.parametrize(
    ("minute", "open_"),
    [
        (NINE_AM - 1, False),
        (NINE_AM, True),
        (NINE_AM + 1, True),
        (SIX_PM - 1, True),
        (SIX_PM, False),
        (SIX_PM + 1, False),
    ],
)
def test_a_working_window_is_half_open(minute, open_):
    windows = (WorkingWindow(MONDAY, NINE_AM, SIX_PM),)

    assert is_within_working_hours(windows, MONDAY, minute) is open_


def test_a_weekday_without_a_row_is_a_day_off():
    windows = (WorkingWindow(MONDAY, NINE_AM, SIX_PM),)

    assert not is_within_working_hours(windows, SATURDAY, NINE_AM + 30)


def test_a_window_can_run_past_midnight():
    windows = (WorkingWindow(MONDAY, 22 * 60, 2 * 60),)

    assert is_within_working_hours(windows, MONDAY, 23 * 60)
    assert is_within_working_hours(windows, MONDAY + 1, 1 * 60)
    assert not is_within_working_hours(windows, MONDAY + 1, 3 * 60)
    assert not is_within_working_hours(windows, MONDAY, 21 * 60 + 59)


def test_a_window_that_starts_where_it_ends_covers_the_whole_day():
    windows = (WorkingWindow(SATURDAY, 0, 0),)

    assert is_within_working_hours(windows, SATURDAY, 0)
    assert is_within_working_hours(windows, SATURDAY, 1439)
    assert not is_within_working_hours(windows, MONDAY, 600)


def test_working_windows_come_back_from_the_database(audit, account_id):
    audit.connection.executemany(
        "INSERT INTO working_hours (account_id, weekday, start_minute, end_minute) "
        "VALUES (?, ?, ?, ?)",
        [(account_id, MONDAY, NINE_AM, SIX_PM), (account_id, SATURDAY, 600, 720)],
    )
    audit.connection.commit()

    windows = working_windows(audit.connection, account_id)

    assert windows == (
        WorkingWindow(MONDAY, NINE_AM, SIX_PM),
        WorkingWindow(SATURDAY, 600, 720),
    )


def test_local_time_is_read_in_the_accounts_own_zone():
    zone = timezone(timedelta(hours=5, minutes=30))
    late_utc = datetime(2026, 3, 14, 20, 0, tzinfo=timezone.utc)

    weekday, minute = local_weekday_and_minute(late_utc, zone)

    assert (weekday, minute) == (6, 90)
    assert local_weekday_and_minute(late_utc, timezone.utc) == (5, 20 * 60)


def test_a_naive_moment_is_read_as_utc():
    naive = datetime(2026, 3, 14, 12, 0)

    assert local_weekday_and_minute(naive, timezone.utc) == (SATURDAY, 720)


@pytest.mark.parametrize("name", ["UTC", "utc", "  UTC  ", "", None])
def test_utc_resolves_without_a_system_timezone_database(name):
    assert resolve_timezone(name) is timezone.utc


def test_an_unresolvable_timezone_degrades_to_utc_instead_of_failing():
    assert resolve_timezone("Not/AZone") is timezone.utc
