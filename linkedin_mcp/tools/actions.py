"""The three MCP-03 tools: queue a one-off action, read it, cancel it.

Issue #26 in one sentence: an MCP tool must not be able to act on LinkedIn,
because a cap the model is trusted to remember is not a cap. So
`action_enqueue_adhoc` validates its arguments, writes one `jobs` row and
returns a job id. The worker leases that row, asks `SafetyGate`, waits out
CORE-04's jitter and appends the `actions_log` entry. Nothing here opens a
browser and nothing here can.

Why a manual action and a campaign action are the same row
----------------------------------------------------------
Both are a `jobs` row with the same `account_id` and the same `action_type`, so
both are counted by the same daily and weekly budgets, deduplicated by the same
window and blocked by the same blacklist. The only difference is that a campaign
job carries `campaign_id` and `step_id` and this one carries NULL, which is what
tells the runner whose approval rule to consult. By the time either reaches
`SafetyGate` they are indistinguishable, which is the DoD line this file exists
to satisfy.

Validation happens here, execution happens later
------------------------------------------------
A malformed profile URL, an over-long note or an unknown action is the caller's
mistake, and it belongs in the tool result they are looking at rather than in a
worker log at two in the morning. Every check the old inline tools performed
before opening a browser still runs, at the same moment, against the same
arguments. That is the migration path: the refusals a caller used to get are
still the refusals they get.
"""

from __future__ import annotations

import logging
from typing import Any

from fastmcp import Context, FastMCP

from linkedin_mcp.audit import audit_linkedin_action
from linkedin_mcp.core.config import (
    ADHOC_CANCEL_ACTION,
    ADHOC_ENQUEUE_ACTION,
    ADHOC_STATUS_ACTION,
    APPROVAL_REQUIRED_ACTIONS,
    INVITE_ACTION,
    PROFILE_VIEW_ACTION,
    PROFILE_VIEW_DIRECT_ACTION,
)
from linkedin_mcp.executors.contract import (
    ACTION_KEY,
    ADHOC_ACTIONS,
    PROFILE_SHAPES,
    RESULT_KEY,
    adhoc_action,
    adhoc_job_spec,
    adhoc_jobs,
    cancel_adhoc_job,
)
from linkedin_mcp.sequences import JobState, insert_job, transaction
from linkedin_mcp.tools.runtime import (
    choice,
    error_result,
    positive_int,
    tool_account_id,
    tool_connection,
)

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_NOTE_CHARS",
    "PROFILE_URL_MARKER",
    "POST_URL_MARKERS",
    "enqueue_action",
    "register_action_tools",
    "validated_payload",
]

MAX_NOTE_CHARS = 300
"""Longest connection note LinkedIn accepts. Its limit, not ours."""

PROFILE_URL_MARKER = "linkedin.com/in/"
POST_URL_MARKERS = ("linkedin.com/posts/", "linkedin.com/feed/update/")

MAX_SEARCH_RESULTS = 100
MAX_FEED_POSTS = 50
SORT_ORDERS = ("relevance", "date_posted")


def _profile_url(value: Any) -> str:
    url = str(value or "").strip()
    if PROFILE_URL_MARKER not in url:
        raise ValueError(
            f"Invalid LinkedIn profile URL. Should contain '{PROFILE_URL_MARKER}'"
        )
    return url


def _post_url(value: Any) -> str:
    url = str(value or "").strip()
    if not any(marker in url for marker in POST_URL_MARKERS):
        raise ValueError("Invalid LinkedIn post URL")
    return url


