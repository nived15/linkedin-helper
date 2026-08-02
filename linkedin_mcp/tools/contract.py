"""The job shape MCP-02 writes and SEQ-04 (#22) executes.

A harvest tool does not scrape. It validates its arguments, writes one row to
`jobs`, and returns the job id, so harvesting a thousand profiles answers the
MCP client in milliseconds instead of holding the connection open for hours.
The scraping itself is SEQ-04's, and this module is the whole contract between
the two.

The row a harvest tool writes
-----------------------------
=================  ===========================================================
`account_id`       The acting account, from `linkedin_mcp.audit`.
`campaign_id`      **NULL.** A harvest belongs to no campaign.
`lead_id`          **NULL.** A harvest is how leads arrive; it has none yet.
`step_id`          **NULL.** There is no campaign step behind it.
`action_type`      The metered LinkedIn action one page of the run spends. See
                   :data:`HARVEST_ACTIONS`. `csv_import` reaches LinkedIn not
                   at all and is listed in :data:`LOCAL_HARVEST_ACTIONS`.
`payload_json`     `{"harvest": <name>, ...}`, sorted keys. The `harvest` key
                   names the extractor; the rest are its arguments. See the
                   per-tool list below.
`scheduled_for`    Enqueue time, so the job is due immediately.
`priority`         :data:`HARVEST_JOB_PRIORITY`, which is 0. A harvest must not
                   jump ahead of outreach that is already due.
`state`            `pending`.
`attempts`         0.
=================  ===========================================================

Three consequences SEQ-04 has to know about
-------------------------------------------
1. **`due_jobs` does not return these rows.** Its SQL inner-joins `campaigns`
   and `campaign_leads`, and a harvest job has neither. A campaign-less job is
   invisible to it by construction, not by accident: SEQ-01's `0003` index
   deliberately excludes NULL `campaign_id`/`lead_id` so ad-hoc jobs stay
   unconstrained. Making the runner pick these up is #22's, and MCP-02 neither
   modifies `due_jobs` nor opens a second queue. Read them with
   :func:`~linkedin_mcp.sequences.jobs.list_jobs` and filter on
   `campaign_id is None`, which is what :func:`harvest_jobs` does.
2. **`Job.spec()` raises on them.** It requires all three of `campaign_id`,
   `lead_id` and `step_id`, so a harvest job has no derivable spec and
   `rebuild_jobs` would delete it. Never hand a harvest job to the rebuild
   path; it is not a projection of campaign state and cannot be reconstructed
   from one.
3. **The run id comes back through the payload.** The extractors open their own
   `harvest_runs` row through
   :func:`~linkedin_mcp.scrape.runs.start_harvest_run`, so the id only exists
   once the worker has started. Write it back onto the job payload under
   :data:`RUN_ID_KEY` and `harvest_status` will report page-level progress
   instead of only the job state.

Payload per tool
----------------
Every payload carries `harvest`. The rest:

- `people_search`: `filters` (a mapping of `PeopleSearchFilters` fields),
  `limit`.
- `post_engagers`: `post`, `engagement` (`all`, `reactions` or `comments`),
  `limit`.
- `group_members`: `group`, `limit`.
- `event_attendees`: `event`, `tab` (`attendees` or `comments`), `limit`.
- `company_employees`: `company`, `limit`.
- `connections`: `limit`.
- `csv_import`: `path`, `encoding`, `delimiter`.

Arguments are validated at enqueue time, not at run time. A bad facet id or an
unreadable file is the caller's mistake and should surface in the tool result
they are looking at, rather than three hours later in a worker log.

Dedupe is not repeated here
---------------------------
Every runner in :data:`HARVEST_ACTIONS` already stores through
`linkedin_mcp.scrape.harvest.harvest_people`, which is DB-03's
`harvest_leads`. Running the same harvest twice resolves onto the same lead
rows. This package adds no second dedupe layer and writes no lead itself.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any

from linkedin_mcp.scrape import (
    EVENT_ATTENDEES_ACTION,
    GROUP_MEMBERS_ACTION,
    PEOPLE_LIST_ACTION,
    PEOPLE_SEARCH_ACTION,
    POST_ENGAGERS_ACTION,
    SOURCE_COMPANY_EMPLOYEES,
    SOURCE_CONNECTIONS,
    SOURCE_CSV_IMPORT,
    SOURCE_EVENT_ATTENDEES,
    SOURCE_GROUP_MEMBERS,
    SOURCE_PEOPLE_SEARCH,
    SOURCE_POST_ENGAGERS,
    import_leads_from_csv,
    run_company_employee_harvest,
    run_connection_harvest,
    run_event_attendee_harvest,
    run_group_member_extraction,
    run_people_search,
    run_post_engager_harvest,
)
from linkedin_mcp.sequences import Job, JobSpec, list_jobs, now_timestamp

__all__ = [
    "CSV_IMPORT_ACTION",
    "HARVEST_ACTIONS",
    "HARVEST_ENQUEUE_ACTION",
    "HARVEST_JOB_PRIORITY",
    "HARVEST_KEY",
    "HARVEST_STATUS_ACTION",
    "LEAD_EXPORT_ACTION",
    "LEAD_READ_ACTION",
    "LOCAL_HARVEST_ACTIONS",
    "RUN_ID_KEY",
    "HarvestAction",
    "harvest_action",
    "harvest_job_spec",
    "harvest_jobs",
    "is_harvest_job",
    "job_harvest_name",
    "job_run_id",
]

HARVEST_KEY = "harvest"
"""Payload key naming which extractor a job runs."""

RUN_ID_KEY = "run_id"
"""Payload key SEQ-04 writes the `harvest_runs` id back onto."""

HARVEST_JOB_PRIORITY = 0
"""Priority every harvest job carries.

