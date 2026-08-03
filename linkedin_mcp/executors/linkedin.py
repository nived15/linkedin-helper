"""The executors: the only code in this repository that touches LinkedIn's DOM.

Every body here used to be the body of an MCP tool in `linkedin_browser_mcp.py`.
Moving them is the substance of MCP-03 (#26). While an MCP tool could click
something, the daily cap was a number the model had to remember, and a cap the
model remembers is advisory. Now the tool writes a `jobs` row and the runner
decides: it claims the job, asks `SafetyGate`, runs the executor, and appends the
`actions_log` row. The safety guarantee stops depending on anyone's good manners.

What an executor may assume
---------------------------
By the time one of these runs, `Worker._run_ad_hoc_job` has already leased the
job, asked the gate and been told yes. So an executor never re-gates, never
counts anything and never decides whether an action is allowed. It navigates,
reads the page, and reports.

What it must not do
-------------------
It must not hold a write lock across a browser call. The runner deliberately
commits its claim before executing, and `ctx.conn` arrives with no transaction
open, so the only write here is
:func:`~linkedin_mcp.executors.contract.record_job_result`, which opens and
closes its own short transaction after the page work is finished.

It must not detect a challenge for itself either.
`linkedin_mcp.executors.support.check_page_is_ours` delegates to
`linkedin_mcp.safety.assert_page_clear`, which flips the account state and writes
the `safety_events` row before it raises. A second copy that raised without
flipping state would leave the account looking healthy to the gate while
LinkedIn had already stopped it, and would pass its own tests while doing so.

Why `action_type` is not enough to route on
-------------------------------------------
MCP-02's harvests are also campaign-less jobs and a People search harvest also
spends `profile_search`. The registry is keyed by `action_type`, so both arrive
at the same executor. Each one therefore dispatches on the payload's `action`
key first and refuses, loudly and by name, anything that is not a registered
one-off action.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any
from urllib.parse import quote

from linkedin_mcp.browser.humanize import (
    dwell_and_click,
    pace,
    scroll_page,
    settle,
    type_text,
)
from linkedin_mcp.browser.navigate import SessionExpiredError, goto_profile
from linkedin_mcp.browser.selectors import selector_fallbacks, selector_payload
from linkedin_mcp.executors.contract import (
    ACTION_KEY,
    ADHOC_ACTIONS,
    DETAIL_SHAPE,
    SUMMARY_SHAPE,
    adhoc_action,
    record_job_result,
)
from linkedin_mcp.executors.support import (
    acting_account_id,
    check_page_is_ours,
    click_selector_fallback,
    fill_selector_fallback,
    query_selector_fallback,
    session_expired_result,
    wait_for_selector_fallback,
)
from linkedin_mcp.worker.actions import ActionContext, ActionResult

logger = logging.getLogger(__name__)

__all__ = [
    "ACTION_HANDLERS",
    "LEAD_ACTIONS",
    "NO_BROWSER",
    "build_executors",
    "linkedin_executors",
]

NO_BROWSER = (
    "this action needs a browser and the worker supplied none; start the daemon "
    "without --no-browser"
)
"""What an executor says when it was handed no browser.

Loudly rather than quietly. A worker that pretended to send an invitation it
never sent would be far worse than one that reported it could not.
"""

MAX_FEED_SCROLLS = 20
MAX_POST_SEARCH_SCROLLS = 10


# ----------------------------------------------------------------------
# shared page plumbing
# ----------------------------------------------------------------------


async def _open(ctx: ActionContext, url: str | None = None):
    """Return a page from the worker's browser, or raise so the runner logs it."""
    if ctx.browser is None:
        raise RuntimeError(NO_BROWSER)
    return await ctx.browser.new_page(url)


async def _save(ctx: ActionContext, page: Any) -> None:
    """Persist cookies after a successful action, exactly as the tools did."""
    if ctx.browser is not None:
        await ctx.browser.save_session(page)


async def _halted(page: Any) -> dict | None:
    """Return the halt result when LinkedIn served an interstitial.

    Called with no `action_type` on purpose. `assert_page_clear` would append
    its own refused row to `actions_log` if given one, and in the worker lane
    the runner already writes exactly one row per job. Two rows for one refusal
    would double-count against the very budgets the gate reads.
    """
    return await check_page_is_ours(page)


def _finish(ctx: ActionContext, result: Any, **detail: Any) -> ActionResult:
    """Write the answer back onto the job and report success.

    The caller of an action tool got a job id, not a page of results, so the
    results have to be readable later. `action_status` reads them from here.
    """
    try:
        record_job_result(ctx.conn, ctx.job.id, result)
    except Exception as exc:  # noqa: BLE001 - a lost result is not a lost action
        # The action itself happened. Failing the job now would retry it and
        # send the invitation twice, which is far worse than a missing result.
        logger.error("could not record the result of job %s: %s", ctx.job.id, exc)
        return ActionResult.ok(**detail, result_error=str(exc))
    return ActionResult.ok(**detail)


