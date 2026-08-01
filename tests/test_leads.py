import sqlite3

import pytest

from linkedin_mcp.core.db import initialize_database
from linkedin_mcp.leads import (
    LeadNotFoundError,
    add_tag,
    add_tags,
    count_leads,
    count_leads_with_tag,
    create_lead,
    custom_field_tokens,
    delete_custom_field,
    delete_lead,
    delete_tag,
    ensure_tag,
    get_custom_field,
    get_custom_fields,
    get_lead,
    get_lead_by_member_id,
    get_lead_by_public_id,
    lead_tag_names,
    leads_with_all_tags,
    leads_with_any_tags,
    leads_with_tag,
    list_leads,
    list_tags,
    normalise_custom_field_key,
    normalise_tag_name,
    remove_tag,
    set_custom_field,
    set_custom_fields,
    update_lead,
)


@pytest.fixture()
def conn(tmp_path):
    connection = initialize_database(tmp_path / "linkedin-helper.db")
    try:
        yield connection
    finally:
        connection.close()


def create_account(conn: sqlite3.Connection, label: str) -> int:
    cursor = conn.execute(
        "INSERT INTO accounts (label, timezone, state) VALUES (?, ?, ?)",
        (label, "Asia/Kolkata", "active"),
    )
    conn.commit()
    return int(cursor.lastrowid)


@pytest.fixture()
def account(conn):
    return create_account(conn, "primary")


def test_create_lead_round_trips_profile_and_badges(conn, account):
    created = create_lead(
        conn,
        account,
        "Ada Lovelace",
        member_id="urn:li:member:1",
        public_id="ada-lovelace",
        headline="Platform engineer",
        organization_name="Analytical Engines",
        connection_count=812,
        badges={"open_to_work": True},
    )

    stored = get_lead(conn, created.id)

    assert stored == created
    assert stored.account_id == account
    assert stored.full_name == "Ada Lovelace"
    assert stored.public_id == "ada-lovelace"
    assert stored.connection_count == 812
    assert stored.badges == {"open_to_work": True}
    assert stored.first_seen_at is not None


def test_create_lead_trims_identifiers_and_requires_a_name(conn, account):
    lead = create_lead(
        conn,
        account,
        "  Grace Hopper  ",
        member_id="  urn:li:member:2  ",
        public_id="   ",
    )

    assert lead.full_name == "Grace Hopper"
    assert lead.member_id == "urn:li:member:2"
    assert lead.public_id is None

    with pytest.raises(ValueError):
        create_lead(conn, account, "   ")


def test_create_lead_rejects_unknown_fields(conn, account):
    with pytest.raises(ValueError, match="unknown lead fields: seniority"):
        create_lead(conn, account, "Alan Turing", seniority="staff")


def test_lookup_by_member_id_and_public_id_is_account_scoped(conn, account):
    other_account = create_account(conn, "secondary")
    lead = create_lead(
        conn,
        account,
        "Barbara Liskov",
        member_id="urn:li:member:3",
        public_id="barbara-liskov",
    )

    assert get_lead_by_member_id(conn, account, "urn:li:member:3") == lead
    assert get_lead_by_public_id(conn, account, "barbara-liskov") == lead
    assert get_lead_by_member_id(conn, other_account, "urn:li:member:3") is None
    assert get_lead_by_public_id(conn, other_account, "barbara-liskov") is None


def test_update_lead_writes_only_the_supplied_fields(conn, account):
    lead = create_lead(
        conn,
        account,
        "Katherine Johnson",
        public_id="katherine-johnson",
        headline="Research mathematician",
    )

    updated = update_lead(
        conn,
        lead.id,
        headline="Aerospace technologist",
        last_visited_at="2026-08-01T10:00:00Z",
        badges={"influencer": True},
    )

    assert updated.headline == "Aerospace technologist"
    assert updated.last_visited_at == "2026-08-01T10:00:00Z"
    assert updated.badges == {"influencer": True}
    assert updated.public_id == "katherine-johnson"
    assert updated.full_name == "Katherine Johnson"
    assert update_lead(conn, lead.id) == updated


def test_update_lead_rejects_unknown_fields_and_missing_leads(conn, account):
    lead = create_lead(conn, account, "Margaret Hamilton", public_id="margaret")

    with pytest.raises(ValueError, match="unknown lead fields"):
        update_lead(conn, lead.id, seniority="principal")

    with pytest.raises(LeadNotFoundError):
        update_lead(conn, lead.id + 999, headline="nope")


def test_delete_lead_removes_tags_and_custom_fields(conn, account):
    lead = create_lead(conn, account, "Radia Perlman", public_id="radia-perlman")
    add_tag(conn, lead.id, "networking")
    set_custom_field(conn, lead.id, "industry", "Infrastructure")

    assert delete_lead(conn, lead.id) is True
    assert delete_lead(conn, lead.id) is False
    assert get_lead(conn, lead.id) is None
    assert conn.execute("SELECT COUNT(*) FROM lead_tags").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM lead_custom_fields").fetchone()[0] == 0


def test_list_leads_is_account_scoped_and_paginated(conn, account):
    other_account = create_account(conn, "secondary")
    first = create_lead(conn, account, "Lead One", public_id="lead-one")
    second = create_lead(conn, account, "Lead Two", public_id="lead-two")
    create_lead(conn, other_account, "Other Lead", public_id="other-lead")

    assert [lead.id for lead in list_leads(conn, account)] == [first.id, second.id]
    assert count_leads(conn, account) == 2
    assert count_leads(conn, other_account) == 1
    assert [lead.id for lead in list_leads(conn, account, limit=1)] == [first.id]
    assert [
        lead.id for lead in list_leads(conn, account, limit=1, offset=1)
    ] == [second.id]


