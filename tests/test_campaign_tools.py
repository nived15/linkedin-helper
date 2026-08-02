"""MCP-01 (#24): the twelve campaign control tools.

Everything here drives a real `FastMCP` instance through a real in-memory MCP
client, because the claim is that an MCP client can control a campaign end to
end. `linkedin_browser_mcp.py` belongs to MCP-03 (#26), so these tests build
their own server, call `register_campaign_tools` on it, and prove the path.

The load-bearing claim is the gate. `RUNNABLE_STATUSES` is `{"active"}` and
`due_jobs` inner-joins `campaigns` on it, so "an unapproved campaign cannot
start" is enforced by the read the worker performs, not by a flag these tools
set. Several tests below therefore assert against `due_jobs` itself rather than
against a tool's own return value, which is the thing that would still be true
if every one of these tools were deleted.
"""

import ast
import re
import socket
from pathlib import Path

import pytest
from fastmcp import Client, FastMCP

from linkedin_mcp.audit import instrument
from linkedin_mcp.audit.log import AuditLog, reset_audit_log, set_audit_log
from linkedin_mcp.core.config import METERED_ACTIONS, UNMETERED_ACTIONS, is_metered
from linkedin_mcp.leads import add_tags, create_lead, ensure_tag
from linkedin_mcp.sequences import (
    RUNNABLE_STATUSES,
    Sublist,
    due_jobs,
    get_campaign,
    list_campaign_leads,
    list_steps,
)
from linkedin_mcp.templating import create_template
from linkedin_mcp.tools.campaigns import (
    CAMPAIGN_ACTION_TYPES,
    MAX_PREVIEW_SAMPLES,
    STATUS_ACTIVE,
    STATUS_APPROVED,
    STATUS_ARCHIVED,
    STATUS_DRAFT,
    STATUS_PAUSED,
    TRANSLATED_ERRORS,
    CampaignErrorReason,
    CampaignToolError,
    campaign_error_classes,
    register_campaign_tools,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE = REPO_ROOT / "linkedin_mcp" / "tools" / "campaigns.py"
MIGRATIONS = REPO_ROOT / "linkedin_mcp" / "core" / "migrations"

GOOD_NOTE = (
    "{IF firstName}Hi {firstName},{ELSE}Hi there,{END} saw your team at "
    "{company} is midway through a Copilot rollout. Happy to compare notes."
)
NEEDS_COMPANY = "Hi {firstName}, how is the rollout at {company} going?"

TOOL_NAMES = (
    "campaign_create",
    "campaign_add_step",
    "campaign_set_template",
    "campaign_set_icp",
    "campaign_preview",
    "campaign_approve",
    "campaign_start",
    "campaign_pause",
    "campaign_resume",
    "campaign_archive",
    "campaign_status",
    "campaign_add_leads",
)


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture()
def audit(tmp_path):
    log = AuditLog.open(tmp_path / "linkedin-helper.db")
    set_audit_log(log)
    try:
        yield log
    finally:
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
def server(audit):
    """A FastMCP instance carrying only the campaign tools."""
    mcp = FastMCP("campaigns-under-test")
    register_campaign_tools(mcp)
    return mcp


@pytest.fixture()
def template(conn, account):
    return create_template(conn, account, "invite", GOOD_NOTE)


@pytest.fixture()
def leads(conn, account):
    ada = create_lead(
        conn,
        account,
        "Ada Lovelace",
        public_id="ada-lovelace",
        first_name="Ada",
        organization_name="Contoso",
    )
    grace = create_lead(
        conn,
        account,
        "Grace Hopper",
        public_id="grace-hopper",
        first_name="Grace",
        organization_name="Fabrikam",
    )
    return [ada.id, grace.id]


def payload(result):
    """Return the dict an in-memory MCP tool call produced."""
    return result.data


async def call(client, _tool, **args):
    return payload(await client.call_tool(_tool, args))


async def ready_campaign(client, template_id, lead_ids, *, name="Q3 platform teams"):
    """Create, define, enrol. Stops short of approval so the gate stays testable."""
    created = await call(client, "campaign_create", name=name)
    campaign_id = created["campaign"]["id"]
    await call(
        client,
        "campaign_add_step",
        campaign_id=campaign_id,
        action_type="connection_request",
        template_id=template_id,
        config={"delay_seconds": 0},
    )
    await call(
        client, "campaign_add_leads", campaign_id=campaign_id, lead_ids=lead_ids
    )
    return campaign_id


# --------------------------------------------------------------------------
# The round trip the definition of done names
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_twelve_tools_round_trip_through_an_mcp_client(
    server, conn, account, template, leads
):
    """Create, define, preview, approve, start, pause, resume, archive."""
    async with Client(server) as client:
        assert {tool.name for tool in await client.list_tools()} == set(TOOL_NAMES)

        created = await call(client, "campaign_create", name="Q3 platform teams")
        assert created["status"] == "success"
        campaign_id = created["campaign"]["id"]
        assert created["campaign"]["status"] == STATUS_DRAFT

        step = await call(
            client,
            "campaign_add_step",
            campaign_id=campaign_id,
            action_type="connection_request",
            template_id=template.id,
            config={"delay_seconds": 0},
        )
        assert step["status"] == "success"
        assert step["step"]["action_type"] == "connection_request"

        icp = await call(
            client,
            "campaign_set_icp",
            campaign_id=campaign_id,
            description="Platform engineering leaders at large software companies",
            required_fields=["headline"],
            threshold=0.6,
        )
        assert icp["status"] == "success"
        assert icp["step"]["ord"] == 1, "the gate qualifies before an invite is spent"
        assert icp["icp"]["description"].startswith("Platform engineering")

        retargeted = await call(
            client, "campaign_set_template", campaign_id=campaign_id, template="invite"
        )
        assert retargeted["status"] == "success"
        assert retargeted["template"]["id"] == template.id

        added = await call(
            client, "campaign_add_leads", campaign_id=campaign_id, lead_ids=leads
        )
        assert added["status"] == "success"
        assert sorted(added["enrolled"]) == sorted(leads)

        preview = await call(client, "campaign_preview", campaign_id=campaign_id)
        assert preview["status"] == "success"
        assert preview["refused"] == 0
        assert [sample["text"] for sample in preview["samples"]] == [
            "Hi Ada, saw your team at Contoso is midway through a Copilot "
            "rollout. Happy to compare notes.",
            "Hi Grace, saw your team at Fabrikam is midway through a Copilot "
            "rollout. Happy to compare notes.",
        ]

        approved = await call(client, "campaign_approve", campaign_id=campaign_id)
        assert approved["status"] == "success"
        assert approved["campaign"]["status"] == STATUS_APPROVED

        started = await call(client, "campaign_start", campaign_id=campaign_id)
        assert started["status"] == "success"
        assert started["campaign"]["status"] == STATUS_ACTIVE
        assert started["due_now"] == 2

        paused = await call(client, "campaign_pause", campaign_id=campaign_id)
        assert paused["campaign"]["status"] == STATUS_PAUSED
        assert paused["open_jobs"] == 2, "pausing keeps the queue"

        resumed = await call(client, "campaign_resume", campaign_id=campaign_id)
        assert resumed["campaign"]["status"] == STATUS_ACTIVE

        state = await call(client, "campaign_status", campaign_id=campaign_id)
        assert state["funnel"]["queue"] == 2
        assert state["due_now"] == 2
        assert state["runnable_statuses"] == sorted(RUNNABLE_STATUSES)

        archived = await call(client, "campaign_archive", campaign_id=campaign_id)
        assert archived["campaign"]["status"] == STATUS_ARCHIVED

    assert get_campaign(conn, campaign_id).status == STATUS_ARCHIVED


@pytest.mark.asyncio
async def test_every_tool_is_reachable_by_name_and_returns_a_typed_refusal(
    server, account
):
    """A missing campaign id is a typed `campaign_not_found` from every tool.

    Reaching all twelve by name is what proves registration, and reaching them
    with an id that does not exist is what proves none of them crashes the
    client with a raw traceback.
    """
    arguments = {
        "campaign_create": {"name": ""},
        "campaign_add_step": {"campaign_id": 4242, "action_type": "message"},
        "campaign_set_template": {"campaign_id": 4242, "template": "invite"},
        "campaign_set_icp": {"campaign_id": 4242, "description": "anyone"},
        "campaign_preview": {"campaign_id": 4242},
        "campaign_approve": {"campaign_id": 4242},
        "campaign_start": {"campaign_id": 4242},
        "campaign_pause": {"campaign_id": 4242},
        "campaign_resume": {"campaign_id": 4242},
        "campaign_archive": {"campaign_id": 4242},
        "campaign_status": {"campaign_id": 4242},
        "campaign_add_leads": {"campaign_id": 4242, "lead_ids": [1]},
    }
    assert set(arguments) == set(TOOL_NAMES)
    known = {reason.value for reason in CampaignErrorReason}

    async with Client(server) as client:
        for name, args in arguments.items():
            result = await call(client, name, **args)
            assert result["status"] == "error", name
            assert result["reason"] in known, name
            assert result["error"], name
            assert result["message"], name

        # `campaign_create` is the one tool with no campaign id to be wrong
        # about, so it is checked against its own refusal.
        blank = await call(client, "campaign_create", name="")
        assert blank["reason"] == CampaignErrorReason.INVALID_ARGUMENT.value


# --------------------------------------------------------------------------
# The gate: approval, and what the worker can actually see
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unapproved_campaign_cannot_start_and_the_worker_sees_nothing(
    server, conn, account, template, leads
):
    """The DoD's central claim, checked at the read layer the worker uses."""
    async with Client(server) as client:
        campaign_id = await ready_campaign(client, template.id, leads)

        assert due_jobs(conn, account) == [], "a draft is invisible to the worker"

        denied = await call(client, "campaign_start", campaign_id=campaign_id)
        assert denied["status"] == "error"
        assert denied["reason"] == CampaignErrorReason.CAMPAIGN_NOT_APPROVED.value
        assert get_campaign(conn, campaign_id).status == STATUS_DRAFT
        assert due_jobs(conn, account) == []

        await call(client, "campaign_approve", campaign_id=campaign_id)
        assert due_jobs(conn, account) == [], "approval alone starts nothing"

        await call(client, "campaign_start", campaign_id=campaign_id)
        assert len(due_jobs(conn, account)) == 2


@pytest.mark.asyncio
async def test_campaign_start_only_flips_a_status_column(
    server, conn, account, template, leads
):
    """Starting writes one column. The jobs it exposes already existed."""
    async with Client(server) as client:
        campaign_id = await ready_campaign(client, template.id, leads)
        await call(client, "campaign_approve", campaign_id=campaign_id)

        before = get_campaign(conn, campaign_id)
        steps_before = [
            (step.id, step.ord, step.action_type, step.template_id)
            for step in list_steps(conn, campaign_id)
        ]
        jobs_before = [
            (job.id, job.state, job.scheduled_for)
            for job in due_jobs(conn, account, campaign_id=campaign_id)
        ]
        queued_before = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE campaign_id = ?", (campaign_id,)
        ).fetchone()[0]

        started = await call(client, "campaign_start", campaign_id=campaign_id)
        after = get_campaign(conn, campaign_id)

        assert started["changed"] is True
        assert started["previous_status"] == STATUS_APPROVED
        assert jobs_before == [], "nothing was due while the campaign was gated"
        assert conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE campaign_id = ?", (campaign_id,)
        ).fetchone()[0] == queued_before, "starting queued no new work"
        assert queued_before == 2, "the queue was written at enrolment, not at start"
        assert after.status == STATUS_ACTIVE
        assert after.name == before.name
        assert steps_before == [
            (step.id, step.ord, step.action_type, step.template_id)
            for step in list_steps(conn, campaign_id)
        ], "the definition was untouched"


