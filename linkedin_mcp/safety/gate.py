"""The one place an automated LinkedIn action is allowed to start.

`SafetyGate.acquire` answers a single question: may this account take this
action against this lead right now. It answers it from the database, in one read
transaction, before any browser is opened, and it answers the same way whether
the caller is a scheduled worker or a language model improvising through the MCP
tools. No cap in here can be raised by a prompt, a tool argument or a database
row: the ceilings in `linkedin_mcp.core.config` are the last word.

Every refusal is a typed exception carrying one of the `RefusalReason` values
the audit package already defines, and every refusal is written to `actions_log`
before it is raised. A quiet night is therefore explainable: the log says which
constraint bound and by how much.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, ClassVar

from linkedin_mcp.audit.instrument import current_account_id
from linkedin_mcp.audit.log import (
    ATTEMPTED_OUTCOMES,
    AuditLog,
    RefusalReason,
    get_audit_log,
    log_refusal,
    utc_timestamp,
)
from linkedin_mcp.core.config import (
    APPROVAL_REQUIRED_ACTIONS,
    INVITE_ACTION,
    dedupe_window_days,
    is_metered,
)
from linkedin_mcp.leads.blacklist import is_blacklisted
from linkedin_mcp.safety.limits import (
    Budget,
    account_limit,
    daily_budget,
    daily_jitter_fraction,
    global_daily_budget,
    hourly_budget,
    is_within_working_hours,
    local_weekday_and_minute,
    metered_universe,
    observed_action_types,
    pending_invite_budget,
    resolve_timezone,
    weekly_budget,
    working_windows,
)

logger = logging.getLogger(__name__)

__all__ = [
    "AccountChallenged",
    "AccountCooldown",
    "AccountLoggedOut",
    "AccountPaused",
    "AccountSnapshot",
    "ActionDisabled",
    "ApprovalRequired",
    "Blacklisted",
    "DailyCapReached",
    "DuplicateAction",
    "HourlyCapReached",
    "Lease",
    "PendingInviteCeilingReached",
    "SAFETY_EVENT_KIND",
    "SAFETY_EVENT_REASONS",
    "SafetyError",
    "SafetyGate",
    "SafetyRefusal",
    "UnknownAccountError",
    "WarmupLimit",
    "WeeklyCapReached",
    "WorkingHoursClosed",
    "acquire",
    "get_gate",
    "guard_action",
    "reset_gate",
    "set_gate",
]

ACTIVE_STATE = "active"
SAFETY_EVENT_KIND = "gate_refusal"


class SafetyError(Exception):
    """Base class for everything `linkedin_mcp.safety` raises."""


class UnknownAccountError(SafetyError):
    """Raised when the gate is asked about an account that does not exist."""

    def __init__(self, account_id: int) -> None:
        self.account_id = account_id
        super().__init__(f"account {account_id} does not exist")


class SafetyRefusal(SafetyError):
    """A typed refusal, already recorded in `actions_log`.

    Subclasses bind themselves to one existing `RefusalReason`. The enum stays
    the single vocabulary for why an action did not run, so a refusal raised
    here, a row in `actions_log` and a report built from that log all use the
    same word.
    """

    reason: ClassVar[RefusalReason | None] = None
    headline: ClassVar[str] = "refused by the safety gate"
    severity: ClassVar[str] = "warning"

    def __init__(
        self,
        account_id: int,
        action_type: str,
        *,
        lead_id: int | None = None,
        message: str | None = None,
        **detail: Any,
    ) -> None:
        if self.reason is None:
            raise TypeError(
                "SafetyRefusal is abstract; raise one of its typed subclasses"
            )
        self.account_id = account_id
        self.action_type = action_type
        self.lead_id = lead_id
        self.detail: dict[str, Any] = dict(detail)
        self.logged = False
        super().__init__(message or self._default_message())

    def _default_message(self) -> str:
        target = "" if self.lead_id is None else f" for lead {self.lead_id}"
        return f"{self.action_type}{target} refused: {self.headline}"

    def audit_detail(self) -> dict[str, Any]:
        """Return the `detail_json` payload stored with the refusal row."""
        payload = dict(self.detail)
        payload["action_type"] = self.action_type
        return payload

    def to_result(self) -> dict[str, Any]:
        """Return the MCP tool result for this refusal.

        `audit_logged` reports whether the refusal row actually landed, so
        `audit_linkedin_action` skips a decision that is already recorded and
        still records one whose write failed.
        """
        payload = dict(self.detail)
        payload.update(
            {
                "status": "refused",
                "reason": self.reason.value,
                "message": str(self),
                "action_type": self.action_type,
                "account_id": self.account_id,
                "audit_logged": self.logged,
            }
        )
        if self.lead_id is not None:
            payload["lead_id"] = self.lead_id
        return payload

    @classmethod
    def for_budget(
        cls,
        budget: Budget,
        account_id: int,
        action_type: str,
        lead_id: int | None = None,
    ) -> SafetyRefusal:
        """Build a refusal that carries the budget which ran out."""
        return cls(account_id, action_type, lead_id=lead_id, **budget.as_detail())


class DailyCapReached(SafetyRefusal):
    reason = RefusalReason.DAILY_CAP_REACHED
    headline = "the rolling 24 hour cap is spent"


class WeeklyCapReached(SafetyRefusal):
    reason = RefusalReason.WEEKLY_CAP_REACHED
    headline = "the rolling 7 day cap is spent"


class PendingInviteCeilingReached(WeeklyCapReached):
    """Too many invitations are still waiting for an answer.

    A backlog of unanswered invitations is the multi-day invite signal LinkedIn
    reacts to, so it maps onto `WEEKLY_CAP_REACHED` and names itself through the
    `limit: pending_invites` detail rather than inventing a second vocabulary.
    """

    headline = "too many invitations are still pending"


class HourlyCapReached(SafetyRefusal):
    reason = RefusalReason.HOURLY_CAP_REACHED
    headline = "the rolling hour burst cap is spent"


class WorkingHoursClosed(SafetyRefusal):
    reason = RefusalReason.OUTSIDE_WORKING_HOURS
    headline = "the account is outside its working hours"


class AccountPaused(SafetyRefusal):
    reason = RefusalReason.ACCOUNT_PAUSED
    headline = "the account is paused"


class AccountCooldown(SafetyRefusal):
    reason = RefusalReason.ACCOUNT_COOLDOWN
    headline = "the account is cooling down"


class AccountChallenged(SafetyRefusal):
    reason = RefusalReason.ACCOUNT_CHALLENGED
    headline = "LinkedIn has challenged the account"
    severity = "critical"


class AccountLoggedOut(SafetyRefusal):
    reason = RefusalReason.ACCOUNT_LOGGED_OUT
    headline = "the account is logged out"


class WarmupLimit(SafetyRefusal):
    reason = RefusalReason.WARMUP_LIMIT
    headline = "the account is still warming up"


class ActionDisabled(SafetyRefusal):
    reason = RefusalReason.ACTION_DISABLED
    headline = "the action is disabled for this account"
    severity = "info"


class Blacklisted(SafetyRefusal):
    reason = RefusalReason.LEAD_BLACKLISTED
    headline = "the lead is on the do-not-contact list"
    severity = "info"


class DuplicateAction(SafetyRefusal):
    reason = RefusalReason.DUPLICATE_ACTION
    headline = "the same action already ran for this lead"
    severity = "info"


class ApprovalRequired(SafetyRefusal):
    reason = RefusalReason.APPROVAL_REQUIRED
    headline = "a human has not approved this action"
    severity = "info"


SAFETY_EVENT_REASONS: frozenset[RefusalReason] = frozenset(
    {
        RefusalReason.DAILY_CAP_REACHED,
        RefusalReason.WEEKLY_CAP_REACHED,
        RefusalReason.HOURLY_CAP_REACHED,
        RefusalReason.WARMUP_LIMIT,
        RefusalReason.OUTSIDE_WORKING_HOURS,
        RefusalReason.ACCOUNT_PAUSED,
        RefusalReason.ACCOUNT_COOLDOWN,
        RefusalReason.ACCOUNT_CHALLENGED,
        RefusalReason.ACCOUNT_LOGGED_OUT,
    }
)
"""Refusals that also raise a `safety_events` alert.

