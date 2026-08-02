"""The `ai_drafts` table: park a row, generate later, approve before sending.

The inversion this package exists for
-------------------------------------
A campaign worker running unattended at 2am must never wait on a language model.
So it does not call one. It writes a row here with everything a model would need
about the lead, sets the status to `needs_generation`, and moves on to the next
lead. Some time later an MCP client, which *is* a language model, lists the open
rows, generates text, and submits it back. Nothing in this module imports an LLM
client, opens a socket, or blocks; the only thing it does is read and write
SQLite. That is what makes an LLM optional rather than load-bearing.

The five statuses are the whole lifecycle
-----------------------------------------
``needs_generation`` -> ``pending_approval`` -> ``approved`` -> ``sent``, with
``rejected`` as the exit at the approval step. They are exactly the CHECK
constraint in `0001_init.sql`.

The one rule that is not negotiable
-----------------------------------
:func:`approved_text` is the only function here that hands generated text to a
caller that can send it, and it raises unless the row is `approved`. Everything
else returns the `Draft` row, whose `generated_text` is inert data. Combined with
:func:`mark_sent` refusing any status but `approved`, there is no path from
"model wrote something" to "LinkedIn received something" that does not pass
through a human decision, unless the campaign's own `approval_mode` is `auto`.

Auto-approve is the existing per-campaign flag
----------------------------------------------
`campaigns.approval_mode` already carries `auto` and `manual_drafts`, and it is
the opt-in the definition of done asks for. No second flag was added. A draft
with no campaign, or a campaign left at the default `manual_drafts`, always
lands in `pending_approval`. Known trade-off, stated plainly: `approval_mode` is
per campaign and not per draft kind, so a campaign set to `auto` auto-approves
its connection notes as well as its ICP verdicts. Splitting them would need a new
column, which is out of scope here.

Style is SEQ-02's, not ours
---------------------------
Submitted text is validated with :func:`linkedin_mcp.templating.style_violations`
and nothing else. There is no second style checker in this repo and there must
never be one, or the two will drift and the weaker will win.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from linkedin_mcp.drafts.errors import (
    DraftNotApprovedError,
    DraftNotFoundError,
    DraftStateError,
    DraftStyleError,
    UnknownDraftKindError,
)
from linkedin_mcp.drafts.verdict import Verdict, encode_verdict, parse_verdict
from linkedin_mcp.sequences.campaigns import get_campaign
from linkedin_mcp.sequences.transaction import now_timestamp, transaction
from linkedin_mcp.templating.style import DEFAULT_STYLE, StylePolicy, style_violations


__all__ = [
    "AUTO_APPROVAL_MODE",
    "DRAFT_COLUMNS",
    "DRAFT_KINDS",
    "DRAFT_STATUSES",
    "MAX_TEXT_LENGTH",
    "OPEN_STATUSES",
    "STATUS_APPROVED",
    "STATUS_NEEDS_GENERATION",
    "STATUS_PENDING_APPROVAL",
    "STATUS_REJECTED",
    "STATUS_SENT",
    "TEXT_KINDS",
    "VERDICT_KINDS",
    "Draft",
    "approve_draft",
    "approved_text",
    "auto_approves",
    "count_drafts",
    "draft_from_row",
    "get_draft",
    "list_drafts",
    "list_pending",
    "mark_sent",
    "open_draft_for",
    "park_draft",
    "require_draft",
    "submit_draft",
    "validate_kind",
    "validate_text",
]

DRAFT_KINDS: tuple[str, ...] = ("connection_note", "message", "comment", "icp_evaluation")
"""The four kinds, matching the `ai_drafts.kind` CHECK constraint exactly."""

TEXT_KINDS: tuple[str, ...] = ("connection_note", "message", "comment")
"""Kinds whose output is free text a human being will read on LinkedIn."""

VERDICT_KINDS: tuple[str, ...] = ("icp_evaluation",)
"""Kinds whose output is a structured verdict rather than text."""

STATUS_NEEDS_GENERATION = "needs_generation"
STATUS_PENDING_APPROVAL = "pending_approval"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_SENT = "sent"

DRAFT_STATUSES: tuple[str, ...] = (
    STATUS_NEEDS_GENERATION,
    STATUS_PENDING_APPROVAL,
    STATUS_APPROVED,
    STATUS_REJECTED,
    STATUS_SENT,
)
"""The five statuses, matching the `ai_drafts.status` CHECK constraint."""

OPEN_STATUSES: tuple[str, ...] = (
    STATUS_NEEDS_GENERATION,
    STATUS_PENDING_APPROVAL,
    STATUS_APPROVED,
)
"""Statuses that still represent outstanding work for a lead.