@pytest.mark.asyncio
async def test_pause_is_load_bearing_and_resume_restores_the_queue(
    server, conn, account, template, leads
):
    """Pausing genuinely stops the worker rather than only labelling it."""
    async with Client(server) as client:
        campaign_id = await ready_campaign(client, template.id, leads)
        await call(client, "campaign_approve", campaign_id=campaign_id)
        await call(client, "campaign_start", campaign_id=campaign_id)
        assert len(due_jobs(conn, account)) == 2

        await call(client, "campaign_pause", campaign_id=campaign_id)
        assert due_jobs(conn, account) == []
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE campaign_id = ?",
                (campaign_id,),
            ).fetchone()[0]
            == 2
        ), "the queue survived the pause"

        await call(client, "campaign_resume", campaign_id=campaign_id)
        assert len(due_jobs(conn, account)) == 2


@pytest.mark.asyncio
async def test_archiving_takes_a_campaign_out_of_the_worker_s_reach(
    server, conn, account, template, leads
):
    async with Client(server) as client:
        campaign_id = await ready_campaign(client, template.id, leads)
        await call(client, "campaign_approve", campaign_id=campaign_id)
        await call(client, "campaign_start", campaign_id=campaign_id)

        archived = await call(client, "campaign_archive", campaign_id=campaign_id)
        assert archived["funnel"]["total"] == 2, "history is kept"
        assert due_jobs(conn, account) == []

        restart = await call(client, "campaign_start", campaign_id=campaign_id)
        assert restart["reason"] == CampaignErrorReason.CAMPAIGN_STATUS_CONFLICT.value