These say something about the account as a whole and belong on the safety
timeline. Per-lead routing decisions such as a blacklist hit or a duplicate are
recorded in `actions_log` only, because a campaign can produce thousands of them
in a run and drowning the timeline would hide the alerts that matter.
"""

STATE_REFUSALS: Mapping[str, type[SafetyRefusal]] = {
    "paused": AccountPaused,
    "cooldown": AccountCooldown,
    "challenged": AccountChallenged,
    "logged_out": AccountLoggedOut,
}


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    """The account fields the gate reads, taken from one consistent snapshot."""

    id: int
    state: str
    timezone: str
    account_age_days: int


@dataclass(frozen=True, slots=True)
class Lease:
    """Permission to run one action, with the budgets that allowed it.

    The lease is advisory rather than a reservation. `actions_log` stays the
    only ledger, so the action is not counted until the caller logs it, and a
    lease that is never used costs nothing.
    """

    account_id: int
    action_type: str
    lead_id: int | None
    granted_at: datetime
    budgets: tuple[Budget, ...] = ()

    def budget(self, scope: str) -> Budget | None:
        """Return the budget checked for one scope, if it was checked."""
        for budget in self.budgets:
            if budget.scope == scope:
                return budget
        return None

    def remaining(self, scope: str) -> int | None:
        """Return what is left in one scope after this action runs."""
        budget = self.budget(scope)
        return None if budget is None else max(0, budget.remaining - 1)

    def to_result(self) -> dict[str, Any]:
        """Return a summary suitable for an MCP tool payload."""
        return {
            "account_id": self.account_id,
            "action_type": self.action_type,
            "lead_id": self.lead_id,
            "granted_at": utc_timestamp(self.granted_at),
            "remaining_today": self.remaining("daily"),
            "remaining_this_week": self.remaining("weekly"),
        }


@contextmanager
def _snapshot(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Read every safety input from one consistent database snapshot.

    Counting usage, reading the account state and checking the blacklist in
    separate reads would let a concurrent write land between them and produce a
    decision that was never true at any single instant. One deferred transaction
    pins them together, and because the gate only reads there is nothing to
    commit.

    A transaction already in flight is refused rather than joined. Joining one
    would mean deciding against a half-written picture, and the commits the
    refusal path performs afterwards would silently commit the other writer's
    work. Failing here is loud, and `guard_action` turns it into a refusal to
    run rather than a permission to.
    """
    if conn.in_transaction:
        raise SafetyError(
            "the safety gate needs a connection with no transaction in flight; "
            "another writer left one open"
        )
    conn.execute("BEGIN")
    try:
        yield conn
    finally:
        conn.rollback()


