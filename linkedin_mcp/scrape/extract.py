"""Turn a rendered LinkedIn results page into structured records.

Extraction runs in Python over element handles rather than as a blob of
injected JavaScript. That is deliberate. The old post search shipped its
extraction as a JS string that walked the DOM guessing at what looked like a
post, which could not be tested without a browser and could not be reasoned
about at all. Element handles with an ordered selector fallback per field are
testable against a duck-typed fake page, and a field that fails to resolve
comes back as None instead of taking the run down with it.

Resilience is the rule here. One malformed card never aborts a page, and a card
missing its headline, location or avatar is still a lead. The only sighting
that gets dropped is one with no profile link at all, because the lead store
cannot resolve a person it has no identifier for.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from linkedin_mcp.browser.selectors import selector_fallbacks
from linkedin_mcp.scrape.records import (
    PersonResult,
    PostResult,
    activity_id_from,
    canonical_post_url,
    canonical_profile_url,
    member_urn_from,
    name_from_slug,
    parse_count,
    parse_distance,
    profile_hash_from,
    public_id_from,
)

logger = logging.getLogger(__name__)

__all__ = [
    "URN_ATTRIBUTES",
    "attr_of",
    "extract_group_members",
    "extract_people",
    "extract_posts",
    "handle_text",
    "parse_current_position",
    "query_all",
    "query_first",
    "text_of",
]

URN_ATTRIBUTES = ("data-chameleon-result-urn", "data-urn", "data-id", "data-entity-urn")
"""Attributes a LinkedIn card hangs its entity URN on, newest first."""

CURRENT_POSITION_PATTERN = re.compile(
    r"^\s*(?:current|current position)\s*:\s*(?P<body>.+)$", re.IGNORECASE
)
WHITESPACE_PATTERN = re.compile(r"\s+")


def _clean(text: str | None) -> str | None:
    """Collapse whitespace and undo LinkedIn's duplicated accessible text.

    LinkedIn renders many labels twice, once visually and once for screen
    readers, so reading a card gives 'Nived Velayudhan\\nNived Velayudhan'. A
    string that is exactly one phrase repeated is halved.
    """
    if text is None:
        return None
    collapsed = WHITESPACE_PATTERN.sub(" ", text.replace("\u00a0", " ")).strip()
    if not collapsed:
        return None
    halves = collapsed.split("\n")
    if len(halves) == 2 and halves[0].strip() == halves[1].strip():
        return halves[0].strip()
    midpoint, remainder = divmod(len(collapsed), 2)
    if remainder == 1 and collapsed[midpoint] == " ":
        left = collapsed[:midpoint]
        right = collapsed[midpoint + 1 :]
        if left and left == right:
            return left
    return collapsed


async def handle_text(handle: Any) -> str | None:
    """Read the visible text of an element handle, however it exposes it."""
    if handle is None:
        return None
    for reader in ("inner_text", "text_content"):
        method = getattr(handle, reader, None)
        if method is None:
            continue
        try:
            return _clean(await method())
        except Exception as error:  # noqa: BLE001 - a dead handle is not fatal
            logger.debug("Reading %s off an element handle failed: %s", reader, error)
    return None


async def query_first(scope: Any, name: str) -> Any:
    """Return the first element matching any fallback for a selector name."""
    for fallback in selector_fallbacks(name):
        try:
            handle = await scope.query_selector(fallback)
        except Exception as error:  # noqa: BLE001 - a bad selector is not fatal
            logger.debug("Selector %r failed for %s: %s", fallback, name, error)
            continue
        if handle is not None:
            return handle
    return None


async def query_all(scope: Any, name: str) -> list[Any]:
    """Return the matches of the first fallback that finds anything."""
    for fallback in selector_fallbacks(name):
        try:
            handles = await scope.query_selector_all(fallback)
        except Exception as error:  # noqa: BLE001 - a bad selector is not fatal
            logger.debug("Selector %r failed for %s: %s", fallback, name, error)
            continue
        if handles:
            return list(handles)
    return []


async def text_of(scope: Any, name: str) -> str | None:
    """Return the text of the first element matching a selector name."""
    return await handle_text(await query_first(scope, name))


async def attr_of(scope: Any, name: str, attribute: str) -> str | None:
    """Return an attribute of the first element matching a selector name."""
    handle = await query_first(scope, name)
    if handle is None:
        return None
    getter = getattr(handle, "get_attribute", None)
    if getter is None:
        return None
    try:
        value = await getter(attribute)
    except Exception as error:  # noqa: BLE001 - a dead handle is not fatal
        logger.debug("Reading @%s off %s failed: %s", attribute, name, error)
        return None
    return value.strip() if isinstance(value, str) and value.strip() else None


async def _urn_of(handle: Any) -> str | None:
    """Return whichever entity URN attribute a card carries."""
    getter = getattr(handle, "get_attribute", None)
    if getter is None:
        return None
    for attribute in URN_ATTRIBUTES:
        try:
            value = await getter(attribute)
        except Exception as error:  # noqa: BLE001 - a dead handle is not fatal
            logger.debug("Reading @%s off a card failed: %s", attribute, error)
            continue
        if value:
            return value
    return None


def parse_current_position(text: str | None) -> tuple[str | None, str | None]:
    """Split a 'Current: Title at Company' line into company and title."""
    if not text:
        return None, None
    match = CURRENT_POSITION_PATTERN.match(text)
    if not match:
        return None, None
    body = match.group("body").strip()
    separator = re.search(r"\s+at\s+", body, re.IGNORECASE)
    if not separator:
        return body or None, None
    title = body[: separator.start()].strip() or None
    company = body[separator.end() :].strip() or None
    return company, title


async def _person_from(
    item: Any,
    *,
    link_selector: str,
    name_selector: str,
    headline_selector: str,
    location_selector: str | None = None,
    distance_selector: str | None = None,
    avatar_selector: str | None = None,
    summary_selector: str | None = None,
    position_selector: str | None = None,
    premium_selector: str | None = None,
) -> PersonResult | None:
    """Build a person from one card, tolerating every missing field but identity."""
    profile_url = await attr_of(item, link_selector, "href")
    public_id = public_id_from(profile_url)
    urn = await _urn_of(item)
    member_id = member_urn_from(urn) or member_urn_from(profile_url)
    hash_id = profile_hash_from(urn) or profile_hash_from(profile_url)

    if public_id is None and member_id is None:
        logger.debug("Skipping a result card with no profile link")
        return None

    full_name = await text_of(item, name_selector)
    if not full_name:
        full_name = name_from_slug(public_id) if public_id else (member_id or "")
    if not full_name:
        return None

    summary = await text_of(item, summary_selector) if summary_selector else None
    position_text = (
        await text_of(item, position_selector) if position_selector else None
    )
    organization_name, organization_title = parse_current_position(position_text)
    if summary is not None and summary == position_text and organization_name:
        summary = None

    badges: dict[str, Any] = {}
    if premium_selector and await query_first(item, premium_selector) is not None:
        badges["premium"] = True

    return PersonResult(
        full_name=full_name,
        public_id=public_id,
        member_id=member_id,
        hash_id=hash_id,
        headline=await text_of(item, headline_selector),
        location_name=(
            await text_of(item, location_selector) if location_selector else None
        ),
        organization_name=organization_name,
        organization_title=organization_title,
        member_distance=parse_distance(
            await text_of(item, distance_selector) if distance_selector else None
        ),
        avatar_url=(
            await attr_of(item, avatar_selector, "src") if avatar_selector else None
        ),
        summary=summary,
        profile_url=canonical_profile_url(profile_url),
        badges=badges,
    )


async def extract_people(page: Any) -> list[PersonResult]:
    """Extract every person on a rendered People search results page."""
    people: list[PersonResult] = []
    for item in await query_all(page, "people_result_item"):
        try:
            person = await _person_from(
                item,
                link_selector="people_result_profile_link",
                name_selector="people_result_name",
                headline_selector="people_result_headline",
                location_selector="people_result_location",
                distance_selector="people_result_distance",
                avatar_selector="people_result_avatar",
                summary_selector="people_result_summary",
                position_selector="people_result_current_position",
                premium_selector="people_result_premium_badge",
            )
        except Exception as error:  # noqa: BLE001 - one bad card is not a bad page
            logger.warning("Skipping an unreadable People search card: %s", error)
            continue
        if person is not None:
            people.append(person)
    return people


async def extract_group_members(page: Any) -> list[PersonResult]:
    """Extract every member on a rendered group member list."""
    members: list[PersonResult] = []
    for item in await query_all(page, "group_member_item"):
        try:
            person = await _person_from(
                item,
                link_selector="group_member_profile_link",
                name_selector="group_member_name",
                headline_selector="group_member_headline",
            )
        except Exception as error:  # noqa: BLE001 - one bad card is not a bad page
            logger.warning("Skipping an unreadable group member card: %s", error)
            continue
        if person is not None:
            members.append(person)
    return members


async def extract_posts(page: Any) -> list[PostResult]:
    """Extract every post on a rendered content search results page."""
    posts: list[PostResult] = []
    for item in await query_all(page, "post_result_item"):
        try:
            posts.append(await _post_from(item))
        except Exception as error:  # noqa: BLE001 - one bad card is not a bad page
            logger.warning("Skipping an unreadable post search card: %s", error)
    return [post for post in posts if post.is_identifiable()]


async def _post_from(item: Any) -> PostResult:
    """Build a post from one card, tolerating every missing field."""
    permalink = await attr_of(item, "post_result_permalink", "href")
    urn = await _urn_of(item)
    author_url = await attr_of(item, "post_result_author_link", "href")
    author_public_id = public_id_from(author_url)

    author: PersonResult | None = None
    if author_public_id or member_urn_from(author_url):
        author_name = await text_of(item, "post_result_author_name")
        if not author_name and author_public_id:
            author_name = name_from_slug(author_public_id)
        if author_name:
            author = PersonResult(
                full_name=author_name,
                public_id=author_public_id,
                member_id=member_urn_from(author_url),
                hash_id=profile_hash_from(author_url),
                headline=await text_of(item, "post_result_author_headline"),
                profile_url=canonical_profile_url(author_url),
            )

    return PostResult(
        post_url=canonical_post_url(permalink),
        activity_id=activity_id_from(urn) or activity_id_from(permalink),
        content=await text_of(item, "post_result_content"),
        posted_at_text=await text_of(item, "post_result_timestamp"),
        reactions=parse_count(await text_of(item, "post_result_reactions")),
        comments=parse_count(await text_of(item, "post_result_comments")),
        reposts=parse_count(await text_of(item, "post_result_reposts")),
        author=author,
    )