@pytest.mark.asyncio
async def test_a_campaign_cannot_be_created_already_running(server, conn, account):
    """There is no argument that would let a caller skip the gate at creation."""
    schema = None
    async with Client(server) as client:
        for tool in await client.list_tools():
            if tool.name == "campaign_create":
                schema = tool.inputSchema
        assert schema is not None
        assert "status" not in schema.get("properties", {})

        created = await call(client, "campaign_create", name="No shortcuts")
    assert get_campaign(conn, created["campaign"]["id"]).status == STATUS_DRAFT


@pytest.mark.asyncio
async def test_approval_freezes_the_definition_and_revoking_reopens_it(
    server, conn, account, template, leads
):
    """An approval is a signature. The document cannot change underneath it."""
    async with Client(server) as client:
        campaign_id = await ready_campaign(client, template.id, leads)
        await call(client, "campaign_approve", campaign_id=campaign_id)

        for name, args in (
            ("campaign_add_step", {"action_type": "message"}),
            ("campaign_set_template", {"template": "invite"}),
            ("campaign_set_icp", {"description": "anyone at all"}),
        ):
            locked = await call(client, name, campaign_id=campaign_id, **args)
            assert locked["reason"] == CampaignErrorReason.DEFINITION_LOCKED.value, name
        assert len(list_steps(conn, campaign_id)) == 1

        revoked = await call(
            client, "campaign_approve", campaign_id=campaign_id, approved=False
        )
        assert revoked["campaign"]["status"] == STATUS_DRAFT
        assert revoked["approved"] is False

        reopened = await call(
            client, "campaign_add_step", campaign_id=campaign_id, action_type="message"
        )
        assert reopened["status"] == "success"
        assert len(list_steps(conn, campaign_id)) == 2


