"""The eight harvest tools: seven that queue an extraction and one that reports.

Every tool here validates, writes one `jobs` row, and returns. None of them
opens a browser, awaits a `run_*` function, or touches LinkedIn, because the
point of MCP-02 is that a harvest of a thousand profiles answers the MCP client
immediately with a job id. The extractors are already written and merged in
`linkedin_mcp.scrape`; SEQ-04 (#22) runs them. The exact row shape those two
agree on is :mod:`linkedin_mcp.tools.contract`.

Validation happens now, execution happens later
-----------------------------------------------
A malformed facet id, an unknown engagement kind or an unreadable CSV is the
caller's mistake, and it should land in the tool result they are looking at
rather than in a worker log at two in the morning. So each tool builds the same
objects the runner will build, `PeopleSearchFilters` and the rest, throws them
away, and stores the plain arguments. That costs nothing and moves every
knowable failure to the moment somebody can fix it.

`harvest_sales_nav` is not here
-------------------------------
Deliberately. SCRAPE-02 is descoped because Sales Navigator needs a paid
subscription, so there is no Sales Navigator extractor to wrap and no tool that
could honestly pretend to be one.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastmcp import Context, FastMCP

from linkedin_mcp.audit import audit_linkedin_action
from linkedin_mcp.scrape import (
    EVENT_TABS,
    SOURCE_COMPANY_EMPLOYEES,
    SOURCE_CONNECTIONS,
    SOURCE_CSV_IMPORT,
    SOURCE_EVENT_ATTENDEES,
    SOURCE_GROUP_MEMBERS,
    SOURCE_PEOPLE_SEARCH,
    SOURCE_POST_ENGAGERS,
    PeopleSearchFilters,
    PostEngagement,
    harvest_run,
)
from linkedin_mcp.sequences import JobState, insert_job, transaction
from linkedin_mcp.tools.contract import (
    HARVEST_ENQUEUE_ACTION,
    HARVEST_STATUS_ACTION,
    harvest_action,
    harvest_job_spec,
    harvest_jobs,
    job_harvest_name,
    job_run_id,
)
from linkedin_mcp.tools.runtime import (
    choice,
    error_result,
    positive_int,
    tool_account_id,
    tool_connection,
)

logger = logging.getLogger(__name__)

__all__ = ["MAX_HARVEST_LIMIT", "enqueue_harvest", "register_harvest_tools"]

MAX_HARVEST_LIMIT = 1000
"""Most people one queued run will ask for.

LinkedIn stops serving a search at roughly a thousand results anyway, which
`linkedin_mcp.scrape.paginate.PLATFORM_RESULT_CEILING` already knows, so a
larger number is a promise the platform will not keep. Queue a second job with
the cursor the first one stopped on instead.
"""

DEFAULT_HARVEST_LIMIT = 100

_ENGAGEMENTS = tuple(member.value for member in PostEngagement)


def enqueue_harvest(name: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Write one harvest job and describe it back to the caller.

    The result names the job id, the action type and the payload, so an agent
    reading it knows exactly what SEQ-04 will run and can poll `harvest_status`
    without guessing.
    """
    action = harvest_action(name)
    conn = tool_connection()
    account_id = tool_account_id()
    spec = harvest_job_spec(account_id, name, payload)
    with transaction(conn):
        job_id = insert_job(conn, spec, state=JobState.PENDING)

    logger.info("Queued %s harvest as job %d", name, job_id)
    return {
        "status": "success",
        "job_id": job_id,
        "harvest": name,
        "tool": action.tool,
        "action_type": spec.action_type,
        "source_type": action.source_type,
        "reaches_linkedin": action.reaches_linkedin,
        "scheduled_for": spec.scheduled_for,
        "priority": spec.priority,
        "state": JobState.PENDING.value,
        "payload": {"harvest": name, **payload},
        "message": (
            f"Queued a {name} harvest as job {job_id}. Nothing has been scraped "
            f"yet; the background runner picks it up. Poll harvest_status("
            f"job_id={job_id}) for progress."
        ),
    }


def _filters_payload(**fields: Any) -> dict[str, Any]:
    """Validate People search filters now and return them as plain JSON data."""
    lists = (
        "connection_degrees",
        "geo_urns",
        "current_companies",
        "past_companies",
        "industries",
        "schools",
    )
    built = PeopleSearchFilters(
        **{name: tuple(fields.get(name) or ()) for name in lists},
        keywords=fields.get("keywords"),
        title=fields.get("title"),
        first_name=fields.get("first_name"),
        last_name=fields.get("last_name"),
    )
    payload: dict[str, Any] = {}
    for name in lists:
        values = getattr(built, name)
        if values:
            payload[name] = list(values)
    for name in ("keywords", "title", "first_name", "last_name"):
        value = getattr(built, name)
        if value:
            payload[name] = value
    if not payload:
        raise ValueError(
            "a People search needs at least one filter; give keywords, a title, "
            "a name or a facet id"
        )
    return payload


