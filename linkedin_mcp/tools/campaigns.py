"""MCP-01 (#24): the twelve campaign control tools.

`campaign_create`, `campaign_add_step`, `campaign_set_template`,
`campaign_set_icp`, `campaign_preview`, `campaign_approve`, `campaign_start`,
`campaign_pause`, `campaign_resume`, `campaign_archive`, `campaign_status` and
`campaign_add_leads`.

Every one of them writes a definition row or flips a status column in the local
SQLite database. Not one of them opens a browser, awaits an executor or reaches
LinkedIn, which is why their action types live in
`linkedin_mcp.core.config.UNMETERED_ACTIONS`: spending an account's scarce daily
budget on a local write would throttle the wrong thing entirely.

The gate, and why it is the status column
-----------------------------------------
`linkedin_mcp.sequences.jobs.due_jobs` inner-joins `campaigns` and filters on
`RUNNABLE_STATUSES`, which is exactly `{"active"}`. So "an unapproved campaign
cannot start" is enforced at the read layer rather than by anything here. What
this module owes that guarantee is narrow and absolute: **no tool may write
`active` except through the gate.** `campaign_create` cannot be asked for a
status at all, and `campaign_start` refuses a campaign that has not been
approved.

Approval is recorded in the status column itself, because a second parallel
approval flag would be a second thing to keep in sync with the one column the
worker actually reads. The lifecycle is::

    draft --approve(True)--> pending_approval --start--> active <--> paused
      ^                            |                        |
      +------- approve(False) -----+------------------------+

`pending_approval` is the only status between `draft` and `active` that the
`campaigns` CHECK constraint in `0001_init.sql` permits, so it carries the
meaning "the definition was approved and is waiting to be started". The name is
inherited from the schema and no migration is added to improve it; the six
statuses and both approval modes this module needs all exist already.

The definition freezes at approval
----------------------------------
`campaign_add_step`, `campaign_set_template` and `campaign_set_icp` work only
while a campaign is a `draft`. Approving a definition and then editing it would
make the approval a signature on a document that has since changed. Reopen with
`campaign_approve(campaign_id, approved=False)` instead, which drops the
campaign back to `draft` and, because `draft` is not runnable, also stops the
worker on its next tick.

Typed errors, not free text
---------------------------
Every failure carries a `reason` from :class:`CampaignErrorReason` and the name
of the exception that produced it. Reasons the sequence engine and the template
engine already have words for are translated rather than reinvented, so
`campaign_add_step` reporting `step_definition_invalid` and
`linkedin_mcp.sequences.StepDefinitionError` are the same fact spelled once.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Sequence
from enum import Enum
from typing import Any, ClassVar

from fastmcp import Context, FastMCP

from linkedin_mcp.audit import audit_linkedin_action
from linkedin_mcp.drafts.routing import (
    ICP_CONFIG_KEY,
    ICP_FILTER_NAME,
    icp_gate_step,
    is_icp_gate,
    register_icp_filter,
)
from linkedin_mcp.leads import (
    LeadNotFoundError,
    get_lead,
    get_tag,
    leads_with_all_tags,
    leads_with_any_tags,
)
from linkedin_mcp.sequences import (
    APPROVAL_MODES,
    LOCAL_ACTIONS,
    MISSING_DATA_DISPOSITIONS,
    ON_FAILURE_MODES,
    ON_FAILURE_RETRY,
    OPEN_JOB_STATES,
    RUNNABLE_STATUSES,
    Campaign,
    CampaignInFlightError,
    CampaignLeadNotFoundError,
    CampaignNotFoundError,
    FilterNotRegisteredError,
    InvalidTransitionError,
    Step,
    StepDefinitionError,
    StepNotFoundError,
    StepSpec,
    Sublist,
    add_step,
    create_campaign,
    due_jobs,
    enrol_leads,
    get_campaign,
    list_campaign_leads,
    list_campaigns,
    list_jobs,
    list_steps,
    set_campaign_status,
    step_at_ord,
    transaction,
)
from linkedin_mcp.templating import (
    RenderRefusal,
    Template,
    TemplateNotFoundError,
    TemplateStyleError,
    TemplateSyntaxError,
    require_template,
    safe_render_for_lead,
)
from linkedin_mcp.tools.runtime import (
    choice,
    positive_int,
    tool_account_id,
    tool_connection,
)
from linkedin_mcp.worker import campaign_funnel, worker_status

logger = logging.getLogger(__name__)

__all__ = [
    "CAMPAIGN_ACTION_TYPES",
    "CAMPAIGN_ADD_LEADS_ACTION",
    "CAMPAIGN_ADD_STEP_ACTION",
    "CAMPAIGN_APPROVE_ACTION",
    "CAMPAIGN_ARCHIVE_ACTION",
    "CAMPAIGN_CREATE_ACTION",
    "CAMPAIGN_PAUSE_ACTION",
    "CAMPAIGN_PREVIEW_ACTION",
    "CAMPAIGN_RESUME_ACTION",
    "CAMPAIGN_SET_ICP_ACTION",
    "CAMPAIGN_SET_TEMPLATE_ACTION",
    "CAMPAIGN_START_ACTION",
    "CAMPAIGN_STATUS_ACTION",
    "CAMPAIGN_TOOLS",
    "CampaignDefinitionIncompleteError",
    "CampaignDefinitionLockedError",
    "CampaignErrorReason",
    "CampaignNotApprovedError",
    "CampaignStatusConflictError",
    "CampaignToolError",
    "MAX_PREVIEW_SAMPLES",
    "NoLeadsEnrolledError",
    "STATUS_ACTIVE",
    "STATUS_APPROVED",
    "STATUS_ARCHIVED",
    "STATUS_COMPLETED",
    "STATUS_DRAFT",
    "STATUS_PAUSED",
    "StepTargetAmbiguousError",
    "TRANSLATED_ERRORS",
    "TemplateNotAttachedError",
    "UnexpectedCampaignFailure",
    "campaign_error_classes",
    "register_campaign_tools",
]

# --------------------------------------------------------------------------
# Audit action types
# --------------------------------------------------------------------------

CAMPAIGN_CREATE_ACTION = "campaign_create"
CAMPAIGN_ADD_STEP_ACTION = "campaign_add_step"
CAMPAIGN_SET_TEMPLATE_ACTION = "campaign_set_template"
CAMPAIGN_SET_ICP_ACTION = "campaign_set_icp"
CAMPAIGN_PREVIEW_ACTION = "campaign_preview"
CAMPAIGN_APPROVE_ACTION = "campaign_approve"
CAMPAIGN_START_ACTION = "campaign_start"
CAMPAIGN_PAUSE_ACTION = "campaign_pause"
CAMPAIGN_RESUME_ACTION = "campaign_resume"
CAMPAIGN_ARCHIVE_ACTION = "campaign_archive"
CAMPAIGN_STATUS_ACTION = "campaign_status"
CAMPAIGN_ADD_LEADS_ACTION = "campaign_add_leads"

CAMPAIGN_ACTION_TYPES: tuple[str, ...] = (
    CAMPAIGN_CREATE_ACTION,
    CAMPAIGN_ADD_STEP_ACTION,
    CAMPAIGN_SET_TEMPLATE_ACTION,
    CAMPAIGN_SET_ICP_ACTION,
    CAMPAIGN_PREVIEW_ACTION,
    CAMPAIGN_APPROVE_ACTION,
    CAMPAIGN_START_ACTION,
    CAMPAIGN_PAUSE_ACTION,
    CAMPAIGN_RESUME_ACTION,
    CAMPAIGN_ARCHIVE_ACTION,
    CAMPAIGN_STATUS_ACTION,
    CAMPAIGN_ADD_LEADS_ACTION,
)
"""Audit action types these tools write.

