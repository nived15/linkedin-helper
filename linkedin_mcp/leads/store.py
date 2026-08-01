"""Lead CRUD, ``{cs_*}`` custom fields and audience queries.

Built directly on the DB-01 schema. Audience reads exclude blacklisted leads by
default so a caller cannot contact one by forgetting to check, and writes that
would create or refresh a blacklisted lead are refused outright.

Deduplication is deliberately out of scope here; that belongs to DB-03.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from linkedin_mcp.leads.blacklist import (
    BLACKLIST_EXCLUSION_SQL,
    guard_identity,
    normalise_identifier,
)
from linkedin_mcp.leads.errors import LeadNotFoundError


__all__ = [
    "CUSTOM_FIELD_PREFIX",
    "IDENTITY_FIELDS",
    "LEAD_COLUMNS",
    "WRITABLE_FIELDS",
    "Lead",
    "count_leads",
    "create_lead",
    "custom_field_tokens",
    "delete_custom_field",
    "delete_lead",
    "get_custom_field",
    "get_custom_fields",
    "get_lead",
    "get_lead_by_member_id",
    "get_lead_by_public_id",
    "lead_from_row",
    "leads_from_rows",
    "list_leads",
    "normalise_custom_field_key",
    "placeholders_for",
    "set_custom_field",
    "set_custom_fields",
    "update_lead",
]

CUSTOM_FIELD_PREFIX = "cs_"

LEAD_COLUMNS: tuple[str, ...] = (
    "id",
    "account_id",
    "member_id",
    "public_id",
    "hash_id",
    "full_name",
    "first_name",
    "last_name",
    "headline",
    "summary",
    "organization_name",
    "organization_title",
    "location_name",
    "member_distance",
    "connection_count",
    "follower_count",
    "connected_at",
    "badges_json",
    "avatar_url",
    "first_seen_at",
    "last_visited_at",
)

IDENTITY_FIELDS: frozenset[str] = frozenset({"member_id", "public_id", "hash_id"})

WRITABLE_FIELDS: frozenset[str] = frozenset(
    {
        "member_id",
        "public_id",
        "hash_id",
        "full_name",
        "first_name",
        "last_name",
        "headline",
        "summary",
        "organization_name",
        "organization_title",
        "location_name",
        "member_distance",
        "connection_count",
        "follower_count",
        "connected_at",
        "badges",
        "avatar_url",
        "last_visited_at",
    }
)

_FIELD_COLUMNS: dict[str, str] = {name: name for name in WRITABLE_FIELDS}
_FIELD_COLUMNS["badges"] = "badges_json"


@dataclass(frozen=True, slots=True)
class Lead:
    id: int
    account_id: int
    full_name: str
    member_id: str | None = None
    public_id: str | None = None
    hash_id: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    headline: str | None = None
    summary: str | None = None
    organization_name: str | None = None
    organization_title: str | None = None
    location_name: str | None = None
    member_distance: str | None = None
    connection_count: int | None = None
    follower_count: int | None = None
    connected_at: str | None = None
    badges: dict[str, Any] = field(default_factory=dict)
    avatar_url: str | None = None
    first_seen_at: str | None = None
    last_visited_at: str | None = None


def lead_from_row(row: sqlite3.Row) -> Lead:
    badges = json.loads(row["badges_json"] or "{}")
    if not isinstance(badges, dict):
        badges = {}
    return Lead(
        id=row["id"],
        account_id=row["account_id"],
        full_name=row["full_name"],
        member_id=row["member_id"],
        public_id=row["public_id"],
        hash_id=row["hash_id"],
        first_name=row["first_name"],
        last_name=row["last_name"],
        headline=row["headline"],
        summary=row["summary"],
        organization_name=row["organization_name"],
        organization_title=row["organization_title"],
        location_name=row["location_name"],
        member_distance=row["member_distance"],
        connection_count=row["connection_count"],
        follower_count=row["follower_count"],
        connected_at=row["connected_at"],
        badges=badges,
        avatar_url=row["avatar_url"],
        first_seen_at=row["first_seen_at"],
        last_visited_at=row["last_visited_at"],
    )


def _validated_fields(fields: Mapping[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(fields) - WRITABLE_FIELDS)
    if unknown:
        raise ValueError(f"unknown lead fields: {', '.join(unknown)}")

    payload: dict[str, Any] = {}
    for name, value in fields.items():
        if name in IDENTITY_FIELDS:
            value = normalise_identifier(value)
        elif name == "badges":
            value = json.dumps(value or {}, sort_keys=True)
        payload[_FIELD_COLUMNS[name]] = value
    return payload


def _require_full_name(full_name: str) -> str:
    cleaned = (full_name or "").strip()
    if not cleaned:
        raise ValueError("full_name is required")
    return cleaned


def create_lead(
    conn: sqlite3.Connection,
    account_id: int,
    full_name: str,
    **fields: Any,
) -> Lead:
    """Insert a lead and return it.

    ``fields`` accepts any name in :data:`WRITABLE_FIELDS`. Creating a lead whose
    ``member_id`` or ``public_id`` sits on the global blacklist raises
    :class:`LeadBlacklistedError`, on every account.
    """
    payload = _validated_fields(fields)
    payload["full_name"] = _require_full_name(full_name)
    guard_identity(
        conn,
        member_id=payload.get("member_id"),
        public_id=payload.get("public_id"),
    )

    columns = ["account_id", *payload]
    values = [account_id, *payload.values()]
    placeholders = ", ".join("?" for _ in columns)
    cursor = conn.execute(
        f"INSERT INTO leads ({', '.join(columns)}) VALUES ({placeholders})",
        values,
    )
    conn.commit()

    created = get_lead(conn, int(cursor.lastrowid))
    if created is None:
        raise LeadNotFoundError(int(cursor.lastrowid))
    return created


def get_lead(conn: sqlite3.Connection, lead_id: int) -> Lead | None:
    """Read a single lead, blacklisted or not."""
    row = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
    return None if row is None else lead_from_row(row)


def get_lead_by_member_id(
    conn: sqlite3.Connection,
    account_id: int,
    member_id: str,
) -> Lead | None:
    row = conn.execute(
        "SELECT * FROM leads WHERE account_id = ? AND member_id = ?",
        (account_id, normalise_identifier(member_id)),
    ).fetchone()
    return None if row is None else lead_from_row(row)


def get_lead_by_public_id(
    conn: sqlite3.Connection,
    account_id: int,
    public_id: str,
) -> Lead | None:
    row = conn.execute(
        "SELECT * FROM leads WHERE account_id = ? AND public_id = ?",
        (account_id, normalise_identifier(public_id)),
    ).fetchone()
    return None if row is None else lead_from_row(row)


def update_lead(conn: sqlite3.Connection, lead_id: int, **fields: Any) -> Lead:
    """Update writable lead fields and return the stored lead.

    Profile data on a blacklisted lead is frozen: refreshing it would mean the
    lead was visited again, so the update is refused with
    :class:`LeadBlacklistedError`.
    """
    current = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
    if current is None:
        raise LeadNotFoundError(lead_id)

    payload = _validated_fields(fields)
    if "full_name" in payload:
        payload["full_name"] = _require_full_name(payload["full_name"])
    if not payload:
        return lead_from_row(current)

    guard_identity(
        conn,
        lead_id=lead_id,
        member_id=current["member_id"],
        public_id=current["public_id"],
    )
    guard_identity(
        conn,
        lead_id=lead_id,
        member_id=payload.get("member_id"),
        public_id=payload.get("public_id"),
    )

    assignments = ", ".join(f"{column} = ?" for column in payload)
    conn.execute(
        f"UPDATE leads SET {assignments} WHERE id = ?",
        [*payload.values(), lead_id],
    )
    conn.commit()

    updated = get_lead(conn, lead_id)
    if updated is None:
        raise LeadNotFoundError(lead_id)
    return updated


def delete_lead(conn: sqlite3.Connection, lead_id: int) -> bool:
    """Delete a lead and its tags, contacts and custom fields.

    Blacklist entries survive because they are keyed on the LinkedIn
    identifiers, so a deleted lead cannot be re-imported to bypass the block.
    """
    cursor = conn.execute("DELETE FROM leads WHERE id = ?", (lead_id,))
    conn.commit()
    return cursor.rowcount > 0


def list_leads(
    conn: sqlite3.Connection,
    account_id: int,
    *,
    include_blacklisted: bool = False,
    limit: int | None = None,
    offset: int = 0,
) -> list[Lead]:
    """List an account's leads, excluding blacklisted ones by default."""
    sql = "SELECT * FROM leads WHERE account_id = ?"
    params: list[Any] = [account_id]
    if not include_blacklisted:
        sql += f" AND {BLACKLIST_EXCLUSION_SQL}"
    sql += " ORDER BY leads.id"
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params += [limit, offset]

    rows = conn.execute(sql, params).fetchall()
    return [lead_from_row(row) for row in rows]


