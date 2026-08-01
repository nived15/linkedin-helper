"""Free-form lead tagging and tag-driven audience queries.

Tags are scoped to an account and stored lower case, so ``Hot Lead`` and
``hot lead`` address the same tag when a campaign builds its audience. Every
audience query drops globally blacklisted leads unless asked otherwise.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from linkedin_mcp.leads.blacklist import BLACKLIST_EXCLUSION_SQL
from linkedin_mcp.leads.errors import LeadNotFoundError
from linkedin_mcp.leads.store import Lead, leads_from_rows, placeholders_for


__all__ = [
    "Tag",
    "add_tag",
    "add_tags",
    "count_leads_with_tag",
    "delete_tag",
    "ensure_tag",
    "get_tag",
    "lead_tag_names",
    "leads_with_all_tags",
    "leads_with_any_tags",
    "leads_with_tag",
    "list_tags",
    "normalise_tag_name",
    "normalise_tag_names",
    "remove_tag",
    "tag_from_row",
]


@dataclass(frozen=True, slots=True)
class Tag:
    id: int
    account_id: int
    name: str
    color: str | None = None


def normalise_tag_name(name: str) -> str:
    """Return the canonical storage form of a tag name."""
    cleaned = " ".join((name or "").split()).lower()
    if not cleaned:
        raise ValueError("tag name must not be empty")
    return cleaned


def normalise_tag_names(names: Iterable[str]) -> list[str]:
    seen: dict[str, None] = {}
    for name in names:
        seen.setdefault(normalise_tag_name(name), None)
    if not seen:
        raise ValueError("provide at least one tag name")
    return list(seen)


def tag_from_row(row: sqlite3.Row) -> Tag:
    return Tag(
        id=row["id"],
        account_id=row["account_id"],
        name=row["name"],
        color=row["color"],
    )


def get_tag(conn: sqlite3.Connection, account_id: int, name: str) -> Tag | None:
    row = conn.execute(
        "SELECT * FROM tags WHERE account_id = ? AND name = ?",
        (account_id, normalise_tag_name(name)),
    ).fetchone()
    return None if row is None else tag_from_row(row)


def ensure_tag(
    conn: sqlite3.Connection,
    account_id: int,
    name: str,
    *,
    color: str | None = None,
) -> Tag:
    """Return the account's tag, creating it when it does not exist yet."""
    canonical = normalise_tag_name(name)
    conn.execute(
        """
        INSERT INTO tags (account_id, name, color)
        VALUES (?, ?, ?)
        ON CONFLICT (account_id, name) DO UPDATE
        SET color = COALESCE(excluded.color, tags.color)
        """,
        (account_id, canonical, color),
    )
    conn.commit()

    tag = get_tag(conn, account_id, canonical)
    if tag is None:
        raise LookupError(f"tag {canonical!r} could not be created")
    return tag


def list_tags(conn: sqlite3.Connection, account_id: int) -> list[Tag]:
    rows = conn.execute(
        "SELECT * FROM tags WHERE account_id = ? ORDER BY name",
        (account_id,),
    ).fetchall()
    return [tag_from_row(row) for row in rows]


def delete_tag(conn: sqlite3.Connection, account_id: int, name: str) -> bool:
    """Delete a tag and detach it from every lead that carried it."""
    cursor = conn.execute(
        "DELETE FROM tags WHERE account_id = ? AND name = ?",
        (account_id, normalise_tag_name(name)),
    )
    conn.commit()
    return cursor.rowcount > 0


def _lead_account_id(conn: sqlite3.Connection, lead_id: int) -> int:
    row = conn.execute(
        "SELECT account_id FROM leads WHERE id = ?",
        (lead_id,),
    ).fetchone()
    if row is None:
        raise LeadNotFoundError(lead_id)
    return int(row["account_id"])


def add_tag(
    conn: sqlite3.Connection,
    lead_id: int,
    name: str,
    *,
    applied_by: str | None = None,
    color: str | None = None,
) -> Tag:
    """Apply one tag to a lead, creating the tag on first use."""
    account_id = _lead_account_id(conn, lead_id)
    tag = ensure_tag(conn, account_id, name, color=color)
    conn.execute(
        """
        INSERT OR IGNORE INTO lead_tags (lead_id, tag_id, applied_by)
        VALUES (?, ?, ?)
        """,
        (lead_id, tag.id, applied_by),
    )
    conn.commit()
    return tag


