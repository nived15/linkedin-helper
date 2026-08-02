"""The three MCP tools that make an LLM the generator instead of a dependency.

`drafts_list_pending` -> `drafts_submit` -> `drafts_approve` is the round trip.
The worker parks rows; the client lists them, writes the text, submits it; a
human releases it. The worker is never in that loop and never waits on it.

Why this module and not the entry point
---------------------------------------
`linkedin_browser_mcp.py` belongs to MCP-02 (#25) exclusively, so these tools are
defined here and attached to a passed-in FastMCP instance by
:func:`register_draft_tools`. The one-line wiring into the server, a call to
`register_draft_tools(mcp)`, is outstanding and belongs to #25 or MCP-03 (#26).
The tests construct their own `FastMCP`, register against it and exercise the
real call path, so the round trip is proven rather than asserted.

All three carry `@audit_linkedin_action`. They touch nothing on LinkedIn, so
their action types are in `UNMETERED_ACTIONS` and cannot eat the account's daily
budget. They are audited anyway because `actions_log` is the record of how a
message came to exist, and "which model wrote this and who approved it" is
exactly the question an audit trail is for.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Any

from fastmcp import Context

from linkedin_mcp.audit.instrument import audit_linkedin_action, current_account_id
from linkedin_mcp.drafts.errors import DraftError
from linkedin_mcp.drafts.session import get_draft_connection
from linkedin_mcp.drafts.store import (
    DRAFT_KINDS,
    DRAFT_STATUSES,
    STATUS_NEEDS_GENERATION,
    approve_draft,
    list_pending,
    require_draft,
    submit_draft,
)


__all__ = [
    "DRAFT_ACTION_TYPES",
    "DRAFT_APPROVE_ACTION",
    "DRAFT_LIST_ACTION",
    "DRAFT_SUBMIT_ACTION",
    "register_draft_tools",
]

DRAFT_LIST_ACTION = "draft_list_pending"
DRAFT_SUBMIT_ACTION = "draft_submit"
DRAFT_APPROVE_ACTION = "draft_approve"

DRAFT_ACTION_TYPES: tuple[str, ...] = (
    DRAFT_LIST_ACTION,
    DRAFT_SUBMIT_ACTION,
    DRAFT_APPROVE_ACTION,
)
"""Audit action types these tools write.

