import sqlite3

import pytest

from linkedin_mcp.core.db import initialize_database
from linkedin_mcp.leads import (
    LeadBlacklistedError,
    LeadNotFoundError,
    add_tag,
    blacklist_identity,
    blacklist_lead,
    count_leads,
    create_lead,
    delete_lead,
    get_lead,
    is_blacklisted,
    is_blacklisted_by_member_id,
    is_blacklisted_by_public_id,
    is_identity_blacklisted,
    leads_with_tag,
    list_blacklist,
    list_leads,
    remove_from_blacklist,
    remove_lead_from_blacklist,
    update_lead,
)


MEMBER_ID = "urn:li:member:99"
PUBLIC_ID = "do-not-contact-me"


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
def accounts(conn):
    return create_account(conn, "account-a"), create_account(conn, "account-b")


def test_blacklisting_on_one_account_blocks_the_lead_on_every_other_account(
    conn, accounts
):
    account_a, account_b = accounts
    lead_a = create_lead(
        conn,
        account_a,
        "Blocked Person",
        member_id=MEMBER_ID,
        public_id=PUBLIC_ID,
    )
    lead_b = create_lead(
        conn,
        account_b,
        "Blocked Person",
        member_id=MEMBER_ID,
        public_id=PUBLIC_ID,
    )

    blacklist_lead(conn, lead_a.id, reason="asked to stop")

    assert is_blacklisted(conn, account_a, lead_a.id) is True
    assert is_blacklisted(conn, account_b, lead_b.id) is True
    assert list_leads(conn, account_b) == []
    assert count_leads(conn, account_b) == 0
    assert [entry.account_id for entry in list_blacklist(conn)] == [account_a]
    assert list_blacklist(conn, account_b) == []


def test_is_blacklisted_predicate_is_false_for_untouched_leads(conn, accounts):
    account_a, account_b = accounts
    clean = create_lead(conn, account_a, "Clean Lead", member_id="urn:li:member:1")
    blocked = create_lead(conn, account_b, "Blocked Lead", member_id=MEMBER_ID)
    blacklist_lead(conn, blocked.id)

    assert is_blacklisted(conn, account_a, clean.id) is False
    assert is_blacklisted(conn, account_b, blocked.id) is True


def test_is_blacklisted_fails_closed_for_unknown_leads(conn, accounts):
    account_a, account_b = accounts
    lead = create_lead(conn, account_a, "Known Lead", member_id="urn:li:member:7")

    assert is_blacklisted(conn, account_a, lead.id + 999) is True
    assert is_blacklisted(conn, account_b, lead.id) is True


def test_identity_predicates_ignore_the_owning_account(conn, accounts):
    account_a, _ = accounts
    blacklist_identity(
        conn,
        account_a,
        member_id=MEMBER_ID,
        public_id=PUBLIC_ID,
        reason="competitor",
    )

    assert is_blacklisted_by_member_id(conn, MEMBER_ID) is True
    assert is_blacklisted_by_public_id(conn, PUBLIC_ID) is True
    assert is_blacklisted_by_member_id(conn, "urn:li:member:other") is False
    assert is_blacklisted_by_public_id(conn, "someone-else") is False
    assert is_identity_blacklisted(conn, public_id=PUBLIC_ID) is True
    assert is_identity_blacklisted(conn) is False


def test_a_public_id_entry_blocks_a_matching_lead_on_another_account(conn, accounts):
    account_a, account_b = accounts
    lead_b = create_lead(
        conn,
        account_b,
        "Matched By Public Id",
        member_id="urn:li:member:1234",
        public_id=PUBLIC_ID,
    )

    assert is_blacklisted(conn, account_b, lead_b.id) is False

    blacklist_identity(conn, account_a, public_id=PUBLIC_ID, reason="opted out")

    assert is_blacklisted(conn, account_b, lead_b.id) is True
    assert is_blacklisted_by_member_id(conn, "urn:li:member:1234") is False
    assert list_leads(conn, account_b) == []


def test_leads_without_identifiers_are_never_matched_by_null_columns(conn, accounts):
    account_a, account_b = accounts
    blacklist_identity(conn, account_a, member_id=MEMBER_ID)
    anonymous = create_lead(conn, account_b, "Anonymous Lead")

    assert is_blacklisted(conn, account_b, anonymous.id) is False
    assert [lead.id for lead in list_leads(conn, account_b)] == [anonymous.id]


def test_creating_a_blacklisted_lead_is_refused_on_any_account(conn, accounts):
    account_a, account_b = accounts
    account_c = create_account(conn, "account-c")
    lead_a = create_lead(conn, account_a, "Blocked", member_id=MEMBER_ID)
    blacklist_lead(conn, lead_a.id, reason="asked to stop")

    with pytest.raises(LeadBlacklistedError):
        create_lead(conn, account_b, "Blocked", member_id=MEMBER_ID)

    with pytest.raises(LeadBlacklistedError):
        create_lead(conn, account_c, "Blocked", member_id=MEMBER_ID)

    assert count_leads(conn, account_b, include_blacklisted=True) == 0


