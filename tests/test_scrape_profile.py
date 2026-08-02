"""SCRAPE-03 profile deep-scraper: extraction, cache windows and persistence.

Nothing here touches a browser or a LinkedIn session. The page is a duck-typed
fake shaped like the ones in `test_scrape_fakes`, so selector matching is exact
string membership: a test has to name the selector it expects the code to try,
and a registry change shows up as a failing test rather than as a fake that
quietly matches anything.
"""

import asyncio
import ast
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from test_scrape_fakes import FakeElement, FakeGate, FakePage, FakeRecorder, RecordingSleep, node

from linkedin_mcp.browser.humanize import FAST, Humanizer
from linkedin_mcp.browser.navigate import NavigationResult, SessionExpiredError
from linkedin_mcp.browser.selectors import SELECTORS, selector_fallbacks
from linkedin_mcp.core.config import HARD_CEILINGS, profile_view_action
from linkedin_mcp.core.db import initialize_database
from linkedin_mcp.leads import (
    CONTACT_INFO_CACHE_DAYS,
    POSITIONS_CACHE_DAYS,
    LeadSection,
    blacklist_identity,
    blacklist_lead,
    create_lead,
    get_custom_fields,
    get_lead,
    mark_section_fetched,
    needs_refresh,
    section_fetched_at,
    upsert_lead,
)
from linkedin_mcp.scrape import (
    ContactEntry,
    EducationEntry,
    ExperienceEntry,
    ProfileDetail,
    ProfileScrapeStatus,
    SectionOutcome,
    SkillEntry,
    contact_kind,
    extract_contact_info,
    extract_education,
    extract_experience,
    extract_profile,
    extract_skills,
    parse_date_range,
    parse_mutual_connections,
    parse_year_range,
    profile_scrape_action,
    run_profile_scrape,
    run_profile_scrapes,
    stale_profile_sections,
    store_profile_detail,
)
from linkedin_mcp.scrape.profile import PROFILE_DETAIL_SOURCE
from linkedin_mcp.scrape.profile_extract import (
    NO_CONTACT_LINK,
    NO_CONTACT_MODAL,
    NOT_FIRST_DEGREE,
    UNKNOWN_DEGREE,
)
from linkedin_mcp.scrape.profile_records import MutualConnections
from linkedin_mcp.scrape.records import PersonResult

REPO_ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc)
PROFILE_URL = "https://www.linkedin.com/in/nived-velayudhan/"


def run(coroutine):
    return asyncio.run(coroutine)


def pacer():
    return Humanizer(FAST, seed=11, sleep=RecordingSleep())


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


# --- Fakes ---------------------------------------------------------------


def section(name: str, rows: list[FakeElement]) -> FakeElement:
    """Build one profile section container holding its own rows."""
    return FakeElement(selectors=(selector_fallbacks(name)[0],), children=rows)


def experience_row(
    *,
    title: str | None = "Solution Engineer",
    company: str | None = "Microsoft",
    company_url: str | None = "https://www.linkedin.com/company/microsoft/",
    dates: str | None = "Jan 2021 - Present \u00b7 5 yrs 7 mos",
    location: str | None = "Bengaluru, Karnataka, India",
) -> FakeElement:
    children: list[FakeElement] = []
    if title is not None:
        children.append(node("profile_experience_entry_title", text=title))
    if company is not None:
        children.append(node("profile_experience_entry_company", text=company))
    if company_url is not None:
        children.append(node("profile_experience_entry_company_link", href=company_url))
    if dates is not None:
        children.append(node("profile_experience_entry_dates", text=dates))
    if location is not None:
        children.append(node("profile_experience_entry_location", text=location))
    return FakeElement(
        selectors=(selector_fallbacks("profile_experience_entry")[0],), children=children
    )


def education_row(
    *,
    school: str | None = "NIT Calicut",
    degree: str | None = "Bachelor of Technology, Computer Science",
    dates: str | None = "2012 - 2016",
) -> FakeElement:
    children: list[FakeElement] = []
    if school is not None:
        children.append(node("profile_education_entry_school", text=school))
    if degree is not None:
        children.append(node("profile_education_entry_degree", text=degree))
    if dates is not None:
        children.append(node("profile_education_entry_dates", text=dates))
    return FakeElement(
        selectors=(selector_fallbacks("profile_education_entry")[0],), children=children
    )


def skill_row(name: str = "GitHub Copilot", endorsements: str | None = "42 endorsements") -> FakeElement:
    children = [node("profile_skills_entry_name", text=name)]
    if endorsements is not None:
        children.append(node("profile_skills_entry_endorsements", text=endorsements))
    return FakeElement(
        selectors=(selector_fallbacks("profile_skills_entry")[0],), children=children
    )


def contact_section(header: str, values: list[str]) -> FakeElement:
    children = [node("profile_contact_info_section_header", text=header)]
    children.extend(
        node("profile_contact_info_section_value", text=value) for value in values
    )
    return FakeElement(
        selectors=(selector_fallbacks("profile_contact_info_section")[0],),
        children=children,
    )


def top_card(
    *,
    name: str | None = "Nived Velayudhan",
    headline: str | None = "Solution Engineer at Microsoft",
    location: str | None = "Bengaluru, Karnataka, India",
    distance: str | None = "2nd",
    avatar: str | None = "https://media.licdn.com/avatar.jpg",
    stats: tuple[str, ...] = ("500+ connections", "12,340 followers"),
    mutual: str | None = None,
    mutual_url: str | None = "https://www.linkedin.com/search/results/people/?facetConnectionOf=x",
    member_urn: str | None = "urn:li:member:918273",
    badges: tuple[str, ...] = (),
) -> FakeElement:
    children: list[FakeElement] = []
    if name is not None:
        children.append(node("profile_detail_name", text=name))
    if headline is not None:
        children.append(node("profile_detail_headline", text=headline))
    if location is not None:
        children.append(node("profile_detail_location", text=location))
    if distance is not None:
        children.append(node("profile_detail_distance", text=distance))
    if avatar is not None:
        children.append(node("profile_detail_avatar", src=avatar))
    for stat in stats:
        children.append(node("profile_detail_network_stats", text=stat))
    if mutual is not None:
        children.append(
            node("profile_detail_mutual_connections", text=mutual, href=mutual_url or "")
        )
    for badge in badges:
        children.append(node(f"profile_detail_{badge}_badge"))

    attrs = {}
    if member_urn is not None:
        attrs["data-urn"] = member_urn
    return FakeElement(
        selectors=(
            selector_fallbacks("profile_detail_top_card")[0],
            selector_fallbacks("profile_detail_member_urn")[0],
        ),
        attrs=attrs,
        children=children,
    )


