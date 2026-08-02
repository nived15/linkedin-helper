"""End to end runs against a temporary database and a fake page."""

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from test_scrape_fakes import (
    FakeElement,
    FakeGate,
    FakePage,
    FakeRecorder,
    FakeScrollPage,
    RecordingSleep,
    group_member_card,
    node,
    people_pages,
    person_card,
    post_card,
)

from linkedin_mcp.audit import count_actions_in_window, instrument
from linkedin_mcp.audit.log import (
    AuditLog,
    Outcome,
    reset_audit_log,
    set_audit_log,
)
from linkedin_mcp.browser.humanize import FAST, Humanizer
from linkedin_mcp.core.config import HARD_CEILINGS
from linkedin_mcp.core.db import initialize_database
from linkedin_mcp.leads import (
    CACHE_WINDOW_DAYS,
    HarvestRefusalReason,
    LeadSection,
    blacklist_lead,
    create_lead,
    get_lead_by_public_id,
    mark_section_fetched,
)
from linkedin_mcp.safety.gate import SafetyGate, guard_action, reset_gate, set_gate
from linkedin_mcp.scrape import (
    PeopleSearchFilters,
    PostSearchFilters,
    ScrapeSummary,
    SearchCursor,
    StopReason,
    harvest_run,
    resume_cursor,
    run_group_member_extraction,
    run_people_search,
    run_post_search,
)
from linkedin_mcp.scrape.groups import GROUP_MEMBERS_ACTION
from linkedin_mcp.scrape.people import PEOPLE_SEARCH_ACTION
from linkedin_mcp.scrape.posts import POST_SEARCH_ACTION

NOW = datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc)


def run(coroutine):
    return asyncio.run(coroutine)


def pacer():
    return Humanizer(FAST, seed=5, sleep=RecordingSleep())


def clock():
    return NOW


@pytest.fixture()
def conn(tmp_path):
    connection = initialize_database(tmp_path / "linkedin-helper.db")
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture()
def account(conn):
    cursor = conn.execute(
        "INSERT INTO accounts (label, timezone, state) VALUES (?, ?, ?)",
        ("primary", "Asia/Kolkata", "active"),
    )
    conn.commit()
    return int(cursor.lastrowid)


def people_search(page, conn, account, **kwargs):
    kwargs.setdefault("humanizer", pacer())
    kwargs.setdefault("guard", FakeGate())
    kwargs.setdefault("record", FakeRecorder())
    kwargs.setdefault("clock", clock)
    filters = kwargs.pop("filters", PeopleSearchFilters(keywords="platform engineer"))
    return run(run_people_search(page, conn, account, filters, **kwargs))


def test_a_people_search_stores_every_result_as_a_lead(conn, account):
    page = FakePage(people_pages(25))

    summary = people_search(page, conn, account, limit=25)

    assert isinstance(summary, ScrapeSummary)
    assert summary.stop_reason is StopReason.COUNT_REACHED
    assert summary.results_new == 25
    assert summary.leads_created == 25
    assert summary.action_type == PEOPLE_SEARCH_ACTION
    assert conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 25

    stored = get_lead_by_public_id(conn, account, "person-0")
    assert stored is not None
    assert stored.full_name == "Person 0"


def test_the_search_url_carries_the_filters_and_the_page_number(conn, account):
    page = FakePage(people_pages(25))
    filters = PeopleSearchFilters(
        keywords="platform engineer",
        connection_degrees=("2nd",),
        geo_urns=("103644278",),
        title="Staff Engineer",
    )

    people_search(page, conn, account, limit=25, filters=filters)

    assert len(page.goto_urls) == 3
    first = page.goto_urls[0]
    assert first.startswith("https://www.linkedin.com/search/results/people/?")
    assert "keywords=platform%20engineer" in first
    assert "network=%5B%22S%22%5D" in first
    assert "geoUrn=%5B%22103644278%22%5D" in first
    assert "titleFreeText=Staff%20Engineer" in first
    assert "page=" not in first
    assert "page=2" in page.goto_urls[1]
    assert page.goto_kwargs[0]["wait_until"] == "domcontentloaded"


