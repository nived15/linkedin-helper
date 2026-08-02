"""Post likers and commenters, the highest-intent free lead source there is.

Someone who reacted to a post about enterprise Copilot rollouts three days ago
has told you what they care about and when. A People search result has told you
a job title. That difference is why this module gets the most care in SCRAPE-04
and why the two lists it reads are treated as one surface with two phases.

Two phases, one post
--------------------
Reactions live behind a modal that opens off the reaction count. Comments live
inline underneath the post. Both are lazily loaded columns of profile links, so
both go through :func:`linkedin_mcp.scrape.sources.run_people_list_harvest` and
therefore through `paginate`: the safety gate is asked before every slice, the
humanizer paces every reveal, and a checkpoint is detected and recorded by the
navigation layer rather than by a marker list kept here.

A person who both reacted and commented is one lead, not two. Within a run the
comments phase starts from the reactions phase's seen keys, so the duplicate is
counted as a duplicate. Across runs and across sources DB-03 does the same job
against the database, so the same person found on a post today and at an event
tomorrow still resolves onto one row.

Budget
------
`post_read`, configured at 100 a day. This surface is a post permalink and its
social detail, not a search route, so metering it as a search would misreport
what the account actually did. It is deliberately not a new action type: adding
one would have created a budget nothing else competes for, which is how an
account ends up taking 50 searches and 100 engager slices in the same day.

Known gaps
----------
Whether LinkedIn's reactions modal paginates by button, by container scroll or
by both has not been checked against a live session. Both gestures are tried and
neither is required, so the wrong guess costs a slice rather than the run. The
comment list's load-more button is likewise a hypothesis; when it is missing the
run stops on no-new-results, which reports honestly as a short harvest rather
than as a failure.

Resuming a combined run replays the reactions phase, because the returned cursor
belongs to the phase that stopped and there is no address for "the modal, seven
reveals in". :func:`run_post_reaction_harvest` and
:func:`run_post_comment_harvest` are the single-phase entry points, and each of
those resumes cleanly from its own cursor.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from linkedin_mcp.browser.humanize import Humanizer, get_humanizer
from linkedin_mcp.scrape.paginate import GuardFn, RecordFn, SearchCursor
from linkedin_mcp.scrape.records import activity_id_from
from linkedin_mcp.scrape.runs import finish_harvest_run, start_harvest_run
from linkedin_mcp.scrape.sources import (
    DEFAULT_LIMIT,
    PAGE_TIMEOUT_MS,
    POST_ENGAGERS_ACTION,
    SOURCE_POST_COMMENTS,
    SOURCE_POST_ENGAGERS,
    SOURCE_POST_REACTIONS,
    PeopleListSurface,
    combine_summaries,
    post_permalink,
    run_people_list_harvest,
)
from linkedin_mcp.scrape.summary import ScrapeSummary

logger = logging.getLogger(__name__)

__all__ = [
    "COMMENTS_SURFACE",
    "DEFAULT_LIMIT",
    "PAGE_TIMEOUT_MS",
    "POST_ENGAGERS_ACTION",
    "REACTIONS_SURFACE",
    "PostEngagement",
    "run_post_comment_harvest",
    "run_post_engager_harvest",
    "run_post_reaction_harvest",
]


class PostEngagement(str, Enum):
    """Which of a post's engagers a run wants."""

    REACTIONS = "reactions"
    COMMENTS = "comments"
    ALL = "all"


REACTIONS_SURFACE = PeopleListSurface(
    source=SOURCE_POST_REACTIONS,
    action_type=POST_ENGAGERS_ACTION,
    item="post_reactor_item",
    link="post_reactor_profile_link",
    name="post_reactor_name",
    headline="post_reactor_headline",
    distance="post_reactor_distance",
    avatar="post_reactor_avatar",
    load_more="post_reactions_load_more",
    opener="post_reactions_trigger",
)
"""The reactions modal. `opener` is the count button that puts it on screen."""

COMMENTS_SURFACE = PeopleListSurface(
    source=SOURCE_POST_COMMENTS,
    action_type=POST_ENGAGERS_ACTION,
    item="post_comment_item",
    link="post_comment_author_link",
    name="post_comment_author_name",
    headline="post_comment_author_headline",
    avatar="post_comment_author_avatar",
    load_more="post_comments_load_more",
)
"""The inline comment list. No opener: comments are already on the post page."""


def _post_params(permalink: str, engagement: PostEngagement) -> dict[str, Any]:
    return {
        "post_url": permalink,
        "activity_id": activity_id_from(permalink),
        "engagement": engagement.value,
    }


