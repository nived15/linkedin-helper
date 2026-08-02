import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from linkedin_mcp.core.db import initialize_database
from linkedin_mcp.leads import (
    CACHE_WINDOW_DAYS,
    CONTACT_INFO_CACHE_DAYS,
    POSITIONS_CACHE_DAYS,
    LeadBlacklistedError,
    LeadIdentityConflictError,
    LeadNotFoundError,
    LeadStoreError,
    IdentityChange,
    LeadSection,
    blacklist_identity,
    count_leads,
    create_lead,
    get_lead,
    get_lead_by_member_id,
    get_lead_by_public_id,
    harvest_leads,
    identity_history,
    leads_needing_refresh,
    list_leads,
    mark_section_fetched,
    merge_fields,
    needs_refresh,
    section_fetched_at,
    stale_sections,
    upsert_lead,
)
from linkedin_mcp.leads.dedupe import (
    FILL_WHEN_ABSENT_COLUMNS,
    MERGED_COLUMNS,
    MONOTONIC_COLUMNS,
    REFRESHED_COLUMNS,
    SECTION_COLUMNS,
    HarvestRefusalReason,
    utc_timestamp,
)
from linkedin_mcp.leads.store import FIELD_COLUMNS

NOW = datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc)


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


def synthetic_profiles(count: int, *, headline: str = "Engineer") -> list[dict]:
    return [
        {
            "full_name": f"Harvest Person {index}",
            "member_id": f"urn:li:member:{index}",
            "public_id": f"harvest-person-{index}",
            "headline": f"{headline} {index}",
            "organization_name": f"Company {index % 25}",
            "location_name": "Bengaluru",
            "member_distance": "2nd",
            "connection_count": 100 + index,
            "badges": {"premium": index % 2 == 0},
        }
        for index in range(count)
    ]


def duplicate_identifiers(conn: sqlite3.Connection, column: str) -> list[str]:
    rows = conn.execute(
        f"""
        SELECT {column} AS value
        FROM leads
        WHERE {column} IS NOT NULL
        GROUP BY account_id, {column}
        HAVING COUNT(*) > 1
        """
    ).fetchall()
    return [row["value"] for row in rows]


def test_every_writable_column_has_exactly_one_merge_rule():
    rules = (
        REFRESHED_COLUMNS,
        FILL_WHEN_ABSENT_COLUMNS,
        MERGED_COLUMNS,
        MONOTONIC_COLUMNS,
    )
    covered = set().union(*rules)

    assert covered == set(FIELD_COLUMNS.values())
    for index, rule in enumerate(rules):
        for other in rules[index + 1 :]:
            assert rule.isdisjoint(other)


def test_upsert_creates_a_lead_when_the_identity_is_new(conn, account):
    result = upsert_lead(
        conn,
        account,
        full_name="Ada Lovelace",
        member_id="urn:li:member:1",
        public_id="ada-lovelace",
        headline="Platform engineer",
    )

    assert result.created is True
    assert result.updated is False
    assert result.unchanged is False
    assert result.changed_fields == ()
    assert result.lead.headline == "Platform engineer"
    assert count_leads(conn, account) == 1


def test_repeat_harvest_never_raises_and_reports_no_change(conn, account):
    profile = {
        "full_name": "Ada Lovelace",
        "member_id": "urn:li:member:1",
        "public_id": "ada-lovelace",
        "headline": "Platform engineer",
    }

    first = upsert_lead(conn, account, **profile)
    second = upsert_lead(conn, account, **profile)

    assert second.created is False
    assert second.unchanged is True
    assert second.changed_fields == ()
    assert second.lead.id == first.lead.id
    assert count_leads(conn, account) == 1


def test_upsert_requires_an_identifier_to_deduplicate_on(conn, account):
    with pytest.raises(ValueError, match="member_id or a public_id"):
        upsert_lead(conn, account, full_name="Anonymous Person")

    assert count_leads(conn, account, include_blacklisted=True) == 0


def test_upsert_requires_a_full_name_when_creating(conn, account):
    with pytest.raises(ValueError, match="full_name is required"):
        upsert_lead(conn, account, member_id="urn:li:member:1", full_name="   ")

    assert count_leads(conn, account, include_blacklisted=True) == 0


