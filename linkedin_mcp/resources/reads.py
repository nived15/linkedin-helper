"""The twelve reads behind MCP-04 (#27), as plain functions over SQLite.

Why these are functions and not resource bodies
-----------------------------------------------
Every `@mcp.resource` body in `linkedin_mcp.resources.server` is three lines: get
the connection, call one of these, wrap it in the envelope. The reads themselves
live here so they can be exercised against a database without a server, and so
the registration module stays short enough to audit at a glance.

Nothing here opens a browser
----------------------------
Every function in this module takes a `sqlite3.Connection` and returns a dict.
None of them imports `playwright`, `linkedin_mcp.browser` or
`linkedin_mcp.executors`, and `tests/test_actions.py` fails the build if any of
them ever does. A resource is a local read: making one drive a page would be
slow, would spend the account's LinkedIn budget on a read, and would bypass the
job queue MCP-03 built.

Nothing here writes an audit row either
---------------------------------------
Deliberately, and `tests/test_audit_log.py` now records that as an exemption
rather than as an oversight. A resource takes no LinkedIn action, so an
`actions_log` row for one would be a fiction, and every one of those fictions
would be counted by `linkedin://safety/today` against the account's real daily
budget. Auditing the reads would corrupt the arithmetic the reads report.

Reuse over reimplementation
---------------------------
`_campaign_payload` and `_step_payload` are imported from
`linkedin_mcp.tools.campaigns`, and `lead_payload` from
`linkedin_mcp.tools.crm`, rather than copied. The campaign one matters most: its
`approved` / `runnable` / `editable` booleans are the only thing that makes
`campaigns.status` legible, because `pending_approval` means "approved, waiting
to be started" rather than "waiting for approval". A second copy of that
derivation would be free to drift from the tools, and a resource that disagreed
with `campaign_status` about whether a campaign was approved would be worse than
no resource at all.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from linkedin_mcp.audit.log import AuditLog
from linkedin_mcp.core.config import (
    GLOBAL_DAILY_CEILING,
    GLOBAL_HOURLY_CEILING,
    METERED_ACTIONS,
    PENDING_INVITE_CEILING,
)
from linkedin_mcp.drafts import list_pending
from linkedin_mcp.drafts.store import STATUS_PENDING_APPROVAL
from linkedin_mcp.leads import get_lead, lead_tag_names, list_leads
from linkedin_mcp.resources.inbox import (
    DEFAULT_UNREAD_LIMIT,
    UNREAD_DEFINITION,
    unread_thread_count,
    unread_threads,
)
from linkedin_mcp.safety import (
    account_limit,
    daily_budget,
    global_daily_budget,
    hourly_budget,
    open_challenges,
    pending_invites,
    recent_safety_events,
    weekly_budget,
)
from linkedin_mcp.sequences import (
    due_jobs,
    get_campaign,
    list_campaigns,
    list_steps,
)
from linkedin_mcp.templating import list_templates
from linkedin_mcp.tools.campaigns import (
    _campaign_payload as campaign_payload,
)
from linkedin_mcp.tools.campaigns import (
    _next_run_at as next_run_at,
)
from linkedin_mcp.tools.campaigns import (
    _open_jobs as open_jobs,
)
from linkedin_mcp.tools.campaigns import (
    _step_payload as step_payload,
)
from linkedin_mcp.tools.crm import lead_payload
from linkedin_mcp.worker import campaign_funnel, worker_status

__all__ = [
    "DEFAULT_LEAD_LIMIT",
    "DEFAULT_TEMPLATE_LIMIT",
    "DRAFTS_PENDING_STATUS",
    "WEEKLY_WINDOW_DAYS",
    "analytics_weekly",
    "campaign_detail",
    "campaign_funnel_read",
    "campaigns_overview",
    "drafts_pending",
    "inbox_unread",
    "lead_detail",
    "leads_active",
    "safety_today",
    "stats_daily",
    "templates_overview",
    "worker_status_read",
]

DEFAULT_LEAD_LIMIT = 100
"""`linkedin://leads/active` is a window onto a CRM, not a dump of one."""

DEFAULT_TEMPLATE_LIMIT = 100
DEFAULT_DRAFT_LIMIT = 50
DEFAULT_SAFETY_EVENT_LIMIT = 10
WEEKLY_WINDOW_DAYS = 7

DRAFTS_PENDING_STATUS = STATUS_PENDING_APPROVAL
"""`pending_approval`, not `drafts.list_pending`'s default of `needs_generation`.