class FakeProfilePage(FakePage):
    """A profile page whose contact overlay only exists once it is clicked."""

    def __init__(
        self,
        elements: list[FakeElement],
        *,
        contact_sections: list[FakeElement] | None = None,
        contact_link: bool = True,
        contact_modal: bool = True,
        url: str = PROFILE_URL,
    ) -> None:
        super().__init__({1: list(elements)}, url=url)
        self.contact_sections = list(contact_sections or [])
        self.contact_modal = contact_modal
        self.modal_open = False
        self.modal_closed = False
        self.trigger_clicks = 0

        if contact_link:
            trigger = FakeElement(
                selectors=(selector_fallbacks("profile_contact_info_trigger")[0],),
                on_click=self._open_modal,
            )
            self.pages[1].append(trigger)

    def _open_modal(self) -> None:
        self.trigger_clicks += 1
        self.modal_open = True

    def _close_modal(self) -> None:
        self.modal_open = False
        self.modal_closed = True

    @property
    def visible(self) -> list[FakeElement]:
        shown = list(super().visible)
        if self.modal_open:
            if self.contact_modal:
                shown.append(
                    FakeElement(
                        selectors=(selector_fallbacks("profile_contact_info_modal")[0],),
                        children=list(self.contact_sections),
                    )
                )
            shown.append(
                FakeElement(
                    selectors=(selector_fallbacks("profile_contact_info_close")[0],),
                    on_click=self._close_modal,
                )
            )
        return shown


def profile_page(**kwargs) -> FakeProfilePage:
    """A complete profile: top card, three sections and a contact overlay."""
    card = kwargs.pop("card", None) or top_card()
    contact_sections = kwargs.pop(
        "contact_sections",
        [
            contact_section("Email", ["nived@example.com"]),
            contact_section("Phone", ["+91 99999 99999"]),
            contact_section("Birthday", ["March 14"]),
        ],
    )
    experience = kwargs.pop("experience", [experience_row()])
    education = kwargs.pop("education", [education_row()])
    skills = kwargs.pop("skills", [skill_row(), skill_row("Platform Engineering", None)])

    elements = [card]
    elements.append(node("profile_detail_about", text="I ship developer tooling."))
    if experience is not None:
        elements.append(section("profile_experience_section", experience))
    if education is not None:
        elements.append(section("profile_education_section", education))
    if skills is not None:
        elements.append(section("profile_skills_section", skills))
    return FakeProfilePage(elements, contact_sections=contact_sections, **kwargs)


class FakeNavigator:
    """Stands in for `goto_profile`, recording exactly how it was asked to go."""

    def __init__(self, *, error: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self.error = error

    async def __call__(self, page, profile_url, **kwargs):
        self.calls.append({"profile_url": profile_url, **kwargs})
        if self.error is not None:
            raise self.error
        slug = profile_url.rstrip("/").rsplit("/", 1)[-1]
        page.url = profile_url
        return NavigationResult(
            url=profile_url, method="search_bar", slug=slug, query=slug
        )


# --- Text parsers --------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Jan 2021 - Present \u00b7 5 yrs 7 mos", ("2021-01", None, True)),
        ("Mar 2015 - Dec 2019", ("2015-03", "2019-12", False)),
        ("2018 - 2020", ("2018", "2020", False)),
        ("Sept 2019 to Present", ("2019-09", None, True)),
        ("Present", (None, None, True)),
        ("Full-time", (None, None, False)),
        ("Bengaluru, Karnataka, India", (None, None, False)),
        ("Remote", (None, None, False)),
        ("", (None, None, False)),
        (None, (None, None, False)),
    ],
)
def test_position_date_ranges_are_split_into_a_start_an_end_and_a_flag(text, expected):
    assert parse_date_range(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2012 - 2016", (2012, 2016)),
        ("2012", (2012, None)),
        ("no years here", (None, None)),
        (None, (None, None)),
    ],
)
def test_education_date_lines_become_years(text, expected):
    assert parse_year_range(text) == expected


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("Email", "email"),
        ("Work email", "work_email"),
        ("Personal email", "personal_email"),
        ("Phone", "phone"),
        ("Mobile", "phone"),
        ("Websites", "website"),
        ("Twitter", "twitter"),
        ("Birthday", None),
        ("Address", None),
        ("", None),
        (None, None),
    ],
)
def test_contact_headers_map_only_onto_kinds_the_table_accepts(header, expected):
    assert contact_kind(header) == expected


@pytest.mark.parametrize(
    ("text", "count", "names"),
    [
        ("Karthik Rao and 12 other mutual connections", 13, ("Karthik Rao",)),
        (
            "Karthik Rao, Ada Lovelace and 12 other mutual connections",
            14,
            ("Karthik Rao", "Ada Lovelace"),
        ),
        ("48 mutual connections", 48, ()),
        ("Ada Lovelace is a mutual connection", 1, ("Ada Lovelace",)),
        ("Follows you", None, ()),
        (None, None, ()),
    ],
)
def test_mutual_connection_blurbs_are_read_into_a_count_and_names(text, count, names):
    mutual = parse_mutual_connections(text)

    assert mutual.count == count
    assert mutual.names == names


# --- Extraction ----------------------------------------------------------


def test_a_complete_profile_yields_every_field():
    page = profile_page(
        card=top_card(distance="1st", badges=("premium", "influencer", "openlink"))
    )

    detail = run(extract_profile(page, humanizer=pacer()))

    assert detail.person.full_name == "Nived Velayudhan"
    assert detail.person.public_id == "nived-velayudhan"
    assert detail.person.member_id == "urn:li:member:918273"
    assert detail.person.headline == "Solution Engineer at Microsoft"
    assert detail.person.location_name == "Bengaluru, Karnataka, India"
    assert detail.person.member_distance == "1st"
    assert detail.person.avatar_url == "https://media.licdn.com/avatar.jpg"
    assert detail.person.summary == "I ship developer tooling."
    assert detail.person.profile_url == PROFILE_URL
    assert detail.connection_count == 500
    assert detail.follower_count == 12340
    assert detail.badges == {"premium": True, "influencer": True, "open_link": True}


def test_every_badge_the_issue_names_is_captured():
    page = profile_page(
        card=top_card(
            distance="1st", badges=("premium", "influencer", "openlink", "jobseeker", "hiring")
        )
    )

    detail = run(extract_profile(page, humanizer=pacer()))

    assert detail.badges == {
        "premium": True,
        "influencer": True,
        "open_link": True,
        "job_seeker": True,
        "hiring": True,
    }


