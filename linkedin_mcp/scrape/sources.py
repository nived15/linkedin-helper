"""One engine for every LinkedIn surface that is a column of profile links.

Post reactions, post comments, event attendees, company employees, your own
connections and your followers are five different URLs and one shape: a lazily
loaded list of people, revealed a slice at a time by scrolling or by a load-more
button. Writing five loops for that would be five places to get the gate wrong,
so this module describes a surface as data and hands the walking to
:func:`linkedin_mcp.scrape.paginate.paginate`, which SCRAPE-01 already built.

What this module does not own
-----------------------------
Nothing here paginates, meters, detects a challenge, or writes to `leads`. The
paged loop, the per-fetch safety gate call and the session check are
`paginate`'s. Persistence and dedupe are `harvest_people`'s, which is DB-02's
`harvest_leads` underneath. Run bookkeeping is `runs.py`'s. This module supplies
the URL, the selectors, and the gesture that reveals the next slice.

Action types
------------
Two, both already configured. A people list spends `profile_search`, for the
reason `groups.py` gives: a paged list of profiles is the same load a People
search is, and a separate budget would let one account take fifty searches and
fifty attendee lists in a day while claiming it stayed inside the search cap.
Post engagers spend `post_read` instead, because that surface is a post
permalink and its social detail rather than a people list route.

Known gaps
----------
The reactions list lives inside a modal. `Humanizer.scroll` scrolls the window,
which does not move a modal's own scroll container, so revealing the next slice
also pulls the last visible row into view and clicks the modal's load-more
button when one is present. Whether LinkedIn's reactions modal actually paginates
by button, by container scroll, or by both has not been checked against a live
session, which is why both gestures are attempted and neither is required.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from linkedin_mcp.browser.humanize import Humanizer, get_humanizer
from linkedin_mcp.leads import HarvestSummary
from linkedin_mcp.scrape.extract import (
    URN_ATTRIBUTES,
    attr_of,
    query_all,
    query_first,
    text_of,
)
from linkedin_mcp.scrape.harvest import harvest_people, stale_lead_ids
from linkedin_mcp.scrape.paginate import (
    SCROLL_DISTANCE,
    GuardFn,
    RecordFn,
    SearchCursor,
    StopReason,
    paginate,
)
from linkedin_mcp.scrape.records import (
    PersonResult,
    activity_id_from,
    canonical_profile_url,
    member_urn_from,
    name_from_slug,
    parse_distance,
    profile_hash_from,
    public_id_from,
)
from linkedin_mcp.scrape.runs import finish_harvest_run, start_harvest_run
from linkedin_mcp.scrape.summary import ScrapeSummary, merge_harvest

logger = logging.getLogger(__name__)

__all__ = [
    "CONNECTIONS_URL",
    "DEFAULT_LIMIT",
    "EVENT_ATTENDEES_URL_TEMPLATE",
    "EVENT_TABS",
    "FOLLOWERS_URL",
    "PAGE_TIMEOUT_MS",
    "PEOPLE_LIST_ACTION",
    "POST_ENGAGERS_ACTION",
    "POST_PERMALINK_TEMPLATE",
    "SOURCE_COMPANY_EMPLOYEES",
    "SOURCE_CONNECTIONS",
    "SOURCE_CSV_IMPORT",
    "SOURCE_EVENT_ATTENDEES",
    "SOURCE_FOLLOWERS",
    "SOURCE_POST_COMMENTS",
    "SOURCE_POST_ENGAGERS",
    "SOURCE_POST_REACTIONS",
    "PeopleListSurface",
    "combine_summaries",
    "company_id_from",
    "company_people_url",
    "event_attendees_url",
    "event_id_from",
    "extract_people_list",
    "post_permalink",
    "run_people_list_harvest",
]

PEOPLE_LIST_ACTION = "profile_search"
"""Budget one slice of a people list spends. Configured at 50 a day."""

POST_ENGAGERS_ACTION = "post_read"
"""Budget one slice of a post's engagers spends. Configured at 100 a day."""

