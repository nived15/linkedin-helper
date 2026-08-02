"""MCP-02 (#25): the eleven lead extraction and CRM tools.

Every test here is offline and deterministic. No browser is launched, no
LinkedIn page is fetched, and the one thing these tools are for is proved
directly: a harvest tool returns a job id without ever touching a page.

The database is a temporary SQLite file reached through the audit log, which is
the same seam `linkedin_mcp.tools.runtime` uses in production. Connections are
closed before the temporary directory is torn down, because Windows will not
delete a file SQLite still holds open.
"""

from __future__ import annotations

import ast
import json
import re
import sqlite3
from pathlib import Path

import pytest

from linkedin_mcp.audit import instrument
from linkedin_mcp.audit.log import AuditLog, reset_audit_log, set_audit_log
from linkedin_mcp.core.config import (
    METERED_ACTIONS,
    UNMETERED_ACTIONS,
    is_metered,
)
from linkedin_mcp.leads import count_leads, harvest_leads, list_leads
from linkedin_mcp.scrape import (
    SOURCE_COMPANY_EMPLOYEES,
    SOURCE_CONNECTIONS,
    SOURCE_CSV_IMPORT,
    SOURCE_EVENT_ATTENDEES,
    SOURCE_GROUP_MEMBERS,
    SOURCE_PEOPLE_SEARCH,
    SOURCE_POST_ENGAGERS,
    import_leads_from_csv,
    start_harvest_run,
)
from linkedin_mcp.sequences import JobState, due_jobs, list_jobs
from linkedin_mcp.tools import (
    HARVEST_ACTIONS,
    HARVEST_ENQUEUE_ACTION,
    HARVEST_JOB_PRIORITY,
    HARVEST_KEY,
    LOCAL_HARVEST_ACTIONS,
    RUN_ID_KEY,
    register_lead_tools,
)
from linkedin_mcp.tools.contract import harvest_job_spec
from linkedin_mcp.tools.crm import EXPORT_COLUMNS

fastmcp = pytest.importorskip("fastmcp")

REPO_ROOT = Path(__file__).resolve().parents[1]

HARVEST_TOOLS = (
    "harvest_people_search",
    "harvest_post_engagers",
    "harvest_group_members",
    "harvest_event_attendees",
    "harvest_company_employees",
    "harvest_connections",
    "harvest_import_csv",
    "harvest_status",
)
CRM_TOOLS = ("lead_search", "lead_get", "lead_export_csv")

PEOPLE = (
    {
        "full_name": "Ada Lovelace",
        "public_id": "ada-lovelace",
        "member_id": "urn:li:member:1",
        "headline": "Platform engineer",
        "organization_name": "Analytical Engines",
        "location_name": "London",
    },
    {
        "full_name": "Grace Hopper",
        "public_id": "grace-hopper",
        "member_id": "urn:li:member:2",
        "headline": "Compiler author",
        "organization_name": "US Navy",
        "location_name": "New York",
    },
    {
        "full_name": "Alan Turing",
        "public_id": "alan-turing",
        "member_id": "urn:li:member:3",
        "headline": "Cryptanalyst",
        "organization_name": "Bletchley Park",
        "location_name": "Milton Keynes",
    },
)


@pytest.fixture
def server(tmp_path):
    """A bare FastMCP with only the MCP-02 tools, on a temporary database."""
    log = AuditLog.open(tmp_path / "linkedin-helper.db")
    set_audit_log(log)
    account_id = log.ensure_account("nived@example.com")
    instrument.set_account_resolver(lambda: account_id)

    mcp = fastmcp.FastMCP("linkedin-test")
    register_lead_tools(mcp)
    try:
        yield mcp
    finally:
        reset_audit_log()
        instrument.reset_account_resolver()
        log.close()


@pytest.fixture
def conn(server):
    from linkedin_mcp.tools.runtime import tool_connection

    return tool_connection()


