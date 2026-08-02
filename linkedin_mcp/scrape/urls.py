"""LinkedIn search URLs, assembled from validated filters.

Why a direct load is fine here
------------------------------
CORE-04 routes profile visits through the search bar because LinkedIn caps
direct loads of `/in/...` at roughly 40 per 24 hours. That cap is about profile
pages. A search result page is an ordinary application route with no such cap,
and its query string is the only place the full filter set can be expressed. So
this module builds URLs and the paginator hands them to `page.goto`. Anything
under `/in/` still goes through `linkedin_mcp.browser.navigate.goto_profile`.
"""

from __future__ import annotations

import re

from linkedin_mcp.scrape.filters import (
    PeopleSearchFilters,
    PostSearchFilters,
    encode_params,
)

__all__ = [
    "GROUP_ID_PATTERN",
    "GROUP_URL_TEMPLATE",
    "PEOPLE_SEARCH_URL",
    "POST_SEARCH_URL",
    "SEARCH_ORIGIN",
    "group_id_from",
    "group_members_url",
    "people_search_url",
    "post_search_url",
]

PEOPLE_SEARCH_URL = "https://www.linkedin.com/search/results/people/"
POST_SEARCH_URL = "https://www.linkedin.com/search/results/content/"
GROUP_URL_TEMPLATE = "https://www.linkedin.com/groups/{group_id}/members/"

SEARCH_ORIGIN = "FACETED_SEARCH"
"""What LinkedIn's own filter panel sets, so a built URL looks like a real one."""

GROUP_ID_PATTERN = re.compile(r"(?:^|/groups/)(?P<group_id>\d+)")


def people_search_url(filters: PeopleSearchFilters, page: int = 1) -> str:
    """Return the People search URL for one page of a filtered search."""
    return _search_url(PEOPLE_SEARCH_URL, filters.to_params(), page)


def post_search_url(filters: PostSearchFilters, page: int = 1) -> str:
    """Return the content search URL for one page of a filtered search."""
    return _search_url(POST_SEARCH_URL, filters.to_params(), page)


def group_id_from(group: str | int) -> str:
    """Return the numeric group id from an id or a group URL."""
    match = GROUP_ID_PATTERN.search(str(group).strip())
    if not match:
        raise ValueError(
            f"{group!r} is not a LinkedIn group id or group URL such as "
            "https://www.linkedin.com/groups/12345/"
        )
    return match.group("group_id")


def group_members_url(group: str | int) -> str:
    """Return the member list URL for a LinkedIn group."""
    return GROUP_URL_TEMPLATE.format(group_id=group_id_from(group))


def _search_url(base: str, params: dict[str, str], page: int) -> str:
    if page < 1:
        raise ValueError(f"search pages start at 1, got {page}")
    query = dict(params)
    query["origin"] = SEARCH_ORIGIN
    if page > 1:
        query["page"] = str(page)
    return f"{base}?{encode_params(query)}"
