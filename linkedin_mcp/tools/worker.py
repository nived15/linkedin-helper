"""MCP-05 (#28): the two tools that stop and start the worker.

Why these had to exist
----------------------
The Phase 4 exit criterion says a fresh MCP client must be able to pause the
worker with tools, resources and prompts alone. It could not. Of the forty tools
the server shipped, the only pause-shaped ones were `campaign_pause` and
`campaign_resume`, and each stops one campaign.

Pausing every campaign is not the same thing. The campaign lane in
:func:`linkedin_mcp.sequences.jobs.due_jobs` inner-joins `campaigns` on
`RUNNABLE_STATUSES`, so it stops. The ad-hoc lane in
:func:`linkedin_mcp.worker.selection.ad_hoc_due_jobs` keys on
``campaign_id IS NULL`` and reads no campaign row, so it does not. Measured on
`2a34682`: with the only campaign paused, `select_due_jobs` still returned an
ad-hoc `profile_view`. A client that trusted "pause every campaign" would have
kept sending.

What these tools are allowed to do
----------------------------------
Write one row in `worker_control` and return. They drive no browser, which
`tests/test_actions.py` fails the build over, and they carry
`@audit_linkedin_action`, which `tests/test_audit_log.py` fails the build over.
Their action types are unmetered in `linkedin_mcp.core.config`, because a pause
reaches LinkedIn not at all and because refusing to stop the worker on the
grounds that the day's budget is spent would be exactly backwards.

Pausing is not cancelling. Queued jobs keep their state and their due time, so a
resume picks the queue up where it stopped. A job already in flight finishes,
because a flag cannot un-send half an invitation.
"""

from __future__ import annotations

from typing import Any

from fastmcp import Context, FastMCP

from linkedin_mcp.audit import audit_linkedin_action
from linkedin_mcp.core.config import WORKER_PAUSE_ACTION, WORKER_RESUME_ACTION
from linkedin_mcp.tools.runtime import error_result, tool_account_id, tool_connection
from linkedin_mcp.worker.control import pause_worker, resume_worker, worker_pause_state
from linkedin_mcp.worker.heartbeat import worker_status

__all__ = ["register_worker_tools"]

MAX_REASON_CHARS = 500
"""A reason is a note for a human, not a document. Longer is truncated."""


def _payload(conn: Any, account_id: int) -> dict[str, Any]:
    """The pause state plus the two numbers that say whether it took effect."""
    state = worker_pause_state(conn, account_id)
    status = worker_status(conn, account_id=account_id)
    return {
        **state.to_result(),
        "live_workers": status["live_workers"],
        "due_jobs": status["due_jobs"],
        "pending_jobs": status["pending_jobs"],
        "active_campaigns": status["active_campaigns"],
        "campaigns_running": status["campaigns_running"],
    }


def register_worker_tools(mcp: FastMCP) -> None:
    """Register `worker_pause` and `worker_resume` on the MCP server."""

    @mcp.tool()
    @audit_linkedin_action(WORKER_PAUSE_ACTION, capture=("reason",))
    async def worker_pause(
        reason: str | None = None,
        paused_by: str | None = None,
        ctx: Context | None = None,
    ) -> dict:
        """Stop the worker. Both job lanes, not one campaign.

        `campaign_pause` stops the campaign it names. This stops everything for
        this account: campaign steps and ad-hoc work such as harvests and one-off
        invitations, which no campaign status can reach.

        Nothing is cancelled. Every queued job keeps its due time, so
        `worker_resume` picks the queue up where it stopped. A job the worker has
        already leased runs to completion, because half an invitation cannot be
        called back by a flag.

        Confirm it took effect by reading `linkedin://worker/status`, which
        reports `paused` and, once the flag is set, `campaigns_running: false`.

        Args:
            reason: Why the worker was stopped, for whoever reads the status
                next. Free text, truncated at 500 characters.
            paused_by: Who stopped it. Defaults to unrecorded.
        """
        try:
            conn = tool_connection()
            account_id = tool_account_id()
            state = pause_worker(
                conn,
                account_id,
                reason=(reason or "")[:MAX_REASON_CHARS] or None,
                paused_by=(paused_by or "").strip() or None,
            )
            payload = _payload(conn, account_id)
            return {
                "status": "success",
                **payload,
                "message": (
                    "The worker is paused. Neither the campaign lane nor the "
                    f"ad-hoc lane will select work. {state.paused_at} is when it "
                    "stopped, and linkedin://worker/status will confirm it."
                ),
            }
        except Exception as error:
            return error_result(f"Could not pause the worker: {error}")

    @mcp.tool()
    @audit_linkedin_action(WORKER_RESUME_ACTION)
    async def worker_resume(ctx: Context | None = None) -> dict:
        """Let the worker select work again, from wherever the queue stopped.

        Resuming does not re-plan anything. Jobs whose due time passed during the
        pause are due now, so the first tick after this catches up in due-time
        order rather than skipping what it missed.

        A paused campaign stays paused. This clears the worker-level pause only,
        so `campaign_resume` is still what restarts one campaign.
        """
        try:
            conn = tool_connection()
            account_id = tool_account_id()
            resume_worker(conn, account_id)
            payload = _payload(conn, account_id)
            return {
                "status": "success",
                **payload,
                "message": (
                    f"The worker may select work again. {payload['due_jobs']} "
                    "job(s) are due now. Campaigns paused with campaign_pause "
                    "stay paused; campaign_resume is what restarts those."
                ),
            }
        except Exception as error:
            return error_result(f"Could not resume the worker: {error}")