@pytest.mark.asyncio
async def test_revoking_approval_stops_a_running_campaign(
    server, conn, account, template, leads
):
    async with Client(server) as client:
        campaign_id = await ready_campaign(client, template.id, leads)
        await call(client, "campaign_approve", campaign_id=campaign_id)
        await call(client, "campaign_start", campaign_id=campaign_id)
        assert len(due_jobs(conn, account)) == 2

        await call(
            client, "campaign_approve", campaign_id=campaign_id, approved=False
        )
        assert due_jobs(conn, account) == []


@pytest.mark.asyncio
async def test_a_definition_that_could_never_run_is_not_approvable(server, account):
    async with Client(server) as client:
        created = await call(client, "campaign_create", name="Empty")
        refused = await call(
            client, "campaign_approve", campaign_id=created["campaign"]["id"]
        )
        assert refused["reason"] == CampaignErrorReason.DEFINITION_INCOMPLETE.value


@pytest.mark.asyncio
async def test_resume_refuses_a_draft_and_start_points_at_resume(
    server, account, template, leads
):
    async with Client(server) as client:
        campaign_id = await ready_campaign(client, template.id, leads)

        early = await call(client, "campaign_resume", campaign_id=campaign_id)
        assert early["reason"] == CampaignErrorReason.CAMPAIGN_NOT_APPROVED.value

        await call(client, "campaign_approve", campaign_id=campaign_id)
        await call(client, "campaign_start", campaign_id=campaign_id)
        await call(client, "campaign_pause", campaign_id=campaign_id)

        misuse = await call(client, "campaign_start", campaign_id=campaign_id)
        assert misuse["reason"] == CampaignErrorReason.CAMPAIGN_STATUS_CONFLICT.value
        assert "campaign_resume" in misuse["message"]


@pytest.mark.asyncio
async def test_status_changes_are_idempotent(server, account, template, leads):
    async with Client(server) as client:
        campaign_id = await ready_campaign(client, template.id, leads)
        await call(client, "campaign_approve", campaign_id=campaign_id)
        await call(client, "campaign_start", campaign_id=campaign_id)

        again = await call(client, "campaign_start", campaign_id=campaign_id)
        assert again["status"] == "success"
        assert again["changed"] is False


# --------------------------------------------------------------------------
# Preview
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preview_renders_for_real_enrolled_leads(
    server, conn, account, template, leads
):
    async with Client(server) as client:
        campaign_id = await ready_campaign(client, template.id, leads)
        preview = await call(client, "campaign_preview", campaign_id=campaign_id)

    enrolled = [record.lead_id for record in list_campaign_leads(conn, campaign_id)]
    assert [sample["lead_id"] for sample in preview["samples"]] == enrolled
    assert all("Lovelace" not in sample["text"] for sample in preview["samples"])
    assert "Ada" in preview["samples"][0]["text"]
    assert "Contoso" in preview["samples"][0]["text"]
    assert preview["enrolled"] == 2


