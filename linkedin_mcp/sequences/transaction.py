"""One transaction helper, used by every write in this package.

The headline requirement of SEQ-01 is that a crash mid-step cannot strand a lead
in `processing`. That is only true if the `campaign_leads` write and the `jobs`
write land together or not at all, so every transition in this package runs
inside :func:`transaction`.

The context manager nests. The outermost block owns the real transaction and is
the only one that commits or rolls back; an inner block takes a `SAVEPOINT` and
unwinds to it on failure. That matters for composition: if SEQ-04 wraps several
transitions in one transaction and catches a failure from one of them, the failed
transition's half-written rows are already gone by the time the handler runs, so
committing the rest cannot preserve them.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from itertools import count

from linkedin_mcp.audit import utc_timestamp

__all__ = [
    "as_timestamp",
    "now_timestamp",
    "shift_timestamp",
    "transaction",
    "utc_now",
]

_STORED_FORMAT = "%Y-%m-%d %H:%M:%S"
_SAVEPOINTS = count()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block as one atomic write, nesting safely inside an enclosing one.

    `BEGIN IMMEDIATE` takes the write lock up front, so two workers racing for the
    same lead serialise here instead of one of them discovering a conflict after
    it has already decided what to write.
    """
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()
        return

    savepoint = f"seq_{next(_SAVEPOINTS)}"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        yield conn
    except BaseException:
        # ROLLBACK TO unwinds the block's writes but leaves the savepoint in
        # place, so it still has to be released for the stack to stay balanced.
        conn.execute(f"ROLLBACK TO {savepoint}")
        conn.execute(f"RELEASE {savepoint}")
        raise
    else:
        conn.execute(f"RELEASE {savepoint}")


def utc_now() -> datetime:
    """Return the current instant, timezone-aware, in UTC."""
    return datetime.now(timezone.utc)


def as_timestamp(moment: datetime | str | None) -> str:
    """Coerce a moment to the one timestamp format the schema stores.

    Every column in this schema holds a UTC string comparable with SQLite's
    `CURRENT_TIMESTAMP`, so this package never invents a second format.
    """
    if moment is None:
        return utc_timestamp()
    if isinstance(moment, str):
        return moment
    return utc_timestamp(moment)


def now_timestamp(now: datetime | str | None = None) -> str:
    """Return `now` as a stored timestamp, defaulting to the current instant."""
    return as_timestamp(now)


def shift_timestamp(moment: datetime | str | None, seconds: float) -> str:
    """Return a stored timestamp `seconds` after `moment`."""
    if isinstance(moment, datetime):
        base = moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)
    elif isinstance(moment, str):
        base = datetime.strptime(moment, _STORED_FORMAT).replace(tzinfo=timezone.utc)
    else:
        base = utc_now()
    return utc_timestamp(base + timedelta(seconds=seconds))
