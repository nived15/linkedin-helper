"""MCP-05 (#28): the names and shared vocabulary of the six prompts.

Kept apart from `server.py` for the same reason
:mod:`linkedin_mcp.resources.contract` is kept apart from
:mod:`linkedin_mcp.resources.server`: a test that wants to know what the prompt
surface is should not have to build a FastMCP server to find out, and the URIs
a prompt tells a client to read should be imported from the module that defines
them rather than typed again here.
"""

from __future__ import annotations

from linkedin_mcp.resources.contract import (
    ANALYTICS_WEEKLY_URI,
    CAMPAIGNS_URI,
    CAMPAIGN_FUNNEL_URI_TEMPLATE,
    DRAFTS_PENDING_URI,
    INBOX_UNREAD_URI,
    LEADS_ACTIVE_URI,
    SAFETY_TODAY_URI,
    STATS_DAILY_URI,
    TEMPLATES_URI,
    WORKER_STATUS_URI,
)

__all__ = [
    "HARVEST_AUDIENCE",
    "NEW_CAMPAIGN",
    "PROMPT_NAMES",
    "REVIEW_DRAFTS",
    "SAFETY_CHECK",
    "TRIAGE_REPLIES",
    "WEEKLY_REPORT",
    "read_first",
]

NEW_CAMPAIGN = "new_campaign"
REVIEW_DRAFTS = "review_drafts"
TRIAGE_REPLIES = "triage_replies"
WEEKLY_REPORT = "weekly_report"
SAFETY_CHECK = "safety_check"
HARVEST_AUDIENCE = "harvest_audience"

PROMPT_NAMES: tuple[str, ...] = (
    NEW_CAMPAIGN,
    REVIEW_DRAFTS,
    TRIAGE_REPLIES,
    WEEKLY_REPORT,
    SAFETY_CHECK,
    HARVEST_AUDIENCE,
)
"""The six prompts issue #28 names, in the order that issue lists them."""

PROMPT_RESOURCES: dict[str, tuple[str, ...]] = {
    NEW_CAMPAIGN: (CAMPAIGNS_URI, LEADS_ACTIVE_URI, TEMPLATES_URI, SAFETY_TODAY_URI),
    REVIEW_DRAFTS: (DRAFTS_PENDING_URI, CAMPAIGNS_URI),
    TRIAGE_REPLIES: (INBOX_UNREAD_URI, LEADS_ACTIVE_URI),
    WEEKLY_REPORT: (ANALYTICS_WEEKLY_URI, STATS_DAILY_URI, CAMPAIGNS_URI),
    SAFETY_CHECK: (SAFETY_TODAY_URI, WORKER_STATUS_URI, STATS_DAILY_URI),
    HARVEST_AUDIENCE: (LEADS_ACTIVE_URI, SAFETY_TODAY_URI),
}
"""Which resources each prompt tells the client to read before it acts.

Imported from `linkedin_mcp.resources.contract` rather than spelled out, so a
resource URI that moves takes every prompt that mentions it along.
"""

FUNNEL_URI_HINT = CAMPAIGN_FUNNEL_URI_TEMPLATE
"""`linkedin://campaigns/{campaign_id}/funnel`, named once and reused."""


def read_first(name: str) -> str:
    """Return the "read these first" line for one prompt.

    A prompt that told a client what to do without telling it what to read would
    be asking a model to guess the current state, which is the failure mode every
    resource in #27 exists to prevent.
    """
    uris = PROMPT_RESOURCES.get(name, ())
    if not uris:
        return ""
    return "Read these resources before you do anything: " + ", ".join(uris) + "."
