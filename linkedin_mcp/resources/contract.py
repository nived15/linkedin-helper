"""URIs, envelope and delivery vocabulary for the MCP-04 (#27) read surface.

Twelve URIs, and why they are constants
---------------------------------------
The issue names twelve `linkedin://...` URIs. They are written down here once so
the registration code, the revision fingerprints, the notifier and the tests all
agree on the same strings. A URI that only existed as a literal inside a
decorator could be registered under one spelling and notified under another, and
nothing would notice.

Nine are static and three are templated. The templated ones carry a
`{campaign_id}` or `{lead_id}` placeholder, so their canonical form is a template
string rather than a resource URI, and they are kept in a separate tuple for that
reason: `await mcp.list_resources()` returns the nine, and
`await mcp.list_resource_templates()` returns the three.

The envelope
------------
Every resource returns the same outer shape. A client should be able to tell,
from the payload alone and without a second round trip, which URI it is holding,
how fresh it is, which account it describes, and whether it will be told when
the answer changes or has to come back and ask. That last part is the polling
fallback the definition of done asks for, and putting it in the body is what
makes it inspectable rather than a promise made in a README.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

__all__ = [
    "ANALYTICS_WEEKLY_URI",
    "CAMPAIGNS_URI",
    "CAMPAIGN_FUNNEL_URI_TEMPLATE",
    "CAMPAIGN_URI_TEMPLATE",
    "DEFAULT_POLL_AFTER_SECONDS",
    "DRAFTS_PENDING_URI",
    "INBOX_UNREAD_URI",
    "LEADS_ACTIVE_URI",
    "LEAD_URI_TEMPLATE",
    "RESOURCE_MIME_TYPE",
    "RESOURCE_TEMPLATE_URIS",
    "RESOURCE_URIS",
    "SAFETY_TODAY_URI",
    "STATS_DAILY_URI",
    "TEMPLATES_URI",
    "WORKER_STATUS_URI",
    "campaign_funnel_uri",
    "campaign_uri",
    "envelope",
    "lead_uri",
    "resource_timestamp",
]

RESOURCE_MIME_TYPE = "application/json"
"""Declared on every resource, because every one of them returns a JSON object.

FastMCP defaults a resource with no declared type to `text/plain`, which is a
lie about a body that is JSON and makes a client parse by guessing.
"""

# --------------------------------------------------------------------------
# The nine static URIs
# --------------------------------------------------------------------------

CAMPAIGNS_URI = "linkedin://campaigns"
LEADS_ACTIVE_URI = "linkedin://leads/active"
DRAFTS_PENDING_URI = "linkedin://drafts/pending"
INBOX_UNREAD_URI = "linkedin://inbox/unread"
WORKER_STATUS_URI = "linkedin://worker/status"
STATS_DAILY_URI = "linkedin://stats/daily"
SAFETY_TODAY_URI = "linkedin://safety/today"
ANALYTICS_WEEKLY_URI = "linkedin://analytics/weekly"
TEMPLATES_URI = "linkedin://templates"

# --------------------------------------------------------------------------
# The three templated URIs
# --------------------------------------------------------------------------

CAMPAIGN_URI_TEMPLATE = "linkedin://campaigns/{campaign_id}"
CAMPAIGN_FUNNEL_URI_TEMPLATE = "linkedin://campaigns/{campaign_id}/funnel"
LEAD_URI_TEMPLATE = "linkedin://leads/{lead_id}"

RESOURCE_URIS: tuple[str, ...] = (
    CAMPAIGNS_URI,
    LEADS_ACTIVE_URI,
    DRAFTS_PENDING_URI,
    INBOX_UNREAD_URI,
    WORKER_STATUS_URI,
    STATS_DAILY_URI,
    SAFETY_TODAY_URI,
    ANALYTICS_WEEKLY_URI,
    TEMPLATES_URI,
)
"""The nine URIs with no placeholder. Every one is a concrete, readable URI."""

RESOURCE_TEMPLATE_URIS: tuple[str, ...] = (
    CAMPAIGN_URI_TEMPLATE,
    CAMPAIGN_FUNNEL_URI_TEMPLATE,
    LEAD_URI_TEMPLATE,
)
"""The three URI templates. Nine plus three is the twelve the issue lists."""

DEFAULT_POLL_AFTER_SECONDS = 30
"""How long a client that cannot be pushed to should wait before re-reading.

Thirty seconds is `WorkerConfig.tick_seconds`. Polling faster than the worker
ticks cannot reveal anything the previous read did not already show, so a
shorter interval would only cost both sides work. This is advertised in every
envelope rather than documented somewhere a client will not read.
"""


def campaign_uri(campaign_id: int | str) -> str:
    """Return the concrete URI for one campaign."""
    return CAMPAIGN_URI_TEMPLATE.format(campaign_id=campaign_id)


def campaign_funnel_uri(campaign_id: int | str) -> str:
    """Return the concrete URI for one campaign's funnel."""
    return CAMPAIGN_FUNNEL_URI_TEMPLATE.format(campaign_id=campaign_id)


def lead_uri(lead_id: int | str) -> str:
    """Return the concrete URI for one lead."""
    return LEAD_URI_TEMPLATE.format(lead_id=lead_id)


def resource_timestamp(now: datetime | None = None) -> str:
    """Return the `as_of` stamp, in the same format the database stores.

    `worker_heartbeat.last_tick_at`, `actions_log.occurred_at` and every other
    timestamp in this schema are `YYYY-MM-DD HH:MM:SS` in UTC. A resource that
    stamped itself in ISO 8601 with an offset would be the only value in the
    payload a client could not compare against the rest of it.
    """
    moment = now or datetime.now(timezone.utc)
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def envelope(
    uri: str,
    *,
    account_id: int | None,
    payload: dict[str, Any],
    updates: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Wrap one resource's body in the shape every resource returns.

    `uri` is the concrete URI, so a templated read says `linkedin://campaigns/7`
    rather than echoing the placeholder back at the caller.
    """
    return {
        "uri": uri,
        "as_of": resource_timestamp(now),
        "account_id": account_id,
        "updates": updates if updates is not None else {},
        **payload,
    }
