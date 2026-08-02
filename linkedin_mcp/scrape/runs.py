"""Bookkeeping for one extraction run, stored in `harvest_runs`.

DB-01 already ships a `harvest_runs` table, so a run gets a row there rather
than a new table. The row carries the filters that produced it and the cursor
the run stopped on, which is what makes a stopped run resumable: a background
runner reads the row, hands the cursor back to `paginate`, and carries on from
the page the gate interrupted.

`actions_log` stays untouched by this module. That ledger is append-only and
belongs to `linkedin_mcp.audit`; this is run metadata, not an action count.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from linkedin_mcp.scrape.paginate import SearchCursor

__all__ = [
    "SOURCE_GROUP_MEMBERS",
    "SOURCE_PEOPLE_SEARCH",
    "SOURCE_POST_SEARCH",
    "finish_harvest_run",
    "harvest_run",
    "resume_cursor",
    "start_harvest_run",
]

SOURCE_PEOPLE_SEARCH = "people_search"
SOURCE_POST_SEARCH = "post_search"
SOURCE_GROUP_MEMBERS = "group_members"

TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


def _stamp(moment: datetime | str | None) -> str:
    if isinstance(moment, str):
        return moment
    when = moment or datetime.now(timezone.utc)
    if when.tzinfo is not None:
        when = when.astimezone(timezone.utc)
    return when.strftime(TIMESTAMP_FORMAT)


def start_harvest_run(
    conn: sqlite3.Connection,
    account_id: int,
    source_type: str,
    params: Mapping[str, Any] | None = None,
    *,
    cursor: SearchCursor | None = None,
    started_at: datetime | str | None = None,
) -> int:
    """Open a run row and return its id."""
    payload = {
        "filters": dict(params or {}),
        "cursor": (cursor or SearchCursor()).as_dict(),
    }
    inserted = conn.execute(
        """
        INSERT INTO harvest_runs (account_id, source_type, params_json, started_at)
        VALUES (?, ?, ?, ?)
        """,
        (account_id, source_type, json.dumps(payload, sort_keys=True), _stamp(started_at)),
    )
    conn.commit()
    return int(inserted.lastrowid)


def finish_harvest_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    found: int,
    new: int,
    cursor: SearchCursor | None = None,
    params: Mapping[str, Any] | None = None,
    finished_at: datetime | str | None = None,
) -> None:
    """Close a run row with its counts and the cursor it stopped on."""
    existing = harvest_run(conn, run_id)
    if existing is None:
        raise LookupError(f"harvest run {run_id} does not exist")
    payload = dict(existing["params"])
    if params is not None:
        payload["filters"] = dict(params)
    payload["cursor"] = (cursor or SearchCursor()).as_dict()
    conn.execute(
        """
        UPDATE harvest_runs
        SET found_count = ?, new_count = ?, params_json = ?, finished_at = ?
        WHERE id = ?
        """,
        (
            int(found),
            int(new),
            json.dumps(payload, sort_keys=True),
            _stamp(finished_at),
            run_id,
        ),
    )
    conn.commit()


def harvest_run(conn: sqlite3.Connection, run_id: int) -> dict[str, Any] | None:
    """Read one run row with its parameters decoded."""
    row = conn.execute(
        "SELECT * FROM harvest_runs WHERE id = ?", (run_id,)
    ).fetchone()
    if row is None:
        return None
    try:
        params = json.loads(row["params_json"] or "{}")
    except json.JSONDecodeError:
        params = {}
    if not isinstance(params, dict):
        params = {}
    return {
        "id": row["id"],
        "account_id": row["account_id"],
        "source_type": row["source_type"],
        "params": params,
        "found_count": row["found_count"],
        "new_count": row["new_count"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
    }


def resume_cursor(conn: sqlite3.Connection, run_id: int) -> SearchCursor:
    """Return the cursor a previous run stopped on, so the next one continues."""
    run = harvest_run(conn, run_id)
    if run is None:
        raise LookupError(f"harvest run {run_id} does not exist")
    return SearchCursor.from_dict(run["params"].get("cursor"))
