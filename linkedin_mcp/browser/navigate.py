"""In-page navigation that avoids direct profile URL loads.

LinkedIn caps direct profile loads at roughly 40 per 24 hours, so the default
path types the lead's name into the global search bar and clicks the matching
result. Direct loading stays available but has to be asked for explicitly.

Every navigation in here ends with a check that LinkedIn served the page we
asked for. The rules live in `linkedin_mcp.safety.detect`, so a challenge halts
the run rather than being scraped as if it were a profile.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote

from linkedin_mcp.safety.detect import Detection, DetectionHalt, assert_page_clear

from .humanize import Humanizer, get_humanizer
from .selectors import selector_fallbacks

logger = logging.getLogger(__name__)

__all__ = [
    "DIRECT_PROFILE_LOAD_DAILY_CAP",
    "FEED_URL",
    "NavigationError",
    "NavigationResult",
    "SessionExpiredError",
    "goto_profile",
    "profile_slug",
    "slug_to_query",
]

FEED_URL = "https://www.linkedin.com/feed/"
DIRECT_PROFILE_LOAD_DAILY_CAP = 40
PROFILE_SLUG_PATTERN = re.compile(r"/in/([^/?#]+)")
SLUG_SPLIT_PATTERN = re.compile(r"[-_\s]+")
MAX_QUERY_WORDS = 4
RESULT_SCAN_ATTEMPTS = 3


class NavigationError(RuntimeError):
    """Raised when in-page navigation cannot reach the requested profile."""


class SessionExpiredError(NavigationError):
    """Raised when LinkedIn bounces the session to a login wall or challenge.

    The detection rules and the account state change live in
    `linkedin_mcp.safety.detect`. This stays the error navigation callers catch,
    and it carries the typed `DetectionHalt` on `halt` so a caller that wants the
    marker, the source and the raw evidence can read them off the detection
    rather than parsing the message.
    """

    def __init__(self, message: str, halt: DetectionHalt | None = None) -> None:
        self.halt = halt
        super().__init__(message)

    @property
    def detection(self) -> Detection | None:
        """The structured detection behind this error, when there was one."""
        return None if self.halt is None else self.halt.detection


@dataclass(frozen=True)
class NavigationResult:
    """Outcome of one profile navigation."""

    url: str
    method: str
    slug: str
    query: str


def profile_slug(profile_url: str) -> str:
    """Extract the public identifier from a LinkedIn profile URL."""
    match = PROFILE_SLUG_PATTERN.search(profile_url or "")
    if not match:
        raise NavigationError(f"Not a LinkedIn profile URL: {profile_url!r}")
    slug = unquote(match.group(1)).strip().strip("/")
    if not slug:
        raise NavigationError(f"Not a LinkedIn profile URL: {profile_url!r}")
    return slug


def slug_to_query(slug: str) -> str:
    """Turn a profile slug into the name a human would type into search."""
    tokens = [token for token in SLUG_SPLIT_PATTERN.split(unquote(slug)) if token]
    words = [token for token in tokens if not _is_identifier_token(token)]
    return " ".join(words[:MAX_QUERY_WORDS]) or slug


async def goto_profile(
    page: Any,
    profile_url: str,
    humanizer: Humanizer | None = None,
    direct: bool = False,
    query: str | None = None,
    timeout: int = 15000,
    allow_direct_fallback: bool = False,
    account_id: int | None = None,
) -> NavigationResult:
    """Open a LinkedIn profile, using the search bar unless direct is requested.

    Args:
        account_id: Account this navigation runs as. Pass it so a challenge is
            recorded against the right account. Left out, a challenge still
            halts the navigation but nothing is written down, because guessing
            the account would stop a session nobody challenged.
    """
    slug = profile_slug(profile_url)
    pacer = humanizer or get_humanizer()

    if direct:
        return await _direct_load(page, profile_url, slug, pacer, timeout, account_id)

    search_query = query or slug_to_query(slug)
    try:
        return await _search_bar_load(page, slug, search_query, pacer, timeout, account_id)
    except SessionExpiredError:
        raise
    except NavigationError as error:
        if not allow_direct_fallback:
            raise
        logger.warning("In-page navigation to %s failed (%s); falling back to a direct load", slug, error)
        return await _direct_load(page, profile_url, slug, pacer, timeout, account_id)


async def _search_bar_load(
    page: Any,
    slug: str,
    query: str,
    pacer: Humanizer,
    timeout: int,
    account_id: int | None = None,
) -> NavigationResult:
    await _ensure_linkedin_context(page, pacer, timeout, account_id)

    search_input = await _open_search_bar(page, pacer, timeout)
    await pacer.click(search_input)
    await pacer.type_text(search_input, query, clear=True)
    await _submit_search(page, search_input, pacer)
    await pacer.settle()
    await _assert_authenticated(page, account_id)

    link = await _find_profile_link(page, slug, pacer)
    if link is None:
        raise NavigationError(
            f"Search results for {query!r} did not contain a link to /in/{slug}"
        )

    await pacer.click(link)
    await _wait_for_profile(page, slug, pacer, timeout, account_id)
    return NavigationResult(url=_current_url(page), method="search_bar", slug=slug, query=query)


async def _direct_load(
    page: Any,
    profile_url: str,
    slug: str,
    pacer: Humanizer,
    timeout: int,
    account_id: int | None = None,
) -> NavigationResult:
    logger.warning(
        "Loading %s by direct URL; LinkedIn caps direct profile loads at about %d per 24h",
        profile_url,
        DIRECT_PROFILE_LOAD_DAILY_CAP,
    )
    await page.goto(profile_url, wait_until="domcontentloaded", timeout=timeout)
    await pacer.settle()
    await _assert_authenticated(page, account_id)
    return NavigationResult(url=_current_url(page), method="direct", slug=slug, query="")


async def _ensure_linkedin_context(
    page: Any,
    pacer: Humanizer,
    timeout: int,
    account_id: int | None = None,
) -> None:
    if "linkedin.com" in _current_url(page):
        await _assert_authenticated(page, account_id)
        return
    await page.goto(FEED_URL, wait_until="domcontentloaded", timeout=timeout)
    await pacer.settle()
    await _assert_authenticated(page, account_id)


async def _assert_authenticated(page: Any, account_id: int | None = None) -> None:
    """Halt when LinkedIn served an interstitial instead of the page we asked for.

    This runs after every navigation in this module. The rules, the account state
    change and the `safety_events` row all live in `linkedin_mcp.safety.detect`,
    so a marker is added in one place and every navigation picks it up.
    """
    try:
        await assert_page_clear(page, account_id=account_id)
    except DetectionHalt as halt:
        raise SessionExpiredError(f"Session expired: {halt}", halt=halt) from halt


async def _open_search_bar(page: Any, pacer: Humanizer, timeout: int) -> Any:
    search_input = await _query_first(page, "global_search_input")
    if search_input is not None:
        return search_input

    trigger = await _query_first(page, "global_search_trigger")
    if trigger is None:
        raise NavigationError("LinkedIn search bar not found on the current page")

    await pacer.click(trigger)
    search_input = await _wait_first(page, "global_search_input", timeout)
    if search_input is None:
        raise NavigationError("LinkedIn search input did not appear after opening the search bar")
    return search_input


async def _submit_search(page: Any, search_input: Any, pacer: Humanizer) -> None:
    keyboard = getattr(page, "keyboard", None)
    if keyboard is not None and hasattr(keyboard, "press"):
        await keyboard.press("Enter")
    else:
        await search_input.press("Enter")
    await pacer.micro_pause()


async def _find_profile_link(page: Any, slug: str, pacer: Humanizer) -> Any:
    selector = f'a[href*="/in/{slug}"]'
    for attempt in range(RESULT_SCAN_ATTEMPTS):
        handle = await page.query_selector(selector)
        if handle is not None:
            return handle
        if attempt < RESULT_SCAN_ATTEMPTS - 1:
            await pacer.scroll(page, 600)
    return None


async def _wait_for_profile(
    page: Any,
    slug: str,
    pacer: Humanizer,
    timeout: int,
    account_id: int | None = None,
) -> None:
    wait_for_url = getattr(page, "wait_for_url", None)
    if wait_for_url is not None:
        try:
            await wait_for_url(f"**/in/{slug}*", timeout=timeout)
        except Exception as error:
            logger.debug("wait_for_url did not confirm /in/%s: %s", slug, error)
    await pacer.settle()
    await _assert_authenticated(page, account_id)

    current = _current_url(page)
    if f"/in/{slug}" not in current:
        raise NavigationError(f"Clicked search result landed on {current!r} instead of /in/{slug}")


async def _query_first(page: Any, name: str) -> Any:
    for fallback in selector_fallbacks(name):
        handle = await page.query_selector(fallback)
        if handle is not None:
            return handle
    return None


async def _wait_first(page: Any, name: str, timeout: int) -> Any:
    fallbacks = selector_fallbacks(name)
    per_selector_timeout = max(1, int(timeout / len(fallbacks)))
    for fallback in fallbacks:
        try:
            handle = await page.wait_for_selector(fallback, timeout=per_selector_timeout)
        except Exception:
            continue
        if handle is not None:
            return handle
    return None


def _current_url(page: Any) -> str:
    return getattr(page, "url", "") or ""


def _is_identifier_token(token: str) -> bool:
    return len(token) >= 6 and any(char.isdigit() for char in token)
