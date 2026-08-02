"""CSV import for lists that came from somewhere other than LinkedIn.

Conference exports, webinar registrations, a spreadsheet somebody kept by hand.
These arrive as files, and a file is the one lead source in SCRAPE-04 that makes
no LinkedIn request at all.

Which is why nothing here asks the safety gate. `SafetyGate` meters what an
account does to LinkedIn; reading a local file does nothing to LinkedIn, and
gating it would spend a browsing budget on an action LinkedIn never sees. Every
other rule still applies in full: rows go through DB-02's `harvest_leads` by way
of `harvest_people`, so DB-03 resolves them onto existing leads and the global
blacklist refuses anyone on it, exactly as it does for a post's likers.

Failing loudly, one row at a time
---------------------------------
A bad row is reported and skipped. A bad file raises. The distinction matters:
one unparseable line out of four hundred should not cost the other 399, but a
file whose header carries no identity column at all is not a list of leads, and
importing nothing from it silently would be the worst outcome available.

Every row is accounted for by number and reason. The four outcomes are exclusive
and :attr:`CsvImportSummary.balanced` checks they add back up to the rows read,
so a row cannot go missing between the file and the summary.
"""

from __future__ import annotations

import csv
import io
import logging
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from linkedin_mcp.leads import (
    HarvestSummary,
    is_blacklisted_by_member_id,
    is_blacklisted_by_public_id,
)
from linkedin_mcp.scrape.harvest import harvest_people, stale_lead_ids
from linkedin_mcp.scrape.records import (
    PersonResult,
    member_urn_from,
    name_from_slug,
    parse_distance,
    public_id_from,
)
from linkedin_mcp.scrape.runs import finish_harvest_run, start_harvest_run
from linkedin_mcp.scrape.sources import SOURCE_CSV_IMPORT
from linkedin_mcp.scrape.summary import merge_harvest

logger = logging.getLogger(__name__)

__all__ = [
    "COLUMN_ALIASES",
    "DEFAULT_BATCH_SIZE",
    "IDENTITY_COLUMNS",
    "SOURCE_CSV_IMPORT",
    "CsvImportError",
    "CsvImportSummary",
    "CsvRowProblem",
    "RowReason",
    "import_leads_from_csv",
    "normalise_header",
]

DEFAULT_BATCH_SIZE = 200
"""Rows handed to the lead store at once, so a huge file is not one statement."""

IDENTITY_COLUMNS = ("profile_url", "public_id", "member_id")
"""A file with none of these is not a list of LinkedIn people."""

COLUMN_ALIASES: Mapping[str, str] = {
    "profile_url": "profile_url",
    "profile": "profile_url",
    "profile_link": "profile_url",
    "linkedin_url": "profile_url",
    "linkedin": "profile_url",
    "linkedin_profile": "profile_url",
    "url": "profile_url",
    "link": "profile_url",
    "public_id": "public_id",
    "publicid": "public_id",
    "public_identifier": "public_id",
    "slug": "public_id",
    "vanity_name": "public_id",
    "username": "public_id",
    "member_id": "member_id",
    "memberid": "member_id",
    "member_urn": "member_id",
    "urn": "member_id",
    "full_name": "full_name",
    "fullname": "full_name",
    "name": "full_name",
    "display_name": "full_name",
    "first_name": "first_name",
    "firstname": "first_name",
    "given_name": "first_name",
    "last_name": "last_name",
    "lastname": "last_name",
    "surname": "last_name",
    "family_name": "last_name",
    "headline": "headline",
    "title": "headline",
    "job_title": "organization_title",
    "position": "organization_title",
    "organization_title": "organization_title",
    "location": "location_name",
    "location_name": "location_name",
    "city": "location_name",
    "company": "organization_name",
    "company_name": "organization_name",
    "organisation": "organization_name",
    "organization": "organization_name",
    "organization_name": "organization_name",
    "summary": "summary",
    "about": "summary",
    "avatar": "avatar_url",
    "avatar_url": "avatar_url",
    "photo": "avatar_url",
    "degree": "member_distance",
    "distance": "member_distance",
    "member_distance": "member_distance",
    "connection_degree": "member_distance",
}
"""Header spellings a real exported file uses, mapped onto lead fields.

Unrecognised columns are ignored rather than rejected. A file with a `notes`
column is still a perfectly good list of leads, and refusing it over a column
nobody asked about is the kind of strictness that makes an import tool useless.
"""


