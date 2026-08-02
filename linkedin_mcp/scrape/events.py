"""Event attendees, harvested from an event's networking list.

An event attendee list is the second best free signal after post engagers. A
person who registered for a talk on developer productivity has said what they
are interested in this month, which is more than a job title says.

The surface itself is unremarkable: a lazily loaded column of profile links, so
it runs through :func:`linkedin_mcp.scrape.sources.run_people_list_harvest` and
inherits the per-slice safety gate, the humanised reveal and the DB-03 dedupe
without adding a line of loop code.

Budget
------
`profile_search`, the same 50 a day a People search and a group member list
spend. An attendee list is a paged list of profiles; giving it a budget of its
own would let one account walk fifty searches and fifty attendee lists in a day
while still claiming it stayed inside the search cap.

Known gap
---------
LinkedIn has moved the attendee route before, and the tab this reads has not
been confirmed against a live event. `tab` is therefore a parameter rather than
a constant, and `EVENT_TABS` lists the routes worth trying. A wrong tab reads as
an empty list and stops on no-new-results, which reports as a harvest that found
nobody rather than as a crash.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime
from typing import Any

from linkedin_mcp.browser.humanize import Humanizer
from linkedin_mcp.scrape.paginate import GuardFn, RecordFn, SearchCursor
from linkedin_mcp.scrape.sources import (
    DEFAULT_LIMIT,
    EVENT_TABS,
    PAGE_TIMEOUT_MS,
    PEOPLE_LIST_ACTION,
    SOURCE_EVENT_ATTENDEES,
    PeopleListSurface,
    event_attendees_url,
    event_id_from,
    run_people_list_harvest,
)
from linkedin_mcp.scrape.summary import ScrapeSummary

__all__ = [
    "ATTENDEE_SURFACE",
    "EVENT_ATTENDEES_ACTION",
    "EVENT_TABS",
    "run_event_attendee_harvest",
]

EVENT_ATTENDEES_ACTION = PEOPLE_LIST_ACTION
"""Attendee lists spend the same budget as a People search."""

ATTENDEE_SURFACE = PeopleListSurface(
    source=SOURCE_EVENT_ATTENDEES,
    action_type=EVENT_ATTENDEES_ACTION,
    item="event_attendee_item",
    link="event_attendee_profile_link",
    name="event_attendee_name",
    headline="event_attendee_headline",
    distance="event_attendee_distance",
    load_more="event_attendee_load_more",
)


async def run_event_attendee_harvest(
    page: Any,
    conn: sqlite3.Connection,
    account_id: int,
    event: str | int,
    *,
    tab: str = EVENT_TABS[0],
    limit: int = DEFAULT_LIMIT,
    cursor: SearchCursor | None = None,
    humanizer: Humanizer | None = None,
    guard: GuardFn | None = None,
    record: RecordFn | None = None,
    clock: Callable[[], datetime] | None = None,
    harvest: bool = True,
    run_id: int | None = None,
    timeout: int = PAGE_TIMEOUT_MS,
) -> ScrapeSummary:
    """Harvest the attendees of a LinkedIn event.

    Args:
        page: Playwright page already signed in to LinkedIn.
        conn: Open connection to the MCP database.
        account_id: Account the run belongs to.
        event: Event id or event URL.
        tab: Which event tab carries the attendee list.
        limit: How many new attendees this run wants.
        cursor: Resume point from a previous run.
        humanizer: Pacing. Defaults to the process-wide humanizer.
        guard: Safety gate. Defaults to `guard_action`.
        record: Audit writer. Defaults to `log_action`.
        clock: Decision time source, injected so a runner stays deterministic.
        harvest: Store attendees through the lead store. Off for a dry run.
        run_id: Existing `harvest_runs` row to continue, if resuming one.
        timeout: Navigation timeout in milliseconds.
    """
    event_id = event_id_from(event)
    return await run_people_list_harvest(
        page,
        conn,
        account_id,
        ATTENDEE_SURFACE,
        event_attendees_url(event_id, tab),
        params={"event_id": event_id, "tab": tab},
        limit=limit,
        cursor=cursor,
        humanizer=humanizer,
        guard=guard,
        record=record,
        clock=clock,
        harvest=harvest,
        run_id=run_id,
        timeout=timeout,
    )
