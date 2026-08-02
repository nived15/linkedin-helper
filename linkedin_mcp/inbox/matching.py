"""Matching a conversation to a lead, and a lead to the queues it is sitting in.

"Active campaign queues" is not redefined here
----------------------------------------------
SEQ-01 already owns that vocabulary.
:data:`~linkedin_mcp.sequences.states.ACTIVE_SUBLISTS` says which sub-lists mean
a lead is still in the flow, and
:data:`~linkedin_mcp.sequences.campaigns.RUNNABLE_STATUSES` says which campaigns
a runner may execute. This module joins on both rather than inventing a third
notion of active, so a campaign that is paused tomorrow changes what this finds
without anything here being edited.

Terminating the sequence
------------------------
:func:`~linkedin_mcp.sequences.transitions.mark_replied` is the whole
transition. It was checked by running it rather than by reading it: after the
call the lead is in `replied`, `open_job_for_lead` returns None, the job row is
`cancelled`, and `due_jobs` no longer offers it. There is no second close path
here, because a second one would be a second chance to get it wrong.

Strangers
---------
A reply from somebody who is not in a campaign, or not in the lead database at
all, is an ordinary event. Recruiters, colleagues and newsletters all land in
the same inbox. Such a thread is reported, not forced into a campaign and not
crashed on. It is deliberately not turned into a new lead either: harvesting
everyone who messages you into the outreach database is a decision for a person
to make, not a side effect of a background scan.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from linkedin_mcp.inbox.threads import InboxThread
from linkedin_mcp.leads import Lead, get_lead_by_member_id, get_lead_by_public_id
from linkedin_mcp.sequences import (
    ACTIVE_SUBLISTS,
    RUNNABLE_STATUSES,
    CampaignLead,
    mark_replied,
)
from linkedin_mcp.sequences.enrollment import campaign_lead_row

logger = logging.getLogger(__name__)

__all__ = [
    "REPLY_DETAIL",
    "ThreadMatch",
    "active_campaign_leads",
    "match_thread",
    "resolve_lead",
    "terminate_sequences",
]

REPLY_DETAIL = "inbox_scan"
"""What `campaign_leads.last_outcome` says about a reply this scanner found."""


@dataclass(frozen=True, slots=True)
class ThreadMatch:
    """How one conversation resolved against the lead database and its campaigns."""

    thread_urn: str
    lead_id: int | None = None
    campaign_ids: tuple[int, ...] = ()

    @property
    def resolved(self) -> bool:
        """True when the conversation belongs to a known lead."""
        return self.lead_id is not None

    @property
    def in_campaign(self) -> bool:
        """True when the lead is sitting in at least one active campaign queue."""
        return bool(self.campaign_ids)


def resolve_lead(
    conn: sqlite3.Connection,
    account_id: int,
    thread: InboxThread,
) -> Lead | None:
    """Find the lead a conversation is with, or None for a stranger.

    The member URN is tried first because it survives a profile slug change,
    which is the whole reason DB-03 keeps both identifiers.
    """
    if thread.participant_member_id:
        lead = get_lead_by_member_id(conn, account_id, thread.participant_member_id)
        if lead is not None:
            return lead
    if thread.participant_public_id:
        lead = get_lead_by_public_id(conn, account_id, thread.participant_public_id)
        if lead is not None:
            return lead
    return None


def active_campaign_leads(
    conn: sqlite3.Connection,
    account_id: int,
    lead_id: int,
) -> list[CampaignLead]:
    """Return every runnable campaign queue this lead is still sitting in."""
    statuses = tuple(sorted(RUNNABLE_STATUSES))
    sublists = tuple(member.value for member in ACTIVE_SUBLISTS)
    rows = conn.execute(
        f"""
        SELECT cl.*
        FROM campaign_leads AS cl
        JOIN campaigns AS c ON c.id = cl.campaign_id
        WHERE c.account_id = ?
          AND cl.lead_id = ?
          AND c.status IN ({", ".join("?" for _ in statuses)})
          AND cl.sublist IN ({", ".join("?" for _ in sublists)})
        ORDER BY cl.campaign_id
        """,
        (account_id, lead_id, *statuses, *sublists),
    ).fetchall()
    return [campaign_lead_row(row) for row in rows]


def terminate_sequences(
    conn: sqlite3.Connection,
    account_id: int,
    lead_id: int,
    *,
    now: datetime | str | None = None,
    detail: str | None = REPLY_DETAIL,
) -> tuple[int, ...]:
    """Move the lead to `replied` in every active queue and return those campaigns.

    Each campaign is its own transition and therefore its own transaction, and
    that is deliberate rather than an oversight. `mark_replied` cancels the open
    job inside the same transaction as the sub-list write, so one campaign is
    always all-or-nothing. Wrapping *several* campaigns in one transaction would
    make a failure on the third undo the first two, and a lead whose stop was
    rolled back is a lead whose follow-up goes out tonight. Independent stops
    mean a failure costs one campaign rather than all of them.

    For the same reason a failure is caught rather than propagated. Letting it
    out would abandon every campaign after it in the list, so it is logged at
    error level, reported to the caller, and the loop carries on.
    """
    stopped: list[int] = []
    for record in active_campaign_leads(conn, account_id, lead_id):
        try:
            mark_replied(conn, record.campaign_id, lead_id, now=now, detail=detail)
        except Exception:
            logger.exception(
                "Could not stop campaign %d for lead %d after a reply; the other "
                "campaigns this lead is in are still being stopped",
                record.campaign_id,
                lead_id,
            )
            continue
        stopped.append(record.campaign_id)
        logger.info(
            "Lead %d replied, so campaign %d stopped sequencing it",
            lead_id,
            record.campaign_id,
        )
    return tuple(stopped)


def match_thread(
    conn: sqlite3.Connection,
    account_id: int,
    thread: InboxThread,
) -> ThreadMatch:
    """Resolve a conversation to a lead and to the queues that lead is in.

    This reads only. Nothing moves until :func:`terminate_sequences` is called,
    so a caller can inspect a match before acting on it.
    """
    lead = resolve_lead(conn, account_id, thread)
    if lead is None:
        return ThreadMatch(thread_urn=thread.thread_urn)
    return ThreadMatch(
        thread_urn=thread.thread_urn,
        lead_id=lead.id,
        campaign_ids=tuple(
            record.campaign_id for record in active_campaign_leads(conn, account_id, lead.id)
        ),
    )
