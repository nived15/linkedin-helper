"""MCP-04 (#27): the twelve `linkedin://...` resources.

Every read here goes through `linkedin_browser_mcp.mcp`, the FastMCP instance
the process actually serves, rather than through a server the test builds for
itself. PR #51 is why. Fifteen tools were merged with full test coverage against
locally-constructed FastMCP instances and were invisible to every real client
for weeks, because nothing ever called their `register_*_tools` factory. A test
that only proves "these resources work when I wire them up myself" would have
passed in that world too.

The writes are a different matter. `campaign_approve` and friends are not
registered on the shipped server yet (PR #51 is fixing that), so the campaign
lifecycle in the VAL-03 test is driven through a real MCP client against a
server carrying the campaign tools, while every read comes back through the
shipped one. Both see the same database because both resolve it through
`linkedin_mcp.tools.runtime`, which is the point of that seam.
"""

from __future__ import annotations

import ast
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client, FastMCP

import linkedin_browser_mcp
from linkedin_mcp.audit import instrument
from linkedin_mcp.audit.log import AuditLog, reset_audit_log, set_audit_log
from linkedin_mcp.core.config import HARD_CEILINGS, METERED_ACTIONS
from linkedin_mcp.drafts import request_draft, submit_draft
from linkedin_mcp.leads import create_lead
from linkedin_mcp.resources import (
    ALL_RESOURCE_URIS,
    CLIENT_CAPABILITY_PATH,
    DEFAULT_POLL_AFTER_SECONDS,
    DRAFTS_PENDING_STATUS,
    RESOURCE_MIME_TYPE,
    RESOURCE_TEMPLATE_URIS,
    RESOURCE_URIS,
    UNREAD_DEFINITION,
    ResourceUpdateNotifier,
    campaign_funnel_uri,
    campaign_uri,
    client_supports_resource_updates,
    lead_uri,
    register_linkedin_resources,
    resource_revisions,
    unread_threads,
)
from linkedin_mcp.resources.notify import as_notification_uri, session_key
from linkedin_mcp.sequences import (
    StepSpec,
    add_step,
    create_campaign,
    enrol_leads,
    get_campaign,
    set_campaign_status,
)
from linkedin_mcp.templating import create_template
from linkedin_mcp.tools.campaigns import register_campaign_tools
from linkedin_mcp.worker import write_heartbeat

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = REPO_ROOT / "linkedin_mcp" / "core" / "migrations"
ENTRY_POINT = REPO_ROOT / "linkedin_browser_mcp.py"

GOOD_NOTE = (
    "{IF firstName}Hi {firstName},{ELSE}Hi there,{END} saw your team at "
    "{company} is midway through a Copilot rollout. Happy to compare notes."
)

SERVED = linkedin_browser_mcp.mcp
"""The shipped server. Reading through anything else would prove nothing."""


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


@pytest.fixture(autouse=True)
def clean_notifier_memory():
    """The shipped notifier is a singleton, so forget the in-process session.

    Without this, one test's revisions leak into the next one's
    `changed_since_last_read` and the polling assertions become order-dependent.
    """
    notifier = linkedin_browser_mcp.linkedin_resource_notifier
    notifier.forget("in-process")
    yield
    notifier.forget("in-process")


async def read(uri: str) -> dict[str, Any]:
    """Read one resource through the shipped server and decode its JSON."""
    result = await SERVED.read_resource(uri)
    assert len(result.contents) == 1
    content = result.contents[0]
    assert content.mime_type == RESOURCE_MIME_TYPE
    return json.loads(content.content)


@pytest.fixture()
def campaign(conn, account):
    template = create_template(conn, account, "invite", GOOD_NOTE)
    made = create_campaign(conn, account, "Copilot rollout leads")
    add_step(conn, made.id, StepSpec("connection_request", template_id=template.id))
    ada = create_lead(conn, account, "Ada Lovelace", public_id="ada", first_name="Ada")
    grace = create_lead(
        conn, account, "Grace Hopper", public_id="grace", first_name="Grace"
    )
    enrol_leads(conn, made.id, [ada.id, grace.id])
    return {"id": made.id, "leads": [ada.id, grace.id], "template": template.id}


