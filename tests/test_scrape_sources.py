"""The people-list engine SCRAPE-04 drives every non-search lead source with.

Nothing here touches a browser or LinkedIn. The fakes below are the SCRAPE-04
half of the duck-typed page from `test_scrape_fakes`, extended with the two
things a lazily loaded list needs and a search results page does not: an element
that answers `scroll_into_view_if_needed`, which is how a modal's own scroll
container gets moved, and a page whose rows only appear after a control is
clicked.

Selector matching stays exact string membership, so a test has to name the
selector the code will actually try. A registry edit that breaks a caller shows
up here rather than against a live session.
"""

import asyncio
import sqlite3
from datetime import datetime, timezone

import pytest
from test_scrape_fakes import (
    FakeElement,
    FakeGate,
    FakePage,
    FakeRecorder,
    RecordingSleep,
    node,
)

from linkedin_mcp.browser.humanize import FAST, Humanizer
from linkedin_mcp.browser.selectors import selector_fallbacks
from linkedin_mcp.core.config import METERED_ACTIONS
from linkedin_mcp.core.db import initialize_database
from linkedin_mcp.leads import blacklist_identity, count_leads, get_lead_by_public_id
from linkedin_mcp.scrape import (
    SOURCE_COMPANY_EMPLOYEES,
    SOURCE_CONNECTIONS,
    SOURCE_EVENT_ATTENDEES,
    SOURCE_FOLLOWERS,
    ScrapeSummary,
    SearchCursor,
    StopReason,
    harvest_run,
    run_company_employee_harvest,
    run_connection_harvest,
    run_event_attendee_harvest,
    run_follower_harvest,
)
from linkedin_mcp.scrape.connections import (
    COMPANY_EMPLOYEE_SURFACE,
    CONNECTION_SURFACE,
    FOLLOWER_SURFACE,
)
from linkedin_mcp.scrape.events import ATTENDEE_SURFACE, EVENT_ATTENDEES_ACTION
from linkedin_mcp.scrape.sources import (
    CONNECTIONS_URL,
    FOLLOWERS_URL,
    PEOPLE_LIST_ACTION,
    PeopleListSurface,
    combine_summaries,
    company_id_from,
    company_people_url,
    event_attendees_url,
    event_id_from,
    extract_people_list,
    post_permalink,
)

NOW = datetime(2026, 8, 2, 9, 0, tzinfo=timezone.utc)


def run(coroutine):
    return asyncio.run(coroutine)


def pacer():
    return Humanizer(FAST, seed=7, sleep=RecordingSleep())


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