def test_a_run_writes_a_harvest_row_carrying_its_cursor(conn, account):
    page = FakePage(people_pages(25))

    summary = people_search(page, conn, account, limit=15)

    row = harvest_run(conn, summary.harvest_run_id)
    assert row["source_type"] == "people_search"
    assert row["account_id"] == account
    assert row["found_count"] == 20
    assert row["new_count"] == 15
    assert row["finished_at"] is not None
    assert row["params"]["filters"]["keywords"] == "platform engineer"
    assert resume_cursor(conn, summary.harvest_run_id) == summary.cursor


def test_a_second_run_resumes_where_the_first_stopped(conn, account):
    page = FakePage(people_pages(40))

    first = people_search(page, conn, account, limit=15)
    second = people_search(page, conn, account, limit=15, cursor=first.cursor)

    assert first.leads_created == 15
    assert second.leads_created == 15
    assert conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 30
    assert second.cursor.collected == 30


def test_a_resumed_run_does_not_re_harvest_the_people_it_already_stored(conn, account):
    page = FakePage(people_pages(20))

    first = people_search(page, conn, account, limit=10)
    second = people_search(page, conn, account, limit=20, cursor=first.cursor)

    assert second.results_new == 10
    assert second.leads_created == 10
    assert second.leads_updated == 0
    assert conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 20


def test_a_repeat_search_updates_rather_than_duplicates(conn, account):
    page = FakePage(people_pages(10))

    people_search(page, conn, account, limit=10)
    second = people_search(page, conn, account, limit=10)

    assert second.leads_created == 0
    assert second.leads_updated + second.leads_unchanged == 10
    assert conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 10


def test_a_blacklisted_person_is_refused_rather_than_resurrected(conn, account):
    lead = create_lead(
        conn,
        account,
        "Person 3",
        public_id="person-3",
        member_id="urn:li:member:1003",
    )
    blacklist_lead(conn, lead.id, reason="asked not to be contacted")
    page = FakePage(people_pages(10))

    summary = people_search(page, conn, account, limit=10)

    assert summary.results_new == 10
    assert summary.leads_created == 9
    assert [refusal.reason for refusal in summary.harvest.refusals] == [
        HarvestRefusalReason.BLACKLISTED
    ]
    assert summary.harvest.refusals[0].public_id == "person-3"
    assert lead.id not in summary.lead_ids
    assert lead.id not in summary.stale_lead_ids


def test_a_gate_refusal_stops_the_run_and_lands_in_the_summary(conn, account):
    page = FakePage(people_pages(40))

    summary = people_search(page, conn, account, limit=40, guard=FakeGate(allow=2))

    assert summary.refused
    assert summary.stop_reason is StopReason.GATE_REFUSED
    assert summary.gate_refusal["reason"] == "daily_cap_reached"
    assert summary.leads_created == 20
    assert summary.as_dict()["status"] == "refused"
    assert harvest_run(conn, summary.harvest_run_id)["new_count"] == 20


def test_a_dry_run_extracts_without_writing_anything(conn, account):
    page = FakePage(people_pages(10))

    summary = people_search(page, conn, account, limit=10, harvest=False)

    assert summary.results_new == 10
    assert summary.harvest_run_id is None
    assert conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM harvest_runs").fetchone()[0] == 0


def test_only_the_leads_past_their_cache_window_are_reported_as_stale(conn, account):
    page = FakePage(people_pages(3))

    people_search(page, conn, account, limit=3)
    fresh = get_lead_by_public_id(conn, account, "person-0")
    mark_section_fetched(conn, fresh.id, LeadSection.POSITIONS, fetched_at=NOW)
    second = people_search(page, conn, account, limit=3)

    assert fresh.id not in second.stale_lead_ids
    assert len(second.stale_lead_ids) == 2


def test_a_stale_section_comes_back_once_its_window_has_passed(conn, account):
    page = FakePage(people_pages(1))

    people_search(page, conn, account, limit=1)
    lead = get_lead_by_public_id(conn, account, "person-0")
    long_ago = NOW - timedelta(days=CACHE_WINDOW_DAYS[LeadSection.POSITIONS] + 1)
    mark_section_fetched(conn, lead.id, LeadSection.POSITIONS, fetched_at=long_ago)

    second = people_search(page, conn, account, limit=1)

    assert second.stale_lead_ids == (lead.id,)


