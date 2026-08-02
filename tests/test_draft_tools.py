"""SEQ-05: `drafts_list_pending`, `drafts_submit` and `drafts_approve`.

The round trip is exercised through a real `FastMCP` instance and a real
in-memory MCP client, not by calling the Python functions, because the thing
being claimed is that an MCP client can drive this. `linkedin_browser_mcp.py`
belongs to MCP-02 (#25), so these tests construct their own server, call
`register_draft_tools` on it, and prove the path end to end.
"""

import ast
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastmcp import Client, FastMCP

from linkedin_mcp.audit import instrument
from linkedin_mcp.audit.log import AuditLog, reset_audit_log, set_audit_log
from linkedin_mcp.core.config import DRAFT_ACTIONS, METERED_ACTIONS, UNMETERED_ACTIONS
from linkedin_mcp.drafts import (
    DRAFT_ACTION_TYPES,
    STATUS_APPROVED,
    STATUS_NEEDS_GENERATION,
    STATUS_PENDING_APPROVAL,
    STATUS_REJECTED,
    approved_text,
    register_draft_tools,
    request_draft,
    reset_draft_connection,
    set_draft_connection,
)
from linkedin_mcp.drafts.errors import DraftNotApprovedError
from linkedin_mcp.leads import create_lead
from linkedin_mcp.safety.limits import (
    global_actions_in_window,
    metered_universe,
    observed_action_types,
)
from linkedin_mcp.sequences import StepSpec, create_campaign, define_steps, enrol_lead

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOLS_MODULE = REPO_ROOT / "linkedin_mcp" / "drafts" / "tools.py"
BASE_TIME = datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc)
DAY = timedelta(days=1)

GOOD_NOTE = (
    "Nived here, from the GitHub Copilot side at Microsoft. Saw your platform "
    "team is midway through a rollout. Happy to compare notes on what sticks."
)
EM_DASH_NOTE = "Nived here \u2014 happy to compare notes on Copilot rollouts."
TOOL_NAMES = ("drafts_list_pending", "drafts_submit", "drafts_approve")


@pytest.fixture()
def audit(tmp_path):
    log = AuditLog.open(tmp_path / "linkedin-helper.db")
    set_audit_log(log)
    set_draft_connection(log.connection)
    try:
        yield log
    finally:
        reset_draft_connection()
        reset_audit_log()
        instrument.reset_account_resolver()
        log.close()


@pytest.fixture()
def conn(audit):
    return audit.connection


@pytest.fixture()
def account(audit):
    resolved = audit.ensure_account("nived@example.com")
    instrument.set_account_resolver(lambda: resolved)
    return resolved


@pytest.fixture()
def lead(conn, account):
    return create_lead(
        conn,
        account,
        "Ada Lovelace",
        public_id="ada-lovelace",
        first_name="Ada",
        organization_name="Contoso",
    ).id


@pytest.fixture()
def campaign(conn, account):
    created = create_campaign(conn, account, "Q3 platform teams", status="active")
    define_steps(conn, created.id, [StepSpec("connection_request")])
    return created


@pytest.fixture()
def server(audit):
    """A FastMCP instance with only the draft tools attached to it."""
    mcp = FastMCP("drafts-under-test")
    register_draft_tools(mcp)
    return mcp


def payload(result):
    """Return the dict an in-memory MCP tool call produced."""
    return result.data


# --------------------------------------------------------------------------
# The round trip the definition of done names
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_submit_approve_round_trips_with_an_mcp_client(
    server, conn, account, campaign, lead
):
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    parked = request_draft(conn, campaign.id, lead, "connection_note")

    async with Client(server) as client:
        assert {tool.name for tool in await client.list_tools()} == set(TOOL_NAMES)

        listed = payload(await client.call_tool("drafts_list_pending", {}))
        assert listed["status"] == "success"
        assert listed["queue"] == STATUS_NEEDS_GENERATION
        assert [draft["id"] for draft in listed["drafts"]] == [parked.id]
        assert listed["drafts"][0]["context"]["lead"]["first_name"] == "Ada"

        submitted = payload(
            await client.call_tool(
                "drafts_submit",
                {"draft_id": parked.id, "text": GOOD_NOTE, "model": "claude-opus-5"},
            )
        )
        assert submitted["status"] == "success"
        assert submitted["draft"]["status"] == STATUS_PENDING_APPROVAL

        with pytest.raises(DraftNotApprovedError):
            approved_text(conn, parked.id)

        approved = payload(
            await client.call_tool("drafts_approve", {"draft_id": parked.id})
        )
        assert approved["draft"]["status"] == STATUS_APPROVED

    assert approved_text(conn, parked.id) == GOOD_NOTE