def test_an_absent_badge_is_left_unrecorded_rather_than_written_as_false():
    """A rotted selector must not strip a badge an earlier sighting saw."""
    detail = run(extract_profile(profile_page(), humanizer=pacer()))

    assert detail.badges == {}


def test_the_professional_history_lands_in_order_with_dates_split():
    page = profile_page(
        experience=[
            experience_row(),
            experience_row(
                title="Consultant",
                company="Accenture",
                company_url="https://www.linkedin.com/company/accenture/",
                dates="Jul 2016 - Dec 2020",
                location="Chennai, India",
            ),
        ]
    )

    detail = run(extract_profile(page, humanizer=pacer()))

    assert [entry.title for entry in detail.experience] == [
        "Solution Engineer",
        "Consultant",
    ]
    assert detail.experience[0].company_id == "microsoft"
    assert detail.experience[0].start_date == "2021-01"
    assert detail.experience[0].end_date is None
    assert detail.experience[0].is_current is True
    assert detail.experience[1].end_date == "2020-12"
    assert detail.experience[1].is_current is False
    assert detail.current_position().company == "Microsoft"


def test_education_splits_the_degree_from_the_field_of_study():
    detail = run(extract_profile(profile_page(), humanizer=pacer()))

    assert len(detail.education) == 1
    assert detail.education[0].school == "NIT Calicut"
    assert detail.education[0].degree == "Bachelor of Technology"
    assert detail.education[0].field_of_study == "Computer Science"
    assert detail.education[0].start_year == 2012
    assert detail.education[0].end_year == 2016


def test_skills_carry_their_endorsement_counts_and_survive_a_missing_one():
    detail = run(extract_profile(profile_page(), humanizer=pacer()))

    assert [skill.skill for skill in detail.skills] == [
        "GitHub Copilot",
        "Platform Engineering",
    ]
    assert detail.skills[0].endorsement_count == 42
    assert detail.skills[1].endorsement_count is None


def test_mutual_connections_are_read_off_the_top_card():
    page = profile_page(
        card=top_card(mutual="Karthik Rao and 12 other mutual connections")
    )

    detail = run(extract_profile(page, humanizer=pacer()))

    assert detail.mutual_connections.count == 13
    assert detail.mutual_connections.names == ("Karthik Rao",)
    assert "facetConnectionOf" in (detail.mutual_connections.url or "")


def test_the_member_own_counts_are_not_confused_with_mutual_connections():
    page = profile_page(
        card=top_card(
            stats=("500+ connections", "12,340 followers", "6 mutual connections")
        )
    )

    detail = run(extract_profile(page, humanizer=pacer()))

    assert detail.connection_count == 500
    assert detail.follower_count == 12340


def test_a_profile_with_no_sections_at_all_is_still_a_lead():
    page = FakeProfilePage([top_card()], contact_link=False)

    detail = run(extract_profile(page, humanizer=pacer()))

    assert detail.person.public_id == "nived-velayudhan"
    assert detail.experience == ()
    assert detail.experience_section_seen is False
    assert detail.education == ()
    assert detail.skills == ()
    assert detail.person.summary is None


def test_a_missing_top_card_degrades_to_reading_the_page():
    """The container is a hypothesis. Losing it must not lose the name."""
    page = FakeProfilePage(
        [node("profile_detail_name", text="Ada Lovelace")], contact_link=False
    )

    detail = run(extract_profile(page, humanizer=pacer()))

    assert detail.person.full_name == "Ada Lovelace"
    assert detail.connection_count is None


def test_one_unreadable_experience_row_does_not_take_the_section_down():
    broken = FakeElement(
        selectors=(selector_fallbacks("profile_experience_entry")[0],), explode=True
    )
    page = profile_page(experience=[broken, experience_row(title="Survivor")])

    entries, outcome = run(extract_experience(page))

    assert outcome is SectionOutcome.READ
    assert [entry.title for entry in entries] == ["Survivor"]


def test_sections_are_read_inside_their_own_container_and_never_mixed():
    """Experience and education render as the same list rows on a real profile."""
    page = profile_page(
        experience=[experience_row(title="Only Job")],
        education=[education_row(school="Only School")],
    )

    experience, _ = run(extract_experience(page))
    education = run(extract_education(page))
    skills = run(extract_skills(page))

    assert [entry.title for entry in experience] == ["Only Job"]
    assert [entry.school for entry in education] == ["Only School"]
    assert [entry.skill for entry in skills] == ["GitHub Copilot", "Platform Engineering"]


def test_a_name_that_did_not_render_falls_back_to_the_slug():
    page = FakeProfilePage([top_card(name=None, member_urn=None)], contact_link=False)

    detail = run(extract_profile(page, humanizer=pacer()))

    assert detail.person.full_name == "Nived Velayudhan"


def test_a_numeric_member_id_attribute_becomes_a_member_urn():
    card = FakeElement(
        selectors=(selector_fallbacks("profile_detail_member_urn")[0],),
        attrs={"data-member-id": "918273"},
        children=[node("profile_detail_name", text="Nived Velayudhan")],
    )
    page = FakeProfilePage([card], contact_link=False)

    detail = run(extract_profile(page, humanizer=pacer()))

    assert detail.person.member_id == "urn:li:member:918273"


# --- Contact info, first degree only -------------------------------------


def test_contact_info_is_read_for_a_first_degree_connection():
    page = profile_page(card=top_card(distance="1st"))

    detail = run(extract_profile(page, humanizer=pacer()))

    assert detail.contact_info_attempted is True
    assert detail.contact_info_skipped_reason is None
    assert page.trigger_clicks == 1
    assert [(entry.kind, entry.value) for entry in detail.contacts] == [
        ("email", "nived@example.com"),
        ("phone", "+91 99999 99999"),
    ]


def test_the_overlay_is_dismissed_after_it_is_read():
    page = profile_page(card=top_card(distance="1st"))

    run(extract_profile(page, humanizer=pacer()))

    assert page.modal_closed is True
    assert page.modal_open is False


@pytest.mark.parametrize("distance", ["2nd", "3rd+"])
def test_contact_info_is_skipped_cleanly_for_anyone_but_a_first_degree(distance, caplog):
    page = profile_page(card=top_card(distance=distance))

    with caplog.at_level("ERROR"):
        detail = run(extract_profile(page, humanizer=pacer()))

    assert detail.contacts == ()
    assert detail.contact_info_attempted is False
    assert detail.contact_info_skipped_reason == NOT_FIRST_DEGREE
    assert page.trigger_clicks == 0
    assert page.modal_open is False
    assert caplog.records == []