class SafetyGate:
    """Runs every safety check and hands back a lease or a typed refusal."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        jitter: Callable[[int, datetime], float] = daily_jitter_fraction,
    ) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._jitter = jitter

    def acquire(
        self,
        account_id: int,
        action_type: str,
        lead_id: int | None = None,
        *,
        now: datetime | None = None,
        approved: bool | None = None,
    ) -> Lease:
        """Return a lease, or raise the typed refusal explaining the no.

        Args:
            account_id: Account the action would run as.
            action_type: Audit action name, which is also the budget it spends.
            lead_id: Lead the action targets, when it targets one. Required for
                the blacklist and duplicate checks to mean anything.
            now: Decision time, defaulting to the gate's clock.
            approved: Whether a human signed this action off. Actions listed in
                `config.APPROVAL_REQUIRED_ACTIONS` refuse without an explicit
                True.
        """
        moment = now or self._clock()
        log = get_audit_log()
        with _snapshot(log.connection) as conn:
            refusal, budgets = self._evaluate(
                conn, log, account_id, action_type, lead_id, moment, approved
            )

        if refusal is not None:
            self._record_refusal(log, refusal, moment)
            raise refusal
        return Lease(account_id, action_type, lead_id, moment, tuple(budgets))

    def _evaluate(
        self,
        conn: sqlite3.Connection,
        log: AuditLog,
        account_id: int,
        action_type: str,
        lead_id: int | None,
        moment: datetime,
        approved: bool | None,
    ) -> tuple[SafetyRefusal | None, list[Budget]]:
        budgets: list[Budget] = []
        if not is_metered(action_type):
            return None, budgets

        account = self._load_account(conn, account_id)
        if account.state != ACTIVE_STATE:
            refusal_class = STATE_REFUSALS.get(account.state, AccountPaused)
            return (
                refusal_class(
                    account_id, action_type, lead_id=lead_id, account_state=account.state
                ),
                budgets,
            )

        limit = account_limit(conn, account_id, action_type)
        if not limit.enabled:
            return ActionDisabled(account_id, action_type, lead_id=lead_id), budgets

        if lead_id is not None:
            unresolved = self._unresolved_lead_refusal(
                conn, account_id, action_type, lead_id
            )
            if unresolved is not None:
                return unresolved, budgets
            if is_blacklisted(conn, account_id, lead_id):
                return Blacklisted(account_id, action_type, lead_id=lead_id), budgets

        duplicate = self._find_duplicate(conn, account_id, action_type, lead_id, moment)
        if duplicate is not None:
            return (
                DuplicateAction(
                    account_id,
                    action_type,
                    lead_id=lead_id,
                    previous_action_id=duplicate["id"],
                    previous_occurred_at=duplicate["occurred_at"],
                ),
                budgets,
            )

        if action_type in APPROVAL_REQUIRED_ACTIONS and approved is not True:
            return ApprovalRequired(account_id, action_type, lead_id=lead_id), budgets

        zone = resolve_timezone(account.timezone)
        weekday, minute = local_weekday_and_minute(moment, zone)
        if not is_within_working_hours(working_windows(conn, account_id), weekday, minute):
            return (
                WorkingHoursClosed(
                    account_id,
                    action_type,
                    lead_id=lead_id,
                    local_weekday=weekday,
                    local_minute=minute,
                    timezone=account.timezone,
                ),
                budgets,
            )

        jitter_fraction = self._jitter(account_id, moment)
        universe = metered_universe(
            action_type, observed_action_types(conn, account_id)
        )

        hourly = hourly_budget(
            account_id, now=moment, log=log, action_types=universe
        )
        budgets.append(hourly)
        if hourly.exhausted:
            return (
                HourlyCapReached.for_budget(hourly, account_id, action_type, lead_id),
                budgets,
            )

        daily = daily_budget(
            account_id,
            action_type,
            configured_cap=limit.daily_cap,
            now=moment,
            account_age_days=account.account_age_days,
            log=log,
            jitter_fraction=jitter_fraction,
        )
        budgets.append(daily)
        if daily.exhausted:
            return self._cap_refusal(daily, account_id, action_type, lead_id), budgets

        overall = global_daily_budget(
            account_id,
            now=moment,
            account_age_days=account.account_age_days,
            log=log,
            jitter_fraction=jitter_fraction,
            action_types=universe,
        )
        budgets.append(overall)
        if overall.exhausted:
            return self._cap_refusal(overall, account_id, action_type, lead_id), budgets

        if limit.weekly_cap is not None:
            weekly = weekly_budget(
                account_id,
                action_type,
                configured_cap=limit.weekly_cap,
                now=moment,
                log=log,
                jitter_fraction=jitter_fraction,
            )
            budgets.append(weekly)
            if weekly.exhausted:
                return (
                    WeeklyCapReached.for_budget(weekly, account_id, action_type, lead_id),
                    budgets,
                )

        if action_type == INVITE_ACTION:
            pending = pending_invite_budget(account_id, now=moment, log=log)
            budgets.append(pending)
            if pending.exhausted:
                return (
                    PendingInviteCeilingReached.for_budget(
                        pending, account_id, action_type, lead_id
                    ),
                    budgets,
                )

        return None, budgets

    @staticmethod
    def _cap_refusal(
        budget: Budget,
        account_id: int,
        action_type: str,
        lead_id: int | None,
    ) -> SafetyRefusal:
        """Name the constraint that actually bound, not just the window.

        A young account hitting a shrunken cap is warming up, not maxed out, and
        saying so is the difference between waiting a day and raising a limit
        that was never the problem.
        """
        refusal_class = WarmupLimit if budget.warmup_bound else DailyCapReached
        return refusal_class.for_budget(budget, account_id, action_type, lead_id)

    @staticmethod
    def _unresolved_lead_refusal(
        conn: sqlite3.Connection,
        account_id: int,
        action_type: str,
        lead_id: int,
    ) -> SafetyRefusal | None:
        """Refuse a lead id that does not resolve, without naming it on the row.

        `leads.is_blacklisted` already fails closed for a lead it cannot find,
        and this keeps that answer loggable: `actions_log.lead_id` has a foreign
        key, so an id that is not in the lead store has to travel in the detail
        rather than in the column.
        """
        row = conn.execute(
            "SELECT 1 FROM leads WHERE id = ? AND account_id = ?",
            (lead_id, account_id),
        ).fetchone()
        if row is not None:
            return None
        return Blacklisted(
            account_id,
            action_type,
            requested_lead_id=lead_id,
            unresolved_lead=True,
            message=(
                f"{action_type} refused: lead {lead_id} is not in this account's "
                "lead store, so it cannot be cleared against the do-not-contact list"
            ),
        )

    @staticmethod
    def _load_account(conn: sqlite3.Connection, account_id: int) -> AccountSnapshot:
        row = conn.execute(
            "SELECT id, state, timezone, account_age_days FROM accounts WHERE id = ?",
            (account_id,),
        ).fetchone()
        if row is None:
            raise UnknownAccountError(account_id)
        return AccountSnapshot(
            id=int(row["id"]),
            state=str(row["state"]),
            timezone=str(row["timezone"]),
            account_age_days=int(row["account_age_days"]),
        )

    @staticmethod
    def _find_duplicate(
        conn: sqlite3.Connection,
        account_id: int,
        action_type: str,
        lead_id: int | None,
        moment: datetime,
    ) -> sqlite3.Row | None:
        """Return the earlier attempt at this action for this lead, if any.

        The lookup is a single indexed existence check rather than a page
        through recent history. Paging would quietly stop looking after a fixed
        number of rows, and at 100 invitations a week that ceiling arrives well
        inside the ninety day invite window, which would let the same person be
        invited twice with nothing logged to say why.

        A failed attempt still blocks a repeat. We cannot tell an invite that
        failed before LinkedIn saw it from one that failed after it landed, and
        inviting the same person twice is exactly the signal we are trying not
        to send.
        """
        if lead_id is None:
            return None
        days = dedupe_window_days(action_type)
        if not days:
            return None

        placeholders = ", ".join("?" for _ in ATTEMPTED_OUTCOMES)
        return conn.execute(
            f"""
            SELECT id, occurred_at
            FROM actions_log
            WHERE account_id = ?
              AND action_type = ?
              AND occurred_at >= ?
              AND lead_id = ?
              AND outcome IN ({placeholders})
            ORDER BY occurred_at DESC, id DESC
            LIMIT 1
            """,
            (
                account_id,
                action_type,
                utc_timestamp(moment - timedelta(days=days)),
                lead_id,
                *ATTEMPTED_OUTCOMES,
            ),
        ).fetchone()

    @staticmethod
    def _record_refusal(
        log: AuditLog,
        refusal: SafetyRefusal,
        moment: datetime,
    ) -> None:
        """Write the refusal down, but never let that write swallow the refusal.

        A refusal the log could not record is still a refusal: losing the row
        costs an explanation, while losing the decision would let the action
        run. The failure is logged loudly and `refusal.logged` stays False so
        the tool's own instrumentation records the row instead.
        """
        try:
            log_refusal(
                refusal.account_id,
                refusal.action_type,
                refusal.reason,
                lead_id=refusal.lead_id,
                detail=refusal.audit_detail(),
                occurred_at=moment,
            )
            refusal.logged = True
            if refusal.reason in SAFETY_EVENT_REASONS:
                _record_safety_event(log.connection, refusal, moment)
        except Exception as exc:
            if log.connection.in_transaction:
                log.connection.rollback()
            logger.error(
                "Failed to record the %s refusal for %s: %s",
                refusal.reason.value,
                refusal.action_type,
                exc,
            )
            refusal.detail["audit_error"] = str(exc)


def _record_safety_event(
    conn: sqlite3.Connection,
    refusal: SafetyRefusal,
    moment: datetime,
) -> None:
    """Put an account-level refusal on the safety timeline.

    The insert opens a write transaction, so a failure has to roll back rather
    than leave one in flight. A wedged transaction on this shared connection
    would block every later gate call and hold the write lock against other
    processes.
    """
    detail = refusal.audit_detail()
    detail["reason"] = refusal.reason.value
    if refusal.lead_id is not None:
        detail["lead_id"] = refusal.lead_id
    try:
        conn.execute(
            """
            INSERT INTO safety_events (account_id, kind, severity, detail_json, occurred_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                refusal.account_id,
                SAFETY_EVENT_KIND,
                refusal.severity,
                json.dumps(detail, default=str, sort_keys=True),
                utc_timestamp(moment),
            ),
        )
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