SOURCE_POST_REACTIONS = "post_reactions"
SOURCE_POST_COMMENTS = "post_comments"
SOURCE_POST_ENGAGERS = "post_engagers"
SOURCE_EVENT_ATTENDEES = "event_attendees"
SOURCE_COMPANY_EMPLOYEES = "company_employees"
SOURCE_CONNECTIONS = "connections"
SOURCE_FOLLOWERS = "followers"
SOURCE_CSV_IMPORT = "csv_import"

DEFAULT_LIMIT = 100
PAGE_TIMEOUT_MS = 30000

POST_PERMALINK_TEMPLATE = "https://www.linkedin.com/feed/update/urn:li:activity:{activity_id}/"
EVENT_ATTENDEES_URL_TEMPLATE = "https://www.linkedin.com/events/{event_id}/{tab}/"
COMPANY_PEOPLE_URL_TEMPLATE = "https://www.linkedin.com/company/{company_id}/people/"
CONNECTIONS_URL = "https://www.linkedin.com/mynetwork/invite-connect/connections/"
FOLLOWERS_URL = "https://www.linkedin.com/mynetwork/network-manager/people-follow/followers/"

EVENT_TABS = ("attendees", "comments")
"""Event tabs that render a people list.

`attendees` is the tab the issue calls Networking. LinkedIn has moved this
route before and the second entry is the older one, so the tab is a parameter
rather than a constant and a caller who finds the default empty can say so.
"""

EVENT_PATH_PATTERN = re.compile(r"/events/(?P<event>[^/?#]+)")
COMPANY_PATH_PATTERN = re.compile(r"/(?:company|school|showcase)/(?P<company>[^/?#]+)")
POST_PATH_PATTERN = re.compile(r"https?://[^/]*linkedin\.com(?P<path>/(?:posts|feed)/[^?#]+)")
DIGITS_PATTERN = re.compile(r"^\d+$")


def post_permalink(post: str | int) -> str:
    """Return a canonical post permalink from an id, a URN or any post URL.

    Everything that identifies a post ends up as the same `/feed/update/` URL,
    which is what makes a liker harvest and a commenter harvest of the same post
    land on one page rather than two. A `/posts/` share link with no activity id
    in it is kept as written, because LinkedIn resolves those itself.
    """
    text = str(post).strip()
    if not text:
        raise ValueError("a post permalink needs a post URL, URN or activity id")
    if DIGITS_PATTERN.match(text):
        return POST_PERMALINK_TEMPLATE.format(activity_id=text)
    activity_id = activity_id_from(text)
    if activity_id:
        return POST_PERMALINK_TEMPLATE.format(activity_id=activity_id)
    match = POST_PATH_PATTERN.match(text)
    if match:
        return f"https://www.linkedin.com{match.group('path')}"
    raise ValueError(
        f"{post!r} is not a LinkedIn post. Give an activity id, a "
        "urn:li:activity:... URN, or a post permalink."
    )


def event_id_from(event: str | int) -> str:
    """Return the event key from an event id or an event URL.

    LinkedIn events are addressed by a slug that ends in digits as often as by
    bare digits, and both resolve, so the whole path segment is kept rather than
    the numeric tail being dug out of it.
    """
    text = str(event).strip()
    if not text:
        raise ValueError("an event needs an id or a LinkedIn event URL")
    match = EVENT_PATH_PATTERN.search(text)
    if match:
        return match.group("event")
    if "/" in text or " " in text:
        raise ValueError(
            f"{event!r} is not a LinkedIn event id or event URL such as "
            "https://www.linkedin.com/events/1234567890/"
        )
    return text


def event_attendees_url(event: str | int, tab: str = EVENT_TABS[0]) -> str:
    """Return the attendee list URL for a LinkedIn event."""
    if tab not in EVENT_TABS:
        raise ValueError(f"{tab!r} is not one of the event tabs {EVENT_TABS}")
    return EVENT_ATTENDEES_URL_TEMPLATE.format(event_id=event_id_from(event), tab=tab)


