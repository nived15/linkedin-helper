"""Append-only audit log for every LinkedIn interaction.

`actions_log` is both the audit trail and the sole input to every rate-limit
calculation, so this module only ever inserts. There is no update or delete
path, and limiter arithmetic is a `COUNT(*)` over this table rather than a
stored counter.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from linkedin_mcp.core.db import DEFAULT_DB_PATH, initialize_database

__all__ = [
    "ATTEMPTED_OUTCOMES",
    "AUDIT_TABLE",
    "AuditLog",
    "DEFAULT_ACCOUNT_LABEL",
    "DEFAULT_ACCOUNT_TIMEZONE",
    "MAX_DETAIL_VALUE_LENGTH",
    "Outcome",
    "RefusalReason",
    "ROLLING_WINDOW_INDEX",
    "TIMESTAMP_FORMAT",
    "count_actions_in_window",
    "get_audit_log",
    "log_action",
    "log_refusal",
    "reset_audit_log",
    "set_audit_log",
    "utc_timestamp",
]

AUDIT_TABLE = "actions_log"
ROLLING_WINDOW_INDEX = "idx_actions_log_account_action_time"
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"
DEFAULT_ACCOUNT_LABEL = "linkedin"
DEFAULT_ACCOUNT_TIMEZONE = "UTC"
MAX_DETAIL_VALUE_LENGTH = 500
SENSITIVE_DETAIL_KEYS = frozenset(
    {"password", "passwd", "secret", "token", "api_key", "cookie", "cookies"}
)


class Outcome(str, Enum):
    """Terminal state of an attempted LinkedIn action."""

    SUCCESS = "success"
    FAILURE = "failure"
    REFUSED = "refused"
    SKIPPED = "skipped"


class RefusalReason(str, Enum):
    """Typed reason a `SafetyGate` refused to let an action run."""

    DAILY_CAP_REACHED = "daily_cap_reached"
    WEEKLY_CAP_REACHED = "weekly_cap_reached"
    HOURLY_CAP_REACHED = "hourly_cap_reached"
    OUTSIDE_WORKING_HOURS = "outside_working_hours"
    ACCOUNT_PAUSED = "account_paused"
    ACCOUNT_COOLDOWN = "account_cooldown"
    ACCOUNT_CHALLENGED = "account_challenged"
    ACCOUNT_LOGGED_OUT = "account_logged_out"
    ACTION_DISABLED = "action_disabled"
    LEAD_BLACKLISTED = "lead_blacklisted"
    DUPLICATE_ACTION = "duplicate_action"
    APPROVAL_REQUIRED = "approval_required"
    WARMUP_LIMIT = "warmup_limit"


ATTEMPTED_OUTCOMES: tuple[str, ...] = (Outcome.SUCCESS.value, Outcome.FAILURE.value)
"""Outcomes that actually reached LinkedIn and therefore count against caps."""


def utc_timestamp(moment: datetime | None = None) -> str:
    """Return a UTC timestamp string comparable with SQLite `CURRENT_TIMESTAMP`."""
    if moment is None:
        moment = datetime.now(timezone.utc)
    elif moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc)
    return moment.strftime(TIMESTAMP_FORMAT)


def _window_bound(moment: datetime | str) -> str:
    """Quantize a window bound to the second granularity the schema stores.

    Every bound uses this one rule, so adjacent half-open windows sharing a
    boundary partition the log exactly: no row is counted twice or missed.
    """
    return moment if isinstance(moment, str) else utc_timestamp(moment)


def _coerce_outcome(outcome: Outcome | str) -> str:
    if isinstance(outcome, Outcome):
        return outcome.value
    try:
        return Outcome(outcome).value
    except ValueError:
        raise ValueError(
            f"Unknown outcome {outcome!r}; expected one of "
            f"{sorted(member.value for member in Outcome)}"
        ) from None


def _coerce_refusal_reason(reason: RefusalReason | str | None) -> str:
    if isinstance(reason, RefusalReason):
        return reason.value
    if reason is None:
        raise ValueError(
            "Refused rows require a typed reason; expected one of "
            f"{sorted(member.value for member in RefusalReason)}"
        )
    try:
        return RefusalReason(reason).value
    except ValueError:
        raise ValueError(
            f"Unknown refusal reason {reason!r}; expected one of "
            f"{sorted(member.value for member in RefusalReason)}"
        ) from None


def _redact(key: str, value: Any) -> Any:
    if key.lower() in SENSITIVE_DETAIL_KEYS:
        return "***" if value is not None else None
    if isinstance(value, str) and len(value) > MAX_DETAIL_VALUE_LENGTH:
        return value[:MAX_DETAIL_VALUE_LENGTH] + "..."
    return value


def _encode_detail(detail: Mapping[str, Any] | None) -> str:
    if not detail:
        return "{}"
    redacted = {str(key): _redact(str(key), value) for key, value in detail.items()}
    return json.dumps(redacted, default=str, sort_keys=True)


def _require_typed_reason(detail: Mapping[str, Any] | None) -> str:
    return _coerce_refusal_reason((detail or {}).get("reason"))


def _normalize_outcomes(outcomes: Iterable[str] | None) -> list[str]:
    if not outcomes:
        return []
    return [_coerce_outcome(outcome) for outcome in outcomes]


class AuditLog:
    """Append-only writer and reader for `actions_log`.

    Every method either inserts a row or counts rows. Nothing here updates or
    deletes audit history.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    @classmethod
    def open(cls, db_path: str | Path = DEFAULT_DB_PATH) -> AuditLog:
        """Open the database, apply migrations, and return a log bound to it."""
        return cls(initialize_database(db_path))

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    def close(self) -> None:
        self._conn.close()

    def ensure_account(
        self,
        label: str = DEFAULT_ACCOUNT_LABEL,
        *,
        timezone_name: str = DEFAULT_ACCOUNT_TIMEZONE,
    ) -> int:
        """Return the id of the account with `label`, creating it when absent.

        `accounts.label` carries no unique constraint, so the insert runs inside
        an immediate transaction and re-checks under the write lock. Two workers
        racing on the same label cannot split one ledger into two account ids.
        """
        existing = self._select_account_id(label)
        if existing is not None:
            return existing

        self._conn.execute("BEGIN IMMEDIATE")
        try:
            account_id = self._select_account_id(label)
            if account_id is None:
                cursor = self._conn.execute(
                    """
                    INSERT INTO accounts (label, timezone, state)
                    VALUES (?, ?, 'active')
                    """,
                    (label, timezone_name),
                )
                account_id = int(cursor.lastrowid)
        except Exception:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()
        return account_id

    def _select_account_id(self, label: str) -> int | None:
        row = self._conn.execute(
            "SELECT id FROM accounts WHERE label = ?", (label,)
        ).fetchone()
        return None if row is None else int(row["id"])

    def record(
        self,
        account_id: int,
        action_type: str,
        outcome: Outcome | str,
        *,
        lead_id: int | None = None,
        campaign_id: int | None = None,
        step_id: int | None = None,
        detail: Mapping[str, Any] | None = None,
        occurred_at: datetime | str | None = None,
    ) -> int:
        """Append one row to `actions_log` and return its id."""
        if not action_type:
            raise ValueError("action_type is required for an audit row")

        resolved_outcome = _coerce_outcome(outcome)
        if resolved_outcome == Outcome.REFUSED.value:
            _require_typed_reason(detail)

        timestamp = (
            occurred_at if isinstance(occurred_at, str) else utc_timestamp(occurred_at)
        )
        cursor = self._conn.execute(
            """
            INSERT INTO actions_log (
                account_id, lead_id, campaign_id, step_id,
                action_type, outcome, detail_json, occurred_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                account_id,
                lead_id,
                campaign_id,
                step_id,
                action_type,
                resolved_outcome,
                _encode_detail(detail),
                timestamp,
            ),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    def record_refusal(
        self,
        account_id: int,
        action_type: str,
        reason: RefusalReason | str | None,
        *,
        lead_id: int | None = None,
        campaign_id: int | None = None,
        step_id: int | None = None,
        detail: Mapping[str, Any] | None = None,
        occurred_at: datetime | str | None = None,
    ) -> int:
        """Append a refused row carrying the typed `SafetyGate` reason."""
        payload: dict[str, Any] = dict(detail or {})
        payload["reason"] = _coerce_refusal_reason(reason)
        return self.record(
            account_id,
            action_type,
            Outcome.REFUSED,
            lead_id=lead_id,
            campaign_id=campaign_id,
            step_id=step_id,
            detail=payload,
            occurred_at=occurred_at,
        )

    def count_in_window(
        self,
        account_id: int,
        action_type: str,
        *,
        since: datetime | str,
        until: datetime | str | None = None,
        outcomes: Sequence[str] = ATTEMPTED_OUTCOMES,
    ) -> int:
        """Count logged actions in `[since, until)` straight from `actions_log`.

        The `account_id = ? AND action_type = ? AND occurred_at >= ?` prefix is
        exactly the leading edge of `idx_actions_log_account_action_time`.
        """
        sql, params = self._window_query(
            account_id, action_type, since=since, until=until, outcomes=outcomes
        )
        return int(self._conn.execute(sql, params).fetchone()[0])

    def count_in_rolling_window(
        self,
        account_id: int,
        action_type: str,
        *,
        window: timedelta,
        now: datetime | None = None,
        outcomes: Sequence[str] = ATTEMPTED_OUTCOMES,
    ) -> int:
        """Count actions in the trailing `window` ending at `now`.

        `occurred_at` is stored at second granularity, so the window covers
        `[now - window, now]` inclusive of the second `now` falls in. A row
        written moments ago still counts, while a clock skewed row dated a later
        second cannot inflate the total.
        """
        end = now or datetime.now(timezone.utc)
        return self.count_in_window(
            account_id,
            action_type,
            since=end - window,
            until=end.replace(microsecond=0) + timedelta(seconds=1),
            outcomes=outcomes,
        )

    def window_query_plan(
        self,
        account_id: int,
        action_type: str,
        *,
        since: datetime | str,
        until: datetime | str | None = None,
        outcomes: Sequence[str] = ATTEMPTED_OUTCOMES,
    ) -> list[str]:
        """Return the SQLite query plan rows for the rolling-window count."""
        sql, params = self._window_query(
            account_id, action_type, since=since, until=until, outcomes=outcomes
        )
        rows = self._conn.execute(f"EXPLAIN QUERY PLAN {sql}", params).fetchall()
        return [row["detail"] for row in rows]

    def entries(
        self,
        account_id: int,
        *,
        action_type: str | None = None,
        limit: int = 50,
    ) -> list[sqlite3.Row]:
        """Return the most recent audit rows for an account, newest first."""
        sql = "SELECT * FROM actions_log WHERE account_id = ?"
        params: list[Any] = [account_id]
        if action_type is not None:
            sql += " AND action_type = ?"
            params.append(action_type)
        sql += " ORDER BY occurred_at DESC, id DESC LIMIT ?"
        params.append(limit)
        return list(self._conn.execute(sql, params).fetchall())

    def refusals(
        self,
        account_id: int,
        *,
        since: datetime | str | None = None,
        limit: int = 50,
    ) -> list[sqlite3.Row]:
        """Return recent refusals so a quiet night is explainable."""
        sql = "SELECT * FROM actions_log WHERE account_id = ? AND outcome = ?"
        params: list[Any] = [account_id, Outcome.REFUSED.value]
        if since is not None:
            sql += " AND occurred_at >= ?"
            params.append(_window_bound(since))
        sql += " ORDER BY occurred_at DESC, id DESC LIMIT ?"
        params.append(limit)
        return list(self._conn.execute(sql, params).fetchall())

    @staticmethod
    def _window_query(
        account_id: int,
        action_type: str,
        *,
        since: datetime | str,
        until: datetime | str | None,
        outcomes: Sequence[str],
    ) -> tuple[str, list[Any]]:
        sql = (
            "SELECT COUNT(*) FROM actions_log "
            "WHERE account_id = ? AND action_type = ? AND occurred_at >= ?"
        )
        params: list[Any] = [account_id, action_type, _window_bound(since)]
        if until is not None:
            sql += " AND occurred_at < ?"
            params.append(_window_bound(until))
        normalized = _normalize_outcomes(outcomes)
        if normalized:
            placeholders = ", ".join("?" for _ in normalized)
            sql += f" AND outcome IN ({placeholders})"
            params.extend(normalized)
        return sql, params


_default_log: AuditLog | None = None


def get_audit_log(db_path: str | Path | None = None) -> AuditLog:
    """Return the process-wide audit log, opening it on first use."""
    global _default_log
    if _default_log is None:
        _default_log = AuditLog.open(db_path or DEFAULT_DB_PATH)
    return _default_log


def set_audit_log(log: AuditLog | None) -> None:
    """Replace the process-wide audit log, primarily for tests."""
    global _default_log
    _default_log = log


def reset_audit_log() -> None:
    """Drop the cached audit log so the next call reopens it."""
    set_audit_log(None)


def log_action(
    account_id: int,
    action_type: str,
    outcome: Outcome | str,
    *,
    lead_id: int | None = None,
    campaign_id: int | None = None,
    step_id: int | None = None,
    detail: Mapping[str, Any] | None = None,
    occurred_at: datetime | str | None = None,
) -> int:
    """Append an audit row using the process-wide audit log."""
    return get_audit_log().record(
        account_id,
        action_type,
        outcome,
        lead_id=lead_id,
        campaign_id=campaign_id,
        step_id=step_id,
        detail=detail,
        occurred_at=occurred_at,
    )


def log_refusal(
    account_id: int,
    action_type: str,
    reason: RefusalReason | str,
    lead_id: int | None = None,
    *,
    campaign_id: int | None = None,
    step_id: int | None = None,
    detail: Mapping[str, Any] | None = None,
    occurred_at: datetime | str | None = None,
) -> int:
    """Log a `SafetyGate` refusal with its typed reason.

    CORE-03 calls this whenever it declines to run an action, so an idle night
    is explained by rows in `actions_log` rather than by silence. A gate that
    logs its own refusal owns that row and should return `audit_logged: True`
    from the tool so the instrumentation decorator does not append a duplicate.
    """
    return get_audit_log().record_refusal(
        account_id,
        action_type,
        reason,
        lead_id=lead_id,
        campaign_id=campaign_id,
        step_id=step_id,
        detail=detail,
        occurred_at=occurred_at,
    )


def count_actions_in_window(
    account_id: int,
    action_type: str,
    *,
    since: datetime | str,
    until: datetime | str | None = None,
    outcomes: Sequence[str] = ATTEMPTED_OUTCOMES,
) -> int:
    """Count actions in a window using the process-wide audit log."""
    return get_audit_log().count_in_window(
        account_id,
        action_type,
        since=since,
        until=until,
        outcomes=outcomes,
    )