def register_harvest_tools(mcp: FastMCP) -> None:
    """Register the eight harvest tools on the MCP server."""

    @mcp.tool()
    @audit_linkedin_action(
        HARVEST_ENQUEUE_ACTION, target="keywords", capture=("limit", "title")
    )
    async def harvest_people_search(
        keywords: str | None = None,
        title: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        connection_degrees: list[str] | None = None,
        geo_urns: list[str] | None = None,
        current_companies: list[str] | None = None,
        past_companies: list[str] | None = None,
        industries: list[str] | None = None,
        schools: list[str] | None = None,
        limit: int = DEFAULT_HARVEST_LIMIT,
        ctx: Context | None = None,
    ) -> dict:
        """Queue a People search harvest. Returns a job id, scrapes nothing yet."""
        try:
            payload = {
                "filters": _filters_payload(
                    keywords=keywords,
                    title=title,
                    first_name=first_name,
                    last_name=last_name,
                    connection_degrees=connection_degrees,
                    geo_urns=geo_urns,
                    current_companies=current_companies,
                    past_companies=past_companies,
                    industries=industries,
                    schools=schools,
                ),
                "limit": positive_int(
                    "limit",
                    limit,
                    default=DEFAULT_HARVEST_LIMIT,
                    maximum=MAX_HARVEST_LIMIT,
                ),
            }
            return enqueue_harvest(SOURCE_PEOPLE_SEARCH, payload)
        except Exception as error:
            return error_result(f"Could not queue the People search harvest: {error}")

    @mcp.tool()
    @audit_linkedin_action(
        HARVEST_ENQUEUE_ACTION, target="post", capture=("engagement", "limit")
    )
    async def harvest_post_engagers(
        post: str,
        engagement: str = PostEngagement.ALL.value,
        limit: int = DEFAULT_HARVEST_LIMIT,
        ctx: Context | None = None,
    ) -> dict:
        """Queue a harvest of a post's likers and commenters."""
        try:
            payload = {
                "post": str(post).strip(),
                "engagement": choice("engagement", engagement, _ENGAGEMENTS),
                "limit": positive_int(
                    "limit",
                    limit,
                    default=DEFAULT_HARVEST_LIMIT,
                    maximum=MAX_HARVEST_LIMIT,
                ),
            }
            if not payload["post"]:
                raise ValueError("post is required; give a post URL or activity id")
            return enqueue_harvest(SOURCE_POST_ENGAGERS, payload)
        except Exception as error:
            return error_result(f"Could not queue the post engager harvest: {error}")

    @mcp.tool()
    @audit_linkedin_action(HARVEST_ENQUEUE_ACTION, target="group", capture=("limit",))
    async def harvest_group_members(
        group: str,
        limit: int = DEFAULT_HARVEST_LIMIT,
        ctx: Context | None = None,
    ) -> dict:
        """Queue a harvest of a LinkedIn group's member list."""
        try:
            payload = {
                "group": str(group).strip(),
                "limit": positive_int(
                    "limit",
                    limit,
                    default=DEFAULT_HARVEST_LIMIT,
                    maximum=MAX_HARVEST_LIMIT,
                ),
            }
            if not payload["group"]:
                raise ValueError("group is required; give a group URL or id")
            return enqueue_harvest(SOURCE_GROUP_MEMBERS, payload)
        except Exception as error:
            return error_result(f"Could not queue the group member harvest: {error}")

    @mcp.tool()
    @audit_linkedin_action(
        HARVEST_ENQUEUE_ACTION, target="event", capture=("tab", "limit")
    )
    async def harvest_event_attendees(
        event: str,
        tab: str = EVENT_TABS[0],
        limit: int = DEFAULT_HARVEST_LIMIT,
        ctx: Context | None = None,
    ) -> dict:
        """Queue a harvest of an event's attendee list."""
        try:
            payload = {
                "event": str(event).strip(),
                "tab": choice("tab", tab, EVENT_TABS),
                "limit": positive_int(
                    "limit",
                    limit,
                    default=DEFAULT_HARVEST_LIMIT,
                    maximum=MAX_HARVEST_LIMIT,
                ),
            }
            if not payload["event"]:
                raise ValueError("event is required; give an event URL or id")
            return enqueue_harvest(SOURCE_EVENT_ATTENDEES, payload)
        except Exception as error:
            return error_result(f"Could not queue the event attendee harvest: {error}")

    @mcp.tool()
    @audit_linkedin_action(HARVEST_ENQUEUE_ACTION, target="company", capture=("limit",))
    async def harvest_company_employees(
        company: str,
        limit: int = DEFAULT_HARVEST_LIMIT,
        ctx: Context | None = None,
    ) -> dict:
        """Queue a harvest of a company's people tab."""
        try:
            payload = {
                "company": str(company).strip(),
                "limit": positive_int(
                    "limit",
                    limit,
                    default=DEFAULT_HARVEST_LIMIT,
                    maximum=MAX_HARVEST_LIMIT,
                ),
            }
            if not payload["company"]:
                raise ValueError("company is required; give a company URL or id")
            return enqueue_harvest(SOURCE_COMPANY_EMPLOYEES, payload)
        except Exception as error:
            return error_result(
                f"Could not queue the company employee harvest: {error}"
            )

    @mcp.tool()
    @audit_linkedin_action(HARVEST_ENQUEUE_ACTION, capture=("limit",))
    async def harvest_connections(
        limit: int = DEFAULT_HARVEST_LIMIT,
        ctx: Context | None = None,
    ) -> dict:
        """Queue a harvest of your own connection list."""
        try:
            payload = {
                "limit": positive_int(
                    "limit",
                    limit,
                    default=DEFAULT_HARVEST_LIMIT,
                    maximum=MAX_HARVEST_LIMIT,
                )
            }
            return enqueue_harvest(SOURCE_CONNECTIONS, payload)
        except Exception as error:
            return error_result(f"Could not queue the connection harvest: {error}")

    @mcp.tool()
    @audit_linkedin_action(
        HARVEST_ENQUEUE_ACTION, target="path", capture=("delimiter",)
    )
    async def harvest_import_csv(
        path: str,
        encoding: str = "utf-8-sig",
        delimiter: str = ",",
        ctx: Context | None = None,
    ) -> dict:
        """Queue an import of a CSV of people. Makes no LinkedIn request at all."""
        try:
            source = Path(str(path).strip())
            if not str(source):
                raise ValueError("path is required; give a CSV file to import")
            if not source.is_file():
                raise ValueError(f"{source} is not a readable file")
            if len(delimiter) != 1:
                raise ValueError(
                    f"delimiter must be a single character, got {delimiter!r}"
                )
            payload = {
                "path": str(source),
                "encoding": encoding,
                "delimiter": delimiter,
            }
            return enqueue_harvest(SOURCE_CSV_IMPORT, payload)
        except Exception as error:
            return error_result(f"Could not queue the CSV import: {error}")

    @mcp.tool()
    @audit_linkedin_action(HARVEST_STATUS_ACTION, capture=("job_id", "run_id"))
    async def harvest_status(
        job_id: int | None = None,
        run_id: int | None = None,
        limit: int = 20,
        ctx: Context | None = None,
    ) -> dict:
        """Report progress for a harvest job, a harvest run, or the recent ones."""
        try:
            conn = tool_connection()
            account_id = tool_account_id()

            if run_id is not None:
                run = _run_progress(conn, int(run_id))
                if run is None:
                    return error_result(f"No harvest run {run_id} exists")
                return {"status": "success", "run": run, "message": _run_message(run)}

            if job_id is not None:
                job = next(
                    (
                        candidate
                        for candidate in harvest_jobs(conn, account_id)
                        if candidate.id == int(job_id)
                    ),
                    None,
                )
                if job is None:
                    return error_result(
                        f"No harvest job {job_id} belongs to this account"
                    )
                report = _job_report(conn, job)
                return {"status": "success", **report, "message": _job_message(report)}

            reports = [
                _job_report(conn, job)
                for job in harvest_jobs(
                    conn,
                    account_id,
                    limit=positive_int("limit", limit, default=20, maximum=200),
                )
            ]
            return {
                "status": "success",
                "count": len(reports),
                "jobs": reports,
                "message": (
                    f"{len(reports)} harvest job(s) queued by this account, "
                    "newest first."
                ),
            }
        except Exception as error:
            return error_result(f"Could not read harvest status: {error}")


