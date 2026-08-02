"""What `linkedin://inbox/unread` means, given a schema with no read flag.

The problem
-----------
`linkedin_mcp.inbox` has no database-backed reader for message content. Every
function that reads a message (`extract_threads`, `list_thread_rows`,
`read_thread_messages`, `open_thread`, `run_inbox_scan`) takes a Playwright
`page`. The ones that take a `sqlite3.Connection` are scan plumbing: matching a
thread to a lead, archiving what a scan found, deciding when the next scan is
due.

And `messages` has no read marker. Its columns are exactly `id`, `account_id`,
`lead_id`, `direction`, `body`, `thread_urn`, `sent_at` and `detected_at`. There
is nowhere to store "a human has seen this", and nothing on the site is scraped
that would tell us.

A resource must not open a browser (see
`tests/test_actions.py::test_no_mcp_tool_in_the_server_can_drive_playwright`,
which since #27 walks every `@mcp.resource` as well as every `@mcp.tool`),
so this URI cannot be satisfied by asking LinkedIn. It has to be defined from
what the schema supports.

The definition this module implements
-------------------------------------
**Unread means: the lead's newest message is inbound.** In plainer words, they
replied and we have not answered yet.

That is the question an operator is actually asking when they open an inbox in a
tool like this one. It is answerable from `direction` and a timestamp, both of
which the schema already has, and it self-heals: the moment the next scan
records an outbound message to that lead, the thread drops off the list.

What this definition excludes, said out loud
--------------------------------------------
- **LinkedIn's own unread badge.** We never scrape it and there is no column for
  it. A thread this repository has never scanned does not appear here at all.
- **A reply that was answered outside this system**, until the next inbox scan
  records the outbound message. Between the human replying in LinkedIn's own UI
  and the next scan, this over-reports. Over-reporting is the safe direction for
  an inbox: a thread shown twice costs a glance, a thread never shown costs a
  reply.
- **Anything older than the scan history.** `run_inbox_scan` archives what it
  read; this reads what was archived.

No migration
------------
A `messages.read_at` column would model a human dismissing a thread, which is a
different feature with its own write path, its own MCP tool and its own
human-in-the-loop question. This issue exposes a read surface. Adding a fourth
migration to it would put a column in the schema that nothing writes.

Ordering
--------
`sent_at` is what LinkedIn showed and `detected_at` is when a scan saw it. Either
can be NULL, so both messages in a comparison are keyed on
`COALESCE(sent_at, detected_at, '')` with the row id as the tie-break. Ids within
a thread are assigned in the order the scan read the thread, so the tie-break is
the site's own ordering rather than an arbitrary one.
"""

from __future__ import annotations

import sqlite3
from typing import Any

__all__ = [
    "DEFAULT_UNREAD_LIMIT",
    "UNREAD_DEFINITION",
    "unread_threads",
    "unread_thread_count",
]

DEFAULT_UNREAD_LIMIT = 50
"""How many threads one read returns. An inbox is a worklist, not an archive."""

UNREAD_DEFINITION = (
    "a lead whose newest stored message is inbound: they replied and no "
    "outbound message to them has been recorded since"
)
"""Returned in the payload, so a client never has to guess what it is holding."""

_SORT_KEY = "COALESCE(sent_at, detected_at, '') || '#' || printf('%012d', id)"

_UNREAD_SQL = f"""
WITH keyed AS (
    SELECT
        id,
        lead_id,
        direction,
        body,
        thread_urn,
        sent_at,
        detected_at,
        {_SORT_KEY} AS sort_key
    FROM messages
    WHERE account_id = ?
),
newest AS (
    SELECT
        lead_id,
        MAX(CASE WHEN direction = 'inbound' THEN sort_key END) AS inbound_key,
        MAX(CASE WHEN direction = 'outbound' THEN sort_key END) AS outbound_key,
        SUM(CASE WHEN direction = 'inbound' THEN 1 ELSE 0 END) AS inbound_count,
        SUM(CASE WHEN direction = 'outbound' THEN 1 ELSE 0 END) AS outbound_count
    FROM keyed
    GROUP BY lead_id
)
SELECT
    keyed.id AS message_id,
    newest.lead_id AS lead_id,
    keyed.body AS body,
    keyed.thread_urn AS thread_urn,
    keyed.sent_at AS sent_at,
    keyed.detected_at AS detected_at,
    newest.inbound_count AS inbound_count,
    newest.outbound_count AS outbound_count,
    newest.outbound_key AS outbound_key,
    leads.full_name AS full_name,
    leads.headline AS headline,
    leads.public_id AS public_id,
    leads.organization_name AS organization_name
FROM newest
JOIN keyed ON keyed.lead_id = newest.lead_id AND keyed.sort_key = newest.inbound_key
LEFT JOIN leads ON leads.id = newest.lead_id
WHERE newest.inbound_key IS NOT NULL
  AND (newest.outbound_key IS NULL OR newest.inbound_key > newest.outbound_key)
ORDER BY newest.inbound_key DESC
"""

_COUNT_SQL = f"""
WITH keyed AS (
    SELECT lead_id, direction, {_SORT_KEY} AS sort_key
    FROM messages
    WHERE account_id = ?
),
newest AS (
    SELECT
        lead_id,
        MAX(CASE WHEN direction = 'inbound' THEN sort_key END) AS inbound_key,
        MAX(CASE WHEN direction = 'outbound' THEN sort_key END) AS outbound_key
    FROM keyed
    GROUP BY lead_id
)
SELECT COUNT(*) AS total
FROM newest
WHERE inbound_key IS NOT NULL
  AND (outbound_key IS NULL OR inbound_key > outbound_key)
"""

_PREVIEW_CHARS = 280


def _preview(body: str | None) -> str:
    """Return a one-glance excerpt of a reply, collapsed onto one line."""
    text = " ".join((body or "").split())
    if len(text) <= _PREVIEW_CHARS:
        return text
    return text[: _PREVIEW_CHARS - 1].rstrip() + "\u2026"


def _thread(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "lead_id": int(row["lead_id"]),
        "full_name": row["full_name"],
        "headline": row["headline"],
        "public_id": row["public_id"],
        "organization_name": row["organization_name"],
        "thread_urn": row["thread_urn"],
        "message_id": int(row["message_id"]),
        "received_at": row["sent_at"] or row["detected_at"],
        "sent_at": row["sent_at"],
        "detected_at": row["detected_at"],
        "preview": _preview(row["body"]),
        "inbound_messages": int(row["inbound_count"]),
        "outbound_messages": int(row["outbound_count"]),
        "ever_contacted": row["outbound_key"] is not None,
    }


def unread_threads(
    conn: sqlite3.Connection,
    account_id: int,
    *,
    limit: int | None = DEFAULT_UNREAD_LIMIT,
) -> list[dict[str, Any]]:
    """Return the threads whose newest stored message is inbound, newest first.

    One query, no browser, no Playwright import. See the module docstring for
    what "unread" means here and what it deliberately does not cover.
    """
    sql = _UNREAD_SQL
    params: list[Any] = [account_id]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    return [_thread(row) for row in conn.execute(sql, params).fetchall()]


def unread_thread_count(conn: sqlite3.Connection, account_id: int) -> int:
    """Count every unread thread, ignoring the page limit."""
    row = conn.execute(_COUNT_SQL, (account_id,)).fetchone()
    return 0 if row is None else int(row["total"])
