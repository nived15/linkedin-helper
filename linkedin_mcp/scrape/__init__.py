"""Standard search result extraction: People, Posts and Group members.

This package is a plain Python API. It registers no MCP tools and starts no
background work, because MCP-02 (#25) owns the tool surface and SEQ-04 (#22)
owns the runner. What lives here is the extraction itself: build a filtered
search URL, walk its pages under the safety gate, read each page into records,
and store the people through the DB-02 lead store.

Three surfaces, one loop
------------------------
- :func:`run_people_search` walks `/search/results/people/` with the full
  filter set.
- :func:`run_post_search` walks `/search/results/content/` and stores the post
  authors as leads.
- :func:`run_group_member_extraction` walks a group's member list, which pages
  by loading more rather than by a page number.

All three share :func:`linkedin_mcp.scrape.paginate.paginate`, which asks the
safety gate before every fetch, paces every page turn through the humanizer,
and stops on the first of four conditions: the requested count is reached,
LinkedIn's roughly 1,000 result ceiling is reached, a page yields nothing new,
or the gate refuses. Each returns a :class:`ScrapeSummary` and a cursor, so an
interrupted run resumes rather than restarting.
"""

from linkedin_mcp.scrape.extract import (
    extract_group_members,
    extract_people,
    extract_posts,
)
from linkedin_mcp.scrape.filters import (
    CONNECTION_DEGREES,
    DATE_POSTED_WINDOWS,
    POST_SORT_ORDERS,
    FilterError,
    PeopleSearchFilters,
    PostSearchFilters,
)
from linkedin_mcp.scrape.groups import (
    GROUP_MEMBERS_ACTION,
    run_group_member_extraction,
)
from linkedin_mcp.scrape.harvest import harvest_people, stale_lead_ids
from linkedin_mcp.scrape.paginate import (
    MAX_SEARCH_PAGE,
    PLATFORM_RESULT_CEILING,
    RESULTS_PER_PAGE,
    PageRun,
    SearchCursor,
    StopReason,
    assert_session_alive,
    paginate,
)
from linkedin_mcp.scrape.people import PEOPLE_SEARCH_ACTION, run_people_search
from linkedin_mcp.scrape.posts import POST_SEARCH_ACTION, run_post_search
from linkedin_mcp.scrape.records import PersonResult, PostResult
from linkedin_mcp.scrape.runs import (
    SOURCE_GROUP_MEMBERS,
    SOURCE_PEOPLE_SEARCH,
    SOURCE_POST_SEARCH,
    finish_harvest_run,
    harvest_run,
    resume_cursor,
    start_harvest_run,
)
from linkedin_mcp.scrape.summary import ScrapeSummary
from linkedin_mcp.scrape.urls import (
    PEOPLE_SEARCH_URL,
    POST_SEARCH_URL,
    group_members_url,
    people_search_url,
    post_search_url,
)

__all__ = [
    "CONNECTION_DEGREES",
    "DATE_POSTED_WINDOWS",
    "GROUP_MEMBERS_ACTION",
    "MAX_SEARCH_PAGE",
    "PEOPLE_SEARCH_ACTION",
    "PEOPLE_SEARCH_URL",
    "PLATFORM_RESULT_CEILING",
    "POST_SEARCH_ACTION",
    "POST_SEARCH_URL",
    "POST_SORT_ORDERS",
    "RESULTS_PER_PAGE",
    "SOURCE_GROUP_MEMBERS",
    "SOURCE_PEOPLE_SEARCH",
    "SOURCE_POST_SEARCH",
    "FilterError",
    "PageRun",
    "PeopleSearchFilters",
    "PersonResult",
    "PostResult",
    "PostSearchFilters",
    "ScrapeSummary",
    "SearchCursor",
    "StopReason",
    "assert_session_alive",
    "extract_group_members",
    "extract_people",
    "extract_posts",
    "finish_harvest_run",
    "group_members_url",
    "harvest_people",
    "harvest_run",
    "paginate",
    "people_search_url",
    "post_search_url",
    "resume_cursor",
    "run_group_member_extraction",
    "run_people_search",
    "run_post_search",
    "stale_lead_ids",
    "start_harvest_run",
]