def _run_progress(conn: Any, run_id: int) -> dict[str, Any] | None:
    """Return one `harvest_runs` row as progress, or None when it is missing."""
    run = harvest_run(conn, run_id)
    if run is None:
        return None
    return {
        "run_id": run["id"],
        "source_type": run["source_type"],
        "found_count": run["found_count"],
        "new_count": run["new_count"],
        "started_at": run["started_at"],
        "finished_at": run["finished_at"],
        "running": run["finished_at"] is None,
        "cursor": run["params"].get("cursor", {}),
        "filters": run["params"].get("filters", {}),
    }


def _job_report(conn: Any, job: Any) -> dict[str, Any]:
    """Describe one harvest job, with its run progress when the run has begun."""
    run_id = job_run_id(job)
    return {
        "job_id": job.id,
        "harvest": job_harvest_name(job),
        "action_type": job.action_type,
        "state": job.state,
        "attempts": job.attempts,
        "scheduled_for": job.scheduled_for,
        "priority": job.priority,
        "locked_by": job.locked_by,
        "locked_at": job.locked_at,
        "last_error": job.last_error,
        "payload": job.payload,
        "run": None if run_id is None else _run_progress(conn, run_id),
    }


def _run_message(run: dict[str, Any]) -> str:
    state = "running" if run["running"] else "finished"
    return (
        f"Harvest run {run['run_id']} ({run['source_type']}) is {state}: "
        f"{run['found_count']} found, {run['new_count']} new."
    )


def _job_message(report: dict[str, Any]) -> str:
    run = report["run"]
    if run is None:
        return (
            f"Harvest job {report['job_id']} ({report['harvest']}) is "
            f"{report['state']}. The runner has not opened a harvest run yet, so "
            f"there are no page counts to report."
        )
    return (
        f"Harvest job {report['job_id']} ({report['harvest']}) is "
        f"{report['state']}. {_run_message(run)}"
    )