def count_leads(
    conn: sqlite3.Connection,
    account_id: int,
    *,
    include_blacklisted: bool = False,
) -> int:
    """Count an account's leads, excluding blacklisted ones by default."""
    sql = "SELECT COUNT(*) FROM leads WHERE account_id = ?"
    if not include_blacklisted:
        sql += f" AND {BLACKLIST_EXCLUSION_SQL}"
    return int(conn.execute(sql, (account_id,)).fetchone()[0])


def normalise_custom_field_key(key: str) -> str:
    """Return the storage key for a custom field.

    ``industry``, ``cs_industry`` and ``{cs_industry}`` all resolve to
    ``industry`` so template placeholders and stored keys cannot drift apart.
    """
    cleaned = (key or "").strip().strip("{}").strip().lower()
    if cleaned.startswith(CUSTOM_FIELD_PREFIX):
        cleaned = cleaned[len(CUSTOM_FIELD_PREFIX) :]
    if not cleaned:
        raise ValueError("custom field key must not be empty")
    return cleaned


def _require_lead(conn: sqlite3.Connection, lead_id: int) -> None:
    row = conn.execute("SELECT 1 FROM leads WHERE id = ?", (lead_id,)).fetchone()
    if row is None:
        raise LeadNotFoundError(lead_id)


