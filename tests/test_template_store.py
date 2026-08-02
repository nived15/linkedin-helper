"""SEQ-02 template storage and rendering against real lead rows.

Pure syntax, spintax, variation and style behaviour lives in
`tests/test_templating.py`.
"""

import re
import sqlite3

import pytest

from linkedin_mcp.core.db import initialize_database, migration_files
from linkedin_mcp.leads import create_lead, delete_lead, set_custom_field
from linkedin_mcp.templating import (
    LEAD_TOKEN_COLUMNS,
    MUTUAL_TOTAL_CUSTOM_FIELD,
    MUTUAL_TOTAL_TOKEN,
    SKIPPED_SUBLIST,
    RenderRefusalReason,
    Template,
    TemplateNotFoundError,
    TemplateStyleError,
    TemplateSyntaxError,
    compile_template,
    count_templates,
    create_template,
    delete_template,
    get_template,
    get_template_by_name,
    inline_template,
    lead_context,
    lead_tokens,
    list_templates,
    render_for_lead,
    render_template,
    require_template,
    safe_render_for_lead,
    update_template,
)


EM_DASH = "\u2014"


@pytest.fixture()
def conn(tmp_path):
    connection = initialize_database(tmp_path / "linkedin-helper.db")
    try:
        yield connection
    finally:
        connection.close()


def create_account(conn: sqlite3.Connection, label: str = "primary") -> int:
    cursor = conn.execute(
        "INSERT INTO accounts (label, timezone, state) VALUES (?, ?, ?)",
        (label, "Asia/Kolkata", "active"),
    )
    conn.commit()
    return int(cursor.lastrowid)


@pytest.fixture()
def account(conn):
    return create_account(conn)


@pytest.fixture()
def lead(conn, account):
    return create_lead(
        conn,
        account,
        "Nived Velayudhan",
        first_name="Nived",
        last_name="Velayudhan",
        headline="Solution Engineer at Microsoft",
        organization_name="Microsoft",
        organization_title="Solution Engineer",
        location_name="Bengaluru",
        member_id="ACoAA123",
        public_id="nivedv",
    )


# --------------------------------------------------------------------------
# The schema SEQ-02 builds on, unchanged
# --------------------------------------------------------------------------

TEMPLATES_DDL = re.compile(
    r"\balter\s+table\s+templates\b"
    r"|\bcreate\s+table\s+(?:if\s+not\s+exists\s+)?templates\b"
    r"|\bcreate\s+unique\s+index\b[^;]*?\bon\s+templates\b",
    re.IGNORECASE,
)


def migrations_touching_templates(paths):
    """Return migrations after 0001_init that reshape the `templates` table.

    Matches three kinds of DDL, and the boundaries are chosen rather than
    incidental:

    * `CREATE TABLE templates` and `ALTER TABLE templates` change the columns
      this package writes.
    * `CREATE UNIQUE INDEX ... ON templates` changes what `create_template` is
      allowed to insert, so it is drift even though the columns are untouched.
      A unique index on `body` would start rejecting valid templates with no
      change to this package at all.
    * A plain `CREATE INDEX ... ON templates` is not matched. It is a read
      optimisation and cannot make a stored template invalid.

    A migration that merely references `templates (id)` in a foreign key is
    another module's business and is not a drift signal here. The `[^;]` in the
    unique-index branch keeps the match inside one statement, so a unique index
    on some other table followed by a plain index on `templates` does not read
    as a single match.
    """
    return [
        path
        for path in paths
        if path.stem != "0001_init"
        and TEMPLATES_DDL.search(path.read_text(encoding="utf-8"))
    ]


def test_seq_02_adds_no_migration():
    # `templates` already exists in 0001_init.sql and is shaped for this feature,
    # so a migration reshaping it would mean the design drifted.
    #
    # The claim is deliberately local. An earlier version asserted the complete
    # migration list, which made this SEQ-02 test fail the moment SEQ-01 landed
    # a legitimate migration of its own. That is a false positive aimed at the
    # wrong author, and the tempting fix is to delete the test rather than read
    # it. Other modules may add all the migrations they like.
    stems = [path.stem for path in migration_files()]
    assert stems[:2] == ["0001_init", "0002_lead_dedupe"]
    assert migrations_touching_templates(migration_files()) == []