@pytest.fixture
def account_id(server):
    from linkedin_mcp.tools.runtime import tool_account_id

    return tool_account_id()


async def call(mcp, name, **kwargs):
    """Invoke a registered tool by name, through the function MCP would call."""
    tool = await mcp.get_tool(name)
    return await tool.fn(**kwargs)


async def tool_names(mcp):
    return {tool.name for tool in await mcp.list_tools()}


def seed_leads(conn, account_id, people=PEOPLE):
    return harvest_leads(conn, account_id, list(people))


# --------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_eleven_tools_are_registered(server):
    names = await tool_names(server)

    assert set(HARVEST_TOOLS) <= names
    assert set(CRM_TOOLS) <= names
    assert len(set(HARVEST_TOOLS) | set(CRM_TOOLS)) == 11


@pytest.mark.asyncio
async def test_the_entry_point_server_exposes_them_too():
    pytest.importorskip("fastmcp")
    import linkedin_browser_mcp

    names = {tool.name for tool in await linkedin_browser_mcp.mcp.list_tools()}

    assert set(HARVEST_TOOLS) <= names
    assert set(CRM_TOOLS) <= names


@pytest.mark.asyncio
async def test_harvest_sales_nav_is_deliberately_absent(server):
    """SCRAPE-02 is descoped: Sales Navigator needs a paid subscription.

    There is no Sales Navigator extractor in `linkedin_mcp.scrape` to wrap, so
    a `harvest_sales_nav` tool could only pretend. Deleting this test is how a
    later contributor would have to argue with that, which is the point of it.
    """
    names = await tool_names(server)

    assert "harvest_sales_nav" not in names
    assert not any("sales_nav" in name for name in names)
    assert "sales_nav" not in HARVEST_ACTIONS
    assert not any("sales" in name for name in HARVEST_ACTIONS)


@pytest.mark.asyncio
async def test_every_tool_in_the_package_carries_the_audit_decorator(server):
    """The local mirror of the repository-wide guard in tests/test_audit_log.py.

    That guard scans the whole tree. This one scans only this package, so a
    failure here names the module MCP-02 owns rather than a list of everything.
    """
    unaudited: list[str] = []
    for path in sorted((REPO_ROOT / "linkedin_mcp" / "tools").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            names = [
                ast.unparse(
                    decorator.func if isinstance(decorator, ast.Call) else decorator
                )
                for decorator in node.decorator_list
            ]
            if "mcp.tool" in names and "audit_linkedin_action" not in names:
                unaudited.append(f"{path.name}:{node.name}")

    assert unaudited == []
    assert len(await tool_names(server)) == 11


# --------------------------------------------------------------------------
# Enqueue, never scrape inline
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name, kwargs, harvest, action_type",
    [
        (
            "harvest_people_search",
            {"keywords": "platform engineer", "limit": 250},
            SOURCE_PEOPLE_SEARCH,
            "profile_search",
        ),
        (
            "harvest_post_engagers",
            {"post": "https://www.linkedin.com/feed/update/urn:li:activity:7", "limit": 40},
            SOURCE_POST_ENGAGERS,
            "post_read",
        ),
        (
            "harvest_group_members",
            {"group": "12345", "limit": 60},
            SOURCE_GROUP_MEMBERS,
            "profile_search",
        ),
        (
            "harvest_event_attendees",
            {"event": "an-event", "limit": 60},
            SOURCE_EVENT_ATTENDEES,
            "profile_search",
        ),
        (
            "harvest_company_employees",
            {"company": "microsoft", "limit": 60},
            SOURCE_COMPANY_EMPLOYEES,
            "profile_search",
        ),
        ("harvest_connections", {"limit": 60}, SOURCE_CONNECTIONS, "profile_search"),
    ],
)
async def test_a_harvest_tool_returns_a_job_id_without_scraping(
    server, conn, account_id, name, kwargs, harvest, action_type
):
    result = await call(server, name, **kwargs)

    assert result["status"] == "success"
    assert result["harvest"] == harvest
    assert result["action_type"] == action_type
    assert isinstance(result["job_id"], int)

    jobs = list_jobs(conn, account_id=account_id)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.id == result["job_id"]
    assert job.state == JobState.PENDING.value
    assert job.payload[HARVEST_KEY] == harvest
    assert job.attempts == 0