Used to keep a retried job from parking a second draft for a step that already
has one in flight.
"""

AUTO_APPROVAL_MODE = "auto"
"""The `campaigns.approval_mode` value that opts a campaign into auto-approve."""

MAX_TEXT_LENGTH: Mapping[str, int] = {
    # LinkedIn's own invitation note limit. Anything longer is silently truncated
    # by the site, which would send half a sentence.
    "connection_note": 300,
    "message": 8000,
    "comment": 1250,
}
"""Longest text each kind may carry, checked at submit time."""

DRAFT_COLUMNS = (
    "id, account_id, campaign_id, lead_id, step_id, kind, context_json, "
    "generated_text, verdict_json, status, model, created_at, decided_at"
)


@dataclass(frozen=True, slots=True)
class Draft:
    """One row of `ai_drafts`, read back."""

    id: int
    account_id: int
    kind: str
    status: str
    campaign_id: int | None = None
    lead_id: int | None = None
    step_id: int | None = None
    context: Mapping[str, Any] = field(default_factory=dict)
    generated_text: str | None = None
    verdict: Mapping[str, Any] | None = None
    model: str | None = None
    created_at: str | None = None
    decided_at: str | None = None

    @property
    def needs_generation(self) -> bool:
        return self.status == STATUS_NEEDS_GENERATION

    @property
    def awaiting_approval(self) -> bool:
        return self.status == STATUS_PENDING_APPROVAL

    @property
    def is_approved(self) -> bool:
        return self.status == STATUS_APPROVED

    @property
    def is_open(self) -> bool:
        """True while this draft still represents outstanding work."""
        return self.status in OPEN_STATUSES

    @property
    def is_verdict_kind(self) -> bool:
        return self.kind in VERDICT_KINDS

    def parsed_verdict(self) -> Verdict:
        """Return the typed verdict, raising when it is missing or malformed."""
        return parse_verdict(self.verdict, draft_id=self.id)

    def to_result(self) -> dict[str, Any]:
        """Return an MCP-shaped view of this row.

        `generated_text` is included because listing and reviewing drafts is the
        point of the tools. Being able to *read* the text is not the same as
        being able to send it: :func:`approved_text` is the only release valve.
        """
        return {
            "id": self.id,
            "account_id": self.account_id,
            "campaign_id": self.campaign_id,
            "lead_id": self.lead_id,
            "step_id": self.step_id,
            "kind": self.kind,
            "status": self.status,
            "context": dict(self.context),
            "generated_text": self.generated_text,
            "verdict": None if self.verdict is None else dict(self.verdict),
            "model": self.model,
            "created_at": self.created_at,
            "decided_at": self.decided_at,
        }


def _decode_object(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def draft_from_row(row: sqlite3.Row) -> Draft:
    verdict_raw = row["verdict_json"]
    return Draft(
        id=row["id"],
        account_id=row["account_id"],
        kind=row["kind"],
        status=row["status"],
        campaign_id=row["campaign_id"],
        lead_id=row["lead_id"],
        step_id=row["step_id"],
        context=_decode_object(row["context_json"]),
        generated_text=row["generated_text"],
        verdict=None if not verdict_raw else _decode_object(verdict_raw),
        model=row["model"],
        created_at=row["created_at"],
        decided_at=row["decided_at"],
    )


def validate_kind(kind: str) -> str:
    """Return the kind, raising when it is outside the CHECK constraint."""
    if kind not in DRAFT_KINDS:
        raise UnknownDraftKindError(kind, DRAFT_KINDS)
    return kind


def _validate_status(status: str) -> str:
    if status not in DRAFT_STATUSES:
        raise ValueError(
            f"unknown draft status {status!r}; expected one of {list(DRAFT_STATUSES)}"
        )
    return status


def validate_text(
    kind: str,
    text: str,
    *,
    draft_id: int | None = None,
    policy: StylePolicy = DEFAULT_STYLE,
) -> str:
    """Return submitted text, or raise because it must not be used.

    Three checks in order: it says something, it fits, and it obeys the writing
    style rules. The style check is SEQ-02's
    :func:`~linkedin_mcp.templating.style.style_violations` with dash checking on,
    so an em dash in generated text is rejected here exactly as it would be if it
    arrived as an `{ai_*}` fragment at render time. Generated text is authored
    text, so it is refused rather than repaired.
    """
    validate_kind(kind)
    cleaned = (text or "").strip()
    if not cleaned:
        raise DraftStyleError(draft_id, ["generated text is empty"])

    limit = MAX_TEXT_LENGTH.get(kind)
    if limit is not None and len(cleaned) > limit:
        raise DraftStyleError(
            draft_id,
            [f"{kind} runs to {len(cleaned)} characters, over the limit of {limit}"],
        )

    violations = style_violations(cleaned, policy)
    if violations:
        raise DraftStyleError(draft_id, violations)
    return cleaned


def auto_approves(conn: sqlite3.Connection, campaign_id: int | None) -> bool:
    """Return True when this campaign opted into auto-approving its drafts.

    A draft with no campaign is never auto-approved: auto-approval is a decision
    somebody made about a specific campaign, and there is nowhere for an ad hoc
    draft to have recorded it.
    """
    if campaign_id is None:
        return False
    campaign = get_campaign(conn, campaign_id)
    return campaign is not None and campaign.approval_mode == AUTO_APPROVAL_MODE


def park_draft(
    conn: sqlite3.Connection,
    *,
    account_id: int,
    kind: str,
    campaign_id: int | None = None,
    lead_id: int | None = None,
    step_id: int | None = None,
    context: Mapping[str, Any] | None = None,
    model: str | None = None,
    now: datetime | str | None = None,
) -> Draft:
    """Write a `needs_generation` row and return immediately.

    This is the worker seam and it is deliberately dull. One INSERT, no network,
    no model, no waiting. SEQ-04 (#22) calls this (usually through
    :func:`linkedin_mcp.drafts.routing.request_draft`, which builds the context
    from campaign state) and carries on with the next lead in the same tick.
    """
    validate_kind(kind)
    created_at = now_timestamp(now)
    payload = json.dumps(dict(context or {}), default=str, sort_keys=True)

    with transaction(conn):
        cursor = conn.execute(
            """
            INSERT INTO ai_drafts
                (account_id, campaign_id, lead_id, step_id, kind, context_json,
                 generated_text, verdict_json, status, model, created_at, decided_at)
            VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, NULL)
            """,
            (
                account_id,
                campaign_id,
                lead_id,
                step_id,
                kind,
                payload,
                STATUS_NEEDS_GENERATION,
                model,
                created_at,
            ),
        )
        draft_id = int(cursor.lastrowid)
    return require_draft(conn, draft_id)


def get_draft(conn: sqlite3.Connection, draft_id: int) -> Draft | None:
    row = conn.execute(
        f"SELECT {DRAFT_COLUMNS} FROM ai_drafts WHERE id = ?",
        (draft_id,),
    ).fetchone()
    return None if row is None else draft_from_row(row)


def require_draft(conn: sqlite3.Connection, draft_id: int) -> Draft:
    draft = get_draft(conn, draft_id)
    if draft is None:
        raise DraftNotFoundError(draft_id)
    return draft


def list_drafts(
    conn: sqlite3.Connection,
    account_id: int | None = None,
    *,
    status: str | Sequence[str] | None = None,
    kind: str | None = None,
    campaign_id: int | None = None,
    lead_id: int | None = None,
    limit: int | None = 50,
) -> list[Draft]:
    """Read drafts, oldest first so the queue is fair."""
    sql = f"SELECT {DRAFT_COLUMNS} FROM ai_drafts WHERE 1 = 1"
    params: list[Any] = []

    if account_id is not None:
        sql += " AND account_id = ?"
        params.append(account_id)
    if status is not None:
        wanted = [status] if isinstance(status, str) else list(status)
        for value in wanted:
            _validate_status(value)
        sql += f" AND status IN ({', '.join('?' for _ in wanted)})"
        params.extend(wanted)
    if kind is not None:
        sql += " AND kind = ?"
        params.append(validate_kind(kind))
    if campaign_id is not None:
        sql += " AND campaign_id = ?"
        params.append(campaign_id)
    if lead_id is not None:
        sql += " AND lead_id = ?"
        params.append(lead_id)

    sql += " ORDER BY id"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(max(1, int(limit)))
    return [draft_from_row(row) for row in conn.execute(sql, params).fetchall()]


def list_pending(
    conn: sqlite3.Connection,
    account_id: int | None = None,
    *,
    status: str = STATUS_NEEDS_GENERATION,
    kind: str | None = None,
    campaign_id: int | None = None,
    limit: int | None = 25,
) -> list[Draft]:
    """Return the queue an MCP client should work through.

    The default is `needs_generation`, which is the generation queue. Pass
    `status='pending_approval'` for the human review queue instead. Those are two
    different jobs done by two different actors and they are deliberately the
    same tool with a different argument.
    """
    return list_drafts(
        conn,
        account_id,
        status=_validate_status(status),
        kind=kind,
        campaign_id=campaign_id,
        limit=limit,
    )


def count_drafts(
    conn: sqlite3.Connection,
    account_id: int | None = None,
    *,
    status: str | None = None,
) -> int:
    sql = "SELECT COUNT(*) AS total FROM ai_drafts WHERE 1 = 1"
    params: list[Any] = []
    if account_id is not None:
        sql += " AND account_id = ?"
        params.append(account_id)
    if status is not None:
        sql += " AND status = ?"
        params.append(_validate_status(status))
    return int(conn.execute(sql, params).fetchone()["total"])


def open_draft_for(
    conn: sqlite3.Connection,
    campaign_id: int,
    lead_id: int,
    kind: str,
    *,
    step_id: int | None = None,
    fragment: str | None = None,
) -> Draft | None:
    """Return the draft already in flight for this lead and step, if any.

    A retried job must not spawn a second draft for the same work. `sent` and
    `rejected` rows are history and do not block a fresh one, which is how a
    rejected draft gets regenerated.

    `fragment` narrows to one `{ai_*}` slot. A hybrid template with two fragments
    has two open drafts at once, so matching on kind alone would find the wrong
    one and park a duplicate of the other.
    """
    validate_kind(kind)
    sql = (
        f"SELECT {DRAFT_COLUMNS} FROM ai_drafts "
        "WHERE campaign_id = ? AND lead_id = ? AND kind = ? "
        f"AND status IN ({', '.join('?' for _ in OPEN_STATUSES)})"
    )
    params: list[Any] = [campaign_id, lead_id, kind, *OPEN_STATUSES]
    if step_id is not None:
        sql += " AND step_id = ?"
        params.append(step_id)
    sql += " ORDER BY id DESC"

    for row in conn.execute(sql, params).fetchall():
        draft = draft_from_row(row)
        if fragment is None or draft.context.get("fragment") == fragment:
            return draft
    return None


_SUBMITTABLE: frozenset[str] = frozenset(
    {STATUS_NEEDS_GENERATION, STATUS_PENDING_APPROVAL, STATUS_REJECTED}
)
"""Statuses a client may submit generated output onto.