def test_the_no_migration_check_ignores_other_modules(tmp_path):
    (tmp_path / "0001_init.sql").write_text(
        "CREATE TABLE IF NOT EXISTS templates (id INTEGER PRIMARY KEY);",
        encoding="utf-8",
    )
    # Shaped like SEQ-01's migration: a new index and a table that merely points
    # at `templates`. Neither is SEQ-02 drift, so neither may trip this check.
    (tmp_path / "0003_sequence_jobs.sql").write_text(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_one_open_job_per_lead\n"
        "    ON jobs (lead_id) WHERE state = 'pending';\n"
        "CREATE TABLE IF NOT EXISTS job_quarantine (\n"
        "    job_id INTEGER NOT NULL,\n"
        "    template_id INTEGER,\n"
        "    FOREIGN KEY (template_id) REFERENCES templates (id)\n"
        ");",
        encoding="utf-8",
    )
    assert migrations_touching_templates(migration_files(tmp_path)) == []

    # Reshaping the table itself is drift, and this still catches it.
    (tmp_path / "0004_template_locale.sql").write_text(
        "ALTER TABLE templates ADD COLUMN locale TEXT;", encoding="utf-8"
    )
    assert [
        path.stem for path in migrations_touching_templates(migration_files(tmp_path))
    ] == ["0004_template_locale"]


def test_a_unique_index_on_templates_counts_as_drift(tmp_path):
    # A unique index adds no column but changes what `create_template` may
    # insert, so it is drift. A plain index is a read optimisation and is not.
    (tmp_path / "0003_template_index.sql").write_text(
        "CREATE INDEX IF NOT EXISTS idx_templates_kind ON templates (kind);",
        encoding="utf-8",
    )
    assert migrations_touching_templates(migration_files(tmp_path)) == []

    (tmp_path / "0004_template_unique.sql").write_text(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_templates_body\n"
        "    ON templates (account_id, body);",
        encoding="utf-8",
    )
    assert [
        path.stem for path in migrations_touching_templates(migration_files(tmp_path))
    ] == ["0004_template_unique"]


def test_a_unique_index_on_another_table_is_not_drift(tmp_path):
    # The exact shape of SEQ-01's migration: a unique index on `jobs` in one
    # statement, then a plain index on `templates` in the next. Matching across
    # the statement boundary would read this as a unique index on templates.
    (tmp_path / "0003_sequence_jobs.sql").write_text(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_one_open_job_per_lead\n"
        "    ON jobs (lead_id) WHERE state = 'pending';\n"
        "CREATE INDEX IF NOT EXISTS idx_templates_account\n"
        "    ON templates (account_id);",
        encoding="utf-8",
    )
    assert migrations_touching_templates(migration_files(tmp_path)) == []


def test_templates_table_has_every_column_the_store_writes(conn):
    columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(templates)").fetchall()
    }
    assert {
        "id",
        "account_id",
        "name",
        "body",
        "variations_json",
        "kind",
        "ai_spec_json",
        "is_ai_generated",
        "created_at",
    } <= columns


# --------------------------------------------------------------------------
# Token to column mapping
# --------------------------------------------------------------------------


def test_public_tokens_map_onto_the_real_lead_columns(conn, lead):
    assert dict(LEAD_TOKEN_COLUMNS) == {
        "firstName": "first_name",
        "lastName": "last_name",
        "fullName": "full_name",
        "company": "organization_name",
        "position": "organization_title",
        "headline": "headline",
        "location": "location_name",
        "memberId": "member_id",
        "publicId": "public_id",
    }
    for token, column in LEAD_TOKEN_COLUMNS.items():
        stored = conn.execute(
            f"SELECT {column} AS value FROM leads WHERE id = ?", (lead.id,)
        ).fetchone()["value"]
        assert lead_tokens(lead)[token] == (stored or "")


def test_lead_tokens_render_none_columns_as_absent(conn, account):
    bare = create_lead(conn, account, "No Details")
    tokens = lead_tokens(bare)
    assert tokens["firstName"] == ""
    assert tokens["company"] == ""
    assert tokens["fullName"] == "No Details"


def test_lead_context_includes_custom_field_tokens(conn, lead):
    set_custom_field(conn, lead.id, "industry", "SaaS")
    context = lead_context(conn, lead)
    assert context["cs_industry"] == "SaaS"
    assert context["firstName"] == "Nived"


def test_lead_context_accepts_a_lead_id(conn, lead):
    assert lead_context(conn, lead.id)["company"] == "Microsoft"