@pytest.mark.asyncio
async def test_a_thousand_profile_harvest_returns_immediately(server, conn, account_id):
    """The headline requirement: a big harvest answers now, not in three hours."""
    result = await call(server, "harvest_people_search", keywords="ai", limit=1000)

    assert result["status"] == "success"
    assert result["payload"]["limit"] == 1000
    assert result["state"] == JobState.PENDING.value
    assert "Nothing has been scraped yet" in result["message"]
    assert list_jobs(conn, account_id=account_id)[0].locked_by is None


@pytest.mark.asyncio
async def test_no_harvest_tool_imports_a_browser_or_awaits_a_runner():
    """Static proof to go with the behavioural one.

    A tool that awaited `run_people_search` would pass the "returns a job id"
    test if it also enqueued, so the absence of the call is checked directly.
    """
    package = REPO_ROOT / "linkedin_mcp" / "tools"
    for path in sorted(package.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        awaited = {
            ast.unparse(node.value.func)
            for node in ast.walk(tree)
            if isinstance(node, ast.Await) and isinstance(node.value, ast.Call)
        }

        assert not any(name.startswith("run_") for name in awaited), path.name
        assert not any(name.endswith(".runner") for name in awaited), path.name
        assert "BrowserSession" not in source, path.name
        assert "playwright" not in source.lower(), path.name


@pytest.mark.asyncio
async def test_the_tool_modules_cannot_call_an_extractor_at_all(server):
    """You cannot call what you never bound.

    `harvest.py` and `crm.py` import no `run_*` function, no CSV importer and
    nothing from the browser package, so there is no name in either module's
    namespace that could start a scrape. `contract.py` does hold the runners,
    because SEQ-04 dispatches on them, and the AST test below is what stops
    this package calling one through the registry.
    """
    import linkedin_mcp.tools.crm as crm_module
    import linkedin_mcp.tools.harvest as harvest_module

    for module in (harvest_module, crm_module):
        bound = set(vars(module))
        assert not {name for name in bound if name.startswith("run_")}, module.__name__
        assert "import_leads_from_csv" not in bound, module.__name__
        assert "BrowserSession" not in bound, module.__name__
        assert "paginate" not in bound, module.__name__


def test_nothing_in_the_package_calls_a_registered_runner():
    """The registry is a lookup table for SEQ-04, not a call site for MCP-02.

    `HARVEST_ACTIONS[name].runner(...)` would be scraping inline through the
    back door, so every `.runner` reference in the package is checked to be a
    read rather than a call.
    """
    package = REPO_ROOT / "linkedin_mcp" / "tools"
    offenders: list[str] = []
    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = node.func
            called = ast.unparse(target)
            if isinstance(target, ast.Attribute) and target.attr == "runner":
                offenders.append(f"{path.name}: {called}")
            if isinstance(target, ast.Name) and target.id.startswith("run_"):
                offenders.append(f"{path.name}: {called}")

    assert offenders == []


@pytest.mark.asyncio
async def test_enqueueing_is_fast_enough_to_be_synchronous(server):
    import time

    started = time.monotonic()
    for index in range(20):
        result = await call(server, "harvest_group_members", group=str(index))
        assert result["status"] == "success"

    assert time.monotonic() - started < 10


# --------------------------------------------------------------------------
# The job shape SEQ-04 (#22) consumes
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_harvest_job_carries_no_campaign_lead_or_step(server, conn, account_id):
    await call(server, "harvest_connections", limit=25)
    job = list_jobs(conn, account_id=account_id)[0]

    assert job.campaign_id is None
    assert job.lead_id is None
    assert job.step_id is None
    assert job.priority == HARVEST_JOB_PRIORITY
    assert job.account_id == account_id


@pytest.mark.asyncio
async def test_a_harvest_job_has_no_derivable_spec(server, conn, account_id):
    """`rebuild_jobs` must never be handed one; it is not campaign state."""
    await call(server, "harvest_connections", limit=25)
    job = list_jobs(conn, account_id=account_id)[0]

    with pytest.raises(ValueError):
        job.spec()


@pytest.mark.asyncio
async def test_due_jobs_does_not_see_harvest_jobs(server, conn, account_id):
    """Documented seam, not a defect of this issue.

    `due_jobs` inner-joins `campaigns` and `campaign_leads`, so a campaign-less
    job is invisible to it. Teaching the runner to lease these is SEQ-04's
    (#22); MCP-02 neither changes `due_jobs` nor opens a second queue.
    """
    await call(server, "harvest_connections", limit=25)

    assert due_jobs(conn, account_id) == []
    assert len(list_jobs(conn, account_id=account_id)) == 1


@pytest.mark.asyncio
async def test_two_harvest_jobs_coexist_despite_the_one_open_job_index(
    server, conn, account_id
):
    """SEQ-01's `0003` index skips NULLs, so ad-hoc jobs are unconstrained."""
    first = await call(server, "harvest_connections", limit=10)
    second = await call(server, "harvest_connections", limit=20)

    assert first["job_id"] != second["job_id"]
    assert len(list_jobs(conn, account_id=account_id)) == 2


@pytest.mark.asyncio
async def test_the_payload_names_the_extractor_seq_04_dispatches_on(
    server, conn, account_id
):
    await call(
        server,
        "harvest_post_engagers",
        post="urn:li:activity:99",
        engagement="reactions",
        limit=30,
    )
    job = list_jobs(conn, account_id=account_id)[0]
    name = job.payload[HARVEST_KEY]

    assert name in HARVEST_ACTIONS
    action = HARVEST_ACTIONS[name]
    assert action.action_type == job.action_type
    assert action.runner.__name__ == "run_post_engager_harvest"
    assert job.payload["post"] == "urn:li:activity:99"
    assert job.payload["engagement"] == "reactions"
    assert job.payload["limit"] == 30


def test_the_payload_is_sorted_json_so_two_identical_harvests_match(account_id):
    first = harvest_job_spec(account_id, SOURCE_CONNECTIONS, {"limit": 10}, now="2026-01-01 00:00:00")
    second = harvest_job_spec(
        account_id, SOURCE_CONNECTIONS, {"limit": 10}, now="2026-01-01 00:00:00"
    )

    assert first == second
    assert json.loads(first.payload_json) == {"harvest": "connections", "limit": 10}


def test_every_registered_harvest_names_a_real_extractor():
    for name, action in HARVEST_ACTIONS.items():
        assert action.name == name
        assert callable(action.runner)
        assert action.source_type
        assert action.tool.startswith("harvest_")


def test_only_the_csv_import_reaches_nothing_on_linkedin():
    local = {name for name, action in HARVEST_ACTIONS.items() if not action.reaches_linkedin}

    assert local == {SOURCE_CSV_IMPORT}
    assert LOCAL_HARVEST_ACTIONS == {SOURCE_CSV_IMPORT}


def test_an_unknown_harvest_name_is_refused_by_the_contract(account_id):
    with pytest.raises(KeyError) as error:
        harvest_job_spec(account_id, "sales_nav", {})

    assert "SCRAPE-02" in str(error.value)


# --------------------------------------------------------------------------
# Action types
# --------------------------------------------------------------------------


def test_no_tool_in_this_package_names_an_action_type_the_config_has_never_heard_of():
    """The `tests/test_limits.py` guard, applied to the modules it cannot see.

    That test reads `linkedin_browser_mcp.py` alone. MCP-02's tools live in
    `linkedin_mcp/tools/`, so the same rule is enforced here for them.
    """
    named: set[str] = set()
    for path in sorted((REPO_ROOT / "linkedin_mcp" / "tools").glob("*.py")):
        source = path.read_text(encoding="utf-8")
        named |= set(re.findall(r'guard_action\(\s*"([a-z_]+)"', source))
        named |= set(re.findall(r'audit_linkedin_action\(\s*"([a-z_]+)"', source))
    named |= {action.action_type for action in HARVEST_ACTIONS.values()}

    assert named
    assert named <= (METERED_ACTIONS | UNMETERED_ACTIONS)


def test_queueing_a_harvest_spends_no_linkedin_budget():
    """Enqueueing writes a row. Metering it would charge one harvest twice."""
    assert not is_metered(HARVEST_ENQUEUE_ACTION)
    assert not is_metered("harvest_status")
    assert not is_metered("lead_read")
    assert not is_metered("lead_export")
    assert not is_metered(SOURCE_CSV_IMPORT)


def test_the_metered_harvests_reuse_the_action_types_scrape_04_chose():
    """No new metered type: engagers are `post_read`, people lists are `profile_search`."""
    metered = {
        action.action_type
        for action in HARVEST_ACTIONS.values()
        if action.reaches_linkedin
    }

    assert metered == {"post_read", "profile_search"}
    assert metered <= METERED_ACTIONS


@pytest.mark.asyncio
async def test_a_queued_harvest_writes_one_unmetered_audit_row(server, conn, account_id):
    await call(server, "harvest_connections", limit=10)

    rows = conn.execute(
        "SELECT action_type, outcome FROM actions_log WHERE account_id = ?",
        (account_id,),
    ).fetchall()

    assert [row["action_type"] for row in rows] == [HARVEST_ENQUEUE_ACTION]
    assert rows[0]["outcome"] == "success"
    assert not is_metered(rows[0]["action_type"])


# --------------------------------------------------------------------------
# Validation happens at enqueue time
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name, kwargs, fragment",
    [
        ("harvest_people_search", {}, "at least one filter"),
        ("harvest_people_search", {"keywords": "ai", "limit": 0}, "at least 1"),
        ("harvest_people_search", {"geo_urns": ["not a facet!"]}, "facet id"),
        ("harvest_post_engagers", {"post": "x", "engagement": "loves"}, "must be one of"),
        ("harvest_post_engagers", {"post": "  "}, "post is required"),
        ("harvest_group_members", {"group": ""}, "group is required"),
        ("harvest_event_attendees", {"event": "e", "tab": "nope"}, "must be one of"),
        ("harvest_company_employees", {"company": ""}, "company is required"),
        ("harvest_import_csv", {"path": "no-such-file.csv"}, "not a readable file"),
    ],
)
async def test_a_bad_argument_fails_at_the_tool_not_in_the_worker(
    server, conn, account_id, name, kwargs, fragment
):
    result = await call(server, name, **kwargs)

    assert result["status"] == "error"
    assert fragment in result["message"]
    assert list_jobs(conn, account_id=account_id) == []


