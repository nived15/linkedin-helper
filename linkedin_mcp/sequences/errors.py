"""Typed failures raised by the sequence engine.

Every error here is a programming or configuration fault the caller can act on.
A lead that legitimately leaves the flow is not an error: it moves to a terminal
sub-list and the transition returns normally.
"""

from __future__ import annotations

__all__ = [
    "CampaignInFlightError",
    "CampaignLeadNotFoundError",
    "CampaignNotFoundError",
    "FilterNotRegisteredError",
    "InvalidTransitionError",
    "SequenceError",
    "StepDefinitionError",
    "StepNotFoundError",
]


class SequenceError(Exception):
    """Base class for every sequence engine failure."""


class CampaignNotFoundError(SequenceError):
    def __init__(self, campaign_id: int) -> None:
        super().__init__(f"campaign {campaign_id} does not exist")
        self.campaign_id = campaign_id


class CampaignLeadNotFoundError(SequenceError):
    def __init__(self, campaign_id: int, lead_id: int) -> None:
        super().__init__(f"lead {lead_id} is not enrolled in campaign {campaign_id}")
        self.campaign_id = campaign_id
        self.lead_id = lead_id


class StepNotFoundError(SequenceError):
    def __init__(self, campaign_id: int, ord_: int) -> None:
        super().__init__(f"campaign {campaign_id} has no step at ord {ord_}")
        self.campaign_id = campaign_id
        self.ord = ord_


class StepDefinitionError(SequenceError):
    """A step list that could never be executed as written."""


class CampaignInFlightError(SequenceError):
    """Refusal to rewrite step definitions under leads that are mid-sequence.

    Redefining steps renumbers the ords a lead's `current_step_ord` points at, so
    a lead already partway through would silently resume at a different action.
    """

    def __init__(self, campaign_id: int, in_flight: int) -> None:
        super().__init__(
            f"campaign {campaign_id} has {in_flight} lead(s) still in the flow; "
            "withdraw them or pass replace=True to redefine the steps anyway"
        )
        self.campaign_id = campaign_id
        self.in_flight = in_flight


class InvalidTransitionError(SequenceError):
    """A sub-list move the state machine does not allow."""

    def __init__(self, campaign_id: int, lead_id: int, source: str, target: str) -> None:
        super().__init__(
            f"lead {lead_id} in campaign {campaign_id} cannot move from "
            f"{source!r} to {target!r}"
        )
        self.campaign_id = campaign_id
        self.lead_id = lead_id
        self.source = source
        self.target = target


class FilterNotRegisteredError(SequenceError):
    def __init__(self, name: str, available: tuple[str, ...] = ()) -> None:
        known = ", ".join(available) if available else "none"
        super().__init__(f"no filter named {name!r} is registered; known filters: {known}")
        self.name = name
        self.available = available