@pytest.mark.asyncio
async def test_preview_needs_leads_a_template_and_a_step(
    server, account, template, leads
):
    async with Client(server) as client:
        created = await call(client, "campaign_create", name="Bare")
        campaign_id = created["campaign"]["id"]

        empty = await call(client, "campaign_preview", campaign_id=campaign_id)
        assert empty["reason"] == CampaignErrorReason.DEFINITION_INCOMPLETE.value

        await call(
            client,
            "campaign_add_step",
            campaign_id=campaign_id,
            action_type="connection_request",
            config={"delay_seconds": 0},
        )
        untemplated = await call(client, "campaign_preview", campaign_id=campaign_id)
        assert untemplated["reason"] == CampaignErrorReason.TEMPLATE_NOT_ATTACHED.value

        await call(
            client,
            "campaign_set_template",
            campaign_id=campaign_id,
            template=template.id,
        )
        leadless = await call(client, "campaign_preview", campaign_id=campaign_id)
        assert leadless["reason"] == CampaignErrorReason.NO_LEADS_ENROLLED.value

        await call(
            client, "campaign_add_leads", campaign_id=campaign_id, lead_ids=leads
        )
        good = await call(client, "campaign_preview", campaign_id=campaign_id)
        assert good["status"] == "success"


@pytest.mark.asyncio
async def test_a_refused_render_never_returns_half_filled_text(
    server, conn, account, leads
):
    """A missing variable is a typed refusal, not a message with a hole in it."""
    strict = create_template(conn, account, "strict", NEEDS_COMPANY)
    nameless = create_lead(
        conn, account, "Anonymous Person", public_id="anonymous-person"
    )

    async with Client(server) as client:
        campaign_id = await ready_campaign(
            client, strict.id, [*leads, nameless.id], name="Strict"
        )
        preview = await call(
            client, "campaign_preview", campaign_id=campaign_id, samples=5
        )

    assert preview["status"] == "success"
    assert preview["refused"] == 1
    assert preview["rendered"] == 2
    refused = [s for s in preview["samples"] if s["status"] == "refused"]
    assert refused[0]["lead_id"] == nameless.id
    assert refused[0]["would_move_to"] == Sublist.SKIPPED.value
    assert refused[0]["reason"]
    assert "text" not in refused[0], "no partial message is ever handed back"


@pytest.mark.asyncio
async def test_preview_writes_nothing(server, conn, account, template, leads):
    async with Client(server) as client:
        campaign_id = await ready_campaign(client, template.id, leads)
        before = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("campaign_leads", "jobs", "campaign_steps", "ai_drafts")
        }
        sublists_before = [
            record.sublist for record in list_campaign_leads(conn, campaign_id)
        ]

        await call(client, "campaign_preview", campaign_id=campaign_id, samples=5)

        after = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("campaign_leads", "jobs", "campaign_steps", "ai_drafts")
        }
    assert before == after
    assert sublists_before == [
        record.sublist for record in list_campaign_leads(conn, campaign_id)
    ]


@pytest.mark.asyncio
async def test_preview_sample_count_is_capped(server, account, template, leads):
    async with Client(server) as client:
        campaign_id = await ready_campaign(client, template.id, leads)
        capped = await call(
            client, "campaign_preview", campaign_id=campaign_id, samples=10_000
        )
    assert capped["status"] == "success"
    assert len(capped["samples"]) <= MAX_PREVIEW_SAMPLES


# --------------------------------------------------------------------------
# Definition tools
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_setting_an_icp_twice_rewrites_the_gate_instead_of_stacking(
    server, conn, account, template, leads
):
    async with Client(server) as client:
        campaign_id = await ready_campaign(client, template.id, leads)

        first = await call(
            client,
            "campaign_set_icp",
            campaign_id=campaign_id,
            description="Platform leads",
            threshold=0.5,
        )
        second = await call(
            client,
            "campaign_set_icp",
            campaign_id=campaign_id,
            description="Platform leads at 500+ engineer companies",
            threshold=0.8,
        )

    assert first["replaced"] is False
    assert second["replaced"] is True
    gates = [step for step in list_steps(conn, campaign_id) if "icp" in step.config]
    assert len(gates) == 1
    assert gates[0].config["min_score"] == 0.8
    assert gates[0].action_type == "filter"


@pytest.mark.asyncio
async def test_an_icp_threshold_outside_zero_to_one_is_refused(
    server, account, template, leads
):
    async with Client(server) as client:
        campaign_id = await ready_campaign(client, template.id, leads)
        refused = await call(
            client,
            "campaign_set_icp",
            campaign_id=campaign_id,
            description="Anybody",
            threshold=7,
        )
    assert refused["reason"] == CampaignErrorReason.INVALID_ARGUMENT.value


