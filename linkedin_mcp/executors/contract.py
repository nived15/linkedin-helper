"""The job shape MCP-03 writes and the worker's registry executes.

An MCP action tool does not act. It validates, writes one `jobs` row and
returns a job id, so the action passes the same `SafetyGate`, receives the same
jitter and lands in the same `actions_log` as a campaign step. This module is
the whole contract between the enqueue side (`linkedin_mcp.tools.actions` and
the tools in `linkedin_browser_mcp.py`) and the execute side
(`linkedin_mcp.executors.linkedin`).

The row an action tool writes
-----------------------------
=================  ===========================================================
`account_id`       The acting account, from `linkedin_mcp.audit`.
`campaign_id`      **NULL.** A one-off action belongs to no campaign.
`lead_id`          The lead when one is known, otherwise NULL. Supplying it is
                   what lets the gate's blacklist and dedupe checks see the
                   action, so a manual invite to somebody already invited last
                   week refuses exactly as a campaign invite would.
`step_id`          **NULL.** There is no campaign step behind it.
`action_type`      The metered LinkedIn action it spends, from
                   `linkedin_mcp.core.config`. Deliberately the *same* names a
                   campaign step uses, so a manual invite and a sequenced
                   invite compete for one budget rather than two.
`payload_json`     `{"action": <name>, ...}`, sorted keys.
`scheduled_for`    Enqueue time, so the job is due immediately.
`priority`         :data:`ADHOC_JOB_PRIORITY`, which is 0.
`state`            `pending`.
=================  ===========================================================

Why the payload needs a discriminator
-------------------------------------
`action_type` is not enough to route on. MCP-02's harvests already use
`profile_search` and `post_read` for their own pages, because a harvest page
costs the same LinkedIn budget as a search. A queue row therefore says what it
is in its payload: :data:`ACTION_KEY` names a one-off action and MCP-02's
`harvest` key names an extraction. An executor registered for `profile_search`
sees both and must dispatch on that key rather than assume.

How the answer gets back
------------------------
An MCP tool that used to return a page of search results now returns a job id,
so the results have to arrive somewhere the caller can read them later. They
are written back onto the job payload under :data:`RESULT_KEY`, which is the
pattern MCP-02 already established for `run_id`, and `action_status` reads
them. The result is also in `actions_log.detail_json`, but that is the ledger
the safety gate counts and it is keyed by account and time rather than by job,
so it is the wrong thing to poll.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any

from linkedin_mcp.core.config import (
    INVITE_ACTION,
    PROFILE_VIEW_ACTION,
    PROFILE_VIEW_DIRECT_ACTION,
)
from linkedin_mcp.sequences import (
    Job,
    JobSpec,
    JobState,
    list_jobs,
    now_timestamp,
    transaction,
)

__all__ = [
    "ACTION_KEY",
    "ADHOC_ACTIONS",
    "ADHOC_JOB_PRIORITY",
    "APPROVED_KEY",
    "DETAIL_SHAPE",
    "MAX_RESULT_BYTES",
    "PROFILE_SHAPES",
    "RESULT_KEY",
    "SUMMARY_SHAPE",
    "AdHocAction",
    "adhoc_action",
    "adhoc_action_name",
    "adhoc_jobs",
    "adhoc_job_spec",
    "cancel_adhoc_job",
    "is_adhoc_action_job",
    "job_result",
    "record_job_result",
]

ACTION_KEY = "action"
"""Payload key naming which one-off action a job runs.

Mirrors MCP-02's `harvest` key deliberately. Two ad-hoc job families now share
the queue and several `action_type` values, and a reader that cannot tell them
apart from the row alone would have to guess.
"""

RESULT_KEY = "result"
"""Payload key the executor writes its answer back onto."""

APPROVED_KEY = "approved"
"""Payload key the runner already reads to decide whether a human signed off.

`Worker._run_ad_hoc_job` reads `payload["approved"]` because an ad-hoc job has
no campaign `approval_mode` to consult and the safe reading of silence is
"nobody approved this". Naming the key here rather than repeating the literal
keeps the two sides of that agreement in one place.
"""

ADHOC_JOB_PRIORITY = 0
"""Priority every one-off action carries.

The same zero a harvest and an unconfigured campaign step get. A manual action
is not more urgent than outreach that is already due; making it jump the queue
would let a chatty afternoon starve the sequences the queue exists to run.
"""

MAX_RESULT_BYTES = 64_000
"""Largest result written back onto a job payload.

A feed browse of twenty posts is a few kilobytes; a runaway extraction is not,
and `jobs.payload_json` is read by every selection pass. Oversized results are
replaced by a truncation notice rather than silently trimmed, because a partial
JSON blob that still parses is the worst of both.
"""

SUMMARY_SHAPE = "summary"
"""Profile payload shape `get_linkedin_profile` used to return: the top card."""

DETAIL_SHAPE = "detail"
"""Profile payload shape `view_linkedin_profile` used to return: plus history."""

PROFILE_SHAPES: tuple[str, ...] = (SUMMARY_SHAPE, DETAIL_SHAPE)
"""The two profile extractions the legacy tools offered, kept distinct.

