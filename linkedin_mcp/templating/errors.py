"""Typed failures for the SEQ-02 template engine.

Two very different kinds of failure live here and the split is deliberate.

`TemplateSyntaxError` and `TemplateStyleError` are *author* mistakes. They are
raised when a template is written or stored, before any lead is involved, so the
person who typed the template sees them immediately.

`RenderRefusal` is a *data* outcome. The template is fine; this lead is not. It
carries a typed reason and the sub-list the campaign engine should move the lead
into, which is always ``skipped``. Refusing is the point of the whole module: a
message that renders "Hi ," because ``first_name`` was never scraped is worse
than no message at all.
"""

from __future__ import annotations

from enum import Enum
from typing import Any


__all__ = [
    "AWAITING_AI_REASONS",
    "RenderRefusal",
    "RenderRefusalReason",
    "SKIPPED_SUBLIST",
    "TemplateError",
    "TemplateNotFoundError",
    "TemplateStyleError",
    "TemplateSyntaxError",
]

SKIPPED_SUBLIST = "skipped"
"""The `campaign_leads.sublist` value a refused render must land in.

Spelled as a plain string on purpose. SEQ-01 (#19) owns
`linkedin_mcp.sequences` and it does not exist on this branch, so importing its
state machine here would break the build. The value matches the CHECK
constraint on `campaign_leads.sublist` in `0001_init.sql`.
"""


class TemplateError(Exception):
    """Base class for everything `linkedin_mcp.templating` raises."""


class TemplateSyntaxError(TemplateError):
    """The template body cannot be parsed, or names a token that does not exist.

    Raised at authoring time by `validate_template` and by every write in
    `linkedin_mcp.templating.store`, so an unparseable body can never reach the
    `templates` table.
    """


class TemplateStyleError(TemplateError):
    """Authored text breaks the writing style rules.

    The rules live in `.github/copilot-instructions.md`: no em dashes, no filler
    openers, short sentences. They bind the template body, every whole-message
    variation, and every AI fragment supplied by SEQ-05. They do not bind lead
    data, which is normalised instead of refused, because a company whose legal
    name contains an em dash is not the template author's fault.
    """


class TemplateNotFoundError(TemplateError):
    """Raised when a template id or name does not resolve."""

    def __init__(self, ref: int | str) -> None:
        self.ref = ref
        super().__init__(f"template {ref!r} does not exist")


class RenderRefusalReason(str, Enum):
    """Typed reason a render refused rather than produced a message."""

    TEMPLATE_INVALID = "template_invalid"
    MISSING_VARIABLE = "missing_variable"
    UNKNOWN_VARIABLE = "unknown_variable"
    MISSING_AI_FRAGMENT = "missing_ai_fragment"
    FRAGMENT_SOURCE_FAILED = "fragment_source_failed"
    STYLE_VIOLATION = "style_violation"
    BROKEN_PUNCTUATION = "broken_punctuation"
    EMPTY_MESSAGE = "empty_message"
    TOO_LONG = "too_long"
    LEAD_NOT_FOUND = "lead_not_found"


AWAITING_AI_REASONS: frozenset[RenderRefusalReason] = frozenset(
    {
        RenderRefusalReason.MISSING_AI_FRAGMENT,
        RenderRefusalReason.FRAGMENT_SOURCE_FAILED,
    }
)
"""Reasons that mean "the draft has not arrived yet", not "this lead is bad".

SEQ-05 (#23) parks an `ai_drafts` row and the message becomes renderable once a
human approves the generated text. A caller that wants to retry later rather
than skip should branch on `RenderRefusal.is_awaiting_ai`. A fragment source
that raised is in the same bucket: the lead is fine, the supplier was not. Until
#23 lands, `sublist` still says `skipped`, which is the safe reading of the
definition of done.
"""


class RenderRefusal(TemplateError):
    """A render that refused to produce a message, with the reason why.

    Callers get this as a typed exception from `render_template` or as the
    `refusal` half of a `RenderResult` from `safe_render_template`. Either way
    the correct handling is to move the lead to `RenderRefusal.sublist`, which is
    always `skipped`, and to send nothing.
    """

    def __init__(
        self,
        reason: RenderRefusalReason,
        message: str,
        **detail: Any,
    ) -> None:
        self.reason = reason
        self.detail: dict[str, Any] = {
            key: value for key, value in detail.items() if value is not None
        }
        super().__init__(message)

    @property
    def sublist(self) -> str:
        """The `campaign_leads` sub-list this lead belongs in. Always `skipped`."""
        return SKIPPED_SUBLIST

    @property
    def is_awaiting_ai(self) -> bool:
        """True when SEQ-05 has simply not supplied the fragment yet."""
        return self.reason in AWAITING_AI_REASONS

    def to_result(self) -> dict[str, Any]:
        """Return an MCP-shaped result, mirroring `SafetyRefusal.to_result`."""
        payload = dict(self.detail)
        payload.update(
            {
                "status": "refused",
                "reason": self.reason.value,
                "message": str(self),
                "sublist": self.sublist,
                "awaiting_ai": self.is_awaiting_ai,
            }
        )
        return payload