Zero is the priority a campaign step gets when its config names none, so a
harvest queued now sits behind nothing and ahead of nothing. Harvesting is
never more urgent than an outreach step that is already due, and a queue where
reads outrank writes is a queue that starves the writes.
"""

CSV_IMPORT_ACTION = SOURCE_CSV_IMPORT
"""Job action type for a CSV import, which makes no LinkedIn request at all."""

HARVEST_ENQUEUE_ACTION = "harvest_enqueue"
"""Audit action type for queueing a harvest. Unmetered: it writes a row."""

HARVEST_STATUS_ACTION = "harvest_status"
"""Audit action type for reading harvest progress. Unmetered: a local read."""

LEAD_READ_ACTION = "lead_read"
"""Audit action type for the CRM reads. Unmetered: a local read."""

LEAD_EXPORT_ACTION = "lead_export"
"""Audit action type for a CSV export. Unmetered: a local read and a file."""

LOCAL_HARVEST_ACTIONS: frozenset[str] = frozenset({CSV_IMPORT_ACTION})
"""Job action types a worker must not spend a safety-gate lease on.

Reading a local file does nothing to LinkedIn. Gating it would spend a
browsing budget on an action LinkedIn never sees, which is the reasoning
`linkedin_mcp.scrape.csv_import` already sets out and this only restates for
the queue.
"""


@dataclass(frozen=True, slots=True)
class HarvestAction:
    """One extractor, named by the payload and dispatched by SEQ-04."""

    name: str
    tool: str
    action_type: str
    source_type: str
    runner: Callable[..., Any]
    payload_fields: tuple[str, ...] = ()

    @property
    def reaches_linkedin(self) -> bool:
        """False when running this makes no request LinkedIn could see."""
        return self.action_type not in LOCAL_HARVEST_ACTIONS


_ACTIONS: tuple[HarvestAction, ...] = (
    HarvestAction(
        name=SOURCE_PEOPLE_SEARCH,
        tool="harvest_people_search",
        action_type=PEOPLE_SEARCH_ACTION,
        source_type=SOURCE_PEOPLE_SEARCH,
        runner=run_people_search,
        payload_fields=("filters", "limit"),
    ),
    HarvestAction(
        name=SOURCE_POST_ENGAGERS,
        tool="harvest_post_engagers",
        action_type=POST_ENGAGERS_ACTION,
        source_type=SOURCE_POST_ENGAGERS,
        runner=run_post_engager_harvest,
        payload_fields=("post", "engagement", "limit"),
    ),
    HarvestAction(
        name=SOURCE_GROUP_MEMBERS,
        tool="harvest_group_members",
        action_type=GROUP_MEMBERS_ACTION,
        source_type=SOURCE_GROUP_MEMBERS,
        runner=run_group_member_extraction,
        payload_fields=("group", "limit"),
    ),
    HarvestAction(
        name=SOURCE_EVENT_ATTENDEES,
        tool="harvest_event_attendees",
        action_type=EVENT_ATTENDEES_ACTION,
        source_type=SOURCE_EVENT_ATTENDEES,
        runner=run_event_attendee_harvest,
        payload_fields=("event", "tab", "limit"),
    ),
    HarvestAction(
        name=SOURCE_COMPANY_EMPLOYEES,
        tool="harvest_company_employees",
        action_type=PEOPLE_LIST_ACTION,
        source_type=SOURCE_COMPANY_EMPLOYEES,
        runner=run_company_employee_harvest,
        payload_fields=("company", "limit"),
    ),
    HarvestAction(
        name=SOURCE_CONNECTIONS,
        tool="harvest_connections",
        action_type=PEOPLE_LIST_ACTION,
        source_type=SOURCE_CONNECTIONS,
        runner=run_connection_harvest,
        payload_fields=("limit",),
    ),
    HarvestAction(
        name=SOURCE_CSV_IMPORT,
        tool="harvest_import_csv",
        action_type=CSV_IMPORT_ACTION,
        source_type=SOURCE_CSV_IMPORT,
        runner=import_leads_from_csv,
        payload_fields=("path", "encoding", "delimiter"),
    ),
)

HARVEST_ACTIONS: Mapping[str, HarvestAction] = MappingProxyType(
    {action.name: action for action in _ACTIONS}
)
"""Every extractor a harvest job can name, keyed by its payload `harvest` value.