class FakeSession:
    """A session that answers the two questions the notifier asks of it."""

    def __init__(self, *, subscribe: bool | None, standard: bool | None = None):
        self.sent: list[Any] = []
        self.client_params = _FakeParams(subscribe=subscribe, standard=standard)

    async def send_resource_updated(self, uri) -> None:
        self.sent.append(uri)


class _FakeCapabilities:
    def __init__(self, *, subscribe: bool | None, standard: bool | None):
        self.experimental = (
            None if subscribe is None else {"resources": {"subscribe": subscribe}}
        )
        self.resources = None if standard is None else _FakeStandard(standard)


class _FakeStandard:
    def __init__(self, subscribe: bool) -> None:
        self.subscribe = subscribe


class _FakeParams:
    def __init__(self, *, subscribe: bool | None, standard: bool | None):
        self.capabilities = _FakeCapabilities(subscribe=subscribe, standard=standard)


class DeadSession(FakeSession):
    """Declares the capability, then fails to deliver. Clients do this."""

    async def send_resource_updated(self, uri) -> None:
        raise ConnectionError("client went away")


# --------------------------------------------------------------------------
# Registration: the twelve exist on the server the process serves
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_shipped_server_registers_all_twelve_resources():
    served = {str(resource.uri) for resource in await SERVED.list_resources()}
    templated = {item.uri_template for item in await SERVED.list_resource_templates()}

    assert served == set(RESOURCE_URIS)
    assert templated == set(RESOURCE_TEMPLATE_URIS)
    assert len(served | templated) == 12
    assert set(ALL_RESOURCE_URIS) == served | templated


@pytest.mark.asyncio
async def test_every_resource_the_server_serves_is_json_under_the_linkedin_scheme():
    for resource in await SERVED.list_resources():
        assert str(resource.uri).startswith("linkedin://")
        assert resource.mime_type == RESOURCE_MIME_TYPE
        assert resource.description, f"{resource.uri} has no description"
    for item in await SERVED.list_resource_templates():
        assert item.uri_template.startswith("linkedin://")
        assert item.mime_type == RESOURCE_MIME_TYPE


def test_the_entry_point_actually_calls_the_registration_factory():
    """The PR #51 failure mode, checked directly.

    `tests/test_tool_registration.py` makes this promise for every
    `register_*_tools` factory. Resources need the same promise or they can be
    written, tested and merged while no client ever sees one.
    """
    tree = ast.parse(ENTRY_POINT.read_text(encoding="utf-8"))
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    assert "register_linkedin_resources" in called
    assert isinstance(
        linkedin_browser_mcp.linkedin_resource_notifier, ResourceUpdateNotifier
    )


def test_registering_twice_on_a_fresh_server_yields_the_same_surface():
    """The factory is reusable, and its notifier is per-registration.

    A module-level notifier shared between servers would leak one client's
    revision memory into another's, which is the sort of thing that only shows
    up under two concurrent clients.
    """
    first = register_linkedin_resources(FastMCP("first"))
    second = register_linkedin_resources(FastMCP("second"))

    assert first is not second
    first.remember("k", {"linkedin://campaigns": "1"})
    assert second.known("k") == {}


# --------------------------------------------------------------------------
# Live state: each URI reads what is actually in SQLite
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_campaigns_resource_reports_live_campaign_rows(conn, account, campaign):
    payload = await read("linkedin://campaigns")

    assert payload["uri"] == "linkedin://campaigns"
    assert payload["account_id"] == account
    assert payload["count"] == 1
    only = payload["campaigns"][0]
    assert only["id"] == campaign["id"]
    assert only["name"] == "Copilot rollout leads"
    assert only["status"] == "draft"
    assert only["funnel"]["queue"] == 2

    create_campaign(conn, account, "second one")
    again = await read("linkedin://campaigns")
    assert again["count"] == 2
    assert again["by_status"] == {"draft": 2}