# ----------------------------------------------------------------------
# the actions
# ----------------------------------------------------------------------


async def connection_request(ctx: ActionContext) -> ActionResult:
    """Send one connection request. Was `send_connection_request`."""
    profile_url = str(ctx.payload.get("profile_url") or "").strip()
    note = ctx.payload.get("note")
    direct = bool(ctx.payload.get("direct", False))

    page = await _open(ctx)
    try:
        await goto_profile(
            page, profile_url, direct=direct, account_id=acting_account_id()
        )
    except SessionExpiredError as expired:
        return ActionResult.failed(
            "the LinkedIn session expired", **session_expired_result(expired)
        )

    await wait_for_selector_fallback(page, "profile_top_card", timeout=10000)

    connect_button = None
    try:
        connect_button = await wait_for_selector_fallback(
            page, "connect_button", timeout=5000
        )
    except Exception:  # noqa: BLE001 - Connect may be hidden under "More"
        try:
            more_button = await wait_for_selector_fallback(
                page, "more_actions_button", timeout=3000
            )
            await dwell_and_click(more_button)
            connect_button = await wait_for_selector_fallback(
                page, "connect_button_more_menu", timeout=3000
            )
        except Exception:  # noqa: BLE001 - there is genuinely no Connect button
            pass

    if not connect_button:
        return ActionResult.skipped(
            "no_connect_button",
            profile_url=profile_url,
            message=(
                "Connect button not found. Profile may already be connected or "
                "pending."
            ),
        )

    await dwell_and_click(connect_button)
    await pace(1.5)

    if note:
        try:
            add_note_button = await wait_for_selector_fallback(
                page, "connect_add_note_button", timeout=3000
            )
            await dwell_and_click(add_note_button)
            note_field = await wait_for_selector_fallback(
                page, "connect_note_field", timeout=3000
            )
            await type_text(note_field, note, clear=True)
        except Exception as note_error:  # noqa: BLE001 - send without the note
            logger.info("Could not add note: %s. Sending without note.", note_error)

    send_button = await wait_for_selector_fallback(
        page, "connect_send_button", timeout=5000
    )
    await dwell_and_click(send_button)
    await pace(2.0)

    profile_name = await page.evaluate(
        """(selectors) => {
            const el = document.querySelector(selectors.profile_name.join(', '));
            return el ? el.innerText.trim() : 'Unknown';
        }""",
        selector_payload("profile_name"),
    )
    await _save(ctx, page)

    return _finish(
        ctx,
        {
            "status": "success",
            "message": f"Connection request sent to {profile_name}",
            "profile_url": profile_url,
            "profile_name": profile_name,
            "note_included": note is not None,
        },
        target=profile_url,
        profile_name=profile_name,
        note_included=note is not None,
    )


async def profile_view(ctx: ActionContext) -> ActionResult:
    """Read one profile. Was `view_linkedin_profile` and `get_linkedin_profile`.

    `direct` is read off the action, never off the payload. LinkedIn caps a
    direct URL load far harder than a view reached through the site, the two
    have separate ceilings, and a payload flag would let a job buy the cheap
    budget and take the expensive route.
    """
    action = adhoc_action(str(ctx.payload.get(ACTION_KEY)))
    profile_url = str(ctx.payload.get("profile_url") or "").strip()
    shape = str(ctx.payload.get("shape") or DETAIL_SHAPE)

    page = await _open(ctx)
    try:
        await goto_profile(
            page,
            profile_url,
            direct=action.direct,
            account_id=acting_account_id(),
        )
    except SessionExpiredError as expired:
        return ActionResult.failed(
            "the LinkedIn session expired", **session_expired_result(expired)
        )

    await wait_for_selector_fallback(page, "profile_top_card", timeout=10000)
    profile = await (
        _read_profile_summary(page) if shape == SUMMARY_SHAPE else _read_profile_detail(page)
    )
    await _save(ctx, page)

    return _finish(
        ctx,
        {
            "status": "success",
            "profile": profile,
            "url": profile_url,
            "shape": shape,
        },
        target=profile_url,
        shape=shape,
        direct=action.direct,
    )