@pytest.mark.asyncio
async def test_set_template_asks_which_step_when_the_answer_is_ambiguous(
    server, account, template, leads
):
    async with Client(server) as client:
        created = await call(client, "campaign_create", name="Two touches")
        campaign_id = created["campaign"]["id"]
        for _ in range(2):
            await call(
                client,
                "campaign_add_step",
                campaign_id=campaign_id,
                action_type="message",
                template_id=template.id,
                config={"delay_seconds": 0},
            )

        ambiguous = await call(
            client, "campaign_set_template", campaign_id=campaign_id, template="invite"
        )
        assert ambiguous["reason"] == CampaignErrorReason.STEP_TARGET_AMBIGUOUS.value
        assert ambiguous["ords"] == [1, 2]

        named = await call(
            client,
            "campaign_set_template",
            campaign_id=campaign_id,
            template="invite",
            position=2,
        )
        assert named["step"]["ord"] == 2

        missing = await call(
            client,
            "campaign_set_template",
            campaign_id=campaign_id,
            template="invite",
            position=9,
        )
        assert missing["reason"] == CampaignErrorReason.STEP_NOT_FOUND.value


@pytest.mark.asyncio
async def test_an_unknown_template_name_is_a_typed_refusal(
    server, account, template, leads
):
    async with Client(server) as client:
        campaign_id = await ready_campaign(client, template.id, leads)
        refused = await call(
            client,
            "campaign_set_template",
            campaign_id=campaign_id,
            template="does-not-exist",
        )
    assert refused["reason"] == CampaignErrorReason.TEMPLATE_NOT_FOUND.value


@pytest.mark.asyncio
async def test_an_unrunnable_step_definition_is_a_typed_refusal(
    server, account, template
):
    async with Client(server) as client:
        created = await call(client, "campaign_create", name="Broken")
        refused = await call(
            client,
            "campaign_add_step",
            campaign_id=created["campaign"]["id"],
            action_type="filter",
        )
    assert refused["reason"] == CampaignErrorReason.STEP_DEFINITION_INVALID.value


# --------------------------------------------------------------------------
# Enrolment
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_leads_can_be_enrolled_by_tag(server, conn, account, template, leads):
    ensure_tag(conn, account, "platform")
    add_tags(conn, leads[0], ["platform"])

    async with Client(server) as client:
        created = await call(client, "campaign_create", name="Tagged")
        campaign_id = created["campaign"]["id"]
        await call(
            client,
            "campaign_add_step",
            campaign_id=campaign_id,
            action_type="connection_request",
            template_id=template.id,
            config={"delay_seconds": 0},
        )
        added = await call(
            client, "campaign_add_leads", campaign_id=campaign_id, tags=["platform"]
        )

    assert added["enrolled"] == [leads[0]]
    assert [r.lead_id for r in list_campaign_leads(conn, campaign_id)] == [leads[0]]


@pytest.mark.asyncio
async def test_enrolling_twice_reports_the_second_call_as_already_enrolled(
    server, account, template, leads
):
    async with Client(server) as client:
        campaign_id = await ready_campaign(client, template.id, leads)
        repeat = await call(
            client, "campaign_add_leads", campaign_id=campaign_id, lead_ids=leads
        )
    assert repeat["enrolled"] == []
    assert sorted(repeat["already_enrolled"]) == sorted(leads)


@pytest.mark.asyncio
async def test_an_unknown_lead_is_a_typed_refusal(server, account, template, leads):
    async with Client(server) as client:
        created = await call(client, "campaign_create", name="Ghosts")
        campaign_id = created["campaign"]["id"]
        await call(
            client,
            "campaign_add_step",
            campaign_id=campaign_id,
            action_type="connection_request",
            template_id=template.id,
            config={"delay_seconds": 0},
        )
        refused = await call(
            client, "campaign_add_leads", campaign_id=campaign_id, lead_ids=[999_999]
        )
        empty = await call(client, "campaign_add_leads", campaign_id=campaign_id)

    assert refused["reason"] == CampaignErrorReason.LEAD_NOT_FOUND.value
    assert empty["reason"] == CampaignErrorReason.INVALID_ARGUMENT.value


@pytest.mark.asyncio
async def test_leads_can_be_added_to_a_running_campaign(
    server, conn, account, template, leads
):
    """Adding leads is not a definition edit, so the freeze does not apply."""
    async with Client(server) as client:
        campaign_id = await ready_campaign(client, template.id, leads[:1])
        await call(client, "campaign_approve", campaign_id=campaign_id)
        await call(client, "campaign_start", campaign_id=campaign_id)

        late = await call(
            client, "campaign_add_leads", campaign_id=campaign_id, lead_ids=leads[1:]
        )
    assert late["enrolled"] == leads[1:]
    assert len(due_jobs(conn, account)) == 2


@pytest.mark.asyncio
async def test_an_archived_campaign_accepts_no_new_leads(
    server, account, template, leads
):
    async with Client(server) as client:
        campaign_id = await ready_campaign(client, template.id, leads[:1])
        await call(client, "campaign_archive", campaign_id=campaign_id)
        refused = await call(
            client, "campaign_add_leads", campaign_id=campaign_id, lead_ids=leads[1:]
        )
    assert refused["reason"] == CampaignErrorReason.CAMPAIGN_STATUS_CONFLICT.value