def test_lead_context_raises_for_an_unknown_lead(conn):
    with pytest.raises(LookupError):
        lead_context(conn, 9999)


def test_mutual_total_reads_the_custom_field(conn, lead):
    # DB-01 has no mutual-connections column, so `{mutualTotal}` aliases the
    # `cs_mutual_total` custom field until the profile scraper adds one.
    set_custom_field(conn, lead.id, "mutual_total", "14")
    context = lead_context(conn, lead)
    assert context[MUTUAL_TOTAL_CUSTOM_FIELD] == "14"
    assert context[MUTUAL_TOTAL_TOKEN] == "14"


def test_mutual_total_is_absent_when_never_scraped(conn, lead):
    assert lead_context(conn, lead)[MUTUAL_TOTAL_TOKEN] == ""


def test_extras_override_the_stored_context(conn, lead):
    context = lead_context(conn, lead, extras={"mutualTotal": 7, "company": "Contoso"})
    assert context[MUTUAL_TOTAL_TOKEN] == "7"
    assert context["company"] == "Contoso"


def test_extras_keys_are_normalised(conn, lead):
    assert lead_context(conn, lead, extras={"cs_Industry": "SaaS"})["cs_industry"] == (
        "SaaS"
    )


# --------------------------------------------------------------------------
# Rendering against a stored lead
# --------------------------------------------------------------------------


def test_render_for_lead_personalises_from_the_database(conn, lead):
    body = "Hi {firstName}, you are a {position} at {company}."
    assert render_for_lead(conn, body, lead).text == (
        "Hi Nived, you are a Solution Engineer at Microsoft."
    )


def test_render_for_lead_accepts_a_lead_id(conn, lead):
    assert render_for_lead(conn, "Hi {firstName}.", lead.id).text == "Hi Nived."


def test_render_for_lead_reports_the_lead_id(conn, lead):
    assert render_for_lead(conn, "Hi {firstName}.", lead).lead_id == lead.id


def test_render_for_lead_uses_custom_fields(conn, lead):
    set_custom_field(conn, lead.id, "industry", "SaaS")
    body = "{IF cs_industry}You are in {cs_industry}.{ELSE}What do you build?{END}"
    assert render_for_lead(conn, body, lead).text == "You are in SaaS."


def test_render_for_lead_falls_back_when_a_custom_field_is_missing(conn, lead):
    body = "{IF cs_industry}You are in {cs_industry}.{ELSE}What do you build?{END}"
    assert render_for_lead(conn, body, lead).text == "What do you build?"


def test_render_for_lead_refuses_when_a_column_is_null(conn, account):
    bare = create_lead(conn, account, "No Details")
    result = safe_render_for_lead(conn, "Hi {firstName},", bare)
    assert not result.ok
    assert result.refusal.reason is RenderRefusalReason.MISSING_VARIABLE
    assert result.sublist == SKIPPED_SUBLIST


def test_render_for_lead_refuses_on_a_whitespace_only_column(conn, account):
    blank = create_lead(conn, account, "Blank First", first_name="   ")
    result = safe_render_for_lead(conn, "Hi {firstName},", blank)
    assert not result.ok
    assert result.refusal.reason is RenderRefusalReason.MISSING_VARIABLE


def test_render_for_lead_guards_the_null_column(conn, account):
    bare = create_lead(conn, account, "No Details")
    body = "{IF firstName}Hi {firstName},{ELSE}Hi there,{END} quick question."
    assert render_for_lead(conn, body, bare).text == "Hi there, quick question."


def test_deleted_lead_is_a_refusal_not_a_crash(conn, lead):
    delete_lead(conn, lead.id)
    result = safe_render_for_lead(conn, "Hi {firstName}.", lead.id)
    assert not result.ok
    assert result.refusal.reason is RenderRefusalReason.LEAD_NOT_FOUND
    assert result.sublist == SKIPPED_SUBLIST


def test_em_dash_in_scraped_company_is_normalised_not_refused(conn, account):
    lead = create_lead(
        conn,
        account,
        "Dashy Person",
        first_name="Dashy",
        organization_name=f"Foo {EM_DASH} Bar",
    )
    message = render_for_lead(conn, "Hi {firstName}, at {company}.", lead)
    assert message.text == "Hi Dashy, at Foo - Bar."
    assert EM_DASH not in message.text


