"""CSV import: loud per row, fatal only when the file itself is not a lead list.

Every fixture here is a file on disk written by the test, so nothing depends on
a network, a browser, or the order the tests run in. The messy file is the point
of the module: it carries a missing column, a blank row, a short row, a
duplicate and a blacklisted person all at once, because that is what a real
export from a conference platform actually looks like.
"""

import json

import pytest

from linkedin_mcp.core.db import initialize_database
from linkedin_mcp.leads import (
    blacklist_identity,
    count_leads,
    get_lead_by_public_id,
)
from linkedin_mcp.scrape import (
    SOURCE_CSV_IMPORT,
    CsvImportError,
    CsvImportSummary,
    RowReason,
    harvest_run,
    import_leads_from_csv,
)
from linkedin_mcp.scrape.csv_import import normalise_header

CLEAN = """profile_url,name,headline,company,location
https://www.linkedin.com/in/nived-velayudhan/,Nived Velayudhan,Solution Engineer,Microsoft,Bengaluru
https://www.linkedin.com/in/ada-lovelace/,Ada Lovelace,Analyst,Analytical Engine,London
https://www.linkedin.com/in/grace-hopper/,Grace Hopper,Rear Admiral,US Navy,Arlington
"""

MESSY = """profile_url,name,headline
https://www.linkedin.com/in/keeper-one/,Keeper One,Platform Engineer

https://www.linkedin.com/in/blocked-person/,Blocked Person,Sales
https://www.linkedin.com/in/keeper-one/,Keeper One Again,Platform Engineer
https://www.linkedin.com/in/short-row/,Short Row
,Nameless Nobody,No Profile At All
https://www.linkedin.com/in/too-many-cells/,Too Many Cells,SRE,extra,cells
https://www.linkedin.com/in/keeper-two/,Keeper Two,Data Engineer
https://www.linkedin.com/in/keeper-three/,Keeper Three,Analytics
"""

NO_IDENTITY_HEADER = """name,headline,company
Nived Velayudhan,Solution Engineer,Microsoft
"""

ALIASED = """LinkedIn URL,Full Name,Job Title,Organisation,City,Degree
https://www.linkedin.com/in/alias-one/,Alias One,Staff Engineer,Contoso,Dublin,2nd
"""


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


def write(tmp_path, text: str, name: str = "leads.csv"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# --- The ordinary case ----------------------------------------------------


def test_a_clean_file_becomes_leads(conn, account, tmp_path):
    summary = import_leads_from_csv(conn, account, write(tmp_path, CLEAN))

    assert isinstance(summary, CsvImportSummary)
    assert summary.source == SOURCE_CSV_IMPORT
    assert summary.rows == 3
    assert summary.imported == 3
    assert summary.skipped == 0
    assert summary.refused == 0
    assert summary.duplicates == 0
    assert summary.leads_created == 3
    assert count_leads(conn, account) == 3

    stored = get_lead_by_public_id(conn, account, "nived-velayudhan")
    assert stored.full_name == "Nived Velayudhan"
    assert stored.headline == "Solution Engineer"
    assert stored.organization_name == "Microsoft"
    assert stored.location_name == "Bengaluru"


def test_the_import_is_recorded_as_a_harvest_run(conn, account, tmp_path):
    summary = import_leads_from_csv(conn, account, write(tmp_path, CLEAN))

    stored = harvest_run(conn, summary.harvest_run_id)
    assert stored["source_type"] == SOURCE_CSV_IMPORT
    assert stored["found_count"] == 3
    assert stored["new_count"] == 3
    assert stored["finished_at"] is not None


def test_header_spellings_a_real_export_uses_are_understood(conn, account, tmp_path):
    summary = import_leads_from_csv(conn, account, write(tmp_path, ALIASED))

    assert summary.imported == 1
    stored = get_lead_by_public_id(conn, account, "alias-one")
    assert stored.full_name == "Alias One"
    assert stored.organization_title == "Staff Engineer"
    assert stored.organization_name == "Contoso"
    assert stored.location_name == "Dublin"
    assert stored.member_distance == "2nd"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Profile URL", "profile_url"),
        ("  LinkedIn-URL  ", "linkedin_url"),
        ("\ufeffprofile_url", "profile_url"),
        ("First  Name", "first_name"),
        ("", ""),
    ],
)
def test_a_header_cell_folds_to_a_comparable_key(raw, expected):
    assert normalise_header(raw) == expected