def test_upsert_rejects_unknown_fields(conn, account):
    with pytest.raises(ValueError, match="unknown lead fields: industry"):
        upsert_lead(
            conn,
            account,
            full_name="Ada Lovelace",
            member_id="urn:li:member:1",
            industry="SaaS",
        )


def test_upsert_trims_identifiers_before_matching(conn, account):
    first = upsert_lead(
        conn,
        account,
        full_name="Ada Lovelace",
        member_id="urn:li:member:1",
        public_id="ada-lovelace",
    )
    second = upsert_lead(
        conn,
        account,
        full_name="Ada Lovelace",
        member_id="  urn:li:member:1  ",
        public_id="  ada-lovelace  ",
    )

    assert second.lead.id == first.lead.id
    assert count_leads(conn, account) == 1


def test_same_identifiers_on_another_account_stay_separate(conn, account):
    other = create_account(conn, "secondary")

    first = upsert_lead(
        conn,
        account,
        full_name="Ada Lovelace",
        member_id="urn:li:member:1",
        public_id="ada-lovelace",
    )
    second = upsert_lead(
        conn,
        other,
        full_name="Ada Lovelace",
        member_id="urn:li:member:1",
        public_id="ada-lovelace",
    )

    assert second.created is True
    assert second.lead.id != first.lead.id


def test_a_lead_created_through_the_store_is_matched_not_duplicated(conn, account):
    stored = create_lead(
        conn,
        account,
        "Ada Lovelace",
        member_id="urn:li:member:1",
        public_id="ada-lovelace",
    )

    result = upsert_lead(
        conn,
        account,
        full_name="Ada Lovelace",
        member_id="urn:li:member:1",
        public_id="ada-l",
        headline="Platform engineer",
    )

    assert result.created is False
    assert result.lead.id == stored.id
    assert result.lead.public_id == "ada-l"
    assert count_leads(conn, account) == 1


def test_vanity_url_change_updates_the_same_row(conn, account):
    created = upsert_lead(
        conn,
        account,
        full_name="Ada Lovelace",
        member_id="urn:li:member:1",
        public_id="ada-lovelace",
    )

    renamed = upsert_lead(
        conn,
        account,
        full_name="Ada Lovelace",
        member_id="urn:li:member:1",
        public_id="ada-l",
    )

    assert renamed.created is False
    assert renamed.lead.id == created.lead.id
    assert renamed.changed_fields == ("public_id",)
    assert renamed.lead.public_id == "ada-l"
    assert count_leads(conn, account) == 1
    assert get_lead_by_public_id(conn, account, "ada-lovelace") is None


def test_vanity_url_change_archives_the_old_slug(conn, account):
    lead = upsert_lead(
        conn,
        account,
        full_name="Ada Lovelace",
        member_id="urn:li:member:1",
        public_id="ada-lovelace",
    ).lead

    upsert_lead(
        conn,
        account,
        full_name="Ada Lovelace",
        member_id="urn:li:member:1",
        public_id="ada-l",
    )

    history = identity_history(conn, lead.id)
    assert [(entry.kind, entry.value, entry.replaced_by) for entry in history] == [
        ("public_id", "ada-lovelace", "ada-l")
    ]


def test_a_recycled_slug_never_merges_two_people(conn, account):
    first = upsert_lead(
        conn,
        account,
        full_name="Ada Lovelace",
        member_id="urn:li:member:1",
        public_id="shared-slug",
        headline="Platform engineer",
    ).lead

    second = upsert_lead(
        conn,
        account,
        full_name="Grace Hopper",
        member_id="urn:li:member:2",
        public_id="shared-slug",
        headline="Compiler author",
    )

    assert second.created is True
    assert second.lead.id != first.id
    assert count_leads(conn, account) == 2

    previous = get_lead(conn, first.id)
    assert previous is not None
    assert previous.member_id == "urn:li:member:1"
    assert previous.public_id is None
    assert previous.headline == "Platform engineer"
    assert get_lead_by_public_id(conn, account, "shared-slug").id == second.lead.id


