"""Persist extracted people through the DB-02 lead store.

Everything a run extracts goes through :func:`linkedin_mcp.leads.harvest_leads`,
which is the batch entry point built for exactly this. It resolves each sighting
onto an existing lead or creates one, merges field by field so a thin search
card never blanks a richer stored record, and collects the profiles it declined
instead of aborting the page.

Blacklist
---------
`harvest_leads` refuses a profile on the global do-not-contact list, so a
blacklisted person is never resurrected by a harvest. The refusal is surfaced in
the summary rather than swallowed, because a run that silently drops people is
a run nobody can audit.

Cache windows
-------------
A search card is cheap; a profile visit is not. After a harvest this module
reports which of the leads it touched are actually stale under the DB-02 cache
windows, so a deep scrape spends its much smaller profile budget on the leads
that need it rather than on all of them.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable, Sequence
from datetime import datetime

from linkedin_mcp.leads import (
    HarvestSummary,
    LeadSection,
    harvest_leads,
    is_blacklisted,
    needs_refresh,
)
from linkedin_mcp.scrape.records import PersonResult

logger = logging.getLogger(__name__)

__all__ = ["harvest_people", "stale_lead_ids"]


def harvest_people(
    conn: sqlite3.Connection,
    account_id: int,
    people: Sequence[PersonResult],
    *,
    fetched_at: datetime | str | None = None,
) -> HarvestSummary:
    """Store a page of extracted people and return the incremental counts."""
    profiles = [
        person.as_lead_fields() for person in people if person.is_identifiable()
    ]
    skipped = len(people) - len(profiles)
    if skipped:
        logger.info(
            "Skipped %d extracted card(s) with no member id or public id", skipped
        )
    if not profiles:
        return HarvestSummary()
    return harvest_leads(conn, account_id, profiles, fetched_at=fetched_at)


def stale_lead_ids(
    conn: sqlite3.Connection,
    account_id: int,
    lead_ids: Iterable[int],
    *,
    section: str | LeadSection = LeadSection.POSITIONS,
    now: datetime | None = None,
) -> tuple[int, ...]:
    """Return the harvested leads a deep scrape would still learn something from.

    Blacklisted leads are dropped. Harvesting one is allowed, queueing a visit
    to one is not.
    """
    stale: list[int] = []
    for lead_id in dict.fromkeys(lead_ids):
        if is_blacklisted(conn, account_id, lead_id):
            continue
        if needs_refresh(conn, lead_id, section, now=now):
            stale.append(lead_id)
    return tuple(stale)