def test_a_path_a_string_and_an_open_file_all_work(conn, account, tmp_path):
    path = write(tmp_path, CLEAN)

    from_path = import_leads_from_csv(conn, account, path)
    from_string = import_leads_from_csv(conn, account, str(path))
    with path.open(encoding="utf-8") as handle:
        from_handle = import_leads_from_csv(conn, account, handle)

    assert from_path.imported == from_string.imported == from_handle.imported == 3
    assert count_leads(conn, account) == 3, "three imports of one file is three leads"


# --- The messy case -------------------------------------------------------


def messy_summary(conn, account, tmp_path):
    blacklist_identity(conn, account, public_id="blocked-person", reason="asked to stop")
    return import_leads_from_csv(conn, account, write(tmp_path, MESSY, "messy.csv"))


def test_a_messy_file_imports_the_good_rows_and_reports_the_rest(
    conn, account, tmp_path
):
    summary = messy_summary(conn, account, tmp_path)

    assert summary.rows == 9
    assert summary.imported == 3, "keeper one, two and three"
    assert summary.skipped == 4, "the blank, the short, the nameless and the overlong"
    assert summary.refused == 1, "the blacklisted person"
    assert summary.duplicates == 1, "keeper one, listed twice"
    assert summary.balanced


def test_every_declined_row_is_reported_with_its_line_number(conn, account, tmp_path):
    summary = messy_summary(conn, account, tmp_path)

    reported = {problem.row: problem.reason for problem in summary.problems}
    assert reported == {
        3: RowReason.BLANK_ROW.value,
        4: RowReason.BLACKLISTED.value,
        6: RowReason.COLUMN_COUNT.value,
        7: RowReason.NO_IDENTITY.value,
        8: RowReason.COLUMN_COUNT.value,
    }
    assert summary.duplicate_rows == (5,)
    assert [problem.row for problem in summary.problems] == sorted(reported)


def test_a_blacklisted_row_is_refused_rather_than_stored(conn, account, tmp_path):
    summary = messy_summary(conn, account, tmp_path)

    assert get_lead_by_public_id(conn, account, "blocked-person") is None
    refusals = [
        problem
        for problem in summary.problems
        if problem.reason == RowReason.BLACKLISTED.value
    ]
    assert len(refusals) == 1
    assert refusals[0].public_id == "blocked-person"
    assert "do-not-contact" in refusals[0].message


def test_a_duplicate_row_produces_one_lead_not_two(conn, account, tmp_path):
    messy_summary(conn, account, tmp_path)

    assert count_leads(conn, account) == 3
    stored = get_lead_by_public_id(conn, account, "keeper-one")
    assert stored is not None
    assert stored.full_name == "Keeper One", "the first sighting wins the name"


def test_a_short_row_is_skipped_without_costing_the_rows_after_it(
    conn, account, tmp_path
):
    summary = messy_summary(conn, account, tmp_path)

    short = {
        problem.row: problem.message
        for problem in summary.problems
        if problem.reason == RowReason.COLUMN_COUNT.value
    }
    assert set(short) == {6, 8}
    assert short[6] == "the row has 2 cells but the header has 3"
    assert short[8] == "the row has 5 cells but the header has 3"
    assert get_lead_by_public_id(conn, account, "keeper-three") is not None


def test_the_counts_add_back_up_to_the_rows_that_were_read(conn, account, tmp_path):
    summary = messy_summary(conn, account, tmp_path)

    assert summary.balanced
    assert (
        summary.imported
        == summary.leads_created + summary.leads_updated + summary.leads_unchanged
    )


# --- Fatal files ----------------------------------------------------------