# --------------------------------------------------------------------------
# Status
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_lists_every_campaign_and_reports_one(
    server, account, template, leads
):
    async with Client(server) as client:
        first = await ready_campaign(client, template.id, leads, name="One")
        await call(client, "campaign_create", name="Two")

        listed = await call(client, "campaign_status")
        assert listed["count"] == 2
        assert {c["name"] for c in listed["campaigns"]} == {"One", "Two"}
        assert all("funnel" in c for c in listed["campaigns"])
        assert "worker" in listed

        one = await call(client, "campaign_status", campaign_id=first)
        assert one["campaign"]["id"] == first
        assert one["funnel"]["total"] == 2
        assert one["open_jobs"] == 2
        assert one["due_now"] == 0, "a draft is due nothing, exactly like the worker"
        assert one["next_run_at"]


@pytest.mark.asyncio
async def test_another_account_s_campaign_reads_as_absent(
    server, audit, conn, account, template, leads
):
    """Ownership is not leaked by the difference between denied and missing."""
    async with Client(server) as client:
        campaign_id = await ready_campaign(client, template.id, leads)

        other = audit.ensure_account("someone-else@example.com")
        instrument.set_account_resolver(lambda: other)
        result = await call(client, "campaign_status", campaign_id=campaign_id)

    assert result["reason"] == CampaignErrorReason.CAMPAIGN_NOT_FOUND.value


# --------------------------------------------------------------------------
# The claims about what this module is not
# --------------------------------------------------------------------------


class Tripwire:
    """Anything that touches this fails the test with a readable message."""

    def __init__(self, what):
        object.__setattr__(self, "_what", what)

    def __getattr__(self, name):
        raise AssertionError(f"a campaign tool reached for {self._what}.{name}")

    def __call__(self, *args, **kwargs):
        raise AssertionError(f"a campaign tool called {self._what}")


@pytest.mark.asyncio
async def test_no_campaign_tool_opens_a_browser_or_touches_the_network(
    server, conn, account, template, leads, monkeypatch
):
    """Behavioural proof, not a promise in a docstring.

    Name resolution, outbound connections and TLS all raise, and the browser
    session class is replaced by a tripwire in both places it is importable
    from. All twelve tools are then driven through a full lifecycle. They all
    still work, so none of them can have reached LinkedIn.

    `socket.socket` itself is deliberately left alone: the Windows event loop
    keeps a self-pipe and does `isinstance(conn, socket.socket)`, so replacing
    the class breaks the harness rather than the code under test.
    """
    import ssl

    import linkedin_mcp.browser as browser_pkg
    import linkedin_mcp.browser.session as browser_session
    import linkedin_mcp.tools.campaigns as module

    monkeypatch.setattr(browser_pkg, "BrowserSession", Tripwire("BrowserSession"))
    monkeypatch.setattr(browser_session, "BrowserSession", Tripwire("BrowserSession"))

    def refuse(*args, **kwargs):
        raise AssertionError("a campaign tool tried to reach the network")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(ssl.SSLContext, "wrap_socket", refuse)

    assert not [
        name
        for name, value in vars(module).items()
        if getattr(value, "__module__", "").startswith("linkedin_mcp.browser")
    ], "nothing from the browser package is bound in this module's namespace"

    async with Client(server) as client:
        created = await call(client, "campaign_create", name="Offline")
        campaign_id = created["campaign"]["id"]
        calls = [
            (
                "campaign_add_step",
                {
                    "action_type": "connection_request",
                    "template_id": template.id,
                    "config": {"delay_seconds": 0},
                },
            ),
            ("campaign_set_icp", {"description": "Platform engineering leaders"}),
            ("campaign_set_template", {"template": "invite", "position": 2}),
            ("campaign_add_leads", {"lead_ids": leads}),
            ("campaign_preview", {}),
            ("campaign_approve", {}),
            ("campaign_start", {}),
            ("campaign_pause", {}),
            ("campaign_resume", {}),
            ("campaign_status", {}),
            ("campaign_archive", {}),
        ]
        exercised = {"campaign_create"}
        for name, args in calls:
            result = await call(client, name, campaign_id=campaign_id, **args)
            assert result["status"] == "success", (name, result)
            exercised.add(name)

    assert exercised == set(TOOL_NAMES), "all twelve ran with the network cut"