Those are two different queues worked by two different actors. `needs_generation`
is what an AI still has to write; `pending_approval` is what a human still has to
sign off, which is the human-in-the-loop gate this whole product is built on. A
person opening `linkedin://drafts/pending` is asking the second question, so the
status is passed explicitly here rather than inherited from a default that would
answer the first one.

The generation queue is still visible: it is reported as a count in
`queue_depths` on the same payload, so nothing is hidden by the choice.
"""


class CampaignNotVisible(LookupError):
    """A campaign id that does not exist, or belongs to another account.

    One exception for both, because "no" and "yes but not yours" are the same
    answer to a caller who should not have had the id. This mirrors
    `linkedin_mcp.tools.campaigns._require_campaign`.
    """


class LeadNotVisible(LookupError):
    """A lead id that does not exist, or belongs to another account."""


def _utc_now(now: datetime | None = None) -> datetime:
    return now or datetime.now(timezone.utc)


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------------
# Campaigns
# --------------------------------------------------------------------------


def campaigns_overview(conn: sqlite3.Connection, account_id: int) -> dict[str, Any]:
    """Every campaign this account owns, each with its funnel.

    `runnable_statuses` is echoed because it is the whole gate: `due_jobs`
    inner-joins `campaigns` and filters on it, so a campaign outside that set
    yields no work however many jobs it has queued.
    """
    campaigns = list_campaigns(conn, account_id)
    payloads = [
        {**campaign_payload(campaign), "funnel": campaign_funnel(conn, campaign.id)}
        for campaign in campaigns
    ]
    by_status: dict[str, int] = {}
    for campaign in campaigns:
        by_status[campaign.status] = by_status.get(campaign.status, 0) + 1
    return {
        "count": len(payloads),
        "campaigns": payloads,
        "by_status": by_status,
        "runnable": sum(1 for campaign in campaigns if campaign.runnable),
        "runnable_statuses": ["active"],
    }


def _visible_campaign(conn: sqlite3.Connection, account_id: int, campaign_id: int):
    campaign = get_campaign(conn, int(campaign_id))
    if campaign is None or campaign.account_id != account_id:
        raise CampaignNotVisible(f"no campaign {campaign_id} for this account")
    return campaign


def campaign_detail(
    conn: sqlite3.Connection, account_id: int, campaign_id: int
) -> dict[str, Any]:
    """One campaign's live state: this is VAL-03.

    The `campaign` block carries `approved`, `runnable` and `editable` alongside
    the raw `status`, because the status word alone misleads. `pending_approval`
    reads in English as "somebody still has to approve this" and means the
    opposite: the definition has been approved and is waiting to be started. The
    three booleans are the same ones `campaign_status` returns, from the same
    function, so a client cannot be told two different things by two surfaces.
    """
    campaign = _visible_campaign(conn, account_id, campaign_id)
    funnel = campaign_funnel(conn, campaign.id)
    due = due_jobs(conn, account_id, campaign_id=campaign.id)
    return {
        "campaign": campaign_payload(campaign),
        "steps": [step_payload(step) for step in list_steps(conn, campaign.id)],
        "funnel": funnel,
        "open_jobs": len(open_jobs(conn, campaign.id)),
        "due_now": len(due),
        "next_run_at": next_run_at(conn, campaign.id),
        "runnable_statuses": ["active"],
        "worker": worker_status(conn, account_id=account_id),
        "status_meaning": {
            "draft": "definition still editable, never approved",
            "pending_approval": "approved, waiting to be started",
            "active": "approved and started; the only status the worker leases from",
            "paused": "approved and started, then stopped; jobs are kept",
            "completed": "finished; no further work will be leased",
            "archived": "kept for the record; the worker will never lease from it",
        },
    }


def campaign_funnel_read(
    conn: sqlite3.Connection, account_id: int, campaign_id: int
) -> dict[str, Any]:
    """One campaign's sub-list populations, plus who is still moving."""
    campaign = _visible_campaign(conn, account_id, campaign_id)
    return {
        "campaign_id": campaign.id,
        "campaign_status": campaign.status,
        "runnable": campaign.runnable,
        "funnel": campaign_funnel(conn, campaign.id),
    }


# --------------------------------------------------------------------------
# Leads
# --------------------------------------------------------------------------