def test_a_header_with_nothing_that_identifies_a_person_raises(
    conn, account, tmp_path
):
    with pytest.raises(CsvImportError) as error:
        import_leads_from_csv(conn, account, write(tmp_path, NO_IDENTITY_HEADER))

    assert "identifies a LinkedIn person" in str(error.value)
    assert count_leads(conn, account) == 0


def test_an_empty_file_raises_rather_than_reporting_a_successful_no_op(
    conn, account, tmp_path
):
    with pytest.raises(CsvImportError):
        import_leads_from_csv(conn, account, write(tmp_path, ""))


def test_a_file_that_is_not_there_raises_with_the_path_in_the_message(
    conn, account, tmp_path
):
    with pytest.raises(CsvImportError) as error:
        import_leads_from_csv(conn, account, tmp_path / "absent.csv")

    assert "absent.csv" in str(error.value)


def test_something_that_is_not_a_file_at_all_raises(conn, account):
    with pytest.raises(CsvImportError):
        import_leads_from_csv(conn, account, 12345)


def test_a_batch_size_below_one_is_refused(conn, account, tmp_path):
    with pytest.raises(ValueError):
        import_leads_from_csv(
            conn, account, write(tmp_path, CLEAN), batch_size=0
        )


# --- Dedupe against the other sources -------------------------------------


def test_a_person_already_harvested_from_linkedin_is_updated_not_duplicated(
    conn, account, tmp_path
):
    first = import_leads_from_csv(conn, account, write(tmp_path, CLEAN))
    richer = write(
        tmp_path,
        "profile_url,name,headline\n"
        "https://www.linkedin.com/in/nived-velayudhan/,Nived Velayudhan,"
        "Solution Engineer at Microsoft\n",
        "richer.csv",
    )

    second = import_leads_from_csv(conn, account, richer)

    assert first.leads_created == 3
    assert second.leads_created == 0
    assert second.leads_updated == 1
    assert count_leads(conn, account) == 3
    stored = get_lead_by_public_id(conn, account, "nived-velayudhan")
    assert stored.headline == "Solution Engineer at Microsoft"


def test_a_member_urn_column_resolves_onto_the_same_person(conn, account, tmp_path):
    by_urn = write(
        tmp_path,
        "member_id,name\nurn:li:member:5551212,Urn Person\n",
        "urn.csv",
    )
    by_digits = write(
        tmp_path,
        "member_id,name,headline\n5551212,Urn Person,Now With A Headline\n",
        "digits.csv",
    )

    first = import_leads_from_csv(conn, account, by_urn)
    second = import_leads_from_csv(conn, account, by_digits)

    assert first.leads_created == 1
    assert second.leads_created == 0
    assert second.leads_updated == 1
    assert count_leads(conn, account) == 1


# --- Dry run --------------------------------------------------------------


def test_a_dry_run_validates_the_file_without_writing_anything(
    conn, account, tmp_path
):
    blacklist_identity(conn, account, public_id="blocked-person", reason="asked to stop")

    summary = import_leads_from_csv(
        conn, account, write(tmp_path, MESSY, "messy.csv"), harvest=False
    )

    assert summary.dry_run
    assert summary.imported == 3, "the rows that would have been stored"
    assert summary.skipped == 4
    assert summary.refused == 1
    assert summary.balanced
    assert summary.leads_created == 0
    assert count_leads(conn, account) == 0
    assert conn.execute("SELECT COUNT(*) FROM harvest_runs").fetchone()[0] == 0


def test_the_summary_serialises_for_an_mcp_tool_result(conn, account, tmp_path):
    summary = messy_summary(conn, account, tmp_path)

    payload = summary.as_dict()

    assert payload["status"] == "success"
    assert payload["source"] == SOURCE_CSV_IMPORT
    assert payload["imported"] == 3
    assert payload["skipped"] == 4
    assert payload["refused"] == 1
    assert payload["duplicates"] == 1
    assert payload["duplicate_rows"] == [5]
    assert {problem["reason"] for problem in payload["problems"]} == {
        RowReason.BLANK_ROW.value,
        RowReason.BLACKLISTED.value,
        RowReason.COLUMN_COUNT.value,
        RowReason.NO_IDENTITY.value,
    }
    assert json.loads(json.dumps(payload)) == payload
