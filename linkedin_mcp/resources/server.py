"""Registration of the twelve MCP-04 (#27) resources onto a FastMCP server.

What a resource is allowed to do
--------------------------------
Read SQLite and return JSON. That is the whole contract, and two repository
guards now hold it up:

- `tests/test_actions.py` walks every `@mcp.resource` in the tree the same way
  it walks every `@mcp.tool`, and fails the build if one can reach Playwright,
  directly or through a helper. A resource that opened a browser would be slow,
  would spend the account's LinkedIn budget on a read, and would sidestep the
  job queue MCP-03 built.
- `tests/test_audit_log.py` records that resources are deliberately outside the
  `@audit_linkedin_action` requirement, and pins that none of them takes a
  LinkedIn action. See the note on the exemption below.

Why resources carry no `@audit_linkedin_action`
-----------------------------------------------
Every `@mcp.tool()` in this repository must carry it, because an MCP tool with
no audit row is a LinkedIn action with no trail. A resource is not a LinkedIn
action. It reads rows that are already local, contacts nobody, and changes
nothing.

Auditing them anyway would not be harmlessly noisy, it would be wrong.
`actions_log` is the ledger `linkedin_mcp.safety.limits` counts to decide
whether the account has budget left, and `linkedin://safety/today` reports that
arithmetic. Writing a row every time somebody looked at the dashboard would
consume the account's daily and hourly allowance with reads, and the resource
whose job is to report the remaining headroom would be the one corrupting it.

That is an argument, not a licence: the exemption is written into
`tests/test_audit_log.py` as an explicit case with a test that no resource takes
a LinkedIn action, so a future resource that started sending invitations fails
the build rather than quietly enjoying the exemption.

Errors
------
A resource raises rather than returning `{"status": "error"}`. Tools return the
error shape because an agent has to decide what to do next; a resource read is a
GET, and MCP already has a way to say "no such resource". FastMCP turns the
raised exception into a `ResourceError` carrying the message.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from fastmcp import Context, FastMCP

from linkedin_mcp.resources.contract import (
    ANALYTICS_WEEKLY_URI,
    CAMPAIGNS_URI,
    CAMPAIGN_FUNNEL_URI_TEMPLATE,
    CAMPAIGN_URI_TEMPLATE,
    DRAFTS_PENDING_URI,
    INBOX_UNREAD_URI,
    LEADS_ACTIVE_URI,
    LEAD_URI_TEMPLATE,
    RESOURCE_MIME_TYPE,
    SAFETY_TODAY_URI,
    STATS_DAILY_URI,
    TEMPLATES_URI,
    WORKER_STATUS_URI,
    campaign_funnel_uri,
    campaign_uri,
    envelope,
    lead_uri,
)
from linkedin_mcp.resources.notify import (
    ResourceUpdateNotifier,
    resource_revisions,
    templated_revisions,
)
from linkedin_mcp.resources.reads import (
    analytics_weekly,
    campaign_detail,
    campaign_funnel_read,
    campaigns_overview,
    drafts_pending,
    inbox_unread,
    lead_detail,
    leads_active,
    safety_today,
    stats_daily,
    templates_overview,
    worker_status_read,
)
from linkedin_mcp.tools.runtime import tool_account_id, tool_connection

logger = logging.getLogger(__name__)

__all__ = ["register_linkedin_resources"]


def register_linkedin_resources(mcp: FastMCP) -> ResourceUpdateNotifier:
    """Register all twelve `linkedin://...` resources on `mcp`.

    Returns the notifier so a caller can inspect or reset it. The shipped server
    ignores the return value; the tests do not.

    The connection and the account come from `linkedin_mcp.tools.runtime`, the
    same two seams every MCP-02 and MCP-03 tool resolves through, so a test that
    swaps the audit log has swapped the resources' database too and a resource
    can never report a different account than the tool beside it.
    """
    notifier = ResourceUpdateNotifier()

    def _session(ctx: Context | None) -> Any:
        """Return the live MCP session, or None when read in-process.

        `ctx.session` raises when there is no request in flight, which is
        exactly what happens when a test or a library caller reads a resource
        directly. That is not an error, it is the polling case.
        """
        if ctx is None:
            return None
        try:
            return ctx.session
        except Exception:  # noqa: BLE001 - absence of a session is the normal case
            return None

    async def _updates(
        conn: sqlite3.Connection,
        account_id: int,
        ctx: Context | None,
        *,
        this_uri: str,
        campaign_ids: tuple[int, ...] = (),
        lead_ids: tuple[int, ...] = (),
    ) -> dict[str, Any]:
        """Push or advertise whatever changed since this session last read."""
        revisions = resource_revisions(conn, account_id)
        revisions.update(
            templated_revisions(
                conn, account_id, campaign_ids=campaign_ids, lead_ids=lead_ids
            )
        )
        delivery = await notifier.announce(
            session=_session(ctx), revisions=revisions, exclude=(this_uri,)
        )
        return delivery.as_payload()

    # ------------------------------------------------------------------
    # Campaigns
    # ------------------------------------------------------------------

    @mcp.resource(CAMPAIGNS_URI, mime_type=RESOURCE_MIME_TYPE)
    async def campaigns_resource(ctx: Context | None = None) -> dict:
        """Every campaign this account owns, each with its funnel."""
        conn = tool_connection()
        account_id = tool_account_id()
        payload = campaigns_overview(conn, account_id)
        updates = await _updates(conn, account_id, ctx, this_uri=CAMPAIGNS_URI)
        return envelope(
            CAMPAIGNS_URI, account_id=account_id, payload=payload, updates=updates
        )

    @mcp.resource(CAMPAIGN_URI_TEMPLATE, mime_type=RESOURCE_MIME_TYPE)
    async def campaign_resource(campaign_id: int, ctx: Context | None = None) -> dict:
        """One campaign's live state, including the derived approval flags.

        This is VAL-03. `status` alone is not enough: `pending_approval` means
        "approved, waiting to be started", so the payload carries `approved`,
        `runnable` and `editable` from the same derivation `campaign_status`
        uses, and a `status_meaning` glossary next to them.
        """
        conn = tool_connection()
        account_id = tool_account_id()
        payload = campaign_detail(conn, account_id, campaign_id)
        uri = campaign_uri(campaign_id)
        updates = await _updates(
            conn, account_id, ctx, this_uri=uri, campaign_ids=(int(campaign_id),)
        )
        return envelope(uri, account_id=account_id, payload=payload, updates=updates)

    @mcp.resource(CAMPAIGN_FUNNEL_URI_TEMPLATE, mime_type=RESOURCE_MIME_TYPE)
    async def campaign_funnel_resource(
        campaign_id: int, ctx: Context | None = None
    ) -> dict:
        """One campaign's sub-list populations and the two totals people ask for."""
        conn = tool_connection()
        account_id = tool_account_id()
        payload = campaign_funnel_read(conn, account_id, campaign_id)
        uri = campaign_funnel_uri(campaign_id)
        updates = await _updates(
            conn, account_id, ctx, this_uri=uri, campaign_ids=(int(campaign_id),)
        )
        return envelope(uri, account_id=account_id, payload=payload, updates=updates)

    # ------------------------------------------------------------------
    # Leads
    # ------------------------------------------------------------------

    @mcp.resource(LEADS_ACTIVE_URI, mime_type=RESOURCE_MIME_TYPE)
    async def leads_active_resource(ctx: Context | None = None) -> dict:
        """Leads this account may still act on, meaning the non-blacklisted ones."""
        conn = tool_connection()
        account_id = tool_account_id()
        payload = leads_active(conn, account_id)
        updates = await _updates(conn, account_id, ctx, this_uri=LEADS_ACTIVE_URI)
        return envelope(
            LEADS_ACTIVE_URI, account_id=account_id, payload=payload, updates=updates
        )

    @mcp.resource(LEAD_URI_TEMPLATE, mime_type=RESOURCE_MIME_TYPE)
    async def lead_resource(lead_id: int, ctx: Context | None = None) -> dict:
        """One lead, its tags, its campaign memberships and its message counts."""
        conn = tool_connection()
        account_id = tool_account_id()
        payload = lead_detail(conn, account_id, lead_id)
        uri = lead_uri(lead_id)
        updates = await _updates(
            conn, account_id, ctx, this_uri=uri, lead_ids=(int(lead_id),)
        )
        return envelope(uri, account_id=account_id, payload=payload, updates=updates)

    # ------------------------------------------------------------------
    # Drafts, inbox
    # ------------------------------------------------------------------

    @mcp.resource(DRAFTS_PENDING_URI, mime_type=RESOURCE_MIME_TYPE)
    async def drafts_pending_resource(ctx: Context | None = None) -> dict:
        """Drafts waiting for a human decision, not for AI generation."""
        conn = tool_connection()
        account_id = tool_account_id()
        payload = drafts_pending(conn, account_id)
        updates = await _updates(conn, account_id, ctx, this_uri=DRAFTS_PENDING_URI)
        return envelope(
            DRAFTS_PENDING_URI, account_id=account_id, payload=payload, updates=updates
        )

    @mcp.resource(INBOX_UNREAD_URI, mime_type=RESOURCE_MIME_TYPE)
    async def inbox_unread_resource(ctx: Context | None = None) -> dict:
        """Threads whose newest stored message is inbound: they replied, we did not.

        The `messages` table has no read marker and nothing scrapes LinkedIn's
        own unread badge, so "unread" is defined from `direction` and a
        timestamp. The payload carries both the definition and what it excludes.
        """
        conn = tool_connection()
        account_id = tool_account_id()
        payload = inbox_unread(conn, account_id)
        updates = await _updates(conn, account_id, ctx, this_uri=INBOX_UNREAD_URI)
        return envelope(
            INBOX_UNREAD_URI, account_id=account_id, payload=payload, updates=updates
        )

    # ------------------------------------------------------------------
    # Worker, stats, safety, templates
    # ------------------------------------------------------------------

    @mcp.resource(WORKER_STATUS_URI, mime_type=RESOURCE_MIME_TYPE)
    async def worker_status_resource(ctx: Context | None = None) -> dict:
        """Whether anything is running, from the heartbeat rather than from hope.

        A worker that wedged hours ago still has `status` reading `running`,
        because that is what it wrote before it wedged. Its `health` reads
        `stalled` and `campaigns_running` is False, which is the honest answer.

        MCP-05 (#28) added `paused` and `pause`, and made `campaigns_running`
        mean what it says. All three now have to hold: a worker is live, no
        worker-level pause is in force, and at least one campaign is runnable.
        Before that it was `bool(live)` alone, so a client that stopped the
        worker and read this to confirm was told campaigns were still running.
        """
        conn = tool_connection()
        account_id = tool_account_id()
        payload = worker_status_read(conn, account_id)
        updates = await _updates(conn, account_id, ctx, this_uri=WORKER_STATUS_URI)
        return envelope(
            WORKER_STATUS_URI, account_id=account_id, payload=payload, updates=updates
        )

    @mcp.resource(STATS_DAILY_URI, mime_type=RESOURCE_MIME_TYPE)
    async def stats_daily_resource(ctx: Context | None = None) -> dict:
        """What this account has done since midnight UTC."""
        conn = tool_connection()
        account_id = tool_account_id()
        payload = stats_daily(conn, account_id)
        updates = await _updates(conn, account_id, ctx, this_uri=STATS_DAILY_URI)
        return envelope(
            STATS_DAILY_URI, account_id=account_id, payload=payload, updates=updates
        )

    @mcp.resource(SAFETY_TODAY_URI, mime_type=RESOURCE_MIME_TYPE)
    async def safety_today_resource(ctx: Context | None = None) -> dict:
        """Remaining headroom per action type, plus the three global ceilings."""
        conn = tool_connection()
        account_id = tool_account_id()
        payload = safety_today(conn, account_id)
        updates = await _updates(conn, account_id, ctx, this_uri=SAFETY_TODAY_URI)
        return envelope(
            SAFETY_TODAY_URI, account_id=account_id, payload=payload, updates=updates
        )

    @mcp.resource(ANALYTICS_WEEKLY_URI, mime_type=RESOURCE_MIME_TYPE)
    async def analytics_weekly_resource(ctx: Context | None = None) -> dict:
        """Seven days of outcomes, per day and per action type."""
        conn = tool_connection()
        account_id = tool_account_id()
        payload = analytics_weekly(conn, account_id)
        updates = await _updates(conn, account_id, ctx, this_uri=ANALYTICS_WEEKLY_URI)
        return envelope(
            ANALYTICS_WEEKLY_URI,
            account_id=account_id,
            payload=payload,
            updates=updates,
        )

    @mcp.resource(TEMPLATES_URI, mime_type=RESOURCE_MIME_TYPE)
    async def templates_resource(ctx: Context | None = None) -> dict:
        """Every stored message template this account owns."""
        conn = tool_connection()
        account_id = tool_account_id()
        payload = templates_overview(conn, account_id)
        updates = await _updates(conn, account_id, ctx, this_uri=TEMPLATES_URI)
        return envelope(
            TEMPLATES_URI, account_id=account_id, payload=payload, updates=updates
        )

    return notifier