def test_an_unreadable_degree_skips_contact_info_rather_than_guessing():
    page = profile_page(card=top_card(distance=None))

    detail = run(extract_profile(page, humanizer=pacer()))

    assert detail.contacts == ()
    assert detail.contact_info_attempted is False
    assert detail.contact_info_skipped_reason == UNKNOWN_DEGREE
    assert page.trigger_clicks == 0


def test_a_first_degree_profile_with_no_contact_link_says_so():
    page = profile_page(card=top_card(distance="1st"), contact_link=False)

    detail = run(extract_profile(page, humanizer=pacer()))

    assert detail.contacts == ()
    assert detail.contact_info_attempted is False
    assert detail.contact_info_skipped_reason == NO_CONTACT_LINK


def test_contact_kinds_the_table_would_reject_are_dropped_not_coerced():
    page = profile_page(
        card=top_card(distance="1st"),
        contact_sections=[
            contact_section("Birthday", ["March 14"]),
            contact_section("Address", ["Somewhere"]),
            contact_section("Work email", ["nived@work.example"]),
        ],
    )

    detail = run(extract_profile(page, humanizer=pacer()))

    assert [(entry.kind, entry.value) for entry in detail.contacts] == [
        ("work_email", "nived@work.example")
    ]


def test_reading_the_page_without_the_overlay_never_clicks():
    page = profile_page(card=top_card(distance="1st"))

    detail = run(extract_profile(page, humanizer=pacer(), read_contact_info=False))

    assert page.trigger_clicks == 0
    assert detail.contacts == ()
    assert detail.contact_info_attempted is False


def test_the_contact_extractor_refuses_a_non_first_degree_without_a_page():
    contacts, outcome, reason = run(
        extract_contact_info(object(), member_distance="2nd", humanizer=pacer())
    )

    assert (contacts, outcome, reason) == ((), SectionOutcome.SKIPPED, NOT_FIRST_DEGREE)


def test_an_overlay_that_opens_without_a_body_is_never_marked_fresh():
    """A click is not a read. Stamping on one would hide a moved selector."""
    page = profile_page(card=top_card(distance="1st"), contact_modal=False)

    detail = run(extract_profile(page, humanizer=pacer()))

    assert page.trigger_clicks == 1
    assert detail.contacts == ()
    assert detail.contact_info_outcome is SectionOutcome.MISSING
    assert detail.contact_info_attempted is False
    assert detail.contact_info_skipped_reason == NO_CONTACT_MODAL


def test_an_overlay_holding_nothing_storable_is_read_and_marked_fresh():
    page = profile_page(
        card=top_card(distance="1st"),
        contact_sections=[contact_section("Birthday", ["March 14"])],
    )

    detail = run(extract_profile(page, humanizer=pacer()))

    assert detail.contacts == ()
    assert detail.contact_info_outcome is SectionOutcome.EMPTY
    assert detail.contact_info_attempted is True


def test_a_first_degree_profile_with_no_contact_link_is_not_marked_fresh():
    page = profile_page(card=top_card(distance="1st"), contact_link=False)

    detail = run(extract_profile(page, humanizer=pacer()))

    assert detail.contact_info_outcome is SectionOutcome.MISSING
    assert detail.contact_info_attempted is False


def test_a_missing_experience_section_is_not_marked_fresh():
    page = profile_page(experience=None)

    detail = run(extract_profile(page, humanizer=pacer()))

    assert detail.experience_outcome is SectionOutcome.MISSING
    assert detail.experience_section_seen is False


def test_a_section_whose_rows_all_fail_to_read_is_not_marked_fresh():
    """Rows that render but do not read is a moved selector, not an empty career."""
    broken = FakeElement(
        selectors=(selector_fallbacks("profile_experience_entry")[0],), explode=True
    )
    page = profile_page(experience=[broken])

    entries, outcome = run(extract_experience(page))

    assert entries == ()
    assert outcome is SectionOutcome.MISSING


def test_an_empty_experience_section_is_read_and_marked_fresh():
    page = profile_page(experience=[])

    detail = run(extract_profile(page, humanizer=pacer()))

    assert detail.experience == ()
    assert detail.experience_outcome is SectionOutcome.EMPTY
    assert detail.experience_section_seen is True


def test_grouped_roles_at_one_company_are_flattened_not_doubled():
    """LinkedIn nests several roles at one employer inside a company row."""
    roles = [
        experience_row(
            title="Principal SE",
            company=None,
            company_url=None,
            dates="Jan 2024 - Present",
            location=None,
        ),
        experience_row(
            title="Solution Engineer",
            company=None,
            company_url=None,
            dates="Jan 2021 - Dec 2023",
            location=None,
        ),
    ]
    group = FakeElement(
        selectors=(selector_fallbacks("profile_experience_entry")[0],),
        children=[
            node("profile_experience_entry_title", text="Microsoft"),
            node(
                "profile_experience_entry_company_link",
                href="https://www.linkedin.com/company/microsoft/",
            ),
            *roles,
        ],
    )
    page = profile_page(experience=[group, experience_row(title="Consultant", company="Accenture")])

    entries, outcome = run(extract_experience(page))

    assert outcome is SectionOutcome.READ
    assert [(entry.title, entry.company) for entry in entries] == [
        ("Principal SE", "Microsoft"),
        ("Solution Engineer", "Microsoft"),
        ("Consultant", "Accenture"),
    ]
    assert entries[0].company_id == "microsoft"
    assert entries[0].is_current is True


# --- Persistence ---------------------------------------------------------


def detail_for(
    *,
    public_id="nived-velayudhan",
    member_id="urn:li:member:918273",
    distance="1st",
    experience=(ExperienceEntry(title="Solution Engineer", company="Microsoft", is_current=True),),
    education=(EducationEntry(school="NIT Calicut", degree="BTech"),),
    skills=(SkillEntry(skill="GitHub Copilot", endorsement_count=42),),
    contacts=(ContactEntry(kind="email", value="nived@example.com"),),
    mutual=MutualConnections(count=13, names=("Karthik Rao",)),
    **kwargs,
) -> ProfileDetail:
    return ProfileDetail(
        person=PersonResult(
            full_name="Nived Velayudhan",
            public_id=public_id,
            member_id=member_id,
            member_distance=distance,
            headline="Solution Engineer at Microsoft",
            badges={"premium": True},
        ),
        connection_count=500,
        follower_count=12340,
        experience=tuple(experience),
        education=tuple(education),
        skills=tuple(skills),
        contacts=tuple(contacts),
        mutual_connections=mutual,
        **kwargs,
    )