def test_deleting_a_blacklisted_lead_does_not_clear_the_block(conn, accounts):
    account_a, account_b = accounts
    lead_a = create_lead(conn, account_a, "Blocked", member_id=MEMBER_ID)
    blacklist_lead(conn, lead_a.id)

    assert delete_lead(conn, lead_a.id) is True
    assert get_lead(conn, lead_a.id) is None

    with pytest.raises(LeadBlacklistedError):
        create_lead(conn, account_b, "Blocked", member_id=MEMBER_ID)

    assert is_blacklisted_by_member_id(conn, MEMBER_ID) is True


def test_updating_a_blacklisted_lead_is_refused(conn, accounts):
    account_a, _ = accounts
    lead = create_lead(conn, account_a, "Blocked", member_id=MEMBER_ID)
    blacklist_lead(conn, lead.id)

    with pytest.raises(LeadBlacklistedError):
        update_lead(conn, lead.id, headline="Refreshed after a profile visit")

    assert get_lead(conn, lead.id).headline is None


def test_adopting_a_blacklisted_identifier_is_refused(conn, accounts):
    account_a, account_b = accounts
    blacklist_identity(conn, account_a, member_id=MEMBER_ID)
    lead_b = create_lead(conn, account_b, "Clean", member_id="urn:li:member:5")

    with pytest.raises(LeadBlacklistedError):
        update_lead(conn, lead_b.id, member_id=MEMBER_ID)

    assert get_lead(conn, lead_b.id).member_id == "urn:li:member:5"


def test_audience_queries_drop_blacklisted_leads_unless_asked(conn, accounts):
    account_a, account_b = accounts
    blocked = create_lead(conn, account_b, "Blocked", member_id=MEMBER_ID)
    kept = create_lead(conn, account_b, "Kept", member_id="urn:li:member:8")
    add_tag(conn, blocked.id, "target")
    add_tag(conn, kept.id, "target")
    blacklist_identity(conn, account_a, member_id=MEMBER_ID, reason="global block")

    assert [lead.id for lead in leads_with_tag(conn, account_b, "target")] == [kept.id]
    assert [lead.id for lead in list_leads(conn, account_b)] == [kept.id]
    assert count_leads(conn, account_b) == 1

    everyone = list_leads(conn, account_b, include_blacklisted=True)
    tagged = leads_with_tag(conn, account_b, "target", include_blacklisted=True)

    assert [lead.id for lead in everyone] == [blocked.id, kept.id]
    assert [lead.id for lead in tagged] == [blocked.id, kept.id]
    assert count_leads(conn, account_b, include_blacklisted=True) == 2


def test_blacklist_entries_are_idempotent_per_account(conn, accounts):
    account_a, account_b = accounts
    first = blacklist_identity(conn, account_a, member_id=MEMBER_ID, reason="first")
    again = blacklist_identity(conn, account_a, member_id=MEMBER_ID, public_id=PUBLIC_ID)
    other = blacklist_identity(conn, account_b, member_id=MEMBER_ID, reason="second")

    assert again.id == first.id
    assert again.reason == "first"
    assert again.public_id == PUBLIC_ID
    assert other.id != first.id
    assert len(list_blacklist(conn)) == 2


def test_blacklist_requires_a_durable_identifier(conn, accounts):
    account_a, _ = accounts
    anonymous = create_lead(conn, account_a, "No Identifiers")

    with pytest.raises(ValueError):
        blacklist_lead(conn, anonymous.id)

    with pytest.raises(ValueError):
        blacklist_identity(conn, account_a, member_id="   ")

    with pytest.raises(LeadNotFoundError):
        blacklist_lead(conn, anonymous.id + 999)


def test_removing_from_the_blacklist_clears_every_account(conn, accounts):
    account_a, account_b = accounts
    lead_a = create_lead(conn, account_a, "Blocked", member_id=MEMBER_ID)
    blacklist_lead(conn, lead_a.id, reason="mistake")
    blacklist_identity(conn, account_b, member_id=MEMBER_ID, reason="mistake")

    assert remove_from_blacklist(conn, member_id=MEMBER_ID) == 2
    assert is_blacklisted(conn, account_a, lead_a.id) is False
    assert list_blacklist(conn) == []
    assert create_lead(conn, account_b, "Blocked", member_id=MEMBER_ID).member_id == (
        MEMBER_ID
    )


def test_remove_lead_from_blacklist_uses_the_lead_identity(conn, accounts):
    account_a, _ = accounts
    lead = create_lead(
        conn,
        account_a,
        "Blocked",
        member_id=MEMBER_ID,
        public_id=PUBLIC_ID,
    )
    blacklist_lead(conn, lead.id)

    assert remove_lead_from_blacklist(conn, lead.id) == 1
    assert is_blacklisted(conn, account_a, lead.id) is False

    with pytest.raises(LeadNotFoundError):
        remove_lead_from_blacklist(conn, lead.id + 999)

    with pytest.raises(ValueError):
        remove_from_blacklist(conn)