async def profile_search(ctx: ActionContext) -> ActionResult:
    """Search People. Was `search_linkedin_profiles`."""
    query = str(ctx.payload.get("query") or "").strip()
    count = int(ctx.payload.get("count") or 5)
    page_num = max(1, int(ctx.payload.get("page") or 1))
    start = (page_num - 1) * 10

    search_url = (
        f"https://www.linkedin.com/search/results/people/?keywords={quote(query)}&start={start}"
    )
    # Navigate via feed first so LinkedIn sees us arriving through the site
    # rather than jumping directly to a search URL, which triggers bot checks.
    page = await _open(ctx, "https://www.linkedin.com/feed/")
    halted = await _halted(page)
    if halted:
        return ActionResult.failed(halted["message"], **halted)
    await settle()
    await page.goto(search_url, wait_until="domcontentloaded", timeout=60000)
    halted = await _halted(page)
    if halted:
        return ActionResult.failed(halted["message"], **halted)

    await wait_for_selector_fallback(page, "search_result_container", timeout=10000)
    profiles = await page.evaluate(
        """({ count, selectors }) => {
            const results = [];
            const profileCards = document.querySelectorAll(selectors.search_result_container.join(', '));

            for (let i = 0; i < Math.min(profileCards.length, count); i++) {
                const card = profileCards[i];
                try {
                    const profile = {
                        name: card.querySelector(selectors.search_result_title_link.join(', '))?.innerText?.trim() || 'Unknown',
                        headline: card.querySelector(selectors.search_result_headline.join(', '))?.innerText?.trim() || '',
                        location: card.querySelector(selectors.search_result_location.join(', '))?.innerText?.trim() || '',
                        profileUrl: card.querySelector(selectors.search_result_profile_link.join(', '))?.href || '',
                        connectionDegree: card.querySelector(selectors.search_result_distance.join(', '))?.innerText?.trim() || '',
                        snippet: card.querySelector(selectors.search_result_snippet.join(', '))?.innerText?.trim() || ''
                    };
                    results.push(profile);
                } catch (e) {
                    console.error("Error extracting profile", e);
                }
            }
            return results;
        }""",
        {
            "count": count,
            "selectors": selector_payload(
                "search_result_container",
                "search_result_distance",
                "search_result_headline",
                "search_result_location",
                "search_result_profile_link",
                "search_result_snippet",
                "search_result_title_link",
            ),
        },
    )
    await _save(ctx, page)

    return _finish(
        ctx,
        {
            "status": "success",
            "profiles": profiles,
            "count": len(profiles),
            "query": query,
            "page": page_num,
            "next_page": page_num + 1,
        },
        target=query,
        count=len(profiles),
    )


async def feed_browse(ctx: ActionContext) -> ActionResult:
    """Read the feed. Was `browse_linkedin_feed`."""
    count = int(ctx.payload.get("count") or 5)
    posts: list[Any] = []
    errors: list[str] = []

    page = await _open(ctx, "https://www.linkedin.com/feed/")
    halted = await _halted(page)
    if halted:
        return ActionResult.failed(halted["message"], **halted)

    await settle()
    for index in range(min(count, MAX_FEED_SCROLLS)):
        try:
            post_selector = None
            for selector in selector_fallbacks("feed_post_container"):
                try:
                    await page.wait_for_selector(selector, timeout=4000)
                    post_selector = selector
                    break
                except Exception:  # noqa: BLE001 - try the next fallback
                    continue

            if not post_selector:
                errors.append(
                    f"Error during scroll {index}: No feed post elements found on page"
                )
                await scroll_page(page, 800)
                continue

            new_posts = await page.evaluate(_FEED_EXTRACT_JS, selector_payload(
                "feed_post_author_headline",
                "feed_post_author_link",
                "feed_post_author_name",
                "feed_post_comments",
                "feed_post_container",
                "feed_post_content",
                "feed_post_likes",
                "feed_post_share_link",
                "feed_post_timestamp",
            ))

            for post in new_posts:
                if post not in posts:
                    posts.append(post)
            if len(posts) >= count:
                break
            await scroll_page(page, 800)
        except Exception as scroll_error:  # noqa: BLE001 - one bad scroll is not fatal
            errors.append(f"Error during scroll {index}: {scroll_error}")
            continue

    await _save(ctx, page)
    return _finish(
        ctx,
        {
            "status": "success",
            "posts": posts[:count],
            "count": len(posts[:count]),
            "errors": errors or None,
        },
        count=len(posts[:count]),
        errors=len(errors),
    )