def add_tags(
    conn: sqlite3.Connection,
    lead_id: int,
    names: Iterable[str],
    *,
    applied_by: str | None = None,
) -> list[Tag]:
    """Apply several tags to a lead."""
    account_id = _lead_account_id(conn, lead_id)
    tags = [ensure_tag(conn, account_id, name) for name in normalise_tag_names(names)]
    conn.executemany(
        """
        INSERT OR IGNORE INTO lead_tags (lead_id, tag_id, applied_by)
        VALUES (?, ?, ?)
        """,
        [(lead_id, tag.id, applied_by) for tag in tags],
    )
    conn.commit()
    return tags


def remove_tag(conn: sqlite3.Connection, lead_id: int, name: str) -> bool:
    """Detach a tag from a lead, leaving the tag itself in place."""
    cursor = conn.execute(
        """
        DELETE FROM lead_tags
        WHERE lead_id = ?
          AND tag_id IN (
              SELECT tags.id
              FROM tags
              JOIN leads ON leads.account_id = tags.account_id
              WHERE leads.id = ? AND tags.name = ?
          )
        """,
        (lead_id, lead_id, normalise_tag_name(name)),
    )
    conn.commit()
    return cursor.rowcount > 0


def lead_tag_names(conn: sqlite3.Connection, lead_id: int) -> list[str]:
    rows = conn.execute(
        """
        SELECT tags.name
        FROM lead_tags
        JOIN tags ON tags.id = lead_tags.tag_id
        WHERE lead_tags.lead_id = ?
        ORDER BY tags.name
        """,
        (lead_id,),
    ).fetchall()
    return [row["name"] for row in rows]


def _tagged_leads_sql(
    names: Sequence[str],
    *,
    include_blacklisted: bool,
    match_all: bool,
) -> str:
    sql = (
        "SELECT leads.* FROM leads"
        " JOIN lead_tags ON lead_tags.lead_id = leads.id"
        " JOIN tags ON tags.id = lead_tags.tag_id"
        " WHERE leads.account_id = ?"
        f" AND tags.name IN ({placeholders_for(names)})"
    )
    if not include_blacklisted:
        sql += f" AND {BLACKLIST_EXCLUSION_SQL}"
    sql += " GROUP BY leads.id"
    if match_all:
        sql += " HAVING COUNT(DISTINCT tags.name) = ?"
    sql += " ORDER BY leads.id"
    return sql


def leads_with_any_tags(
    conn: sqlite3.Connection,
    account_id: int,
    names: Iterable[str],
    *,
    include_blacklisted: bool = False,
) -> list[Lead]:
    """Return leads carrying at least one of the tags."""
    canonical = normalise_tag_names(names)
    params: list[Any] = [account_id, *canonical]
    sql = _tagged_leads_sql(
        canonical,
        include_blacklisted=include_blacklisted,
        match_all=False,
    )
    return leads_from_rows(conn.execute(sql, params).fetchall())


def leads_with_all_tags(
    conn: sqlite3.Connection,
    account_id: int,
    names: Iterable[str],
    *,
    include_blacklisted: bool = False,
) -> list[Lead]:
    """Return leads carrying every one of the tags."""
    canonical = normalise_tag_names(names)
    params: list[Any] = [account_id, *canonical, len(canonical)]
    sql = _tagged_leads_sql(
        canonical,
        include_blacklisted=include_blacklisted,
        match_all=True,
    )
    return leads_from_rows(conn.execute(sql, params).fetchall())


def leads_with_tag(
    conn: sqlite3.Connection,
    account_id: int,
    name: str,
    *,
    include_blacklisted: bool = False,
) -> list[Lead]:
    """Return leads carrying a single tag."""
    return leads_with_any_tags(
        conn,
        account_id,
        [name],
        include_blacklisted=include_blacklisted,
    )


def count_leads_with_tag(
    conn: sqlite3.Connection,
    account_id: int,
    name: str,
    *,
    include_blacklisted: bool = False,
) -> int:
    sql = (
        "SELECT COUNT(DISTINCT leads.id) FROM leads"
        " JOIN lead_tags ON lead_tags.lead_id = leads.id"
        " JOIN tags ON tags.id = lead_tags.tag_id"
        " WHERE leads.account_id = ? AND tags.name = ?"
    )
    if not include_blacklisted:
        sql += f" AND {BLACKLIST_EXCLUSION_SQL}"
    row = conn.execute(sql, (account_id, normalise_tag_name(name))).fetchone()
    return int(row[0])