def rows(conn, table, lead_id):
    return conn.execute(
        f"SELECT * FROM {table} WHERE lead_id = ? ORDER BY rowid", (lead_id,)
    ).fetchall()


def test_one_visit_fills_every_table_the_issue_names(conn, account):
    result = store_profile_detail(
        conn,
        account,
        detail_for(),
        sections_fetched=(LeadSection.CONTACT_INFO, LeadSection.POSITIONS),
        fetched_at=NOW,
        visited_at=NOW,
    )

    assert result.stored is True
    lead = get_lead(conn, result.lead_id)
    assert lead.full_name == "Nived Velayudhan"
    assert lead.member_id == "urn:li:member:918273"
    assert lead.member_distance == "1st"
    assert lead.connection_count == 500
    assert lead.follower_count == 12340
    assert lead.organization_name == "Microsoft"
    assert lead.organization_title == "Solution Engineer"
    assert lead.badges == {"premium": True}
    assert lead.last_visited_at is not None

    assert [row["title"] for row in rows(conn, "lead_experience", lead.id)] == [
        "Solution Engineer"
    ]
    assert [row["school"] for row in rows(conn, "lead_education", lead.id)] == [
        "NIT Calicut"
    ]
    assert [row["skill"] for row in rows(conn, "lead_skills", lead.id)] == [
        "GitHub Copilot"
    ]
    assert [row["value"] for row in rows(conn, "lead_contacts", lead.id)] == [
        "nived@example.com"
    ]
    assert get_custom_fields(conn, lead.id)["mutual_connections"] == "13"
    assert get_custom_fields(conn, lead.id)["mutual_connection_names"] == "Karthik Rao"


def test_the_cache_windows_are_stamped_by_the_visit(conn, account):
    result = store_profile_detail(
        conn,
        account,
        detail_for(),
        sections_fetched=(LeadSection.CONTACT_INFO, LeadSection.POSITIONS),
        fetched_at=NOW,
    )

    assert result.sections_marked == ("contact_info", "positions")
    assert section_fetched_at(conn, result.lead_id, LeadSection.POSITIONS) is not None
    assert not needs_refresh(conn, result.lead_id, LeadSection.POSITIONS, now=NOW)
    assert not needs_refresh(conn, result.lead_id, LeadSection.CONTACT_INFO, now=NOW)


def test_a_section_that_was_not_read_is_not_stamped(conn, account):
    result = store_profile_detail(
        conn,
        account,
        detail_for(distance="2nd", contacts=()),
        sections_fetched=(LeadSection.POSITIONS,),
        fetched_at=NOW,
    )

    assert result.sections_marked == ("positions",)
    assert section_fetched_at(conn, result.lead_id, LeadSection.CONTACT_INFO) is None


def test_a_second_visit_replaces_the_career_rather_than_doubling_it(conn, account):
    first = store_profile_detail(conn, account, detail_for(), fetched_at=NOW)
    second = store_profile_detail(
        conn,
        account,
        detail_for(
            experience=(
                ExperienceEntry(title="Principal SE", company="Microsoft", is_current=True),
                ExperienceEntry(title="Solution Engineer", company="Microsoft"),
            )
        ),
        fetched_at=NOW,
    )

    assert first.lead_id == second.lead_id
    stored = rows(conn, "lead_experience", second.lead_id)
    assert [row["title"] for row in stored] == ["Principal SE", "Solution Engineer"]
    assert [row["ord"] for row in stored] == [0, 1]


def test_an_empty_section_never_deletes_what_is_already_stored(conn, account):
    """An empty list is a selector that stopped matching, not a deleted career."""
    first = store_profile_detail(conn, account, detail_for(), fetched_at=NOW)

    store_profile_detail(
        conn,
        account,
        detail_for(experience=(), education=(), skills=()),
        fetched_at=NOW,
    )

    assert len(rows(conn, "lead_experience", first.lead_id)) == 1
    assert len(rows(conn, "lead_education", first.lead_id)) == 1
    assert len(rows(conn, "lead_skills", first.lead_id)) == 1


def test_contacts_are_added_without_deleting_ones_this_visit_did_not_see(conn, account):
    first = store_profile_detail(conn, account, detail_for(), fetched_at=NOW)
    conn.execute(
        "INSERT INTO lead_contacts (lead_id, kind, value, source) VALUES (?, ?, ?, ?)",
        (first.lead_id, "phone", "+91 88888 88888", "csv_import"),
    )
    conn.commit()

    store_profile_detail(
        conn,
        account,
        detail_for(contacts=(ContactEntry(kind="website", value="https://nived.dev"),)),
        fetched_at=NOW,
    )

    stored = {row["value"]: row["source"] for row in rows(conn, "lead_contacts", first.lead_id)}
    assert stored == {
        "nived@example.com": "profile_contact_info",
        "+91 88888 88888": "csv_import",
        "https://nived.dev": "profile_contact_info",
    }


def test_a_blacklisted_lead_is_refused_by_the_dedupe_layer_not_by_a_second_check(conn, account):
    lead = create_lead(
        conn,
        account,
        "Nived Velayudhan",
        member_id="urn:li:member:918273",
        public_id="nived-velayudhan",
    )
    blacklist_lead(conn, lead.id, reason="asked not to be contacted")

    result = store_profile_detail(conn, account, detail_for(), fetched_at=NOW)

    assert result.stored is False
    assert result.refused is True
    assert result.harvest.refusals[0].reason == "blacklisted"
    assert rows(conn, "lead_experience", lead.id) == []


def test_a_profile_with_no_identifiers_is_dropped_rather_than_stored_twice(conn, account):
    result = store_profile_detail(
        conn, account, detail_for(public_id=None, member_id=None), fetched_at=NOW
    )

    assert result.stored is False
    assert result.harvest.found == 0


def test_a_thin_visit_never_blanks_a_richer_stored_record(conn, account):
    upsert_lead(
        conn,
        account,
        full_name="Nived Velayudhan",
        member_id="urn:li:member:918273",
        public_id="nived-velayudhan",
        headline="Solution Engineer at Microsoft",
        location_name="Bengaluru, Karnataka, India",
    )

    thin = ProfileDetail(
        person=PersonResult(
            full_name="Nived Velayudhan",
            public_id="nived-velayudhan",
            member_id="urn:li:member:918273",
        )
    )
    result = store_profile_detail(conn, account, thin, fetched_at=NOW)

    lead = get_lead(conn, result.lead_id)
    assert lead.headline == "Solution Engineer at Microsoft"
    assert lead.location_name == "Bengaluru, Karnataka, India"