def test_the_summary_serialises_to_a_tool_friendly_payload(conn, account):
    page = FakePage(people_pages(5))

    payload = people_search(page, conn, account, limit=5).as_dict()

    assert payload["status"] == "success"
    assert payload["source"] == "people_search"
    assert payload["action_type"] == "profile_search"
    assert payload["stop_reason"] == "count_reached"
    assert payload["leads_created"] == 5
    assert payload["cursor"]["page"] == 2
    assert json.dumps(payload)


def test_a_post_search_stores_the_authors_and_returns_the_posts(conn, account):
    pages = {
        1: [
            post_card(
                activity=f"urn:li:activity:71000{index}",
                permalink=f"https://www.linkedin.com/feed/update/urn:li:activity:71000{index}/",
                author_slug=f"author-{index}",
                author_name=f"Author {index}",
            )
            for index in range(3)
        ]
    }
    page = FakePage(pages)
    filters = PostSearchFilters(keywords="github copilot", date_posted="past-week")

    summary = run(
        run_post_search(
            page,
            conn,
            account,
            filters,
            limit=3,
            humanizer=pacer(),
            guard=FakeGate(),
            record=FakeRecorder(),
            clock=clock,
        )
    )

    assert summary.action_type == POST_SEARCH_ACTION
    assert summary.results_new == 3
    assert summary.leads_created == 3
    assert [post.activity_id for post in summary.posts] == ["710000", "710001", "710002"]
    assert "datePosted=%5B%22past-week%22%5D" in page.goto_urls[0]
    assert page.goto_urls[0].startswith(
        "https://www.linkedin.com/search/results/content/?"
    )
    assert get_lead_by_public_id(conn, account, "author-0") is not None
    assert summary.as_dict()["posts"][0]["activity_id"] == "710000"


def test_a_post_search_survives_a_page_of_posts_with_no_authors(conn, account):
    page = FakePage({1: [post_card(author_slug=None, author_name=None)]})

    summary = run(
        run_post_search(
            page,
            conn,
            account,
            PostSearchFilters(keywords="copilot"),
            limit=5,
            humanizer=pacer(),
            guard=FakeGate(),
            record=FakeRecorder(),
            clock=clock,
        )
    )

    assert summary.results_new == 1
    assert summary.leads_created == 0
    assert summary.stop_reason is StopReason.NO_NEW_RESULTS


def test_group_members_load_more_until_the_list_runs_out(conn, account):
    cards = [
        group_member_card(slug=f"member-{index}", name=f"Member {index}")
        for index in range(9)
    ]
    button = node("group_member_load_more")
    page = FakeScrollPage(cards, step=3, load_more_button=button, reveal_on="click")

    summary = run(
        run_group_member_extraction(
            page,
            conn,
            account,
            "https://www.linkedin.com/groups/1234567/",
            limit=50,
            humanizer=pacer(),
            guard=FakeGate(),
            record=FakeRecorder(),
            clock=clock,
        )
    )

    assert summary.source == "group_members"
    assert summary.action_type == GROUP_MEMBERS_ACTION
    assert summary.results_new == 9
    assert summary.leads_created == 9
    assert summary.stop_reason is StopReason.NO_NEW_RESULTS
    assert page.goto_urls == ["https://www.linkedin.com/groups/1234567/members/"]
    assert button.clicks >= 1


def test_group_members_stop_at_the_requested_count(conn, account):
    cards = [group_member_card(slug=f"member-{index}") for index in range(30)]
    page = FakeScrollPage(cards, step=5)

    summary = run(
        run_group_member_extraction(
            page,
            conn,
            account,
            9876543,
            limit=10,
            humanizer=pacer(),
            guard=FakeGate(),
            record=FakeRecorder(),
            clock=clock,
        )
    )

    assert summary.stop_reason is StopReason.COUNT_REACHED
    assert summary.results_new == 10
    assert page.goto_urls == ["https://www.linkedin.com/groups/9876543/members/"]


def test_a_resumed_group_run_replays_its_load_more_steps(conn, account):
    cards = [group_member_card(slug=f"member-{index}") for index in range(30)]
    page = FakeScrollPage(cards, step=5)

    first = run(
        run_group_member_extraction(
            page,
            conn,
            account,
            "1234567",
            limit=10,
            humanizer=pacer(),
            guard=FakeGate(),
            record=FakeRecorder(),
            clock=clock,
        )
    )
    second = run(
        run_group_member_extraction(
            page,
            conn,
            account,
            "1234567",
            limit=10,
            cursor=first.cursor,
            humanizer=pacer(),
            guard=FakeGate(),
            record=FakeRecorder(),
            clock=clock,
        )
    )

    assert second.results_new == 10
    assert conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 20
    assert len(page.goto_urls) == 2