def company_id_from(company: str | int) -> str:
    """Return the company slug or id from a company URL or a bare slug."""
    text = str(company).strip()
    if not text:
        raise ValueError("a company needs a slug, an id or a LinkedIn company URL")
    match = COMPANY_PATH_PATTERN.search(text)
    if match:
        return match.group("company")
    if "/" in text or " " in text:
        raise ValueError(
            f"{company!r} is not a LinkedIn company id or company URL such as "
            "https://www.linkedin.com/company/microsoft/"
        )
    return text


def company_people_url(company: str | int) -> str:
    """Return the employee list URL for a LinkedIn company page."""
    return COMPANY_PEOPLE_URL_TEMPLATE.format(company_id=company_id_from(company))


@dataclass(frozen=True, slots=True)
class PeopleListSurface:
    """The selectors and gestures that make one people list readable.

    Only `item`, `link` and `name` are required, because every other field is
    one LinkedIn redesign away from not rendering and a sighting with a profile
    link is already a lead. An unset optional selector is never queried, and a
    set one that finds nothing yields None rather than raising.
    """

    source: str
    action_type: str
    item: str
    link: str
    name: str
    headline: str | None = None
    location: str | None = None
    distance: str | None = None
    avatar: str | None = None
    load_more: str | None = None
    opener: str | None = None


async def _urn_of(handle: Any) -> str | None:
    """Return whichever entity URN attribute a row carries, if any."""
    getter = getattr(handle, "get_attribute", None)
    if getter is None:
        return None
    for attribute in URN_ATTRIBUTES:
        try:
            value = await getter(attribute)
        except Exception as error:  # noqa: BLE001 - a dead handle is not fatal
            logger.debug("Reading @%s off a list row failed: %s", attribute, error)
            continue
        if value:
            return value
    return None


async def _person_from_row(item: Any, surface: PeopleListSurface) -> PersonResult | None:
    """Build a person from one list row, tolerating every missing field but identity."""
    profile_url = await attr_of(item, surface.link, "href")
    public_id = public_id_from(profile_url)
    urn = await _urn_of(item)
    member_id = member_urn_from(urn) or member_urn_from(profile_url)
    hash_id = profile_hash_from(urn) or profile_hash_from(profile_url)

    if public_id is None and member_id is None:
        logger.debug("Skipping a %s row with no profile link", surface.source)
        return None

    full_name = await text_of(item, surface.name)
    if not full_name:
        full_name = name_from_slug(public_id) if public_id else (member_id or "")
    if not full_name:
        return None

    return PersonResult(
        full_name=full_name,
        public_id=public_id,
        member_id=member_id,
        hash_id=hash_id,
        headline=await text_of(item, surface.headline) if surface.headline else None,
        location_name=(
            await text_of(item, surface.location) if surface.location else None
        ),
        member_distance=parse_distance(
            await text_of(item, surface.distance) if surface.distance else None
        ),
        avatar_url=(
            await attr_of(item, surface.avatar, "src") if surface.avatar else None
        ),
        profile_url=canonical_profile_url(profile_url),
    )


async def extract_people_list(
    page: Any, surface: PeopleListSurface
) -> list[PersonResult]:
    """Extract every person on a rendered people list.

    One unreadable row never takes the slice down with it, which matters more
    here than on a search page: a reactions modal mixes people with companies,
    and a company row has no `/in/` link to read.
    """
    people: list[PersonResult] = []
    for item in await query_all(page, surface.item):
        try:
            person = await _person_from_row(item, surface)
        except Exception as error:  # noqa: BLE001 - one bad row is not a bad slice
            logger.warning("Skipping an unreadable %s row: %s", surface.source, error)
            continue
        if person is not None:
            people.append(person)
    return people