def test_a_recycled_slug_is_archived_against_its_previous_owner(conn, account):
    previous = upsert_lead(
        conn,
        account,
        full_name="Ada Lovelace",
        member_id="urn:li:member:1",
        public_id="shared-slug",
    ).lead

    claimed = upsert_lead(
        conn,
        account,
        full_name="Grace Hopper",
        member_id="urn:li:member:2",
        public_id="shared-slug",
    )

    entry = identity_history(conn, previous.id)[0]
    assert entry.kind == "public_id"
    assert entry.value == "shared-slug"
    assert entry.replaced_by is None
    assert entry.claimed_by_lead_id == claimed.lead.id
    assert claimed.released == (
        IdentityChange(
            lead_id=previous.id,
            kind="public_id",
            value="shared-slug",
            claimed_by_lead_id=claimed.lead.id,
        ),
    )


def test_a_recycled_slug_moves_between_two_stored_leads(conn, account):
    previous = upsert_lead(
        conn,
        account,
        full_name="Ada Lovelace",
        member_id="urn:li:member:1",
        public_id="shared-slug",
    ).lead
    other = upsert_lead(
        conn,
        account,
        full_name="Grace Hopper",
        member_id="urn:li:member:2",
        public_id="grace-hopper",
    ).lead

    moved = upsert_lead(
        conn,
        account,
        full_name="Grace Hopper",
        member_id="urn:li:member:2",
        public_id="shared-slug",
    )

    assert moved.created is False
    assert moved.lead.id == other.id
    assert moved.lead.public_id == "shared-slug"
    assert get_lead(conn, previous.id).public_id is None
    assert count_leads(conn, account) == 2
    assert [
        (entry.lead_id, entry.value, entry.replaced_by, entry.claimed_by_lead_id)
        for entry in identity_history(conn)
    ] == [
        (previous.id, "shared-slug", None, other.id),
        (other.id, "grace-hopper", "shared-slug", None),
    ]


def test_a_slug_only_row_learns_its_member_id(conn, account):
    seen = upsert_lead(conn, account, full_name="Ada Lovelace", public_id="ada-lovelace")
    assert seen.lead.member_id is None

    enriched = upsert_lead(
        conn,
        account,
        full_name="Ada Lovelace",
        member_id="urn:li:member:1",
        public_id="ada-lovelace",
    )

    assert enriched.created is False
    assert enriched.lead.id == seen.lead.id
    assert enriched.changed_fields == ("member_id",)
    assert count_leads(conn, account) == 1


def test_member_id_is_write_once(conn, account):
    lead = upsert_lead(
        conn,
        account,
        full_name="Ada Lovelace",
        member_id="urn:li:member:1",
        public_id="ada-lovelace",
    ).lead

    result = upsert_lead(
        conn,
        account,
        full_name="Ada Lovelace",
        public_id="ada-lovelace",
        member_id=None,
    )

    assert result.lead.id == lead.id
    assert result.lead.member_id == "urn:li:member:1"
    assert "member_id" not in result.changed_fields


def test_a_source_without_member_ids_still_matches_on_the_slug(conn, account):
    lead = upsert_lead(
        conn,
        account,
        full_name="Ada Lovelace",
        member_id="urn:li:member:1",
        public_id="ada-lovelace",
    ).lead

    result = upsert_lead(
        conn,
        account,
        full_name="Ada Lovelace",
        public_id="ada-lovelace",
        headline="Platform engineer",
    )

    assert result.created is False
    assert result.lead.id == lead.id
    assert result.lead.headline == "Platform engineer"
    assert count_leads(conn, account) == 1


def test_an_unresolvable_collision_raises_and_writes_nothing(conn, account):
    slug_only = upsert_lead(conn, account, full_name="Unknown Person", public_id="shared-slug").lead
    known = upsert_lead(
        conn,
        account,
        full_name="Ada Lovelace",
        member_id="urn:li:member:1",
        public_id="ada-lovelace",
    ).lead

    with pytest.raises(LeadIdentityConflictError) as excinfo:
        upsert_lead(
            conn,
            account,
            full_name="Ada Lovelace",
            member_id="urn:li:member:1",
            public_id="shared-slug",
        )

    assert excinfo.value.kind == "public_id"
    assert excinfo.value.value == "shared-slug"
    assert {excinfo.value.lead_id, excinfo.value.other_lead_id} == {known.id, slug_only.id}
    assert get_lead(conn, slug_only.id).public_id == "shared-slug"
    assert get_lead(conn, known.id).public_id == "ada-lovelace"
    assert count_leads(conn, account) == 2
    assert identity_history(conn) == []


