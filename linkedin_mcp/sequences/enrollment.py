"""Enrolment: putting leads into a campaign's state machine.

An enrolled lead gets one `campaign_leads` row and, if it is still in the flow,
one job. Both writes land in the same transaction, so a lead never exists in the
state machine without the queue knowing about it.

Two things bar a lead at the door, and both land it in `excluded` rather than
being silently dropped. A row that records why is worth more than no row at all:

- the global blacklist, which DB-02 owns, and
- the campaign's own exclude list, read as a `tags.id`.

An excluded row still occupies the campaign, so re-running an enrolment does not
retry the door check every night.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from linkedin_mcp.leads import is_blacklisted
from linkedin_mcp.sequences import jobs as jobs_module
from linkedin_mcp.sequences.campaigns import require_campaign
from linkedin_mcp.sequences.errors import (
    CampaignLeadNotFoundError,
    StepDefinitionError,
)
from linkedin_mcp.sequences.jobs import JobSpec
from linkedin_mcp.sequences.states import (
    ACTIVE_SUBLISTS,
    TERMINAL_SUBLISTS,
    Sublist,
    coerce_sublist,
)
from linkedin_mcp.sequences.steps import find_step_at_ord, first_step_ord
from linkedin_mcp.sequences.transaction import (
    now_timestamp,
    shift_timestamp,
    transaction,
)

__all__ = [
    "BLACKLIST_OUTCOME",
    "CampaignLead",
    "EnrolmentSummary",
    "EXCLUDE_LIST_OUTCOME",
    "campaign_lead_row",
    "enrol_lead",
    "enrol_leads",
    "get_campaign_lead",
    "list_campaign_leads",
    "on_exclude_list",
    "require_campaign_lead",
    "sublist_counts",
    "withdraw_lead",
]

BLACKLIST_OUTCOME = "blacklisted"
EXCLUDE_LIST_OUTCOME = "campaign_exclude_list"
ENROLLED_OUTCOME = "enrolled"


@dataclass(frozen=True, slots=True)
class CampaignLead:
    """One lead's position in one campaign: the state machine row itself."""

    campaign_id: int
    lead_id: int
    current_step_ord: int
    sublist: str
    next_run_at: str | None = None
    attempts: int = 0
    last_outcome: str | None = None
    entered_at: str | None = None
    updated_at: str | None = None

    @property
    def in_flow(self) -> bool:
        """True while the lead is still working through the sequence."""
        return self.sublist in {member.value for member in ACTIVE_SUBLISTS}

    @property
    def is_terminal(self) -> bool:
        return self.sublist in {member.value for member in TERMINAL_SUBLISTS}


@dataclass(frozen=True, slots=True)
class EnrolmentSummary:
    """Outcome of enrolling a batch of leads."""

    campaign_id: int
    enrolled: tuple[int, ...] = ()
    excluded: tuple[int, ...] = ()
    already_enrolled: tuple[int, ...] = ()

    @property
    def total(self) -> int:
        return len(self.enrolled) + len(self.excluded) + len(self.already_enrolled)


def campaign_lead_row(row: sqlite3.Row) -> CampaignLead:
    return CampaignLead(
        campaign_id=row["campaign_id"],
        lead_id=row["lead_id"],
        current_step_ord=row["current_step_ord"],
        sublist=row["sublist"],
        next_run_at=row["next_run_at"],
        attempts=row["attempts"],
        last_outcome=row["last_outcome"],
        entered_at=row["entered_at"],
        updated_at=row["updated_at"],
    )


def get_campaign_lead(
    conn: sqlite3.Connection,
    campaign_id: int,
    lead_id: int,
) -> CampaignLead | None:
    row = conn.execute(
        "SELECT * FROM campaign_leads WHERE campaign_id = ? AND lead_id = ?",
        (campaign_id, lead_id),
    ).fetchone()
    return None if row is None else campaign_lead_row(row)


def require_campaign_lead(
    conn: sqlite3.Connection,
    campaign_id: int,
    lead_id: int,
) -> CampaignLead:
    """Read one state machine row, raising when the lead is not enrolled."""
    enrolled = get_campaign_lead(conn, campaign_id, lead_id)
    if enrolled is None:
        raise CampaignLeadNotFoundError(campaign_id, lead_id)
    return enrolled


def on_exclude_list(conn: sqlite3.Connection, campaign_id: int, lead_id: int) -> bool:
    """Return True when the lead carries the campaign's exclude tag."""
    campaign = require_campaign(conn, campaign_id)
    if campaign.exclude_list_id is None:
        return False
    row = conn.execute(
        "SELECT 1 FROM lead_tags WHERE lead_id = ? AND tag_id = ?",
        (lead_id, campaign.exclude_list_id),
    ).fetchone()
    return row is not None


def _door_check(conn: sqlite3.Connection, campaign_id: int, lead_id: int) -> str | None:
    """Return the reason a lead may not enter, or None to let it in."""
    campaign = require_campaign(conn, campaign_id)
    if is_blacklisted(conn, campaign.account_id, lead_id):
        return BLACKLIST_OUTCOME
    if on_exclude_list(conn, campaign_id, lead_id):
        return EXCLUDE_LIST_OUTCOME
    return None