@pytest.mark.asyncio
async def test_the_audit_log_records_every_campaign_action_with_its_reason(
    server, conn, account, template, leads
):
    """A refusal is auditable as a failure carrying its typed reason."""
    async with Client(server) as client:
        campaign_id = await ready_campaign(client, template.id, leads)
        await call(client, "campaign_start", campaign_id=campaign_id)

    rows = conn.execute(
        "SELECT action_type, outcome, detail_json FROM actions_log ORDER BY id"
    ).fetchall()
    recorded = [(row["action_type"], row["outcome"]) for row in rows]
    assert ("campaign_create", "success") in recorded
    assert ("campaign_add_step", "success") in recorded
    assert ("campaign_add_leads", "success") in recorded
    assert ("campaign_start", "failure") in recorded

    refusal = [row for row in rows if row["action_type"] == "campaign_start"][0]
    assert "campaign_not_approved" in refusal["detail_json"]


def test_the_module_never_names_a_driver_or_a_browser_session():
    """The static half of the same claim, and the package guard's own rule."""
    source = MODULE.read_text(encoding="utf-8")
    assert "BrowserSession" not in source
    assert "playwright" not in source.lower()
    assert "requests" not in source.lower()
    assert "httpx" not in source.lower()


def test_the_module_contains_no_manual_sleep():
    """Pacing lives in `linkedin_mcp.browser.humanize`, and nothing here paces."""
    source = MODULE.read_text(encoding="utf-8")
    assert not re.search(r"\b(?:asyncio|time)\.sleep\s*\(", source)
    assert "wait_for_timeout" not in source


def test_mcp_01_added_no_migration():
    """Every column this issue needs already exists in `0001_init.sql`.

    Asserted locally rather than against the whole migration list, so an
    unrelated issue adding a migration cannot fail this test and blame MCP-01.
    """
    names = [path.name for path in MIGRATIONS.glob("*.sql")]
    assert not [
        name
        for name in names
        if any(word in name.lower() for word in ("campaign", "approval", "mcp_01"))
    ], names
    assert "0001_init.sql" in names


# --------------------------------------------------------------------------
# Instrumentation, config and the error vocabulary
# --------------------------------------------------------------------------


def test_every_campaign_tool_is_instrumented_and_unmetered():
    """The decorator shape and the config registration, checked together.

    The repo-wide guard in `tests/test_audit_log.py` walks string literals, so
    this resolves the constants the decorators actually use.
    """
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    tools = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        names = {
            ast.unparse(d.func) if isinstance(d, ast.Call) else ast.unparse(d)
            for d in node.decorator_list
        }
        if "mcp.tool" not in names:
            continue
        assert "audit_linkedin_action" in names, node.name
        for decorator in node.decorator_list:
            if (
                isinstance(decorator, ast.Call)
                and ast.unparse(decorator.func) == "audit_linkedin_action"
            ):
                tools[node.name] = ast.unparse(decorator.args[0])

    assert set(tools) == set(TOOL_NAMES)

    import linkedin_mcp.tools.campaigns as module

    for tool_name, constant in tools.items():
        action = getattr(module, constant)
        assert action == tool_name, (tool_name, constant, action)
        assert action in UNMETERED_ACTIONS, action
        assert not is_metered(action), action


def test_the_campaign_action_types_are_registered_and_never_metered():
    assert len(CAMPAIGN_ACTION_TYPES) == 12
    assert set(CAMPAIGN_ACTION_TYPES) <= set(UNMETERED_ACTIONS)
    assert not set(CAMPAIGN_ACTION_TYPES) & set(METERED_ACTIONS)


def test_every_refusal_reason_has_a_typed_exception_behind_it():
    """No reason may exist that only free text could have produced."""
    own = {cls.reason for cls in campaign_error_classes()}
    translated = {reason for _, reason in TRANSLATED_ERRORS}
    assert own | translated == set(CampaignErrorReason)
    assert not own & translated, "a reason should have exactly one owner"


def test_the_error_base_class_cannot_be_raised_untyped():
    with pytest.raises(TypeError):
        raise CampaignToolError("a free-text error wearing a typed coat")


def test_a_typed_error_serialises_with_its_reason_and_class():
    from linkedin_mcp.tools.campaigns import CampaignNotApprovedError

    error = CampaignNotApprovedError("not yet", campaign_id=7, campaign_status="draft")
    assert error.to_result() == {
        "status": "error",
        "reason": "campaign_not_approved",
        "error": "CampaignNotApprovedError",
        "message": "not yet",
        "campaign_id": 7,
        "campaign_status": "draft",
    }


def test_detail_can_never_shadow_the_error_envelope():
    """A detail called `status` must not turn a failure into a success."""
    from linkedin_mcp.tools.campaigns import CampaignStatusConflictError

    error = CampaignStatusConflictError("nope", status="active", reason="mine")
    assert error.to_result()["status"] == "error"
    assert error.to_result()["reason"] == "campaign_status_conflict"