async def post_search(ctx: ActionContext) -> ActionResult:
    """Search posts. Was `search_linkedin_posts`."""
    query = str(ctx.payload.get("query") or "").strip()
    count = int(ctx.payload.get("count") or 10)
    sort_by = str(ctx.payload.get("sort_by") or "relevance")

    keywords = quote(query)
    search_url = f"https://www.linkedin.com/search/results/content/?keywords={keywords}"
    if sort_by == "date_posted":
        search_url += "&sortBy=date_posted"

    page = await _open(ctx, search_url)
    halted = await _halted(page)
    if halted:
        return ActionResult.failed(halted["message"], **halted)

    # LinkedIn's React app takes about ten seconds to hydrate search results.
    await pace(10.0)

    collected: list[dict] = []
    seen_keys: list[str] = []
    scroll_attempts = 0

    while len(collected) < count and scroll_attempts < MAX_POST_SEARCH_SCROLLS:
        new_posts = await page.evaluate(_POST_SEARCH_EXTRACT_JS, {
            "alreadySeen": seen_keys,
            "selectors": selector_payload(
                "search_posts_aria_elements",
                "search_posts_author_links",
                "search_posts_author_name_hidden",
                "search_posts_comments_count",
                "search_posts_feed_link",
                "search_posts_permalink",
                "search_posts_reactions_count",
                "search_posts_urn_container",
            ),
        })

        for post in new_posts:
            key = post["dedupeKey"]
            if key not in seen_keys:
                seen_keys.append(key)
                collected.append(post)

        if len(collected) >= count:
            break

        await page.evaluate(
            """(selectors) => {
                const main = document.querySelector(selectors.search_posts_scroll_main.join(', '));
                if (main) {
                    main.scrollTop = main.scrollHeight;
                } else {
                    window.scrollTo(0, document.body.scrollHeight);
                }
            }""",
            selector_payload("search_posts_scroll_main"),
        )
        await pace(3.0)
        scroll_attempts += 1

    posts = [
        {
            "post_number": index,
            "post_url": item["postUrl"],
            "author": item["author"] or "Unknown",
            "content": item["content"][:400]
            + ("..." if len(item["content"]) > 400 else ""),
            "timestamp": item["timestamp"],
            "likes": item["likes"],
            "comments": item["comments"],
        }
        for index, item in enumerate(collected[:count], start=1)
    ]
    await _save(ctx, page)

    return _finish(
        ctx,
        {"status": "success", "query": query, "count": len(posts), "posts": posts},
        target=query,
        count=len(posts),
    )


async def post_interaction(ctx: ActionContext) -> ActionResult:
    """Read, like, comment on or share one post. Was `interact_with_linkedin_post`.

    One body for four actions because they share a navigation and a page read,
    and four copies of that would drift. They stay four `action_type` values, so
    a like and a share still spend different budgets.
    """
    name = str(ctx.payload.get(ACTION_KEY))
    post_url = str(ctx.payload.get("post_url") or "").strip()
    comment = ctx.payload.get("comment")

    page = await _open(ctx, post_url)
    halted = await _halted(page)
    if halted:
        return ActionResult.failed(halted["message"], **halted)

    await wait_for_selector_fallback(page, "post_detail_container", timeout=10000)
    post_content = await page.evaluate(
        """(selectors) => {
            const post = document.querySelector(selectors.post_detail_container.join(', '));
            return {
                author: post?.querySelector(selectors.post_detail_author.join(', '))?.innerText?.trim() || 'Unknown',
                content: post?.querySelector(selectors.post_detail_content.join(', '))?.innerText?.trim() || '',
                engagementCount: post?.querySelector(selectors.post_detail_engagement.join(', '))?.innerText?.trim() || '0'
            };
        }""",
        selector_payload(
            "post_detail_author",
            "post_detail_container",
            "post_detail_content",
            "post_detail_engagement",
        ),
    )

    if name == "post_like":
        liked = await page.evaluate(
            """(selectors) => {
                const likeButton = document.querySelector(selectors.post_like_button.join(', '));
                if (!likeButton) return false;
                const isLiked = likeButton.getAttribute('aria-pressed') === 'true';
                if (!isLiked) {
                    likeButton.click();
                    return true;
                }
                return false;
            }""",
            selector_payload("post_like_button"),
        )
        result = {
            "status": "success",
            "action": "like",
            "performed": liked,
            "message": (
                "Successfully liked the post" if liked else "Post was already liked"
            ),
        }
    elif name == "post_comment":
        # The tool refuses an empty comment at enqueue time. If one reaches
        # here the payload was hand-edited, and posting an empty comment is
        # worse than refusing to.
        if not comment:
            return ActionResult.failed("post_comment carries no comment text")
        await click_selector_fallback(page, "post_comment_trigger")
        await fill_selector_fallback(page, "post_comment_editor", str(comment))
        await click_selector_fallback(page, "post_comment_submit")
        await pace(2.0)
        result = {
            "status": "success",
            "action": "comment",
            "message": "Comment posted successfully",
        }
    elif name == "post_share":
        await click_selector_fallback(page, "post_repost_button", timeout=5000)
        await pace(1.0)
        await click_selector_fallback(page, "post_repost_option", timeout=5000)
        await pace(2.0)
        result = {
            "status": "success",
            "action": "share",
            "message": "Post shared successfully",
        }
    else:
        result = {"status": "success", "action": "read", "post": post_content}

    await _save(ctx, page)
    return _finish(ctx, result, target=post_url, action=result["action"])


# ----------------------------------------------------------------------
# extraction the profile actions share
# ----------------------------------------------------------------------