def enrol_lead(
    conn: sqlite3.Connection,
    campaign_id: int,
    lead_id: int,
    *,
    now: datetime | str | None = None,
    start_ord: int | None = None,
) -> CampaignLead:
    """Enrol one lead and enqueue its first step, atomically.

    A lead that is blacklisted or on the campaign's exclude list is still given a
    row, in `excluded`, with no job. Returning quietly would leave no record that
    the lead was considered.
    """
    moment = now_timestamp(now)
    with transaction(conn):
        campaign = require_campaign(conn, campaign_id)
        existing = get_campaign_lead(conn, campaign_id, lead_id)
        if existing is not None:
            return existing

        opening_ord = start_ord if start_ord is not None else first_step_ord(conn, campaign_id)
        if opening_ord is None:
            raise StepDefinitionError(
                f"campaign {campaign_id} has no steps, so nothing can be enrolled into it"
            )
        step = find_step_at_ord(conn, campaign_id, opening_ord)
        if step is None:
            raise StepDefinitionError(
                f"campaign {campaign_id} has no step at ord {opening_ord}"
            )

        blocked = _door_check(conn, campaign_id, lead_id)
        if blocked is not None:
            conn.execute(
                """
                INSERT INTO campaign_leads
                    (campaign_id, lead_id, current_step_ord, sublist, next_run_at,
                     attempts, last_outcome, entered_at, updated_at)
                VALUES (?, ?, ?, ?, NULL, 0, ?, ?, ?)
                """,
                (
                    campaign_id,
                    lead_id,
                    opening_ord,
                    Sublist.EXCLUDED.value,
                    blocked,
                    moment,
                    moment,
                ),
            )
            return require_campaign_lead(conn, campaign_id, lead_id)

        due_at = shift_timestamp(moment, step.delay_seconds) if step.delay_seconds else moment
        conn.execute(
            """
            INSERT INTO campaign_leads
                (campaign_id, lead_id, current_step_ord, sublist, next_run_at,
                 attempts, last_outcome, entered_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)
            """,
            (
                campaign_id,
                lead_id,
                opening_ord,
                Sublist.QUEUE.value,
                due_at,
                ENROLLED_OUTCOME,
                moment,
                moment,
            ),
        )
        jobs_module.insert_job(
            conn,
            JobSpec(
                campaign_id=campaign_id,
                lead_id=lead_id,
                step_id=step.id,
                account_id=campaign.account_id,
                action_type=step.action_type,
                payload_json=jobs_module.step_payload(step.ord),
                scheduled_for=due_at,
                priority=step.priority,
            ),
        )
        return require_campaign_lead(conn, campaign_id, lead_id)


def enrol_leads(
    conn: sqlite3.Connection,
    campaign_id: int,
    lead_ids: Iterable[int],
    *,
    now: datetime | str | None = None,
) -> EnrolmentSummary:
    """Enrol a batch of leads, one transaction per lead.

    Per lead rather than per batch on purpose: a single unenrollable lead should
    not roll back the hundreds that were fine.
    """
    enrolled: list[int] = []
    excluded: list[int] = []
    already: list[int] = []
    for lead_id in lead_ids:
        before = get_campaign_lead(conn, campaign_id, lead_id)
        if before is not None:
            already.append(lead_id)
            continue
        record = enrol_lead(conn, campaign_id, lead_id, now=now)
        if record.sublist == Sublist.EXCLUDED.value:
            excluded.append(lead_id)
        else:
            enrolled.append(lead_id)
    return EnrolmentSummary(
        campaign_id=campaign_id,
        enrolled=tuple(enrolled),
        excluded=tuple(excluded),
        already_enrolled=tuple(already),
    )


def list_campaign_leads(
    conn: sqlite3.Connection,
    campaign_id: int,
    *,
    sublist: Sublist | str | None = None,
) -> list[CampaignLead]:
    """Read a campaign's leads, optionally one sub-list at a time."""
    sql = "SELECT * FROM campaign_leads WHERE campaign_id = ?"
    params: list[object] = [campaign_id]
    if sublist is not None:
        sql += " AND sublist = ?"
        params.append(coerce_sublist(sublist).value)
    sql += " ORDER BY lead_id"
    return [campaign_lead_row(row) for row in conn.execute(sql, params).fetchall()]


def sublist_counts(conn: sqlite3.Connection, campaign_id: int) -> dict[str, int]:
    """Return the size of every sub-list, including the empty ones.

    Every one of the seven keys is always present, so a caller rendering a
    campaign summary never has to guess whether a missing key means zero or means
    the sub-list does not exist.
    """
    counts = {member.value: 0 for member in Sublist}
    rows = conn.execute(
        "SELECT sublist, COUNT(*) AS total FROM campaign_leads WHERE campaign_id = ? "
        "GROUP BY sublist",
        (campaign_id,),
    ).fetchall()
    for row in rows:
        counts[row["sublist"]] = int(row["total"])
    return counts


def withdraw_lead(conn: sqlite3.Connection, campaign_id: int, lead_id: int) -> bool:
    """Remove a lead from a campaign entirely, cancelling any open job.

    Withdrawal is not a sub-list. It is for a lead that should never have been
    enrolled, so it leaves no state machine row at all. To stop a lead while
    keeping the record, move it to a terminal sub-list instead.
    """
    with transaction(conn):
        jobs_module.close_open_jobs(
            conn,
            campaign_id,
            lead_id,
            jobs_module.JobState.CANCELLED,
            error="lead withdrawn from campaign",
        )
        cursor = conn.execute(
            "DELETE FROM campaign_leads WHERE campaign_id = ? AND lead_id = ?",
            (campaign_id, lead_id),
        )
        return cursor.rowcount > 0
