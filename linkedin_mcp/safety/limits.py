"""Rolling-window rate-limit arithmetic for the safety gate.

Every number here is derived, never stored. A cap is a clamp against
`linkedin_mcp.core.config.HARD_CEILINGS`, and usage is a `COUNT(*)` over
`actions_log` through the audit package. There is no counter column to drift out
of sync with reality, so a crashed run, a manual database edit or a second
worker cannot leave the limiter believing an account has budget it already
spent.

Only `success` and `failure` rows consume budget. A refusal costs nothing, which
is what lets the gate log every decision without the log eating the caps it is
supposed to measure.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from math import floor
from zoneinfo import ZoneInfo

from linkedin_mcp.audit.log import (
    ATTEMPTED_OUTCOMES,
    AuditLog,
    Outcome,
    count_actions_in_window,
)
from linkedin_mcp.core.config import (
    CONNECTION_ACCEPTED_ACTION,
    GLOBAL_DAILY_CEILING,
    GLOBAL_HOURLY_CEILING,
    INVITE_ACTION,
    JITTER_MAX_SHRINK,
    METERED_ACTIONS,
    PENDING_INVITE_CEILING,
    PENDING_INVITE_WINDOW_DAYS,
    RAMP_UP_DAYS,
    RAMP_UP_START_FRACTION,
    UNMETERED_ACTIONS,
    ceiling_for,
)

logger = logging.getLogger(__name__)

__all__ = [
    "AccountLimit",
    "Budget",
    "DAY",
    "HOUR",
    "JITTER_RESOLUTION",
    "MINUTES_PER_DAY",
    "WEEK",
    "WorkingWindow",
    "account_limit",
    "actions_in_window",
    "daily_budget",
    "daily_jitter_fraction",
    "global_actions_in_window",
    "global_daily_budget",
    "hourly_budget",
    "is_within_working_hours",
    "local_weekday_and_minute",
    "metered_universe",
    "observed_action_types",
    "pending_invite_budget",
    "pending_invites",
    "ramp_up_cap",
    "ramp_up_fraction",
    "resolve_timezone",
    "shrink_for_jitter",
    "weekly_budget",
    "working_windows",
]

HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
WEEK = timedelta(days=7)

MINUTES_PER_DAY = 1440
JITTER_RESOLUTION = 10_000
"""Buckets the per-day jitter is drawn from, so the shrink is a stable fraction."""


@dataclass(frozen=True, slots=True)
class AccountLimit:
    """One account's caps for one action type, already clamped to the ceiling."""

    action_type: str
    daily_cap: int
    weekly_cap: int | None
    enabled: bool


@dataclass(frozen=True, slots=True)
class Budget:
    """What one window allows, what it has already spent, and why.

    `configured` is the cap after clamping the account row against the hard
    ceiling. `after_ramp` applies the warm-up curve for a young account, and
    `cap` applies the day's jitter on top. Keeping all three lets a refusal say
    which constraint actually bound rather than just reporting a number.
    """

    scope: str
    action_type: str | None
    configured: int
    after_ramp: int
    cap: int
    used: int
    jitter_fraction: float = 0.0

    @property
    def remaining(self) -> int:
        return max(0, self.cap - self.used)

    @property
    def exhausted(self) -> bool:
        return self.used >= self.cap

    @property
    def warmup_bound(self) -> bool:
        """True when only the warm-up curve stands between usage and the cap."""
        if self.after_ramp >= self.configured:
            return False
        return self.used < shrink_for_jitter(self.configured, self.jitter_fraction)

    def as_detail(self) -> dict[str, object]:
        """Return the refusal payload describing this budget."""
        detail: dict[str, object] = {
            "limit": self.scope,
            "cap": self.cap,
            "used": self.used,
            "configured_cap": self.configured,
        }
        if self.action_type is not None:
            detail["limited_action"] = self.action_type
        if self.jitter_fraction:
            detail["jitter_fraction"] = round(self.jitter_fraction, 4)
        if self.after_ramp != self.configured:
            detail["warmup_cap"] = self.after_ramp
        return detail


@dataclass(frozen=True, slots=True)
class WorkingWindow:
    """One weekday's open hours as half-open minutes of the local day."""

    weekday: int
    start_minute: int
    end_minute: int

    @property
    def wraps(self) -> bool:
        """True when the window runs past midnight into the next day."""
        return self.end_minute < self.start_minute

    def covers(self, minute: int) -> bool:
        """True when `minute` on this weekday falls inside the window."""
        if self.start_minute == self.end_minute:
            return True
        if self.wraps:
            return minute >= self.start_minute
        return self.start_minute <= minute < self.end_minute

    def covers_after_midnight(self, minute: int) -> bool:
        """True when `minute` on the following day is still inside this window."""
        return self.wraps and minute < self.end_minute


