"""MCP-04 (#27): the `linkedin://...` resource surface.

Before this package the server exposed zero resources and zero prompts. It now
exposes twelve resources, nine concrete and three templated, all reading live
state out of the same SQLite database the tools write to.

Resources are the read half of the MCP interface. A tool is a verb an agent
chooses to perform; a resource is a noun it can look at. Splitting them this way
means an agent can watch a campaign without being offered a button that changes
it, which is what the human-in-the-loop rule in this repository is for.

Layout
------
- `contract` — the twelve URIs, the envelope, the poll interval
- `notify`   — capability detection, `notifications/resources/updated`, fallback
- `inbox`    — what "unread" means, given a `messages` table with no read flag
- `reads`    — the twelve reads, as plain functions over a connection
- `server`   — `register_linkedin_resources`, the `@mcp.resource` bodies
"""

from linkedin_mcp.resources.contract import (
    ANALYTICS_WEEKLY_URI,
    CAMPAIGN_FUNNEL_URI_TEMPLATE,
    CAMPAIGN_URI_TEMPLATE,
    CAMPAIGNS_URI,
    DEFAULT_POLL_AFTER_SECONDS,
    DRAFTS_PENDING_URI,
    INBOX_UNREAD_URI,
    LEAD_URI_TEMPLATE,
    LEADS_ACTIVE_URI,
    RESOURCE_MIME_TYPE,
    RESOURCE_TEMPLATE_URIS,
    RESOURCE_URIS,
    SAFETY_TODAY_URI,
    STATS_DAILY_URI,
    TEMPLATES_URI,
    WORKER_STATUS_URI,
    campaign_funnel_uri,
    campaign_uri,
    envelope,
    lead_uri,
    resource_timestamp,
)
from linkedin_mcp.resources.inbox import (
    DEFAULT_UNREAD_LIMIT,
    UNREAD_DEFINITION,
    unread_thread_count,
    unread_threads,
)
from linkedin_mcp.resources.notify import (
    CLIENT_CAPABILITY_KEY,
    CLIENT_CAPABILITY_PATH,
    Delivery,
    ResourceUpdateNotifier,
    client_supports_resource_updates,
    resource_revisions,
    session_key,
    templated_revisions,
)
from linkedin_mcp.resources.reads import (
    DRAFTS_PENDING_STATUS,
    CampaignNotVisible,
    LeadNotVisible,
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
from linkedin_mcp.resources.server import register_linkedin_resources

ALL_RESOURCE_URIS: tuple[str, ...] = RESOURCE_URIS + RESOURCE_TEMPLATE_URIS
"""All twelve, concrete and templated, in the order the issue lists them."""

__all__ = [
    "ALL_RESOURCE_URIS",
    "ANALYTICS_WEEKLY_URI",
    "CAMPAIGNS_URI",
    "CAMPAIGN_FUNNEL_URI_TEMPLATE",
    "CAMPAIGN_URI_TEMPLATE",
    "CLIENT_CAPABILITY_KEY",
    "CLIENT_CAPABILITY_PATH",
    "DEFAULT_POLL_AFTER_SECONDS",
    "DEFAULT_UNREAD_LIMIT",
    "DRAFTS_PENDING_STATUS",
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
    "UNREAD_DEFINITION",
    "WORKER_STATUS_URI",
    "CampaignNotVisible",
    "Delivery",
    "LeadNotVisible",
    "ResourceUpdateNotifier",
    "analytics_weekly",
    "campaign_detail",
    "campaign_funnel_read",
    "campaign_funnel_uri",
    "campaign_uri",
    "campaigns_overview",
    "client_supports_resource_updates",
    "drafts_pending",
    "envelope",
    "inbox_unread",
    "lead_detail",
    "lead_uri",
    "leads_active",
    "register_linkedin_resources",
    "resource_revisions",
    "resource_timestamp",
    "safety_today",
    "session_key",
    "stats_daily",
    "templated_revisions",
    "templates_overview",
    "unread_thread_count",
    "unread_threads",
    "worker_status_read",
]