class RowReason(str, Enum):
    """Why one CSV row did not become a lead.

    The first three are the caller's file being untidy and count as skipped.
    Anything else is a refusal, which means the row named a real person the
    database declined to store.
    """

    BLANK_ROW = "blank_row"
    COLUMN_COUNT = "column_count"
    NO_IDENTITY = "no_identity"
    BLACKLISTED = "blacklisted"


SKIP_REASONS = frozenset(
    {
        RowReason.BLANK_ROW.value,
        RowReason.COLUMN_COUNT.value,
        RowReason.NO_IDENTITY.value,
    }
)
"""Problems that mean the row was unusable, rather than the person refused."""


class CsvImportError(ValueError):
    """The file as a whole cannot be imported."""


@dataclass(frozen=True, slots=True)
class CsvRowProblem:
    """One row the import declined, kept so nothing fails silently."""

    row: int
    reason: str
    message: str
    member_id: str | None = None
    public_id: str | None = None

    @property
    def skipped(self) -> bool:
        """True when the row was unusable rather than the person refused."""
        return self.reason in SKIP_REASONS

    def as_dict(self) -> dict[str, Any]:
        return {
            "row": self.row,
            "reason": self.reason,
            "message": self.message,
            "member_id": self.member_id,
            "public_id": self.public_id,
        }