def _exclusive_end(moment: datetime) -> datetime:
    """Return the upper bound that includes the second `moment` falls in.

    `occurred_at` is stored to the second, so a row written moments ago must
    still count while a clock-skewed row dated a later second must not.
    """
    return moment.replace(microsecond=0) + timedelta(seconds=1)


def _count(
    log: AuditLog | None,
    account_id: int,
    action_type: str,
    *,
    since: datetime,
    until: datetime,
    outcomes: Sequence[str],
) -> int:
    if log is None:
        return count_actions_in_window(
            account_id, action_type, since=since, until=until, outcomes=outcomes
        )
    return log.count_in_window(
        account_id, action_type, since=since, until=until, outcomes=outcomes
    )


def actions_in_window(
    account_id: int,
    action_type: str,
    *,
    window: timedelta,
    now: datetime | None = None,
    log: AuditLog | None = None,
    outcomes: Sequence[str] = ATTEMPTED_OUTCOMES,
) -> int:
    """Count attempted actions of one type in the trailing `window`."""
    end = now or datetime.now(timezone.utc)
    return _count(
        log,
        account_id,
        action_type,
        since=end - window,
        until=_exclusive_end(end),
        outcomes=outcomes,
    )


def observed_action_types(
    conn: sqlite3.Connection,
    account_id: int,
) -> frozenset[str]:
    """Return every action type this account has ever logged.

    The global ceilings have to be total. Summing only the action types that
    happen to be configured would let a tool nobody added to `HARD_CEILINGS`
    spend its default fifty a day completely invisibly, and every configured
    action would still see the full hundred and fifty left. That is a hard
    ceiling being exceeded, which is the one thing that must be impossible.

    `DISTINCT action_type` walks the leading columns of
    `idx_actions_log_account_action_time`, so the cost is one seek per distinct
    name rather than a scan of the history.
    """
    rows = conn.execute(
        "SELECT DISTINCT action_type FROM actions_log WHERE account_id = ?",
        (account_id,),
    ).fetchall()
    return frozenset(row["action_type"] for row in rows)


def metered_universe(
    action_type: str | None = None,
    observed: Iterable[str] | None = None,
) -> frozenset[str]:
    """Return the action types the global budgets sum over.

    `HARD_CEILINGS` is the registry of everything this system is configured to
    do, `observed` is everything an account has actually done, and the action
    being gated joins both. Anything in `UNMETERED_ACTIONS` drops out, so the
    set is closed by exclusion: a new action type counts against the global
    ceilings from its first row, whether or not anyone gave it a ceiling.
    """
    universe = set(METERED_ACTIONS)
    if observed is not None:
        universe |= set(observed)
    if action_type is not None:
        universe.add(action_type)
    return frozenset(universe) - UNMETERED_ACTIONS


def global_actions_in_window(
    account_id: int,
    *,
    window: timedelta,
    now: datetime | None = None,
    log: AuditLog | None = None,
    action_types: Iterable[str] | None = None,
) -> int:
    """Count every metered action in the trailing `window`.

    `actions_log` is indexed on `(account_id, action_type, occurred_at)`, so the
    global total is the sum of the per-type counts rather than one unindexed
    scan.
    """
    types = metered_universe() if action_types is None else action_types
    return sum(
        actions_in_window(account_id, action_type, window=window, now=now, log=log)
        for action_type in sorted(set(types))
    )


def _day_key(day: date | datetime | str) -> str:
    if isinstance(day, str):
        return day[:10]
    if isinstance(day, datetime):
        return day.date().isoformat()
    return day.isoformat()


def daily_jitter_fraction(account_id: int, day: date | datetime | str) -> float:
    """Return the deterministic cap shrink for one account on one day.

    A cap that is exactly the same every single day is a signature. Shrinking it
    by nought to ten percent hides that edge, and deriving the slice from a hash
    of the account and the date keeps it stable: the same day always yields the
    same cap, so an account never gains budget by retrying and a restart never
    changes today's answer. `hashlib` rather than `hash()`, because the built-in
    is salted per process and would hand out a different cap after a restart.
    """
    seed = f"{account_id}:{_day_key(day)}".encode()
    digest = hashlib.blake2b(seed, digest_size=8).digest()
    bucket = int.from_bytes(digest, "big") % (JITTER_RESOLUTION + 1)
    return JITTER_MAX_SHRINK * bucket / JITTER_RESOLUTION


def shrink_for_jitter(cap: int, fraction: float) -> int:
    """Apply a jitter fraction to a cap without ever shrinking it to nothing."""
    if not 0.0 <= fraction < 1.0:
        raise ValueError(f"jitter fraction must be within 0..1, got {fraction}")
    if cap <= 0:
        return 0
    return max(1, floor(cap * (1.0 - fraction)))