async def _scroll_last_row_into_view(page: Any, surface: PeopleListSurface) -> None:
    """Pull the last rendered row into view, which scrolls its own container.

    A window scroll does not move a modal, and the reactions list is a modal.
    Asking the last row to scroll itself into view moves whichever element is
    actually scrollable, so the same gesture works for a modal and for a page.
    """
    rows = await query_all(page, surface.item)
    if not rows:
        return
    reveal = getattr(rows[-1], "scroll_into_view_if_needed", None)
    if reveal is None:
        return
    try:
        await reveal()
    except Exception as error:  # noqa: BLE001 - a detached row is not fatal
        logger.debug("Scrolling the last %s row into view failed: %s", surface.source, error)


async def _click_named(page: Any, name: str, pacer: Humanizer) -> bool:
    """Click the first control matching a selector name, reporting whether it was there."""
    control = await query_first(page, name)
    if control is None:
        return False
    try:
        await pacer.click(control)
    except Exception as error:  # noqa: BLE001 - a stale control is not fatal
        logger.debug("Clicking %s failed: %s", name, error)
        return False
    return True


async def _reveal_more(page: Any, surface: PeopleListSurface, pacer: Humanizer) -> None:
    """Advance a lazily loaded people list by one slice.

    Three gestures, none of them required. LinkedIn reveals more rows on window
    scroll on some lists, on container scroll on others, and only on a button on
    the rest. When none of them reveals anything new the paginator stops on its
    no-new-results condition, which is an ordinary outcome rather than an error.
    """
    await pacer.scroll(page, SCROLL_DISTANCE)
    await _scroll_last_row_into_view(page, surface)
    if surface.load_more:
        await _click_named(page, surface.load_more, pacer)


async def run_people_list_harvest(
    page: Any,
    conn: sqlite3.Connection,
    account_id: int,
    surface: PeopleListSurface,
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
    limit: int = DEFAULT_LIMIT,
    cursor: SearchCursor | None = None,
    humanizer: Humanizer | None = None,
    guard: GuardFn | None = None,
    record: RecordFn | None = None,
    clock: Callable[[], datetime] | None = None,
    harvest: bool = True,
    run_id: int | None = None,
    manage_run: bool = True,
    timeout: int = PAGE_TIMEOUT_MS,
) -> ScrapeSummary:
    """Walk one lazily loaded people list and store everyone on it as a lead.

    Args:
        page: Playwright page already signed in to LinkedIn.
        conn: Open connection to the MCP database.
        account_id: Account the run belongs to.
        surface: Selectors and gestures for this list.
        url: Where the list lives.
        params: Extra run parameters recorded on the `harvest_runs` row.
        limit: How many new people this run wants.
        cursor: Resume point from a previous run.
        humanizer: Pacing. Defaults to the process-wide humanizer.
        guard: Safety gate. Defaults to `guard_action`.
        record: Audit writer. Defaults to `log_action`.
        clock: Decision time source, injected so a runner stays deterministic.
        harvest: Store people through the lead store. Off for a dry run.
        run_id: Existing `harvest_runs` row this run belongs to.
        manage_run: Open and close the run row here. Off when a caller runs
            several phases of one logical harvest and closes the row itself.
        timeout: Navigation timeout in milliseconds.
    """
    pacer = humanizer or get_humanizer()
    tick = clock or (lambda: datetime.now(timezone.utc))
    started = tick()
    run_params = {"url": url, **dict(params or {})}
    loaded = False

    async def fetch(target: Any, step: int) -> None:
        nonlocal loaded
        if not loaded:
            # A list route is an ordinary application page rather than an
            # `/in/` profile, so CORE-04's direct profile load cap does not
            # apply and a plain goto is the honest way to reach it.
            await target.goto(url, wait_until="domcontentloaded", timeout=timeout)
            loaded = True
            if surface.opener:
                await _click_named(target, surface.opener, pacer)
            for _ in range(step - 1):
                await _reveal_more(target, surface, pacer)
            return
        await _reveal_more(target, surface, pacer)

    totals = HarvestSummary()

    async def on_page(people: list[PersonResult], step: int) -> None:
        nonlocal totals
        if not harvest:
            return
        slice_summary = harvest_people(conn, account_id, people, fetched_at=tick())
        totals = merge_harvest(totals, slice_summary)
        logger.debug(
            "%s slice %d stored %d new and %d updated lead(s)",
            surface.source,
            step,
            slice_summary.created,
            slice_summary.updated,
        )

    if harvest and manage_run and run_id is None:
        run_id = start_harvest_run(
            conn,
            account_id,
            surface.source,
            run_params,
            cursor=cursor,
            started_at=started,
        )

    run = await paginate(
        page,
        action_type=surface.action_type,
        account_id=account_id,
        fetch=fetch,
        extract=lambda target: extract_people_list(target, surface),
        key=lambda person: person.dedupe_key,
        limit=limit,
        cursor=cursor,
        humanizer=pacer,
        guard=guard,
        record=record,
        clock=tick,
        on_page=on_page,
        scroll_before_extract=False,
        detail={"source": surface.source, **dict(params or {})},
    )

    stale = (
        stale_lead_ids(conn, account_id, totals.lead_ids, now=tick())
        if harvest
        else ()
    )

    if harvest and manage_run and run_id is not None:
        finish_harvest_run(
            conn,
            run_id,
            found=run.results_seen,
            new=totals.created,
            cursor=run.cursor,
            params=run_params,
            finished_at=tick(),
        )

    return ScrapeSummary(
        source=surface.source,
        action_type=surface.action_type,
        stop_reason=run.stop_reason,
        cursor=run.cursor,
        pages_fetched=run.pages_fetched,
        results_seen=run.results_seen,
        results_new=len(run.results),
        duplicates_skipped=run.duplicates_skipped,
        people=run.results,
        harvest=totals,
        gate_refusal=run.gate_refusal,
        harvest_run_id=run_id,
        stale_lead_ids=stale,
    )


