"""Persist one profile visit through the DB-02 store and the DB-03 dedupe layer.

The lead row itself never sees raw SQL from here. It goes through
:func:`linkedin_mcp.leads.harvest_leads`, which resolves the sighting onto an
existing lead or creates one, merges field by field so a thin sighting never
blanks a richer record, and refuses a blacklisted profile instead of
resurrecting it.

Cache stamps come last
----------------------
:func:`~linkedin_mcp.leads.mark_section_fetched` is called after every write for
the visit has succeeded, not alongside the lead write. A stamp is a promise that
the section is stored, so stamping last means the worst case is a wasted revisit
in a fortnight rather than a section marked fresh with nothing behind it.

Child tables
------------
``lead_experience``, ``lead_education`` and ``lead_skills`` are ordered lists
owned by the profile, so each is replaced wholesale, but only when the visit
actually produced rows. A selector that stops matching returns an empty list,
and treating that as "this person deleted their career" would destroy good data
on the strength of a DOM change.

``lead_contacts`` is different. Contact details also arrive from CSV imports and
from a human typing them in, so they are upserted rather than replaced. Nothing
in a profile visit is allowed to delete a contact it did not put there.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from linkedin_mcp.leads import (
    HarvestSummary,
    LeadSection,
    harvest_leads,
    mark_section_fetched,
    set_custom_fields,
)
from linkedin_mcp.leads.dedupe import normalise_section, utc_timestamp
from linkedin_mcp.scrape.profile_records import (
    ContactEntry,
    EducationEntry,
    ExperienceEntry,
    ProfileDetail,
    SkillEntry,
)

logger = logging.getLogger(__name__)

__all__ = [
    "CONTACT_SOURCE",
    "ProfileStoreResult",
    "replace_education",
    "replace_experience",
    "replace_skills",
    "store_profile_detail",
    "upsert_contacts",
]

CONTACT_SOURCE = "profile_contact_info"
"""What a contact row records as its provenance when a profile visit found it."""


@dataclass(frozen=True, slots=True)
class ProfileStoreResult:
    """What one profile visit changed in the database."""

    lead_id: int | None = None
    harvest: HarvestSummary = field(default_factory=HarvestSummary)
    experience_rows: int = 0
    education_rows: int = 0
    skill_rows: int = 0
    contact_rows: int = 0
    custom_fields: tuple[str, ...] = ()
    sections_marked: tuple[str, ...] = ()

    @property
    def stored(self) -> bool:
        """True when the visit reached a lead row."""
        return self.lead_id is not None

    @property
    def refused(self) -> bool:
        """True when the dedupe layer declined the profile."""
        return bool(self.harvest.refusals)

    def as_dict(self) -> dict[str, Any]:
        return {
            "lead_id": self.lead_id,
            "leads_created": self.harvest.created,
            "leads_updated": self.harvest.updated,
            "leads_unchanged": self.harvest.unchanged,
            "experience_rows": self.experience_rows,
            "education_rows": self.education_rows,
            "skill_rows": self.skill_rows,
            "contact_rows": self.contact_rows,
            "custom_fields": list(self.custom_fields),
            "sections_marked": list(self.sections_marked),
            "harvest_refusals": [
                {
                    "reason": refusal.reason,
                    "message": refusal.message,
                    "member_id": refusal.member_id,
                    "public_id": refusal.public_id,
                }
                for refusal in self.harvest.refusals
            ],
        }


def replace_experience(
    conn: sqlite3.Connection,
    lead_id: int,
    entries: Sequence[ExperienceEntry],
) -> int:
    """Replace a lead's positions, keeping the order the profile listed them in."""
    rows = [entry for entry in entries if not entry.is_empty()]
    if not rows:
        return 0

    conn.execute("DELETE FROM lead_experience WHERE lead_id = ?", (lead_id,))
    conn.executemany(
        """
        INSERT INTO lead_experience
            (lead_id, ord, title, company, company_id, start_date, end_date, location)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                lead_id,
                index,
                entry.title,
                entry.company,
                entry.company_id,
                entry.start_date,
                entry.end_date,
                entry.location,
            )
            for index, entry in enumerate(rows)
        ],
    )
    return len(rows)


def replace_education(
    conn: sqlite3.Connection,
    lead_id: int,
    entries: Sequence[EducationEntry],
) -> int:
    """Replace a lead's schools, keeping the order the profile listed them in."""
    rows = [entry for entry in entries if not entry.is_empty()]
    if not rows:
        return 0

    conn.execute("DELETE FROM lead_education WHERE lead_id = ?", (lead_id,))
    conn.executemany(
        """
        INSERT INTO lead_education
            (lead_id, ord, school, degree, field, start_year, end_year)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                lead_id,
                index,
                entry.school,
                entry.degree,
                entry.field_of_study,
                entry.start_year,
                entry.end_year,
            )
            for index, entry in enumerate(rows)
        ],
    )
    return len(rows)


def replace_skills(
    conn: sqlite3.Connection,
    lead_id: int,
    entries: Sequence[SkillEntry],
) -> int:
    """Replace a lead's skills. The table is keyed on the skill, so order is lost."""
    rows: list[SkillEntry] = []
    seen: set[str] = set()
    for entry in entries:
        if entry.is_empty() or entry.skill in seen:
            continue
        seen.add(entry.skill)
        rows.append(entry)
    if not rows:
        return 0

    conn.execute("DELETE FROM lead_skills WHERE lead_id = ?", (lead_id,))
    conn.executemany(
        "INSERT INTO lead_skills (lead_id, skill, endorsement_count) VALUES (?, ?, ?)",
        [(lead_id, entry.skill, entry.endorsement_count) for entry in rows],
    )
    return len(rows)


def upsert_contacts(
    conn: sqlite3.Connection,
    lead_id: int,
    entries: Sequence[ContactEntry],
    *,
    verified_at: str | None = None,
) -> int:
    """Add or refresh contact details without deleting any this visit did not see."""
    rows = [entry for entry in entries if entry.is_valid()]
    if not rows:
        return 0

    conn.executemany(
        """
        INSERT INTO lead_contacts (lead_id, kind, value, source, verified_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (lead_id, kind, value)
        DO UPDATE SET source = excluded.source, verified_at = excluded.verified_at
        """,
        [
            (
                lead_id,
                entry.kind,
                entry.value.strip(),
                entry.source or CONTACT_SOURCE,
                verified_at,
            )
            for entry in rows
        ],
    )
    return len(rows)


def store_profile_detail(
    conn: sqlite3.Connection,
    account_id: int,
    detail: ProfileDetail,
    *,
    sections_fetched: Sequence[str | LeadSection] = (),
    fetched_at: datetime | str | None = None,
    visited_at: datetime | str | None = None,
) -> ProfileStoreResult:
    """Store one profile visit and return what it changed.

    Args:
        conn: Open connection to the MCP database.
        account_id: Account the visit ran as.
        detail: What the page said.
        sections_fetched: Cache sections this visit actually read. Stamped in the
            same transaction as the lead write.
        fetched_at: Timestamp for those section stamps.
        visited_at: Timestamp for ``leads.last_visited_at``, which only moves
            forward.

    A profile the dedupe layer refuses, because it is blacklisted or because its
    identifiers cannot be resolved onto one person, comes back with the refusal
    on :attr:`ProfileStoreResult.harvest` and nothing written. That is
    deliberate: a run that silently drops people is a run nobody can audit.
    """
    if not detail.is_identifiable():
        logger.info("Skipping a profile with no member id or public id")
        return ProfileStoreResult()

    fields = detail.as_lead_fields()
    if visited_at is not None:
        fields["last_visited_at"] = visited_at

    sections = tuple(normalise_section(section) for section in sections_fetched)
    summary = harvest_leads(conn, account_id, [fields], fetched_at=fetched_at)
    if not summary.lead_ids:
        return ProfileStoreResult(harvest=summary)

    lead_id = summary.lead_ids[0]
    stamp = _as_text(fetched_at)

    try:
        experience_rows = replace_experience(conn, lead_id, detail.experience)
        education_rows = replace_education(conn, lead_id, detail.education)
        skill_rows = replace_skills(conn, lead_id, detail.skills)
        contact_rows = upsert_contacts(
            conn, lead_id, detail.contacts, verified_at=stamp
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    custom = detail.custom_fields()
    if custom:
        set_custom_fields(conn, lead_id, custom)

    for section in sections:
        mark_section_fetched(conn, lead_id, section, fetched_at=fetched_at)

    return ProfileStoreResult(
        lead_id=lead_id,
        harvest=summary,
        experience_rows=experience_rows,
        education_rows=education_rows,
        skill_rows=skill_rows,
        contact_rows=contact_rows,
        custom_fields=tuple(sorted(custom)),
        sections_marked=tuple(sorted(sections)),
    )


def _as_text(moment: datetime | str | None) -> str | None:
    if moment is None:
        return None
    if isinstance(moment, datetime):
        return utc_timestamp(moment)
    return str(moment)