@pytest.mark.asyncio
async def test_the_review_queue_is_the_same_tool_with_a_different_status(
    server, conn, account, campaign, lead
):
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    first = request_draft(conn, campaign.id, lead, "connection_note")
    second = request_draft(conn, campaign.id, lead, "comment")

    async with Client(server) as client:
        await client.call_tool("drafts_submit", {"draft_id": second.id, "text": GOOD_NOTE})

        generation = payload(await client.call_tool("drafts_list_pending", {}))
        review = payload(
            await client.call_tool(
                "drafts_list_pending", {"status": STATUS_PENDING_APPROVAL}
            )
        )

    assert [draft["id"] for draft in generation["drafts"]] == [first.id]
    assert [draft["id"] for draft in review["drafts"]] == [second.id]


@pytest.mark.asyncio
async def test_an_icp_verdict_round_trips_through_the_tools(
    server, conn, account, campaign, lead
):
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    parked = request_draft(conn, campaign.id, lead, "icp_evaluation")
    verdict = {"match": True, "score": 0.77, "reason": "Owns internal developer tooling."}

    async with Client(server) as client:
        submitted = payload(
            await client.call_tool(
                "drafts_submit", {"draft_id": parked.id, "verdict": verdict}
            )
        )
        approved = payload(
            await client.call_tool("drafts_approve", {"draft_id": parked.id})
        )

    assert submitted["draft"]["verdict"] == verdict
    assert approved["draft"]["status"] == STATUS_APPROVED


@pytest.mark.asyncio
async def test_rejecting_through_the_tool_blocks_the_text(
    server, conn, account, campaign, lead
):
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    parked = request_draft(conn, campaign.id, lead, "connection_note")

    async with Client(server) as client:
        await client.call_tool("drafts_submit", {"draft_id": parked.id, "text": GOOD_NOTE})
        rejected = payload(
            await client.call_tool(
                "drafts_approve",
                {"draft_id": parked.id, "approved": False, "note": "too salesy"},
            )
        )

    assert rejected["draft"]["status"] == STATUS_REJECTED
    with pytest.raises(DraftNotApprovedError):
        approved_text(conn, parked.id)


@pytest.mark.asyncio
async def test_an_em_dash_is_refused_at_the_tool_boundary(
    server, conn, account, campaign, lead
):
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    parked = request_draft(conn, campaign.id, lead, "connection_note")

    async with Client(server) as client:
        result = payload(
            await client.call_tool(
                "drafts_submit", {"draft_id": parked.id, "text": EM_DASH_NOTE}
            )
        )
        still_queued = payload(await client.call_tool("drafts_list_pending", {}))

    assert result["status"] == "error"
    assert result["reason"] == "DraftStyleError"
    assert "forbidden dash" in result["message"]
    assert [draft["id"] for draft in still_queued["drafts"]] == [parked.id]


@pytest.mark.asyncio
async def test_the_tools_report_bad_input_instead_of_raising(server, account):
    async with Client(server) as client:
        unknown_status = payload(
            await client.call_tool("drafts_list_pending", {"status": "whenever"})
        )
        unknown_kind = payload(
            await client.call_tool("drafts_list_pending", {"kind": "carrier_pigeon"})
        )
        missing = payload(await client.call_tool("drafts_submit", {"draft_id": 4242, "text": "hi"}))
        cannot_approve = payload(await client.call_tool("drafts_approve", {"draft_id": 4242}))

    assert unknown_status["status"] == "error"
    assert unknown_kind["status"] == "error"
    assert missing["reason"] == "DraftNotFoundError"
    assert cannot_approve["reason"] == "DraftNotFoundError"


@pytest.mark.asyncio
async def test_the_list_limit_is_capped(server, conn, account, campaign, lead):
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    for _ in range(3):
        request_draft(conn, campaign.id, lead, "comment")

    async with Client(server) as client:
        one = payload(await client.call_tool("drafts_list_pending", {"limit": 1}))
        huge = payload(await client.call_tool("drafts_list_pending", {"limit": 10_000}))

    assert one["count"] == 1
    assert huge["count"] == 3


def test_register_draft_tools_returns_the_three_callables(audit):
    mcp = FastMCP("returned")
    registered = register_draft_tools(mcp)

    assert set(registered) == set(TOOL_NAMES)
    assert all(callable(func) for func in registered.values())


@pytest.mark.asyncio
async def test_tools_can_be_registered_on_more_than_one_server(audit):
    first = FastMCP("first")
    second = FastMCP("second")

    register_draft_tools(first)
    register_draft_tools(second)

    async with Client(first) as client:
        assert {tool.name for tool in await client.list_tools()} == set(TOOL_NAMES)
    async with Client(second) as client:
        assert {tool.name for tool in await client.list_tools()} == set(TOOL_NAMES)


# --------------------------------------------------------------------------
# Instrumentation, and the budget it must not spend
# --------------------------------------------------------------------------


def decorator_names(node: ast.AST) -> list[str]:
    names = []
    for decorator in getattr(node, "decorator_list", []):
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(target, ast.Attribute):
            prefix = target.value.id if isinstance(target.value, ast.Name) else ""
            names.append(f"{prefix}.{target.attr}" if prefix else target.attr)
        elif isinstance(target, ast.Name):
            names.append(target.id)
    return names