async def _read_profile_summary(page: Any) -> Any:
    """The top card `get_linkedin_profile` returned, follower count and all."""
    return await page.evaluate(
        """(selectors) => {
            const getData = (selectorList) => {
                for (const selector of selectorList) {
                    const el = document.querySelector(selector);
                    if (el && el.innerText.trim()) return el.innerText.trim();
                }
                return null;
            };

            let followerCount = null;
            const followerElements = document.querySelectorAll(selectors.profile_follower_items.join(', '));
            for (const el of followerElements) {
                const text = el.innerText.trim().toLowerCase();
                if (text.includes('follower')) {
                    const match = text.match(/([0-9,]+)/);
                    if (match) followerCount = parseInt(match[1].replace(/,/g, ''));
                }
            }
            if (!followerCount) {
                const allSpans = document.querySelectorAll(selectors.profile_text_spans.join(', '));
                for (const span of allSpans) {
                    const text = span.innerText.trim().toLowerCase();
                    if (text.includes('follower')) {
                        const match = text.match(/([0-9,]+)/);
                        if (match) followerCount = parseInt(match[1].replace(/,/g, ''));
                        break;
                    }
                }
            }

            return {
                name: getData(selectors.profile_name),
                headline: getData(selectors.profile_headline),
                location: getData(selectors.profile_location),
                follower_count: followerCount,
                connection_count: (() => {
                    const elements = document.querySelectorAll(selectors.profile_connection_count.join(', '));
                    for (const el of elements) {
                        const text = el.innerText.trim();
                        const match = text.match(/([0-9,]+)/);
                        if (match && !text.toLowerCase().includes('follower')) return parseInt(match[1].replace(/,/g, ''));
                    }
                    return null;
                })(),
                about: getData(selectors.profile_about),
                profile_url: window.location.href
            };
        }""",
        selector_payload(
            "profile_about",
            "profile_connection_count",
            "profile_follower_items",
            "profile_headline",
            "profile_location",
            "profile_name",
            "profile_text_spans",
        ),
    )


async def _read_profile_detail(page: Any) -> Any:
    """The experience and education `view_linkedin_profile` returned."""
    return await page.evaluate(
        """(selectors) => {
            const getData = (selectorList, scope = document, property = 'innerText') => {
                for (const selector of selectorList) {
                    const element = scope.querySelector(selector);
                    if (element && element[property]?.trim()) return element[property].trim();
                }
                return null;
            };

            return {
                name: getData(selectors.profile_name),
                headline: getData(selectors.profile_headline),
                location: getData(selectors.profile_location),
                connectionDegree: getData(selectors.profile_connection_count),
                about: getData(selectors.profile_about),
                experience: Array.from(document.querySelectorAll(selectors.profile_experience_item.join(', ')))
                    .map(exp => ({
                        title: getData(selectors.profile_experience_title, exp) || '',
                        company: getData(selectors.profile_experience_company, exp) || '',
                        duration: getData(selectors.profile_experience_duration, exp) || ''
                    })),
                education: Array.from(document.querySelectorAll(selectors.profile_education_item.join(', ')))
                    .map(edu => ({
                        school: getData(selectors.profile_education_school, edu) || '',
                        degree: getData(selectors.profile_education_degree, edu) || '',
                        field: getData(selectors.profile_education_field, edu) || '',
                        dates: getData(selectors.profile_education_dates, edu) || ''
                    }))
            };
        }""",
        selector_payload(
            "profile_about",
            "profile_connection_count",
            "profile_education_dates",
            "profile_education_degree",
            "profile_education_field",
            "profile_education_item",
            "profile_education_school",
            "profile_experience_company",
            "profile_experience_duration",
            "profile_experience_item",
            "profile_experience_title",
            "profile_headline",
            "profile_location",
            "profile_name",
        ),
    )