@pytest.mark.asyncio
async def test_leads_resources_report_live_lead_rows(conn, account, campaign):
    listing = await read("linkedin://leads/active")
    assert listing["count"] == 2
    assert {lead["full_name"] for lead in listing["leads"]} == {
        "Ada Lovelace",
        "Grace Hopper",
    }
    assert all(lead["in_flight_campaigns"] == 1 for lead in listing["leads"])

    ada = campaign["leads"][0]
    detail = await read(lead_uri(ada))
    assert detail["lead"]["id"] == ada
    assert detail["lead"]["full_name"] == "Ada Lovelace"
    assert [entry["campaign_id"] for entry in detail["campaigns"]] == [campaign["id"]]


@pytest.mark.asyncio
async def test_templates_resource_reports_live_template_rows(conn, account):
    empty = await read("linkedin://templates")
    assert empty["count"] == 0

    create_template(conn, account, "invite", GOOD_NOTE)
    create_template(conn, account, "follow up", "Hi {firstName}, any thoughts?")

    payload = await read("linkedin://templates")
    assert payload["count"] == 2
    assert {template["name"] for template in payload["templates"]} == {
        "invite",
        "follow up",
    }


@pytest.mark.asyncio
async def test_stats_and_analytics_resources_read_the_audit_log(audit, account):
    before = await read("linkedin://stats/daily")
    assert before["actions"] == 0

    audit.record(account, "connection_request", "success")
    audit.record(account, "connection_request", "failure")
    audit.record(account, "profile_view", "success")

    daily = await read("linkedin://stats/daily")
    assert daily["actions"] == 3
    assert daily["by_action_type"]["connection_request"] == {"success": 1, "failure": 1}
    assert daily["by_action_type"]["profile_view"] == {"success": 1}
    assert daily["by_outcome"] == {"success": 2, "failure": 1}

    weekly = await read("linkedin://analytics/weekly")
    assert weekly["window_days"] == 7
    assert weekly["actions"] == 3
    assert weekly["invitations_sent"] == 1, (
        "the failed invitation never left, so it is not a sent invitation"
    )
    assert weekly["acceptance_rate"] is None
    assert "connection_accepted" in weekly["acceptance_rate_caveat"]


# --------------------------------------------------------------------------
# SEAM 2: what `linkedin://inbox/unread` means
# --------------------------------------------------------------------------


