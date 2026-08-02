"""The three CRM read tools: `lead_search`, `lead_get` and `lead_export_csv`.

Thin wrappers over DB-02's lead store. Nothing here writes a lead, and nothing
here writes SQL against the `leads` table: `list_leads`, `count_leads`,
`get_lead`, `get_lead_by_member_id`, `get_lead_by_public_id`, the tag queries
and the custom field reads already exist and already exclude blacklisted people
by default, which is the property a second hand-rolled query would quietly
lose.

The export round-trips
----------------------
`lead_export_csv` writes the header spellings `linkedin_mcp.scrape.csv_import`
already recognises, and it writes a profile URL built from `public_id`. So an
exported file fed back through `harvest_import_csv` resolves onto the same
leads through DB-03's dedupe rather than creating a second copy of everybody.
An export you cannot re-import is a backup you cannot restore.
"""

from __future__ import annotations

import csv
import io
import logging
from pathlib import Path
from typing import Any

from fastmcp import Context, FastMCP

from linkedin_mcp.audit import audit_linkedin_action
from linkedin_mcp.leads import (
    Lead,
    count_leads,
    get_custom_fields,
    get_lead,
    get_lead_by_member_id,
    get_lead_by_public_id,
    is_blacklisted,
    lead_tag_names,
    leads_with_all_tags,
    leads_with_any_tags,
    list_leads,
)
from linkedin_mcp.tools.contract import LEAD_EXPORT_ACTION, LEAD_READ_ACTION
from linkedin_mcp.tools.runtime import (
    choice,
    error_result,
    positive_int,
    tool_account_id,
    tool_connection,
)

logger = logging.getLogger(__name__)

__all__ = [
    "EXPORT_COLUMNS",
    "MAX_SEARCH_LIMIT",
    "lead_payload",
    "profile_url_for",
    "register_crm_tools",
    "write_export_csv",
]

MAX_SEARCH_LIMIT = 500
DEFAULT_SEARCH_LIMIT = 50
MAX_EXPORT_ROWS = 10000

TAG_MATCHES = ("any", "all")

EXPORT_COLUMNS: tuple[str, ...] = (
    "profile_url",
    "public_id",
    "member_id",
    "full_name",
    "first_name",
    "last_name",
    "headline",
    "organization_name",
    "organization_title",
    "location_name",
    "member_distance",
    "summary",
    "avatar_url",
)
"""Header of an exported file.

Every name is a key `linkedin_mcp.scrape.csv_import.COLUMN_ALIASES` maps back
onto a lead field, which is what makes the export re-importable.
"""

SEARCHABLE_FIELDS: tuple[str, ...] = (
    "full_name",
    "headline",
    "organization_name",
    "organization_title",
    "location_name",
)


def profile_url_for(lead: Lead) -> str | None:
    """Return the public profile URL of a lead, when it has a public id.

    `leads` stores no URL column, so the export rebuilds one from `public_id`.
    `csv_import.public_id_from` reverses it exactly, which is the round trip.
    """
    return f"https://www.linkedin.com/in/{lead.public_id}" if lead.public_id else None


def lead_payload(lead: Lead) -> dict[str, Any]:
    """Return a lead as JSON friendly data for a tool result."""
    return {
        "id": lead.id,
        "full_name": lead.full_name,
        "member_id": lead.member_id,
        "public_id": lead.public_id,
        "profile_url": profile_url_for(lead),
        "headline": lead.headline,
        "organization_name": lead.organization_name,
        "organization_title": lead.organization_title,
        "location_name": lead.location_name,
        "member_distance": lead.member_distance,
        "connection_count": lead.connection_count,
        "follower_count": lead.follower_count,
        "connected_at": lead.connected_at,
        "first_seen_at": lead.first_seen_at,
        "last_visited_at": lead.last_visited_at,
    }


def _matches(lead: Lead, needle: str) -> bool:
    """True when a lead's text fields contain the query, case-insensitively."""
    for name in SEARCHABLE_FIELDS:
        value = getattr(lead, name, None)
        if value and needle in str(value).lower():
            return True
    return False


def write_export_csv(leads: list[Lead], handle: Any) -> int:
    """Write leads to an open text handle in the re-importable shape."""
    writer = csv.writer(handle, lineterminator="\n")
    writer.writerow(EXPORT_COLUMNS)
    for lead in leads:
        writer.writerow(
            [
                profile_url_for(lead) or "",
                lead.public_id or "",
                lead.member_id or "",
                lead.full_name or "",
                lead.first_name or "",
                lead.last_name or "",
                lead.headline or "",
                lead.organization_name or "",
                lead.organization_title or "",
                lead.location_name or "",
                lead.member_distance or "",
                lead.summary or "",
                lead.avatar_url or "",
            ]
        )
    return len(leads)


def _selected_leads(
    conn: Any,
    account_id: int,
    *,
    tags: list[str] | None,
    match: str,
    include_blacklisted: bool,
) -> list[Lead]:
    """Read the candidate leads, letting the tag queries do the narrowing."""
    wanted = [name for name in (tags or []) if str(name).strip()]
    if not wanted:
        return list_leads(
            conn, account_id, include_blacklisted=include_blacklisted
        )
    if match == "all":
        return leads_with_all_tags(
            conn, account_id, wanted, include_blacklisted=include_blacklisted
        )
    return leads_with_any_tags(
        conn, account_id, wanted, include_blacklisted=include_blacklisted
    )