_FEED_EXTRACT_JS = """(selectors) => {
    let postElements = [];
    for (const sel of selectors.feed_post_container) {
        const els = document.querySelectorAll(sel);
        if (els.length > 0) { postElements = Array.from(els); break; }
    }

    const getText = (el, ...sels) => {
        for (const s of sels) {
            const found = el.querySelector(s);
            if (found && found.innerText.trim()) return found.innerText.trim();
        }
        return '';
    };

    return postElements.map(post => {
        try {
            let postUrl = null;
            const urn = post.getAttribute('data-urn') || post.getAttribute('data-id') || '';
            if (urn.includes('activity')) {
                const activityId = urn.split(':').pop();
                postUrl = 'https://www.linkedin.com/feed/update/urn:li:activity:' + activityId;
            }
            if (!postUrl) {
                const shareLink = post.querySelector(selectors.feed_post_share_link.join(', '));
                if (shareLink) postUrl = shareLink.href.split('?')[0];
            }

            const authorLink = post.querySelector(
                selectors.feed_post_author_link.join(', ')
            );
            const authorProfileUrl = authorLink ? authorLink.href.split('?')[0] : null;

            const likesText = getText(post, ...selectors.feed_post_likes);
            const likesCount = parseInt((likesText || '0').replace(/[^0-9]/g, '')) || 0;

            let commentsCount = 0;
            for (const btn of post.querySelectorAll(
                selectors.feed_post_comments.join(', ')
            )) {
                const t = btn.innerText?.trim() || btn.getAttribute('aria-label') || '';
                const m = t.match(/([0-9]+)/);
                if (m) { commentsCount = parseInt(m[1]); break; }
            }

            return {
                author: getText(post, ...selectors.feed_post_author_name) || 'Unknown',
                author_headline: getText(post, ...selectors.feed_post_author_headline),
                author_profile_url: authorProfileUrl,
                content: getText(post, ...selectors.feed_post_content),
                timestamp: getText(post, ...selectors.feed_post_timestamp),
                post_url: postUrl,
                likes_count: likesCount,
                comments_count: commentsCount
            };
        } catch (e) {
            return null;
        }
    }).filter(p => p !== null && (p.content || p.author !== 'Unknown'));
}"""


_POST_SEARCH_EXTRACT_JS = """({ alreadySeen, selectors }) => {
    const results = [];
    const seenKeys = new Set(alreadySeen);

    const authorLinks = Array.from(document.querySelectorAll(selectors.search_posts_author_links.join(', ')));

    for (const aLink of authorLinks) {
        let container = aLink.parentElement;
        let depth = 0;
        while (container && depth < 25) {
            const text = (container.innerText || '').trim();
            if (text.length > 300 && text.length < 12000) break;
            depth++;
            container = container.parentElement;
        }
        if (!container) continue;

        const containerText = (container.innerText || '').trim();
        if (containerText.length < 100) continue;

        const dedupeKey = containerText.slice(0, 80);
        if (seenKeys.has(dedupeKey)) continue;
        seenKeys.add(dedupeKey);

        let author = '';
        const inLinks = Array.from(container.querySelectorAll(selectors.search_posts_author_links.join(', ')));
        for (const il of inLinks) {
            const hiddenSpan = il.querySelector(selectors.search_posts_author_name_hidden.join(', '));
            let raw = hiddenSpan ? hiddenSpan.textContent.trim() : (il.textContent || '').trim().replace(/\\s+/g, ' ');
            let candidate = raw.split('•')[0].trim().split('\\n')[0].trim();
            if (candidate.length >= 2 && candidate.length <= 60) {
                author = candidate;
                break;
            }
        }
        if (!author) {
            for (const line of containerText.split('\\n').map(l => l.trim()).filter(Boolean).slice(0, 8)) {
                if (line.length >= 3 && line.length <= 60 && !line.match(/^(Feed post|Follow|\\d|•|http)/i)) {
                    author = line;
                    break;
                }
            }
        }

        let postUrl = '';
        const feedLink = container.querySelector(selectors.search_posts_feed_link.join(', '));
        if (feedLink) {
            postUrl = feedLink.href.split('?')[0];
        }
        if (!postUrl) {
            const urnSelector = selectors.search_posts_urn_container.join(', ');
            let urnEl = container.closest(urnSelector)
                     || container.querySelector(urnSelector);
            if (!urnEl) {
                let p = container;
                for (let i = 0; i < 5 && p; i++) {
                    const urn = p.getAttribute && p.getAttribute('data-urn');
                    if (urn && urn.includes('urn:li:activity')) { urnEl = p; break; }
                    p = p.parentElement;
                }
            }
            if (urnEl) {
                const urn = urnEl.getAttribute('data-urn');
                postUrl = 'https://www.linkedin.com/feed/update/' + urn;
            }
        }
        if (!postUrl) {
            const postsLink = container.querySelector(selectors.search_posts_permalink.join(', '));
            if (postsLink) postUrl = postsLink.href.split('?')[0];
        }
        if (!postUrl) {
            const html = container.innerHTML || '';
            const urnMatch = html.match(/urn:li:activity:(\\d+)/);
            if (urnMatch) {
                postUrl = 'https://www.linkedin.com/feed/update/urn:li:activity:' + urnMatch[1];
            }
        }

        const timeMatch = containerText.match(/\\b(\\d+[smh]\\b|\\d+[dw]\\b|\\d+ (min|hour|day|week|month)s? ago|just now)/i);
        const timestamp = timeMatch ? timeMatch[0] : '';

        const allLines = containerText.split('\\n').map(l => l.trim()).filter(Boolean);
        const followIdx = allLines.findIndex(l => l.match(/^Follow$/i));
        const bodyStart = followIdx >= 0 ? followIdx + 1 : 4;
        const contentLines = allLines.slice(bodyStart)
            .filter(l => l.length > 15)
            .filter(l => !l.match(/^(Feed post|Like|Comment|Repost|Send|View my services|\\d+$)/i));
        const content = contentLines.join(' ').slice(0, 600);

        let likes = '';
        let comments = '';

        const ariaEls = Array.from(container.querySelectorAll(selectors.search_posts_aria_elements.join(', ')));
        for (const el of ariaEls) {
            const label = el.getAttribute('aria-label') || '';
            const lower = label.toLowerCase();
            if (/\\d/.test(label) && lower.includes('reaction') && !likes) {
                const m = label.match(/[\\d,]+/);
                if (m) likes = m[0].replace(/,/g, '') + ' reactions';
            } else if (/\\d/.test(label) && lower.includes('comment') && !comments) {
                const m = label.match(/[\\d,]+/);
                if (m) comments = m[0].replace(/,/g, '') + ' comments';
            }
        }

        if (!likes) {
            const rxEl = container.querySelector(selectors.search_posts_reactions_count.join(', '));
            if (rxEl) {
                const txt = (rxEl.innerText || '').trim();
                if (/^[\\d,]+$/.test(txt)) likes = txt.replace(/,/g, '') + ' reactions';
            }
        }
        if (!comments) {
            const cmEl = container.querySelector(selectors.search_posts_comments_count.join(', '));
            if (cmEl) {
                const txt = (cmEl.innerText || '').trim();
                if (/^[\\d,]+$/.test(txt)) comments = txt.replace(/,/g, '') + ' comments';
            }
        }

        if (!likes) {
            const rxMatch = containerText.match(/([\\d,]+)\\s+reaction/i);
            if (rxMatch) likes = rxMatch[1].replace(/,/g, '') + ' reactions';
        }
        if (!comments) {
            const cmMatch = containerText.match(/([\\d,]+)\\s+comment/i);
            if (cmMatch) comments = cmMatch[1].replace(/,/g, '') + ' comments';
        }

        if (content.length > 20 && postUrl && (postUrl.includes('/feed/update/') || postUrl.includes('/posts/'))) {
            results.push({ postUrl, author, content, timestamp, likes, comments, dedupeKey });
        }
    }
    return results;
}"""