class ListRow(FakeElement):
    """A row that can be asked to scroll itself into view, as Playwright can.

    The reactions list lives in a modal, and a window scroll does not move a
    modal. Pulling the last row into view is how the production code moves
    whichever element is really scrollable, so the fake has to expose it.
    """

    def __init__(self, *args, on_reveal=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.on_reveal = on_reveal
        self.reveals = 0

    async def scroll_into_view_if_needed(self) -> None:
        self.reveals += 1
        if self.on_reveal is not None:
            self.on_reveal()


def list_row(
    surface: PeopleListSurface,
    *,
    slug: str | None = "nived-velayudhan",
    name: str | None = "Nived Velayudhan",
    headline: str | None = "Solution Engineer at Microsoft",
    distance: str | None = "2nd degree connection",
    avatar: str | None = "https://media.licdn.com/avatar.jpg",
    urn: str | None = None,
    on_reveal=None,
    explode: bool = False,
    fallback: int = 0,
) -> ListRow:
    """Build one row of a people list, carrying only the fields given.

    `fallback` picks which entry of each selector chain the row answers to, so a
    test can render the markup LinkedIn served two redesigns ago and check the
    chain still resolves it.
    """
    children: list[FakeElement] = []
    if slug is not None:
        children.append(
            node(
                surface.link,
                index=fallback,
                href=f"https://www.linkedin.com/in/{slug}?trk=search",
            )
        )
    if name is not None:
        children.append(node(surface.name, index=fallback, text=name))
    if headline is not None and surface.headline:
        children.append(node(surface.headline, index=fallback, text=headline))
    if distance is not None and surface.distance:
        children.append(node(surface.distance, index=fallback, text=distance))
    if avatar is not None and surface.avatar:
        children.append(node(surface.avatar, index=fallback, src=avatar))
    return ListRow(
        selectors=(selector_fallbacks(surface.item)[fallback],),
        attrs={"data-chameleon-result-urn": urn} if urn else {},
        children=children,
        on_reveal=on_reveal,
        explode=explode,
    )


class PeopleListPage(FakePage):
    """A lazily loaded people list that reveals one more slice per gesture.

    `reveal_on` picks the gesture, because LinkedIn uses both and the difference
    matters: a button is a click the humanizer paces, and a container scroll is
    a row asking to be brought into view.
    """

    def __init__(
        self,
        rows: list[ListRow],
        surface: PeopleListSurface,
        *,
        step: int = 3,
        reveal_on: str = "click",
        url: str = "https://www.linkedin.com/feed/",
    ) -> None:
        super().__init__(url=url)
        self.rows = list(rows)
        self.surface = surface
        self.step = step
        self.revealed = step
        self.reveal_on = reveal_on
        self.button = FakeElement(
            selectors=(selector_fallbacks(surface.load_more)[0],),
            on_click=self.reveal_more,
        )
        if reveal_on == "view":
            for row in self.rows:
                row.on_reveal = self.reveal_more

    def reveal_more(self) -> None:
        self.revealed = min(len(self.rows), self.revealed + self.step)

    async def goto(self, url: str, **kwargs) -> None:
        await super().goto(url, **kwargs)
        self.revealed = self.step

    @property
    def visible(self) -> list[FakeElement]:
        shown: list[FakeElement] = list(self.rows[: self.revealed])
        if self.reveal_on == "click" and self.revealed < len(self.rows):
            shown.append(self.button)
        return shown


def rows_for(
    surface: PeopleListSurface, count: int, *, prefix: str = "person", start: int = 0
) -> list[ListRow]:
    """Build `count` distinct rows for a surface."""
    return [
        list_row(
            surface,
            slug=f"{prefix}-{index}",
            name=f"Person {index}",
            urn=f"urn:li:member:{2000 + index}",
        )
        for index in range(start, start + count)
    ]


def harvest_kwargs(**overrides):
    kwargs = {
        "humanizer": pacer(),
        "guard": FakeGate(),
        "record": FakeRecorder(),
        "clock": clock,
    }
    kwargs.update(overrides)
    return kwargs


# --- URL and identifier helpers ------------------------------------------


def test_a_post_is_reachable_from_an_id_a_urn_or_a_permalink():
    expected = "https://www.linkedin.com/feed/update/urn:li:activity:7123456789/"

    assert post_permalink("7123456789") == expected
    assert post_permalink("urn:li:activity:7123456789") == expected
    assert post_permalink(7123456789) == expected
    assert (
        post_permalink(
            "https://www.linkedin.com/feed/update/urn:li:activity:7123456789/?utm=x"
        )
        == expected
    )


def test_a_share_link_with_no_activity_urn_is_kept_for_linkedin_to_resolve():
    share = "https://www.linkedin.com/posts/nived-velayudhan_copilot-activity-42-abc"

    assert post_permalink(share) == share


@pytest.mark.parametrize("bad", ["", "   ", "not a post", "https://example.com/x"])
def test_something_that_is_not_a_post_raises_rather_than_being_guessed_at(bad):
    with pytest.raises(ValueError):
        post_permalink(bad)


def test_an_event_is_reachable_from_an_id_a_slug_or_a_url():
    assert event_id_from("1234567890") == "1234567890"
    assert event_id_from("https://www.linkedin.com/events/1234567890/") == "1234567890"
    assert (
        event_id_from("https://www.linkedin.com/events/ai-devtools-summit-9911/?x=1")
        == "ai-devtools-summit-9911"
    )
    assert (
        event_attendees_url("1234567890")
        == "https://www.linkedin.com/events/1234567890/attendees/"
    )


def test_an_unknown_event_tab_is_refused_rather_than_navigated_to():
    with pytest.raises(ValueError):
        event_attendees_url("1234567890", "networking")


def test_a_company_is_reachable_from_a_slug_or_a_url():
    assert company_id_from("microsoft") == "microsoft"
    assert company_id_from("https://www.linkedin.com/company/microsoft/") == "microsoft"
    assert company_id_from("https://www.linkedin.com/school/mit/about/") == "mit"
    assert (
        company_people_url("microsoft")
        == "https://www.linkedin.com/company/microsoft/people/"
    )


@pytest.mark.parametrize("bad", ["", "  ", "https://example.com/nothing"])
def test_something_that_is_not_a_company_raises(bad):
    with pytest.raises(ValueError):
        company_id_from(bad)


# --- Generic extraction ---------------------------------------------------


def test_a_full_row_reads_into_a_person():
    page = FakePage({1: [list_row(ATTENDEE_SURFACE)]})

    people = run(extract_people_list(page, ATTENDEE_SURFACE))

    assert len(people) == 1
    person = people[0]
    assert person.full_name == "Nived Velayudhan"
    assert person.public_id == "nived-velayudhan"
    assert person.headline == "Solution Engineer at Microsoft"
    assert person.member_distance == "2nd"
    assert person.profile_url == "https://www.linkedin.com/in/nived-velayudhan/"


def test_every_optional_field_may_be_missing_without_raising():
    page = FakePage(
        {1: [list_row(ATTENDEE_SURFACE, name=None, headline=None, distance=None)]}
    )

    people = run(extract_people_list(page, ATTENDEE_SURFACE))

    assert len(people) == 1
    assert people[0].headline is None
    assert people[0].member_distance is None
    assert people[0].full_name == "Nived Velayudhan", "the slug supplies a label"


def test_a_row_with_no_profile_link_is_dropped_rather_than_stored_nameless():
    page = FakePage({1: [list_row(ATTENDEE_SURFACE, slug=None)]})

    assert run(extract_people_list(page, ATTENDEE_SURFACE)) == []


def test_one_unreadable_row_does_not_cost_the_rest_of_the_slice():
    page = FakePage(
        {
            1: [
                list_row(ATTENDEE_SURFACE, slug="good-one", explode=True),
                list_row(ATTENDEE_SURFACE, slug="good-two", name="Good Two"),
            ]
        }
    )

    people = run(extract_people_list(page, ATTENDEE_SURFACE))

    assert [person.public_id for person in people] == ["good-two"]


@pytest.mark.parametrize(
    "surface",
    [ATTENDEE_SURFACE, COMPANY_EMPLOYEE_SURFACE, CONNECTION_SURFACE, FOLLOWER_SURFACE],
    ids=lambda surface: surface.source,
)
def test_markup_that_only_a_later_fallback_matches_still_resolves(surface):
    """The leading selectors are hypotheses. The chain is what makes that safe."""
    page = FakePage({1: [list_row(surface, slug="old-markup", fallback=1)]})

    people = run(extract_people_list(page, surface))

    assert [person.public_id for person in people] == ["old-markup"]


@pytest.mark.parametrize(
    "surface",
    [ATTENDEE_SURFACE, COMPANY_EMPLOYEE_SURFACE, CONNECTION_SURFACE, FOLLOWER_SURFACE],
    ids=lambda surface: surface.source,
)
def test_every_selector_a_surface_names_has_a_spare(surface):
    named = [
        surface.item,
        surface.link,
        surface.name,
        surface.headline,
        surface.location,
        surface.distance,
        surface.avatar,
        surface.load_more,
        surface.opener,
    ]
    for name in [entry for entry in named if entry]:
        assert len(selector_fallbacks(name)) >= 2, f"{name} has no fallback"


# --- Event attendees ------------------------------------------------------


def test_an_event_attendee_run_stores_every_attendee_as_a_lead(conn, account):
    page = PeopleListPage(rows_for(ATTENDEE_SURFACE, 9, prefix="attendee"), ATTENDEE_SURFACE)

    summary = run(
        run_event_attendee_harvest(
            page, conn, account, "1234567890", limit=9, **harvest_kwargs()
        )
    )

    assert isinstance(summary, ScrapeSummary)
    assert summary.source == SOURCE_EVENT_ATTENDEES
    assert summary.action_type == EVENT_ATTENDEES_ACTION
    assert summary.stop_reason is StopReason.COUNT_REACHED
    assert summary.results_new == 9
    assert summary.leads_created == 9
    assert count_leads(conn, account) == 9
    assert page.goto_urls == ["https://www.linkedin.com/events/1234567890/attendees/"]


def test_an_attendee_run_records_the_event_on_its_harvest_run_row(conn, account):
    page = PeopleListPage(rows_for(ATTENDEE_SURFACE, 3, prefix="attendee"), ATTENDEE_SURFACE)

    summary = run(
        run_event_attendee_harvest(
            page, conn, account, "https://www.linkedin.com/events/777/", limit=3,
            **harvest_kwargs(),
        )
    )

    stored = harvest_run(conn, summary.harvest_run_id)
    assert stored["source_type"] == SOURCE_EVENT_ATTENDEES
    assert stored["params"]["filters"]["event_id"] == "777"
    assert stored["params"]["filters"]["tab"] == "attendees"
    assert stored["new_count"] == 3
    assert stored["finished_at"] is not None


def test_an_attendee_list_pages_by_clicking_load_more(conn, account):
    page = PeopleListPage(
        rows_for(ATTENDEE_SURFACE, 9, prefix="attendee"), ATTENDEE_SURFACE, step=3
    )

    summary = run(
        run_event_attendee_harvest(
            page, conn, account, "1234567890", limit=50, **harvest_kwargs()
        )
    )

    assert summary.results_new == 9
    assert summary.stop_reason is StopReason.NO_NEW_RESULTS
    assert page.button.clicks >= 2


def test_a_modal_style_list_is_paged_by_pulling_the_last_row_into_view(conn, account):
    page = PeopleListPage(
        rows_for(ATTENDEE_SURFACE, 9, prefix="attendee"),
        ATTENDEE_SURFACE,
        step=3,
        reveal_on="view",
    )

    summary = run(
        run_event_attendee_harvest(
            page, conn, account, "1234567890", limit=50, **harvest_kwargs()
        )
    )

    assert summary.results_new == 9
    assert any(row.reveals for row in page.rows), (
        "a window scroll does not move a modal, so the last row has to be asked"
    )


# --- Company, connections, followers --------------------------------------


def test_a_company_employee_run_walks_the_people_tab(conn, account):
    page = PeopleListPage(
        rows_for(COMPANY_EMPLOYEE_SURFACE, 6, prefix="employee"),
        COMPANY_EMPLOYEE_SURFACE,
    )

    summary = run(
        run_company_employee_harvest(
            page, conn, account, "microsoft", limit=6, **harvest_kwargs()
        )
    )

    assert summary.source == SOURCE_COMPANY_EMPLOYEES
    assert summary.leads_created == 6
    assert page.goto_urls == ["https://www.linkedin.com/company/microsoft/people/"]


def test_a_connection_run_walks_your_own_network(conn, account):
    page = PeopleListPage(
        rows_for(CONNECTION_SURFACE, 4, prefix="connection"), CONNECTION_SURFACE
    )

    summary = run(
        run_connection_harvest(page, conn, account, limit=4, **harvest_kwargs())
    )

    assert summary.source == SOURCE_CONNECTIONS
    assert summary.leads_created == 4
    assert page.goto_urls == [CONNECTIONS_URL]


def test_a_follower_run_walks_the_follower_list(conn, account):
    page = PeopleListPage(
        rows_for(FOLLOWER_SURFACE, 4, prefix="follower"), FOLLOWER_SURFACE
    )

    summary = run(run_follower_harvest(page, conn, account, limit=4, **harvest_kwargs()))

    assert summary.source == SOURCE_FOLLOWERS
    assert summary.leads_created == 4
    assert page.goto_urls == [FOLLOWERS_URL]


# --- Safety, blacklist and resumption -------------------------------------


def test_every_people_list_spends_a_budget_the_config_has_heard_of():
    assert PEOPLE_LIST_ACTION in METERED_ACTIONS
    for surface in (
        ATTENDEE_SURFACE,
        COMPANY_EMPLOYEE_SURFACE,
        CONNECTION_SURFACE,
        FOLLOWER_SURFACE,
    ):
        assert surface.action_type in METERED_ACTIONS, surface.source


def test_the_gate_is_asked_before_every_slice_not_once_at_the_start(conn, account):
    page = PeopleListPage(rows_for(ATTENDEE_SURFACE, 9, prefix="attendee"), ATTENDEE_SURFACE)
    gate = FakeGate()

    run(
        run_event_attendee_harvest(
            page, conn, account, "1234567890", limit=50, **harvest_kwargs(guard=gate)
        )
    )

    assert len(gate.calls) >= 3
    assert {call["action_type"] for call in gate.calls} == {PEOPLE_LIST_ACTION}
    assert {call["account_id"] for call in gate.calls} == {account}


def test_a_refused_gate_stops_the_run_and_says_so(conn, account):
    page = PeopleListPage(rows_for(ATTENDEE_SURFACE, 9, prefix="attendee"), ATTENDEE_SURFACE)

    summary = run(
        run_event_attendee_harvest(
            page, conn, account, "1234567890", limit=50,
            **harvest_kwargs(guard=FakeGate(allow=1)),
        )
    )

    assert summary.refused
    assert summary.stop_reason is StopReason.GATE_REFUSED
    assert summary.gate_refusal["reason"] == "daily_cap_reached"
    assert summary.leads_created == 3, "what it got before the refusal is kept"


def test_a_blacklisted_attendee_is_refused_rather_than_resurrected(conn, account):
    blacklist_identity(conn, account, public_id="attendee-1", reason="asked to stop")
    page = PeopleListPage(rows_for(ATTENDEE_SURFACE, 3, prefix="attendee"), ATTENDEE_SURFACE)

    summary = run(
        run_event_attendee_harvest(
            page, conn, account, "1234567890", limit=3, **harvest_kwargs()
        )
    )

    assert summary.leads_created == 2
    assert get_lead_by_public_id(conn, account, "attendee-1") is None
    reasons = {refusal.reason for refusal in summary.harvest.refusals}
    assert reasons == {"blacklisted"}


def test_a_run_resumes_from_the_cursor_it_stopped_on(conn, account):
    rows = rows_for(ATTENDEE_SURFACE, 9, prefix="attendee")
    first = run(
        run_event_attendee_harvest(
            PeopleListPage(rows, ATTENDEE_SURFACE), conn, account, "1234567890",
            limit=3, **harvest_kwargs(),
        )
    )

    assert first.stop_reason is StopReason.COUNT_REACHED
    assert first.cursor.collected == 3

    second = run(
        run_event_attendee_harvest(
            PeopleListPage(rows, ATTENDEE_SURFACE), conn, account, "1234567890",
            limit=3, cursor=first.cursor, **harvest_kwargs(),
        )
    )

    assert second.results_new == 3
    assert second.cursor.collected == 6
    assert count_leads(conn, account) == 6, "a resumed run adds, it does not repeat"


def test_a_dry_run_reads_without_writing(conn, account):
    page = PeopleListPage(rows_for(ATTENDEE_SURFACE, 6, prefix="attendee"), ATTENDEE_SURFACE)

    summary = run(
        run_event_attendee_harvest(
            page, conn, account, "1234567890", limit=6, harvest=False,
            **harvest_kwargs(),
        )
    )

    assert summary.results_new == 6
    assert summary.leads_created == 0
    assert count_leads(conn, account) == 0
    assert summary.harvest_run_id is None


def test_the_same_person_on_two_sources_is_one_lead(conn, account):
    """A person found at an event and again on a company page is one row."""
    shared = "shared-person"
    event_page = PeopleListPage(
        [
            list_row(ATTENDEE_SURFACE, slug=shared, name="Shared Person"),
            list_row(ATTENDEE_SURFACE, slug="attendee-only", name="Attendee Only"),
        ],
        ATTENDEE_SURFACE,
    )
    company_page = PeopleListPage(
        [
            list_row(COMPANY_EMPLOYEE_SURFACE, slug=shared, name="Shared Person"),
            list_row(COMPANY_EMPLOYEE_SURFACE, slug="employee-only", name="Employee Only"),
        ],
        COMPANY_EMPLOYEE_SURFACE,
    )

    first = run(
        run_event_attendee_harvest(
            event_page, conn, account, "1234567890", limit=10, **harvest_kwargs()
        )
    )
    second = run(
        run_company_employee_harvest(
            company_page, conn, account, "microsoft", limit=10, **harvest_kwargs()
        )
    )

    assert first.leads_created == 2
    assert second.leads_created == 1, "the shared person resolved onto the event lead"
    assert count_leads(conn, account) == 3


# --- Summary folding ------------------------------------------------------


def _summary(**overrides) -> ScrapeSummary:
    base = {
        "source": "a",
        "action_type": PEOPLE_LIST_ACTION,
        "stop_reason": StopReason.NO_NEW_RESULTS,
        "cursor": SearchCursor(),
        "results_seen": 1,
        "results_new": 1,
        "pages_fetched": 1,
    }
    base.update(overrides)
    return ScrapeSummary(**base)


def test_folding_one_phase_only_renames_the_source():
    folded = combine_summaries("combined", _summary(results_new=4))

    assert folded.source == "combined"
    assert folded.results_new == 4


def test_folding_two_phases_adds_the_counts_and_keeps_the_later_cursor():
    first = _summary(results_new=4, results_seen=5, pages_fetched=2)
    second = _summary(
        results_new=3,
        results_seen=3,
        pages_fetched=1,
        cursor=SearchCursor(page=2, collected=7),
        stop_reason=StopReason.COUNT_REACHED,
    )

    folded = combine_summaries("combined", first, second)

    assert folded.results_new == 7
    assert folded.results_seen == 8
    assert folded.pages_fetched == 3
    assert folded.cursor.collected == 7
    assert folded.stop_reason is StopReason.COUNT_REACHED


def test_a_refusal_in_either_phase_makes_the_whole_run_refused():
    refused = _summary(
        stop_reason=StopReason.GATE_REFUSED, gate_refusal={"reason": "daily_cap_reached"}
    )

    folded = combine_summaries("combined", refused, _summary())

    assert folded.refused
    assert folded.gate_refusal == {"reason": "daily_cap_reached"}


def test_the_engine_never_writes_leads_with_raw_sql(conn, account):
    """Every source goes through the DB-02 store, so the table stays consistent."""
    page = PeopleListPage(rows_for(CONNECTION_SURFACE, 3, prefix="c"), CONNECTION_SURFACE)

    run(run_connection_harvest(page, conn, account, limit=3, **harvest_kwargs()))

    rows = conn.execute(
        "SELECT full_name, first_name, last_name, public_id FROM leads ORDER BY id"
    ).fetchall()
    assert [row["public_id"] for row in rows] == ["c-0", "c-1", "c-2"]
    assert all(row["first_name"] == "Person" for row in rows), (
        "upsert_lead split the name, which raw SQL would not have"
    )
    assert isinstance(conn, sqlite3.Connection)