def register_crm_tools(mcp: FastMCP) -> None:
    """Register the three CRM read tools on the MCP server."""

    @mcp.tool()
    @audit_linkedin_action(LEAD_READ_ACTION, target="query", capture=("tags", "limit"))
    async def lead_search(
        query: str | None = None,
        tags: list[str] | None = None,
        match: str = "any",
        limit: int = DEFAULT_SEARCH_LIMIT,
        offset: int = 0,
        include_blacklisted: bool = False,
        ctx: Context | None = None,
    ) -> dict:
        """Search stored leads by text and tags. Reads locally, never LinkedIn."""
        try:
            conn = tool_connection()
            account_id = tool_account_id()
            selected = _selected_leads(
                conn,
                account_id,
                tags=tags,
                match=choice("match", match, TAG_MATCHES),
                include_blacklisted=bool(include_blacklisted),
            )

            needle = (query or "").strip().lower()
            if needle:
                selected = [lead for lead in selected if _matches(lead, needle)]

            start = max(0, int(offset or 0))
            size = positive_int(
                "limit", limit, default=DEFAULT_SEARCH_LIMIT, maximum=MAX_SEARCH_LIMIT
            )
            page = selected[start : start + size]

            return {
                "status": "success",
                "count": len(page),
                "matched": len(selected),
                "total": count_leads(
                    conn, account_id, include_blacklisted=bool(include_blacklisted)
                ),
                "offset": start,
                "limit": size,
                "leads": [lead_payload(lead) for lead in page],
                "message": (
                    f"{len(selected)} lead(s) matched, showing {len(page)} from "
                    f"offset {start}."
                ),
            }
        except Exception as error:
            return error_result(f"Could not search leads: {error}")

    @mcp.tool()
    @audit_linkedin_action(
        LEAD_READ_ACTION, target="lead_id", capture=("member_id", "public_id")
    )
    async def lead_get(
        lead_id: int | None = None,
        member_id: str | None = None,
        public_id: str | None = None,
        ctx: Context | None = None,
    ) -> dict:
        """Read one stored lead with its tags, custom fields and blacklist state."""
        try:
            conn = tool_connection()
            account_id = tool_account_id()

            if lead_id is not None:
                lead = get_lead(conn, int(lead_id))
            elif member_id:
                lead = get_lead_by_member_id(conn, account_id, str(member_id))
            elif public_id:
                lead = get_lead_by_public_id(conn, account_id, str(public_id))
            else:
                raise ValueError("give one of lead_id, member_id or public_id")

            if lead is None:
                return error_result("No lead matches that identifier")
            if lead.account_id != account_id:
                return error_result("No lead matches that identifier")

            return {
                "status": "success",
                "lead": {
                    **lead_payload(lead),
                    "summary": lead.summary,
                    "avatar_url": lead.avatar_url,
                    "badges": lead.badges,
                    "contact_info_fetched_at": lead.contact_info_fetched_at,
                    "positions_fetched_at": lead.positions_fetched_at,
                },
                "tags": lead_tag_names(conn, lead.id),
                "custom_fields": get_custom_fields(conn, lead.id),
                "blacklisted": is_blacklisted(conn, account_id, lead.id),
                "message": f"Lead {lead.id}: {lead.full_name}",
            }
        except Exception as error:
            return error_result(f"Could not read the lead: {error}")

    @mcp.tool()
    @audit_linkedin_action(LEAD_EXPORT_ACTION, target="path", capture=("tags",))
    async def lead_export_csv(
        path: str | None = None,
        tags: list[str] | None = None,
        match: str = "any",
        limit: int | None = None,
        include_blacklisted: bool = False,
        ctx: Context | None = None,
    ) -> dict:
        """Export stored leads to CSV in a shape `harvest_import_csv` can re-read."""
        try:
            conn = tool_connection()
            account_id = tool_account_id()
            selected = _selected_leads(
                conn,
                account_id,
                tags=tags,
                match=choice("match", match, TAG_MATCHES),
                include_blacklisted=bool(include_blacklisted),
            )
            if limit is not None:
                selected = selected[
                    : positive_int(
                        "limit", limit, default=MAX_EXPORT_ROWS, maximum=MAX_EXPORT_ROWS
                    )
                ]

            if path:
                target = Path(str(path).strip())
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("w", encoding="utf-8", newline="") as handle:
                    written = write_export_csv(selected, handle)
                return {
                    "status": "success",
                    "path": str(target),
                    "count": written,
                    "columns": list(EXPORT_COLUMNS),
                    "message": (
                        f"Exported {written} lead(s) to {target}. Re-importing it "
                        "with harvest_import_csv resolves onto the same leads."
                    ),
                }

            buffer = io.StringIO()
            written = write_export_csv(selected, buffer)
            return {
                "status": "success",
                "path": None,
                "count": written,
                "columns": list(EXPORT_COLUMNS),
                "csv": buffer.getvalue(),
                "message": (
                    f"Exported {written} lead(s) as CSV text. Give a path to write "
                    "a file instead."
                ),
            }
        except Exception as error:
            return error_result(f"Could not export leads: {error}")