def test_mutual_total_renders_from_the_custom_field(conn, lead):
    set_custom_field(conn, lead.id, "mutual_total", "14")
    body = "{IF mutualTotal}We share {mutualTotal} connections.{ELSE}New to me.{END}"
    assert render_for_lead(conn, body, lead).text == "We share 14 connections."


def test_mutual_total_falls_back_when_absent(conn, lead):
    body = "{IF mutualTotal}We share {mutualTotal} connections.{ELSE}New to me.{END}"
    assert render_for_lead(conn, body, lead).text == "New to me."


def test_mutual_total_unguarded_and_absent_refuses(conn, lead):
    result = safe_render_for_lead(conn, "We share {mutualTotal} connections.", lead)
    assert not result.ok
    assert result.refusal.detail["token"] == "mutualTotal"


def test_render_for_lead_passes_extras_through(conn, lead):
    body = "We share {mutualTotal} connections."
    assert render_for_lead(conn, body, lead, extras={"mutualTotal": 9}).text == (
        "We share 9 connections."
    )


def test_render_for_lead_supports_the_ai_seam(conn, lead):
    body = "{ai_opener} How is {company} approaching this?"
    message = render_for_lead(
        conn, body, lead, fragments={"opener": "Saw your eval harness post."}
    )
    assert message.text == (
        "Saw your eval harness post. How is Microsoft approaching this?"
    )
    assert message.fragments_used == ("opener",)


def test_render_for_lead_enforces_a_connection_note_limit(conn, lead):
    body = "Hi {firstName}, " + ("a" * 300)
    result = safe_render_for_lead(conn, body, lead, max_chars=300)
    assert not result.ok
    assert result.refusal.reason is RenderRefusalReason.TOO_LONG


# --------------------------------------------------------------------------
# Storing templates
# --------------------------------------------------------------------------


def test_create_and_read_back_a_template(conn, account):
    created = create_template(
        conn,
        account,
        "invite",
        "Hi {firstName}, saw your work at {company}.",
        variations=["Hello {firstName}, nice work at {company}."],
    )
    assert created.id is not None
    assert created.account_id == account
    assert created.kind == "static"
    assert created.variations == ("Hello {firstName}, nice work at {company}.",)
    assert created.is_ai_generated is False
    assert created.created_at

    fetched = get_template(conn, created.id)
    assert fetched == created


def test_template_bodies_puts_the_body_first(conn, account):
    created = create_template(conn, account, "t", "A", variations=["B", "C"])
    assert created.bodies() == ("A", "B", "C")


def test_get_template_by_name(conn, account):
    created = create_template(conn, account, "invite", "Hi there.")
    assert get_template_by_name(conn, account, "invite") == created
    assert get_template_by_name(conn, account, "missing") is None


def test_template_names_are_trimmed(conn, account):
    created = create_template(conn, account, "  invite  ", "Hi there.")
    assert created.name == "invite"


def test_blank_template_name_is_rejected(conn, account):
    with pytest.raises(ValueError):
        create_template(conn, account, "   ", "Hi there.")


def test_template_names_are_unique_per_account(conn, account):
    create_template(conn, account, "invite", "Hi there.")
    with pytest.raises(sqlite3.IntegrityError):
        create_template(conn, account, "invite", "Hello again.")


def test_two_accounts_can_share_a_template_name(conn, account):
    other = create_account(conn, "secondary")
    create_template(conn, account, "invite", "Hi there.")
    created = create_template(conn, other, "invite", "Hi there.")
    assert created.account_id == other


def test_require_template_resolves_by_id_and_name(conn, account):
    created = create_template(conn, account, "invite", "Hi there.")
    assert require_template(conn, created.id) == created
    assert require_template(conn, "invite", account_id=account) == created


def test_require_template_raises_for_a_missing_row(conn, account):
    with pytest.raises(TemplateNotFoundError):
        require_template(conn, 999)
    with pytest.raises(TemplateNotFoundError):
        require_template(conn, "nope", account_id=account)


def test_require_template_by_name_needs_an_account(conn):
    with pytest.raises(ValueError):
        require_template(conn, "invite")


def test_list_and_count_templates(conn, account):
    other = create_account(conn, "secondary")
    create_template(conn, account, "one", "Hi.")
    create_template(conn, account, "two", "Hello. {ai_closer}", kind="hybrid")
    create_template(conn, other, "three", "Hey.")

    assert [template.name for template in list_templates(conn, account)] == [
        "one",
        "two",
    ]
    assert count_templates(conn, account) == 2
    assert count_templates(conn, other) == 1