async def profile_follow(ctx: ActionContext) -> ActionResult:
    """Follow one LinkedIn profile. Mirrors the Follow button on a profile page."""
    profile_url = str(ctx.payload.get("profile_url") or "").strip()

    page = await _open(ctx)
    try:
        await goto_profile(
            page, profile_url, direct=False, account_id=acting_account_id()
        )
    except SessionExpiredError as expired:
        return ActionResult.failed(
            "the LinkedIn session expired", **session_expired_result(expired)
        )

    await wait_for_selector_fallback(page, "profile_top_card", timeout=10000)

    follow_button = None
    try:
        follow_button = await wait_for_selector_fallback(
            page, "follow_button", timeout=5000
        )
    except Exception:  # noqa: BLE001 - Follow may be hidden under "More"
        try:
            more_button = await wait_for_selector_fallback(
                page, "more_actions_button", timeout=3000
            )
            await dwell_and_click(more_button)
            follow_button = await wait_for_selector_fallback(
                page, "follow_button_more_menu", timeout=3000
            )
        except Exception:  # noqa: BLE001 - no Follow button found
            pass

    if not follow_button:
        return ActionResult.skipped(
            "no_follow_button",
            profile_url=profile_url,
            message=(
                "Follow button not found. Profile may already be followed, or "
                "this is a 1st-degree connection where Follow replaces Connect."
            ),
        )

    await dwell_and_click(follow_button)
    await pace(1.5)

    profile_name = await page.evaluate(
        """(selectors) => {
            const el = document.querySelector(selectors.profile_name.join(', '));
            return el ? el.innerText.trim() : 'Unknown';
        }""",
        selector_payload("profile_name"),
    )
    await _save(ctx, page)

    return _finish(
        ctx,
        {
            "status": "success",
            "message": f"Now following {profile_name}",
            "profile_url": profile_url,
            "profile_name": profile_name,
        },
        target=profile_url,
        profile_name=profile_name,
    )


ACTION_HANDLERS = {
    "connection_request": connection_request,
    "profile_view": profile_view,
    "profile_view_direct": profile_view,
    "profile_search": profile_search,
    "feed_browse": feed_browse,
    "post_search": post_search,
    "post_read": post_interaction,
    "post_like": post_interaction,
    "post_comment": post_interaction,
    "post_share": post_interaction,
    "profile_follow": profile_follow,
}
"""One handler per registered action name.

Keyed by the payload's `action`, not by `action_type`, because two actions can
share a type (a summary and a detail profile read) and two families can share a
type (a one-off search and MCP-02's People harvest).
"""