def test_newer_non_empty_values_win(conn, account):
    lead = upsert_lead(
        conn,
        account,
        full_name="Ada Lovelace",
        member_id="urn:li:member:1",
        headline="Platform engineer",
        organization_name="Analytical Engines",
        connection_count=812,
    ).lead

    result = upsert_lead(
        conn,
        account,
        full_name="Ada Lovelace",
        member_id="urn:li:member:1",
        headline="Principal engineer",
        connection_count=900,
    )

    assert result.updated is True
    assert result.changed_fields == ("connection_count", "headline")
    assert result.lead.id == lead.id
    assert result.lead.headline == "Principal engineer"
    assert result.lead.connection_count == 900


def test_blank_values_never_erase_stored_profile_data(conn, account):
    upsert_lead(
        conn,
        account,
        full_name="Ada Lovelace",
        member_id="urn:li:member:1",
        headline="Platform engineer",
        summary="Builds analytical engines",
        organization_name="Analytical Engines",
    )

    result = upsert_lead(
        conn,
        account,
        full_name="Ada Lovelace",
        member_id="urn:li:member:1",
        headline="   ",
        summary=None,
        organization_name="",
    )

    assert result.unchanged is True
    assert result.lead.headline == "Platform engineer"
    assert result.lead.summary == "Builds analytical engines"
    assert result.lead.organization_name == "Analytical Engines"


def test_a_zero_count_is_stored_because_it_is_not_blank(conn, account):
    upsert_lead(
        conn,
        account,
        full_name="Ada Lovelace",
        member_id="urn:li:member:1",
        follower_count=40,
    )

    result = upsert_lead(
        conn,
        account,
        full_name="Ada Lovelace",
        member_id="urn:li:member:1",
        follower_count=0,
    )

    assert result.changed_fields == ("follower_count",)
    assert result.lead.follower_count == 0


def test_badges_merge_key_by_key(conn, account):
    upsert_lead(
        conn,
        account,
        full_name="Ada Lovelace",
        member_id="urn:li:member:1",
        badges={"premium": True, "open_to_work": False},
    )

    result = upsert_lead(
        conn,
        account,
        full_name="Ada Lovelace",
        member_id="urn:li:member:1",
        badges={"open_to_work": True, "hiring": True},
    )

    assert result.changed_fields == ("badges",)
    assert result.lead.badges == {"premium": True, "open_to_work": True, "hiring": True}


def test_repeating_the_same_badges_changes_nothing(conn, account):
    badges = {"premium": True, "influencer": True}
    upsert_lead(
        conn,
        account,
        full_name="Ada Lovelace",
        member_id="urn:li:member:1",
        badges=badges,
    )

    result = upsert_lead(
        conn,
        account,
        full_name="Ada Lovelace",
        member_id="urn:li:member:1",
        badges=dict(reversed(list(badges.items()))),
    )

    assert result.unchanged is True


def test_connected_at_fills_once_and_is_then_left_alone(conn, account):
    filled = upsert_lead(
        conn,
        account,
        full_name="Ada Lovelace",
        member_id="urn:li:member:1",
        connected_at="2026-01-05 10:00:00",
    )
    assert filled.lead.connected_at == "2026-01-05 10:00:00"

    result = upsert_lead(
        conn,
        account,
        full_name="Ada Lovelace",
        member_id="urn:li:member:1",
        connected_at="2026-03-09 10:00:00",
    )

    assert result.unchanged is True
    assert result.lead.connected_at == "2026-01-05 10:00:00"


def test_last_visited_at_only_moves_forward(conn, account):
    upsert_lead(
        conn,
        account,
        full_name="Ada Lovelace",
        member_id="urn:li:member:1",
        last_visited_at=NOW,
    )

    backwards = upsert_lead(
        conn,
        account,
        full_name="Ada Lovelace",
        member_id="urn:li:member:1",
        last_visited_at=NOW - timedelta(days=3),
    )
    assert backwards.unchanged is True
    assert backwards.lead.last_visited_at == utc_timestamp(NOW)

    forwards = upsert_lead(
        conn,
        account,
        full_name="Ada Lovelace",
        member_id="urn:li:member:1",
        last_visited_at=NOW + timedelta(days=1),
    )
    assert forwards.changed_fields == ("last_visited_at",)
    assert forwards.lead.last_visited_at == utc_timestamp(NOW + timedelta(days=1))


