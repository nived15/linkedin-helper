"""The structured result one extraction run hands back.

Nothing in this package prints. A run returns this summary and the caller
decides what to do with it, which is what lets an MCP tool render it as a
result payload and a background runner store it without either of them parsing
log lines.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from linkedin_mcp.leads import HarvestSummary
from linkedin_mcp.scrape.paginate import SearchCursor, StopReason
from linkedin_mcp.scrape.records import PersonResult, PostResult

__all__ = ["ScrapeSummary", "merge_harvest"]


def merge_harvest(left: HarvestSummary, right: HarvestSummary) -> HarvestSummary:
    """Add one page's harvest counts onto the running total."""
    return HarvestSummary(
        found=left.found + right.found,
        created=left.created + right.created,
        updated=left.updated + right.updated,
        unchanged=left.unchanged + right.unchanged,
        lead_ids=left.lead_ids + right.lead_ids,
        refusals=left.refusals + right.refusals,
    )


@dataclass(frozen=True, slots=True)
class ScrapeSummary:
    """What one extraction run found, stored and stopped on."""

    source: str
    action_type: str
    stop_reason: StopReason
    cursor: SearchCursor
    pages_fetched: int = 0
    results_seen: int = 0
    results_new: int = 0
    duplicates_skipped: int = 0
    people: tuple[PersonResult, ...] = ()
    posts: tuple[PostResult, ...] = ()
    harvest: HarvestSummary = field(default_factory=HarvestSummary)
    gate_refusal: Mapping[str, Any] | None = None
    harvest_run_id: int | None = None
    stale_lead_ids: tuple[int, ...] = ()

    @property
    def leads_created(self) -> int:
        return self.harvest.created

    @property
    def leads_updated(self) -> int:
        return self.harvest.updated

    @property
    def leads_unchanged(self) -> int:
        return self.harvest.unchanged

    @property
    def lead_ids(self) -> tuple[int, ...]:
        return self.harvest.lead_ids

    @property
    def hit_platform_ceiling(self) -> bool:
        """True when LinkedIn stopped serving results, not the caller."""
        return self.stop_reason is StopReason.PLATFORM_CEILING

    @property
    def refused(self) -> bool:
        """True when the safety gate ended the run."""
        return self.stop_reason is StopReason.GATE_REFUSED

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON friendly payload, suitable for an MCP tool result."""
        return {
            "status": "refused" if self.refused else "success",
            "source": self.source,
            "action_type": self.action_type,
            "stop_reason": self.stop_reason.value,
            "pages_fetched": self.pages_fetched,
            "results_seen": self.results_seen,
            "results_new": self.results_new,
            "duplicates_skipped": self.duplicates_skipped,
            "leads_created": self.leads_created,
            "leads_updated": self.leads_updated,
            "leads_unchanged": self.leads_unchanged,
            "lead_ids": list(self.lead_ids),
            "harvest_refusals": [
                {
                    "reason": refusal.reason,
                    "message": refusal.message,
                    "member_id": refusal.member_id,
                    "public_id": refusal.public_id,
                }
                for refusal in self.harvest.refusals
            ],
            "gate_refusal": dict(self.gate_refusal) if self.gate_refusal else None,
            "cursor": self.cursor.as_dict(),
            "harvest_run_id": self.harvest_run_id,
            "stale_lead_ids": list(self.stale_lead_ids),
            "posts": [post.as_dict() for post in self.posts],
        }