# --- The run: gate, navigation, cache windows ----------------------------


def scrape(page, conn, account, *, navigator=None, gate=None, recorder=None, **kwargs):
    navigator = navigator or FakeNavigator()
    return (
        run(
            run_profile_scrape(
                page,
                conn,
                account,
                kwargs.pop("profile_url", PROFILE_URL),
                humanizer=pacer(),
                guard=gate or FakeGate(),
                record=recorder or FakeRecorder(),
                clock=clock,
                navigate=navigator,
                **kwargs,
            )
        ),
        navigator,
    )


def test_a_visit_navigates_in_page_and_stores_everything(conn, account):
    page = profile_page(card=top_card(distance="1st"))
    recorder = FakeRecorder()

    result, navigator = scrape(page, conn, account, recorder=recorder)

    assert result.status is ProfileScrapeStatus.SCRAPED
    assert result.action_type == "profile_view"
    assert navigator.calls[0]["direct"] is False
    assert navigator.calls[0]["account_id"] == account
    assert result.navigation.method == "search_bar"
    assert result.sections_fetched == ("contact_info", "positions")
    assert result.lead_id is not None
    assert recorder.rows[0]["action_type"] == "profile_view"
    assert recorder.rows[0]["detail"]["source"] == PROFILE_DETAIL_SOURCE
    assert rows(conn, "lead_experience", result.lead_id)


def test_the_gate_is_asked_before_the_page_is_loaded(conn, account):
    page = profile_page()
    gate = FakeGate(allow=0)

    result, navigator = scrape(page, conn, account, gate=gate)

    assert result.status is ProfileScrapeStatus.REFUSED
    assert result.gate_refusal["reason"] == "daily_cap_reached"
    assert navigator.calls == []
    assert gate.calls[0]["action_type"] == "profile_view"


def test_a_direct_load_is_budgeted_against_the_forty_a_day_cap(conn, account):
    page = profile_page()
    gate = FakeGate()

    result, navigator = scrape(page, conn, account, gate=gate, direct=True)

    assert result.action_type == "profile_view_direct"
    assert gate.calls[0]["action_type"] == "profile_view_direct"
    assert navigator.calls[0]["direct"] is True
    assert HARD_CEILINGS["profile_view_direct"].daily == 40
    assert HARD_CEILINGS["profile_view"].daily == 100


def test_the_action_type_comes_from_the_config_rather_than_a_literal():
    assert profile_scrape_action(False) == profile_view_action(False)
    assert profile_scrape_action(True) == profile_view_action(True)


def test_a_lead_whose_sections_are_all_fresh_is_never_visited(conn, account):
    lead = create_lead(
        conn,
        account,
        "Nived Velayudhan",
        public_id="nived-velayudhan",
        member_distance="2nd",
    )
    mark_section_fetched(conn, lead.id, LeadSection.POSITIONS, fetched_at=NOW)

    result, navigator = scrape(profile_page(), conn, account)

    assert result.status is ProfileScrapeStatus.SKIPPED_FRESH
    assert navigator.calls == []


def test_a_stale_positions_window_earns_a_visit(conn, account):
    lead = create_lead(
        conn, account, "Nived Velayudhan", public_id="nived-velayudhan", member_distance="2nd"
    )
    mark_section_fetched(
        conn,
        lead.id,
        LeadSection.POSITIONS,
        fetched_at=NOW - timedelta(days=POSITIONS_CACHE_DAYS + 1),
    )

    result, navigator = scrape(profile_page(), conn, account)

    assert result.status is ProfileScrapeStatus.SCRAPED
    assert result.sections_requested == ("positions",)
    assert len(navigator.calls) == 1


def test_a_fresh_contact_window_is_not_reopened_on_a_positions_visit(conn, account):
    lead = create_lead(
        conn, account, "Nived Velayudhan", public_id="nived-velayudhan", member_distance="1st"
    )
    mark_section_fetched(conn, lead.id, LeadSection.CONTACT_INFO, fetched_at=NOW)
    mark_section_fetched(
        conn,
        lead.id,
        LeadSection.POSITIONS,
        fetched_at=NOW - timedelta(days=POSITIONS_CACHE_DAYS + 1),
    )
    page = profile_page(card=top_card(distance="1st"))

    result, _ = scrape(page, conn, account)

    assert result.sections_requested == ("positions",)
    assert page.trigger_clicks == 0
    assert result.sections_fetched == ("positions",)


def test_a_stale_contact_window_reopens_the_overlay(conn, account):
    lead = create_lead(
        conn, account, "Nived Velayudhan", public_id="nived-velayudhan", member_distance="1st"
    )
    mark_section_fetched(
        conn,
        lead.id,
        LeadSection.CONTACT_INFO,
        fetched_at=NOW - timedelta(days=CONTACT_INFO_CACHE_DAYS + 1),
    )
    mark_section_fetched(conn, lead.id, LeadSection.POSITIONS, fetched_at=NOW)
    page = profile_page(card=top_card(distance="1st"))

    result, _ = scrape(page, conn, account)

    assert "contact_info" in result.sections_requested
    assert page.trigger_clicks == 1


def test_contact_info_never_makes_a_non_first_degree_lead_look_stale(conn, account):
    lead = create_lead(
        conn, account, "Nived Velayudhan", public_id="nived-velayudhan", member_distance="3rd+"
    )
    mark_section_fetched(conn, lead.id, LeadSection.POSITIONS, fetched_at=NOW)

    assert stale_profile_sections(conn, get_lead(conn, lead.id), now=NOW) == ()
    assert needs_refresh(conn, lead.id, LeadSection.CONTACT_INFO, now=NOW)


def test_forcing_a_visit_overrides_the_cache_windows(conn, account):
    lead = create_lead(
        conn,
        account,
        "Nived Velayudhan",
        public_id="nived-velayudhan",
        member_distance="2nd",
    )
    mark_section_fetched(conn, lead.id, LeadSection.POSITIONS, fetched_at=NOW)

    result, navigator = scrape(profile_page(), conn, account, force=True)

    assert result.status is ProfileScrapeStatus.SCRAPED
    assert len(navigator.calls) == 1