def combine_summaries(
    source: str,
    first: ScrapeSummary,
    second: ScrapeSummary | None = None,
    *,
    run_id: int | None = None,
) -> ScrapeSummary:
    """Fold two phases of one logical harvest into a single summary.

    A refusal in either phase wins, because a run that was stopped by the gate
    halfway is a refused run however much it collected first. Everything else
    reports the phase that finished last, which is where a resume would pick up.
    """
    if second is None:
        return ScrapeSummary(
            source=source,
            action_type=first.action_type,
            stop_reason=first.stop_reason,
            cursor=first.cursor,
            pages_fetched=first.pages_fetched,
            results_seen=first.results_seen,
            results_new=first.results_new,
            duplicates_skipped=first.duplicates_skipped,
            people=first.people,
            posts=first.posts,
            harvest=first.harvest,
            gate_refusal=first.gate_refusal,
            harvest_run_id=run_id if run_id is not None else first.harvest_run_id,
            stale_lead_ids=first.stale_lead_ids,
        )

    refusal = first.gate_refusal or second.gate_refusal
    stop_reason = (
        StopReason.GATE_REFUSED
        if first.refused or second.refused
        else second.stop_reason
    )
    stale = tuple(dict.fromkeys(first.stale_lead_ids + second.stale_lead_ids))
    return ScrapeSummary(
        source=source,
        action_type=second.action_type,
        stop_reason=stop_reason,
        cursor=second.cursor,
        pages_fetched=first.pages_fetched + second.pages_fetched,
        results_seen=first.results_seen + second.results_seen,
        results_new=first.results_new + second.results_new,
        duplicates_skipped=first.duplicates_skipped + second.duplicates_skipped,
        people=first.people + second.people,
        posts=first.posts + second.posts,
        harvest=merge_harvest(first.harvest, second.harvest),
        gate_refusal=refusal,
        harvest_run_id=(
            run_id
            if run_id is not None
            else (first.harvest_run_id or second.harvest_run_id)
        ),
        stale_lead_ids=stale,
    )