def test_first_seen_at_survives_every_later_sighting(conn, account):
    created = upsert_lead(
        conn,
        account,
        full_name="Ada Lovelace",
        member_id="urn:li:member:1",
        public_id="ada-lovelace",
    ).lead

    updated = upsert_lead(
        conn,
        account,
        full_name="Ada Lovelace",
        member_id="urn:li:member:1",
        public_id="ada-l",
        headline="Principal engineer",
    ).lead

    assert updated.first_seen_at == created.first_seen_at


def test_merge_fields_reports_only_real_changes():
    stored = {
        "member_id": "urn:li:member:1",
        "public_id": "ada-lovelace",
        "headline": "Platform engineer",
        "badges_json": '{"premium": true}',
        "connected_at": "2026-01-05 10:00:00",
        "last_visited_at": "2026-07-01 10:00:00",
    }

    assert merge_fields(stored, {"headline": "Platform engineer"}) == {}
    assert merge_fields(stored, {"headline": "Principal"}) == {"headline": "Principal"}
    assert merge_fields(stored, {"member_id": "urn:li:member:2"}) == {}
    assert merge_fields(stored, {"connected_at": "2026-04-01 10:00:00"}) == {}
    assert merge_fields(stored, {"last_visited_at": "2026-06-01 10:00:00"}) == {}
    assert merge_fields(stored, {"last_visited_at": "2026-08-01 10:00:00"}) == {
        "last_visited_at": "2026-08-01 10:00:00"
    }
    assert merge_fields({}, {"member_id": "urn:li:member:9"}) == {
        "member_id": "urn:li:member:9"
    }


def test_upserting_a_blacklisted_identity_is_refused(conn, account):
    blacklist_identity(conn, account, public_id="ada-lovelace", reason="asked to stop")

    with pytest.raises(LeadBlacklistedError):
        upsert_lead(
            conn,
            account,
            full_name="Ada Lovelace",
            member_id="urn:li:member:1",
            public_id="ada-lovelace",
        )

    assert count_leads(conn, account, include_blacklisted=True) == 0


def test_a_blacklisted_lead_is_never_refreshed(conn, account):
    lead = upsert_lead(
        conn,
        account,
        full_name="Ada Lovelace",
        member_id="urn:li:member:1",
        public_id="ada-lovelace",
        headline="Platform engineer",
    ).lead
    blacklist_identity(conn, account, member_id="urn:li:member:1")

    with pytest.raises(LeadBlacklistedError):
        upsert_lead(
            conn,
            account,
            full_name="Ada Lovelace",
            member_id="urn:li:member:1",
            headline="Principal engineer",
        )

    assert get_lead(conn, lead.id).headline == "Platform engineer"


def test_a_vanity_url_change_cannot_resurrect_a_blacklisted_lead(conn, account):
    lead = upsert_lead(
        conn,
        account,
        full_name="Ada Lovelace",
        member_id="urn:li:member:1",
        public_id="ada-lovelace",
    ).lead
    blacklist_identity(conn, account, public_id="ada-lovelace", reason="asked to stop")
    assert list_leads(conn, account) == []

    with pytest.raises(LeadBlacklistedError):
        upsert_lead(
            conn,
            account,
            full_name="Ada Lovelace",
            member_id="urn:li:member:1",
            public_id="ada-l",
        )

    stored = get_lead(conn, lead.id)
    assert stored.public_id == "ada-lovelace"
    assert list_leads(conn, account) == []
    assert identity_history(conn) == []


def test_freeing_a_slug_cannot_resurrect_its_blacklisted_owner(conn, account):
    blocked = upsert_lead(
        conn,
        account,
        full_name="Ada Lovelace",
        member_id="urn:li:member:1",
        public_id="shared-slug",
    ).lead
    blacklist_identity(conn, account, public_id="shared-slug", reason="asked to stop")

    with pytest.raises(LeadBlacklistedError):
        upsert_lead(
            conn,
            account,
            full_name="Grace Hopper",
            member_id="urn:li:member:2",
            public_id="shared-slug",
        )

    assert get_lead(conn, blocked.id).public_id == "shared-slug"
    assert list_leads(conn, account) == []
    assert count_leads(conn, account, include_blacklisted=True) == 1