@pytest.mark.asyncio
async def test_a_limit_above_the_platform_ceiling_is_clamped(server, conn, account_id):
    result = await call(server, "harvest_connections", limit=99999)

    assert result["status"] == "success"
    assert result["payload"]["limit"] == 1000


@pytest.mark.asyncio
async def test_people_search_filters_survive_the_round_trip(server, conn, account_id):
    result = await call(
        server,
        "harvest_people_search",
        keywords="developer advocate",
        title="Engineer",
        connection_degrees=["2nd"],
        geo_urns=["urn:li:fsd_geo:103644278"],
        limit=200,
    )
    filters = result["payload"]["filters"]
    stored = list_jobs(conn, account_id=account_id)[0].payload["filters"]

    assert filters["keywords"] == "developer advocate"
    assert filters["title"] == "Engineer"
    assert filters["connection_degrees"] == ["S"]
    assert filters["geo_urns"] == ["103644278"]
    assert stored == filters


# --------------------------------------------------------------------------
# harvest_status
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_harvest_status_reports_a_queued_job_before_the_run_exists(
    server, conn, account_id
):
    queued = await call(server, "harvest_connections", limit=10)

    status = await call(server, "harvest_status", job_id=queued["job_id"])

    assert status["status"] == "success"
    assert status["job_id"] == queued["job_id"]
    assert status["state"] == JobState.PENDING.value
    assert status["harvest"] == SOURCE_CONNECTIONS
    assert status["run"] is None
    assert "has not opened a harvest run yet" in status["message"]


