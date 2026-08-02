"""Typed failures for the SEQ-05 drafts package.

The split matters. `DraftStyleError` and `MalformedVerdictError` are *client*
mistakes: the MCP client generated something that cannot be used, and it should
regenerate. `DraftNotApprovedError` is a *safety* verdict: the text exists and is
fine, but no human has released it, so nothing may send it. Never collapse the
two, because the second one is the human-in-the-loop rule of this whole project
and it must be impossible to mistake for a transient error worth retrying past.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


__all__ = [
    "DraftError",
    "DraftNotApprovedError",
    "DraftNotFoundError",
    "DraftStateError",
    "DraftStyleError",
    "MalformedVerdictError",
    "UnknownDraftKindError",
]


class DraftError(Exception):
    """Base class for everything `linkedin_mcp.drafts` raises."""


class DraftNotFoundError(DraftError):
    """Raised when a draft id does not resolve."""

    def __init__(self, draft_id: int) -> None:
        self.draft_id = draft_id
        super().__init__(f"ai draft {draft_id!r} does not exist")


class UnknownDraftKindError(DraftError):
    """Raised for a kind outside the `ai_drafts` CHECK constraint."""

    def __init__(self, kind: Any, allowed: Sequence[str]) -> None:
        self.kind = kind
        self.allowed = tuple(allowed)
        super().__init__(
            f"unknown draft kind {kind!r}; expected one of {list(self.allowed)}"
        )


class DraftStateError(DraftError):
    """Raised when a lifecycle move is not legal from the draft's status.

    Submitting text onto an already approved draft, or approving a draft nobody
    has generated yet, are both this. The draft is left exactly as it was.
    """

    def __init__(self, draft_id: int, status: str, attempted: str) -> None:
        self.draft_id = draft_id
        self.status = status
        self.attempted = attempted
        super().__init__(f"ai draft {draft_id} is {status!r}; it cannot be {attempted}")


class DraftStyleError(DraftError):
    """Submitted text breaks Nived's writing style rules.

    The rules live in `.github/copilot-instructions.md` and are enforced by
    SEQ-02's :func:`linkedin_mcp.templating.style_violations`, which this package
    calls rather than reimplementing. An em dash in a submitted draft lands here,
    the row keeps its previous status, and the client regenerates.
    """

    def __init__(self, draft_id: int | None, violations: Sequence[Any]) -> None:
        self.draft_id = draft_id
        self.violations = tuple(violations)
        first = self.violations[0] if self.violations else "text is unusable"
        super().__init__(f"generated text breaks a writing style rule: {first}")


class MalformedVerdictError(DraftError):
    """An `icp_evaluation` verdict is missing, unparseable or the wrong shape.

    Raised instead of guessing. A broken verdict is never read as a match: the
    lead is not routed at all and the caller decides, which keeps a truncated
    model response from inviting somebody who was never qualified.
    """

    def __init__(self, detail: str, *, draft_id: int | None = None) -> None:
        self.draft_id = draft_id
        self.detail = detail
        prefix = "" if draft_id is None else f"ai draft {draft_id}: "
        super().__init__(f"{prefix}malformed ICP verdict: {detail}")


class DraftNotApprovedError(DraftError):
    """Something tried to send AI-generated text that no human released.

    This is the safety rule of the project expressed as an exception. It is
    raised by :func:`linkedin_mcp.drafts.store.approved_text`, which is the only
    function in this package that hands generated text to a caller able to send
    it. Everything else returns the row, not the payload.
    """

    def __init__(self, draft_id: int, status: str) -> None:
        self.draft_id = draft_id
        self.status = status
        super().__init__(
            f"ai draft {draft_id} is {status!r}, not 'approved'; generated text "
            "may not be sent until a human approves it"
        )
