"""Archiving both sides of a conversation into `messages`.

The table already models this
-----------------------------
`messages` ships in `0001_init.sql` with a `direction` CHECK covering
`outbound` and `inbound`, a `thread_urn` and a `lead_id`. Nothing needed adding,
so nothing was added.

Never writing the same message twice
------------------------------------
A delta scan opens a thread again whenever anything in it changed, so the same
older messages are read again every time somebody replies. The identity of an
archived message is therefore
`(account_id, lead_id, thread_urn, direction, body, sent_at)`, and it is matched
as a *multiset* rather than as a set.

That distinction is the point. A set would collapse a person genuinely sending
"ok" twice into one row, losing a real message. A multiset counts how many
copies of each identity the thread now shows and how many the database already
holds, and inserts only the difference. Re-scanning an unchanged thread inserts
nothing; a genuine repeat inserts exactly one.

`detected_at` is the scan time and is deliberately outside the identity. When it
were part of the key every scan would look new, which is the exact bug the key
exists to prevent.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from linkedin_mcp.inbox.threads import INBOUND, OUTBOUND, InboxThread, ThreadMessage
from linkedin_mcp.sequences.transaction import now_timestamp, transaction

logger = logging.getLogger(__name__)

__all__ = [
    "ArchiveResult",
    "archive_thread",
    "existing_message_keys",
    "thread_messages",
]


@dataclass(frozen=True, slots=True)
class ArchiveResult:
    """What archiving one thread wrote, and what it recognised as already there."""

    thread_urn: str
    lead_id: int
    inserted: int = 0
    skipped: int = 0
    inbound: int = 0
    outbound: int = 0
    message_ids: tuple[int, ...] = ()

    @property
    def total(self) -> int:
        return self.inserted + self.skipped


def existing_message_keys(
    conn: sqlite3.Connection,
    account_id: int,
    lead_id: int,
    thread_urn: str,
) -> Counter[tuple[str, str, str | None]]:
    """Count the archived copies of each message identity in one thread."""
    rows = conn.execute(
        """
        SELECT direction, body, sent_at
        FROM messages
        WHERE account_id = ? AND lead_id = ? AND thread_urn = ?
        """,
        (account_id, lead_id, thread_urn),
    ).fetchall()
    return Counter((row["direction"], row["body"], row["sent_at"]) for row in rows)


def thread_messages(messages: Iterable[ThreadMessage]) -> tuple[ThreadMessage, ...]:
    """Return the messages worth archiving, dropping anything with no body."""
    return tuple(message for message in messages if message.body)


def archive_thread(
    conn: sqlite3.Connection,
    account_id: int,
    lead_id: int,
    thread: InboxThread,
    *,
    detected_at: datetime | str | None = None,
    messages: Sequence[ThreadMessage] | None = None,
) -> ArchiveResult:
    """Store both directions of one conversation, without ever duplicating a row.

    Args:
        conn: Open connection to the MCP database.
        account_id: Account whose inbox the thread was read from.
        lead_id: Lead the conversation is with. `messages.lead_id` is not
            nullable, so a thread whose participant does not resolve to a lead
            is reported by the scanner rather than archived against a guess.
        thread: The conversation, carrying its messages.
        detected_at: When this scan saw them. Not part of the identity.
        messages: Override the thread's own messages, for callers that read them
            separately.
    """
    candidates = thread_messages(
        thread.messages if messages is None else messages
    )
    if not candidates:
        return ArchiveResult(thread_urn=thread.thread_urn, lead_id=lead_id)

    moment = now_timestamp(detected_at)
    inserted_ids: list[int] = []
    skipped = 0
    inbound = 0
    outbound = 0

    with transaction(conn):
        already = existing_message_keys(conn, account_id, lead_id, thread.thread_urn)
        for message in candidates:
            key = message.archive_key
            if already[key] > 0:
                already[key] -= 1
                skipped += 1
                continue
            cursor = conn.execute(
                """
                INSERT INTO messages
                    (account_id, lead_id, direction, body, thread_urn, sent_at, detected_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    account_id,
                    lead_id,
                    message.direction,
                    message.body,
                    thread.thread_urn,
                    message.sent_at,
                    moment,
                ),
            )
            inserted_ids.append(int(cursor.lastrowid))
            if message.direction == INBOUND:
                inbound += 1
            elif message.direction == OUTBOUND:
                outbound += 1

    logger.debug(
        "Archived %d new and recognised %d existing message(s) in thread %s",
        len(inserted_ids),
        skipped,
        thread.thread_urn,
    )
    return ArchiveResult(
        thread_urn=thread.thread_urn,
        lead_id=lead_id,
        inserted=len(inserted_ids),
        skipped=skipped,
        inbound=inbound,
        outbound=outbound,
        message_ids=tuple(inserted_ids),
    )
