"""Group member extraction.

A group member list is a people list that pages by loading more rather than by
a page number, so the paginator's page counter counts load-more steps here
instead of URLs. Everything else is the same: the gate is asked before every
step, the humanizer paces them, and members are stored through the lead store.

Action type
-----------
This spends `profile_search` rather than an action type of its own. A group
member page is a paged list of profiles, which is the same thing a People search
returns and the same kind of load LinkedIn watches for. Giving it a separate
budget would let a run take 50 People searches and 50 group pages in a day while
still claiming it stayed inside the search cap.

Resuming
--------
Resuming a scroll surface costs more than resuming a URL surface. There is no
address for step 7, so a resumed run reloads the member list and replays the
load-more steps it had already taken. That is slower than a page parameter, and
it is the honest cost of the surface rather than a bug.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from linkedin_mcp.browser.humanize import Humanizer, get_humanizer
from linkedin_mcp.leads import HarvestSummary
from linkedin_mcp.scrape.extract import extract_group_members, query_first
from linkedin_mcp.scrape.harvest import harvest_people, stale_lead_ids
from linkedin_mcp.scrape.paginate import (
    SCROLL_DISTANCE,
    GuardFn,
    RecordFn,
    SearchCursor,
    paginate,
)
from linkedin_mcp.scrape.records import PersonResult
from linkedin_mcp.scrape.runs import (
    SOURCE_GROUP_MEMBERS,
    finish_harvest_run,
    start_harvest_run,
)
from linkedin_mcp.scrape.summary import ScrapeSummary, merge_harvest
from linkedin_mcp.scrape.urls import group_id_from, group_members_url

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_LIMIT",
    "GROUP_MEMBERS_ACTION",
    "PAGE_TIMEOUT_MS",
    "run_group_member_extraction",
]

GROUP_MEMBERS_ACTION = "profile_search"
"""Group member pages spend the same budget as a People search."""

DEFAULT_LIMIT = 100
PAGE_TIMEOUT_MS = 30000


async def run_group_member_extraction(
    page: Any,
    conn: sqlite3.Connection,
    account_id: int,
    group: str | int,
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
    """Extract a LinkedIn group's member list and store the members as leads.

    Args:
        page: Playwright page already signed in to LinkedIn.
        conn: Open connection to the MCP database.
        account_id: Account the run belongs to.
        group: Group id or group URL.
        limit: How many new members this run wants.
        cursor: Resume point from a previous run.
        humanizer: Pacing. Defaults to the process-wide humanizer.
        guard: Safety gate. Defaults to `guard_action`.
        record: Audit writer. Defaults to `log_action`.
        clock: Decision time source, injected so a runner stays deterministic.
        harvest: Store members through the lead store. Off for a dry run.
        run_id: Existing `harvest_runs` row to continue, if resuming one.
        timeout: Navigation timeout in milliseconds.
    """
    group_id = group_id_from(group)
    url = group_members_url(group_id)
    pacer = humanizer or get_humanizer()
    tick = clock or (lambda: datetime.now(timezone.utc))
    started = tick()
    loaded = False

    async def fetch(target: Any, page_number: int) -> None:
        nonlocal loaded
        if not loaded:
            # A group member list is a normal application route, not a /in/
            # page, so CORE-04's direct profile load cap does not apply.
            await target.goto(url, wait_until="domcontentloaded", timeout=timeout)
            loaded = True
            for _ in range(page_number - 1):
                await _load_more(target, pacer)
            return
        await _load_more(target, pacer)

    totals = HarvestSummary()

    async def on_page(members: list[PersonResult], page_number: int) -> None:
        nonlocal totals
        if not harvest:
            return
        page_summary = harvest_people(conn, account_id, members, fetched_at=tick())
        totals = merge_harvest(totals, page_summary)
        logger.debug(
            "Group %s step %d stored %d new member(s)",
            group_id,
            page_number,
            page_summary.created,
        )

    params = {"group_id": group_id, "url": url}

    if harvest and run_id is None:
        run_id = start_harvest_run(
            conn,
            account_id,
            SOURCE_GROUP_MEMBERS,
            params,
            cursor=cursor,
            started_at=started,
        )

    run = await paginate(
        page,
        action_type=GROUP_MEMBERS_ACTION,
        account_id=account_id,
        fetch=fetch,
        extract=extract_group_members,
        key=lambda member: member.dedupe_key,
        limit=limit,
        cursor=cursor,
        humanizer=pacer,
        guard=guard,
        record=record,
        clock=tick,
        on_page=on_page,
        scroll_before_extract=False,
        detail={"source": SOURCE_GROUP_MEMBERS, "group_id": group_id},
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
            params=params,
            finished_at=tick(),
        )

    return ScrapeSummary(
        source=SOURCE_GROUP_MEMBERS,
        action_type=GROUP_MEMBERS_ACTION,
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


async def _load_more(page: Any, pacer: Humanizer) -> None:
    """Advance a group member list by one step.

    LinkedIn shows a load-more button on some group lists and lazy loads on
    scroll on others, so both are tried. When neither reveals anything new the
    paginator stops on its no-new-results condition.
    """
    await pacer.scroll(page, SCROLL_DISTANCE)
    button = await query_first(page, "group_member_load_more")
    if button is None:
        return
    try:
        await pacer.click(button)
    except Exception as error:  # noqa: BLE001 - a stale button is not fatal
        logger.debug("Group load-more button could not be clicked: %s", error)