def test_a_caller_that_already_knows_the_lead_can_say_so(conn, account):
    """A lead whose vanity URL changed is not found by slug, so ids win."""
    lead = create_lead(
        conn,
        account,
        "Nived Velayudhan",
        member_id="urn:li:member:918273",
        public_id="an-older-slug",
        member_distance="2nd",
    )
    mark_section_fetched(conn, lead.id, LeadSection.POSITIONS, fetched_at=NOW)

    by_slug, slug_navigator = scrape(profile_page(), conn, account)
    by_id, id_navigator = scrape(profile_page(), conn, account, lead_id=lead.id)

    assert by_slug.status is ProfileScrapeStatus.SCRAPED
    assert len(slug_navigator.calls) == 1
    assert by_id.status is ProfileScrapeStatus.SKIPPED_FRESH
    assert id_navigator.calls == []


def test_a_lead_id_from_another_account_is_rejected_rather_than_scraped(conn, account):
    other = int(
        conn.execute(
            "INSERT INTO accounts (label, timezone, state) VALUES (?, ?, ?)",
            ("secondary", "Asia/Kolkata", "active"),
        ).lastrowid
    )
    conn.commit()
    lead = create_lead(conn, other, "Someone Else", public_id="someone-else")

    with pytest.raises(ValueError):
        scrape(profile_page(), conn, account, lead_id=lead.id)


def test_a_blacklisted_lead_is_never_visited(conn, account):
    lead = create_lead(conn, account, "Nived Velayudhan", public_id="nived-velayudhan")
    blacklist_lead(conn, lead.id, reason="asked not to be contacted")

    result, navigator = scrape(profile_page(), conn, account)

    assert result.status is ProfileScrapeStatus.SKIPPED_BLACKLISTED
    assert navigator.calls == []


def test_a_blacklisted_slug_with_no_lead_row_is_never_visited(conn, account):
    """The block is global and keyed on identifiers, not on this account's rows."""
    other = int(
        conn.execute(
            "INSERT INTO accounts (label, timezone, state) VALUES (?, ?, ?)",
            ("secondary", "Asia/Kolkata", "active"),
        ).lastrowid
    )
    conn.commit()
    blacklist_identity(
        conn, other, public_id="nived-velayudhan", reason="blocked elsewhere"
    )

    result, navigator = scrape(profile_page(), conn, account)

    assert result.status is ProfileScrapeStatus.SKIPPED_BLACKLISTED
    assert navigator.calls == []
    assert conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 0


class CountingHumanizer(Humanizer):
    """A humanizer that counts the between-action cooldowns it is asked for."""

    def __init__(self) -> None:
        super().__init__(FAST, seed=3, sleep=RecordingSleep())
        self.cooldowns = 0

    async def cooldown(self) -> float:
        self.cooldowns += 1
        return await super().cooldown()


def test_a_skipped_profile_costs_no_cooldown(conn, account):
    """The pause is between things LinkedIn can see, not between database reads."""
    fresh = create_lead(
        conn, account, "Two", public_id="two", member_distance="2nd"
    )
    mark_section_fetched(conn, fresh.id, LeadSection.POSITIONS, fetched_at=NOW)
    beat = CountingHumanizer()

    batch = run(
        run_profile_scrapes(
            profile_page(),
            conn,
            account,
            [
                "https://www.linkedin.com/in/one/",
                "https://www.linkedin.com/in/two/",
                "https://www.linkedin.com/in/three/",
            ],
            humanizer=beat,
            guard=FakeGate(),
            record=FakeRecorder(),
            clock=clock,
            navigate=FakeNavigator(),
        )
    )

    assert [result.status for result in batch.results] == [
        ProfileScrapeStatus.SCRAPED,
        ProfileScrapeStatus.SKIPPED_FRESH,
        ProfileScrapeStatus.SCRAPED,
    ]
    assert batch.visited == 2
    assert beat.cooldowns == 1


def test_a_refused_profile_costs_no_cooldown(conn, account):
    beat = CountingHumanizer()

    run(
        run_profile_scrapes(
            profile_page(),
            conn,
            account,
            ["https://www.linkedin.com/in/one/", "https://www.linkedin.com/in/two/"],
            humanizer=beat,
            guard=FakeGate(allow=1),
            record=FakeRecorder(),
            clock=clock,
            navigate=FakeNavigator(),
        )
    )

    assert beat.cooldowns == 0


def test_a_challenge_mid_visit_propagates_and_is_recorded(conn, account):
    recorder = FakeRecorder()
    navigator = FakeNavigator(error=SessionExpiredError("Session expired: checkpoint"))

    with pytest.raises(SessionExpiredError):
        scrape(profile_page(), conn, account, navigator=navigator, recorder=recorder)

    assert recorder.rows[0]["outcome"].value == "failure"
    assert recorder.rows[0]["action_type"] == "profile_view"


def test_a_dry_run_reads_the_profile_without_storing_it(conn, account):
    result, _ = scrape(profile_page(), conn, account, harvest=False)

    assert result.status is ProfileScrapeStatus.SCRAPED
    assert result.detail is not None
    assert result.lead_id is None
    assert conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 0


def test_a_batch_paces_between_visits_and_stops_at_the_gate(conn, account):
    page = profile_page()
    gate = FakeGate(allow=2)
    navigator = FakeNavigator()

    batch = run(
        run_profile_scrapes(
            page,
            conn,
            account,
            [
                "https://www.linkedin.com/in/one/",
                "https://www.linkedin.com/in/two/",
                "https://www.linkedin.com/in/three/",
            ],
            humanizer=pacer(),
            guard=gate,
            record=FakeRecorder(),
            clock=clock,
            navigate=navigator,
        )
    )

    assert batch.visited == 2
    assert batch.refused is True
    assert len(navigator.calls) == 2
    assert batch.results[-1].status is ProfileScrapeStatus.REFUSED


def test_a_batch_visits_each_profile_once(conn, account):
    navigator = FakeNavigator()

    batch = run(
        run_profile_scrapes(
            profile_page(),
            conn,
            account,
            ["https://www.linkedin.com/in/one/", "https://www.linkedin.com/in/one/"],
            humanizer=pacer(),
            guard=FakeGate(),
            record=FakeRecorder(),
            clock=clock,
            navigate=navigator,
        )
    )

    assert len(navigator.calls) == 1
    assert len(batch.results) == 1


def test_the_batch_summary_is_json_friendly(conn, account):
    batch = run(
        run_profile_scrapes(
            profile_page(card=top_card(distance="1st")),
            conn,
            account,
            [PROFILE_URL],
            humanizer=pacer(),
            guard=FakeGate(),
            record=FakeRecorder(),
            clock=clock,
            navigate=FakeNavigator(),
        )
    )

    payload = batch.as_dict()

    assert payload["status"] == "success"
    assert payload["source"] == PROFILE_DETAIL_SOURCE
    assert payload["results"][0]["profile"]["public_id"] == "nived-velayudhan"
    assert payload["results"][0]["store"]["experience_rows"] == 1