@pytest.mark.asyncio
async def test_harvest_status_reports_progress_for_a_running_harvest(
    server, conn, account_id
):
    """The DoD case: a run that has started but not finished shows its counts."""
    queued = await call(server, "harvest_people_search", keywords="ai", limit=500)
    run_id = start_harvest_run(
        conn,
        account_id,
        SOURCE_PEOPLE_SEARCH,
        {"keywords": "ai"},
        started_at="2026-03-01 09:00:00",
    )
    _attach_run(conn, queued["job_id"], run_id)
    conn.execute(
        "UPDATE harvest_runs SET found_count = ?, new_count = ? WHERE id = ?",
        (120, 80, run_id),
    )
    conn.commit()

    status = await call(server, "harvest_status", job_id=queued["job_id"])

    assert status["run"]["run_id"] == run_id
    assert status["run"]["found_count"] == 120
    assert status["run"]["new_count"] == 80
    assert status["run"]["running"] is True
    assert "120 found, 80 new" in status["message"]


@pytest.mark.asyncio
async def test_harvest_status_reads_a_run_directly(server, conn, account_id):
    run_id = start_harvest_run(conn, account_id, SOURCE_CONNECTIONS, {})

    status = await call(server, "harvest_status", run_id=run_id)

    assert status["status"] == "success"
    assert status["run"]["run_id"] == run_id
    assert status["run"]["running"] is True