def set_custom_field(
    conn: sqlite3.Connection,
    lead_id: int,
    key: str,
    value: Any,
) -> None:
    """Store or replace one ``{cs_*}`` custom field on a lead."""
    set_custom_fields(conn, lead_id, {key: value})


def set_custom_fields(
    conn: sqlite3.Connection,
    lead_id: int,
    values: Mapping[str, Any],
) -> None:
    """Store or replace several ``{cs_*}`` custom fields in one transaction."""
    _require_lead(conn, lead_id)
    rows = [
        (
            lead_id,
            normalise_custom_field_key(key),
            None if value is None else str(value),
        )
        for key, value in values.items()
    ]
    if not rows:
        return

    conn.executemany(
        """
        INSERT INTO lead_custom_fields (lead_id, key, value)
        VALUES (?, ?, ?)
        ON CONFLICT (lead_id, key) DO UPDATE SET value = excluded.value
        """,
        rows,
    )
    conn.commit()


def get_custom_fields(conn: sqlite3.Connection, lead_id: int) -> dict[str, str | None]:
    """Return a lead's custom fields keyed by their normalised names."""
    rows = conn.execute(
        "SELECT key, value FROM lead_custom_fields WHERE lead_id = ? ORDER BY key",
        (lead_id,),
    ).fetchall()
    return {row["key"]: row["value"] for row in rows}


def get_custom_field(
    conn: sqlite3.Connection,
    lead_id: int,
    key: str,
    default: str | None = None,
) -> str | None:
    row = conn.execute(
        "SELECT value FROM lead_custom_fields WHERE lead_id = ? AND key = ?",
        (lead_id, normalise_custom_field_key(key)),
    ).fetchone()
    if row is None or row["value"] is None:
        return default
    return row["value"]


def custom_field_tokens(conn: sqlite3.Connection, lead_id: int) -> dict[str, str]:
    """Return custom fields keyed as ``cs_*`` tokens for the template engine.

    SEQ-02 expands ``{cs_industry}``, so this returns ``{"cs_industry": "SaaS"}``
    with missing values rendered as empty strings.
    """
    return {
        f"{CUSTOM_FIELD_PREFIX}{key}": value or ""
        for key, value in get_custom_fields(conn, lead_id).items()
    }


def delete_custom_field(conn: sqlite3.Connection, lead_id: int, key: str) -> bool:
    cursor = conn.execute(
        "DELETE FROM lead_custom_fields WHERE lead_id = ? AND key = ?",
        (lead_id, normalise_custom_field_key(key)),
    )
    conn.commit()
    return cursor.rowcount > 0


def leads_from_rows(rows: Iterable[sqlite3.Row]) -> list[Lead]:
    return [lead_from_row(row) for row in rows]


def placeholders_for(values: Sequence[Any]) -> str:
    """Return a parameter placeholder list sized for ``values``."""
    return ", ".join("?" for _ in values)