def test_a_card_that_cannot_be_read_does_not_stop_the_harvest(conn, account):
    broken = FakeElement(selectors=("div.reusable-search__result-container",), explode=True)
    page = FakePage({1: [broken, person_card(slug="survivor", name="Survivor")]})

    summary = people_search(page, conn, account, limit=5)

    assert summary.leads_created == 1
    assert get_lead_by_public_id(conn, account, "survivor") is not None


class GateHarness:
    """The real SafetyGate, wired to a temporary audit log."""

    def __init__(self, tmp_path, conn):
        self.log = AuditLog.open(tmp_path / "audit.db")
        set_audit_log(self.log)
        self.account_id = self.log.ensure_account("scrape@example.com")
        instrument.set_account_resolver(lambda: self.account_id)
        self.gate = SafetyGate(clock=lambda: NOW, jitter=lambda _a, _m: 0.0)
        set_gate(self.gate)
        # The audit ledger and the lead store are separate databases, so the
        # account row has to exist in both under the same id.
        conn.execute(
            "INSERT INTO accounts (id, label, timezone, state) VALUES (?, ?, ?, ?)",
            (self.account_id, "gated", "Asia/Kolkata", "active"),
        )
        conn.commit()

    def spend(self, action_type, count):
        for index in range(1, count + 1):
            self.log.record(
                self.account_id,
                action_type,
                Outcome.SUCCESS,
                occurred_at=NOW - timedelta(minutes=5 * index),
            )

    def close(self):
        reset_audit_log()
        reset_gate()
        instrument.reset_account_resolver()
        self.log.close()


@pytest.fixture()
def real_gate(tmp_path, conn):
    harness = GateHarness(tmp_path, conn)
    try:
        yield harness
    finally:
        harness.close()


def test_the_real_safety_gate_stops_a_run_at_the_daily_search_cap(conn, real_gate):
    cap = HARD_CEILINGS[PEOPLE_SEARCH_ACTION].daily
    real_gate.spend(PEOPLE_SEARCH_ACTION, cap - 2)
    page = FakePage(people_pages(100))

    summary = run(
        run_people_search(
            page,
            conn,
            real_gate.account_id,
            PeopleSearchFilters(keywords="engineer"),
            limit=100,
            humanizer=pacer(),
            guard=guard_action,
            clock=clock,
        )
    )

    assert summary.stop_reason is StopReason.GATE_REFUSED
    assert summary.pages_fetched == 2
    assert summary.gate_refusal["reason"] == "daily_cap_reached"
    assert summary.results_new == 20


def test_the_real_audit_log_records_one_row_per_fetched_page(conn, real_gate):
    page = FakePage(people_pages(25))

    summary = run(
        run_people_search(
            page,
            conn,
            real_gate.account_id,
            PeopleSearchFilters(keywords="engineer"),
            limit=25,
            humanizer=pacer(),
            guard=guard_action,
            clock=clock,
        )
    )

    rows = real_gate.log.connection.execute(
        "SELECT action_type, outcome, detail_json FROM actions_log "
        "WHERE action_type = ? ORDER BY id",
        (PEOPLE_SEARCH_ACTION,),
    ).fetchall()

    assert len(rows) == summary.pages_fetched == 3
    assert {row["outcome"] for row in rows} == {"success"}
    assert [json.loads(row["detail_json"])["page"] for row in rows] == [1, 2, 3]
    assert {json.loads(row["detail_json"])["source"] for row in rows} == {"people_search"}


def test_the_gate_sees_the_page_spend_it_was_told_about(conn, real_gate):
    page = FakePage(people_pages(30))

    run(
        run_people_search(
            page,
            conn,
            real_gate.account_id,
            PeopleSearchFilters(keywords="engineer"),
            limit=30,
            humanizer=pacer(),
            guard=guard_action,
            clock=clock,
        )
    )

    spent = count_actions_in_window(
        real_gate.account_id,
        PEOPLE_SEARCH_ACTION,
        since=NOW - timedelta(days=1),
        until=NOW + timedelta(minutes=1),
    )
    assert spent == 3