Every one is in `linkedin_mcp.core.config.UNMETERED_ACTIONS`. The metered
universe is closed by exclusion, so a name left out of that set would silently
spend the account's daily and hourly LinkedIn budget on a local database write.
"""

CAMPAIGN_TOOLS: tuple[str, ...] = CAMPAIGN_ACTION_TYPES
"""Tool names, which are deliberately the same words as the action types."""

# --------------------------------------------------------------------------
# Statuses
# --------------------------------------------------------------------------

STATUS_DRAFT = "draft"
STATUS_APPROVED = "pending_approval"
"""Approved definition, waiting to be started.

The spelling comes from the `campaigns` CHECK constraint in `0001_init.sql`,
which offers no `approved`. Adding one would be a migration this issue does not
need, so the meaning is documented instead of the schema being widened.
"""
STATUS_ACTIVE = "active"
STATUS_PAUSED = "paused"
STATUS_COMPLETED = "completed"
STATUS_ARCHIVED = "archived"

EDITABLE_STATUSES: frozenset[str] = frozenset({STATUS_DRAFT})
"""Statuses in which the step list may still be rewritten."""

APPROVED_STATUSES: frozenset[str] = frozenset(
    {STATUS_APPROVED, STATUS_ACTIVE, STATUS_PAUSED, STATUS_COMPLETED}
)
"""Statuses that can only have been reached by passing `campaign_approve`."""

ENROLABLE_STATUSES: frozenset[str] = frozenset(
    {STATUS_DRAFT, STATUS_APPROVED, STATUS_ACTIVE, STATUS_PAUSED}
)
"""Statuses that still accept new leads. Adding leads is not a definition edit."""

DEFAULT_PREVIEW_SAMPLES = 3
MAX_PREVIEW_SAMPLES = 25
MAX_ADD_LEADS = 1000
TAG_MATCHES = ("any", "all")

# --------------------------------------------------------------------------
# Typed errors
# --------------------------------------------------------------------------


class CampaignErrorReason(str, Enum):
    """The single vocabulary for why a campaign tool did not do the thing."""

    CAMPAIGN_NOT_FOUND = "campaign_not_found"
    CAMPAIGN_NOT_APPROVED = "campaign_not_approved"
    CAMPAIGN_STATUS_CONFLICT = "campaign_status_conflict"
    CAMPAIGN_IN_FLIGHT = "campaign_in_flight"
    DEFINITION_LOCKED = "definition_locked"
    DEFINITION_INCOMPLETE = "definition_incomplete"
    STEP_NOT_FOUND = "step_not_found"
    STEP_TARGET_AMBIGUOUS = "step_target_ambiguous"
    STEP_DEFINITION_INVALID = "step_definition_invalid"
    TEMPLATE_NOT_FOUND = "template_not_found"
    TEMPLATE_NOT_ATTACHED = "template_not_attached"
    TEMPLATE_INVALID = "template_invalid"
    RENDER_REFUSED = "render_refused"
    NO_LEADS_ENROLLED = "no_leads_enrolled"
    LEAD_NOT_FOUND = "lead_not_found"
    LEAD_NOT_ENROLLED = "lead_not_enrolled"
    INVALID_TRANSITION = "invalid_transition"
    FILTER_NOT_REGISTERED = "filter_not_registered"
    INVALID_ARGUMENT = "invalid_argument"
    UNEXPECTED_FAILURE = "unexpected_failure"


class CampaignToolError(Exception):
    """Base class for the refusals this module raises itself.

    Subclasses bind to one :class:`CampaignErrorReason`, exactly as
    `linkedin_mcp.safety.gate.SafetyRefusal` binds to a `RefusalReason`. A
    reason with no class behind it, or a class with no reason, is a free-text
    error wearing a typed coat, so `tests/test_campaign_tools.py` checks the
    two sets agree.
    """

    reason: ClassVar[CampaignErrorReason | None] = None

    def __init__(self, message: str, **detail: Any) -> None:
        if self.reason is None:
            raise TypeError(
                "CampaignToolError is abstract; raise one of its typed subclasses"
            )
        self.detail: dict[str, Any] = {
            key: value for key, value in detail.items() if value is not None
        }
        super().__init__(message)

    def to_result(self) -> dict[str, Any]:
        """Return the MCP tool result for this failure.

        The envelope is written first and the detail only fills gaps, so a
        detail key can never shadow `status`, `reason`, `error` or `message`.
        """
        payload: dict[str, Any] = {
            "status": "error",
            "reason": self.reason.value,
            "error": type(self).__name__,
            "message": str(self),
        }
        for key, value in self.detail.items():
            payload.setdefault(key, value)
        return payload


class CampaignNotApprovedError(CampaignToolError):
    """A start was asked for before the human gate was passed."""

    reason = CampaignErrorReason.CAMPAIGN_NOT_APPROVED


class CampaignStatusConflictError(CampaignToolError):
    """The campaign is in a status from which this move is not offered."""

    reason = CampaignErrorReason.CAMPAIGN_STATUS_CONFLICT


class CampaignDefinitionLockedError(CampaignToolError):
    """An approved or running campaign cannot have its definition rewritten."""

    reason = CampaignErrorReason.DEFINITION_LOCKED


class CampaignDefinitionIncompleteError(CampaignToolError):
    """The definition could not be approved because it could never run."""

    reason = CampaignErrorReason.DEFINITION_INCOMPLETE


class StepTargetAmbiguousError(CampaignToolError):
    """No step was named and the campaign offers more than one candidate."""

    reason = CampaignErrorReason.STEP_TARGET_AMBIGUOUS


class TemplateNotAttachedError(CampaignToolError):
    """A preview was asked for on a campaign whose steps carry no template."""

    reason = CampaignErrorReason.TEMPLATE_NOT_ATTACHED


class NoLeadsEnrolledError(CampaignToolError):
    """A preview was asked for before any real lead was enrolled."""

    reason = CampaignErrorReason.NO_LEADS_ENROLLED


class UnexpectedCampaignFailure(CampaignToolError):
    """Something no tool here anticipated. Loud, typed, and carries the cause."""

    reason = CampaignErrorReason.UNEXPECTED_FAILURE


TRANSLATED_ERRORS: tuple[tuple[type[Exception], CampaignErrorReason], ...] = (
    (CampaignNotFoundError, CampaignErrorReason.CAMPAIGN_NOT_FOUND),
    (CampaignLeadNotFoundError, CampaignErrorReason.LEAD_NOT_ENROLLED),
    (CampaignInFlightError, CampaignErrorReason.CAMPAIGN_IN_FLIGHT),
    (StepNotFoundError, CampaignErrorReason.STEP_NOT_FOUND),
    (StepDefinitionError, CampaignErrorReason.STEP_DEFINITION_INVALID),
    (InvalidTransitionError, CampaignErrorReason.INVALID_TRANSITION),
    (FilterNotRegisteredError, CampaignErrorReason.FILTER_NOT_REGISTERED),
    (LeadNotFoundError, CampaignErrorReason.LEAD_NOT_FOUND),
    (TemplateNotFoundError, CampaignErrorReason.TEMPLATE_NOT_FOUND),
    (RenderRefusal, CampaignErrorReason.RENDER_REFUSED),
    (TemplateSyntaxError, CampaignErrorReason.TEMPLATE_INVALID),
    (TemplateStyleError, CampaignErrorReason.TEMPLATE_INVALID),
    (ValueError, CampaignErrorReason.INVALID_ARGUMENT),
)
"""Typed exceptions other packages already own, mapped onto this vocabulary.