class FakeSearchInput(FakeElement):
    """A search box the humanizer can actually type into."""

    def __init__(self, selector: str) -> None:
        super().__init__(selectors=(selector,))
        self.value = ""

    async def fill(self, text: str) -> None:
        self.value = text

    async def press(self, key: str) -> None:
        return None


class FakeSearchToProfilePage(FakeProfilePage):
    """The real CORE-04 path: search bar, result click, profile.

    Nothing here answers `goto` for a `/in/` URL. The only way this page reaches
    a profile is by clicking a result, which is the whole point of the exercise.
    """

    def __init__(self, slug: str, profile_elements: list[FakeElement], **kwargs) -> None:
        super().__init__(profile_elements, url="https://www.linkedin.com/feed/", **kwargs)
        self.profile_elements = list(self.pages[1])
        self.slug = slug
        self.on_profile = False

        result_link = FakeElement(
            selectors=(f'a[href*="/in/{slug}"]',), on_click=self._open_profile
        )
        self.search_elements = [
            FakeSearchInput(selector_fallbacks("global_search_input")[0]),
            result_link,
        ]

    def _open_profile(self) -> None:
        self.on_profile = True
        self.url = f"https://www.linkedin.com/in/{self.slug}/"

    @property
    def visible(self) -> list[FakeElement]:
        if not self.on_profile:
            return list(self.search_elements)
        return super().visible


def test_the_default_navigation_walks_the_search_bar_rather_than_the_address_bar(
    conn, account
):
    """CORE-04 end to end. No fake navigator, no `goto` of a /in/ URL."""
    page = FakeSearchToProfilePage(
        "nived-velayudhan",
        [
            top_card(distance="1st"),
            section("profile_experience_section", [experience_row()]),
        ],
    )

    result = run(
        run_profile_scrape(
            page,
            conn,
            account,
            PROFILE_URL,
            humanizer=pacer(),
            guard=FakeGate(),
            record=FakeRecorder(),
            clock=clock,
        )
    )

    assert result.status is ProfileScrapeStatus.SCRAPED
    assert result.navigation.method == "search_bar"
    assert page.goto_urls == []
    assert page.keyboard.pressed == ["Enter"]
    assert result.detail.person.public_id == "nived-velayudhan"
    assert rows(conn, "lead_experience", result.lead_id)


# --- Repo guards ---------------------------------------------------------


PROFILE_MODULES = (
    "linkedin_mcp/scrape/profile.py",
    "linkedin_mcp/scrape/profile_extract.py",
    "linkedin_mcp/scrape/profile_records.py",
    "linkedin_mcp/scrape/profile_store.py",
)


def test_the_deep_scraper_never_loads_a_profile_url_directly():
    """CORE-04 owns navigation. A `page.goto` here would dodge the 40/24h cap."""
    offenders = []
    for name in PROFILE_MODULES:
        source = (REPO_ROOT / name).read_text(encoding="utf-8")
        for node_ in ast.walk(ast.parse(source)):
            if not isinstance(node_, ast.Call):
                continue
            target = node_.func
            if isinstance(target, ast.Attribute) and target.attr == "goto":
                offenders.append(f"{name}: {ast.dump(target)}")
    assert offenders == []


def test_the_deep_scraper_routes_navigation_through_core_04():
    source = (REPO_ROOT / "linkedin_mcp/scrape/profile.py").read_text(encoding="utf-8")

    assert "from linkedin_mcp.browser.navigate import (" in source
    assert "goto_profile" in source
    assert "profile_view_action" in source


def test_the_deep_scraper_gates_only_action_types_the_config_knows():
    configured = set(HARD_CEILINGS)

    assert {profile_scrape_action(False), profile_scrape_action(True)} <= configured


def test_no_profile_module_sleeps_outside_the_humanizer():
    pattern = re.compile(r"\b(?:asyncio|time)\.sleep\s*\(")
    offenders = [
        name
        for name in PROFILE_MODULES
        if pattern.search((REPO_ROOT / name).read_text(encoding="utf-8"))
    ]

    assert offenders == []


def test_every_selector_the_deep_scraper_names_is_registered():
    source = (REPO_ROOT / "linkedin_mcp/scrape/profile_extract.py").read_text(
        encoding="utf-8"
    )
    referenced = {
        match.group(1)
        for match in re.finditer(
            r'"(profile_(?:detail|experience|education|skills|contact_info)_[a-z_]+)"',
            source,
        )
    }

    assert len(referenced) >= 20
    assert referenced <= set(SELECTORS)


def test_every_new_selector_group_keeps_a_fallback_chain():
    """One selector is a single point of failure against markup we cannot verify."""
    groups = [
        name
        for name in SELECTORS
        if name.startswith(("profile_detail_", "profile_contact_info_"))
        or name.startswith(
            ("profile_experience_entry", "profile_experience_section", "profile_education_entry")
        )
        or name.startswith(("profile_education_section", "profile_skills_"))
    ]

    assert len(groups) >= 25
    thin = [name for name in groups if len(SELECTORS[name]) < 2]
    assert thin == []


def test_the_profile_tables_need_no_migration():
    """Every table this issue writes to already exists in 0001_init.sql."""
    schema = (REPO_ROOT / "linkedin_mcp/core/migrations/0001_init.sql").read_text(
        encoding="utf-8"
    )

    for table in ("lead_contacts", "lead_experience", "lead_education", "lead_skills"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in schema

    # Scoped to this issue on purpose. Asserting the whole migration list would
    # make any unrelated issue that adds a migration fail a SCRAPE-03 test,
    # which points a false positive at the wrong author. What this issue can
    # promise is that it added no migration of its own.
    assert [
        path.name
        for path in (REPO_ROOT / "linkedin_mcp/core/migrations").glob("*.sql")
        if "profile" in path.name
    ] == []


def test_contact_rows_satisfy_the_schema_check_constraint(conn, account):
    """The kinds this scraper can produce are exactly the kinds the table allows."""
    lead = create_lead(conn, account, "Nived Velayudhan", public_id="nived-velayudhan")

    for kind in ("email", "work_email", "personal_email", "phone", "website", "twitter"):
        conn.execute(
            "INSERT INTO lead_contacts (lead_id, kind, value) VALUES (?, ?, ?)",
            (lead.id, kind, f"{kind}-value"),
        )
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO lead_contacts (lead_id, kind, value) VALUES (?, ?, ?)",
            (lead.id, "birthday", "March 14"),
        )
    conn.rollback()
