"""MCP-05 (#28): six MCP prompts, so a guided flow is not Copilot-only.

`.github/agents/`, `.github/skills/` and `.github/prompts/` hold five workflows
each, as Markdown that only Copilot in VS Code can see. This package is the same
flows expressed in the protocol, so any MCP client gets them.

- :mod:`~linkedin_mcp.prompts.voice` generates Nived's writing rules from
  `linkedin_mcp/templating/style.py` rather than restating them, so the prompts
  cannot drift from the checker that rejects a template.
- :mod:`~linkedin_mcp.prompts.contract` names the six prompts and imports the
  resource URIs they point clients at.
- :mod:`~linkedin_mcp.prompts.server` registers them.

Nothing here opens a browser, writes to the database or spends an action budget.
Every function is a string builder. See `server.py` for why that means prompts
carry no `@audit_linkedin_action` and for the two repository guards that hold
both halves of it up.
"""

from linkedin_mcp.prompts.contract import (
    HARVEST_AUDIENCE,
    NEW_CAMPAIGN,
    PROMPT_NAMES,
    PROMPT_RESOURCES,
    REVIEW_DRAFTS,
    SAFETY_CHECK,
    TRIAGE_REPLIES,
    WEEKLY_REPORT,
    read_first,
)
from linkedin_mcp.prompts.server import register_linkedin_prompts
from linkedin_mcp.prompts.voice import PERSONA, banned_dash_names, voice_rules

__all__ = [
    "HARVEST_AUDIENCE",
    "NEW_CAMPAIGN",
    "PERSONA",
    "PROMPT_NAMES",
    "PROMPT_RESOURCES",
    "REVIEW_DRAFTS",
    "SAFETY_CHECK",
    "TRIAGE_REPLIES",
    "WEEKLY_REPORT",
    "banned_dash_names",
    "read_first",
    "register_linkedin_prompts",
    "voice_rules",
]