Every one of them is in `linkedin_mcp.core.config.UNMETERED_ACTIONS`. The metered
universe is closed by exclusion, so a draft action type left out of that set
would quietly spend the account's LinkedIn budget on database bookkeeping.
"""

DEFAULT_LIST_LIMIT = 25
MAX_LIST_LIMIT = 200


def _error(message: str, **extra: Any) -> dict[str, Any]:
    return {"status": "error", "message": message, **extra}


def register_draft_tools(
    mcp: Any,
    *,
    connection_factory: Callable[[], sqlite3.Connection] | None = None,
    account_resolver: Callable[[], int] | None = None,
) -> dict[str, Callable[..., Any]]:
    """Attach the three draft tools to a FastMCP instance.

    Returns the undecorated coroutine functions keyed by tool name so a caller,
    or a test, can invoke them directly as well as through the MCP surface.

    Args:
        mcp: the `FastMCP` instance to register against.
        connection_factory: how to obtain the database connection. Defaults to
            :func:`linkedin_mcp.drafts.session.get_draft_connection`.
        account_resolver: how to resolve the acting account id. Defaults to the
            audit package's resolver, so tool rows and draft rows agree on which
            account they belong to.
    """
    resolve_conn = connection_factory or get_draft_connection
    resolve_account = account_resolver or current_account_id

    @mcp.tool()
    @audit_linkedin_action(DRAFT_LIST_ACTION, capture=("kind", "status", "campaign_id"))
    async def drafts_list_pending(
        kind: str | None = None,
        status: str = STATUS_NEEDS_GENERATION,
        campaign_id: int | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
        ctx: Context | None = None,
    ) -> dict:
        """List drafts waiting for work, oldest first.

        `status='needs_generation'` (the default) is the generation queue this
        client should write text for. `status='pending_approval'` is the human
        review queue. Two different jobs, one tool, one argument.

        Args:
            kind: restrict to one of connection_note, message, comment,
                icp_evaluation.
            status: which queue to read.
            campaign_id: restrict to one campaign.
            limit: how many rows to return, capped at 200.
        """
        if status not in DRAFT_STATUSES:
            return _error(
                f"unknown status {status!r}; expected one of {list(DRAFT_STATUSES)}"
            )
        if kind is not None and kind not in DRAFT_KINDS:
            return _error(f"unknown kind {kind!r}; expected one of {list(DRAFT_KINDS)}")

        try:
            drafts = list_pending(
                resolve_conn(),
                resolve_account(),
                status=status,
                kind=kind,
                campaign_id=campaign_id,
                limit=max(1, min(int(limit), MAX_LIST_LIMIT)),
            )
        except DraftError as error:
            return _error(str(error))

        return {
            "status": "success",
            "queue": status,
            "count": len(drafts),
            "drafts": [draft.to_result() for draft in drafts],
        }

    @mcp.tool()
    @audit_linkedin_action(DRAFT_SUBMIT_ACTION, target="draft_id", capture=("model",))
    async def drafts_submit(
        draft_id: int,
        text: str | None = None,
        verdict: dict[str, Any] | None = None,
        model: str | None = None,
        ctx: Context | None = None,
    ) -> dict:
        """Submit generated output against a parked draft.

        Text kinds need `text`. `icp_evaluation` needs `verdict`, an object with
        `match`, `score` and `reason`. Submitted text is held to Nived's writing
        style rules from `.github/copilot-instructions.md`: an em dash, a banned
        filler opener or an over-long sentence is refused here and nothing is
        written, so regenerate and submit again.

        The draft lands in `pending_approval`. It becomes `approved` immediately
        only when the owning campaign's `approval_mode` is `auto`, which is a
        deliberate per-campaign opt-in and not the default.
        """
        conn = resolve_conn()
        try:
            draft = submit_draft(
                conn,
                int(draft_id),
                text=text,
                verdict=verdict,
                model=model,
                account_id=resolve_account(),
            )
        except DraftError as error:
            return _error(str(error), draft_id=draft_id, reason=type(error).__name__)

        return {
            "status": "success",
            "message": f"draft {draft.id} is {draft.status}",
            "draft": draft.to_result(),
        }

    @mcp.tool()
    @audit_linkedin_action(DRAFT_APPROVE_ACTION, target="draft_id", capture=("approved",))
    async def drafts_approve(
        draft_id: int,
        approved: bool = True,
        note: str | None = None,
        reviewed_text: str | None = None,
        ctx: Context | None = None,
    ) -> dict:
        """Release a draft for use, or reject it.

        This is the human-in-the-loop gate. Until a draft is approved, nothing in
        this system will put its text into a message: the template engine sees an
        unapproved fragment as absent and refuses the whole render, and
        `mark_sent` refuses any status but `approved`.

        Pass `approved=False` to reject, which is also how an approval is revoked
        before anything is sent.

        Pass `reviewed_text` with the exact text that was read. If the draft was
        regenerated in between, the approval is refused rather than releasing text
        nobody looked at.
        """
        conn = resolve_conn()
        try:
            draft = approve_draft(
                conn,
                int(draft_id),
                approved=bool(approved),
                note=note,
                expected_text=reviewed_text,
                account_id=resolve_account(),
            )
        except DraftError as error:
            return _error(str(error), draft_id=draft_id, reason=type(error).__name__)

        return {
            "status": "success",
            "message": f"draft {draft.id} is {draft.status}",
            "draft": draft.to_result(),
        }

    return {
        "drafts_list_pending": drafts_list_pending,
        "drafts_submit": drafts_submit,
        "drafts_approve": drafts_approve,
    }


def draft_snapshot(conn: sqlite3.Connection, draft_id: int) -> dict[str, Any]:
    """Return one draft as an MCP-shaped dict. Handy for diagnostics."""
    return require_draft(conn, draft_id).to_result()