def _query(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("query is required; give something to search for")
    return text


def validated_payload(name: str, fields: dict[str, Any]) -> dict[str, Any]:
    """Return the stored payload for one action, or raise the caller's mistake.

    Runs exactly the checks the inline tools ran before they opened a browser,
    so a bad URL is still refused at the call site rather than discovered by a
    worker an hour later.
    """
    action = adhoc_action(name)
    given = {key: value for key, value in fields.items() if value is not None}
    unknown = set(given) - set(action.fields)
    if unknown:
        raise ValueError(
            f"{name} does not take {', '.join(sorted(unknown))}; it takes "
            f"{', '.join(action.fields) or 'no arguments'}"
        )

    payload: dict[str, Any] = {}

    if "profile_url" in action.required:
        payload["profile_url"] = _profile_url(given.get("profile_url"))
    if "post_url" in action.required:
        payload["post_url"] = _post_url(given.get("post_url"))
    if "query" in action.required:
        payload["query"] = _query(given.get("query"))

    if name == INVITE_ACTION:
        note = given.get("note")
        if note is not None:
            note = str(note)
            if len(note) > MAX_NOTE_CHARS:
                raise ValueError(
                    f"Connection note too long ({len(note)} chars). "
                    f"Max {MAX_NOTE_CHARS} characters."
                )
            payload["note"] = note
        payload["direct"] = bool(given.get("direct", False))

    if name in (PROFILE_VIEW_ACTION, PROFILE_VIEW_DIRECT_ACTION):
        payload["shape"] = choice(
            "shape", given.get("shape", action.defaults["shape"]), PROFILE_SHAPES
        )

    if name == "post_comment":
        comment = str(given.get("comment") or "").strip()
        if not comment:
            raise ValueError("comment is required for post_comment")
        payload["comment"] = comment

    if "count" in action.fields:
        maximum = MAX_FEED_POSTS if name == "feed_browse" else MAX_SEARCH_RESULTS
        payload["count"] = positive_int(
            "count",
            given.get("count"),
            default=int(action.defaults["count"]),
            maximum=maximum,
        )

    if "sort_by" in action.fields:
        payload["sort_by"] = choice(
            "sort_by", given.get("sort_by", action.defaults["sort_by"]), SORT_ORDERS
        )

    if "page" in action.fields:
        payload["page"] = positive_int(
            "page",
            given.get("page"),
            default=int(action.defaults.get("page", 1)),
            maximum=100,
        )

    return payload


def enqueue_action(
    name: str,
    payload: dict[str, Any] | None = None,
    *,
    lead_id: int | None = None,
    approved: bool = False,
) -> dict[str, Any]:
    """Write one action job and describe it back to the caller.

    The result names the job id, the action type it will spend and the payload,
    so an agent reading it knows exactly what the worker will do and can poll
    `action_status` without guessing.
    """
    action = adhoc_action(name)
    conn = tool_connection()
    account_id = tool_account_id()
    spec = adhoc_job_spec(
        account_id,
        name,
        payload or {},
        lead_id=lead_id,
        approved=approved,
    )
    with transaction(conn):
        job_id = insert_job(conn, spec, state=JobState.PENDING)

    logger.info("Queued %s as job %d", name, job_id)
    return {
        "status": "queued",
        "job_id": job_id,
        "action": name,
        "action_type": spec.action_type,
        "legacy_tool": action.legacy_tool,
        "lead_id": lead_id,
        "approved": bool(approved),
        "approval_required": spec.action_type in APPROVAL_REQUIRED_ACTIONS,
        "scheduled_for": spec.scheduled_for,
        "priority": spec.priority,
        "state": JobState.PENDING.value,
        "payload": {ACTION_KEY: name, **(payload or {})},
        "message": (
            f"Queued {name} as job {job_id}. Nothing has reached LinkedIn yet: "
            f"the worker leases the job, asks SafetyGate, waits out the jitter "
            f"and then acts. Poll action_status(job_id={job_id}) for the result."
        ),
    }


def _job_report(job: Any) -> dict[str, Any]:
    """Describe one queued action, including whatever the executor wrote back."""
    return {
        "job_id": job.id,
        "action": job.payload.get(ACTION_KEY),
        "action_type": job.action_type,
        "state": job.state,
        "attempts": job.attempts,
        "scheduled_for": job.scheduled_for,
        "priority": job.priority,
        "locked_by": job.locked_by,
        "locked_at": job.locked_at,
        "last_error": job.last_error,
        "lead_id": job.lead_id,
        "payload": {
            key: value for key, value in job.payload.items() if key != RESULT_KEY
        },
        "result": job.payload.get(RESULT_KEY),
    }


def _job_message(report: dict[str, Any]) -> str:
    if report["result"] is not None:
        return (
            f"Job {report['job_id']} ({report['action']}) is {report['state']}. "
            f"The result is in the 'result' field."
        )
    if report["last_error"]:
        return (
            f"Job {report['job_id']} ({report['action']}) is {report['state']}: "
            f"{report['last_error']}"
        )
    return (
        f"Job {report['job_id']} ({report['action']}) is {report['state']}. The "
        f"worker has not produced a result yet."
    )


def register_action_tools(mcp: FastMCP) -> None:
    """Register the three MCP-03 tools on the MCP server."""

    @mcp.tool()
    @audit_linkedin_action(
        ADHOC_ENQUEUE_ACTION, target="action", capture=("lead_id", "approved")
    )
    async def action_enqueue_adhoc(
        action: str,
        profile_url: str | None = None,
        post_url: str | None = None,
        query: str | None = None,
        note: str | None = None,
        comment: str | None = None,
        count: int | None = None,
        sort_by: str | None = None,
        shape: str | None = None,
        direct: bool = False,
        lead_id: int | None = None,
        approved: bool = False,
        ctx: Context | None = None,
    ) -> dict:
        """Queue one LinkedIn action. Returns a job id and touches LinkedIn not at all.

        Every action this server can perform goes through here, so a manual
        one-off is subject to the same daily cap, the same dedupe window and the
        same ledger as a campaign step. Call `action_enqueue_adhoc()` with an
        unknown action to see the list.

        Args:
            action: What to do, e.g. connection_request, profile_view,
                profile_search, post_search, feed_browse, post_read, post_like,
                post_comment, post_share.
            lead_id: The lead this action targets, when one is stored. Supplying
                it is what lets the gate's blacklist and dedupe checks see the
                action, so an invite to somebody already invited last week is
                refused exactly as a campaign invite would be.
            approved: Assert that a human signed this off. Invitations, comments
                and shares are refused without it.
        """
        try:
            name = str(action or "").strip()
            if name == PROFILE_VIEW_ACTION and direct:
                # `direct` names the action rather than riding in the payload:
                # LinkedIn caps a direct URL load at roughly 40 a day against
                # 100 for a view reached through the site, and the two have
                # separate ceilings. Resolving it here means the gate meters
                # the route actually taken.
                name = PROFILE_VIEW_DIRECT_ACTION
            payload = validated_payload(
                name,
                {
                    "profile_url": profile_url,
                    "post_url": post_url,
                    "query": query,
                    "note": note,
                    "comment": comment,
                    "count": count,
                    "sort_by": sort_by,
                    "shape": shape,
                    "direct": direct if name == INVITE_ACTION else None,
                },
            )
            return enqueue_action(
                name,
                payload,
                lead_id=None if lead_id is None else int(lead_id),
                approved=bool(approved),
            )
        except Exception as error:
            return error_result(
                f"Could not queue the action: {error}",
                known_actions=sorted(ADHOC_ACTIONS),
            )

    @mcp.tool()
    @audit_linkedin_action(ADHOC_STATUS_ACTION, capture=("job_id",))
    async def action_status(
        job_id: int | None = None,
        limit: int = 20,
        ctx: Context | None = None,
    ) -> dict:
        """Report a queued action's state and result, or list the recent ones.

        This is where the answer to a read action arrives. A tool that used to
        return a page of search results now returns a job id, and the worker
        writes the results back onto the job for this tool to read.
        """
        try:
            conn = tool_connection()
            account_id = tool_account_id()

            if job_id is not None:
                job = next(
                    (
                        candidate
                        for candidate in adhoc_jobs(conn, account_id)
                        if candidate.id == int(job_id)
                    ),
                    None,
                )
                if job is None:
                    return error_result(
                        f"No queued action {job_id} belongs to this account"
                    )
                report = _job_report(job)
                return {"status": "success", **report, "message": _job_message(report)}

            reports = [
                _job_report(job)
                for job in adhoc_jobs(
                    conn,
                    account_id,
                    limit=positive_int("limit", limit, default=20, maximum=200),
                )
            ]
            return {
                "status": "success",
                "count": len(reports),
                "jobs": reports,
                "message": (
                    f"{len(reports)} queued action(s) for this account, newest "
                    "first."
                ),
            }
        except Exception as error:
            return error_result(f"Could not read the action queue: {error}")

    @mcp.tool()
    @audit_linkedin_action(ADHOC_CANCEL_ACTION, capture=("job_id",))
    async def action_cancel(job_id: int, ctx: Context | None = None) -> dict:
        """Cancel a queued action before the worker picks it up.

        A job the worker has already leased cannot be cancelled, and this says
        so rather than pretending. Half an invitation is not a thing that can be
        called back from here.
        """
        try:
            conn = tool_connection()
            account_id = tool_account_id()
            job = next(
                (
                    candidate
                    for candidate in adhoc_jobs(conn, account_id)
                    if candidate.id == int(job_id)
                ),
                None,
            )
            if job is None:
                return error_result(
                    f"No queued action {job_id} belongs to this account"
                )
            if not cancel_adhoc_job(conn, job):
                return error_result(
                    f"Job {job_id} is {job.state}, not pending, so it is too "
                    "late to cancel it.",
                    job_id=job.id,
                    state=job.state,
                )
            return {
                "status": "success",
                "job_id": job.id,
                "action": job.payload.get(ACTION_KEY),
                "state": JobState.CANCELLED.value,
                "message": (
                    f"Cancelled job {job_id} before it reached LinkedIn."
                ),
            }
        except Exception as error:
            return error_result(f"Could not cancel the action: {error}")
