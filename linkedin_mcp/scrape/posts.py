"""Post search extraction, filtered and paginated.

A post search returns posts, and posts are not leads. The authors are, so a run
stores every author it can identify through the lead store and returns the posts
themselves in the summary. No new table is added for the posts: DB-01 has no
post table, and inventing one here would commit the schema to a shape SCRAPE-02
and the engagement work have not agreed on yet. The permalink and activity id
travel in the summary, which is everything a caller needs to open a post again.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from linkedin_mcp.browser.humanize import Humanizer, get_humanizer
from linkedin_mcp.leads import HarvestSummary
from linkedin_mcp.scrape.extract import extract_posts
from linkedin_mcp.scrape.filters import PostSearchFilters
from linkedin_mcp.scrape.harvest import harvest_people, stale_lead_ids
from linkedin_mcp.scrape.paginate import (
    GuardFn,
    RecordFn,
    SearchCursor,
    paginate,
)
from linkedin_mcp.scrape.records import PostResult
from linkedin_mcp.scrape.runs import (
    SOURCE_POST_SEARCH,
    finish_harvest_run,
    start_harvest_run,
)
from linkedin_mcp.scrape.summary import ScrapeSummary, merge_harvest
from linkedin_mcp.scrape.urls import post_search_url

logger = logging.getLogger(__name__)

__all__ = ["DEFAULT_LIMIT", "PAGE_TIMEOUT_MS", "POST_SEARCH_ACTION", "run_post_search"]

POST_SEARCH_ACTION = "post_search"
"""Budget a page of content search spends. Configured at 50 a day."""

DEFAULT_LIMIT = 50
PAGE_TIMEOUT_MS = 30000


async def run_post_search(
    page: Any,
    conn: sqlite3.Connection,
    account_id: int,
    filters: PostSearchFilters,
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
    """Extract filtered post search results and store their authors as leads.

    Args:
        page: Playwright page already signed in to LinkedIn.
        conn: Open connection to the MCP database.
        account_id: Account the run belongs to.
        filters: Validated content search filters.
        limit: How many new posts this run wants, up to the platform ceiling.
        cursor: Resume point from a previous run.
        humanizer: Pacing. Defaults to the process-wide humanizer.
        guard: Safety gate. Defaults to `guard_action`.
        record: Audit writer. Defaults to `log_action`.
        clock: Decision time source, injected so a runner stays deterministic.
        harvest: Store post authors through the lead store. Off for a dry run.
        run_id: Existing `harvest_runs` row to continue, if resuming one.
        timeout: Navigation timeout in milliseconds.
    """
    pacer = humanizer or get_humanizer()
    tick = clock or (lambda: datetime.now(timezone.utc))
    started = tick()

    async def fetch(target: Any, page_number: int) -> None:
        url = post_search_url(filters, page_number)
        # Same reasoning as the People search: a results page is not a /in/
        # page, so CORE-04's direct profile load cap does not apply to it.
        await target.goto(url, wait_until="domcontentloaded", timeout=timeout)

    totals = HarvestSummary()

    async def on_page(posts: list[PostResult], page_number: int) -> None:
        nonlocal totals
        if not harvest:
            return
        authors = [post.author for post in posts if post.author is not None]
        if not authors:
            return
        page_summary = harvest_people(conn, account_id, authors, fetched_at=tick())
        totals = merge_harvest(totals, page_summary)
        logger.debug(
            "Post search page %d stored %d new author(s)",
            page_number,
            page_summary.created,
        )

    if harvest and run_id is None:
        run_id = start_harvest_run(
            conn,
            account_id,
            SOURCE_POST_SEARCH,
            filters.describe(),
            cursor=cursor,
            started_at=started,
        )

    run = await paginate(
        page,
        action_type=POST_SEARCH_ACTION,
        account_id=account_id,
        fetch=fetch,
        extract=extract_posts,
        key=lambda post: post.dedupe_key,
        limit=limit,
        cursor=cursor,
        humanizer=pacer,
        guard=guard,
        record=record,
        clock=tick,
        on_page=on_page,
        detail={"source": SOURCE_POST_SEARCH},
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
        source=SOURCE_POST_SEARCH,
        action_type=POST_SEARCH_ACTION,
        stop_reason=run.stop_reason,
        cursor=run.cursor,
        pages_fetched=run.pages_fetched,
        results_seen=run.results_seen,
        results_new=len(run.results),
        duplicates_skipped=run.duplicates_skipped,
        posts=run.results,
        harvest=totals,
        gate_refusal=run.gate_refusal,
        harvest_run_id=run_id,
        stale_lead_ids=stale,
    )
