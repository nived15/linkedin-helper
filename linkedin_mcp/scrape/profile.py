"""One profile visit, end to end: gate, navigate, extract, store.

This is the importable entry point MCP-02 (#25) will wrap as a tool. It
registers nothing and schedules nothing. Give it a page, a connection, an
account and a profile URL, and it decides whether the visit is worth spending,
asks the safety gate, walks LinkedIn's own UI to the profile, reads it, and
stores it through the lead layer.

Never a direct URL load
-----------------------
LinkedIn caps direct ``/in/`` loads at roughly 40 per 24 hours and does not cap
in-page navigation anywhere near as hard. Every visit therefore goes through
:func:`linkedin_mcp.browser.navigate.goto_profile` with ``direct=False``, which
types the name into the global search bar and clicks the result. There is no
``page.goto`` in this module. ``direct=True`` remains reachable for a caller
that genuinely needs it, and when it is used the action is budgeted as
``profile_view_direct`` rather than ``profile_view``, so the two caps stay
honest. The action type comes from
:func:`linkedin_mcp.core.config.profile_view_action` rather than a literal, so
the budget and the gate can never disagree about which one this is.

Spending the budget on the leads that need it
---------------------------------------------
A profile visit is the most expensive thing this codebase does. Before spending
one, :func:`needs_refresh` is asked whether the sections this visit would read
are actually stale, and a lead whose sections are all fresh is skipped without
loading anything. Contact info counts towards that decision only for 1st-degree
connections, because for anyone else there is nothing behind the link to fetch.

Contact info
------------
Contact details are attempted only for 1st-degree connections. Any other degree
skips cleanly: no click, no exception, no error row. The reason is recorded on
the result so a caller can tell "we looked and there was nothing" apart from "we
never looked".
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from linkedin_mcp.audit import Outcome, log_action
from linkedin_mcp.browser.humanize import Humanizer, get_humanizer
from linkedin_mcp.browser.navigate import (
    NavigationResult,
    SessionExpiredError,
    goto_profile,
    profile_slug,
)
from linkedin_mcp.core.config import profile_view_action
from linkedin_mcp.leads import (
    HarvestSummary,
    Lead,
    LeadSection,
    get_lead,
    get_lead_by_public_id,
    is_blacklisted,
    is_blacklisted_by_public_id,
    needs_refresh,
)
from linkedin_mcp.safety import guard_action
from linkedin_mcp.scrape.paginate import GuardFn, RecordFn
from linkedin_mcp.scrape.profile_extract import extract_profile
from linkedin_mcp.scrape.profile_records import ProfileDetail
from linkedin_mcp.scrape.profile_store import ProfileStoreResult, store_profile_detail

logger = logging.getLogger(__name__)

__all__ = [
    "PROFILE_DETAIL_SOURCE",
    "PROFILE_SCROLL_DISTANCE",
    "PROFILE_TIMEOUT_MS",
    "ProfileScrapeBatch",
    "ProfileScrapeResult",
    "ProfileScrapeStatus",
    "profile_scrape_action",
    "run_profile_scrape",
    "run_profile_scrapes",
    "stale_profile_sections",
]

PROFILE_DETAIL_SOURCE = "profile_detail"
"""What the audit rows call this surface."""

PROFILE_SCROLL_DISTANCE = 2400
"""Pixels to scroll before reading, so the lazy sections below the fold render."""

PROFILE_TIMEOUT_MS = 30000

NavigateFn = Callable[..., Any]


def profile_scrape_action(direct: bool) -> str:
    """Return the budget one profile visit spends.

    Delegated to the config so the 40-a-day direct cap and the 100-a-day in-page
    cap are named in exactly one place.
    """
    return profile_view_action(direct)


class ProfileScrapeStatus(str, Enum):
    """How one profile visit ended."""

    SCRAPED = "scraped"
    SKIPPED_FRESH = "skipped_fresh"
    SKIPPED_BLACKLISTED = "skipped_blacklisted"
    REFUSED = "refused"
    NOT_STORED = "not_stored"


@dataclass(frozen=True, slots=True)
class ProfileScrapeResult:
    """What one profile visit read, stored and cost."""

    profile_url: str
    slug: str
    status: ProfileScrapeStatus
    action_type: str | None = None
    lead_id: int | None = None
    detail: ProfileDetail | None = None
    store: ProfileStoreResult = field(default_factory=ProfileStoreResult)
    sections_requested: tuple[str, ...] = ()
    sections_fetched: tuple[str, ...] = ()
    navigation: NavigationResult | None = None
    gate_refusal: Mapping[str, Any] | None = None

    @property
    def visited(self) -> bool:
        """True when this result cost a page load."""
        return self.navigation is not None

    @property
    def refused(self) -> bool:
        return self.status is ProfileScrapeStatus.REFUSED

    @property
    def harvest(self) -> HarvestSummary:
        return self.store.harvest

    @property
    def contact_info_skipped_reason(self) -> str | None:
        """Why contact info was not read, or None when it was."""
        return None if self.detail is None else self.detail.contact_info_skipped_reason

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON friendly payload, suitable for an MCP tool result."""
        return {
            "status": self.status.value,
            "source": PROFILE_DETAIL_SOURCE,
            "profile_url": self.profile_url,
            "slug": self.slug,
            "action_type": self.action_type,
            "visited": self.visited,
            "navigation_method": None if self.navigation is None else self.navigation.method,
            "lead_id": self.lead_id,
            "sections_requested": list(self.sections_requested),
            "sections_fetched": list(self.sections_fetched),
            "contact_info_skipped_reason": self.contact_info_skipped_reason,
            "gate_refusal": dict(self.gate_refusal) if self.gate_refusal else None,
            "profile": None if self.detail is None else self.detail.as_dict(),
            "store": self.store.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class ProfileScrapeBatch:
    """What a run of profile visits read, and why it stopped."""

    results: tuple[ProfileScrapeResult, ...] = ()
    gate_refusal: Mapping[str, Any] | None = None

    @property
    def visited(self) -> int:
        return sum(1 for result in self.results if result.visited)

    @property
    def scraped(self) -> int:
        return sum(
            1 for result in self.results if result.status is ProfileScrapeStatus.SCRAPED
        )

    @property
    def skipped(self) -> int:
        return sum(
            1
            for result in self.results
            if result.status
            in {
                ProfileScrapeStatus.SKIPPED_FRESH,
                ProfileScrapeStatus.SKIPPED_BLACKLISTED,
            }
        )

    @property
    def refused(self) -> bool:
        return self.gate_refusal is not None

    @property
    def lead_ids(self) -> tuple[int, ...]:
        return tuple(
            result.lead_id for result in self.results if result.lead_id is not None
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": "refused" if self.refused else "success",
            "source": PROFILE_DETAIL_SOURCE,
            "visited": self.visited,
            "scraped": self.scraped,
            "skipped": self.skipped,
            "lead_ids": list(self.lead_ids),
            "gate_refusal": dict(self.gate_refusal) if self.gate_refusal else None,
            "results": [result.as_dict() for result in self.results],
        }


def stale_profile_sections(
    conn: sqlite3.Connection,
    lead: Lead | None,
    *,
    now: datetime | None = None,
    force: bool = False,
) -> tuple[str, ...]:
    """Return the sections a visit to this lead would actually learn something from.

    A lead the database has never seen is entirely stale, because there is
    nothing stored to be fresh. Contact info only counts for a lead already
    known to be a 1st-degree connection: for anyone else the link is not there,
    so counting it stale would send the browser out for something LinkedIn will
    not show.
    """
    positions = LeadSection.POSITIONS.value
    contact = LeadSection.CONTACT_INFO.value

    if lead is None:
        return (contact, positions)
    if force:
        return (contact, positions)

    stale: list[str] = []
    if needs_refresh(conn, lead.id, LeadSection.CONTACT_INFO, now=now) and (
        lead.member_distance == "1st"
    ):
        stale.append(contact)
    if needs_refresh(conn, lead.id, LeadSection.POSITIONS, now=now):
        stale.append(positions)
    return tuple(stale)


async def run_profile_scrape(
    page: Any,
    conn: sqlite3.Connection,
    account_id: int,
    profile_url: str,
    *,
    lead_id: int | None = None,
    direct: bool = False,
    force: bool = False,
    humanizer: Humanizer | None = None,
    guard: GuardFn | None = None,
    record: RecordFn | None = None,
    clock: Callable[[], datetime] | None = None,
    navigate: NavigateFn | None = None,
    before_visit: Callable[[], Any] | None = None,
    harvest: bool = True,
    timeout: int = PROFILE_TIMEOUT_MS,
) -> ProfileScrapeResult:
    """Visit one profile and store everything it shows.

    Args:
        page: Playwright page already signed in to LinkedIn.
        conn: Open connection to the MCP database.
        account_id: Account the visit runs as.
        profile_url: Any LinkedIn ``/in/`` URL. Only the slug is used.
        lead_id: The stored lead this URL belongs to, when the caller already
            knows it. Left out, the lead is looked up by the URL slug, which
            misses a lead whose vanity URL has changed since it was stored and
            therefore visits it again rather than skipping it. A runner working
            from :func:`~linkedin_mcp.leads.leads_needing_refresh` should pass
            the id it already has.
        direct: Load the URL directly instead of navigating in-page. Costs the
            much smaller ``profile_view_direct`` budget, so leave it off.
        force: Visit even when every section is inside its cache window.
        humanizer: Pacing. Defaults to the process-wide humanizer.
        guard: Safety gate. Defaults to `guard_action`.
        record: Audit writer. Defaults to `log_action`.
        clock: Decision time source, injected so a runner stays deterministic.
        navigate: Navigation function. Defaults to `goto_profile`, which is the
            only path to a profile page in this codebase. It is injectable so a
            test can drive the visit without a browser, not so a caller can
            bypass the direct-load budget.
        before_visit: Awaited once the visit is certain to happen, after the
            gate has approved and before the page loads. A batch hangs its
            cooldown here so a skipped or refused profile costs no wait.
        harvest: Store what was read. Off for a dry run.
        timeout: Navigation timeout in milliseconds.

    Raises:
        SessionExpiredError: LinkedIn served a checkpoint or a login wall. The
            account state and the `safety_events` row are already written by
            then, and this propagates rather than returning a result, because a
            visit that quietly returns nothing looks exactly like an empty
            profile.
    """
    slug = profile_slug(profile_url)
    pacer = humanizer or get_humanizer()
    ask = guard or guard_action
    write = record or log_action
    tick = clock or (lambda: datetime.now(timezone.utc))
    travel = navigate or goto_profile

    lead = _resolve_lead(conn, account_id, slug, lead_id)

    # The slug is checked as well as the lead. The blacklist is global and keyed
    # on LinkedIn identifiers, so a person blocked through another account, or
    # blocked before this account ever stored them, has no lead row here. The
    # dedupe layer would refuse the write afterwards, but only after the visit
    # had already been paid for and LinkedIn had already seen it.
    blocked = is_blacklisted_by_public_id(conn, slug) or (
        lead is not None and is_blacklisted(conn, account_id, lead.id)
    )
    if blocked:
        logger.info("Not visiting %s: the lead is on the do-not-contact list", slug)
        return ProfileScrapeResult(
            profile_url=profile_url,
            slug=slug,
            status=ProfileScrapeStatus.SKIPPED_BLACKLISTED,
            lead_id=None if lead is None else lead.id,
        )

    now = tick()
    requested = stale_profile_sections(conn, lead, now=now, force=force)
    if not requested:
        logger.debug("Not visiting %s: every cached section is still fresh", slug)
        return ProfileScrapeResult(
            profile_url=profile_url,
            slug=slug,
            status=ProfileScrapeStatus.SKIPPED_FRESH,
            lead_id=None if lead is None else lead.id,
            sections_requested=(),
        )

    action_type = profile_scrape_action(direct)
    refusal = ask(
        action_type,
        account_id=account_id,
        lead_id=None if lead is None else lead.id,
        now=now,
    )
    if refusal is not None:
        logger.info(
            "Safety gate stopped a profile visit to %s: %s",
            slug,
            refusal.get("message", refusal.get("reason")),
        )
        return ProfileScrapeResult(
            profile_url=profile_url,
            slug=slug,
            status=ProfileScrapeStatus.REFUSED,
            action_type=action_type,
            lead_id=None if lead is None else lead.id,
            sections_requested=requested,
            gate_refusal=refusal,
        )

    detail_row = {"source": PROFILE_DETAIL_SOURCE, "slug": slug, "direct": direct}

    if before_visit is not None:
        await before_visit()

    try:
        navigation = await travel(
            page,
            profile_url,
            humanizer=pacer,
            direct=direct,
            account_id=account_id,
            timeout=timeout,
        )
    except SessionExpiredError as error:
        write(
            account_id,
            action_type,
            Outcome.FAILURE,
            lead_id=None if lead is None else lead.id,
            detail={**detail_row, "error": str(error)},
            occurred_at=tick(),
        )
        raise

    write(
        account_id,
        action_type,
        Outcome.SUCCESS,
        lead_id=None if lead is None else lead.id,
        detail={**detail_row, "method": navigation.method},
        occurred_at=tick(),
    )

    await pacer.scroll(page, PROFILE_SCROLL_DISTANCE)

    detail = await extract_profile(
        page,
        profile_url=profile_url,
        humanizer=pacer,
        read_contact_info=LeadSection.CONTACT_INFO.value in requested,
    )

    fetched = _sections_fetched(detail)

    if not harvest:
        return ProfileScrapeResult(
            profile_url=profile_url,
            slug=slug,
            status=ProfileScrapeStatus.SCRAPED,
            action_type=action_type,
            lead_id=None if lead is None else lead.id,
            detail=detail,
            sections_requested=requested,
            sections_fetched=fetched,
            navigation=navigation,
        )

    stored = store_profile_detail(
        conn,
        account_id,
        detail,
        sections_fetched=fetched,
        fetched_at=now,
        visited_at=now,
    )

    return ProfileScrapeResult(
        profile_url=profile_url,
        slug=slug,
        status=(
            ProfileScrapeStatus.SCRAPED
            if stored.stored
            else ProfileScrapeStatus.NOT_STORED
        ),
        action_type=action_type,
        lead_id=stored.lead_id,
        detail=detail,
        store=stored,
        sections_requested=requested,
        sections_fetched=fetched,
        navigation=navigation,
    )


def _resolve_lead(
    conn: sqlite3.Connection,
    account_id: int,
    slug: str,
    lead_id: int | None,
) -> Lead | None:
    """Find the stored lead this visit is about, by id when the caller knows it."""
    if lead_id is None:
        return get_lead_by_public_id(conn, account_id, slug)

    lead = get_lead(conn, lead_id)
    if lead is None:
        raise ValueError(f"lead {lead_id} does not exist")
    if lead.account_id != account_id:
        raise ValueError(
            f"lead {lead_id} belongs to account {lead.account_id}, not {account_id}"
        )
    return lead


def _sections_fetched(detail: ProfileDetail) -> tuple[str, ...]:
    """Return the cache sections this visit is entitled to mark fresh.

    A section is only marked when the page was understood. A member with no
    positions listed counts, because the section rendered and was empty, and
    revisiting them every fortnight to confirm that would waste the budget the
    cache windows exist to protect. A section that did not resolve does not
    count, and neither does a contact overlay that opened without a readable
    body: marking either would suppress the retry that would have noticed the
    markup had moved.
    """
    sections: list[str] = []
    if detail.contact_info_outcome.was_seen():
        sections.append(LeadSection.CONTACT_INFO.value)
    if detail.experience_outcome.was_seen():
        sections.append(LeadSection.POSITIONS.value)
    return tuple(sections)


async def run_profile_scrapes(
    page: Any,
    conn: sqlite3.Connection,
    account_id: int,
    profile_urls: Iterable[str],
    *,
    limit: int | None = None,
    direct: bool = False,
    force: bool = False,
    humanizer: Humanizer | None = None,
    guard: GuardFn | None = None,
    record: RecordFn | None = None,
    clock: Callable[[], datetime] | None = None,
    navigate: NavigateFn | None = None,
    harvest: bool = True,
    timeout: int = PROFILE_TIMEOUT_MS,
) -> ProfileScrapeBatch:
    """Visit a list of profiles, pacing between them and stopping at the gate.

    The gate is asked per profile rather than once for the batch, so a run that
    crosses a cap boundary halfway through stops there. A refusal ends the batch
    instead of skipping one profile and carrying on, because the next profile
    would be refused for the same reason and asking again is just noise.

    Skipping a fresh or blacklisted lead costs no page load, and neither does a
    profile the gate refuses. The cooldown therefore hangs off `before_visit`,
    which only fires once the visit is certain, so a run of skips does not spend
    a minute each pretending to be human at nothing.
    """
    pacer = humanizer or get_humanizer()
    results: list[ProfileScrapeResult] = []
    refusal: Mapping[str, Any] | None = None
    visited = 0

    async def cool_down() -> None:
        if visited:
            await pacer.cooldown()

    for profile_url in _unique(profile_urls):
        if limit is not None and visited >= limit:
            break

        result = await run_profile_scrape(
            page,
            conn,
            account_id,
            profile_url,
            direct=direct,
            force=force,
            humanizer=pacer,
            guard=guard,
            record=record,
            clock=clock,
            navigate=navigate,
            before_visit=cool_down,
            harvest=harvest,
            timeout=timeout,
        )
        results.append(result)

        if result.refused:
            refusal = result.gate_refusal
            break
        if result.visited:
            visited += 1

    return ProfileScrapeBatch(results=tuple(results), gate_refusal=refusal)


def _unique(profile_urls: Iterable[str]) -> Sequence[str]:
    """Drop repeats without reordering, so a list never visits anyone twice."""
    return list(dict.fromkeys(url for url in profile_urls if url))