Order matters: the first matching entry wins, so the specific subclasses come
before the bases they inherit from.
"""


def campaign_error_classes() -> tuple[type[CampaignToolError], ...]:
    """Return every concrete `CampaignToolError` subclass, for the guard test."""

    def walk(cls: type[CampaignToolError]) -> list[type[CampaignToolError]]:
        found: list[type[CampaignToolError]] = []
        for child in cls.__subclasses__():
            if child.reason is not None:
                found.append(child)
            found.extend(walk(child))
        return found

    return tuple(walk(CampaignToolError))


def _translate(error: Exception) -> CampaignErrorReason | None:
    for kind, reason in TRANSLATED_ERRORS:
        if isinstance(error, kind):
            return reason
    return None


def _failure(error: Exception, **extra: Any) -> dict[str, Any]:
    """Turn any exception into the typed failure shape every tool returns."""
    if isinstance(error, CampaignToolError):
        payload = error.to_result()
    else:
        reason = _translate(error)
        if reason is None:
            logger.exception("Unexpected campaign tool failure")
            payload = UnexpectedCampaignFailure(
                f"{type(error).__name__}: {error}", cause=type(error).__name__
            ).to_result()
        else:
            payload = {
                "status": "error",
                "reason": reason.value,
                "error": type(error).__name__,
                "message": str(error),
            }
            if isinstance(error, RenderRefusal):
                payload["refusal"] = error.to_result()
    for key, value in extra.items():
        if value is not None:
            payload.setdefault(key, value)
    logger.error("%s: %s", payload["reason"], payload["message"])
    return payload


# --------------------------------------------------------------------------
# Shared reads and payload shapes
# --------------------------------------------------------------------------


def _require_campaign(
    conn: sqlite3.Connection, account_id: int, campaign_id: int
) -> Campaign:
    """Read a campaign this account owns.

    A campaign belonging to somebody else reads as absent rather than as
    forbidden, because "no" and "yes but not yours" are the same answer to a
    caller who should not have known the id in the first place.
    """
    campaign = get_campaign(conn, int(campaign_id))
    if campaign is None or campaign.account_id != account_id:
        raise CampaignNotFoundError(int(campaign_id))
    return campaign


def _campaign_payload(campaign: Campaign) -> dict[str, Any]:
    return {
        "id": campaign.id,
        "account_id": campaign.account_id,
        "name": campaign.name,
        "status": campaign.status,
        "approval_mode": campaign.approval_mode,
        "exclude_list_id": campaign.exclude_list_id,
        "created_at": campaign.created_at,
        "started_at": campaign.started_at,
        "paused_at": campaign.paused_at,
        "approved": campaign.status in APPROVED_STATUSES,
        "runnable": campaign.runnable,
        "editable": campaign.status in EDITABLE_STATUSES,
    }


def _step_payload(step: Step) -> dict[str, Any]:
    return {
        "id": step.id,
        "ord": step.ord,
        "action_type": step.action_type,
        "config": dict(step.config),
        "template_id": step.template_id,
        "bunch_size": step.bunch_size,
        "on_failure": step.on_failure,
        "on_missing_data": step.on_missing_data,
        "delay_seconds": step.delay_seconds,
        "is_filter": step.is_filter,
        "is_local": step.is_local,
        "is_icp_gate": is_icp_gate(step),
    }


def _require_editable(campaign: Campaign) -> None:
    """Refuse to rewrite a definition somebody has already signed off."""
    if campaign.status in EDITABLE_STATUSES:
        return
    raise CampaignDefinitionLockedError(
        f"campaign {campaign.id} is {campaign.status!r}, so its definition is "
        "frozen; call campaign_approve(approved=False) to reopen it as a draft",
        campaign_id=campaign.id,
        campaign_status=campaign.status,
    )


def _resolve_template(
    conn: sqlite3.Connection, account_id: int, ref: int | str
) -> Template:
    """Resolve a template by id or by name, refusing another account's row."""
    if isinstance(ref, str) and ref.strip().lstrip("-").isdigit():
        ref = int(ref.strip())
    template = require_template(conn, ref, account_id=account_id)
    if template.account_id is not None and template.account_id != account_id:
        raise TemplateNotFoundError(ref)
    return template


