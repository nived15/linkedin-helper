"""Company employees, your own connections, and your followers.

Three surfaces that have nothing in common on LinkedIn and everything in common
here: each is a lazily loaded column of profile links, so each is a
:class:`linkedin_mcp.scrape.sources.PeopleListSurface` and each runs through the
same gated, paced, deduplicating loop.

Why your own network is worth harvesting
----------------------------------------
It is the only source where the relationship already exists. A connection list
seeds a re-engagement campaign, and a follower list is a warm audience that has
opted in without being asked. Both also give DB-03 a set of first-degree
identities to resolve later sightings against, which makes every other source
dedupe better.

Budget
------
All three spend `profile_search`, the same 50 a day as a People search, for the
reason `groups.py` gives: these are paged lists of profiles and a separate
budget would let one account walk fifty of each in a day while claiming it
stayed inside the search cap.

Known gaps
----------
None of the three routes has been confirmed against a live session. The
follower list in particular has moved between `/mynetwork/` and `/feed/` more
than once. A wrong URL reads as an empty list and stops on no-new-results, so
the failure mode is a harvest that reports finding nobody rather than a crash,
and the fix is one constant in `sources.py`.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime
from typing import Any

from linkedin_mcp.browser.humanize import Humanizer
from linkedin_mcp.scrape.paginate import GuardFn, RecordFn, SearchCursor
from linkedin_mcp.scrape.sources import (
    CONNECTIONS_URL,
    DEFAULT_LIMIT,
    FOLLOWERS_URL,
    PAGE_TIMEOUT_MS,
    PEOPLE_LIST_ACTION,
    SOURCE_COMPANY_EMPLOYEES,
    SOURCE_CONNECTIONS,
    SOURCE_FOLLOWERS,
    PeopleListSurface,
    company_id_from,
    company_people_url,
    run_people_list_harvest,
)
from linkedin_mcp.scrape.summary import ScrapeSummary

__all__ = [
    "COMPANY_EMPLOYEE_SURFACE",
    "CONNECTION_SURFACE",
    "FOLLOWER_SURFACE",
    "PEOPLE_LIST_ACTION",
    "run_company_employee_harvest",
    "run_connection_harvest",
    "run_follower_harvest",
]

COMPANY_EMPLOYEE_SURFACE = PeopleListSurface(
    source=SOURCE_COMPANY_EMPLOYEES,
    action_type=PEOPLE_LIST_ACTION,
    item="company_employee_item",
    link="company_employee_profile_link",
    name="company_employee_name",
    headline="company_employee_headline",
    load_more="company_employee_load_more",
)

CONNECTION_SURFACE = PeopleListSurface(
    source=SOURCE_CONNECTIONS,
    action_type=PEOPLE_LIST_ACTION,
    item="connection_item",
    link="connection_profile_link",
    name="connection_name",
    headline="connection_headline",
    load_more="connection_load_more",
)

FOLLOWER_SURFACE = PeopleListSurface(
    source=SOURCE_FOLLOWERS,
    action_type=PEOPLE_LIST_ACTION,
    item="follower_item",
    link="follower_profile_link",
    name="follower_name",
    headline="follower_headline",
    load_more="follower_load_more",
)


async def run_company_employee_harvest(
    page: Any,
    conn: sqlite3.Connection,
    account_id: int,
    company: str | int,
    *,
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
    """Harvest the employees listed on a company page.

    Args:
        page: Playwright page already signed in to LinkedIn.
        conn: Open connection to the MCP database.
        account_id: Account the run belongs to.
        company: Company slug, numeric id, or company URL.
        limit: How many new employees this run wants.
        cursor: Resume point from a previous run.
        humanizer: Pacing. Defaults to the process-wide humanizer.
        guard: Safety gate. Defaults to `guard_action`.
        record: Audit writer. Defaults to `log_action`.
        clock: Decision time source, injected so a runner stays deterministic.
        harvest: Store employees through the lead store. Off for a dry run.
        run_id: Existing `harvest_runs` row to continue, if resuming one.
        timeout: Navigation timeout in milliseconds.
    """
    company_id = company_id_from(company)
    return await run_people_list_harvest(
        page,
        conn,
        account_id,
        COMPANY_EMPLOYEE_SURFACE,
        company_people_url(company_id),
        params={"company_id": company_id},
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


async def run_connection_harvest(
    page: Any,
    conn: sqlite3.Connection,
    account_id: int,
    *,
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
    """Harvest your own first-degree connections.

    Arguments match :func:`run_company_employee_harvest`, minus the company.
    """
    return await run_people_list_harvest(
        page,
        conn,
        account_id,
        CONNECTION_SURFACE,
        CONNECTIONS_URL,
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


async def run_follower_harvest(
    page: Any,
    conn: sqlite3.Connection,
    account_id: int,
    *,
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
    """Harvest the people following your profile.

    Arguments match :func:`run_connection_harvest`.
    """
    return await run_people_list_harvest(
        page,
        conn,
        account_id,
        FOLLOWER_SURFACE,
        FOLLOWERS_URL,
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