They cost the same navigation and differ only in how much of the page is read,
so they share an `action_type` and are told apart by the payload. Collapsing
them into one would change what `get_linkedin_profile` returns, which is the
regression the migration path exists to avoid.
"""


@dataclass(frozen=True, slots=True)
class AdHocAction:
    """One queued verb: what it costs, what it needs, and who used to do it.

    `legacy_tool` is documentation with teeth. Every action here exists because
    a tool in `linkedin_browser_mcp.py` used to perform it inline, and the
    migration is only honest if each of those tools still reaches the same
    behaviour through the queue.
    """

    name: str
    action_type: str
    legacy_tool: str
    required: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()
    approval_required: bool = False
    defaults: Mapping[str, Any] = field(default_factory=dict)

    @property
    def fields(self) -> tuple[str, ...]:
        return self.required + self.optional

    @property
    def direct(self) -> bool:
        """True when this action loads a profile URL straight from the address bar.

        Read off the action name rather than carried in the payload. LinkedIn
        caps direct loads at roughly forty a day against a hundred for a view
        reached through the site, so the two have separate `action_type` values
        and separate budgets. A payload flag would let a job claim the cheaper
        budget and then take the expensive route, which is precisely the
        accounting hole `profile_view_direct` exists to close.
        """
        return self.action_type == PROFILE_VIEW_DIRECT_ACTION


_ACTIONS: tuple[AdHocAction, ...] = (
    AdHocAction(
        name=INVITE_ACTION,
        action_type=INVITE_ACTION,
        legacy_tool="send_connection_request",
        required=("profile_url",),
        optional=("note", "direct"),
        approval_required=True,
        defaults={"direct": False},
    ),
    AdHocAction(
        name=PROFILE_VIEW_ACTION,
        action_type=PROFILE_VIEW_ACTION,
        legacy_tool="view_linkedin_profile",
        required=("profile_url",),
        optional=("shape",),
        defaults={"shape": DETAIL_SHAPE},
    ),
    AdHocAction(
        name=PROFILE_VIEW_DIRECT_ACTION,
        action_type=PROFILE_VIEW_DIRECT_ACTION,
        legacy_tool="view_linkedin_profile",
        required=("profile_url",),
        optional=("shape",),
        defaults={"shape": DETAIL_SHAPE},
    ),
    AdHocAction(
        name="profile_search",
        action_type="profile_search",
        legacy_tool="search_linkedin_profiles",
        required=("query",),
        optional=("count",),
        defaults={"count": 5},
    ),
    AdHocAction(
        name="post_search",
        action_type="post_search",
        legacy_tool="search_linkedin_posts",
        required=("query",),
        optional=("count", "sort_by"),
        defaults={"count": 10, "sort_by": "relevance"},
    ),
    AdHocAction(
        name="feed_browse",
        action_type="feed_browse",
        legacy_tool="browse_linkedin_feed",
        optional=("count",),
        defaults={"count": 5},
    ),
    AdHocAction(
        name="post_read",
        action_type="post_read",
        legacy_tool="interact_with_linkedin_post",
        required=("post_url",),
    ),
    AdHocAction(
        name="post_like",
        action_type="post_like",
        legacy_tool="interact_with_linkedin_post",
        required=("post_url",),
    ),
    AdHocAction(
        name="post_comment",
        action_type="post_comment",
        legacy_tool="interact_with_linkedin_post",
        required=("post_url", "comment"),
        approval_required=True,
    ),
    AdHocAction(
        name="post_share",
        action_type="post_share",
        legacy_tool="interact_with_linkedin_post",
        required=("post_url",),
        approval_required=True,
    ),
)

ADHOC_ACTIONS: Mapping[str, AdHocAction] = MappingProxyType(
    {action.name: action for action in _ACTIONS}
)
"""Every one-off action a job may name, keyed by its payload `action` value.

