"""MCP-05 (#28): the six prompts, and the Phase 4 exit criterion.

Everything here goes through `linkedin_browser_mcp.mcp`, the FastMCP instance
the process actually serves, rather than a server the test builds for itself.
PR #51 is why: fifteen tools were merged with full coverage against locally
constructed instances and were invisible to every real client for weeks, because
nothing ever called their factory.

The exit criterion
------------------
    a fresh MCP client with no Copilot-specific files can discover campaigns,
    inspect the funnel, review drafts and pause the worker using tools,
    resources and prompts alone

`test_a_fresh_client_discovers_inspects_reviews_and_pauses` walks it literally.
It touches nothing under `.github/`, reads only what an MCP handshake would
expose, and finishes by confirming the pause from a resource and proving an
ad-hoc job does not run while it is in force. That last clause is the one that
failed before this issue: `campaign_pause` never stopped the ad-hoc lane, and
`campaigns_running` read True whenever a worker was live.

The rendering call
------------------
`mcp.get_prompt(name)` is a lookup that returns the `Prompt` object; its second
parameter is a `VersionSpec`, so passing an arguments dict raises
``'dict' object has no attribute 'matches'`` and yields nothing. The call that
renders is `mcp.render_prompt(name, arguments)`, and it returns a `PromptResult`
whose `messages` carry the text. Every test below renders for real, because a
test that silently received None would pass while proving nothing.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client

import linkedin_browser_mcp
from linkedin_mcp.audit import instrument
from linkedin_mcp.audit.log import AuditLog, reset_audit_log, set_audit_log
from linkedin_mcp.drafts import request_draft, submit_draft
from linkedin_mcp.leads import create_lead
from linkedin_mcp.prompts import (
    PROMPT_NAMES,
    PROMPT_RESOURCES,
    banned_dash_names,
    read_first,
    voice_rules,
)
from linkedin_mcp.resources.contract import RESOURCE_MIME_TYPE
from linkedin_mcp.sequences import (
    StepSpec,
    add_step,
    create_campaign,
    enrol_leads,
    set_campaign_status,
)
from linkedin_mcp.templating import create_template
from linkedin_mcp.templating.style import (
    FILLER_OPENERS,
    FORBIDDEN_DASHES,
    StylePolicy,
    style_violations,
)
from linkedin_mcp.worker import is_worker_paused, select_due_jobs, write_heartbeat

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = REPO_ROOT / "linkedin_mcp" / "core" / "migrations"

SERVED = linkedin_browser_mcp.mcp
"""The shipped server. Rendering through anything else would prove nothing."""

REQUIRED_ARGUMENTS: dict[str, dict[str, str]] = {
    "new_campaign": {"audience": "Platform engineering leads at UK banks"},
    "harvest_audience": {"source": "harvest_post_engagers"},
}
"""The arguments each prompt cannot render without. The rest default."""

GOOD_NOTE = (
    "{IF firstName}Hi {firstName},{ELSE}Hi there,{END} saw your team at "
    "{company} is midway through a Copilot rollout. Happy to compare notes."
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


async def render(name: str, arguments: dict[str, Any] | None = None) -> str:
    """Render one prompt off the shipped server and return its text.

    `render_prompt` rather than `get_prompt`. The latter is a lookup whose
    second parameter is a version spec, so handing it an arguments dict raises
    inside the provider and returns None, which a careless test would read as an
    empty prompt rather than as a wrong call.
    """
    result = await SERVED.render_prompt(name, arguments or REQUIRED_ARGUMENTS.get(name, {}))
    assert result.messages, f"{name} rendered no messages"
    text = "\n".join(
        message.content.text
        for message in result.messages
        if getattr(message.content, "text", None)
    )
    assert text.strip(), f"{name} rendered empty text"
    return text


async def read(uri: str) -> dict[str, Any]:
    """Read one resource through the shipped server and decode its JSON."""
    result = await SERVED.read_resource(uri)
    assert len(result.contents) == 1
    content = result.contents[0]
    assert content.mime_type == RESOURCE_MIME_TYPE
    return json.loads(content.content)


async def call(name: str, **arguments) -> dict[str, Any]:
    """Call one tool through a real MCP client against the shipped server."""
    async with Client(SERVED) as client:
        result = await client.call_tool(name, arguments)
    return result.data


# --------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_shipped_server_registers_all_six_prompts():
    served = {prompt.name for prompt in await SERVED.list_prompts()}

    assert served == set(PROMPT_NAMES)
    assert len(served) == 6


@pytest.mark.asyncio
async def test_every_prompt_declares_the_arguments_it_needs():
    """A required argument must be advertised as required.

    A client picks its form fields off `list_prompts`, so a prompt that needed
    an audience but did not say so would render a walkthrough with a hole in it
    and no way for the client to know.
    """
    declared = {
        prompt.name: {
            argument.name: bool(argument.required)
            for argument in (prompt.arguments or [])
        }
        for prompt in await SERVED.list_prompts()
    }

    assert declared["new_campaign"]["audience"] is True
    assert declared["new_campaign"]["goal"] is False
    assert declared["new_campaign"]["daily_invite_cap"] is False
    assert declared["harvest_audience"]["source"] is True
    assert declared["safety_check"] == {}

    for name, arguments in declared.items():
        required = {key for key, needed in arguments.items() if needed}
        assert required == set(REQUIRED_ARGUMENTS.get(name, {})), name


@pytest.mark.asyncio
async def test_every_prompt_carries_a_description_a_client_can_show():
    for prompt in await SERVED.list_prompts():
        assert prompt.description, prompt.name
        assert len(prompt.description) > 40, prompt.name


@pytest.mark.asyncio
async def test_a_prompt_missing_a_required_argument_fails_loudly():
    """Fail loudly is a project rule, and it applies to a bad render too."""
    with pytest.raises(Exception):
        await SERVED.render_prompt("new_campaign", {})


# --------------------------------------------------------------------------
# Voice, derived rather than restated
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_prompt_carries_the_voice_rules():
    """The DoD line: prompts carry Nived's voice rules.

    Checked by the rules being present in every rendered prompt, not by a claim
    in a pull request body.
    """
    rules = voice_rules()

    for name in PROMPT_NAMES:
        text = await render(name)
        assert rules in text, name


@pytest.mark.asyncio
async def test_the_prompt_text_obeys_the_rules_it_states():
    """A prompt that preaches "no em dashes" while containing one is the joke.

    Every prompt is run through the same checker that rejects a template, so the
    DoD line is checkable rather than a claim. This is cheap and it is the
    reason the rules are generated from `style.py` instead of retyped.
    """
    offenders: dict[str, list[str]] = {}

    for name in PROMPT_NAMES:
        text = await render(name)
        violations = style_violations(text)
        if violations:
            offenders[name] = [f"{v.kind}: {v.excerpt}" for v in violations]

    assert offenders == {}, offenders


@pytest.mark.asyncio
async def test_the_prompts_contain_none_of_the_dashes_they_ban():
    for name in PROMPT_NAMES:
        text = await render(name)
        for dash in FORBIDDEN_DASHES:
            assert dash not in text, (name, dash)


def test_the_voice_rules_are_generated_from_style_not_copied():
    """Change the policy, change what every prompt says.

    Three copies of these rules already exist as Markdown and as code. A fourth
    that drifted would be invisible until a client followed the prompt and the
    template store refused what it produced, so this pins the derivation rather
    than the wording.
    """
    default = voice_rules()
    assert f"under {StylePolicy().max_sentence_words} words" in default
    for opener in FILLER_OPENERS:
        assert opener in default

    stricter = voice_rules(StylePolicy(max_sentence_words=12, filler_openers=("moo",)))
    assert "under 12 words" in stricter
    assert '"moo"' in stricter
    assert FILLER_OPENERS[0] not in stricter


def test_the_dashes_are_named_rather_than_shown():
    """Listing the characters would put them in the text that bans them."""
    names = banned_dash_names()

    assert len(names) == len(FORBIDDEN_DASHES)
    assert "em dash" in names
    assert not set(FORBIDDEN_DASHES) & set("".join(names))


# --------------------------------------------------------------------------
# What each prompt has to say
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_new_campaign_walks_audience_sequence_templates_limits_then_approve():
    """The DoD line for `new_campaign`, in the order the issue names.

    The stage headings are asserted to appear in order rather than merely to be
    present, because a walkthrough that mentioned approval before templates
    would be a different and worse workflow.
    """
    text = await render("new_campaign")

    stages = [
        "Stage 1, audience.",
        "Stage 2, sequence.",
        "Stage 3, templates.",
        "Stage 4, limits.",
        "Stage 5, the gate.",
    ]
    positions = [text.index(stage) for stage in stages]
    assert positions == sorted(positions), text

    assert "campaign_create" in text
    assert "campaign_add_leads" in text
    assert "campaign_add_step" in text
    assert "campaign_set_template" in text
    assert "campaign_preview" in text
    assert "campaign_approve" in text

    approve_at = text.index("campaign_approve")
    for tool in ("campaign_create", "campaign_add_step", "campaign_set_template"):
        assert text.index(tool) < approve_at, tool

    assert "Platform engineering leads at UK banks" in text


@pytest.mark.asyncio
async def test_new_campaign_states_the_real_ceilings_rather_than_round_numbers():
    """"Numbers over generalities" is one of the rules it teaches."""
    from linkedin_mcp.core.config import (
        GLOBAL_DAILY_CEILING,
        GLOBAL_HOURLY_CEILING,
        INVITE_ACTION,
        ceiling_for,
    )

    text = await render("new_campaign")
    invite = ceiling_for(INVITE_ACTION)

    assert f"{invite.daily} invitations a day" in text
    assert f"{invite.weekly} a week" in text
    assert f"{GLOBAL_DAILY_CEILING} metered actions a day" in text
    assert f"{GLOBAL_HOURLY_CEILING} an hour" in text


@pytest.mark.asyncio
async def test_new_campaign_refuses_to_approve_on_its_own_initiative():
    """Human-in-the-loop is a project rule, so the prompt has to say it."""
    text = await render("new_campaign")

    assert "Never call campaign_approve or campaign_start on your own" in text
    assert "wait for him to say yes" in text


@pytest.mark.asyncio
async def test_review_drafts_separates_the_generation_queue_from_the_review_queue():
    text = await render("review_drafts")

    assert "drafts_list_pending" in text
    assert "needs_generation" in text
    assert "pending_approval" in text
    assert "drafts_submit" in text
    assert "drafts_approve" in text
    assert text.index("Queue one, generation.") < text.index("Queue two, approval.")


@pytest.mark.asyncio
async def test_triage_replies_names_the_unread_definition_and_sends_nothing():
    text = await render("triage_replies")

    assert "linkedin://inbox/unread" in text
    assert "newest stored message is inbound" in text
    assert "action_enqueue_adhoc" in text
    assert "He decides" in text


@pytest.mark.asyncio
async def test_weekly_report_reads_the_ledger_rather_than_guessing():
    text = await render("weekly_report")

    assert "linkedin://analytics/weekly" in text
    assert "linkedin://worker/status" in text
    assert "Do not estimate anything" in text


@pytest.mark.asyncio
async def test_safety_check_knows_how_to_actually_stop_the_worker():
    """The prompt has to name the tool that stops both lanes, not campaign_pause.

    A safety prompt that told a client to pause every campaign would leave the
    ad-hoc lane sending invitations, which is precisely the gap #28 found.
    """
    text = await render("safety_check")

    assert "worker_pause" in text
    assert "worker_resume" in text
    assert "stops both job lanes" in text
    assert "does not read campaign status" in text
    assert "campaigns_running reads false" in text
    assert "Never raise a cap to clear a refusal" in text


@pytest.mark.asyncio
async def test_harvest_audience_names_every_source_and_no_sales_navigator():
    text = await render("harvest_audience")

    for tool in (
        "harvest_people_search",
        "harvest_post_engagers",
        "harvest_group_members",
        "harvest_event_attendees",
        "harvest_company_employees",
        "harvest_connections",
        "harvest_import_csv",
    ):
        assert tool in text, tool

    assert "harvest_sales_nav" not in text
    assert "no Sales Navigator source" in text
    assert "harvest_status" in text


@pytest.mark.asyncio
async def test_every_prompt_points_at_the_resources_it_depends_on():
    """A prompt that told a client what to do without what to read is guessing."""
    for name in PROMPT_NAMES:
        text = await render(name)
        line = read_first(name)
        assert line, name
        assert line in text, name
        for uri in PROMPT_RESOURCES[name]:
            assert uri in text, (name, uri)


def test_the_prompt_resource_map_uses_real_uris():
    """Imported from the resource contract, so a URI that moves takes these."""
    from linkedin_mcp.resources import ALL_RESOURCE_URIS

    for name, uris in PROMPT_RESOURCES.items():
        assert uris, name
        for uri in uris:
            assert uri in set(ALL_RESOURCE_URIS), (name, uri)


# --------------------------------------------------------------------------
# A prompt spends nothing
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rendering_every_prompt_writes_no_actions_log_row(conn, account):
    """The runtime half of the audit exemption.

    `tests/test_audit_log.py` asserts no prompt carries the decorator and none
    of them calls anything that spends an action. This asserts the consequence:
    render all six through the shipped server and the rate limiter's ledger is
    still empty, so a client listing and rendering the prompt menu on connect
    has consumed none of the day's budget.
    """
    for name in PROMPT_NAMES:
        await render(name)

    rows = conn.execute(
        "SELECT COUNT(*) AS total FROM actions_log WHERE account_id = ?",
        (account,),
    ).fetchone()["total"]

    assert rows == 0


# --------------------------------------------------------------------------
# The Phase 4 exit criterion
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_fresh_client_discovers_inspects_reviews_and_pauses(conn, account):
    """The exit criterion, walked rather than asserted.

    Tools, resources and prompts only. Nothing under `.github/` is opened and no
    Copilot-specific file is consulted, so this is what a client that had never
    heard of this repository could do from an MCP handshake alone.

    The last step is the one that failed before #28. Pausing every campaign left
    the ad-hoc lane selecting work, and `linkedin://worker/status` reported
    `campaigns_running: true` whenever any worker was live, so a client that
    paused and then read the resource to confirm was told the opposite.
    """
    # ---- what a client can see at all -------------------------------------
    tools = {tool.name for tool in await SERVED.list_tools()}
    resources = {str(item.uri) for item in await SERVED.list_resources()}
    templates = {item.uri_template for item in await SERVED.list_resource_templates()}
    prompts = {prompt.name for prompt in await SERVED.list_prompts()}

    assert {"worker_pause", "worker_resume", "campaign_approve"} <= tools
    assert "linkedin://campaigns" in resources
    assert "linkedin://campaigns/{campaign_id}/funnel" in templates
    assert "linkedin://drafts/pending" in resources
    assert "linkedin://worker/status" in resources
    assert set(PROMPT_NAMES) <= prompts

    # ---- a prompt tells it how ---------------------------------------------
    guide = await render("safety_check")
    assert "worker_pause" in guide

    # ---- something to discover ---------------------------------------------
    template = create_template(conn, account, "invite", GOOD_NOTE)
    made = create_campaign(conn, account, "Copilot rollout leads")
    add_step(conn, made.id, StepSpec("connection_request", template_id=template.id))
    ada = create_lead(conn, account, "Ada Lovelace", public_id="ada", first_name="Ada")
    enrol_leads(conn, made.id, [ada.id])
    set_campaign_status(conn, made.id, "pending_approval")
    set_campaign_status(conn, made.id, "active")

    draft = request_draft(conn, made.id, ada.id, "connection_note")
    draft = submit_draft(
        conn, draft.id, text="Saw your Copilot rollout. Happy to compare notes."
    )
    assert draft.status == "pending_approval"

    write_heartbeat(conn, "w1", account, "idle")

    # An ad-hoc job, queued through the tool a real client would use. This is
    # the lane campaign_pause cannot reach.
    queued = await call(
        "action_enqueue_adhoc", action="profile_search", query="solution engineer"
    )
    assert queued["status"] == "queued"
    adhoc_id = queued["job_id"]

    # ---- discover campaigns -------------------------------------------------
    overview = await read("linkedin://campaigns")
    assert [item["id"] for item in overview["campaigns"]] == [made.id]
    assert overview["campaigns"][0]["status"] == "active"

    # ---- inspect the funnel -------------------------------------------------
    funnel = await read(f"linkedin://campaigns/{made.id}/funnel")
    assert funnel["campaign_id"] == made.id
    assert funnel["funnel"]["queue"] == 1

    # ---- review drafts ------------------------------------------------------
    pending = await read("linkedin://drafts/pending")
    assert [item["id"] for item in pending["drafts"]] == [draft.id]

    listed = await call("drafts_list_pending", status="pending_approval")
    assert listed["status"] == "success"
    assert [item["id"] for item in listed["drafts"]] == [draft.id]

    # ---- the worker looks busy before the pause -----------------------------
    before = await read("linkedin://worker/status")
    assert before["live_workers"] == 1
    assert before["active_campaigns"] == 1
    assert before["campaigns_running"] is True
    assert before["paused"] is False
    assert len(select_due_jobs(conn, account)) >= 1

    # ---- pause the worker ---------------------------------------------------
    paused = await call("worker_pause", reason="reviewing the invite note")
    assert paused["status"] == "success"

    # ---- confirm it, from a resource ----------------------------------------
    after = await read("linkedin://worker/status")
    assert after["paused"] is True
    assert after["pause"]["reason"] == "reviewing the invite note"
    assert after["campaigns_running"] is False, (
        "a client that pauses and then reads the resource to confirm must be "
        "told it stopped; this field used to be bool(live) and said otherwise"
    )
    assert after["queue_is_moving"] is False

    # ---- and the ad-hoc job will not run ------------------------------------
    selection = select_due_jobs(conn, account)
    assert selection.paused is True
    assert selection.jobs() == ()
    assert adhoc_id not in {job.id for job in selection.jobs()}
    assert is_worker_paused(conn, account) is True

    # ---- resume, and everything comes back ----------------------------------
    resumed = await call("worker_resume")
    assert resumed["status"] == "success"

    restored = await read("linkedin://worker/status")
    assert restored["paused"] is False
    assert restored["campaigns_running"] is True
    assert adhoc_id in {job.id for job in select_due_jobs(conn, account).jobs()}


@pytest.mark.asyncio
async def test_pausing_every_campaign_would_not_have_satisfied_the_criterion(
    conn, account
):
    """Why the criterion needed a new tool rather than a loop over the old one.

    Kept beside the walkthrough so the two are read together. Pausing the only
    campaign leaves the ad-hoc job selected, so a client that demonstrated
    "pause" this way would still be sending.
    """
    made = create_campaign(conn, account, "Copilot rollout leads")
    add_step(conn, made.id, StepSpec("connection_request"))
    set_campaign_status(conn, made.id, "pending_approval")
    set_campaign_status(conn, made.id, "active")

    queued = await call(
        "action_enqueue_adhoc", action="profile_search", query="solution engineer"
    )
    adhoc_id = queued["job_id"]

    await call("campaign_pause", campaign_id=made.id)

    selection = select_due_jobs(conn, account)
    assert selection.paused is False
    assert adhoc_id in {job.id for job in selection.jobs()}

    await call("worker_pause")

    assert select_due_jobs(conn, account).jobs() == ()


# --------------------------------------------------------------------------
# Scope of this change
# --------------------------------------------------------------------------


def test_the_github_workflows_still_exist_through_the_transition():
    """The DoD line: no window where Nived cannot post or engage.

    The prompts are additive. Every agent, skill and slash command under
    `.github/` is still there, so a Copilot session keeps working exactly as it
    did while other clients gain the same flows. Anything that should be removed
    is a follow-up with its own argument, not a side effect of this one.
    """
    for directory, expected in (
        (
            "agents",
            {
                "analytics",
                "content-posting",
                "network-growth",
                "top-voices",
                "trending-topics",
            },
        ),
        (
            "skills",
            {
                "growth-report",
                "network-campaign",
                "review-and-post",
                "trending-workflow",
                "voice-engagement",
            },
        ),
        (
            "prompts",
            {
                "grow-network",
                "post-content",
                "trending-discover",
                "trending-engage",
                "weekly-analytics",
            },
        ),
    ):
        path = REPO_ROOT / ".github" / directory
        assert path.is_dir(), directory
        found = {item.name.split(".")[0] for item in path.iterdir()}
        assert expected <= found, (directory, sorted(found))


def test_the_prompt_package_reaches_no_database_and_no_browser():
    """A prompt is a string builder, and this is what makes that structural.

    `tests/test_actions.py` already fails the build if a prompt can reach
    Playwright and `tests/test_audit_log.py` if one takes a LinkedIn action.
    This is the narrower claim that makes the audit exemption honest: the
    package imports nothing that could read the account's state, so a prompt
    cannot become a resource by accident.
    """
    banned = ("sqlite3", "linkedin_mcp.browser", "playwright", "tool_connection")

    for path in sorted((REPO_ROOT / "linkedin_mcp" / "prompts").glob("*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)

        for name in banned:
            assert not any(
                item == name or item.startswith(name + ".") for item in imported
            ), (path.name, name)


def test_mcp_05_owns_exactly_one_migration_and_it_is_named_for_this_issue():
    """Scoped to filenames this branch could have created, deliberately.

    Asserting the global migration list is what two sessions did in wave 4, and
    it meant an unrelated branch adding a legitimate migration failed a test
    belonging to somebody else. So this only says what #28 added.

    The migration is justified rather than incidental. The pause has to survive
    a worker restart, and it cannot ride on `accounts.state`: that column is the
    detection subsystem's, where `challenged` and `logged_out` are written and
    ranked, so an operator resume could clear a challenge nobody had resolved.
    """
    mine = sorted(
        path.name for path in MIGRATIONS.glob("*.sql") if "worker_pause" in path.name
    )

    assert mine == ["0004_worker_pause.sql"], mine

    body = (MIGRATIONS / "0004_worker_pause.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS worker_control" in body
    assert "ALTER TABLE" not in body, "0001_init.sql is not this issue's to reshape"
    assert (MIGRATIONS / "0001_init.sql").read_text(encoding="utf-8").count(
        "worker_control"
    ) == 0


def test_the_prompt_package_ships_no_migration_of_its_own():
    assert list((REPO_ROOT / "linkedin_mcp" / "prompts").rglob("*.sql")) == []