def _templated_step(
    campaign_id: int, steps: Sequence[Step], position: int | None
) -> Step:
    """Pick the step a preview or a template write is aimed at."""
    if position is not None:
        wanted = int(position)
        for step in steps:
            if step.ord == wanted:
                return step
        raise StepNotFoundError(campaign_id, wanted)

    candidates = [step for step in steps if step.template_id is not None]
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        outreach = [step for step in steps if step.action_type not in LOCAL_ACTIONS]
        if len(outreach) == 1:
            return outreach[0]
        raise StepTargetAmbiguousError(
            "no step was named and none carries a template; pass position=<ord>",
            campaign_id=campaign_id,
            ords=[step.ord for step in steps],
        )
    raise StepTargetAmbiguousError(
        "several steps carry a template; pass position=<ord> to say which one",
        campaign_id=campaign_id,
        ords=[step.ord for step in candidates],
    )


def _open_jobs(conn: sqlite3.Connection, campaign_id: int) -> list[Any]:
    return list_jobs(conn, campaign_id=campaign_id, states=OPEN_JOB_STATES)


def _next_run_at(conn: sqlite3.Connection, campaign_id: int) -> str | None:
    scheduled = [job.scheduled_for for job in _open_jobs(conn, campaign_id)]
    return min(scheduled) if scheduled else None


def _status_change(
    conn: sqlite3.Connection,
    campaign: Campaign,
    target: str,
    *,
    message: str,
) -> dict[str, Any]:
    """Flip one status column and describe the flip. This is the whole of it."""
    if campaign.status == target:
        return {
            "status": "success",
            "changed": False,
            "campaign": _campaign_payload(campaign),
            "previous_status": campaign.status,
            "message": f"campaign {campaign.id} is already {target!r}",
        }
    updated = set_campaign_status(conn, campaign.id, target)
    return {
        "status": "success",
        "changed": True,
        "campaign": _campaign_payload(updated),
        "previous_status": campaign.status,
        "message": message,
    }


def _selected_lead_ids(
    conn: sqlite3.Connection,
    account_id: int,
    *,
    lead_ids: Sequence[int] | None,
    tags: Sequence[str] | None,
    match: str,
) -> list[int]:
    """Resolve the leads to enrol from explicit ids, from tags, or from both."""
    resolved: list[int] = []
    seen: set[int] = set()

    for raw in lead_ids or ():
        lead_id = int(raw)
        lead = get_lead(conn, lead_id)
        if lead is None or lead.account_id != account_id:
            raise LeadNotFoundError(f"lead {lead_id} does not exist for this account")
        if lead_id not in seen:
            seen.add(lead_id)
            resolved.append(lead_id)

    wanted = [str(name).strip() for name in (tags or []) if str(name).strip()]
    if wanted:
        query = leads_with_all_tags if match == "all" else leads_with_any_tags
        for lead in query(conn, account_id, wanted):
            if lead.id not in seen:
                seen.add(lead.id)
                resolved.append(lead.id)

    if not resolved:
        raise ValueError(
            "give lead_ids, or tags naming leads to enrol; neither matched anybody"
        )
    return resolved


# --------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------