def test_a_slug_freed_from_a_member_blacklisted_lead_still_blocks_that_lead(conn, account):
    blocked = upsert_lead(
        conn,
        account,
        full_name="Ada Lovelace",
        member_id="urn:li:member:1",
        public_id="shared-slug",
    ).lead
    blacklist_identity(conn, account, member_id="urn:li:member:1", reason="asked to stop")

    claimed = upsert_lead(
        conn,
        account,
        full_name="Grace Hopper",
        member_id="urn:li:member:2",
        public_id="shared-slug",
    )

    assert claimed.created is True
    assert get_lead(conn, blocked.id).public_id is None
    assert [lead.id for lead in list_leads(conn, account)] == [claimed.lead.id]


def test_cache_windows_match_the_extraction_budget():
    assert CONTACT_INFO_CACHE_DAYS == 21
    assert POSITIONS_CACHE_DAYS == 14
    assert CACHE_WINDOW_DAYS == {"contact_info": 21, "positions": 14}
    assert set(SECTION_COLUMNS) == {section.value for section in LeadSection}


def test_a_section_needs_refreshing_until_it_is_fetched(conn, account):
    lead = upsert_lead(
        conn,
        account,
        full_name="Ada Lovelace",
        member_id="urn:li:member:1",
    ).lead

    assert section_fetched_at(conn, lead.id, LeadSection.CONTACT_INFO) is None
    assert needs_refresh(conn, lead.id, "contact_info", now=NOW) is True
    assert stale_sections(conn, lead.id, now=NOW) == ("contact_info", "positions")

    mark_section_fetched(conn, lead.id, "contact_info", fetched_at=NOW)

    assert section_fetched_at(conn, lead.id, "contact_info") == utc_timestamp(NOW)
    assert needs_refresh(conn, lead.id, "contact_info", now=NOW) is False
    assert stale_sections(conn, lead.id, now=NOW) == ("positions",)


@pytest.mark.parametrize(
    ("section", "fresh_days", "stale_days"),
    [("contact_info", 20, 21), ("positions", 13, 14)],
)
def test_each_section_expires_on_its_own_window(conn, account, section, fresh_days, stale_days):
    lead = upsert_lead(
        conn,
        account,
        full_name="Ada Lovelace",
        member_id="urn:li:member:1",
    ).lead
    mark_section_fetched(conn, lead.id, section, fetched_at=NOW)

    assert needs_refresh(conn, lead.id, section, now=NOW + timedelta(days=fresh_days)) is False
    assert needs_refresh(conn, lead.id, section, now=NOW + timedelta(days=stale_days)) is True


def test_upsert_records_the_sections_it_fetched(conn, account):
    result = upsert_lead(
        conn,
        account,
        full_name="Ada Lovelace",
        member_id="urn:li:member:1",
        sections_fetched=[LeadSection.POSITIONS],
        fetched_at=NOW,
    )

    assert result.sections_marked == ("positions",)
    assert result.lead.positions_fetched_at == utc_timestamp(NOW)
    assert result.lead.contact_info_fetched_at is None
    assert needs_refresh(conn, result.lead.id, "positions", now=NOW) is False
    assert needs_refresh(conn, result.lead.id, "contact_info", now=NOW) is True


def test_an_unknown_section_is_rejected(conn, account):
    lead = upsert_lead(
        conn,
        account,
        full_name="Ada Lovelace",
        member_id="urn:li:member:1",
    ).lead

    with pytest.raises(ValueError, match="unknown lead section"):
        needs_refresh(conn, lead.id, "languages", now=NOW)


def test_the_cache_predicate_fails_loudly_for_an_unknown_lead(conn, account):
    with pytest.raises(LeadNotFoundError):
        needs_refresh(conn, 4321, "contact_info", now=NOW)


def test_leads_needing_refresh_queues_the_stalest_first(conn, account):
    never = upsert_lead(conn, account, full_name="Never Fetched", member_id="m-never").lead
    old = upsert_lead(conn, account, full_name="Old Fetch", member_id="m-old").lead
    recent = upsert_lead(conn, account, full_name="Recent Fetch", member_id="m-recent").lead
    fresh = upsert_lead(conn, account, full_name="Fresh Fetch", member_id="m-fresh").lead

    mark_section_fetched(conn, old.id, "contact_info", fetched_at=NOW - timedelta(days=90))
    mark_section_fetched(conn, recent.id, "contact_info", fetched_at=NOW - timedelta(days=30))
    mark_section_fetched(conn, fresh.id, "contact_info", fetched_at=NOW - timedelta(days=2))

    queued = leads_needing_refresh(conn, account, "contact_info", now=NOW)

    assert [lead.id for lead in queued] == [never.id, old.id, recent.id]