SEQ-04 dispatches on this. MCP-02 never calls a `runner` itself: a tool that
awaited one would be scraping inline, which is the entire thing this issue
exists to stop.
"""


def harvest_action(name: str) -> HarvestAction:
    """Return the extractor a payload names, rejecting one nobody registered."""
    try:
        return HARVEST_ACTIONS[name]
    except KeyError:
        known = ", ".join(sorted(HARVEST_ACTIONS))
        raise KeyError(
            f"{name!r} is not a registered harvest. Known harvests: {known}. "
            "Sales Navigator is deliberately absent; see SCRAPE-02."
        ) from None


def harvest_job_spec(
    account_id: int,
    name: str,
    payload: Mapping[str, Any] | None = None,
    *,
    now: datetime | str | None = None,
    priority: int = HARVEST_JOB_PRIORITY,
) -> JobSpec:
    """Build the queue row for one harvest.

    `campaign_id`, `lead_id` and `step_id` are None on purpose. `JobSpec`
    annotates them as `int`, but it is a plain dataclass with no runtime
    checking, and SEQ-01's unique index skips NULLs, so an ad-hoc job is
    allowed and unconstrained.
    """
    action = harvest_action(name)
    body: dict[str, Any] = {HARVEST_KEY: action.name}
    body.update(payload or {})
    return JobSpec(
        campaign_id=None,  # type: ignore[arg-type]
        lead_id=None,  # type: ignore[arg-type]
        step_id=None,  # type: ignore[arg-type]
        account_id=account_id,
        action_type=action.action_type,
        payload_json=json.dumps(body, sort_keys=True),
        scheduled_for=now_timestamp(now),
        priority=int(priority),
    )


def is_harvest_job(job: Job) -> bool:
    """True when a queue row is a harvest rather than a campaign step."""
    return (
        job.campaign_id is None
        and job.lead_id is None
        and job.payload.get(HARVEST_KEY) in HARVEST_ACTIONS
    )


def job_harvest_name(job: Job) -> str | None:
    """Return the extractor a job names, or None when it is campaign work."""
    return job.payload.get(HARVEST_KEY) if is_harvest_job(job) else None


def job_run_id(job: Job) -> int | None:
    """Return the `harvest_runs` id SEQ-04 recorded on the job, if it has one."""
    value = job.payload.get(RUN_ID_KEY)
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def harvest_jobs(
    conn: sqlite3.Connection,
    account_id: int,
    *,
    limit: int | None = None,
) -> list[Job]:
    """Return an account's harvest jobs, newest first.

    Reads through :func:`~linkedin_mcp.sequences.jobs.list_jobs` and filters in
    Python rather than adding a second query over `jobs`, because the queue's
    SQL belongs to SEQ-01 and its scheduler read belongs to SEQ-04.
    """
    jobs = [job for job in list_jobs(conn, account_id=account_id) if is_harvest_job(job)]
    jobs.sort(key=lambda job: job.id, reverse=True)
    return jobs if limit is None else jobs[: max(0, int(limit))]