@dataclass(frozen=True, slots=True)
class CsvImportSummary:
    """What one CSV import read, stored and declined.

    `imported` counts rows that were accepted. On a real import that means they
    reached the database, and `leads_created` plus `leads_updated` plus
    `leads_unchanged` add up to it. On a dry run it means they passed validation
    and would have been stored, and `dry_run` says so, because a caller reading
    only the counts should never have to guess whether anything was written.
    """

    source: str = SOURCE_CSV_IMPORT
    path: str | None = None
    columns: tuple[str, ...] = ()
    rows: int = 0
    imported: int = 0
    skipped: int = 0
    refused: int = 0
    duplicates: int = 0
    duplicate_rows: tuple[int, ...] = ()
    dry_run: bool = False
    harvest: HarvestSummary = field(default_factory=HarvestSummary)
    problems: tuple[CsvRowProblem, ...] = ()
    harvest_run_id: int | None = None
    stale_lead_ids: tuple[int, ...] = ()

    @property
    def leads_created(self) -> int:
        return self.harvest.created

    @property
    def leads_updated(self) -> int:
        return self.harvest.updated

    @property
    def leads_unchanged(self) -> int:
        return self.harvest.unchanged

    @property
    def lead_ids(self) -> tuple[int, ...]:
        return self.harvest.lead_ids

    @property
    def balanced(self) -> bool:
        """True when every row read is accounted for by exactly one outcome."""
        return (
            self.rows
            == self.imported + self.skipped + self.refused + self.duplicates
        )

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON friendly payload, suitable for an MCP tool result."""
        return {
            "status": "success",
            "source": self.source,
            "path": self.path,
            "columns": list(self.columns),
            "rows": self.rows,
            "imported": self.imported,
            "skipped": self.skipped,
            "refused": self.refused,
            "duplicates": self.duplicates,
            "duplicate_rows": list(self.duplicate_rows),
            "dry_run": self.dry_run,
            "leads_created": self.leads_created,
            "leads_updated": self.leads_updated,
            "leads_unchanged": self.leads_unchanged,
            "lead_ids": list(self.lead_ids),
            "problems": [problem.as_dict() for problem in self.problems],
            "harvest_run_id": self.harvest_run_id,
            "stale_lead_ids": list(self.stale_lead_ids),
        }


def normalise_header(name: str | None) -> str:
    """Fold one header cell into a comparable key."""
    text = (name or "").lstrip("\ufeff").strip().lower()
    for character in (" ", "-", ".", "/"):
        text = text.replace(character, "_")
    while "__" in text:
        text = text.replace("__", "_")
    return text.strip("_")


def _map_columns(header: Sequence[str]) -> tuple[list[str], tuple[str, ...]]:
    """Return the canonical field per column, and the recognised field names."""
    mapped = [COLUMN_ALIASES.get(normalise_header(cell), "") for cell in header]
    recognised = tuple(dict.fromkeys(name for name in mapped if name))
    return mapped, recognised


def _cell(values: Mapping[str, str], name: str) -> str | None:
    value = (values.get(name) or "").strip()
    return value or None


def _person_from_row(values: Mapping[str, str]) -> PersonResult | None:
    """Build a person from one mapped row, or None when it has no identity."""
    profile_url = _cell(values, "profile_url")
    public_id = public_id_from(profile_url) or _cell(values, "public_id")
    raw_member = _cell(values, "member_id")
    member_id = member_urn_from(raw_member) or member_urn_from(profile_url)
    if member_id is None and raw_member and raw_member.isdigit():
        member_id = f"urn:li:member:{raw_member}"

    if not public_id and not member_id:
        return None

    full_name = _cell(values, "full_name")
    if not full_name:
        parts = [_cell(values, "first_name"), _cell(values, "last_name")]
        full_name = " ".join(part for part in parts if part) or None
    if not full_name:
        # A row with an identity and no name is still a person. The slug reads
        # as a label until a real sighting overwrites it, which is the same
        # thing `extract.py` does for a search card whose name did not render.
        full_name = name_from_slug(public_id) if public_id else member_id
    if not full_name:
        return None

    return PersonResult(
        full_name=full_name,
        public_id=public_id,
        member_id=member_id,
        headline=_cell(values, "headline"),
        location_name=_cell(values, "location_name"),
        organization_name=_cell(values, "organization_name"),
        organization_title=_cell(values, "organization_title"),
        member_distance=parse_distance(_cell(values, "member_distance")),
        avatar_url=_cell(values, "avatar_url"),
        summary=_cell(values, "summary"),
        profile_url=profile_url,
    )


def _read_rows(
    source: Any, encoding: str, delimiter: str
) -> tuple[list[list[str]], str | None]:
    """Read a CSV into rows, from a path or an open file."""
    if hasattr(source, "read"):
        text = source.read()
        label = getattr(source, "name", None)
    elif isinstance(source, (str, Path)):
        path = Path(source)
        try:
            text = path.read_text(encoding=encoding)
        except OSError as error:
            raise CsvImportError(f"cannot read {path}: {error}") from error
        label = str(path)
    else:
        raise CsvImportError(
            f"{source!r} is not a CSV path or an open file. Give a path, a "
            "Path, or a file object."
        )
    if isinstance(text, bytes):
        text = text.decode(encoding)
    try:
        rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    except csv.Error as error:
        raise CsvImportError(f"the CSV could not be parsed: {error}") from error
    return rows, label


def import_leads_from_csv(
    conn: sqlite3.Connection,
    account_id: int,
    source: Any,
    *,
    encoding: str = "utf-8-sig",
    delimiter: str = ",",
    fetched_at: datetime | str | None = None,
    harvest: bool = True,
    run_id: int | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    clock: Any = None,
) -> CsvImportSummary:
    """Import an externally sourced list of people as leads.

    Args:
        conn: Open connection to the MCP database.
        account_id: Account the import belongs to.
        source: CSV path, `Path`, or an open text file object.
        encoding: File encoding. Defaults to UTF-8 with an optional BOM,
            because that is what a spreadsheet export actually produces.
        delimiter: Field separator, for the exports that use semicolons.
        fetched_at: When the rows were observed. Defaults to now.
        harvest: Store rows through the lead store. Off for a dry run that
            still reports every row it would have declined.
        run_id: Existing `harvest_runs` row to record this import against.
        batch_size: Rows handed to the lead store at once.
        clock: Decision time source, injected so a caller stays deterministic.

    Raises:
        CsvImportError: The file is unreadable, empty, or carries no column
            that could identify a LinkedIn person.
    """
    if batch_size < 1:
        raise ValueError(f"batch size must be at least 1, got {batch_size}")

    tick = clock or (lambda: datetime.now(timezone.utc))
    stamp = fetched_at if fetched_at is not None else tick()

    rows, label = _read_rows(source, encoding, delimiter)
    if not rows:
        raise CsvImportError("the CSV is empty, so there is no header to read")

    header = rows[0]
    mapped, recognised = _map_columns(header)
    if not any(name in recognised for name in IDENTITY_COLUMNS):
        raise CsvImportError(
            "the CSV header has no column that identifies a LinkedIn person. "
            f"Expected one of {list(IDENTITY_COLUMNS)}, or an alias such as "
            f"'linkedin_url' or 'slug'. Got {list(header)}."
        )

    problems: list[CsvRowProblem] = []
    duplicate_rows: list[int] = []
    people: list[PersonResult] = []
    row_of: list[int] = []
    seen: set[str] = set()
    read = 0

    for number, cells in enumerate(rows[1:], start=2):
        read += 1

        if not cells or all(not (cell or "").strip() for cell in cells):
            # A blank line is padding rather than corruption. It still gets a
            # row number and a reason, because a file with forty of them is
            # telling you something about where it came from.
            problems.append(
                CsvRowProblem(number, RowReason.BLANK_ROW.value, "the row is empty")
            )
            continue

        if len(cells) != len(header):
            problems.append(
                CsvRowProblem(
                    number,
                    RowReason.COLUMN_COUNT.value,
                    f"the row has {len(cells)} cells but the header has {len(header)}",
                )
            )
            continue

        values: dict[str, str] = {}
        for field_name, cell in zip(mapped, cells):
            if field_name and not values.get(field_name):
                values[field_name] = cell

        person = _person_from_row(values)
        if person is None:
            problems.append(
                CsvRowProblem(
                    number,
                    RowReason.NO_IDENTITY.value,
                    "the row has no usable profile URL, public id or member id",
                )
            )
            continue

        if is_blacklisted_by_member_id(
            conn, person.member_id
        ) or is_blacklisted_by_public_id(conn, person.public_id):
            # `harvest_leads` refuses this too, and it is the authority. Testing
            # it here is what attaches the CSV row number to the refusal, which
            # is the only thing that makes a blacklisted row findable in a file
            # of four hundred.
            problems.append(
                CsvRowProblem(
                    number,
                    RowReason.BLACKLISTED.value,
                    "the person is on the global do-not-contact list",
                    member_id=person.member_id,
                    public_id=person.public_id,
                )
            )
            continue

        if person.dedupe_key in seen:
            duplicate_rows.append(number)
            continue

        seen.add(person.dedupe_key)
        people.append(person)
        row_of.append(number)

    run_params = {"path": label, "columns": list(recognised), "rows": read}
    if harvest and run_id is None:
        run_id = start_harvest_run(
            conn,
            account_id,
            SOURCE_CSV_IMPORT,
            run_params,
            started_at=tick(),
        )

    totals = HarvestSummary()
    if harvest:
        for offset in range(0, len(people), batch_size):
            batch = people[offset : offset + batch_size]
            batch_summary = harvest_people(conn, account_id, batch, fetched_at=stamp)
            for refusal in batch_summary.refusals:
                problems.append(
                    CsvRowProblem(
                        row_of[offset + refusal.index],
                        refusal.reason,
                        refusal.message,
                        member_id=refusal.member_id,
                        public_id=refusal.public_id,
                    )
                )
            totals = merge_harvest(totals, batch_summary)

    skipped = sum(1 for problem in problems if problem.skipped)
    refused = len(problems) - skipped
    imported = read - skipped - refused - len(duplicate_rows)

    stale = (
        stale_lead_ids(conn, account_id, totals.lead_ids, now=tick())
        if harvest
        else ()
    )

    if harvest and run_id is not None:
        finish_harvest_run(
            conn,
            run_id,
            found=read,
            new=totals.created,
            params=run_params,
            finished_at=tick(),
        )

    summary = CsvImportSummary(
        path=label,
        columns=recognised,
        rows=read,
        imported=imported,
        skipped=skipped,
        refused=refused,
        duplicates=len(duplicate_rows),
        duplicate_rows=tuple(duplicate_rows),
        dry_run=not harvest,
        harvest=totals,
        problems=tuple(sorted(problems, key=lambda problem: problem.row)),
        harvest_run_id=run_id,
        stale_lead_ids=stale,
    )

    logger.info(
        "CSV import of %s read %d row(s): %d imported, %d skipped, %d refused, "
        "%d duplicate",
        label,
        summary.rows,
        summary.imported,
        summary.skipped,
        summary.refused,
        summary.duplicates,
    )
    return summary