def test_leads_needing_refresh_never_queues_a_blacklisted_lead(conn, account):
    wanted = upsert_lead(conn, account, full_name="Ada Lovelace", member_id="urn:li:member:1").lead
    upsert_lead(conn, account, full_name="Grace Hopper", member_id="urn:li:member:2")
    blacklist_identity(conn, account, member_id="urn:li:member:2")

    queued = leads_needing_refresh(conn, account, "positions", now=NOW)

    assert [lead.id for lead in queued] == [wanted.id]
    assert len(leads_needing_refresh(conn, account, "positions", now=NOW, include_blacklisted=True)) == 2


def test_leads_needing_refresh_pages_through_the_queue(conn, account):
    for index in range(5):
        upsert_lead(conn, account, full_name=f"Person {index}", member_id=f"m-{index}")

    page = leads_needing_refresh(conn, account, "positions", now=NOW, limit=2, offset=2)

    assert len(page) == 2


def test_an_unknown_account_fails_with_a_typed_store_error(conn):
    with pytest.raises(LeadStoreError) as excinfo:
        upsert_lead(conn, 999, full_name="Ada Lovelace", member_id="urn:li:member:1")

    assert not isinstance(excinfo.value, LeadIdentityConflictError)
    assert conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 0


def test_a_profile_repeated_inside_one_run_is_stored_once(conn, account):
    profiles = synthetic_profiles(4)
    overlapping = [*profiles, *profiles[2:]]

    summary = harvest_leads(conn, account, overlapping)

    assert summary.found == 6
    assert summary.created == 4
    assert summary.matched == 2
    assert count_leads(conn, account) == 4
    assert duplicate_identifiers(conn, "public_id") == []


def test_the_two_freshness_predicates_agree_on_a_non_canonical_timestamp(conn, account):
    lead = upsert_lead(
        conn,
        account,
        full_name="Ada Lovelace",
        member_id="urn:li:member:1",
    ).lead
    expired = NOW - timedelta(days=CONTACT_INFO_CACHE_DAYS, hours=7)

    mark_section_fetched(conn, lead.id, "contact_info", fetched_at=expired.isoformat())

    assert section_fetched_at(conn, lead.id, "contact_info") == utc_timestamp(expired)
    assert needs_refresh(conn, lead.id, "contact_info", now=NOW) is True
    assert [
        queued.id for queued in leads_needing_refresh(conn, account, "contact_info", now=NOW)
    ] == [lead.id]


@pytest.mark.parametrize(
    ("supplied", "stored"),
    [
        ("2026-08-01 09:00:00", "2026-08-01 09:00:00"),
        ("2026-08-01T09:00:00", "2026-08-01 09:00:00"),
        ("2026-08-01T09:00:00Z", "2026-08-01 09:00:00"),
        ("2026-08-01 14:30:00+05:30", "2026-08-01 09:00:00"),
        (NOW, "2026-08-01 09:00:00"),
    ],
)
def test_every_accepted_timestamp_spelling_is_stored_as_utc(conn, account, supplied, stored):
    lead = upsert_lead(
        conn,
        account,
        full_name="Ada Lovelace",
        member_id="urn:li:member:1",
        last_visited_at=supplied,
    ).lead

    assert lead.last_visited_at == stored
    assert mark_section_fetched(conn, lead.id, "positions", fetched_at=supplied) == stored


def test_an_offset_timestamp_is_stored_as_utc(conn, account):
    lead = upsert_lead(
        conn,
        account,
        full_name="Ada Lovelace",
        member_id="urn:li:member:1",
        last_visited_at="2026-08-01 14:30:00+05:30",
    ).lead

    assert lead.last_visited_at == "2026-08-01 09:00:00"