def store_message(
    conn, account_id: int, lead_id: int, direction: str, body: str, sent_at: str
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO messages (account_id, lead_id, direction, body, thread_urn,
                              sent_at, detected_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (account_id, lead_id, direction, body, f"urn:thread:{lead_id}", sent_at, sent_at),
    )
    conn.commit()
    return int(cursor.lastrowid)


@pytest.mark.asyncio
async def test_inbox_unread_means_they_replied_and_we_have_not_answered(
    conn, account, campaign
):
    """The definition is a choice, and it is the payload's job to say so.

    `messages` has no read flag: its columns are id, account_id, lead_id,
    direction, body, thread_urn, sent_at, detected_at. Nothing in the schema
    records whether a human looked at something, and nothing in
    `linkedin_mcp.inbox` reads message content without a Playwright page, which
    a resource is forbidden to open. So "unread" here means the newest stored
    message from a lead is inbound.
    """
    ada, grace = campaign["leads"]

    empty = await read("linkedin://inbox/unread")
    assert empty["count"] == 0
    assert empty["definition"] == UNREAD_DEFINITION
    assert "newest stored message is inbound" in empty["definition"]
    assert empty["excludes"]

    store_message(conn, account, ada, "outbound", "hello", "2026-03-01 09:00:00")
    assert (await read("linkedin://inbox/unread"))["count"] == 0

    store_message(conn, account, ada, "inbound", "sure, tell me more", "2026-03-01 10:00:00")
    replied = await read("linkedin://inbox/unread")
    assert replied["count"] == 1
    thread = replied["threads"][0]
    assert thread["lead_id"] == ada
    assert thread["full_name"] == "Ada Lovelace"
    assert thread["preview"] == "sure, tell me more"
    assert thread["ever_contacted"] is True
    assert thread["inbound_messages"] == 1
    assert thread["outbound_messages"] == 1

    store_message(conn, account, ada, "outbound", "here you go", "2026-03-01 11:00:00")
    assert (await read("linkedin://inbox/unread"))["count"] == 0

    store_message(conn, account, grace, "inbound", "who are you?", "2026-03-02 08:00:00")
    both = await read("linkedin://inbox/unread")
    assert [thread["lead_id"] for thread in both["threads"]] == [grace]


def test_unread_falls_back_to_insertion_order_when_timestamps_tie(conn, account, campaign):
    """Two messages in the same second must not make the answer arbitrary.

    A scan writes the outbound and the reply it found with whatever `sent_at`
    LinkedIn rendered, and those collide at one-second resolution. Ties break on
    the row id, so the last row written wins, which is the order the scan saw
    them in.
    """
    ada = campaign["leads"][0]
    store_message(conn, account, ada, "inbound", "hi", "2026-03-01 09:00:00")
    store_message(conn, account, ada, "outbound", "hello back", "2026-03-01 09:00:00")

    assert unread_threads(conn, account) == []

    store_message(conn, account, ada, "inbound", "one more", "2026-03-01 09:00:00")
    threads = unread_threads(conn, account)
    assert [thread["lead_id"] for thread in threads] == [ada]


def test_unread_ignores_other_accounts(conn, audit, account, campaign):
    other = audit.ensure_account("someone-else@example.com")
    ada = campaign["leads"][0]
    store_message(conn, other, ada, "inbound", "not yours", "2026-03-01 09:00:00")

    assert unread_threads(conn, account) == []
    assert [thread["lead_id"] for thread in unread_threads(conn, other)] == [ada]


# --------------------------------------------------------------------------
# SEAM 3: which draft status `linkedin://drafts/pending` shows
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drafts_pending_shows_the_ones_waiting_for_a_human(conn, account, campaign):
    """`list_pending` defaults to `needs_generation`; this resource does not.

    `needs_generation` means a draft the AI has not written yet. Nobody polls a
    resource called `drafts/pending` to watch a generation queue. They poll it
    because they are the human in the loop and something is waiting on them, and
    that status is `pending_approval`. The generation queue is still visible in
    `queue_depths`, so choosing one did not hide the other.
    """
    assert DRAFTS_PENDING_STATUS == "pending_approval"

    lead = campaign["leads"][0]
    request_draft(conn, campaign["id"], lead, "connection_note")
    waiting = request_draft(conn, campaign["id"], campaign["leads"][1], "connection_note")
    waiting = submit_draft(
        conn, waiting.id, text="Hi Grace, saw the rollout at Fabrikam. How is it going?"
    )
    assert waiting.status == "pending_approval"

    payload = await read("linkedin://drafts/pending")

    assert payload["status"] == "pending_approval"
    assert payload["count"] == 1
    assert payload["drafts"][0]["id"] == waiting.id
    assert payload["queue_depths"] == {"needs_generation": 1, "pending_approval": 1}
    assert "waiting for a human" in payload["definition"]


# --------------------------------------------------------------------------
# The worker, honestly
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_status_admits_a_stalled_worker(conn, account):
    """A heartbeat that says `running` and last ticked two hours ago is stalled.

    This is the DoD's honesty requirement. The arithmetic belongs to
    `linkedin_mcp.worker.worker_status`, which already does it; what is checked
    here is that the resource reports what that function says rather than
    parroting the `status` column.
    """
    now = datetime.now(timezone.utc)

    write_heartbeat(
        conn, "worker-1", account, "running", now=now - timedelta(seconds=170)
    )
    fresh = await read("linkedin://worker/status")
    assert fresh["stalled_workers"] == 0
    assert fresh["workers"][0]["status"] == "running"
    assert fresh["workers"][0]["stalled"] is False

    write_heartbeat(conn, "worker-1", account, "running", now=now - timedelta(hours=2))
    stalled = await read("linkedin://worker/status")

    assert stalled["stalled_workers"] == 1
    assert stalled["live_workers"] == 0
    worker = stalled["workers"][0]
    assert worker["status"] == "running", "the column still says running; that is the point"
    assert worker["stalled"] is True
    assert worker["age_seconds"] >= 7200
    assert stalled["stalled_after_seconds"] == 180


@pytest.mark.asyncio
async def test_worker_status_reports_an_empty_queue_without_a_worker(conn, account):
    payload = await read("linkedin://worker/status")

    assert payload["workers"] == []
    assert payload["live_workers"] == 0
    assert payload["pending_jobs"] == 0
    assert payload["campaigns_running"] is False
    assert payload["challenged_accounts"] == []


# --------------------------------------------------------------------------
# Safety headroom, per action type
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_safety_today_reports_remaining_headroom_for_every_metered_action(
    audit, account
):
    payload = await read("linkedin://safety/today")

    assert set(payload["actions"]) == set(METERED_ACTIONS)
    for action_type, block in payload["actions"].items():
        ceiling = HARD_CEILINGS[action_type]
        assert block["daily"]["cap"] <= ceiling.daily
        assert block["daily"]["used"] == 0
        assert block["daily"]["exhausted"] is False
        if "weekly" in block:
            assert block["weekly"]["cap"] <= ceiling.weekly
            assert block["remaining"] == min(
                block["daily"]["remaining"], block["weekly"]["remaining"]
            )
        else:
            assert block["remaining"] == block["daily"]["remaining"]


@pytest.mark.asyncio
async def test_safety_today_headroom_shrinks_as_actions_are_spent(audit, account):
    """`HARD_CEILINGS` is a mappingproxy, so this spends the real ledger.

    There is no way to patch the ceiling down to make this quick, which is
    deliberate on the config's part. So the test records real `actions_log` rows
    and watches the resource notice.
    """
    before = await read("linkedin://safety/today")
    start = before["actions"]["connection_request"]["remaining"]
    assert start > 0

    for _ in range(3):
        audit.record(account, "connection_request", "success")

    after = await read("linkedin://safety/today")
    block = after["actions"]["connection_request"]
    assert block["daily"]["used"] == 3
    assert block["remaining"] == start - 3
    assert after["actions"]["profile_view"]["daily"]["used"] == 0


@pytest.mark.asyncio
async def test_safety_today_reports_exhaustion_once_the_cap_is_spent(audit, account):
    cap = (await read("linkedin://safety/today"))["actions"]["connection_request"][
        "daily"
    ]["cap"]

    for _ in range(cap):
        audit.record(account, "connection_request", "success")

    block = (await read("linkedin://safety/today"))["actions"]["connection_request"]
    assert block["daily"]["used"] >= cap
    assert block["remaining"] == 0
    assert block["daily"]["exhausted"] is True
    assert block["weekly"]["exhausted"] is False


@pytest.mark.asyncio
async def test_safety_today_carries_the_global_ceilings_and_the_binding_one(
    audit, account
):
    payload = await read("linkedin://safety/today")

    assert payload["ceilings"]["global_daily"]["remaining"] >= 0
    assert payload["ceilings"]["global_hourly"]["remaining"] >= 0
    assert payload["ceilings"]["pending_invites"]["cap"] > 0
    assert payload["binding_limit"] in {
        "global_daily",
        "global_hourly",
        "pending_invites",
    }
    assert payload["account_state"] == "active"
    assert payload["challenged_accounts"] == []


# --------------------------------------------------------------------------
# Notifications, and the fallback for everyone else
# --------------------------------------------------------------------------


def test_capability_detection_keys_on_the_documented_field():
    assert CLIENT_CAPABILITY_PATH == "capabilities.experimental.resources.subscribe"

    supported, reason = client_supports_resource_updates(
        FakeSession(subscribe=True)
    )
    assert supported is True
    assert CLIENT_CAPABILITY_PATH in reason

    declined, reason = client_supports_resource_updates(FakeSession(subscribe=False))
    assert declined is False
    assert CLIENT_CAPABILITY_PATH in reason

    silent, _ = client_supports_resource_updates(FakeSession(subscribe=None))
    assert silent is False

    absent, reason = client_supports_resource_updates(None)
    assert absent is False
    assert "no live MCP session" in reason


def test_the_standard_capability_field_wins_if_a_client_ever_sends_one():
    """Forward compatibility, not speculation.

    Resource subscription is a server capability in the base spec today, so
    there is no `ClientCapabilities.resources` for a client to fill in. If a
    later revision adds one, this check starts reading it with no code change.
    """
    promoted = FakeSession(subscribe=False, standard=True)
    supported, _ = client_supports_resource_updates(promoted)
    assert supported is True


@pytest.mark.asyncio
async def test_a_capable_client_is_pushed_the_uris_that_moved(conn, account, campaign):
    notifier = ResourceUpdateNotifier()
    session = FakeSession(subscribe=True)

    first = await notifier.announce(
        session=session, revisions=resource_revisions(conn, account)
    )
    assert first.push is True
    assert first.notified == (), "a first read has nothing to compare against"
    assert session.sent == []

    create_campaign(conn, account, "something new")

    second = await notifier.announce(
        session=session, revisions=resource_revisions(conn, account)
    )
    assert second.push is True
    assert "linkedin://campaigns" in second.notified
    assert [str(uri) for uri in session.sent] == [
        str(as_notification_uri(uri)) for uri in second.notified
    ]
    assert second.as_payload()["method"] == "notifications/resources/updated"


@pytest.mark.asyncio
async def test_a_client_without_the_capability_is_told_how_long_to_wait(
    conn, account, campaign
):
    notifier = ResourceUpdateNotifier()
    session = FakeSession(subscribe=False)

    await notifier.announce(session=session, revisions=resource_revisions(conn, account))
    create_campaign(conn, account, "something new")
    delivery = await notifier.announce(
        session=session, revisions=resource_revisions(conn, account)
    )

    assert delivery.push is False
    assert session.sent == []
    payload = delivery.as_payload()
    assert payload["method"] == "poll"
    assert payload["poll_after_seconds"] == DEFAULT_POLL_AFTER_SECONDS
    assert "linkedin://campaigns" in payload["changed_since_last_read"]
    assert payload["client_capability"] == CLIENT_CAPABILITY_PATH


@pytest.mark.asyncio
async def test_a_client_that_declares_support_then_disappears_falls_back(
    conn, account, campaign
):
    notifier = ResourceUpdateNotifier()
    session = DeadSession(subscribe=True)

    await notifier.announce(session=session, revisions=resource_revisions(conn, account))
    create_campaign(conn, account, "something new")
    delivery = await notifier.announce(
        session=session, revisions=resource_revisions(conn, account)
    )

    assert delivery.push is False
    assert delivery.poll_after_seconds == DEFAULT_POLL_AFTER_SECONDS
    assert "linkedin://campaigns" in delivery.changed


def test_two_sessions_do_not_share_revision_memory(conn, account, campaign):
    notifier = ResourceUpdateNotifier()
    one, two = FakeSession(subscribe=True), FakeSession(subscribe=True)

    assert session_key(one) != session_key(two)
    revisions = resource_revisions(conn, account)
    notifier.remember(session_key(one), revisions)

    assert notifier.known(session_key(two)) == {}
    assert notifier.changed_uris(session_key(two), revisions) == ()


def test_a_status_flip_changes_the_campaign_fingerprint(conn, account, campaign):
    """Count-and-max fingerprints would miss the most important change there is.

    Starting a campaign writes no row and moves no id. It rewrites one column.
    If that did not move the fingerprint, a poller would never learn that the
    campaign it is watching had gone live.
    """
    before = resource_revisions(conn, account)["linkedin://campaigns"]
    set_campaign_status(conn, campaign["id"], "pending_approval")
    after = resource_revisions(conn, account)["linkedin://campaigns"]

    assert before != after


@pytest.mark.asyncio
async def test_the_polling_fallback_is_wired_into_the_shipped_resources(
    conn, account, campaign
):
    """The fallback is not just unit-testable, it is what a real read returns.

    An in-process read has no MCP session at all, which is the same situation a
    client that never declared the capability is in as far as this server is
    concerned: nothing will be pushed, so the body has to carry the news.
    """
    first = await read("linkedin://campaigns")
    assert first["updates"]["push"] is False
    assert first["updates"]["poll_after_seconds"] == DEFAULT_POLL_AFTER_SECONDS
    assert first["updates"]["changed_since_last_read"] == []

    create_lead(conn, account, "Katherine Johnson", public_id="katherine")

    second = await read("linkedin://campaigns")
    assert "linkedin://leads/active" in second["updates"]["changed_since_last_read"]
    assert "linkedin://campaigns" not in second["updates"]["changed_since_last_read"], (
        "the URI being read is excluded; telling a client the thing in its hand "
        "just changed is how you build a polling loop"
    )


# --------------------------------------------------------------------------
# VAL-03: a real campaign, read through the shipped server
# --------------------------------------------------------------------------


@pytest.fixture()
def campaign_tools(audit):
    """A server carrying the campaign tools, for the writes.

    PR #51 registers these on the shipped server. Until it lands, VAL-03 still
    needs a real MCP client driving the real approval path, so it gets one here
    while every read goes through `linkedin_browser_mcp.mcp`.
    """
    mcp = FastMCP("campaign-writes")
    register_campaign_tools(mcp)
    return mcp


async def call(server: FastMCP, name: str, **arguments) -> dict[str, Any]:
    async with Client(server) as client:
        result = await client.call_tool(name, arguments)
    return result.data


@pytest.mark.asyncio
async def test_val_03_a_client_reads_accurate_live_campaign_state(
    conn, account, campaign, campaign_tools
):
    """The acceptance criterion, end to end.

    Build a campaign, move it through the real approval path with real MCP tool
    calls, and read `linkedin://campaigns/{id}` off the shipped server after
    every transition. The returned state is compared against the database rather
    than against itself.
    """
    campaign_id = campaign["id"]
    uri = campaign_uri(campaign_id)

    payload = await read(uri)
    row = get_campaign(conn, campaign_id)
    assert payload["campaign"]["id"] == campaign_id
    assert payload["campaign"]["name"] == row.name == "Copilot rollout leads"
    assert payload["campaign"]["status"] == row.status == "draft"
    assert payload["funnel"]["queue"] == 2
    assert len(payload["steps"]) == 1
    assert payload["steps"][0]["action_type"] == "connection_request"

    approved = await call(campaign_tools, "campaign_approve", campaign_id=campaign_id)
    assert approved["status"] == "success"

    after_approval = await read(uri)
    assert after_approval["campaign"]["status"] == get_campaign(conn, campaign_id).status
    assert after_approval["campaign"]["status"] == "pending_approval"
    assert after_approval["campaign"]["approved"] is True
    assert after_approval["campaign"]["runnable"] is False
    assert after_approval["campaign"]["editable"] is False
    assert after_approval["status_meaning"]["pending_approval"] == (
        "approved, waiting to be started"
    )

    started = await call(campaign_tools, "campaign_start", campaign_id=campaign_id)
    assert started["status"] == "success"

    running = await read(uri)
    assert running["campaign"]["status"] == "active"
    assert running["campaign"]["runnable"] is True
    assert running["due_now"] >= 0
    assert running["runnable_statuses"] == ["active"]
    assert running["worker"]["campaigns_running"] is False, (
        "an active campaign with no live worker is not running, and the payload "
        "says so rather than inferring motion from the status column"
    )

    paused = await call(campaign_tools, "campaign_pause", campaign_id=campaign_id)
    assert paused["status"] == "success"

    halted = await read(uri)
    assert halted["campaign"]["status"] == "paused"
    assert halted["campaign"]["approved"] is True
    assert halted["campaign"]["runnable"] is False


@pytest.mark.asyncio
async def test_val_03_the_derived_flags_match_the_tool_for_every_status(
    conn, account, campaign, campaign_tools
):
    """SEAM 4, pinned across all six statuses.

    `pending_approval` reads in English as "needs approval" and means the
    opposite. A client shown the bare word would conclude a human has to act
    when what the campaign actually needs is starting, so the resource carries
    the three derived booleans and this checks them against
    `campaign_status`, the tool a client would otherwise have to call.
    """
    campaign_id = campaign["id"]
    expected = {
        "draft": (False, False, True),
        "pending_approval": (True, False, False),
        "active": (True, True, False),
        "paused": (True, False, False),
        "completed": (True, False, False),
        "archived": (False, False, False),
    }

    for status, (approved, runnable, editable) in expected.items():
        set_campaign_status(conn, campaign_id, status)

        payload = (await read(campaign_uri(campaign_id)))["campaign"]
        from_tool = await call(
            campaign_tools, "campaign_status", campaign_id=campaign_id
        )

        assert payload["status"] == status
        assert (payload["approved"], payload["runnable"], payload["editable"]) == (
            approved,
            runnable,
            editable,
        ), status
        assert payload["approved"] == from_tool["campaign"]["approved"]
        assert payload["runnable"] == from_tool["campaign"]["runnable"]
        assert payload["editable"] == from_tool["campaign"]["editable"]


@pytest.mark.asyncio
async def test_the_funnel_resource_agrees_with_the_campaign_resource(
    conn, account, campaign
):
    detail = await read(campaign_uri(campaign["id"]))
    funnel = await read(campaign_funnel_uri(campaign["id"]))

    assert funnel["campaign_id"] == campaign["id"]
    assert funnel["funnel"] == detail["funnel"]
    assert funnel["campaign_status"] == detail["campaign"]["status"]
    assert funnel["runnable"] == detail["campaign"]["runnable"]


@pytest.mark.asyncio
async def test_a_campaign_belonging_to_another_account_is_not_readable(
    conn, audit, account, campaign
):
    """Account scoping is the resource's job, not the caller's.

    `get_campaign` takes an id and no account, so a resource that trusted it
    would happily serve another account's campaign to whoever asked.
    """
    other = audit.ensure_account("someone-else@example.com")
    theirs = create_campaign(conn, other, "not yours")

    with pytest.raises(Exception) as caught:
        await read(campaign_uri(theirs.id))
    assert str(theirs.id) in str(caught.value)

    with pytest.raises(Exception):
        await read(campaign_uri(9999))


@pytest.mark.asyncio
async def test_a_lead_belonging_to_another_account_is_not_readable(
    conn, audit, account, campaign
):
    other = audit.ensure_account("someone-else@example.com")
    theirs = create_lead(conn, other, "Not Yours", public_id="not-yours")

    with pytest.raises(Exception):
        await read(lead_uri(theirs.id))


# --------------------------------------------------------------------------
# The promise the audit exemption rests on
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reading_every_resource_writes_no_audit_row(
    audit, conn, account, campaign
):
    """The runtime half of the exemption in `tests/test_audit_log.py`.

    Resources carry no `@audit_linkedin_action` because they spend no LinkedIn
    budget. If that were wrong, `actions_log` would grow here, and the budget
    `linkedin://safety/today` reports would shrink because somebody looked at
    it.
    """

    def counts() -> tuple[int, int]:
        actions = conn.execute("SELECT COUNT(*) AS n FROM actions_log").fetchone()["n"]
        jobs = conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
        return int(actions), int(jobs)

    before = counts()

    for uri in RESOURCE_URIS:
        await read(uri)
    await read(campaign_uri(campaign["id"]))
    await read(campaign_funnel_uri(campaign["id"]))
    await read(lead_uri(campaign["leads"][0]))

    assert counts() == before

    headroom = await read("linkedin://safety/today")
    assert headroom["actions"]["profile_view"]["daily"]["used"] == 0


# --------------------------------------------------------------------------
# Housekeeping
# --------------------------------------------------------------------------


def test_the_advertised_poll_interval_matches_the_worker_tick():
    """Thirty seconds is not a taste; it is `WorkerConfig.tick_seconds`.

    Polling faster than the worker ticks cannot reveal anything the previous
    read did not already show. If the worker's tick ever changes, the interval
    this server advertises should move with it rather than quietly becoming a
    number nobody can justify.
    """
    from linkedin_mcp.worker import WorkerConfig

    assert DEFAULT_POLL_AFTER_SECONDS == int(WorkerConfig(account_id=1).tick_seconds)


def test_this_change_added_no_migration_of_its_own():
    """Scoped to filenames this branch could have created, deliberately.

    Asserting the global migration list is what two sessions did in wave 4, and
    it meant an unrelated branch adding a legitimate migration failed a test
    belonging to somebody else. So this only says "#27 did not add one", which
    is the claim it is entitled to make.
    """
    added = sorted(
        path.name
        for path in MIGRATIONS.glob("*.sql")
        if path.name.startswith(("0004_resource", "0004_inbox", "0004_unread"))
    )

    assert added == [], (
        "MCP-04 defines 'unread' from the columns `messages` already has, so it "
        f"should not have added a migration: {added}"
    )