@pytest.mark.asyncio
async def test_harvest_status_lists_recent_jobs_when_given_nothing(
    server, conn, account_id
):
    await call(server, "harvest_connections", limit=10)
    await call(server, "harvest_group_members", group="42")

    status = await call(server, "harvest_status")

    assert status["count"] == 2
    assert [job["harvest"] for job in status["jobs"]] == [
        SOURCE_GROUP_MEMBERS,
        SOURCE_CONNECTIONS,
    ]


@pytest.mark.asyncio
async def test_harvest_status_refuses_an_unknown_job(server):
    status = await call(server, "harvest_status", job_id=4242)

    assert status["status"] == "error"
    assert "4242" in status["message"]


@pytest.mark.asyncio
async def test_harvest_status_ignores_campaign_jobs(server, conn, account_id):
    """A campaign step is not a harvest and must not show up in this view."""
    conn.execute(
        """
        INSERT INTO jobs
            (account_id, campaign_id, lead_id, step_id, action_type, payload_json,
             scheduled_for, priority, state, attempts)
        VALUES (?, NULL, NULL, NULL, 'message', '{"step_ord": 1}',
                '2026-01-01 00:00:00', 0, 'pending', 0)
        """,
        (account_id,),
    )
    conn.commit()
    await call(server, "harvest_connections", limit=10)

    status = await call(server, "harvest_status")

    assert status["count"] == 1
    assert status["jobs"][0]["harvest"] == SOURCE_CONNECTIONS


# --------------------------------------------------------------------------
# CRM reads
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lead_search_returns_stored_leads(server, conn, account_id):
    seed_leads(conn, account_id)

    result = await call(server, "lead_search")

    assert result["status"] == "success"
    assert result["count"] == 3
    assert {lead["full_name"] for lead in result["leads"]} == {
        "Ada Lovelace",
        "Grace Hopper",
        "Alan Turing",
    }