def test_an_unparseable_timestamp_is_rejected(conn, account):
    lead = upsert_lead(
        conn,
        account,
        full_name="Ada Lovelace",
        member_id="urn:li:member:1",
    ).lead

    with pytest.raises(ValueError, match="is not a timestamp"):
        mark_section_fetched(conn, lead.id, "contact_info", fetched_at="last tuesday")

    assert section_fetched_at(conn, lead.id, "contact_info") is None


def test_a_mistyped_section_fails_the_call_not_every_profile(conn, account):
    with pytest.raises(ValueError, match="unknown lead section"):
        harvest_leads(conn, account, synthetic_profiles(5), sections_fetched=["contact-info"])

    assert count_leads(conn, account, include_blacklisted=True) == 0


def test_harvest_stamps_every_section_it_fetched(conn, account):
    summary = harvest_leads(
        conn,
        account,
        synthetic_profiles(3),
        sections_fetched=["positions"],
        fetched_at=NOW,
    )

    assert summary.created == 3
    for lead_id in summary.lead_ids:
        assert section_fetched_at(conn, lead_id, "positions") == utc_timestamp(NOW)
        assert needs_refresh(conn, lead_id, "positions", now=NOW) is False


def test_harvest_counts_new_versus_updated(conn, account):
    profiles = synthetic_profiles(10)

    first = harvest_leads(conn, account, profiles)
    second = harvest_leads(conn, account, synthetic_profiles(10, headline="Staff engineer"))

    assert (first.found, first.created, first.matched) == (10, 10, 0)
    assert (second.found, second.created, second.updated, second.unchanged) == (10, 0, 10, 0)
    assert second.lead_ids == first.lead_ids
    assert count_leads(conn, account) == 10


def test_harvest_keeps_going_past_a_refused_profile(conn, account):
    blacklist_identity(conn, account, public_id="harvest-person-3")
    profiles = synthetic_profiles(5)
    profiles.append({"full_name": "No Identity"})

    summary = harvest_leads(conn, account, profiles)

    assert summary.found == 6
    assert summary.created == 4
    assert summary.refused == 2
    assert [refusal.reason for refusal in summary.refusals] == [
        HarvestRefusalReason.BLACKLISTED.value,
        HarvestRefusalReason.INVALID_PROFILE.value,
    ]
    assert summary.refusals[0].public_id == "harvest-person-3"
    assert count_leads(conn, account, include_blacklisted=True) == 4


def test_harvest_reports_an_identity_conflict_without_stopping(conn, account):
    upsert_lead(conn, account, full_name="Unknown Person", public_id="harvest-person-2")
    upsert_lead(conn, account, full_name="Harvest Person 2", member_id="urn:li:member:2")

    summary = harvest_leads(conn, account, synthetic_profiles(4))

    assert summary.found == 4
    assert summary.refused == 1
    assert summary.refusals[0].reason == HarvestRefusalReason.IDENTITY_CONFLICT.value
    assert summary.refusals[0].index == 2
    assert summary.created == 3


def test_harvesting_500_profiles_twice_leaves_no_duplicates(conn, account):
    profiles = synthetic_profiles(500)

    first = harvest_leads(conn, account, profiles)
    second = harvest_leads(conn, account, profiles)

    assert (first.found, first.created, first.matched, first.refused) == (500, 500, 0, 0)
    assert (second.found, second.created, second.matched, second.refused) == (500, 0, 500, 0)
    assert (second.updated, second.unchanged) == (0, 500)
    assert second.lead_ids == first.lead_ids

    assert count_leads(conn, account) == 500
    assert duplicate_identifiers(conn, "member_id") == []
    assert duplicate_identifiers(conn, "public_id") == []
    assert conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 500


def test_a_third_harvest_counts_only_the_profiles_that_changed(conn, account):
    harvest_leads(conn, account, synthetic_profiles(500))

    changed = synthetic_profiles(500)
    for profile in changed[:100]:
        profile["headline"] = "Staff engineer"
    for profile in changed[100:150]:
        profile["public_id"] = profile["public_id"].replace("harvest-person", "person")

    summary = harvest_leads(conn, account, changed)

    assert (summary.created, summary.updated, summary.unchanged) == (0, 150, 350)
    assert count_leads(conn, account) == 500
    assert duplicate_identifiers(conn, "public_id") == []
    assert len(identity_history(conn, kind="public_id")) == 50
    assert get_lead_by_member_id(conn, account, "urn:li:member:120").public_id == "person-120"
