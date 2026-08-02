"""The seam a passive worker uses: park, defer, and route an ICP verdict.

What SEQ-04 (#22) calls, in order
---------------------------------
Nothing in this module blocks, sleeps, retries or reaches the network. A tick
that finds a step needing generated text does this and moves to the next lead::

    gate = ensure_draft(conn, campaign_id, lead_id, "connection_note", step=step)
    if not gate.ready:
        defer_for_approval(conn, campaign_id, lead_id, now=now)
        continue                      # nothing is blocked, nothing is waiting

    text = approved_text(conn, gate.draft.id)
    ...send it, then mark_sent(conn, gate.draft.id)

:func:`defer_for_approval` is a `refuse_step` carrying
`RefusalReason.APPROVAL_REQUIRED`, which is exactly what SEQ-01's own docstring
says a step waiting on approval should be. It re-queues the lead rather than
failing it, and a refusal does not spend the step's attempt budget, so a draft
that sits unapproved for a week costs the lead nothing.

The ICP gate
------------
An ICP gate is an ordinary `filter` step, which means `Step.is_local` is True and
a worker must not spend a safety-gate lease on it. That is not a convention, it
is requirement 4 of this issue made structural: `filter` is already in
:data:`~linkedin_mcp.sequences.steps.LOCAL_ACTIONS`, so an ICP evaluation cannot
consume LinkedIn budget even by accident. :func:`icp_gate_step` builds the spec.

:func:`route_icp_verdict` then moves the lead. A match completes the step, so the
lead advances to whatever comes next and lands in `successful` when the gate was
the last step. A no-match calls `fail_step`, which lands the lead in `failed`
because :func:`icp_gate_step` sets `on_failure='fail'`. That coupling is
deliberate and worth stating: a no-match is a verdict, not a transient error, so
retrying it would just re-ask a settled question. A gate step defined by hand
with `on_failure='retry'` will re-queue instead, which is the step policy doing
its documented job rather than this module being unreliable.

A malformed verdict routes nothing at all. See :mod:`linkedin_mcp.drafts.verdict`.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from linkedin_mcp.audit import RefusalReason
from linkedin_mcp.drafts.context import ICP_CONFIG_KEY, draft_context
from linkedin_mcp.drafts.errors import DraftStateError
from linkedin_mcp.drafts.store import (
    STATUS_APPROVED,
    STATUS_SENT,
    Draft,
    list_drafts,
    mark_sent,
    open_draft_for,
    park_draft,
    require_draft,
    validate_kind,
)
from linkedin_mcp.drafts.verdict import Verdict, parse_verdict
from linkedin_mcp.sequences.campaigns import require_campaign
from linkedin_mcp.sequences.enrollment import CampaignLead
from linkedin_mcp.sequences.filters import FilterContext, register_filter
from linkedin_mcp.sequences.steps import (
    FILTER_ACTION,
    ON_FAILURE_FAIL,
    Step,
    StepSpec,
)
from linkedin_mcp.sequences.transaction import transaction
from linkedin_mcp.sequences.transitions import (
    complete_step,
    current_step,
    fail_step,
    refuse_step,
)


__all__ = [
    "ICP_ACTION",
    "ICP_FILTER_NAME",
    "ICP_MATCH_OUTCOME",
    "ICP_NO_MATCH_OUTCOME",
    "DraftGate",
    "IcpRouting",
    "defer_for_approval",
    "ensure_draft",
    "ensure_fragment_drafts",
    "icp_gate_step",
    "is_icp_gate",
    "latest_verdict",
    "register_icp_filter",
    "request_draft",
    "route_icp_verdict",
]

ICP_FILTER_NAME = "icp_match"
"""The name a campaign step config uses to point at the ICP predicate."""

ICP_ACTION = FILTER_ACTION
"""An ICP gate is a filter step, and filter steps reach nothing on LinkedIn."""

ICP_MATCH_OUTCOME = "icp_match"
ICP_NO_MATCH_OUTCOME = "icp_no_match"


@dataclass(frozen=True, slots=True)
class DraftGate:
    """The answer to "can this step run yet?"."""

    draft: Draft
    parked: bool
    """True when this call created the row rather than finding it."""

    @property
    def ready(self) -> bool:
        """True when a human, or an opted-in campaign, has released the draft."""
        return self.draft.status == STATUS_APPROVED

    @property
    def status(self) -> str:
        return self.draft.status


@dataclass(frozen=True, slots=True)
class IcpRouting:
    """Where an ICP verdict sent the lead."""

    draft: Draft
    verdict: Verdict
    record: CampaignLead

    @property
    def sublist(self) -> str:
        return self.record.sublist

    @property
    def matched(self) -> bool:
        return self.verdict.match

    def to_result(self) -> dict[str, Any]:
        return {
            "status": "success",
            "draft_id": self.draft.id,
            "campaign_id": self.record.campaign_id,
            "lead_id": self.record.lead_id,
            "match": self.verdict.match,
            "score": self.verdict.score,
            "reason": self.verdict.reason,
            "sublist": self.record.sublist,
        }


def request_draft(
    conn: sqlite3.Connection,
    campaign_id: int,
    lead_id: int,
    kind: str,
    *,
    step: Step | None = None,
    fragment: str | None = None,
    extras: Mapping[str, Any] | None = None,
    model: str | None = None,
    now: datetime | str | None = None,
) -> Draft:
    """Park one draft for a lead on a campaign step, and return at once.

    Resolves the account from the campaign and the step from the lead's current
    position unless one is passed, builds the context, writes the row and
    returns. No model is called and nothing is awaited.
    """
    validate_kind(kind)
    campaign = require_campaign(conn, campaign_id)
    resolved_step = step if step is not None else current_step(conn, campaign_id, lead_id)

    payload = draft_context(
        conn,
        kind,
        campaign_id=campaign_id,
        lead_id=lead_id,
        step=resolved_step,
        extras=extras,
    )
    if fragment:
        payload["fragment"] = fragment

    return park_draft(
        conn,
        account_id=campaign.account_id,
        kind=kind,
        campaign_id=campaign_id,
        lead_id=lead_id,
        step_id=None if resolved_step is None else resolved_step.id,
        context=payload,
        model=model,
        now=now,
    )


def ensure_draft(
    conn: sqlite3.Connection,
    campaign_id: int,
    lead_id: int,
    kind: str,
    *,
    step: Step | None = None,
    fragment: str | None = None,
    extras: Mapping[str, Any] | None = None,
    model: str | None = None,
    now: datetime | str | None = None,
) -> DraftGate:
    """Return the draft for this step, parking one if none is in flight.

    Idempotent by design. A step that is retried, refused and retried again for a
    week must not leave seven rows in the review queue, so an open draft for the
    same lead, step and kind is reused. `sent` and `rejected` rows are history and
    do not block a fresh one, which is how a rejected draft gets regenerated.

    The lookup and the insert are one transaction, so two workers ticking the
    same lead at once cannot both decide there is nothing parked yet.
    """
    with transaction(conn):
        resolved_step = (
            step if step is not None else current_step(conn, campaign_id, lead_id)
        )
        step_id = None if resolved_step is None else resolved_step.id

        existing = open_draft_for(
            conn, campaign_id, lead_id, kind, step_id=step_id, fragment=fragment
        )
        if existing is not None:
            return DraftGate(draft=existing, parked=False)

        draft = request_draft(
            conn,
            campaign_id,
            lead_id,
            kind,
            step=resolved_step,
            fragment=fragment,
            extras=extras,
            model=model,
            now=now,
        )
        return DraftGate(draft=draft, parked=True)


def ensure_fragment_drafts(
    conn: sqlite3.Connection,
    campaign_id: int,
    lead_id: int,
    *,
    step: Step | None = None,
    kind: str = "message",
    extras: Mapping[str, Any] | None = None,
    model: str | None = None,
    now: datetime | str | None = None,
) -> list[DraftGate]:
    """Park one draft per `{ai_*}` fragment the step's template is missing.

    A hybrid template refuses to render while any fragment is unfilled, so the
    fragment list is the precise definition of the work. Returns one gate per
    fragment; the caller sends only when every gate is `ready`.
    """
    resolved_step = step if step is not None else current_step(conn, campaign_id, lead_id)
    context = draft_context(
        conn,
        kind,
        campaign_id=campaign_id,
        lead_id=lead_id,
        step=resolved_step,
    )
    fragments = list((context.get("template") or {}).get("fragments") or [])
    return [
        ensure_draft(
            conn,
            campaign_id,
            lead_id,
            kind,
            step=resolved_step,
            fragment=name,
            extras=extras,
            model=model,
            now=now,
        )
        for name in fragments
    ]


def defer_for_approval(
    conn: sqlite3.Connection,
    campaign_id: int,
    lead_id: int,
    *,
    now: datetime | str | None = None,
    retry_after: int | None = None,
) -> CampaignLead:
    """Put the lead back in the queue because its draft is not released yet.

    A `refuse_step` with `RefusalReason.APPROVAL_REQUIRED`. It re-queues rather
    than failing, and it does not spend the step's attempt budget, so waiting on
    a human is free. This is the call that makes "the worker never blocks on an
    LLM" true: the worker does not wait, it declines and comes back.
    """
    return refuse_step(
        conn,
        campaign_id,
        lead_id,
        reason=RefusalReason.APPROVAL_REQUIRED,
        now=now,
        retry_after=retry_after,
    )


def icp_gate_step(
    icp: Any,
    *,
    delay_seconds: int = 0,
    on_failure: str = ON_FAILURE_FAIL,
    priority: int = 0,
    config: Mapping[str, Any] | None = None,
) -> StepSpec:
    """Build the step spec for an ICP qualification gate.

    Two properties are deliberate. The action type is `filter`, so the step is in
    `LOCAL_ACTIONS` and a worker will not spend LinkedIn budget qualifying
    somebody. And `on_failure` defaults to `fail`, so a no-match verdict lands the
    lead in `failed` on the first answer instead of re-asking a settled question.

    Put this before the invite step. That ordering is the point: filtering costs
    zero LinkedIn actions and invitations are the scarcest thing the account has.
    """
    merged: dict[str, Any] = {"filter": ICP_FILTER_NAME, ICP_CONFIG_KEY: icp}
    if delay_seconds:
        merged["delay_seconds"] = int(delay_seconds)
    if priority:
        merged["priority"] = int(priority)
    merged.update(dict(config or {}))
    return StepSpec(action_type=ICP_ACTION, config=merged, on_failure=on_failure)


def route_icp_verdict(
    conn: sqlite3.Connection,
    draft_id: int,
    *,
    now: datetime | str | None = None,
    worker_id: str | None = None,
) -> IcpRouting:
    """Move the lead according to an approved ICP verdict.

    A match completes the step, so the lead advances and lands in `successful`
    when the gate was the last step. A no-match fails the step, which lands the
    lead in `failed` for a gate built by :func:`icp_gate_step`.

    Three refusals, all loud:

    * The draft must be an `icp_evaluation`.
    * It must be `approved`. A verdict routes a real person out of a campaign, so
      it goes through the same release as generated text. Campaigns that want
      this unattended set `approval_mode='auto'`.
    * The verdict must parse. A malformed or partial verdict raises
      :class:`~linkedin_mcp.drafts.errors.MalformedVerdictError` and the lead is
      not moved at all, in either direction. Treating a broken verdict as a match
      would invite somebody nobody qualified; treating it as a no-match would
      quietly bin good leads whenever a client regressed.
    """
    draft = require_draft(conn, draft_id)
    if not draft.is_verdict_kind:
        raise DraftStateError(
            draft_id, draft.status, f"routed; it is a {draft.kind} draft"
        )
    if draft.status != STATUS_APPROVED:
        raise DraftStateError(
            draft_id, draft.status, "routed; an ICP verdict must be approved first"
        )
    if draft.campaign_id is None or draft.lead_id is None:
        raise DraftStateError(
            draft_id, draft.status, "routed; it names no campaign and lead to move"
        )

    verdict = parse_verdict(draft.verdict, draft_id=draft_id)

    # One transaction covering the sub-list move and the draft's own status.
    # Splitting them would let a crash in between leave the lead routed and the
    # verdict still reusable, which is a second route waiting to happen.
    with transaction(conn):
        standing = current_step(conn, draft.campaign_id, draft.lead_id)
        _require_matching_gate(draft, standing)

        if verdict.match:
            record = complete_step(
                conn,
                draft.campaign_id,
                draft.lead_id,
                now=now,
                outcome=f"{ICP_MATCH_OUTCOME}: score={verdict.score}",
                worker_id=worker_id,
            )
        else:
            record = fail_step(
                conn,
                draft.campaign_id,
                draft.lead_id,
                error=f"{ICP_NO_MATCH_OUTCOME}: {verdict.reason}",
                now=now,
                worker_id=worker_id,
            )

        mark_sent(conn, draft_id, now=now)

    return IcpRouting(draft=require_draft(conn, draft_id), verdict=verdict, record=record)


def is_icp_gate(step: Step | None) -> bool:
    """True when this step is an ICP qualification gate.

    A verdict may only resolve a filter step. That is the property that matters:
    a filter reaches nothing on LinkedIn, so resolving one can never be the same
    mistake as resolving an invite or a message with somebody's ICP score.
    """
    if step is None or not step.is_filter:
        return False
    return step.filter_name == ICP_FILTER_NAME or ICP_CONFIG_KEY in step.config


def _require_matching_gate(draft: Draft, standing: Step | None) -> None:
    """Refuse to resolve any step other than the gate this verdict evaluated."""
    if draft.step_id is None:
        raise DraftStateError(
            draft.id,
            draft.status,
            "routed; it was parked without a step, so there is nothing it can resolve",
        )
    if standing is None or standing.id != draft.step_id:
        # The lead has moved on since this verdict was parked. Completing or
        # failing whatever it is standing on now would resolve a step this
        # verdict never evaluated.
        raise DraftStateError(
            draft.id,
            draft.status,
            f"routed; it was parked for step {draft.step_id} and the lead is "
            f"now on {None if standing is None else standing.id}",
        )
    if not is_icp_gate(standing):
        raise DraftStateError(
            draft.id,
            draft.status,
            f"routed; step {standing.id} is a {standing.action_type!r} step, "
            "not an ICP gate",
        )


def latest_verdict(
    conn: sqlite3.Connection,
    campaign_id: int,
    lead_id: int,
    *,
    step_id: int | None = None,
) -> Verdict | None:
    """Return the most recent released ICP verdict for a lead, or None.

    Only `approved` and `sent` rows count. A verdict still sitting in
    `pending_approval` has not been released and must not decide anything.

    The newest released row is the answer, full stop. If it is missing its
    verdict or the verdict is malformed this raises instead of quietly falling
    back to an older one, because "the client regressed" and "the client said no"
    are not the same fact and a campaign with two gates must not answer the
    second with the first one's verdict. `step_id` narrows to a single gate for
    exactly that reason.
    """
    candidates = list_drafts(
        conn,
        status=(STATUS_APPROVED, STATUS_SENT),
        kind="icp_evaluation",
        campaign_id=campaign_id,
        lead_id=lead_id,
        limit=None,
    )
    for draft in reversed(candidates):
        if step_id is not None and draft.step_id != step_id:
            continue
        return parse_verdict(draft.verdict, draft_id=draft.id)
    return None


def _icp_predicate(context: FilterContext) -> bool:
    """The `icp_match` filter: read the released verdict, do not generate one.

    This is the alternative routing shape. A campaign that would rather send a
    no-match to `skipped` or `excluded` than to `failed` uses a plain filter step
    and SEQ-01's `apply_filter_step`, and this predicate answers from the verdict
    the client already submitted. It never calls a model, which is what keeps a
    filter step honest about being local and free.

    The verdict is looked up for *this* step, so a campaign with two ICP gates
    cannot answer the second one with the first one's verdict. With no released
    verdict it raises, because returning False would silently drop every lead
    whose draft was merely still waiting for a human.
    """
    verdict = latest_verdict(
        context.conn,
        context.campaign_id,
        context.lead_id,
        step_id=context.step.id,
    )
    if verdict is None:
        raise LookupError(
            f"lead {context.lead_id} has no released ICP verdict for step "
            f"{context.step.id} of campaign {context.campaign_id}; park an "
            "icp_evaluation draft first"
        )
    threshold = context.config.get("min_score")
    if threshold is not None and verdict.score < float(threshold):
        return False
    return verdict.match


def register_icp_filter(*, name: str = ICP_FILTER_NAME, replace: bool = True) -> None:
    """Register the ICP predicate with SEQ-01's filter registry.

    Registration is explicit rather than done on import, so importing this
    package never mutates another package's global state behind a caller's back.
    """
    register_filter(name, _icp_predicate, replace=replace)