def register_campaign_tools(mcp: FastMCP) -> None:
    """Register the twelve MCP-01 campaign control tools on `mcp`.

    They are defined here rather than in `linkedin_browser_mcp.py` so that the
    entry point stays owned by one issue and the tools can be exercised against
    a bare `FastMCP` in a test. Wiring them into the server is the single line
    `register_campaign_tools(mcp)`.
    """

    @mcp.tool()
    @audit_linkedin_action(
        CAMPAIGN_CREATE_ACTION, target="name", capture=("approval_mode",)
    )
    async def campaign_create(
        name: str,
        approval_mode: str = "manual_drafts",
        exclude_list_id: int | None = None,
        exclude_tag: str | None = None,
        ctx: Context | None = None,
    ) -> dict:
        """Create a campaign. It always starts as a `draft` and runs nothing.

        There is deliberately no `status` argument. `campaign_approve` is the
        only human gate and `campaign_start` is the only way to reach `active`,
        so a campaign cannot be created already running.

        Args:
            name: what the campaign is called. Must not be blank.
            approval_mode: `manual_drafts` (the default) holds every generated
                message for review. `auto` releases drafts as soon as they are
                submitted, which is a per-campaign opt-in and not the default.
            exclude_list_id: a `tags.id`. Every lead carrying that tag enrols
                straight into `excluded`, which keeps a do-not-contact audience
                out of this campaign without blacklisting anybody globally.
            exclude_tag: the same thing named as a tag, resolved for you.
        """
        try:
            conn = tool_connection()
            account_id = tool_account_id()
            mode = choice("approval_mode", approval_mode, APPROVAL_MODES)

            resolved_exclude = None if exclude_list_id is None else int(exclude_list_id)
            if exclude_tag:
                tag = get_tag(conn, account_id, str(exclude_tag))
                if tag is None:
                    raise ValueError(f"no tag named {exclude_tag!r} exists")
                resolved_exclude = tag.id

            campaign = create_campaign(
                conn,
                account_id,
                str(name),
                status=STATUS_DRAFT,
                approval_mode=mode,
                exclude_list_id=resolved_exclude,
            )
            return {
                "status": "success",
                "campaign": _campaign_payload(campaign),
                "message": (
                    f"Created campaign {campaign.id} as a draft. Add steps, then "
                    "campaign_approve it, then campaign_start it."
                ),
            }
        except Exception as error:
            return _failure(error, name=name)

    @mcp.tool()
    @audit_linkedin_action(
        CAMPAIGN_ADD_STEP_ACTION,
        target="campaign_id",
        capture=("action_type", "position"),
    )
    async def campaign_add_step(
        campaign_id: int,
        action_type: str,
        config: dict | None = None,
        template_id: int | None = None,
        bunch_size: int = 1,
        on_failure: str = ON_FAILURE_RETRY,
        on_missing_data: str | None = None,
        position: int | None = None,
        ctx: Context | None = None,
    ) -> dict:
        """Append a step to a draft campaign, or insert it at `position`.

        Args:
            campaign_id: the campaign to extend.
            action_type: what the step does. `filter` decides whether a lead
                stays in the flow and needs `config.filter`. Everything else is
                an outreach action the worker executes, for example
                `connection_request`, `message`, `profile_view` or `post_like`.
            config: step configuration. `delay_seconds` is how long after the
                previous step this one becomes due, `max_attempts` and
                `retry_backoff_seconds` shape retries, `priority` orders the
                queue, and for a filter step `on_no_match` picks the exit.
            template_id: the message template this step renders.
            bunch_size: how many leads one execution handles.
            on_failure: `retry`, `skip` or `fail`.
            on_missing_data: `visit_extract` to fetch the gap, or `skip`.
            position: 1-based ord to insert at. Omit to append.
        """
        try:
            conn = tool_connection()
            account_id = tool_account_id()
            campaign = _require_campaign(conn, account_id, campaign_id)
            _require_editable(campaign)

            template = (
                None
                if template_id is None
                else _resolve_template(conn, account_id, int(template_id))
            )
            spec = StepSpec(
                action_type=str(action_type or "").strip(),
                config=dict(config or {}),
                template_id=None if template is None else template.id,
                bunch_size=int(bunch_size),
                on_failure=choice("on_failure", on_failure, ON_FAILURE_MODES),
                on_missing_data=(
                    None
                    if on_missing_data is None
                    else choice(
                        "on_missing_data", on_missing_data, MISSING_DATA_DISPOSITIONS
                    )
                ),
            )
            step = add_step(
                conn,
                campaign.id,
                spec,
                ord_=None if position is None else int(position),
            )
            return {
                "status": "success",
                "campaign_id": campaign.id,
                "step": _step_payload(step),
                "steps": [_step_payload(row) for row in list_steps(conn, campaign.id)],
                "message": (
                    f"Added a {step.action_type!r} step at ord {step.ord} of "
                    f"campaign {campaign.id}."
                ),
            }
        except Exception as error:
            return _failure(error, campaign_id=campaign_id)

    @mcp.tool()
    @audit_linkedin_action(
        CAMPAIGN_SET_TEMPLATE_ACTION, target="campaign_id", capture=("position",)
    )
    async def campaign_set_template(
        campaign_id: int,
        template: int | str,
        position: int | None = None,
        ctx: Context | None = None,
    ) -> dict:
        """Attach a stored message template to one step of a draft campaign.

        The template keeps living in `templates`, with its variables, spintax
        and whole-message variations; the step only points at it. Rendering
        happens when the job runs, never here.

        Args:
            campaign_id: the campaign to change.
            template: a template id, or a template name within this account.
            position: which step, by 1-based ord. Omit it when the campaign has
                exactly one step that could carry a template.
        """
        try:
            conn = tool_connection()
            account_id = tool_account_id()
            campaign = _require_campaign(conn, account_id, campaign_id)
            _require_editable(campaign)

            steps = list_steps(conn, campaign.id)
            if not steps:
                raise CampaignDefinitionIncompleteError(
                    f"campaign {campaign.id} has no steps to attach a template to",
                    campaign_id=campaign.id,
                )
            target = _templated_step(campaign.id, steps, position)
            resolved = _resolve_template(conn, account_id, template)

            with transaction(conn):
                conn.execute(
                    "UPDATE campaign_steps SET template_id = ? WHERE id = ?",
                    (resolved.id, target.id),
                )
            updated = step_at_ord(conn, campaign.id, target.ord)
            return {
                "status": "success",
                "campaign_id": campaign.id,
                "step": _step_payload(updated),
                "template": {
                    "id": resolved.id,
                    "name": resolved.name,
                    "kind": resolved.kind,
                    "variations": len(resolved.bodies()),
                },
                "message": (
                    f"Step {updated.ord} of campaign {campaign.id} now renders "
                    f"template {resolved.id} ({resolved.name!r})."
                ),
            }
        except Exception as error:
            return _failure(error, campaign_id=campaign_id)

    @mcp.tool()
    @audit_linkedin_action(
        CAMPAIGN_SET_ICP_ACTION, target="campaign_id", capture=("threshold",)
    )
    async def campaign_set_icp(
        campaign_id: int,
        description: str,
        required_fields: list[str] | None = None,
        optional_fields: list[str] | None = None,
        excluded_fields: list[str] | None = None,
        threshold: float | None = None,
        position: int | None = None,
        ctx: Context | None = None,
    ) -> dict:
        """Attach a plain-language ideal customer profile as a qualification gate.

        The gate is an ordinary `filter` step, so it reaches nothing on LinkedIn
        and costs no budget to run. It goes in front of the outreach steps by
        default, which is the point: qualifying is free and an invitation is the
        scarcest thing the account has.

        Calling this twice rewrites the existing gate rather than stacking a
        second one.

        Args:
            campaign_id: the campaign to qualify for.
            description: the ICP in plain language, as a human would say it.
            required_fields: profile fields a match must have.
            optional_fields: fields that count in a match's favour.
            excluded_fields: signals that rule a lead out.
            threshold: minimum verdict score, 0 to 1, for a lead to pass.
            position: 1-based ord to place the gate at. Defaults to the front.
        """
        try:
            conn = tool_connection()
            account_id = tool_account_id()
            campaign = _require_campaign(conn, account_id, campaign_id)
            _require_editable(campaign)

            text = str(description or "").strip()
            if not text:
                raise ValueError("an ICP needs a plain-language description")
            score = None if threshold is None else float(threshold)
            if score is not None and not 0.0 <= score <= 1.0:
                raise ValueError(f"threshold must be between 0 and 1, got {score}")

            icp: dict[str, Any] = {"description": text}
            for key, values in (
                ("required", required_fields),
                ("optional", optional_fields),
                ("excluded", excluded_fields),
            ):
                cleaned = [str(name).strip() for name in (values or []) if str(name).strip()]
                if cleaned:
                    icp[key] = cleaned
            if score is not None:
                icp["threshold"] = score

            extra: dict[str, Any] = {}
            if score is not None:
                # `min_score` is the key the registered `icp_match` predicate
                # reads. It lives at the top of the step config, beside the
                # criteria rather than inside them.
                extra["min_score"] = score

            # Registering is explicit by design in SEQ-05, so a campaign that
            # names the predicate makes sure it exists. It is a process-local
            # registry write and is idempotent.
            register_icp_filter()

            spec = icp_gate_step(icp, config=extra)
            steps = list_steps(conn, campaign.id)
            existing = next((step for step in steps if is_icp_gate(step)), None)

            if existing is None:
                step = add_step(
                    conn,
                    campaign.id,
                    spec,
                    ord_=1 if position is None else int(position),
                )
                replaced = False
            else:
                with transaction(conn):
                    conn.execute(
                        "UPDATE campaign_steps SET config_json = ? WHERE id = ?",
                        (json.dumps(dict(spec.config), sort_keys=True), existing.id),
                    )
                step = step_at_ord(conn, campaign.id, existing.ord)
                replaced = True

            return {
                "status": "success",
                "campaign_id": campaign.id,
                "step": _step_payload(step),
                "replaced": replaced,
                "filter": ICP_FILTER_NAME,
                "icp": dict(step.config.get(ICP_CONFIG_KEY, {})),
                "steps": [_step_payload(row) for row in list_steps(conn, campaign.id)],
                "message": (
                    f"Campaign {campaign.id} qualifies leads at ord {step.ord} "
                    "before any outreach step spends an invite."
                ),
            }
        except Exception as error:
            return _failure(error, campaign_id=campaign_id)

    @mcp.tool()
    @audit_linkedin_action(
        CAMPAIGN_PREVIEW_ACTION, target="campaign_id", capture=("samples", "position")
    )
    async def campaign_preview(
        campaign_id: int,
        samples: int = DEFAULT_PREVIEW_SAMPLES,
        position: int | None = None,
        ctx: Context | None = None,
    ) -> dict:
        """Show the workflow and render sample messages for real enrolled leads.

        Read-only, and it renders against the people actually enrolled rather
        than invented examples, because a template that reads beautifully with
        made-up data is exactly the one that produces "Hi ," in production.

        A lead whose render refuses comes back as a typed refusal with its
        reason and the sub-list the engine would move it to. No half-filled text
        is ever returned, so nothing here can be copied into a message by
        mistake.

        Args:
            campaign_id: the campaign to preview.
            samples: how many leads to render for, capped at 25.
            position: which step, by 1-based ord. Omit to use the step carrying
                the template.
        """
        try:
            conn = tool_connection()
            account_id = tool_account_id()
            campaign = _require_campaign(conn, account_id, campaign_id)
            steps = list_steps(conn, campaign.id)
            if not steps:
                raise CampaignDefinitionIncompleteError(
                    f"campaign {campaign.id} has no steps, so there is nothing to preview",
                    campaign_id=campaign.id,
                )

            wanted = positive_int(
                "samples", samples, default=DEFAULT_PREVIEW_SAMPLES,
                maximum=MAX_PREVIEW_SAMPLES,
            )
            target = _templated_step(campaign.id, steps, position)
            if target.template_id is None:
                raise TemplateNotAttachedError(
                    f"step {target.ord} of campaign {campaign.id} carries no "
                    "template; call campaign_set_template first",
                    campaign_id=campaign.id,
                    ord=target.ord,
                )
            template = _resolve_template(conn, account_id, int(target.template_id))

            enrolled = [
                record
                for record in list_campaign_leads(conn, campaign.id)
                if record.sublist != Sublist.EXCLUDED.value
            ]
            if not enrolled:
                raise NoLeadsEnrolledError(
                    f"campaign {campaign.id} has no enrolled leads to render for; "
                    "call campaign_add_leads first",
                    campaign_id=campaign.id,
                )

            rendered: list[dict[str, Any]] = []
            for sequence, record in enumerate(enrolled[:wanted]):
                result = safe_render_for_lead(
                    conn, template, record.lead_id, sequence=sequence
                )
                if result.ok:
                    message = result.rendered
                    rendered.append(
                        {
                            "status": "success",
                            "lead_id": record.lead_id,
                            "sublist": record.sublist,
                            "sequence": sequence,
                            "variation_index": message.variation_index,
                            "text": message.text,
                            "characters": len(message.text),
                            "tokens_used": list(message.tokens_used),
                            "fragments_used": list(message.fragments_used),
                            "warnings": list(message.warnings),
                        }
                    )
                else:
                    refusal = result.refusal
                    rendered.append(
                        {
                            "status": "refused",
                            "lead_id": record.lead_id,
                            "sublist": record.sublist,
                            "sequence": sequence,
                            "reason": refusal.reason.value,
                            "error": type(refusal).__name__,
                            "message": str(refusal),
                            "would_move_to": refusal.sublist,
                            "awaiting_ai": refusal.is_awaiting_ai,
                        }
                    )

            refused = sum(1 for sample in rendered if sample["status"] == "refused")
            return {
                "status": "success",
                "campaign": _campaign_payload(campaign),
                "workflow": [_step_payload(row) for row in steps],
                "step": _step_payload(target),
                "template": {
                    "id": template.id,
                    "name": template.name,
                    "kind": template.kind,
                    "variations": len(template.bodies()),
                },
                "enrolled": len(enrolled),
                "rendered": len(rendered) - refused,
                "refused": refused,
                "samples": rendered,
                "message": (
                    f"Rendered {len(rendered) - refused} of {len(rendered)} sample(s) "
                    f"for campaign {campaign.id}. Nothing was sent and nothing was "
                    "written."
                ),
            }
        except Exception as error:
            return _failure(error, campaign_id=campaign_id)

    @mcp.tool()
    @audit_linkedin_action(
        CAMPAIGN_APPROVE_ACTION, target="campaign_id", capture=("approved", "note")
    )
    async def campaign_approve(
        campaign_id: int,
        approved: bool = True,
        note: str | None = None,
        ctx: Context | None = None,
    ) -> dict:
        """The single human gate. Approve a campaign definition, or revoke it.

        Approving moves a `draft` to `pending_approval`, which means the
        definition has been signed off and the campaign is waiting for
        `campaign_start`. Nothing runs yet: `active` is the only status the
        worker executes and only `campaign_start` writes it.

        Approving also freezes the definition. `campaign_add_step`,
        `campaign_set_template` and `campaign_set_icp` refuse afterwards,
        because editing an approved definition would make the approval a
        signature on a document that has since changed.

        Pass `approved=False` to revoke. That drops the campaign back to
        `draft`, which is not runnable, so a running campaign stops on the
        worker's next tick and its definition is editable again.
        """
        try:
            conn = tool_connection()
            account_id = tool_account_id()
            campaign = _require_campaign(conn, account_id, campaign_id)

            if campaign.status in (STATUS_COMPLETED, STATUS_ARCHIVED):
                raise CampaignStatusConflictError(
                    f"campaign {campaign.id} is {campaign.status!r} and cannot be "
                    "approved or reopened",
                    campaign_id=campaign.id,
                    campaign_status=campaign.status,
                )

            if not approved:
                result = _status_change(
                    conn,
                    campaign,
                    STATUS_DRAFT,
                    message=(
                        f"Approval revoked for campaign {campaign.id}. It is a draft "
                        "again, which is not runnable, so the worker stops on its "
                        "next tick."
                    ),
                )
                result["approved"] = False
                if note:
                    result["note"] = note
                return result

            steps = list_steps(conn, campaign.id)
            if not steps:
                raise CampaignDefinitionIncompleteError(
                    f"campaign {campaign.id} has no steps, so there is nothing to "
                    "approve",
                    campaign_id=campaign.id,
                )
            for step in steps:
                if step.template_id is not None:
                    _resolve_template(conn, account_id, int(step.template_id))

            result = _status_change(
                conn,
                campaign,
                STATUS_APPROVED,
                message=(
                    f"Campaign {campaign.id} is approved with {len(steps)} step(s). "
                    "Call campaign_start to hand it to the worker."
                ),
            )
            result["approved"] = True
            result["steps"] = [_step_payload(step) for step in steps]
            if note:
                result["note"] = note
            return result
        except Exception as error:
            return _failure(error, campaign_id=campaign_id)

    @mcp.tool()
    @audit_linkedin_action(CAMPAIGN_START_ACTION, target="campaign_id")
    async def campaign_start(
        campaign_id: int,
        ctx: Context | None = None,
    ) -> dict:
        """Hand an approved campaign to the worker by flipping its status.

        This writes one column and returns. It launches nothing, sends nothing
        and waits for nothing: the background worker leases due jobs on its next
        tick, and it only ever sees campaigns whose status is `active`.

        A campaign that has not been through `campaign_approve` is refused here
        with `campaign_not_approved`. Use `campaign_resume` for a paused one.
        """
        try:
            conn = tool_connection()
            account_id = tool_account_id()
            campaign = _require_campaign(conn, account_id, campaign_id)

            if campaign.status == STATUS_DRAFT:
                raise CampaignNotApprovedError(
                    f"campaign {campaign.id} is a draft and has not been approved; "
                    "call campaign_approve first",
                    campaign_id=campaign.id,
                    campaign_status=campaign.status,
                )
            if campaign.status == STATUS_PAUSED:
                raise CampaignStatusConflictError(
                    f"campaign {campaign.id} is paused; call campaign_resume",
                    campaign_id=campaign.id,
                    campaign_status=campaign.status,
                )
            if campaign.status in (STATUS_COMPLETED, STATUS_ARCHIVED):
                raise CampaignStatusConflictError(
                    f"campaign {campaign.id} is {campaign.status!r} and cannot start",
                    campaign_id=campaign.id,
                    campaign_status=campaign.status,
                )

            result = _status_change(
                conn,
                campaign,
                STATUS_ACTIVE,
                message=(
                    f"Campaign {campaign.id} is active. The worker picks its due "
                    "jobs up on the next tick; nothing was started here."
                ),
            )
            result["due_now"] = len(
                due_jobs(conn, account_id, campaign_id=campaign.id)
            )
            result["next_run_at"] = _next_run_at(conn, campaign.id)
            return result
        except Exception as error:
            return _failure(error, campaign_id=campaign_id)

    @mcp.tool()
    @audit_linkedin_action(CAMPAIGN_PAUSE_ACTION, target="campaign_id")
    async def campaign_pause(
        campaign_id: int,
        ctx: Context | None = None,
    ) -> dict:
        """Stop a running campaign without throwing its queue away.

        Load-bearing rather than advisory: `due_jobs` filters on the campaign
        status, so a paused campaign yields no work on the very next tick even
        though every job row stays exactly where it was. Resuming picks up where
        it stopped.
        """
        try:
            conn = tool_connection()
            account_id = tool_account_id()
            campaign = _require_campaign(conn, account_id, campaign_id)

            if campaign.status not in (STATUS_ACTIVE, STATUS_PAUSED):
                raise CampaignStatusConflictError(
                    f"campaign {campaign.id} is {campaign.status!r}, so there is "
                    "nothing running to pause",
                    campaign_id=campaign.id,
                    campaign_status=campaign.status,
                )

            result = _status_change(
                conn,
                campaign,
                STATUS_PAUSED,
                message=(
                    f"Campaign {campaign.id} is paused. Its queue is intact and the "
                    "worker leases nothing from it until it is resumed."
                ),
            )
            result["open_jobs"] = len(_open_jobs(conn, campaign.id))
            return result
        except Exception as error:
            return _failure(error, campaign_id=campaign_id)

    @mcp.tool()
    @audit_linkedin_action(CAMPAIGN_RESUME_ACTION, target="campaign_id")
    async def campaign_resume(
        campaign_id: int,
        ctx: Context | None = None,
    ) -> dict:
        """Put a paused campaign back to work. Flips one status column.

        `started_at` is not rewritten, so a campaign paused and resumed keeps
        one honest start date.
        """
        try:
            conn = tool_connection()
            account_id = tool_account_id()
            campaign = _require_campaign(conn, account_id, campaign_id)

            if campaign.status == STATUS_DRAFT:
                raise CampaignNotApprovedError(
                    f"campaign {campaign.id} is a draft and has not been approved; "
                    "call campaign_approve then campaign_start",
                    campaign_id=campaign.id,
                    campaign_status=campaign.status,
                )
            if campaign.status not in (STATUS_PAUSED, STATUS_ACTIVE):
                raise CampaignStatusConflictError(
                    f"campaign {campaign.id} is {campaign.status!r}, so there is "
                    "nothing paused to resume",
                    campaign_id=campaign.id,
                    campaign_status=campaign.status,
                )

            result = _status_change(
                conn,
                campaign,
                STATUS_ACTIVE,
                message=(
                    f"Campaign {campaign.id} is active again. The worker resumes it "
                    "on the next tick."
                ),
            )
            result["due_now"] = len(
                due_jobs(conn, account_id, campaign_id=campaign.id)
            )
            result["next_run_at"] = _next_run_at(conn, campaign.id)
            return result
        except Exception as error:
            return _failure(error, campaign_id=campaign_id)

    @mcp.tool()
    @audit_linkedin_action(CAMPAIGN_ARCHIVE_ACTION, target="campaign_id")
    async def campaign_archive(
        campaign_id: int,
        ctx: Context | None = None,
    ) -> dict:
        """Retire a campaign. Archived campaigns never run again.

        The rows stay: leads keep their history and the funnel still reads, so
        an archived campaign remains auditable. It simply stops being runnable,
        and reopening it means approving it again from `draft`.
        """
        try:
            conn = tool_connection()
            account_id = tool_account_id()
            campaign = _require_campaign(conn, account_id, campaign_id)
            result = _status_change(
                conn,
                campaign,
                STATUS_ARCHIVED,
                message=(
                    f"Campaign {campaign.id} is archived. Its rows are kept and its "
                    "funnel still reads, but the worker will never lease from it."
                ),
            )
            result["funnel"] = campaign_funnel(conn, campaign.id)
            return result
        except Exception as error:
            return _failure(error, campaign_id=campaign_id)

    @mcp.tool()
    @audit_linkedin_action(CAMPAIGN_STATUS_ACTION, target="campaign_id")
    async def campaign_status(
        campaign_id: int | None = None,
        ctx: Context | None = None,
    ) -> dict:
        """Report where a campaign has got to, or list every campaign.

        With no `campaign_id` this lists the account's campaigns with their
        funnels. With one it adds the step list, the next scheduled run, how
        much work is due right now and whether a worker is alive to do it.

        `due_now` is honest about the gate: it is zero for anything that is not
        `active`, because that is exactly what the worker's own read returns.
        """
        try:
            conn = tool_connection()
            account_id = tool_account_id()
            worker = worker_status(conn, account_id=account_id)

            if campaign_id is None:
                campaigns = list_campaigns(conn, account_id)
                return {
                    "status": "success",
                    "count": len(campaigns),
                    "campaigns": [
                        {
                            **_campaign_payload(campaign),
                            "funnel": campaign_funnel(conn, campaign.id),
                        }
                        for campaign in campaigns
                    ],
                    "worker": worker,
                    "message": f"{len(campaigns)} campaign(s) for this account.",
                }

            campaign = _require_campaign(conn, account_id, campaign_id)
            funnel = campaign_funnel(conn, campaign.id)
            open_jobs = _open_jobs(conn, campaign.id)
            due = due_jobs(conn, account_id, campaign_id=campaign.id)
            return {
                "status": "success",
                "campaign": _campaign_payload(campaign),
                "steps": [_step_payload(step) for step in list_steps(conn, campaign.id)],
                "funnel": funnel,
                "open_jobs": len(open_jobs),
                "due_now": len(due),
                "next_run_at": _next_run_at(conn, campaign.id),
                "runnable_statuses": sorted(RUNNABLE_STATUSES),
                "worker": worker,
                "message": (
                    f"Campaign {campaign.id} is {campaign.status!r}: "
                    f"{funnel['in_flight']} lead(s) still in the flow, "
                    f"{funnel['finished']} finished, {len(due)} due now."
                ),
            }
        except Exception as error:
            return _failure(error, campaign_id=campaign_id)

    @mcp.tool()
    @audit_linkedin_action(
        CAMPAIGN_ADD_LEADS_ACTION, target="campaign_id", capture=("tags", "match")
    )
    async def campaign_add_leads(
        campaign_id: int,
        lead_ids: list[int] | None = None,
        tags: list[str] | None = None,
        match: str = "any",
        limit: int = MAX_ADD_LEADS,
        ctx: Context | None = None,
    ) -> dict:
        """Enrol stored leads into a campaign's queue.

        Every lead gets a `campaign_leads` row and, if it is still in the flow,
        one job, both written in the same transaction. A blacklisted lead or one
        carrying the campaign's exclude tag still gets a row, in `excluded`,
        with no job: a record of why somebody was not contacted is worth more
        than no record at all.

        Adding leads is not a definition edit, so it works on a draft, an
        approved, an active or a paused campaign.

        Args:
            campaign_id: the campaign to enrol into.
            lead_ids: explicit lead ids.
            tags: enrol every lead carrying these tags instead, or as well.
            match: `any` (the default) or `all`, for how tags combine.
            limit: most leads to enrol in one call.
        """
        try:
            conn = tool_connection()
            account_id = tool_account_id()
            campaign = _require_campaign(conn, account_id, campaign_id)

            if campaign.status not in ENROLABLE_STATUSES:
                raise CampaignStatusConflictError(
                    f"campaign {campaign.id} is {campaign.status!r} and accepts no "
                    "new leads",
                    campaign_id=campaign.id,
                    campaign_status=campaign.status,
                )

            chosen = _selected_lead_ids(
                conn,
                account_id,
                lead_ids=lead_ids,
                tags=tags,
                match=choice("match", match, TAG_MATCHES),
            )
            capped = chosen[
                : positive_int(
                    "limit", limit, default=MAX_ADD_LEADS, maximum=MAX_ADD_LEADS
                )
            ]
            summary = enrol_leads(conn, campaign.id, capped)

            return {
                "status": "success",
                "campaign_id": campaign.id,
                "considered": len(capped),
                "enrolled": list(summary.enrolled),
                "excluded": list(summary.excluded),
                "already_enrolled": list(summary.already_enrolled),
                "funnel": campaign_funnel(conn, campaign.id),
                "next_run_at": _next_run_at(conn, campaign.id),
                "message": (
                    f"Enrolled {len(summary.enrolled)} lead(s) into campaign "
                    f"{campaign.id}; {len(summary.excluded)} excluded at the door "
                    f"and {len(summary.already_enrolled)} already there."
                ),
            }
        except Exception as error:
            return _failure(error, campaign_id=campaign_id)
