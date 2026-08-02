"""Extraction from a rendered results page, over duck-typed element handles."""

import asyncio

import pytest
from test_scrape_fakes import (
    FakeElement,
    FakePage,
    group_member_card,
    node,
    person_card,
    post_card,
)

from linkedin_mcp.browser.selectors import selector_fallbacks
from linkedin_mcp.scrape.extract import (
    extract_group_members,
    extract_people,
    extract_posts,
    parse_current_position,
)
from linkedin_mcp.scrape.records import parse_count, public_id_from


def run(coroutine):
    return asyncio.run(coroutine)


def one_page(cards):
    return FakePage({1: list(cards)})


def test_a_complete_card_yields_every_field():
    page = one_page([person_card(premium=True, current="Current: Solution Engineer at Microsoft")])

    people = run(extract_people(page))

    assert len(people) == 1
    person = people[0]
    assert person.full_name == "Nived Velayudhan"
    assert person.public_id == "nived-velayudhan"
    assert person.hash_id == "ACoAAB1nived"
    assert person.headline == "Solution Engineer at Microsoft"
    assert person.location_name == "Bengaluru, Karnataka, India"
    assert person.member_distance == "2nd"
    assert person.avatar_url == "https://media.licdn.com/avatar.jpg"
    assert person.organization_name == "Microsoft"
    assert person.organization_title == "Solution Engineer"
    assert person.badges == {"premium": True}
    assert person.profile_url == "https://www.linkedin.com/in/nived-velayudhan/"


def test_a_card_missing_its_headline_is_still_a_lead():
    page = one_page([person_card(headline=None, location=None, avatar=None)])

    people = run(extract_people(page))

    assert len(people) == 1
    assert people[0].headline is None
    assert people[0].location_name is None
    assert people[0].is_identifiable()
    assert "full_name" in people[0].as_lead_fields()
    assert "headline" not in people[0].as_lead_fields()


def test_a_card_with_no_profile_link_is_skipped_and_the_rest_survive():
    page = one_page(
        [
            person_card(slug=None, urn=None, name="Ghost"),
            person_card(slug="real-person", name="Real Person"),
        ]
    )

    people = run(extract_people(page))

    assert [person.public_id for person in people] == ["real-person"]


def test_a_card_with_no_name_falls_back_to_its_slug():
    page = one_page([person_card(slug="jane-doe-91827364", name=None)])

    people = run(extract_people(page))

    assert people[0].full_name == "Jane Doe"
    assert people[0].public_id == "jane-doe-91827364"


def test_a_member_urn_becomes_the_member_id_and_a_profile_urn_becomes_the_hash():
    member = one_page([person_card(urn="urn:li:member:918273")])
    profile = one_page([person_card(urn="urn:li:fsd_profile:ACoAAB1x")])

    assert run(extract_people(member))[0].member_id == "urn:li:member:918273"
    assert run(extract_people(member))[0].hash_id is None
    assert run(extract_people(profile))[0].member_id is None
    assert run(extract_people(profile))[0].hash_id == "ACoAAB1x"


def test_one_exploding_card_does_not_take_the_page_down():
    broken = FakeElement(
        selectors=(selector_fallbacks("people_result_item")[0],), explode=True
    )
    page = one_page([broken, person_card(slug="survivor", name="Survivor")])

    people = run(extract_people(page))

    assert [person.full_name for person in people] == ["Survivor"]


def test_a_later_selector_fallback_still_resolves():
    card = FakeElement(
        selectors=(selector_fallbacks("people_result_item")[2],),
        attrs={"data-urn": "urn:li:member:5"},
        children=[
            node("people_result_profile_link", index=1, href="/in/fallback-person/"),
            node("people_result_name", index=2, text="Fallback Person"),
            node("people_result_headline", index=2, text="Staff Engineer"),
        ],
    )

    people = run(extract_people(one_page([card])))

    assert people[0].full_name == "Fallback Person"
    assert people[0].public_id == "fallback-person"
    assert people[0].headline == "Staff Engineer"


def test_the_names_split_into_first_and_last_for_the_lead_store():
    page = one_page([person_card(name="Ada Byron Lovelace")])

    fields = run(extract_people(page))[0].as_lead_fields()

    assert fields["first_name"] == "Ada"
    assert fields["last_name"] == "Byron Lovelace"


def test_a_summary_that_only_repeats_the_current_position_is_dropped():
    line = "Current: Principal Engineer at GitHub"
    page = one_page([person_card(summary=line, current=line)])

    person = run(extract_people(page))[0]

    assert person.summary is None
    assert person.organization_name == "GitHub"


def test_post_cards_carry_their_permalink_author_and_counts():
    page = one_page([post_card()])

    posts = run(extract_posts(page))

    assert len(posts) == 1
    post = posts[0]
    assert post.activity_id == "7123456789"
    assert post.post_url == (
        "https://www.linkedin.com/feed/update/urn:li:activity:7123456789/"
    )
    assert post.reactions == 1240
    assert post.comments == 88
    assert post.reposts == 12
    assert post.posted_at_text == "2d"
    assert post.author is not None
    assert post.author.public_id == "nived-velayudhan"
    assert post.author.headline == "Solution Engineer at Microsoft"


def test_a_post_with_no_author_link_is_still_a_post():
    page = one_page([post_card(author_slug=None, author_name=None)])

    posts = run(extract_posts(page))

    assert len(posts) == 1
    assert posts[0].author is None
    assert posts[0].activity_id == "7123456789"


def test_a_post_with_no_permalink_and_no_urn_is_dropped():
    page = one_page([post_card(activity=None, permalink=None)])

    assert run(extract_posts(page)) == []


def test_group_member_rows_become_people():
    page = one_page(
        [
            group_member_card(slug="member-one", name="Member One"),
            group_member_card(slug="member-two", name="Member Two", headline=None),
        ]
    )

    members = run(extract_group_members(page))

    assert [member.public_id for member in members] == ["member-one", "member-two"]
    assert members[0].headline == "Platform Engineer"
    assert members[1].headline is None


def test_an_empty_page_extracts_nothing_without_raising():
    assert run(extract_people(FakePage({}))) == []
    assert run(extract_posts(FakePage({}))) == []
    assert run(extract_group_members(FakePage({}))) == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1,240 reactions", 1240),
        ("88 comments", 88),
        ("2K reactions", 2000),
        ("1.5M reactions", 15_000_000),
        ("no numbers here", None),
        (None, None),
    ],
)
def test_engagement_counts_are_parsed_or_dropped(text, expected):
    assert parse_count(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Current: Solution Engineer at Microsoft", ("Microsoft", "Solution Engineer")),
        ("Current: Founder", ("Founder", None)),
        ("Talks about AI", (None, None)),
        (None, (None, None)),
    ],
)
def test_current_position_lines_are_split_into_company_and_title(text, expected):
    assert parse_current_position(text) == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.linkedin.com/in/someone/", "someone"),
        ("/in/someone?trk=x", "someone"),
        ("https://www.linkedin.com/company/microsoft/", None),
        (None, None),
    ],
)
def test_public_ids_are_read_off_profile_urls(url, expected):
    assert public_id_from(url) == expected