def test_every_draft_tool_is_instrumented():
    """Pre-empts the audit guard tightening to scan every tool-registering module."""
    tree = ast.parse(TOOLS_MODULE.read_text(encoding="utf-8"))
    tools = {
        node.name: decorator_names(node)
        for node in ast.walk(tree)
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
        and "mcp.tool" in decorator_names(node)
    }

    assert set(tools) == set(TOOL_NAMES)
    assert [name for name, decorators in tools.items() if "audit_linkedin_action" not in decorators] == []


def test_no_draft_tool_names_an_action_the_config_has_never_heard_of():
    source = TOOLS_MODULE.read_text(encoding="utf-8")
    named = set(re.findall(r'audit_linkedin_action\(\s*([A-Z_]+)', source))

    assert named == {"DRAFT_LIST_ACTION", "DRAFT_SUBMIT_ACTION", "DRAFT_APPROVE_ACTION"}
    assert set(DRAFT_ACTION_TYPES) <= (METERED_ACTIONS | UNMETERED_ACTIONS)


def test_draft_actions_are_unmetered_so_they_cannot_eat_the_linkedin_budget():
    """The metered universe is closed by exclusion, so this has to be explicit."""
    assert set(DRAFT_ACTION_TYPES) == set(DRAFT_ACTIONS)
    assert set(DRAFT_ACTION_TYPES) <= UNMETERED_ACTIONS
    assert METERED_ACTIONS.isdisjoint(DRAFT_ACTION_TYPES)
    assert metered_universe(None, DRAFT_ACTION_TYPES).isdisjoint(DRAFT_ACTION_TYPES)


@pytest.mark.asyncio
async def test_a_full_icp_qualification_spends_no_linkedin_budget(
    server, conn, account, campaign, lead
):
    """Requirement asserted end to end, through the real tool call path.

    Every row the tools wrote is audited, and not one of them counts against the
    account's daily or hourly ceilings. That is what makes running the gate
    before the invite step affordable.
    """
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    parked = request_draft(conn, campaign.id, lead, "icp_evaluation")
    verdict = {"match": False, "score": 0.12, "reason": "No tooling remit."}

    async with Client(server) as client:
        await client.call_tool("drafts_list_pending", {})
        await client.call_tool("drafts_submit", {"draft_id": parked.id, "verdict": verdict})
        await client.call_tool("drafts_approve", {"draft_id": parked.id})

    observed = observed_action_types(conn, account)
    universe = metered_universe(None, observed)

    assert observed == set(DRAFT_ACTION_TYPES)
    assert universe.isdisjoint(observed)
    assert (
        global_actions_in_window(account, window=DAY, action_types=universe) == 0
    )


@pytest.mark.asyncio
async def test_the_audit_trail_records_who_generated_and_who_approved(
    server, conn, account, campaign, lead
):
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    parked = request_draft(conn, campaign.id, lead, "connection_note")

    async with Client(server) as client:
        await client.call_tool(
            "drafts_submit",
            {"draft_id": parked.id, "text": GOOD_NOTE, "model": "claude-opus-5"},
        )
        await client.call_tool("drafts_approve", {"draft_id": parked.id})

    rows = {
        row["action_type"]: row["detail_json"]
        for row in conn.execute(
            "SELECT action_type, detail_json FROM actions_log ORDER BY id"
        ).fetchall()
    }

    assert "claude-opus-5" in rows["draft_submit"]
    assert str(parked.id) in rows["draft_submit"]
    assert "draft_approve" in rows


@pytest.mark.asyncio
async def test_a_refused_submission_is_still_audited(server, conn, account, campaign, lead):
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    parked = request_draft(conn, campaign.id, lead, "connection_note")

    async with Client(server) as client:
        await client.call_tool("drafts_submit", {"draft_id": parked.id, "text": EM_DASH_NOTE})

    row = conn.execute(
        "SELECT outcome FROM actions_log WHERE action_type = 'draft_submit'"
    ).fetchone()

    assert row["outcome"] == "failure"


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------


def test_the_tools_module_does_not_touch_the_mcp_entry_point():
    """MCP-02 (#25) owns `linkedin_browser_mcp.py`; this package must not.

    Checked against the parsed imports rather than the raw text, so the module
    is free to explain the ownership boundary in its own docstring.
    """
    tree = ast.parse(TOOLS_MODULE.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    assert "linkedin_browser_mcp" not in imported
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "FastMCP"
        for node in ast.walk(tree)
    )


def test_the_connection_resolver_is_replaceable(audit, tmp_path):
    """So a test, or MCP-03, can point the tools at a different database."""
    other = AuditLog.open(tmp_path / "other.db")
    try:
        mcp = FastMCP("injected")
        registered = register_draft_tools(mcp, connection_factory=lambda: other.connection)
        assert set(registered) == set(TOOL_NAMES)
    finally:
        other.close()
