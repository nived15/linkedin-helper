"""Building the `context_json` a parked draft carries.

The context is the whole reason a worker can park and walk away. Whatever the
generating client needs to know has to be in the row, because by the time the
client reads it the worker is long gone and the lead may have moved on. So the
context is assembled once, at park time, from data that is already local:
`leads`, `campaigns`, `campaign_steps` and, when the step names one, the
template.

Three blocks, and each earns its place:

* `lead` and `tokens` are who this is about. `tokens` is SEQ-02's own
  :func:`~linkedin_mcp.templating.tokens.lead_context` output, so what the client
  sees is exactly what the template will be able to interpolate.
* `template` names the `{ai_*}` fragments the message is missing. A hybrid
  template with an unfilled fragment refuses to render, so this list is the
  precise definition of "what needs writing".
* `voice` restates the writing style rules as data. A generating client reads
  the constraints out of the row instead of being trusted to remember them, and
  the same rules are then enforced on submission by
  :func:`linkedin_mcp.drafts.store.validate_text`. Telling the client the rule
  and checking the rule are different jobs and both are done.

Nothing here calls a model or reaches the network.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Any

from linkedin_mcp.drafts.store import MAX_TEXT_LENGTH, validate_kind
from linkedin_mcp.leads.store import get_lead
from linkedin_mcp.sequences.campaigns import get_campaign
from linkedin_mcp.sequences.steps import Step
from linkedin_mcp.templating.parser import parse_template
from linkedin_mcp.templating.store import get_template
from linkedin_mcp.templating.style import DEFAULT_STYLE, StylePolicy
from linkedin_mcp.templating.tokens import lead_context


__all__ = [
    "ICP_CONFIG_KEY",
    "draft_context",
    "icp_criteria",
    "lead_summary",
    "style_brief",
    "template_brief",
]

ICP_CONFIG_KEY = "icp"
"""Where a step config keeps its Ideal Customer Profile description."""

_LEAD_FIELDS: tuple[str, ...] = (
    "id",
    "full_name",
    "first_name",
    "last_name",
    "headline",
    "summary",
    "organization_name",
    "organization_title",
    "location_name",
    "member_distance",
    "connection_count",
    "follower_count",
    "public_id",
)


def lead_summary(conn: sqlite3.Connection, lead_id: int) -> dict[str, Any]:
    """Return the lead fields worth putting in front of a model."""
    lead = get_lead(conn, lead_id)
    if lead is None:
        return {"id": lead_id, "missing": True}
    return {
        field: getattr(lead, field)
        for field in _LEAD_FIELDS
        if getattr(lead, field, None) not in (None, "")
    }


def style_brief(
    kind: str,
    policy: StylePolicy = DEFAULT_STYLE,
) -> dict[str, Any]:
    """Return the writing style rules as data the client can act on.

    These are the rules in `.github/copilot-instructions.md`, read straight off
    the policy object SEQ-02 enforces, so this brief cannot drift away from the
    check that will reject the submission.
    """
    brief: dict[str, Any] = {
        "no_dashes": list(policy.forbidden_dashes),
        "banned_openers": list(policy.filler_openers),
        "max_sentence_words": policy.max_sentence_words,
        "guidance": (
            "Write like a practitioner, not a vendor. Short sentences. "
            "Numbers over generalities. No em dashes anywhere, for any reason. "
            "No filler openers. Prose over bullet points."
        ),
    }
    limit = MAX_TEXT_LENGTH.get(kind)
    if limit is not None:
        brief["max_characters"] = limit
    return brief


def template_brief(
    conn: sqlite3.Connection,
    template_id: int | None,
) -> dict[str, Any] | None:
    """Describe the template a step will render, and what it is missing.

    `fragments` is the list of `{ai_*}` names that must be supplied for the
    render to succeed. A hybrid template missing one refuses with
    `MISSING_AI_FRAGMENT` rather than rendering a hole, so this list is not
    advice, it is the render's precondition.
    """
    if template_id is None:
        return None
    template = get_template(conn, template_id)
    if template is None:
        return None

    brief: dict[str, Any] = {
        "id": template.id,
        "name": template.name,
        "kind": template.kind,
        "body": template.body,
    }
    if template.ai_spec:
        brief["ai_spec"] = dict(template.ai_spec)
    try:
        program = parse_template(template.body)
    except Exception:
        # A template that no longer parses is an authoring problem, not a reason
        # to refuse to park a draft. The body is in the brief either way.
        return brief
    brief["fragments"] = list(program.fragments())
    brief["variables"] = list(program.variables())
    return brief


def icp_criteria(step: Step | None) -> Any:
    """Return the ICP description a step config carries, or None."""
    if step is None:
        return None
    return step.config.get(ICP_CONFIG_KEY)


def draft_context(
    conn: sqlite3.Connection,
    kind: str,
    *,
    campaign_id: int | None = None,
    lead_id: int | None = None,
    step: Step | None = None,
    extras: Mapping[str, Any] | None = None,
    policy: StylePolicy = DEFAULT_STYLE,
) -> dict[str, Any]:
    """Assemble the `context_json` payload for one parked draft."""
    validate_kind(kind)
    context: dict[str, Any] = {"kind": kind, "voice": style_brief(kind, policy)}

    if campaign_id is not None:
        campaign = get_campaign(conn, campaign_id)
        context["campaign"] = (
            {"id": campaign_id, "missing": True}
            if campaign is None
            else {
                "id": campaign.id,
                "name": campaign.name,
                "approval_mode": campaign.approval_mode,
            }
        )

    if lead_id is not None:
        context["lead"] = lead_summary(conn, lead_id)
        try:
            context["tokens"] = lead_context(conn, lead_id)
        except LookupError:
            context["tokens"] = {}

    if step is not None:
        context["step"] = {
            "id": step.id,
            "ord": step.ord,
            "action_type": step.action_type,
            "template_id": step.template_id,
        }
        brief = template_brief(conn, step.template_id)
        if brief is not None:
            context["template"] = brief
        criteria = icp_criteria(step)
        if criteria is not None:
            context[ICP_CONFIG_KEY] = criteria

    if kind in ("icp_evaluation",):
        context["expected_output"] = {
            "match": "boolean, does this lead fit the ideal customer profile",
            "score": "number between 0 and 1",
            "reason": "one short sentence a human can audit",
        }

    for key, value in (extras or {}).items():
        context[str(key)] = value
    return context