@pytest.mark.asyncio
async def test_lead_search_filters_by_text(server, conn, account_id):
    seed_leads(conn, account_id)

    result = await call(server, "lead_search", query="compiler")

    assert result["matched"] == 1
    assert result["leads"][0]["full_name"] == "Grace Hopper"


@pytest.mark.asyncio
async def test_lead_search_filters_by_tag(server, conn, account_id):
    from linkedin_mcp.leads import add_tag

    summary = seed_leads(conn, account_id)
    add_tag(conn, summary.lead_ids[0], "priority")

    result = await call(server, "lead_search", tags=["priority"])

    assert result["matched"] == 1
    assert result["leads"][0]["id"] == summary.lead_ids[0]


@pytest.mark.asyncio
async def test_lead_search_pages(server, conn, account_id):
    seed_leads(conn, account_id)

    first = await call(server, "lead_search", limit=2, offset=0)
    second = await call(server, "lead_search", limit=2, offset=2)

    assert first["count"] == 2
    assert second["count"] == 1
    assert first["matched"] == 3


@pytest.mark.asyncio
async def test_lead_search_hides_blacklisted_people_by_default(
    server, conn, account_id
):
    from linkedin_mcp.leads import blacklist_lead

    summary = seed_leads(conn, account_id)
    blacklist_lead(conn, summary.lead_ids[0], reason="asked to stop")

    hidden = await call(server, "lead_search")
    shown = await call(server, "lead_search", include_blacklisted=True)

    assert hidden["matched"] == 2
    assert shown["matched"] == 3


@pytest.mark.asyncio
async def test_lead_get_reads_one_lead_by_every_identifier(server, conn, account_id):
    summary = seed_leads(conn, account_id)
    lead_id = summary.lead_ids[0]

    by_id = await call(server, "lead_get", lead_id=lead_id)
    by_public = await call(server, "lead_get", public_id="ada-lovelace")
    by_member = await call(server, "lead_get", member_id="urn:li:member:1")

    assert by_id["lead"]["id"] == lead_id
    assert by_public["lead"]["id"] == lead_id
    assert by_member["lead"]["id"] == lead_id
    assert by_id["lead"]["profile_url"] == "https://www.linkedin.com/in/ada-lovelace"
    assert by_id["blacklisted"] is False


@pytest.mark.asyncio
async def test_lead_get_carries_tags_and_custom_fields(server, conn, account_id):
    from linkedin_mcp.leads import add_tag, set_custom_field

    summary = seed_leads(conn, account_id)
    lead_id = summary.lead_ids[1]
    add_tag(conn, lead_id, "icp")
    set_custom_field(conn, lead_id, "industry", "defence")

    result = await call(server, "lead_get", lead_id=lead_id)

    assert result["tags"] == ["icp"]
    assert result["custom_fields"] == {"industry": "defence"}


@pytest.mark.asyncio
async def test_lead_get_needs_an_identifier(server):
    result = await call(server, "lead_get")

    assert result["status"] == "error"
    assert "lead_id" in result["message"]


@pytest.mark.asyncio
async def test_lead_get_refuses_a_lead_that_is_not_there(server):
    result = await call(server, "lead_get", lead_id=9999)

    assert result["status"] == "error"
    assert "No lead matches" in result["message"]


# --------------------------------------------------------------------------
# Export / import round trip
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lead_export_csv_writes_the_columns_the_importer_reads(
    server, conn, account_id, tmp_path
):
    seed_leads(conn, account_id)
    target = tmp_path / "leads.csv"

    result = await call(server, "lead_export_csv", path=str(target))

    assert result["status"] == "success"
    assert result["count"] == 3
    header = target.read_text(encoding="utf-8").splitlines()[0].split(",")
    assert header == list(EXPORT_COLUMNS)


@pytest.mark.asyncio
async def test_lead_export_csv_returns_text_when_given_no_path(
    server, conn, account_id
):
    seed_leads(conn, account_id)

    result = await call(server, "lead_export_csv")

    assert result["path"] is None
    assert result["csv"].splitlines()[0].startswith("profile_url")


