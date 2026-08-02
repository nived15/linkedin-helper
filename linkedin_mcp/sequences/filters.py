"""Filter predicates: how a linear engine gets conditional behaviour.

A filter step never forks the flow. It asks one question and either lets the lead
walk to the next step or drops it out of the sequence entirely. There is no
second branch to schedule, no join to reconcile and no graph to walk, which is
the whole reason the engine can stay linear.

Where the lead lands on a no-match is the step's `on_no_match` config:

- `skipped` (the default) means the lead did not match *this* campaign's
  conditions. Re-enrolling it in another campaign, or in this one after its data
  changes, is entirely legitimate.
- `excluded` means the lead must not be contacted by this campaign at all. It is
  final and there is no transition out of it.

Predicates are injected rather than imported. A filter that needs the template
engine (SEQ-02, #20) or ICP qualification (SEQ-05, #23) is registered by that
package when it lands, so nothing here imports a module that does not exist yet.
The built-ins below only read the local database and are therefore offline and
deterministic.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from linkedin_mcp.leads import get_lead, lead_tag_names, normalise_tag_name
from linkedin_mcp.sequences.errors import FilterNotRegisteredError, StepDefinitionError
from linkedin_mcp.sequences.steps import Step

__all__ = [
    "BUILT_IN_FILTERS",
    "FilterContext",
    "FilterPredicate",
    "evaluate_filter",
    "get_filter",
    "register_filter",
    "registered_filters",
    "reset_filters",
    "unregister_filter",
]


@dataclass(frozen=True, slots=True)
class FilterContext:
    """Everything a predicate is given. Read-only by convention.

    A predicate that writes to the database would make the filter step's outcome
    depend on how many times it ran, and a retried job runs it more than once.
    """

    conn: sqlite3.Connection
    account_id: int
    campaign_id: int
    lead_id: int
    step: Step
    config: Mapping[str, Any] = field(default_factory=dict)

    def lead(self) -> Any:
        """Return the `Lead` this filter is deciding about, or None."""
        return get_lead(self.conn, self.lead_id)


FilterPredicate = Callable[[FilterContext], bool]
"""A filter: given a context, True keeps the lead in the flow."""


def _config_strings(config: Mapping[str, Any], key: str) -> list[str]:
    raw = config.get(key)
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, Iterable):
        return [str(item) for item in raw]
    return [str(raw)]


def _filter_always(context: FilterContext) -> bool:
    return True


def _filter_never(context: FilterContext) -> bool:
    return False


def _filter_has_tag(context: FilterContext) -> bool:
    wanted = _config_strings(context.config, "tag") or _config_strings(context.config, "tags")
    if not wanted:
        raise StepDefinitionError("the has_tag filter needs a 'tag' in its step config")
    held = {normalise_tag_name(name) for name in lead_tag_names(context.conn, context.lead_id)}
    return any(normalise_tag_name(name) in held for name in wanted)


def _filter_has_all_tags(context: FilterContext) -> bool:
    wanted = _config_strings(context.config, "tags")
    if not wanted:
        raise StepDefinitionError("the has_all_tags filter needs 'tags' in its step config")
    held = {normalise_tag_name(name) for name in lead_tag_names(context.conn, context.lead_id)}
    return all(normalise_tag_name(name) in held for name in wanted)


def _filter_lacks_tag(context: FilterContext) -> bool:
    return not _filter_has_tag(context)


def _filter_is_connected(context: FilterContext) -> bool:
    lead = context.lead()
    if lead is None:
        return False
    if lead.connected_at:
        return True
    return (lead.member_distance or "").strip().lower() in {"1st", "1", "first"}


def _filter_has_email(context: FilterContext) -> bool:
    row = context.conn.execute(
        """
        SELECT 1 FROM lead_contacts
        WHERE lead_id = ? AND kind IN ('email', 'work_email', 'personal_email')
        LIMIT 1
        """,
        (context.lead_id,),
    ).fetchone()
    return row is not None


def _filter_headline_contains(context: FilterContext) -> bool:
    needles = _config_strings(context.config, "contains")
    if not needles:
        raise StepDefinitionError(
            "the headline_contains filter needs 'contains' in its step config"
        )
    lead = context.lead()
    headline = (getattr(lead, "headline", None) or "").lower()
    if not headline:
        return False
    return any(needle.lower() in headline for needle in needles)


def _filter_organization_in(context: FilterContext) -> bool:
    wanted = {name.strip().lower() for name in _config_strings(context.config, "organizations")}
    if not wanted:
        raise StepDefinitionError(
            "the organization_in filter needs 'organizations' in its step config"
        )
    lead = context.lead()
    organization = (getattr(lead, "organization_name", None) or "").strip().lower()
    return bool(organization) and organization in wanted


BUILT_IN_FILTERS: Mapping[str, FilterPredicate] = {
    "always": _filter_always,
    "never": _filter_never,
    "has_tag": _filter_has_tag,
    "has_all_tags": _filter_has_all_tags,
    "lacks_tag": _filter_lacks_tag,
    "is_connected": _filter_is_connected,
    "has_email": _filter_has_email,
    "headline_contains": _filter_headline_contains,
    "organization_in": _filter_organization_in,
}
"""Predicates that need nothing beyond the local database."""

_REGISTRY: dict[str, FilterPredicate] = dict(BUILT_IN_FILTERS)


def register_filter(name: str, predicate: FilterPredicate, *, replace: bool = False) -> None:
    """Register a predicate under a name a step's config can point at.

    This is the seam SEQ-05 (#23) uses for ICP qualification: it registers an
    `icp_match` predicate, and a campaign gains ICP filtering by naming it in a
    step config. No import from this package into that one is needed.
    """
    key = (name or "").strip()
    if not key:
        raise ValueError("a filter name is required")
    if key in _REGISTRY and not replace:
        raise ValueError(f"a filter named {key!r} is already registered")
    _REGISTRY[key] = predicate


def unregister_filter(name: str) -> bool:
    """Remove a registered predicate. Returns False when it was not registered."""
    return _REGISTRY.pop(name, None) is not None


def reset_filters() -> None:
    """Restore the registry to the built-ins, primarily for tests."""
    _REGISTRY.clear()
    _REGISTRY.update(BUILT_IN_FILTERS)


def registered_filters() -> tuple[str, ...]:
    """Return every registered filter name, sorted."""
    return tuple(sorted(_REGISTRY))


def get_filter(name: str) -> FilterPredicate:
    """Return a registered predicate, raising when the name is unknown."""
    try:
        return _REGISTRY[name]
    except KeyError:
        raise FilterNotRegisteredError(name, registered_filters()) from None


def evaluate_filter(
    conn: sqlite3.Connection,
    account_id: int,
    campaign_id: int,
    lead_id: int,
    step: Step,
) -> bool:
    """Run a filter step's predicate and return whether the lead stays in the flow."""
    if not step.is_filter:
        raise StepDefinitionError(
            f"step {step.ord} of campaign {campaign_id} is a {step.action_type!r} step, "
            "not a filter"
        )
    name = step.filter_name
    if not name:
        raise StepDefinitionError(
            f"filter step {step.ord} of campaign {campaign_id} names no filter"
        )
    predicate = get_filter(name)
    context = FilterContext(
        conn=conn,
        account_id=account_id,
        campaign_id=campaign_id,
        lead_id=lead_id,
        step=step,
        config=step.config,
    )
    return bool(predicate(context))