`approved` and `sent` are absent on purpose. Letting a client overwrite text a
human has already signed off would mean the approved thing and the sent thing
are different things, which is the exact hole this package exists to close.
"""


def submit_draft(
    conn: sqlite3.Connection,
    draft_id: int,
    *,
    text: str | None = None,
    verdict: Mapping[str, Any] | str | None = None,
    model: str | None = None,
    now: datetime | str | None = None,
    policy: StylePolicy = DEFAULT_STYLE,
) -> Draft:
    """Store generated output against a parked draft.

    Text kinds need `text`; `icp_evaluation` needs `verdict`. Both are validated
    before anything is written, so a rejected submission leaves the row exactly
    as it was and the client can try again.

    The row lands in `pending_approval`, or in `approved` when the owning
    campaign's `approval_mode` is `auto`.
    """
    draft = require_draft(conn, draft_id)
    if draft.status not in _SUBMITTABLE:
        raise DraftStateError(draft_id, draft.status, "submitted to")

    generated_text: str | None = None
    verdict_json: str | None = None

    if draft.is_verdict_kind:
        parsed = parse_verdict(verdict, draft_id=draft_id)
        verdict_json = encode_verdict(parsed)
        # A verdict may carry a human-readable summary too. It is never sent to
        # LinkedIn, but it is held to the same style rules so an operator reading
        # the review queue sees Nived's voice everywhere.
        if text is not None and text.strip():
            generated_text = validate_text(
                "message", text, draft_id=draft_id, policy=policy
            )
    else:
        if verdict is not None:
            raise DraftStateError(draft_id, draft.status, "given a verdict; it is a text draft")
        if text is None:
            raise DraftStyleError(draft_id, ["no generated text was submitted"])
        generated_text = validate_text(draft.kind, text, draft_id=draft_id, policy=policy)

    status = (
        STATUS_APPROVED
        if auto_approves(conn, draft.campaign_id)
        else STATUS_PENDING_APPROVAL
    )
    moment = now_timestamp(now)

    with transaction(conn):
        conn.execute(
            """
            UPDATE ai_drafts
            SET generated_text = ?, verdict_json = ?, status = ?, model = ?,
                decided_at = ?
            WHERE id = ?
            """,
            (
                generated_text,
                verdict_json,
                status,
                model if model is not None else draft.model,
                moment if status == STATUS_APPROVED else None,
                draft_id,
            ),
        )
    return require_draft(conn, draft_id)


def approve_draft(
    conn: sqlite3.Connection,
    draft_id: int,
    *,
    approved: bool = True,
    note: str | None = None,
    now: datetime | str | None = None,
) -> Draft:
    """Release a draft for use, or reject it.

    Approving is legal from `pending_approval`, and re-approving an `approved`
    row is a no-op so a retried tool call is safe. Rejecting is legal from either,
    because a human revoking an approval before anything was sent is exactly the
    behaviour this rule exists to allow. Nothing can be approved out of
    `needs_generation`, since there is no text to approve, and nothing can be
    approved out of `sent`, since it has already gone.
    """
    draft = require_draft(conn, draft_id)
    action = "approved" if approved else "rejected"

    if draft.status == STATUS_NEEDS_GENERATION:
        raise DraftStateError(draft_id, draft.status, f"{action}; nothing has been generated")
    if draft.status == STATUS_SENT:
        raise DraftStateError(draft_id, draft.status, f"{action}; it has already been sent")
    if draft.status == STATUS_REJECTED and approved:
        raise DraftStateError(
            draft_id, draft.status, "approved; regenerate it and submit again"
        )
    if draft.status == STATUS_APPROVED and approved:
        return draft
    if draft.status == STATUS_REJECTED and not approved:
        return draft

    moment = now_timestamp(now)
    context = dict(draft.context)
    if note:
        context["approval_note"] = str(note)[:500]

    with transaction(conn):
        conn.execute(
            "UPDATE ai_drafts SET status = ?, decided_at = ?, context_json = ? WHERE id = ?",
            (
                STATUS_APPROVED if approved else STATUS_REJECTED,
                moment,
                json.dumps(context, default=str, sort_keys=True),
                draft_id,
            ),
        )
    return require_draft(conn, draft_id)


def approved_text(conn: sqlite3.Connection, draft_id: int) -> str:
    """Return generated text that is cleared to send, or raise.

    The only release valve in this package. Every send path must come through
    here rather than reading `generated_text` off a `Draft`, because this is the
    single line that enforces "AI-generated free text is never sent without
    approval".
    """
    draft = require_draft(conn, draft_id)
    if draft.status != STATUS_APPROVED:
        raise DraftNotApprovedError(draft_id, draft.status)
    if not draft.generated_text:
        raise DraftNotApprovedError(draft_id, draft.status)
    return draft.generated_text


def mark_sent(
    conn: sqlite3.Connection,
    draft_id: int,
    *,
    now: datetime | str | None = None,
) -> Draft:
    """Record that an approved draft was used. Legal only from `approved`.

    The second half of the safety rule. Even a caller that reached around
    :func:`approved_text` cannot close the loop without an approved row.
    """
    draft = require_draft(conn, draft_id)
    if draft.status != STATUS_APPROVED:
        raise DraftNotApprovedError(draft_id, draft.status)

    with transaction(conn):
        conn.execute(
            "UPDATE ai_drafts SET status = ?, decided_at = ? WHERE id = ?",
            (STATUS_SENT, now_timestamp(now), draft_id),
        )
    return require_draft(conn, draft_id)