@pytest.mark.asyncio
async def test_export_then_reimport_does_not_duplicate_leads(
    server, conn, account_id, tmp_path
):
    """The round trip DB-03 makes safe, proved end to end.

    The export is written by `lead_export_csv`, queued by `harvest_import_csv`,
    and then run exactly the way SEQ-04 will run it: by handing the job payload
    to the runner the contract names. Nothing here re-implements dedupe; the
    lead count is simply expected not to move.
    """
    seed_leads(conn, account_id)
    before = count_leads(conn, account_id)
    target = tmp_path / "round-trip.csv"

    exported = await call(server, "lead_export_csv", path=str(target))
    assert exported["count"] == before

    queued = await call(server, "harvest_import_csv", path=str(target))
    assert queued["status"] == "success"
    assert queued["harvest"] == SOURCE_CSV_IMPORT

    job = next(
        job for job in list_jobs(conn, account_id=account_id) if job.id == queued["job_id"]
    )
    summary = _run_queued_csv_import(conn, account_id, job)

    assert summary.rows == before
    assert summary.imported == before
    assert summary.leads_created == 0
    assert count_leads(conn, account_id) == before


@pytest.mark.asyncio
async def test_a_second_import_of_the_same_file_still_creates_nobody(
    server, conn, account_id, tmp_path
):
    seed_leads(conn, account_id)
    target = tmp_path / "twice.csv"
    await call(server, "lead_export_csv", path=str(target))
    queued = await call(server, "harvest_import_csv", path=str(target))
    job = next(
        job for job in list_jobs(conn, account_id=account_id) if job.id == queued["job_id"]
    )

    first = _run_queued_csv_import(conn, account_id, job)
    second = _run_queued_csv_import(conn, account_id, job)

    assert first.leads_created == 0
    assert second.leads_created == 0
    assert count_leads(conn, account_id) == 3
    assert len(list_leads(conn, account_id)) == 3


@pytest.mark.asyncio
async def test_the_same_person_from_two_sources_is_still_one_lead(
    server, conn, account_id, tmp_path
):
    """DB-03's property, preserved through the export path."""
    seed_leads(conn, account_id, PEOPLE[:1])
    target = tmp_path / "one.csv"
    await call(server, "lead_export_csv", path=str(target))

    harvest_leads(
        conn,
        account_id,
        [{"full_name": "Ada Lovelace", "member_id": "urn:li:member:1"}],
    )
    queued = await call(server, "harvest_import_csv", path=str(target))
    job = next(
        job for job in list_jobs(conn, account_id=account_id) if job.id == queued["job_id"]
    )
    _run_queued_csv_import(conn, account_id, job)

    assert count_leads(conn, account_id) == 1


def _attach_run(conn: sqlite3.Connection, job_id: int, run_id: int) -> None:
    """Do what SEQ-04 must do: record the run id back onto the job payload."""
    row = conn.execute("SELECT payload_json FROM jobs WHERE id = ?", (job_id,)).fetchone()
    payload = json.loads(row["payload_json"])
    payload[RUN_ID_KEY] = run_id
    conn.execute(
        "UPDATE jobs SET payload_json = ? WHERE id = ?",
        (json.dumps(payload, sort_keys=True), job_id),
    )
    conn.commit()


def _run_queued_csv_import(conn, account_id, job):
    """Run a queued CSV import the way the contract says SEQ-04 will.

    Dispatch on the payload's `harvest` key, look the runner up in
    `HARVEST_ACTIONS`, and pass the payload through. This is the test standing
    in for the worker, not a second worker: it calls the merged extractor
    unchanged.
    """
    action = HARVEST_ACTIONS[job.payload[HARVEST_KEY]]
    assert action.runner is import_leads_from_csv
    return action.runner(
        conn,
        account_id,
        job.payload["path"],
        encoding=job.payload["encoding"],
        delimiter=job.payload["delimiter"],
    )
