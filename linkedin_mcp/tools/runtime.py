"""Shared plumbing for the MCP-02 tool surface.

Two seams and one convention, kept in one place so every tool resolves its
database and its account the same way.

The connection is the audit log's. `AuditLog.open` already applies migrations
and holds a connection to the one MCP database, so borrowing it means these
tools cannot drift onto a second file, and a test that swaps the audit log with
`set_audit_log` has swapped the tools' database too.

The account is `linkedin_mcp.audit.current_account_id`, which is the same
resolution the audit decorator uses, so a tool result and its `actions_log` row
can never disagree about who acted.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Mapping
from typing import Any

from linkedin_mcp.audit import current_account_id
from linkedin_mcp.audit.log import get_audit_log

logger = logging.getLogger(__name__)

__all__ = [
    "choice",
    "error_result",
    "positive_int",
    "tool_account_id",
    "tool_connection",
]


def tool_connection() -> sqlite3.Connection:
    """Return the open connection these tools read and write through."""
    return get_audit_log().connection


def tool_account_id() -> int:
    """Return the account these tools act as."""
    return current_account_id()


def error_result(message: str, **extra: Any) -> dict[str, Any]:
    """Return the failure shape every tool in this package uses.

    The message is logged as well as returned, because a tool result reaches
    the agent and the log reaches the human reading stderr afterwards.
    """
    logger.error(message)
    return {"status": "error", "message": message, **extra}


def positive_int(name: str, value: Any, *, default: int, maximum: int) -> int:
    """Coerce a caller-supplied count into a sane bound.

    Raises rather than silently clamping a nonsense value, because a caller who
    asked for -1 results wanted something this cannot guess.
    """
    if value is None:
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a whole number, got {value!r}") from None
    if number < 1:
        raise ValueError(f"{name} must be at least 1, got {number}")
    return min(number, maximum)


def choice(name: str, value: Any, allowed: Mapping[str, Any] | tuple[str, ...]) -> str:
    """Return a validated lowercase choice, naming the alternatives on failure."""
    text = str(value or "").strip().lower()
    if text not in allowed:
        options = ", ".join(sorted(allowed))
        raise ValueError(f"{name} must be one of {options}, got {value!r}")
    return text