def _campaign_memberships(
    conn: sqlite3.Connection, lead_id: int
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            campaign_leads.campaign_id AS campaign_id,
            campaigns.name AS campaign_name,
            campaigns.status AS campaign_status,
            campaign_leads.sublist AS sublist,
            campaign_leads.current_step_ord AS current_step_ord,
            campaign_leads.next_run_at AS next_run_at,
            campaign_leads.attempts AS attempts,
            campaign_leads.last_outcome AS last_outcome
        FROM campaign_leads
        LEFT JOIN campaigns ON campaigns.id = campaign_leads.campaign_id
        WHERE campaign_leads.lead_id = ?
        ORDER BY campaign_leads.campaign_id
        """,
        (lead_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _in_flight_counts(conn: sqlite3.Connection, account_id: int) -> dict[int, int]:
    """How many campaigns each lead is still moving through.

    `queue` and `processing` are the two sub-lists SEQ-01 counts as in flight,
    which is the same arithmetic `campaign_funnel` does.
    """
    rows = conn.execute(
        """
        SELECT campaign_leads.lead_id AS lead_id, COUNT(*) AS total
        FROM campaign_leads
        JOIN campaigns ON campaigns.id = campaign_leads.campaign_id
        WHERE campaigns.account_id = ?
          AND campaign_leads.sublist IN ('queue', 'processing')
        GROUP BY campaign_leads.lead_id
        """,
        (account_id,),
    ).fetchall()
    return {int(row["lead_id"]): int(row["total"]) for row in rows}


def leads_active(
    conn: sqlite3.Connection,
    account_id: int,
    *,
    limit: int | None = DEFAULT_LEAD_LIMIT,
) -> dict[str, Any]:
    """The leads this account may still act on.

    "Active" is `list_leads`' default: every lead the account owns that is not
    blacklisted. Blacklisting is the one flag in this schema that makes a lead
    permanently un-actionable, so it is the honest line to draw. Each lead
    carries `in_flight_campaigns`, so a client can tell an enrolled lead from one
    sitting in the CRM untouched without a second query per lead.
    """
    leads = list_leads(conn, account_id, limit=limit)
    in_flight = _in_flight_counts(conn, account_id)
    payloads = [
        {
            **lead_payload(lead),
            "in_flight_campaigns": in_flight.get(lead.id, 0),
        }
        for lead in leads
    ]
    total = conn.execute(
        "SELECT COUNT(*) AS total FROM leads WHERE account_id = ?", (account_id,)
    ).fetchone()["total"]
    return {
        "count": len(payloads),
        "leads": payloads,
        "limit": limit,
        "total_leads_including_blacklisted": int(total),
        "definition": "leads owned by this account that are not blacklisted",
    }


def lead_detail(
    conn: sqlite3.Connection, account_id: int, lead_id: int
) -> dict[str, Any]:
    """One lead, its tags, its campaign memberships and its message counts."""
    lead = get_lead(conn, int(lead_id))
    if lead is None or lead.account_id != account_id:
        raise LeadNotVisible(f"no lead {lead_id} for this account")
    counts = conn.execute(
        """
        SELECT
            SUM(CASE WHEN direction = 'inbound' THEN 1 ELSE 0 END) AS inbound,
            SUM(CASE WHEN direction = 'outbound' THEN 1 ELSE 0 END) AS outbound
        FROM messages
        WHERE lead_id = ?
        """,
        (lead.id,),
    ).fetchone()
    return {
        "lead": {
            **lead_payload(lead),
            "summary": lead.summary,
            "badges": dict(lead.badges),
            "contact_info_fetched_at": lead.contact_info_fetched_at,
            "positions_fetched_at": lead.positions_fetched_at,
        },
        "tags": lead_tag_names(conn, lead.id),
        "campaigns": _campaign_memberships(conn, lead.id),
        "messages": {
            "inbound": int(counts["inbound"] or 0),
            "outbound": int(counts["outbound"] or 0),
        },
    }


# --------------------------------------------------------------------------
# Drafts
# --------------------------------------------------------------------------


def _draft_payload(draft: Any) -> dict[str, Any]:
    return {
        "id": draft.id,
        "campaign_id": draft.campaign_id,
        "lead_id": draft.lead_id,
        "step_id": draft.step_id,
        "kind": draft.kind,
        "status": draft.status,
        "generated_text": draft.generated_text,
        "verdict": dict(draft.verdict) if draft.verdict else None,
        "model": draft.model,
        "created_at": draft.created_at,
        "decided_at": draft.decided_at,
    }


def drafts_pending(
    conn: sqlite3.Connection,
    account_id: int,
    *,
    limit: int | None = DEFAULT_DRAFT_LIMIT,
) -> dict[str, Any]:
    """Drafts waiting for a human decision, plus the depth of the other queues."""
    drafts = list_pending(
        conn, account_id, status=DRAFTS_PENDING_STATUS, limit=limit
    )
    rows = conn.execute(
        """
        SELECT status, COUNT(*) AS total
        FROM ai_drafts
        WHERE account_id = ?
        GROUP BY status
        """,
        (account_id,),
    ).fetchall()
    return {
        "status": DRAFTS_PENDING_STATUS,
        "count": len(drafts),
        "drafts": [_draft_payload(draft) for draft in drafts],
        "limit": limit,
        "queue_depths": {row["status"]: int(row["total"]) for row in rows},
        "definition": (
            "drafts with status 'pending_approval': written, and waiting for a "
            "human to approve or reject them"
        ),
    }


# --------------------------------------------------------------------------
# Inbox
# --------------------------------------------------------------------------


def inbox_unread(
    conn: sqlite3.Connection,
    account_id: int,
    *,
    limit: int | None = DEFAULT_UNREAD_LIMIT,
) -> dict[str, Any]:
    """Threads whose newest stored message is inbound.

    See `linkedin_mcp.resources.inbox` for what that definition covers and, more
    importantly, what it does not: the `messages` table has no read marker and
    nothing scrapes LinkedIn's own unread badge.
    """
    threads = unread_threads(conn, account_id, limit=limit)
    return {
        "count": len(threads),
        "total_unread": unread_thread_count(conn, account_id),
        "threads": threads,
        "limit": limit,
        "definition": UNREAD_DEFINITION,
        "excludes": [
            "LinkedIn's own unread badge, which nothing here scrapes",
            "threads no inbox scan has archived yet",
            "replies answered outside this system, until the next scan sees it",
        ],
    }


# --------------------------------------------------------------------------
# Worker
# --------------------------------------------------------------------------


def worker_status_read(
    conn: sqlite3.Connection,
    account_id: int,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Whether anything is actually running, from the SEQ-04 heartbeat.

    Passed straight through from `linkedin_mcp.worker.worker_status`, which
    already refuses to call a worker alive on the strength of a campaign row
    saying `active`. A worker that died on Friday leaves `campaigns_running`
    False and its own entry `stalled` True with `status` still reading whatever
    phase it was in when it wedged. That last part is the honest bit: `status`
    is what the worker said it was doing, `health` is whether anyone still
    believes it.

    MCP-05 (#28): the payload also carries `paused`, `pause` and
    `active_campaigns`, and `campaigns_running` is False whenever the worker is
    paused or no campaign is runnable. This is the read a client uses to confirm
    that `worker_pause` took effect, so it has to be right in both directions
    rather than only in the safe one.
    """
    report = worker_status(conn, account_id=account_id, now=now)
    report["challenged_accounts"] = open_challenges(conn)
    return report


# --------------------------------------------------------------------------
# Stats and analytics
# --------------------------------------------------------------------------


def _action_rows(
    conn: sqlite3.Connection,
    account_id: int,
    *,
    since: str,
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT action_type, outcome, COUNT(*) AS total
        FROM actions_log
        WHERE account_id = ? AND occurred_at >= ?
        GROUP BY action_type, outcome
        ORDER BY action_type, outcome
        """,
        (account_id, since),
    ).fetchall()


def _fold(rows: list[sqlite3.Row]) -> tuple[dict[str, dict[str, int]], dict[str, int]]:
    by_action: dict[str, dict[str, int]] = {}
    by_outcome: dict[str, int] = {}
    for row in rows:
        action = str(row["action_type"])
        outcome = str(row["outcome"])
        total = int(row["total"])
        by_action.setdefault(action, {})[outcome] = total
        by_outcome[outcome] = by_outcome.get(outcome, 0) + total
    return by_action, by_outcome


def stats_daily(
    conn: sqlite3.Connection,
    account_id: int,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """What this account has done since midnight UTC.

    The window is the calendar day rather than a rolling 24 hours, because this
    is the "what happened today" question. The rolling window that actually
    binds the account is `linkedin://safety/today`, and the two deliberately
    disagree: a run that finished at 23:00 yesterday is out of this count and
    still inside the budget.
    """
    moment = _utc_now(now)
    since = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    rows = _action_rows(conn, account_id, since=_stamp(since))
    by_action, by_outcome = _fold(rows)
    drafts = conn.execute(
        """
        SELECT status, COUNT(*) AS total
        FROM ai_drafts
        WHERE account_id = ? AND COALESCE(decided_at, created_at) >= ?
        GROUP BY status
        """,
        (account_id, _stamp(since)),
    ).fetchall()
    replies = conn.execute(
        """
        SELECT COUNT(*) AS total
        FROM messages
        WHERE account_id = ? AND direction = 'inbound'
          AND COALESCE(sent_at, detected_at, '') >= ?
        """,
        (account_id, _stamp(since)),
    ).fetchone()
    return {
        "window": "calendar day, UTC",
        "since": _stamp(since),
        "actions": sum(by_outcome.values()),
        "by_action_type": by_action,
        "by_outcome": by_outcome,
        "drafts_touched": {row["status"]: int(row["total"]) for row in drafts},
        "replies_received": int(replies["total"] if replies else 0),
    }


def analytics_weekly(
    conn: sqlite3.Connection,
    account_id: int,
    *,
    now: datetime | None = None,
    days: int = WEEKLY_WINDOW_DAYS,
) -> dict[str, Any]:
    """Seven days of outcomes, per day and per action type.

    Includes an invitation acceptance rate, with the caveat attached rather than
    implied: nothing writes `connection_accepted` rows yet, so the numerator is
    zero until something does, and the rate reads as unknown rather than as
    zero percent.
    """
    moment = _utc_now(now)
    since = moment - timedelta(days=days)
    rows = _action_rows(conn, account_id, since=_stamp(since))
    by_action, by_outcome = _fold(rows)

    per_day = conn.execute(
        """
        SELECT substr(occurred_at, 1, 10) AS day, COUNT(*) AS total
        FROM actions_log
        WHERE account_id = ? AND occurred_at >= ?
        GROUP BY day
        ORDER BY day
        """,
        (account_id, _stamp(since)),
    ).fetchall()

    campaigns = list_campaigns(conn, account_id)
    funnels = {
        campaign.id: campaign_funnel(conn, campaign.id) for campaign in campaigns
    }
    rollup: dict[str, int] = {}
    for funnel in funnels.values():
        for key, value in funnel.items():
            rollup[key] = rollup.get(key, 0) + int(value)

    # Only successes count as sent. A `failure` row means the invitation never
    # left, so counting it would inflate the denominator of a rate whose whole
    # point is "of the invitations that went out, how many landed".
    invites = by_action.get("connection_request", {}).get("success", 0)
    accepted = by_action.get("connection_accepted", {}).get("success", 0)
    replies = conn.execute(
        """
        SELECT COUNT(*) AS total
        FROM messages
        WHERE account_id = ? AND direction = 'inbound'
          AND COALESCE(sent_at, detected_at, '') >= ?
        """,
        (account_id, _stamp(since)),
    ).fetchone()

    return {
        "window_days": days,
        "since": _stamp(since),
        "actions": sum(by_outcome.values()),
        "by_action_type": by_action,
        "by_outcome": by_outcome,
        "by_day": {str(row["day"]): int(row["total"]) for row in per_day},
        "campaign_funnels": {str(key): value for key, value in funnels.items()},
        "funnel_rollup": rollup,
        "replies_received": int(replies["total"] if replies else 0),
        "invitations_sent": invites,
        "invitations_accepted": accepted,
        "acceptance_rate": (
            None if invites == 0 or accepted == 0 else round(accepted / invites, 4)
        ),
        "acceptance_rate_caveat": (
            "nothing writes 'connection_accepted' rows yet, so this reads as "
            "unknown rather than as zero"
        ),
    }


# --------------------------------------------------------------------------
# Safety
# --------------------------------------------------------------------------


def _budget_payload(budget: Any) -> dict[str, Any]:
    return {
        "scope": budget.scope,
        "cap": budget.cap,
        "configured_cap": budget.configured,
        "warmup_cap": budget.after_ramp,
        "used": budget.used,
        "remaining": budget.remaining,
        "exhausted": budget.exhausted,
        "warmup_bound": budget.warmup_bound,
        "jitter_fraction": round(budget.jitter_fraction, 4),
    }


def safety_today(
    conn: sqlite3.Connection,
    account_id: int,
    *,
    now: datetime | None = None,
    log: AuditLog | None = None,
) -> dict[str, Any]:
    """Remaining headroom per action type, so the limits are inspectable.

    One entry per metered action type, each with the daily budget and, where the
    action has one, the weekly budget. `remaining` is the number a caller wants:
    how many more of this the gate will allow before it starts refusing.

    Three caps sit above the per-action ones and are reported alongside, because
    an account with twenty invitations left and two of its global daily hundred
    and fifty spent has two, not twenty. `binding_limit` names whichever of them
    is closest to biting.
    """
    moment = _utc_now(now)
    account = conn.execute(
        "SELECT state, timezone, account_age_days FROM accounts WHERE id = ?",
        (account_id,),
    ).fetchone()
    age_days = int(account["account_age_days"]) if account is not None else 0

    actions: dict[str, Any] = {}
    for action_type in sorted(METERED_ACTIONS):
        limit = account_limit(conn, account_id, action_type)
        daily = daily_budget(
            account_id,
            action_type,
            configured_cap=limit.daily_cap,
            now=moment,
            account_age_days=age_days,
            log=log,
        )
        entry: dict[str, Any] = {
            "enabled": limit.enabled,
            "daily": _budget_payload(daily),
            "remaining": daily.remaining if limit.enabled else 0,
        }
        if limit.weekly_cap is not None:
            weekly = weekly_budget(
                account_id,
                action_type,
                configured_cap=limit.weekly_cap,
                now=moment,
                log=log,
            )
            entry["weekly"] = _budget_payload(weekly)
            if limit.enabled:
                entry["remaining"] = min(daily.remaining, weekly.remaining)
        actions[action_type] = entry

    overall = global_daily_budget(
        account_id, now=moment, account_age_days=age_days, log=log
    )
    hourly = hourly_budget(account_id, now=moment, log=log)
    outstanding = pending_invites(account_id, now=moment, log=log)

    ceilings = {
        "global_daily": _budget_payload(overall),
        "global_hourly": _budget_payload(hourly),
        "pending_invites": {
            "cap": PENDING_INVITE_CEILING,
            "used": outstanding,
            "remaining": max(0, PENDING_INVITE_CEILING - outstanding),
            "exhausted": outstanding >= PENDING_INVITE_CEILING,
        },
    }
    binding = min(
        ceilings.items(), key=lambda item: item[1]["remaining"]
    )[0]

    return {
        "account_state": None if account is None else account["state"],
        "account_timezone": None if account is None else account["timezone"],
        "account_age_days": age_days,
        "in_warmup": 0 < age_days < 14,
        "actions": actions,
        "ceilings": ceilings,
        "binding_limit": binding,
        "global_daily_ceiling": GLOBAL_DAILY_CEILING,
        "global_hourly_ceiling": GLOBAL_HOURLY_CEILING,
        "challenged_accounts": open_challenges(conn),
        "recent_events": recent_safety_events(
            conn, account_id, limit=DEFAULT_SAFETY_EVENT_LIMIT
        ),
    }


# --------------------------------------------------------------------------
# Templates
# --------------------------------------------------------------------------


def templates_overview(
    conn: sqlite3.Connection,
    account_id: int,
    *,
    limit: int | None = DEFAULT_TEMPLATE_LIMIT,
) -> dict[str, Any]:
    """Every stored template, with its variations counted rather than dumped."""
    templates = list_templates(conn, account_id, limit=limit)
    payloads = [
        {
            "id": template.id,
            "name": template.name,
            "kind": template.kind,
            "body": template.body,
            "variations": list(template.variations),
            "variation_count": len(template.variations),
            "uses_ai": template.uses_ai,
            "is_ai_generated": template.is_ai_generated,
            "ai_spec": dict(template.ai_spec),
            "created_at": template.created_at,
        }
        for template in templates
    ]
    by_kind: dict[str, int] = {}
    for template in templates:
        by_kind[template.kind] = by_kind.get(template.kind, 0) + 1
    return {
        "count": len(payloads),
        "templates": payloads,
        "by_kind": by_kind,
        "limit": limit,
    }