def test_list_templates_filters_by_kind(conn, account):
    create_template(conn, account, "one", "Hi.")
    create_template(conn, account, "two", "Hello. {ai_closer}", kind="hybrid")
    assert [t.name for t in list_templates(conn, account, kind="hybrid")] == ["two"]
    assert [t.name for t in list_templates(conn, account, kind="static")] == ["one"]


def test_list_templates_rejects_an_unknown_kind(conn, account):
    with pytest.raises(ValueError):
        list_templates(conn, account, kind="magic")


def test_list_templates_paginates(conn, account):
    for index in range(5):
        create_template(conn, account, f"t{index}", "Hi.")
    page = list_templates(conn, account, limit=2, offset=2)
    assert [template.name for template in page] == ["t2", "t3"]


def test_delete_template(conn, account):
    created = create_template(conn, account, "invite", "Hi there.")
    assert delete_template(conn, created.id) is True
    assert get_template(conn, created.id) is None
    assert delete_template(conn, created.id) is False


def test_deleting_an_account_cascades_to_its_templates(conn, account):
    create_template(conn, account, "invite", "Hi there.")
    conn.execute("DELETE FROM accounts WHERE id = ?", (account,))
    conn.commit()
    assert count_templates(conn, account) == 0


def test_ai_spec_round_trips(conn, account):
    spec = {"fragments": {"opener": {"prompt": "Reference their latest post"}}}
    created = create_template(
        conn,
        account,
        "hybrid",
        "{ai_opener} Rest of it.",
        kind="hybrid",
        ai_spec=spec,
        is_ai_generated=True,
    )
    fetched = get_template(conn, created.id)
    assert fetched.ai_spec == spec
    assert fetched.is_ai_generated is True
    assert fetched.uses_ai is True


def test_required_fragments_are_derivable_from_a_stored_template(conn, account):
    created = create_template(
        conn,
        account,
        "hybrid",
        "{ai_opener} Rest. {ai_closer}",
        kind="hybrid",
    )
    programs = compile_template(created)
    assert programs[0].fragments() == ("closer", "opener")


# --------------------------------------------------------------------------
# Validation happens before anything is stored
# --------------------------------------------------------------------------


def test_unparseable_body_is_never_stored(conn, account):
    with pytest.raises(TemplateSyntaxError):
        create_template(conn, account, "broken", "Hi {firstName")
    assert count_templates(conn, account) == 0


def test_unknown_token_is_never_stored(conn, account):
    with pytest.raises(TemplateSyntaxError):
        create_template(conn, account, "broken", "Hi {nickname}.")
    assert count_templates(conn, account) == 0


def test_em_dash_is_never_stored(conn, account):
    with pytest.raises(TemplateStyleError):
        create_template(conn, account, "dashy", f"Hi there {EM_DASH} hello.")
    assert count_templates(conn, account) == 0


def test_em_dash_in_a_variation_is_never_stored(conn, account):
    with pytest.raises(TemplateStyleError):
        create_template(
            conn,
            account,
            "dashy",
            "Hi there.",
            variations=[f"Hello {EM_DASH} hi."],
        )
    assert count_templates(conn, account) == 0


def test_filler_opener_is_never_stored(conn, account):
    with pytest.raises(TemplateStyleError):
        create_template(conn, account, "filler", "In today's world, we all ship AI.")
    assert count_templates(conn, account) == 0


def test_static_kind_with_an_ai_token_is_never_stored(conn, account):
    with pytest.raises(TemplateSyntaxError):
        create_template(conn, account, "mixed", "{ai_opener} Rest.", kind="static")
    assert count_templates(conn, account) == 0


def test_hybrid_kind_without_an_ai_token_is_never_stored(conn, account):
    with pytest.raises(TemplateSyntaxError):
        create_template(conn, account, "hollow", "Just text.", kind="hybrid")
    assert count_templates(conn, account) == 0


def test_unknown_kind_is_rejected_before_the_check_constraint(conn, account):
    with pytest.raises(ValueError):
        create_template(conn, account, "weird", "Hi.", kind="magic")


# --------------------------------------------------------------------------
# Updates revalidate the merged template
# --------------------------------------------------------------------------