async def run_post_reaction_harvest(
    page: Any,
    conn: sqlite3.Connection,
    account_id: int,
    post: str | int,
    *,
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
    """Harvest everyone who reacted to a post.

    Args:
        page: Playwright page already signed in to LinkedIn.
        conn: Open connection to the MCP database.
        account_id: Account the run belongs to.
        post: Post URL, `urn:li:activity:...` URN, or bare activity id.
        limit: How many new reactors this run wants.
        cursor: Resume point from a previous run.
        humanizer: Pacing. Defaults to the process-wide humanizer.
        guard: Safety gate. Defaults to `guard_action`.
        record: Audit writer. Defaults to `log_action`.
        clock: Decision time source, injected so a runner stays deterministic.
        harvest: Store reactors through the lead store. Off for a dry run.
        run_id: Existing `harvest_runs` row this run belongs to.
        manage_run: Open and close the run row here.
        timeout: Navigation timeout in milliseconds.
    """
    permalink = post_permalink(post)
    return await run_people_list_harvest(
        page,
        conn,
        account_id,
        REACTIONS_SURFACE,
        permalink,
        params=_post_params(permalink, PostEngagement.REACTIONS),
        limit=limit,
        cursor=cursor,
        humanizer=humanizer,
        guard=guard,
        record=record,
        clock=clock,
        harvest=harvest,
        run_id=run_id,
        manage_run=manage_run,
        timeout=timeout,
    )


async def run_post_comment_harvest(
    page: Any,
    conn: sqlite3.Connection,
    account_id: int,
    post: str | int,
    *,
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
    """Harvest everyone who commented on a post.

    Arguments match :func:`run_post_reaction_harvest`.
    """
    permalink = post_permalink(post)
    return await run_people_list_harvest(
        page,
        conn,
        account_id,
        COMMENTS_SURFACE,
        permalink,
        params=_post_params(permalink, PostEngagement.COMMENTS),
        limit=limit,
        cursor=cursor,
        humanizer=humanizer,
        guard=guard,
        record=record,
        clock=clock,
        harvest=harvest,
        run_id=run_id,
        manage_run=manage_run,
        timeout=timeout,
    )


async def run_post_engager_harvest(
    page: Any,
    conn: sqlite3.Connection,
    account_id: int,
    post: str | int,
    *,
    engagement: PostEngagement | str = PostEngagement.ALL,
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
    """Harvest a post's engagers, reactions first and comments second.

    `limit` is the budget for the post as a whole, not per phase, so a post with
    600 reactions and 40 comments asked for 100 engagers does not quietly return
    200. Whatever the reactions phase leaves unspent is what the comments phase
    gets, and a reactions phase that used the lot means the comments phase never
    runs.

    A gate refusal in the reactions phase stops the run there. Carrying on into
    the comments phase would ask the gate again for a budget it has just said is
    spent, which is exactly the behaviour the per-fetch gate exists to prevent.

    Args:
        page: Playwright page already signed in to LinkedIn.
        conn: Open connection to the MCP database.
        account_id: Account the run belongs to.
        post: Post URL, `urn:li:activity:...` URN, or bare activity id.
        engagement: Reactions, comments, or both.
        limit: How many new engagers this run wants, across both phases.
        cursor: Resume point, applied to the first phase this run walks.
        humanizer: Pacing. Defaults to the process-wide humanizer.
        guard: Safety gate. Defaults to `guard_action`.
        record: Audit writer. Defaults to `log_action`.
        clock: Decision time source, injected so a runner stays deterministic.
        harvest: Store engagers through the lead store. Off for a dry run.
        run_id: Existing `harvest_runs` row to continue, if resuming one.
        timeout: Navigation timeout in milliseconds.
    """
    wanted = PostEngagement(engagement)
    if limit < 1:
        raise ValueError(f"limit must be at least 1, got {limit}")

    permalink = post_permalink(post)
    tick = clock or (lambda: datetime.now(timezone.utc))
    pacer = humanizer or get_humanizer()

    if wanted is PostEngagement.REACTIONS:
        return await run_post_reaction_harvest(
            page, conn, account_id, permalink,
            limit=limit, cursor=cursor, humanizer=pacer, guard=guard,
            record=record, clock=tick, harvest=harvest, run_id=run_id,
            timeout=timeout,
        )
    if wanted is PostEngagement.COMMENTS:
        return await run_post_comment_harvest(
            page, conn, account_id, permalink,
            limit=limit, cursor=cursor, humanizer=pacer, guard=guard,
            record=record, clock=tick, harvest=harvest, run_id=run_id,
            timeout=timeout,
        )

    params = _post_params(permalink, wanted)
    if harvest and run_id is None:
        run_id = start_harvest_run(
            conn,
            account_id,
            SOURCE_POST_ENGAGERS,
            params,
            cursor=cursor,
            started_at=tick(),
        )

    reactions = await run_post_reaction_harvest(
        page, conn, account_id, permalink,
        limit=limit, cursor=cursor, humanizer=pacer, guard=guard, record=record,
        clock=tick, harvest=harvest, run_id=run_id, manage_run=False,
        timeout=timeout,
    )

    comments: ScrapeSummary | None = None
    remaining = limit - reactions.results_new
    if reactions.refused:
        logger.info(
            "Skipping the comments phase for %s: the gate stopped the reactions phase",
            permalink,
        )
    elif remaining < 1:
        logger.debug("Reactions filled the limit for %s; no comments phase", permalink)
    else:
        # Seeding the comments phase with the reactions phase's keys is what
        # makes a person who both reacted and commented one result rather than
        # two. The collected count travels with it so the platform ceiling is
        # measured against the post, not against each list separately.
        comments = await run_post_comment_harvest(
            page, conn, account_id, permalink,
            limit=remaining,
            cursor=SearchCursor(
                page=1,
                collected=reactions.cursor.collected,
                seen_keys=reactions.cursor.seen_keys,
            ),
            humanizer=pacer, guard=guard, record=record, clock=tick,
            harvest=harvest, run_id=run_id, manage_run=False, timeout=timeout,
        )

    summary = combine_summaries(
        SOURCE_POST_ENGAGERS, reactions, comments, run_id=run_id
    )

    if harvest and run_id is not None:
        finish_harvest_run(
            conn,
            run_id,
            found=summary.results_seen,
            new=summary.leads_created,
            cursor=summary.cursor,
            params=params,
            finished_at=tick(),
        )

    return summary