_gate: SafetyGate | None = None


def get_gate() -> SafetyGate:
    """Return the process-wide gate, building it on first use."""
    global _gate
    if _gate is None:
        _gate = SafetyGate()
    return _gate


def set_gate(gate: SafetyGate | None) -> None:
    """Replace the process-wide gate, primarily for tests."""
    global _gate
    _gate = gate


def reset_gate() -> None:
    """Drop the cached gate so the next call rebuilds it."""
    set_gate(None)


def acquire(
    account_id: int,
    action_type: str,
    lead_id: int | None = None,
    *,
    now: datetime | None = None,
    approved: bool | None = None,
) -> Lease:
    """Acquire a lease from the process-wide gate."""
    return get_gate().acquire(
        account_id, action_type, lead_id, now=now, approved=approved
    )


def guard_action(
    action_type: str,
    *,
    lead_id: int | None = None,
    account_id: int | None = None,
    approved: bool | None = None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Return the refusal an MCP tool should return, or None to proceed.

    MCP tools never raise, so this is the edge that turns the gate's typed
    exceptions into ordinary tool results. A gate that cannot reach its own
    database refuses too: silently losing the caps would be far worse than a
    loud error, so the failure fails closed.
    """
    try:
        resolved_account = (
            current_account_id() if account_id is None else account_id
        )
        get_gate().acquire(
            resolved_account, action_type, lead_id, now=now, approved=approved
        )
    except SafetyRefusal as refusal:
        logger.info("Safety gate refused %s: %s", action_type, refusal)
        return refusal.to_result()
    except Exception as exc:
        logger.error("Safety gate failed for %s: %s", action_type, exc)
        return {
            "status": "error",
            "message": f"Safety gate unavailable, refusing to run {action_type}: {exc}",
        }
    return None
