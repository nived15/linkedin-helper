"""Exceptions raised by the lead storage engine."""

from __future__ import annotations


__all__ = [
    "LeadBlacklistedError",
    "LeadIdentityConflictError",
    "LeadNotFoundError",
    "LeadStoreError",
]


class LeadStoreError(RuntimeError):
    """Base class for lead storage failures."""


class LeadNotFoundError(LeadStoreError):
    """Raised when a lead id does not resolve to a stored lead."""

    def __init__(self, lead_id: int) -> None:
        super().__init__(f"lead {lead_id} does not exist")
        self.lead_id = lead_id


class LeadBlacklistedError(LeadStoreError):
    """Raised when an operation targets a globally blacklisted identity."""

    def __init__(
        self,
        *,
        lead_id: int | None = None,
        member_id: str | None = None,
        public_id: str | None = None,
    ) -> None:
        identity = ", ".join(
            f"{label}={value!r}"
            for label, value in (
                ("lead_id", lead_id),
                ("member_id", member_id),
                ("public_id", public_id),
            )
            if value is not None
        )
        super().__init__(
            "lead is on the global do-not-contact blacklist and is blocked for "
            f"every account and campaign ({identity or 'unknown identity'})"
        )
        self.lead_id = lead_id
        self.member_id = member_id
        self.public_id = public_id


class LeadIdentityConflictError(LeadStoreError):
    """Raised when an incoming identity cannot be resolved to exactly one lead.

    Deduplication refuses rather than guesses here: resolving the collision
    either way would delete an identifier the database already holds, or fold
    two people into one row.
    """

    def __init__(
        self,
        message: str,
        *,
        kind: str,
        value: str | None = None,
        lead_id: int | None = None,
        other_lead_id: int | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.value = value
        self.lead_id = lead_id
        self.other_lead_id = other_lead_id
