"""Global do-not-contact blacklist.

Every entry records the account that added it, but enforcement ignores that
account on purpose. A lead blacklisted once by any account is blocked for every
account and every campaign, which is stricter than a per-campaign exclude list.
Matching uses the durable LinkedIn identifiers (``member_id`` and ``public_id``)
rather than a lead row id, so deleting and re-importing a lead never clears the
block.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from linkedin_mcp.leads.errors import LeadBlacklistedError, LeadNotFoundError


__all__ = [
    "BLACKLIST_EXCLUSION_SQL",
    "BlacklistEntry",
    "blacklist_identity",
    "blacklist_lead",
    "entry_from_row",
    "find_entry",
    "get_entry",
    "guard_identity",
    "is_blacklisted",
    "is_blacklisted_by_member_id",
    "is_blacklisted_by_public_id",
    "is_identity_blacklisted",
    "list_blacklist",
    "normalise_identifier",
    "remove_from_blacklist",
    "remove_lead_from_blacklist",
]

BLACKLIST_EXCLUSION_SQL = (
    "NOT EXISTS ("
    " SELECT 1 FROM blacklist AS bl"
    " WHERE (bl.member_id IS NOT NULL AND bl.member_id = leads.member_id)"
    " OR (bl.public_id IS NOT NULL AND bl.public_id = leads.public_id)"
    ")"
)


@dataclass(frozen=True, slots=True)
class BlacklistEntry:
    id: int
    account_id: int
    member_id: str | None = None
    public_id: str | None = None
    reason: str | None = None
    added_at: str | None = None


def normalise_identifier(value: str | None) -> str | None:
    """Return a trimmed identifier, collapsing blank values to ``None``."""
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def entry_from_row(row: sqlite3.Row) -> BlacklistEntry:
    return BlacklistEntry(
        id=row["id"],
        account_id=row["account_id"],
        member_id=row["member_id"],
        public_id=row["public_id"],
        reason=row["reason"],
        added_at=row["added_at"],
    )


def is_identity_blacklisted(
    conn: sqlite3.Connection,
    *,
    member_id: str | None = None,
    public_id: str | None = None,
) -> bool:
    """Return True when the identity is blacklisted on any account."""
    member_id = normalise_identifier(member_id)
    public_id = normalise_identifier(public_id)
    if member_id is None and public_id is None:
        return False

    row = conn.execute(
        """
        SELECT 1
        FROM blacklist
        WHERE (? IS NOT NULL AND member_id = ?)
           OR (? IS NOT NULL AND public_id = ?)
        LIMIT 1
        """,
        (member_id, member_id, public_id, public_id),
    ).fetchone()
    return row is not None


def is_blacklisted_by_member_id(conn: sqlite3.Connection, member_id: str | None) -> bool:
    """Return True when the LinkedIn member id is blacklisted on any account."""
    return is_identity_blacklisted(conn, member_id=member_id)


def is_blacklisted_by_public_id(conn: sqlite3.Connection, public_id: str | None) -> bool:
    """Return True when the LinkedIn public id is blacklisted on any account."""
    return is_identity_blacklisted(conn, public_id=public_id)


def is_blacklisted(conn: sqlite3.Connection, account_id: int, lead_id: int) -> bool:
    """Return True when the lead must never be contacted.

    ``account_id`` scopes the lead lookup only. The blacklist match itself is
    global, so a lead blacklisted through one account is reported as blacklisted
    for all of them. A lead that does not resolve for the account fails closed
    and returns True.
    """
    row = conn.execute(
        "SELECT member_id, public_id FROM leads WHERE id = ? AND account_id = ?",
        (lead_id, account_id),
    ).fetchone()
    if row is None:
        return True
    return is_identity_blacklisted(
        conn,
        member_id=row["member_id"],
        public_id=row["public_id"],
    )


def guard_identity(
    conn: sqlite3.Connection,
    *,
    member_id: str | None = None,
    public_id: str | None = None,
    lead_id: int | None = None,
) -> None:
    """Raise :class:`LeadBlacklistedError` when the identity is blacklisted."""
    if is_identity_blacklisted(conn, member_id=member_id, public_id=public_id):
        raise LeadBlacklistedError(
            lead_id=lead_id,
            member_id=normalise_identifier(member_id),
            public_id=normalise_identifier(public_id),
        )


def find_entry(
    conn: sqlite3.Connection,
    account_id: int,
    *,
    member_id: str | None = None,
    public_id: str | None = None,
) -> BlacklistEntry | None:
    """Return the entry an account already holds for the identity, if any."""
    member_id = normalise_identifier(member_id)
    public_id = normalise_identifier(public_id)
    if member_id is None and public_id is None:
        return None

    row = conn.execute(
        """
        SELECT *
        FROM blacklist
        WHERE account_id = ?
          AND ((? IS NOT NULL AND member_id = ?) OR (? IS NOT NULL AND public_id = ?))
        LIMIT 1
        """,
        (account_id, member_id, member_id, public_id, public_id),
    ).fetchone()
    return None if row is None else entry_from_row(row)


def get_entry(conn: sqlite3.Connection, entry_id: int) -> BlacklistEntry:
    row = conn.execute("SELECT * FROM blacklist WHERE id = ?", (entry_id,)).fetchone()
    if row is None:
        raise LookupError(f"blacklist entry {entry_id} does not exist")
    return entry_from_row(row)


def blacklist_identity(
    conn: sqlite3.Connection,
    account_id: int,
    *,
    member_id: str | None = None,
    public_id: str | None = None,
    reason: str | None = None,
) -> BlacklistEntry:
    """Blacklist a LinkedIn identity that may not exist as a lead yet."""
    member_id = normalise_identifier(member_id)
    public_id = normalise_identifier(public_id)
    if member_id is None and public_id is None:
        raise ValueError(
            "a blacklist entry needs a member_id or a public_id to stay "
            "enforceable across accounts"
        )

    existing = find_entry(conn, account_id, member_id=member_id, public_id=public_id)
    if existing is not None:
        conn.execute(
            """
            UPDATE blacklist
            SET member_id = COALESCE(member_id, ?),
                public_id = COALESCE(public_id, ?),
                reason = COALESCE(?, reason)
            WHERE id = ?
            """,
            (member_id, public_id, reason, existing.id),
        )
        conn.commit()
        return get_entry(conn, existing.id)

    cursor = conn.execute(
        """
        INSERT INTO blacklist (account_id, member_id, public_id, reason)
        VALUES (?, ?, ?, ?)
        """,
        (account_id, member_id, public_id, reason),
    )
    conn.commit()
    return get_entry(conn, int(cursor.lastrowid))


def blacklist_lead(
    conn: sqlite3.Connection,
    lead_id: int,
    *,
    reason: str | None = None,
) -> BlacklistEntry:
    """Blacklist the identity behind a stored lead.

    The lead row is left in place so the CRM keeps its history. Deleting that
    row later does not lift the block, because the entry is keyed on the
    LinkedIn identifiers rather than the row id.
    """
    row = conn.execute(
        "SELECT account_id, member_id, public_id FROM leads WHERE id = ?",
        (lead_id,),
    ).fetchone()
    if row is None:
        raise LeadNotFoundError(lead_id)

    member_id = normalise_identifier(row["member_id"])
    public_id = normalise_identifier(row["public_id"])
    if member_id is None and public_id is None:
        raise ValueError(
            f"lead {lead_id} has no member_id or public_id, so it cannot be "
            "blacklisted globally"
        )

    return blacklist_identity(
        conn,
        row["account_id"],
        member_id=member_id,
        public_id=public_id,
        reason=reason,
    )


def list_blacklist(
    conn: sqlite3.Connection,
    account_id: int | None = None,
) -> list[BlacklistEntry]:
    """List entries, optionally narrowed to the account that added them."""
    if account_id is None:
        rows = conn.execute("SELECT * FROM blacklist ORDER BY id").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM blacklist WHERE account_id = ? ORDER BY id",
            (account_id,),
        ).fetchall()
    return [entry_from_row(row) for row in rows]


def remove_from_blacklist(
    conn: sqlite3.Connection,
    *,
    member_id: str | None = None,
    public_id: str | None = None,
) -> int:
    """Delete matching entries on every account and return the row count.

    Removal is global for the same reason enforcement is: leaving another
    account's entry behind would keep the lead blocked while looking cleared.
    """
    member_id = normalise_identifier(member_id)
    public_id = normalise_identifier(public_id)
    if member_id is None and public_id is None:
        raise ValueError("provide a member_id or a public_id to clear")

    cursor = conn.execute(
        """
        DELETE FROM blacklist
        WHERE (? IS NOT NULL AND member_id = ?)
           OR (? IS NOT NULL AND public_id = ?)
        """,
        (member_id, member_id, public_id, public_id),
    )
    conn.commit()
    return cursor.rowcount


def remove_lead_from_blacklist(conn: sqlite3.Connection, lead_id: int) -> int:
    """Clear the entries matching a stored lead's identity."""
    row = conn.execute(
        "SELECT member_id, public_id FROM leads WHERE id = ?",
        (lead_id,),
    ).fetchone()
    if row is None:
        raise LeadNotFoundError(lead_id)
    return remove_from_blacklist(
        conn,
        member_id=row["member_id"],
        public_id=row["public_id"],
    )