There is no `send_message` and no `endorse_skills` here, deliberately. Issue #26
names them as examples of verbs that must be queued rather than executed
inline, and the queue is ready for both, but this repository has no message
composer selectors and no endorsement selectors, so an executor for either
would be a promise the codebase cannot keep. That is the same reasoning MCP-02
used to leave `harvest_sales_nav` out, and adding the tool without the
extractor is the failure mode it avoids. Add the selectors, add the executor,
add the entry: nothing else here has to change.
"""


def adhoc_action(name: str) -> AdHocAction:
    """Return the action a payload names, rejecting one nobody registered."""
    try:
        return ADHOC_ACTIONS[name]
    except KeyError:
        known = ", ".join(sorted(ADHOC_ACTIONS))
        raise KeyError(
            f"{name!r} is not a registered action. Known actions: {known}. "
            "send_message and endorse_skills are deliberately absent until the "
            "selectors behind them exist."
        ) from None


def adhoc_job_spec(
    account_id: int,
    name: str,
    payload: Mapping[str, Any] | None = None,
    *,
    lead_id: int | None = None,
    approved: bool = False,
    now: datetime | str | None = None,
    priority: int = ADHOC_JOB_PRIORITY,
) -> JobSpec:
    """Build the queue row for one one-off action.

    `campaign_id` and `step_id` are None on purpose, and `lead_id` may be too.
    `JobSpec` annotates them as `int` but performs no runtime checking, and
    SEQ-01's unique index skips NULLs, so an ad-hoc row is legal and
    unconstrained. This is exactly the shape MCP-02 writes for a harvest, which
    is why `ad_hoc_due_jobs` already returns it without a line of new selection
    code.
    """
    action = adhoc_action(name)
    body: dict[str, Any] = {ACTION_KEY: action.name}
    body.update(action.defaults)
    body.update(payload or {})
    body[APPROVED_KEY] = bool(approved)
    return JobSpec(
        campaign_id=None,  # type: ignore[arg-type]
        lead_id=lead_id,  # type: ignore[arg-type]
        step_id=None,  # type: ignore[arg-type]
        account_id=account_id,
        action_type=action.action_type,
        payload_json=json.dumps(body, sort_keys=True),
        scheduled_for=now_timestamp(now),
        priority=int(priority),
    )


def is_adhoc_action_job(job: Job) -> bool:
    """True when a queue row is a one-off action rather than a harvest or a step.

    The campaign check is what keeps this disjoint from campaign work, and the
    payload check is what keeps it disjoint from MCP-02's harvests, which share
    both the campaign-less shape and, for a People search, the `action_type`.
    """
    return job.campaign_id is None and job.payload.get(ACTION_KEY) in ADHOC_ACTIONS


def adhoc_action_name(job: Job) -> str | None:
    """Return the action a job names, or None when it is not a one-off action."""
    return job.payload.get(ACTION_KEY) if is_adhoc_action_job(job) else None


def job_result(job: Job) -> Any:
    """Return whatever the executor wrote back onto the job, or None."""
    return job.payload.get(RESULT_KEY)


def record_job_result(
    conn: sqlite3.Connection,
    job_id: int,
    result: Any,
) -> bool:
    """Write an executor's answer back onto a job payload. Returns False if gone.

    Opens its own short transaction. The runner deliberately commits its claim
    before running an executor so no write lock is held across a browser call,
    so this is the executor's own write and has to carry its own transaction.
    """
    row = conn.execute(
        "SELECT payload_json FROM jobs WHERE id = ?", (int(job_id),)
    ).fetchone()
    if row is None:
        return False
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except (TypeError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    encoded = json.dumps(result, sort_keys=True, default=str)
    if len(encoded) > MAX_RESULT_BYTES:
        payload[RESULT_KEY] = {
            "truncated": True,
            "bytes": len(encoded),
            "message": (
                f"the result was {len(encoded)} bytes, over the "
                f"{MAX_RESULT_BYTES} the queue stores; read actions_log for the "
                "summary or ask for fewer items"
            ),
        }
    else:
        payload[RESULT_KEY] = json.loads(encoded)

    with transaction(conn):
        conn.execute(
            "UPDATE jobs SET payload_json = ? WHERE id = ?",
            (json.dumps(payload, sort_keys=True), int(job_id)),
        )
    return True


def adhoc_jobs(
    conn: sqlite3.Connection,
    account_id: int,
    *,
    limit: int | None = None,
) -> list[Job]:
    """Return an account's one-off action jobs, newest first.

    Reads through :func:`~linkedin_mcp.sequences.jobs.list_jobs` and filters in
    Python rather than adding a second query over `jobs`, for the same reason
    MCP-02 does: the queue's SQL belongs to SEQ-01 and its scheduler read
    belongs to SEQ-04.
    """
    jobs = [
        job
        for job in list_jobs(conn, account_id=account_id)
        if is_adhoc_action_job(job)
    ]
    jobs.sort(key=lambda job: job.id, reverse=True)
    return jobs if limit is None else jobs[: max(0, int(limit))]


def cancel_adhoc_job(conn: sqlite3.Connection, job: Job) -> bool:
    """Cancel a queued one-off action. Returns False when it is too late.

    Only a `pending` row can be cancelled. A `leased` one is already in a
    worker's hands and may have half-sent an invitation, so flipping its state
    from underneath would let the runner's own close write over the
    cancellation and would report a stop that never happened. The honest answer
    for a leased job is no.
    """
    if job.state != JobState.PENDING.value:
        return False
    with transaction(conn):
        changed = conn.execute(
            "UPDATE jobs SET state = ?, last_error = ? "
            "WHERE id = ? AND state = ?",
            (
                JobState.CANCELLED.value,
                "cancelled by action_cancel before the worker leased it",
                int(job.id),
                JobState.PENDING.value,
            ),
        ).rowcount
    return bool(changed)
