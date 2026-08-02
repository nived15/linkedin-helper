"""People search extraction, filtered and paginated.

This is the importable entry point MCP-02 (#25) will wrap as a tool. It takes a
page and a validated filter set, walks the result pages until one of the four
stop conditions fires, stores every person through the lead store, and returns a
summary. It registers nothing, prints nothing and schedules nothing.

Driving it from a background job
--------------------------------
SEQ-04 (#22) owns the runner. Everything it needs is a parameter here: the page
and the clock are injected, the gate is asked before every fetch, and the run
both accepts and returns a cursor so an interrupted search resumes on the page
it stopped on rather than starting over.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from linkedin_mcp.browser.humanize import Humanizer, get_humanizer
from linkedin_mcp.leads import HarvestSummary
from linkedin_mcp.scrape.extract import extract_people
from linkedin_mcp.scrape.filters import PeopleSearchFilters
from linkedin_mcp.scrape.harvest import harvest_people, stale_lead_ids
from linkedin_mcp.scrape.paginate import (
    GuardFn,
    RecordFn,
    SearchCursor,
    paginate,
)
from linkedin_mcp.scrape.records import PersonResult
from linkedin_mcp.scrape.runs import (
    SOURCE_PEOPLE_SEARCH,
    finish_harvest_run,
    start_harvest_run,
)
from linkedin_mcp.scrape.summary import ScrapeSummary, merge_harvest
from linkedin_mcp.scrape.urls import people_search_url

logger = logging.getLogger(__name__)

__all__ = ["DEFAULT_LIMIT", "PEOPLE_SEARCH_ACTION", "PAGE_TIMEOUT_MS", "run_people_search"]

PEOPLE_SEARCH_ACTION = "profile_search"
"""Budget a page of People search spends. Configured at 50 a day."""

DEFAULT_LIMIT = 100
PAGE_TIMEOUT_MS = 30000


async def run_people_search(
    page: Any,
    conn: sqlite3.Connection,
    account_id: int,
    filters: PeopleSearchFilters,
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
    """Extract filtered People search results and store them as leads.

    Args:
        page: Playwright page already signed in to LinkedIn.
        conn: Open connection to the MCP database.
        account_id: Account the run belongs to.
        filters: Validated People search filters.
        limit: How many new people this run wants, up to the platform ceiling.
        cursor: Resume point from a previous run.
        humanizer: Pacing. Defaults to the process-wide humanizer.
        guard: Safety gate. Defaults to `guard_action`.
        record: Audit writer. Defaults to `log_action`.
        clock: Decision time source, injected so a runner stays deterministic.
        harvest: Store results through the lead store. Off for a dry run.
        run_id: Existing `harvest_runs` row to continue, if resuming one.
        timeout: Navigation timeout in milliseconds.
    """
    pacer = humanizer or get_humanizer()
    tick = clock or (lambda: datetime.now(timezone.utc))
    started = tick()

    async def fetch(target: Any, page_number: int) -> None:
        url = people_search_url(filters, page_number)
        # A direct load is correct here. CORE-04 routes /in/ pages through the
        # search bar because LinkedIn caps direct profile loads at about 40 a
        # day. Search result pages carry no such cap, and the query string is
        # the only place the full filter set can be expressed.
        await target.goto(url, wait_until="domcontentloaded", timeout=timeout)

    totals = HarvestSummary()

    async def on_page(people: list[PersonResult], page_number: int) -> None:
        nonlocal totals
        if not harvest:
            return
        page_summary = harvest_people(conn, account_id, people, fetched_at=tick())
        totals = merge_harvest(totals, page_summary)
        logger.debug(
            "People search page %d stored %d new and %d updated lead(s)",
            page_number,
            page_summary.created,
            page_summary.updated,
        )

    if harvest and run_id is None:
        run_id = start_harvest_run(
            conn,
            account_id,
            SOURCE_PEOPLE_SEARCH,
            filters.describe(),
            cursor=cursor,
            started_at=started,
        )

    run = await paginate(
        page,
        action_type=PEOPLE_SEARCH_ACTION,
        account_id=account_id,
        fetch=fetch,
        extract=extract_people,
        key=lambda person: person.dedupe_key,
        limit=limit,
        cursor=cursor,
        humanizer=pacer,
        guard=guard,
        record=record,
        clock=tick,
        on_page=on_page,
        detail={"source": SOURCE_PEOPLE_SEARCH},
    )

    stale = (
        stale_lead_ids(conn, account_id, totals.lead_ids, now=tick())
        if harvest
        else ()
    )

    if harvest and run_id is not None:
        finish_harvest_run(
            conn,
            run_id,
            found=run.results_seen,
            new=totals.created,
            cursor=run.cursor,
            params=filters.describe(),
            finished_at=tick(),
        )

    return ScrapeSummary(
        source=SOURCE_PEOPLE_SEARCH,
        action_type=PEOPLE_SEARCH_ACTION,
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