def test_tags_are_case_insensitive_and_idempotent(conn, account):
    lead = create_lead(conn, account, "Jean Bartik", public_id="jean-bartik")

    first = add_tag(conn, lead.id, "  Hot   Lead ")
    second = add_tag(conn, lead.id, "hot lead")

    assert normalise_tag_name(" Hot   Lead ") == "hot lead"
    assert first.id == second.id
    assert first.name == "hot lead"
    assert lead_tag_names(conn, lead.id) == ["hot lead"]
    assert [tag.name for tag in list_tags(conn, account)] == ["hot lead"]


def test_tags_do_not_leak_between_accounts(conn, account):
    other_account = create_account(conn, "secondary")
    mine = create_lead(conn, account, "Mine", public_id="mine")
    theirs = create_lead(conn, other_account, "Theirs", public_id="theirs")
    add_tag(conn, mine.id, "founder")
    add_tag(conn, theirs.id, "founder")

    my_tag = ensure_tag(conn, account, "founder")
    their_tag = ensure_tag(conn, other_account, "founder")

    assert my_tag.id != their_tag.id
    assert [lead.id for lead in leads_with_tag(conn, account, "founder")] == [mine.id]
    assert [
        lead.id for lead in leads_with_tag(conn, other_account, "founder")
    ] == [theirs.id]


def test_tag_queries_build_audiences(conn, account):
    both = create_lead(conn, account, "Both Tags", public_id="both")
    only_devtools = create_lead(conn, account, "Devtools", public_id="devtools")
    untagged = create_lead(conn, account, "Untagged", public_id="untagged")
    add_tags(conn, both.id, ["devtools", "Enterprise"], applied_by="harvester")
    add_tag(conn, only_devtools.id, "devtools")

    any_match = leads_with_any_tags(conn, account, ["devtools", "enterprise"])
    all_match = leads_with_all_tags(conn, account, ["devtools", "enterprise"])

    assert [lead.id for lead in any_match] == [both.id, only_devtools.id]
    assert [lead.id for lead in all_match] == [both.id]
    assert count_leads_with_tag(conn, account, "devtools") == 2
    assert untagged.id not in {lead.id for lead in any_match}
    assert lead_tag_names(conn, both.id) == ["devtools", "enterprise"]


def test_tag_queries_reject_empty_input(conn, account):
    lead = create_lead(conn, account, "Empty Tags", public_id="empty-tags")

    with pytest.raises(ValueError):
        leads_with_any_tags(conn, account, [])

    with pytest.raises(ValueError):
        add_tag(conn, lead.id, "   ")


def test_remove_tag_detaches_without_deleting_the_tag(conn, account):
    lead = create_lead(conn, account, "Frances Allen", public_id="frances-allen")
    add_tag(conn, lead.id, "compilers")

    assert remove_tag(conn, lead.id, "COMPILERS") is True
    assert remove_tag(conn, lead.id, "compilers") is False
    assert lead_tag_names(conn, lead.id) == []
    assert [tag.name for tag in list_tags(conn, account)] == ["compilers"]


def test_delete_tag_detaches_it_from_every_lead(conn, account):
    lead = create_lead(conn, account, "Shafi Goldwasser", public_id="shafi")
    add_tag(conn, lead.id, "crypto")

    assert delete_tag(conn, account, "crypto") is True
    assert delete_tag(conn, account, "crypto") is False
    assert lead_tag_names(conn, lead.id) == []
    assert list_tags(conn, account) == []


def test_tagging_a_missing_lead_fails_loudly(conn):
    with pytest.raises(LeadNotFoundError):
        add_tag(conn, 4242, "ghost")


def test_custom_fields_resolve_by_name_for_the_template_engine(conn, account):
    lead = create_lead(conn, account, "Anita Borg", public_id="anita-borg")

    set_custom_fields(
        conn,
        lead.id,
        {"industry": "Developer tools", "{cs_Seat_Count}": 4200, "cs_owner": None},
    )
    set_custom_field(conn, lead.id, "CS_Industry", "Platform engineering")

    assert normalise_custom_field_key("{cs_Industry}") == "industry"
    assert get_custom_fields(conn, lead.id) == {
        "industry": "Platform engineering",
        "owner": None,
        "seat_count": "4200",
    }
    assert get_custom_field(conn, lead.id, "cs_seat_count") == "4200"
    assert get_custom_field(conn, lead.id, "owner", "unassigned") == "unassigned"
    assert get_custom_field(conn, lead.id, "missing") is None
    assert custom_field_tokens(conn, lead.id) == {
        "cs_industry": "Platform engineering",
        "cs_owner": "",
        "cs_seat_count": "4200",
    }


def test_custom_fields_can_be_deleted_and_are_lead_scoped(conn, account):
    first = create_lead(conn, account, "First", public_id="first")
    second = create_lead(conn, account, "Second", public_id="second")
    set_custom_field(conn, first.id, "industry", "Fintech")

    assert get_custom_fields(conn, second.id) == {}
    assert delete_custom_field(conn, first.id, "{cs_industry}") is True
    assert delete_custom_field(conn, first.id, "industry") is False
    assert get_custom_fields(conn, first.id) == {}


def test_custom_fields_require_a_real_lead_and_a_real_key(conn, account):
    lead = create_lead(conn, account, "Real Lead", public_id="real-lead")

    with pytest.raises(LeadNotFoundError):
        set_custom_field(conn, lead.id + 999, "industry", "SaaS")

    with pytest.raises(ValueError):
        set_custom_field(conn, lead.id, "{cs_}", "SaaS")
