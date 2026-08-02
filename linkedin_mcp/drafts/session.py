"""Where the draft tools get their database connection.

The MCP entry point owns process wiring, and `linkedin_browser_mcp.py` belongs to
MCP-02 (#25), so this package resolves its own connection instead of being handed
one. The resolver is replaceable, which is what makes the tools testable against
a temporary database without a monkeypatch.

Resolution order:

1. a connection set with :func:`set_draft_connection`
2. a factory passed to :func:`linkedin_mcp.drafts.tools.register_draft_tools`
3. the process-wide audit log's connection, which is already open on the same
   database with migrations applied

Point 3 is deliberate. Opening a second connection to the same SQLite file would
mean two write locks and a `database is locked` waiting to happen under the
worker, and the audit log has to exist for any tool to run anyway.
"""

from __future__ import annotations

import sqlite3

from linkedin_mcp.audit.log import get_audit_log


__all__ = [
    "get_draft_connection",
    "reset_draft_connection",
    "set_draft_connection",
]

_connection: sqlite3.Connection | None = None


def set_draft_connection(conn: sqlite3.Connection | None) -> None:
    """Override the connection the draft tools use."""
    global _connection
    _connection = conn


def reset_draft_connection() -> None:
    """Fall back to the audit log's connection."""
    set_draft_connection(None)


def get_draft_connection() -> sqlite3.Connection:
    """Return the connection the draft tools should read and write."""
    if _connection is not None:
        return _connection
    return get_audit_log().connection
