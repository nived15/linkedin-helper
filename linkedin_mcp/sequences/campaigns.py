"""Campaign rows: the container a step list and a lead population hang off.

This is deliberately thin. The interesting state lives in `campaign_steps` and
`campaign_leads`; a campaign row carries the account it runs under, its status
and its approval mode, which SEQ-04 (#22) passes to the safety gate so an
unapproved sequence refuses instead of quietly inviting people overnight.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from linkedin_mcp.sequences.errors import CampaignNotFoundError
from linkedin_mcp.sequences.transaction import now_timestamp, transaction

__all__ = [
    "APPROVAL_MODES",
    "CAMPAIGN_STATUSES",
    "RUNNABLE_STATUSES",
    "Campaign",
    "campaign_row",
    "create_campaign",
    "get_campaign",
    "is_runnable",
    "list_campaigns",
    "require_campaign",
    "set_campaign_status",
]

CAMPAIGN_STATUSES: tuple[str, ...] = (
    "draft",
    "pending_approval",
    "active",
    "paused",
    "completed",
    "archived",
)
"""Statuses the `campaigns` CHECK constraint accepts."""

APPROVAL_MODES: tuple[str, ...] = ("auto", "manual_drafts")
"""Approval modes the `campaigns` CHECK constraint accepts."""

RUNNABLE_STATUSES: frozenset[str] = frozenset({"active"})
"""The only status whose jobs a runner may execute.

Everything else still derives jobs, because pausing a campaign must not throw the
queue away. SEQ-04 filters on the campaign status when it leases work.
"""


@dataclass(frozen=True, slots=True)
class Campaign:
    id: int
    account_id: int
    name: str
    status: str
    approval_mode: str
    exclude_list_id: int | None = None
    created_at: str | None = None
    started_at: str | None = None
    paused_at: str | None = None

    @property
    def runnable(self) -> bool:
        return self.status in RUNNABLE_STATUSES


def campaign_row(row: sqlite3.Row) -> Campaign:
    return Campaign(
        id=row["id"],
        account_id=row["account_id"],
        name=row["name"],
        status=row["status"],
        approval_mode=row["approval_mode"],
        exclude_list_id=row["exclude_list_id"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        paused_at=row["paused_at"],
    )


def _validate(status: str, approval_mode: str) -> None:
    if status not in CAMPAIGN_STATUSES:
        raise ValueError(
            f"unknown campaign status {status!r}; expected one of {list(CAMPAIGN_STATUSES)}"
        )
    if approval_mode not in APPROVAL_MODES:
        raise ValueError(
            f"unknown approval mode {approval_mode!r}; expected one of {list(APPROVAL_MODES)}"
        )


def create_campaign(
    conn: sqlite3.Connection,
    account_id: int,
    name: str,
    *,
    status: str = "draft",
    approval_mode: str = "manual_drafts",
    exclude_list_id: int | None = None,
    now: datetime | str | None = None,
) -> Campaign:
    """Create a campaign and return it.

    `exclude_list_id` is read as a `tags.id`. Every lead carrying that tag enrols
    straight into `excluded`, which is how an operator keeps a do-not-contact
    audience out of one campaign without blacklisting the people globally.
    """
    label = (name or "").strip()
    if not label:
        raise ValueError("campaign name is required")
    _validate(status, approval_mode)

    created_at = now_timestamp(now)
    with transaction(conn):
        cursor = conn.execute(
            """
            INSERT INTO campaigns
                (account_id, name, status, approval_mode, exclude_list_id, created_at,
                 started_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                account_id,
                label,
                status,
                approval_mode,
                exclude_list_id,
                created_at,
                created_at if status == "active" else None,
            ),
        )
        campaign_id = int(cursor.lastrowid)
    return require_campaign(conn, campaign_id)


def get_campaign(conn: sqlite3.Connection, campaign_id: int) -> Campaign | None:
    row = conn.execute(
        "SELECT * FROM campaigns WHERE id = ?",
        (campaign_id,),
    ).fetchone()
    return None if row is None else campaign_row(row)


def require_campaign(conn: sqlite3.Connection, campaign_id: int) -> Campaign:
    """Read a campaign, raising when it does not exist."""
    campaign = get_campaign(conn, campaign_id)
    if campaign is None:
        raise CampaignNotFoundError(campaign_id)
    return campaign


def list_campaigns(
    conn: sqlite3.Connection,
    account_id: int,
    *,
    status: str | None = None,
) -> list[Campaign]:
    sql = "SELECT * FROM campaigns WHERE account_id = ?"
    params: list[object] = [account_id]
    if status is not None:
        if status not in CAMPAIGN_STATUSES:
            raise ValueError(f"unknown campaign status {status!r}")
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY id"
    return [campaign_row(row) for row in conn.execute(sql, params).fetchall()]


def set_campaign_status(
    conn: sqlite3.Connection,
    campaign_id: int,
    status: str,
    *,
    now: datetime | str | None = None,
) -> Campaign:
    """Move a campaign to a new status, stamping `started_at` and `paused_at`.

    `started_at` is set the first time a campaign goes active and never rewritten,
    so a campaign paused and resumed keeps one start date.
    """
    if status not in CAMPAIGN_STATUSES:
        raise ValueError(
            f"unknown campaign status {status!r}; expected one of {list(CAMPAIGN_STATUSES)}"
        )
    moment = now_timestamp(now)
    with transaction(conn):
        current = require_campaign(conn, campaign_id)
        started_at = current.started_at
        if status == "active" and started_at is None:
            started_at = moment
        paused_at = moment if status == "paused" else current.paused_at
        conn.execute(
            "UPDATE campaigns SET status = ?, started_at = ?, paused_at = ? WHERE id = ?",
            (status, started_at, paused_at, campaign_id),
        )
    return require_campaign(conn, campaign_id)


def is_runnable(conn: sqlite3.Connection, campaign_id: int) -> bool:
    """Return True when a runner may execute this campaign's jobs."""
    return require_campaign(conn, campaign_id).runnable