def test_update_changes_the_body(conn, account):
    created = create_template(conn, account, "invite", "Hi there.")
    updated = update_template(conn, created.id, body="Hi {firstName}.")
    assert updated.body == "Hi {firstName}."
    assert updated.id == created.id


def test_update_with_no_fields_is_a_no_op(conn, account):
    created = create_template(conn, account, "invite", "Hi there.")
    assert update_template(conn, created.id) == created


def test_update_rejects_unknown_fields(conn, account):
    created = create_template(conn, account, "invite", "Hi there.")
    with pytest.raises(ValueError):
        update_template(conn, created.id, colour="blue")


def test_update_rejects_a_missing_template(conn):
    with pytest.raises(TemplateNotFoundError):
        update_template(conn, 999, body="Hi.")


def test_update_revalidates_the_new_body(conn, account):
    created = create_template(conn, account, "invite", "Hi there.")
    with pytest.raises(TemplateStyleError):
        update_template(conn, created.id, body=f"Hi {EM_DASH} there.")
    assert get_template(conn, created.id).body == "Hi there."


def test_update_catches_a_kind_change_that_orphans_a_fragment(conn, account):
    created = create_template(
        conn, account, "hybrid", "{ai_opener} Rest.", kind="hybrid"
    )
    with pytest.raises(TemplateSyntaxError):
        update_template(conn, created.id, kind="static")
    assert get_template(conn, created.id).kind == "hybrid"


def test_update_replaces_variations(conn, account):
    created = create_template(conn, account, "invite", "A", variations=["B"])
    updated = update_template(conn, created.id, variations=["C", "D"])
    assert updated.variations == ("C", "D")


# --------------------------------------------------------------------------
# End to end
# --------------------------------------------------------------------------


def test_a_stored_template_splits_evenly_across_a_lead_queue(conn, account):
    template = create_template(
        conn,
        account,
        "invite",
        "Hi {firstName}, saw your work at {company}.",
        variations=[
            "Hi {firstName}, nice work at {company}.",
            "Hi {firstName}, good things happening at {company}.",
        ],
    )
    leads = [
        create_lead(
            conn,
            account,
            f"Person {index}",
            first_name=f"Person{index}",
            organization_name="Contoso",
            public_id=f"person-{index}",
        )
        for index in range(99)
    ]

    counts = {0: 0, 1: 0, 2: 0}
    for sequence, person in enumerate(leads):
        message = render_for_lead(conn, template, person, sequence=sequence)
        counts[message.variation_index] += 1
        assert person.first_name in message.text
        assert EM_DASH not in message.text

    assert counts == {0: 33, 1: 33, 2: 33}


def test_a_queue_mixes_sent_messages_and_skipped_leads(conn, account):
    template = create_template(
        conn, account, "invite", "Hi {firstName}, quick question about {company}."
    )
    good = create_lead(
        conn,
        account,
        "Good Lead",
        first_name="Good",
        organization_name="Contoso",
        public_id="good",
    )
    incomplete = create_lead(
        conn, account, "Incomplete Lead", first_name="Incomplete", public_id="bad"
    )

    sent = safe_render_for_lead(conn, template, good, sequence=0)
    skipped = safe_render_for_lead(conn, template, incomplete, sequence=1)

    assert sent.ok
    assert sent.rendered.text == "Hi Good, quick question about Contoso."
    assert not skipped.ok
    assert skipped.sublist == SKIPPED_SUBLIST
    assert skipped.refusal.detail["token"] == "company"
    assert skipped.refusal.detail["lead_id"] == incomplete.id


def test_a_fragment_source_failure_is_not_reported_as_a_missing_lead(conn, lead):
    def broken(name):
        raise LookupError("draft store lookup failed")

    result = safe_render_for_lead(conn, "{ai_opener} Rest.", lead, fragments=broken)
    assert not result.ok
    assert result.refusal.reason is RenderRefusalReason.FRAGMENT_SOURCE_FAILED


def test_inline_template_defers_validation_to_the_renderer(conn, lead):
    template = inline_template("Hi {firstName")
    result = safe_render_for_lead(conn, template, lead)
    assert not result.ok
    assert result.refusal.reason is RenderRefusalReason.TEMPLATE_INVALID


def test_a_template_dataclass_can_be_rendered_without_a_row():
    template = Template(name="ad hoc", body="Hi {firstName}.")
    assert template.id is None
    assert render_template(template, {"firstName": "Sam"}).text == "Hi Sam."