LEAD_ACTIONS = frozenset({"connection_request", "profile_view", "profile_view_direct"})
"""Actions a campaign step can run, because their target is the lead itself.

A search or a post interaction has no lead-derived target: a campaign step that
wanted one would have to say which post, and SEQ-02's step config is the place
for that rather than this module's guesswork.
"""


def _lead_profile_url(ctx: ActionContext) -> str | None:
    """Return the stored profile URL for the lead a campaign step targets."""
    if ctx.lead_id is None or ctx.conn is None:
        return None
    row = ctx.conn.execute(
        "SELECT public_id FROM leads WHERE id = ?", (int(ctx.lead_id),)
    ).fetchone()
    if row is None:
        return None
    public_id = row["public_id"] if not isinstance(row, tuple) else row[0]
    if not public_id:
        return None
    return f"https://www.linkedin.com/in/{public_id}"


async def _campaign_step(ctx: ActionContext, action_type: str) -> ActionResult:
    """Run a sequenced step through the same executor a one-off action uses.

    This is what makes a campaign do anything. SEQ-02 defines a step by its
    `action_type` and its config, with no payload discriminator, so a step's
    arguments have to be derived rather than read: the target is the lead's
    stored profile and the note is the step's own config.

    Deriving the URL rather than trusting the payload matters. A campaign step
    is generated by the scheduler for a lead it picked, so the only defensible
    target is that lead. Letting the payload override it would let a step aimed
    at one lead act on another.
    """
    if action_type not in LEAD_ACTIONS:
        return ActionResult.failed(
            f"campaign step {ctx.step.id} asks for {action_type!r}, which has no "
            "target this executor can derive from a lead. Define it as a one-off "
            "action or extend LEAD_ACTIONS with a step config that names its target."
        )

    profile_url = _lead_profile_url(ctx)
    if not profile_url:
        return ActionResult.skipped(
            "no_profile_url",
            lead_id=ctx.lead_id,
            message=(
                "the lead has no public_id, so there is no profile to open. "
                "Harvest or import the lead again to fill it in."
            ),
        )

    config = dict(ctx.config or {})
    payload: dict[str, Any] = {
        ACTION_KEY: action_type,
        "profile_url": profile_url,
    }
    if action_type == "connection_request":
        note = config.get("note") or ctx.payload.get("note")
        if note:
            payload["note"] = str(note)
        payload["direct"] = bool(config.get("direct", False))
    else:
        payload["shape"] = str(config.get("shape") or DETAIL_SHAPE)

    return await ACTION_HANDLERS[action_type](replace(ctx, payload=payload))


def _dispatcher(action_type: str):
    """Return the executor registered for one `action_type`.

    Three kinds of job arrive here, because the registry is keyed by
    `action_type` and three families share those keys.

    A one-off action names itself in its payload and runs. A campaign step names
    nothing, because SEQ-02 defines a step by its `action_type` and its config
    rather than by a payload discriminator, so it is recognised by the presence
    of `ctx.step` and its arguments are derived from the step and the lead. A
    harvest queued by MCP-02 also names nothing and also has no step, and is
    refused by name rather than guessed at: running it as a one-off search would
    spend the same budget and store none of the leads it was queued to collect.
    """

    async def execute(ctx: ActionContext) -> ActionResult:
        name = ctx.payload.get(ACTION_KEY)
        registered = ADHOC_ACTIONS.get(name) if isinstance(name, str) else None
        if registered is None:
            if ctx.step is not None:
                return await _campaign_step(ctx, action_type)
            return ActionResult.failed(
                f"job {ctx.job.id} carries no registered one-off action and no "
                f"campaign step, so MCP-03's {action_type!r} executor will not "
                "run it. A harvest queued by MCP-02 shares this action type and "
                "belongs to the harvest runner, not here."
            )
        if registered.action_type != action_type:
            return ActionResult.failed(
                f"job {ctx.job.id} names action {name!r}, which spends "
                f"{registered.action_type!r}, but the row was queued as "
                f"{action_type!r}. Refusing rather than charging the wrong budget."
            )
        return await ACTION_HANDLERS[registered.name](ctx)

    execute.__name__ = f"execute_{action_type}"
    execute.__qualname__ = execute.__name__
    return execute


def build_executors() -> dict[str, Any]:
    """Return the `action_type` to coroutine mapping the worker registers.

    One entry per distinct `action_type` in
    :data:`~linkedin_mcp.executors.contract.ADHOC_ACTIONS`, built from the
    catalogue rather than listed by hand so a new action cannot be added
    without becoming runnable.
    """
    return {
        action.action_type: _dispatcher(action.action_type)
        for action in ADHOC_ACTIONS.values()
    }


linkedin_executors = build_executors
"""The `module:attribute` target `worker.py --executors` accepts.

`worker.load_executors` calls a non-mapping attribute, so naming the builder
here means ``--executors linkedin_mcp.executors.linkedin:linkedin_executors``
works without a second wrapper.
"""