def ramp_up_fraction(account_age_days: int) -> float:
    """Return the share of its caps an account of this age has earned.

    Warm-up runs from day one to `RAMP_UP_DAYS`, starting at
    `RAMP_UP_START_FRACTION` of every cap and reaching the full number on the
    last day. Age zero is the schema default for `accounts.account_age_days` and
    means nobody recorded an age, so it reads as an established account rather
    than as a brand new one. Registering a genuinely new account means writing
    `account_age_days = 1` on its first day, which is the difference between an
    account that is being warmed up on purpose and one whose age was never
    filled in.
    """
    age = int(account_age_days)
    if age < 0:
        raise ValueError(f"account_age_days must be >= 0, got {account_age_days}")
    if age == 0 or age >= RAMP_UP_DAYS:
        return 1.0
    span = RAMP_UP_DAYS - 1
    return RAMP_UP_START_FRACTION + (1.0 - RAMP_UP_START_FRACTION) * ((age - 1) / span)


def ramp_up_cap(cap: int, account_age_days: int) -> int:
    """Scale a cap down for an account that is still warming up."""
    if cap <= 0:
        return 0
    fraction = ramp_up_fraction(account_age_days)
    if fraction >= 1.0:
        return cap
    return max(1, floor(cap * fraction))


def account_limit(
    conn: sqlite3.Connection,
    account_id: int,
    action_type: str,
) -> AccountLimit:
    """Return the account's caps for an action, clamped to the hard ceiling.

    An account with no row for the action runs at the ceiling. A row may only
    tighten it: a database asking for a thousand invites a day still gets thirty.
    """
    ceiling = ceiling_for(action_type)
    row = conn.execute(
        """
        SELECT daily_cap, weekly_cap, enabled
        FROM account_limits
        WHERE account_id = ? AND action_type = ?
        """,
        (account_id, action_type),
    ).fetchone()
    if row is None:
        return AccountLimit(action_type, ceiling.daily, ceiling.weekly, True)
    return AccountLimit(
        action_type,
        ceiling.clamp_daily(row["daily_cap"]),
        ceiling.clamp_weekly(row["weekly_cap"]),
        bool(row["enabled"]),
    )


def daily_budget(
    account_id: int,
    action_type: str,
    *,
    configured_cap: int,
    now: datetime,
    account_age_days: int = RAMP_UP_DAYS,
    log: AuditLog | None = None,
    jitter_fraction: float | None = None,
) -> Budget:
    """Return the 24 hour budget for one action type."""
    fraction = (
        daily_jitter_fraction(account_id, now)
        if jitter_fraction is None
        else jitter_fraction
    )
    after_ramp = ramp_up_cap(configured_cap, account_age_days)
    return Budget(
        scope="daily",
        action_type=action_type,
        configured=configured_cap,
        after_ramp=after_ramp,
        cap=shrink_for_jitter(after_ramp, fraction),
        used=actions_in_window(account_id, action_type, window=DAY, now=now, log=log),
        jitter_fraction=fraction,
    )


def weekly_budget(
    account_id: int,
    action_type: str,
    *,
    configured_cap: int,
    now: datetime,
    log: AuditLog | None = None,
    jitter_fraction: float | None = None,
) -> Budget:
    """Return the 7 day budget for one action type."""
    fraction = (
        daily_jitter_fraction(account_id, now)
        if jitter_fraction is None
        else jitter_fraction
    )
    return Budget(
        scope="weekly",
        action_type=action_type,
        configured=configured_cap,
        after_ramp=configured_cap,
        cap=shrink_for_jitter(configured_cap, fraction),
        used=actions_in_window(account_id, action_type, window=WEEK, now=now, log=log),
        jitter_fraction=fraction,
    )


def global_daily_budget(
    account_id: int,
    *,
    now: datetime,
    account_age_days: int = RAMP_UP_DAYS,
    log: AuditLog | None = None,
    ceiling: int = GLOBAL_DAILY_CEILING,
    jitter_fraction: float | None = None,
    action_type: str | None = None,
    action_types: Iterable[str] | None = None,
) -> Budget:
    """Return the 24 hour budget across every metered action type."""
    fraction = (
        daily_jitter_fraction(account_id, now)
        if jitter_fraction is None
        else jitter_fraction
    )
    after_ramp = ramp_up_cap(ceiling, account_age_days)
    universe = metered_universe(action_type) if action_types is None else action_types
    return Budget(
        scope="global_daily",
        action_type=None,
        configured=ceiling,
        after_ramp=after_ramp,
        cap=shrink_for_jitter(after_ramp, fraction),
        used=global_actions_in_window(
            account_id,
            window=DAY,
            now=now,
            log=log,
            action_types=universe,
        ),
        jitter_fraction=fraction,
    )


