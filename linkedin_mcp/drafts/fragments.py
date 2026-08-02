"""The SEQ-02 seam: approved drafts become `{ai_*}` template fragments.

SEQ-02 renders a hybrid template by asking an injected `fragments` source for
each `{ai_*}` token, and refuses the whole render with `MISSING_AI_FRAGMENT`
when the source returns nothing. That refusal is the safety property this module
turns into a guarantee.

:func:`fragment_source` only ever yields text from drafts whose status is
`approved`. A draft sitting in `pending_approval` therefore looks *identical* to
a draft that was never generated, and the render refuses. There is no code path
in which unapproved generated text reaches a rendered message, because the
render never sees it in the first place.

The refusal is also the right kind of refusal:
`RenderRefusal.is_awaiting_ai` is True for `MISSING_AI_FRAGMENT`, so a caller
knows the lead is fine and the draft simply has not landed yet, and re-queues
instead of discarding the lead.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping

from linkedin_mcp.drafts.store import STATUS_APPROVED, Draft, list_drafts
from linkedin_mcp.templating.render import FragmentSource
from linkedin_mcp.templating.tokens import fragment_name


__all__ = [
    "approved_fragments",
    "fragment_source",
    "pending_fragments",
]

_TEXT_KINDS: tuple[str, ...] = ("connection_note", "message", "comment")


def _fragment_key(draft: Draft) -> str | None:
    raw = draft.context.get("fragment")
    if not raw:
        return None
    return fragment_name(str(raw))


def approved_fragments(
    conn: sqlite3.Connection,
    campaign_id: int,
    lead_id: int,
    *,
    step_id: int | None = None,
) -> dict[str, str]:
    """Return every released fragment for this lead, keyed by bare name.

    Later drafts win, so regenerating and re-approving a fragment replaces the
    earlier one rather than racing it.
    """
    found: dict[str, str] = {}
    for draft in list_drafts(
        conn,
        status=STATUS_APPROVED,
        campaign_id=campaign_id,
        lead_id=lead_id,
        limit=None,
    ):
        if draft.kind not in _TEXT_KINDS or not draft.generated_text:
            continue
        if step_id is not None and draft.step_id is not None and draft.step_id != step_id:
            continue
        name = _fragment_key(draft)
        if name:
            found[name] = draft.generated_text
    return found


def pending_fragments(
    conn: sqlite3.Connection,
    campaign_id: int,
    lead_id: int,
    *,
    step_id: int | None = None,
) -> dict[str, str]:
    """Return the fragments that exist but are not released, name to status.

    Useful for explaining a `MISSING_AI_FRAGMENT` refusal to a human: "the text
    is written, nobody has approved it" reads very differently from "nothing has
    been generated".
    """
    waiting: dict[str, str] = {}
    for draft in list_drafts(
        conn,
        campaign_id=campaign_id,
        lead_id=lead_id,
        limit=None,
    ):
        if draft.kind not in _TEXT_KINDS or draft.status == STATUS_APPROVED:
            continue
        if step_id is not None and draft.step_id is not None and draft.step_id != step_id:
            continue
        name = _fragment_key(draft)
        if name:
            waiting[name] = draft.status
    return waiting


def fragment_source(
    conn: sqlite3.Connection,
    campaign_id: int,
    lead_id: int,
    *,
    step_id: int | None = None,
    extra: Mapping[str, str] | None = None,
) -> FragmentSource:
    """Return the callable to hand SEQ-02 as `fragments=`.

    Approved drafts only. Anything else resolves to `None`, which is what makes
    the render refuse rather than send unapproved text.
    """
    released = approved_fragments(conn, campaign_id, lead_id, step_id=step_id)
    for key, value in (extra or {}).items():
        released[fragment_name(str(key))] = value

    def lookup(name: str) -> str | None:
        return released.get(fragment_name(name))

    return lookup