def hourly_budget(
    account_id: int,
    *,
    now: datetime,
    log: AuditLog | None = None,
    ceiling: int = GLOBAL_HOURLY_CEILING,
    action_type: str | None = None,
    action_types: Iterable[str] | None = None,
) -> Budget:
    """Return the rolling hour budget across every metered action type.

    The hourly ceiling is a burst guard, so it is deliberately un-jittered. It
    exists to stop fifty actions landing in five minutes, not to disguise a
    daily pattern.
    """
    universe = metered_universe(action_type) if action_types is None else action_types
    return Budget(
        scope="hourly",
        action_type=None,
        configured=ceiling,
        after_ramp=ceiling,
        cap=ceiling,
        used=global_actions_in_window(
            account_id,
            window=HOUR,
            now=now,
            log=log,
            action_types=universe,
        ),
    )


def pending_invites(
    account_id: int,
    *,
    now: datetime,
    log: AuditLog | None = None,
    window_days: int = PENDING_INVITE_WINDOW_DAYS,
) -> int:
    """Estimate how many invitations are still waiting for an answer.

    LinkedIn does not tell us which invitations are outstanding, so the estimate
    is invitations sent minus acceptances observed inside the same window.
    Nothing writes `connection_accepted` rows yet, which makes this an
    over-count rather than an under-count, and over-counting is the safe
    direction for a ceiling whose job is to stop a backlog building.
    """
    window = timedelta(days=window_days)
    sent = actions_in_window(account_id, INVITE_ACTION, window=window, now=now, log=log)
    accepted = actions_in_window(
        account_id,
        CONNECTION_ACCEPTED_ACTION,
        window=window,
        now=now,
        log=log,
        outcomes=(Outcome.SUCCESS.value,),
    )
    return max(0, sent - accepted)


def pending_invite_budget(
    account_id: int,
    *,
    now: datetime,
    log: AuditLog | None = None,
    ceiling: int = PENDING_INVITE_CEILING,
    window_days: int = PENDING_INVITE_WINDOW_DAYS,
) -> Budget:
    """Return the outstanding invitation budget."""
    return Budget(
        scope="pending_invites",
        action_type=INVITE_ACTION,
        configured=ceiling,
        after_ramp=ceiling,
        cap=ceiling,
        used=pending_invites(account_id, now=now, log=log, window_days=window_days),
    )


def resolve_timezone(name: str | None) -> tzinfo:
    """Return the account's timezone, falling back to UTC rather than failing.

    `zoneinfo` needs a system tz database or the `tzdata` package, and neither
    is guaranteed on Windows. A missing tz database must not take the safety
    gate down, so an unresolvable name degrades to UTC with a warning.
    """
    cleaned = (name or "").strip()
    if not cleaned or cleaned.upper() == "UTC":
        return timezone.utc
    try:
        return ZoneInfo(cleaned)
    except Exception:
        logger.warning("Unknown account timezone %r; falling back to UTC", cleaned)
        return timezone.utc


def local_weekday_and_minute(moment: datetime, zone: tzinfo) -> tuple[int, int]:
    """Return the local weekday (Monday is 0) and minute of the local day."""
    aware = moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)
    local = aware.astimezone(zone)
    return local.weekday(), local.hour * 60 + local.minute


def working_windows(
    conn: sqlite3.Connection,
    account_id: int,
) -> tuple[WorkingWindow, ...]:
    """Return every configured working window for an account."""
    rows = conn.execute(
        """
        SELECT weekday, start_minute, end_minute
        FROM working_hours
        WHERE account_id = ?
        ORDER BY weekday, start_minute
        """,
        (account_id,),
    ).fetchall()
    return tuple(
        WorkingWindow(row["weekday"], row["start_minute"], row["end_minute"])
        for row in rows
    )


def is_within_working_hours(
    windows: Sequence[WorkingWindow],
    weekday: int,
    minute: int,
) -> bool:
    """Return True when the local clock is inside a configured window.

    An account with no schedule at all is always open, because a fresh install
    that silently refused everything would be worse than useless. Once any row
    exists the schedule is authoritative, so a weekday with no row is a day off.
    A window whose end is before its start runs past midnight, and a window
    whose start equals its end covers the whole day.
    """
    if not windows:
        return True
    yesterday = (weekday - 1) % 7
    for window in windows:
        if window.weekday == weekday and window.covers(minute):
            return True
        if window.weekday == yesterday and window.covers_after_midnight(minute):
            return True
    return False
