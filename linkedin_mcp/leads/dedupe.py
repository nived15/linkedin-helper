"""Deduplication and merge across repeated harvests.

A person can surface many times: once per search page, again from a post's
engagers, again from a deep profile scrape. Every sighting lands here, and this
module decides whether it is a new lead or a fresh look at a stored one.

Matching
--------
``member_id`` is LinkedIn's durable identifier and wins every tie. ``public_id``
is the vanity URL slug: it changes when someone edits their profile URL and
LinkedIn hands the freed slug to somebody else, so it is treated as the current
label for a person rather than proof of who they are. Matching only ever looks
at the identifiers a row holds right now. A released ``public_id`` is archived
in ``lead_identity_history``, and matching deliberately ignores that archive,
because a slug in there may since have been recycled onto a different person.

No lead row is ever deleted or folded into another one. ``actions_log`` is
append-only and its ``lead_id`` is ``ON DELETE SET NULL``, so removing a row to
tidy up a collision would silently strip the audit trail off every action ever
taken on that person. A collision is resolved by moving the contested
identifier, never by removing the row that used to hold it, and the old value
is archived so nothing the database already knew is lost.

Merge rules, by field
---------------------
``member_id``
    Write once. Filled when the stored row has none, never overwritten with a
    different value: a different member id is a different person.
``public_id``, ``hash_id``
    Newest sighting wins. The displaced value is archived.
``full_name``, ``first_name``, ``last_name``, ``headline``, ``summary``,
``organization_name``, ``organization_title``, ``location_name``,
``member_distance``, ``avatar_url``, ``connection_count``, ``follower_count``
    Newest non-empty sighting wins. A missing or blank incoming value never
    erases what is stored, because a search-result row carries far less than a
    profile scrape and must not blank out the richer record.
``connected_at``
    Filled when absent, then left alone. When you connected is history, not an
    observation to refresh.
``badges``
    Merged key by key, with incoming keys winning. Badges arrive from different
    surfaces, so an absent key means "not observed here" rather than "false".
    Use :func:`linkedin_mcp.leads.store.update_lead` to replace the set wholesale.
``last_visited_at``
    Moves forward only.
``first_seen_at``
    Never rewritten. It records the first sighting.

Blacklist
---------
Every row an upsert touches is checked against the global do-not-contact list,
along with the incoming identity. Refreshing a blacklisted lead is refused the
same way :func:`linkedin_mcp.leads.store.update_lead` refuses it, which also
stops a vanity-URL change from moving a lead out from under a ``public_id``
blacklist entry and quietly making it contactable again.

Cache windows
-------------
Contact info is considered fresh for 21 days and positions for 14, matching the
extraction budget the roadmap sets. :func:`needs_refresh` is the predicate the
SCRAPE-03 deep-scraper calls before spending a profile visit on a section it
already has.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any

from linkedin_mcp.leads.blacklist import (
    BLACKLIST_EXCLUSION_SQL,
    guard_identity,
    is_identity_blacklisted,
    normalise_identifier,
)
from linkedin_mcp.leads.errors import (
    LeadBlacklistedError,
    LeadIdentityConflictError,
    LeadNotFoundError,
    LeadStoreError,
)
from linkedin_mcp.leads.store import (
    COLUMN_FIELDS,
    Lead,
    get_lead,
    lead_from_row,
    normalise_lead_fields,
    require_full_name,
)


__all__ = [
    "CACHE_WINDOW_DAYS",
    "CONTACT_INFO_CACHE_DAYS",
    "FILL_WHEN_ABSENT_COLUMNS",
    "IDENTITY_COLUMNS",
    "MERGED_COLUMNS",
    "MONOTONIC_COLUMNS",
    "POSITIONS_CACHE_DAYS",
    "REFRESHED_COLUMNS",
    "SECTION_COLUMNS",
    "TIMESTAMP_FORMAT",
    "HarvestRefusal",
    "HarvestRefusalReason",
    "HarvestSummary",
    "IdentityChange",
    "LeadSection",
    "UpsertResult",
    "cache_window_days",
    "harvest_leads",
    "identity_history",
    "leads_needing_refresh",
    "mark_section_fetched",
    "merge_fields",
    "needs_refresh",
    "normalise_section",
    "section_fetched_at",
    "stale_sections",
    "upsert_lead",
    "utc_timestamp",
]

TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

CONTACT_INFO_CACHE_DAYS = 21
POSITIONS_CACHE_DAYS = 14


class LeadSection(str, Enum):
    """Profile sections whose freshness the extraction budget tracks."""

    CONTACT_INFO = "contact_info"
    POSITIONS = "positions"


CACHE_WINDOW_DAYS: Mapping[str, int] = MappingProxyType(
    {
        LeadSection.CONTACT_INFO.value: CONTACT_INFO_CACHE_DAYS,
        LeadSection.POSITIONS.value: POSITIONS_CACHE_DAYS,
    }
)

SECTION_COLUMNS: Mapping[str, str] = MappingProxyType(
    {
        LeadSection.CONTACT_INFO.value: "contact_info_fetched_at",
        LeadSection.POSITIONS.value: "positions_fetched_at",
    }
)

IDENTITY_COLUMNS: frozenset[str] = frozenset({"member_id", "public_id", "hash_id"})

REFRESHED_COLUMNS: frozenset[str] = frozenset(
    {
        "avatar_url",
        "connection_count",
        "first_name",
        "follower_count",
        "full_name",
        "hash_id",
        "headline",
        "last_name",
        "location_name",
        "member_distance",
        "organization_name",
        "organization_title",
        "public_id",
        "summary",
    }
)

FILL_WHEN_ABSENT_COLUMNS: frozenset[str] = frozenset({"connected_at", "member_id"})

MERGED_COLUMNS: frozenset[str] = frozenset({"badges_json"})

MONOTONIC_COLUMNS: frozenset[str] = frozenset({"last_visited_at"})


class HarvestRefusalReason(str, Enum):
    """Why a harvested profile did not reach the database."""

    BLACKLISTED = "blacklisted"
    IDENTITY_CONFLICT = "identity_conflict"
    INVALID_PROFILE = "invalid_profile"


@dataclass(frozen=True, slots=True)
class IdentityChange:
    """An identifier a lead used to hold, kept so a merge never loses one."""

    lead_id: int
    kind: str
    value: str
    replaced_by: str | None = None
    claimed_by_lead_id: int | None = None
    observed_at: str | None = None


@dataclass(frozen=True, slots=True)
class UpsertResult:
    """Outcome of resolving one sighting against the database."""

    lead: Lead
    created: bool
    changed_fields: tuple[str, ...] = ()
    sections_marked: tuple[str, ...] = ()
    released: tuple[IdentityChange, ...] = ()

    @property
    def updated(self) -> bool:
        """True when an existing lead changed, which is what a re-harvest reports."""
        return not self.created and bool(self.changed_fields)

    @property
    def unchanged(self) -> bool:
        """True when an existing lead was matched and had nothing to learn."""
        return not self.created and not self.changed_fields


@dataclass(frozen=True, slots=True)
class HarvestRefusal:
    """A profile the harvest declined, kept so nothing fails silently."""

    index: int
    reason: str
    message: str
    member_id: str | None = None
    public_id: str | None = None


@dataclass(frozen=True, slots=True)
class HarvestSummary:
    """Incremental counts for one harvest run."""

    found: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    lead_ids: tuple[int, ...] = ()
    refusals: tuple[HarvestRefusal, ...] = ()

    @property
    def matched(self) -> int:
        """Profiles that resolved onto a lead the database already had."""
        return self.updated + self.unchanged

    @property
    def refused(self) -> int:
        return len(self.refusals)


def utc_timestamp(moment: datetime | None = None) -> str:
    """Return a UTC timestamp string comparable with SQLite ``CURRENT_TIMESTAMP``."""
    if moment is None:
        moment = datetime.now(timezone.utc)
    elif moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc)
    return moment.strftime(TIMESTAMP_FORMAT)


def _as_timestamp(value: datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return utc_timestamp(value)
    text = str(value).strip()
    return text or None


def _parse_timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.strptime(text, TIMESTAMP_FORMAT)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def normalise_section(section: str | LeadSection) -> str:
    """Return the canonical name of a cached profile section."""
    if isinstance(section, LeadSection):
        return section.value

    cleaned = str(section or "").strip().lower()
    if cleaned not in SECTION_COLUMNS:
        known = ", ".join(sorted(SECTION_COLUMNS))
        raise ValueError(f"unknown lead section {section!r}; expected one of {known}")
    return cleaned


def cache_window_days(
    section: str | LeadSection,
    *,
    window_days: int | None = None,
) -> int:
    """Return how long a section stays fresh, honouring an explicit override."""
    if window_days is not None:
        if window_days < 0:
            raise ValueError("window_days must not be negative")
        return window_days
    return CACHE_WINDOW_DAYS[normalise_section(section)]


def _column_value(stored: Mapping[str, Any] | sqlite3.Row, column: str) -> Any:
    try:
        return stored[column]
    except (KeyError, IndexError):
        return None


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _decode_badges(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    try:
        decoded = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def merge_fields(
    stored: Mapping[str, Any] | sqlite3.Row,
    incoming: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the stored columns a sighting changes, keyed by storage column.

    ``incoming`` is a payload from
    :func:`linkedin_mcp.leads.store.normalise_lead_fields`. An empty result
    means the sighting taught the database nothing, which is what a repeat
    harvest of unchanged profiles produces.
    """
    changes: dict[str, Any] = {}

    for column, value in incoming.items():
        current = _column_value(stored, column)

        if column in MERGED_COLUMNS:
            merged = {**_decode_badges(current), **_decode_badges(value)}
            if merged != _decode_badges(current):
                changes[column] = json.dumps(merged, sort_keys=True)
            continue

        if column in MONOTONIC_COLUMNS:
            incoming_moment = _parse_timestamp(_as_timestamp(value))
            current_moment = _parse_timestamp(current)
            if incoming_moment is None:
                continue
            if current_moment is None or incoming_moment > current_moment:
                changes[column] = _as_timestamp(value)
            continue

        if column in FILL_WHEN_ABSENT_COLUMNS:
            if current is None and not _is_blank(value):
                changes[column] = value
            continue

        if _is_blank(value) or value == current:
            continue
        changes[column] = value

    return changes


def _harvest_payload(fields: Mapping[str, Any]) -> dict[str, Any]:
    payload = normalise_lead_fields(fields)
    for column, value in list(payload.items()):
        if isinstance(value, datetime):
            payload[column] = utc_timestamp(value)
    if "full_name" in payload:
        name = str(payload["full_name"] or "").strip()
        if name:
            payload["full_name"] = name
        else:
            del payload["full_name"]
    return payload


def _row_by_identifier(
    conn: sqlite3.Connection,
    account_id: int,
    column: str,
    value: str | None,
) -> sqlite3.Row | None:
    if value is None:
        return None
    return conn.execute(
        f"SELECT * FROM leads WHERE account_id = ? AND {column} = ?",
        (account_id, value),
    ).fetchone()


def _resolve(
    row_by_member: sqlite3.Row | None,
    row_by_public: sqlite3.Row | None,
    *,
    member_id: str | None,
    public_id: str | None,
) -> tuple[sqlite3.Row | None, sqlite3.Row | None]:
    """Pick the row a sighting belongs to and the row that must free a slug.

    A ``None`` survivor means the sighting is a new person. The second row, when
    present, is a different person whose vanity URL LinkedIn has since handed to
    the incoming member, so it gives up the slug and keeps everything else.
    """
    if row_by_public is None:
        return row_by_member, None
    if row_by_member is not None and row_by_member["id"] == row_by_public["id"]:
        return row_by_member, None
    if member_id is None:
        return row_by_public, None
    if row_by_member is None and row_by_public["member_id"] is None:
        return row_by_public, None

    if row_by_public["member_id"] is None:
        raise LeadIdentityConflictError(
            f"public_id {public_id!r} is held by lead {row_by_public['id']}, which has "
            f"no member_id, while member_id {member_id!r} is already lead "
            f"{row_by_member['id']}; resolve the two rows by hand rather than "
            "guessing which person owns the slug",
            kind="public_id",
            value=public_id,
            lead_id=row_by_member["id"],
            other_lead_id=row_by_public["id"],
        )

    return row_by_member, row_by_public


def _section_stamps(
    sections: Iterable[str | LeadSection],
    fetched_at: datetime | str | None,
) -> dict[str, str]:
    columns = {SECTION_COLUMNS[normalise_section(section)] for section in sections}
    if not columns:
        return {}
    stamp = _as_timestamp(fetched_at) or utc_timestamp()
    return {column: stamp for column in sorted(columns)}


def _guard_release(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    """Refuse to free a slug when losing it would unblock a blacklisted lead.

    The blacklist matches on the identifiers a lead holds right now, so taking
    one away can quietly turn a blocked lead back into a contactable one. A lead
    still blocked by its member id afterwards is safe to touch.
    """
    if not is_identity_blacklisted(
        conn,
        member_id=row["member_id"],
        public_id=row["public_id"],
    ):
        return
    if is_identity_blacklisted(conn, member_id=row["member_id"]):
        return
    raise LeadBlacklistedError(
        lead_id=row["id"],
        member_id=row["member_id"],
        public_id=row["public_id"],
    )


def _identity_change(row: sqlite3.Row | Mapping[str, Any]) -> IdentityChange:
    return IdentityChange(
        lead_id=row["lead_id"],
        kind=row["kind"],
        value=row["value"],
        replaced_by=row["replaced_by"],
        claimed_by_lead_id=row["claimed_by_lead_id"],
        observed_at=row["observed_at"],
    )


def upsert_lead(
    conn: sqlite3.Connection,
    account_id: int,
    *,
    sections_fetched: Sequence[str | LeadSection] = (),
    fetched_at: datetime | str | None = None,
    **fields: Any,
) -> UpsertResult:
    """Resolve one harvested profile onto a lead, creating or merging as needed.

    Repeat harvests never raise: the same profile seen twice returns the same
    lead with :attr:`UpsertResult.unchanged` set. ``sections_fetched`` records
    the cache windows in the same transaction as the profile write, so a crash
    cannot leave a section marked fresh with nothing stored behind it.

    Raises :class:`~linkedin_mcp.leads.errors.LeadBlacklistedError` when the
    incoming identity or any row the merge touches is on the global
    do-not-contact list, and
    :class:`~linkedin_mcp.leads.errors.LeadIdentityConflictError` when the
    identifiers cannot be resolved onto one person without losing one.
    """
    payload = _harvest_payload(fields)
    member_id = payload.get("member_id")
    public_id = payload.get("public_id")
    if member_id is None and public_id is None:
        raise ValueError(
            "an upsert needs a member_id or a public_id, otherwise the same "
            "person is stored twice on the next harvest"
        )

    guard_identity(conn, member_id=member_id, public_id=public_id)

    section_stamps = _section_stamps(sections_fetched, fetched_at)
    survivor, release_from = _resolve(
        _row_by_identifier(conn, account_id, "member_id", member_id),
        _row_by_identifier(conn, account_id, "public_id", public_id),
        member_id=member_id,
        public_id=public_id,
    )

    if survivor is None:
        payload["full_name"] = require_full_name(payload.get("full_name", ""))
    else:
        guard_identity(
            conn,
            lead_id=survivor["id"],
            member_id=survivor["member_id"],
            public_id=survivor["public_id"],
        )
    if release_from is not None:
        _guard_release(conn, release_from)

    changed: dict[str, Any] = {}
    archives: list[tuple[int, str, str, str | None, int | None]] = []
    displaced: list[tuple[int, str, str, str | None, int | None]] = []

    try:
        if release_from is not None:
            conn.execute(
                "UPDATE leads SET public_id = NULL WHERE id = ?",
                (release_from["id"],),
            )

        if survivor is None:
            columns = {**payload, **section_stamps}
            column_list = ", ".join(["account_id", *columns])
            placeholders = ", ".join("?" for _ in range(len(columns) + 1))
            cursor = conn.execute(
                f"INSERT INTO leads ({column_list}) VALUES ({placeholders})",
                [account_id, *columns.values()],
            )
            lead_id = int(cursor.lastrowid)
            created = True
        else:
            lead_id = int(survivor["id"])
            created = False
            changed = merge_fields(survivor, payload)
            updates = {**changed, **section_stamps}
            if updates:
                assignments = ", ".join(f"{column} = ?" for column in updates)
                conn.execute(
                    f"UPDATE leads SET {assignments} WHERE id = ?",
                    [*updates.values(), lead_id],
                )
            for column in sorted(IDENTITY_COLUMNS & set(changed)):
                if survivor[column] is not None:
                    displaced.append(
                        (lead_id, column, survivor[column], changed[column], None)
                    )

        if release_from is not None:
            archives.append(
                (release_from["id"], "public_id", release_from["public_id"], None, lead_id)
            )
        archives.extend(displaced)

        if archives:
            conn.executemany(
                """
                INSERT INTO lead_identity_history
                    (lead_id, kind, value, replaced_by, claimed_by_lead_id)
                VALUES (?, ?, ?, ?, ?)
                """,
                archives,
            )
        conn.commit()
    except sqlite3.IntegrityError as error:
        conn.rollback()
        if "UNIQUE" not in str(error).upper():
            raise LeadStoreError(
                f"could not store member_id {member_id!r} with public_id "
                f"{public_id!r} on account {account_id}: {error}"
            ) from error
        raise LeadIdentityConflictError(
            f"storing member_id {member_id!r} with public_id {public_id!r} on "
            f"account {account_id} collided with another lead: {error}",
            kind="public_id" if public_id is not None else "member_id",
            value=public_id if public_id is not None else member_id,
        ) from error
    except Exception:
        conn.rollback()
        raise

    lead = get_lead(conn, lead_id)
    if lead is None:
        raise LeadNotFoundError(lead_id)

    return UpsertResult(
        lead=lead,
        created=created,
        changed_fields=tuple(sorted(COLUMN_FIELDS[column] for column in changed)),
        sections_marked=tuple(
            sorted(normalise_section(section) for section in sections_fetched)
        ),
        released=tuple(
            IdentityChange(
                lead_id=row_id,
                kind=kind,
                value=value,
                replaced_by=replaced_by,
                claimed_by_lead_id=claimed_by,
            )
            for row_id, kind, value, replaced_by, claimed_by in archives
        ),
    )


def _refusal(
    index: int,
    profile: Mapping[str, Any],
    error: Exception,
) -> HarvestRefusal:
    if isinstance(error, LeadBlacklistedError):
        reason = HarvestRefusalReason.BLACKLISTED
    elif isinstance(error, LeadIdentityConflictError):
        reason = HarvestRefusalReason.IDENTITY_CONFLICT
    else:
        reason = HarvestRefusalReason.INVALID_PROFILE
    return HarvestRefusal(
        index=index,
        reason=reason.value,
        message=str(error),
        member_id=normalise_identifier(profile.get("member_id")),
        public_id=normalise_identifier(profile.get("public_id")),
    )


def harvest_leads(
    conn: sqlite3.Connection,
    account_id: int,
    profiles: Iterable[Mapping[str, Any]],
    *,
    sections_fetched: Sequence[str | LeadSection] = (),
    fetched_at: datetime | str | None = None,
) -> HarvestSummary:
    """Upsert a page of harvested profiles and report the incremental counts.

    One unusable profile never aborts a run of hundreds: blacklisted and
    ambiguous identities are collected in
    :attr:`HarvestSummary.refusals` with the reason attached.
    """
    created = 0
    updated = 0
    unchanged = 0
    found = 0
    lead_ids: list[int] = []
    refusals: list[HarvestRefusal] = []

    for index, profile in enumerate(profiles):
        found += 1
        try:
            result = upsert_lead(
                conn,
                account_id,
                sections_fetched=sections_fetched,
                fetched_at=fetched_at,
                **profile,
            )
        except (LeadBlacklistedError, LeadIdentityConflictError, ValueError) as error:
            refusals.append(_refusal(index, profile, error))
            continue

        lead_ids.append(result.lead.id)
        if result.created:
            created += 1
        elif result.updated:
            updated += 1
        else:
            unchanged += 1

    return HarvestSummary(
        found=found,
        created=created,
        updated=updated,
        unchanged=unchanged,
        lead_ids=tuple(lead_ids),
        refusals=tuple(refusals),
    )


def _require_lead_row(conn: sqlite3.Connection, lead_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
    if row is None:
        raise LeadNotFoundError(lead_id)
    return row


def section_fetched_at(
    conn: sqlite3.Connection,
    lead_id: int,
    section: str | LeadSection,
) -> str | None:
    """Return when a section was last extracted, or ``None`` if it never was."""
    column = SECTION_COLUMNS[normalise_section(section)]
    return _require_lead_row(conn, lead_id)[column]


def mark_section_fetched(
    conn: sqlite3.Connection,
    lead_id: int,
    section: str | LeadSection,
    *,
    fetched_at: datetime | str | None = None,
) -> str:
    """Record that a section was just extracted and return the stored timestamp."""
    column = SECTION_COLUMNS[normalise_section(section)]
    _require_lead_row(conn, lead_id)
    stamp = _as_timestamp(fetched_at) or utc_timestamp()
    conn.execute(f"UPDATE leads SET {column} = ? WHERE id = ?", (stamp, lead_id))
    conn.commit()
    return stamp


def needs_refresh(
    conn: sqlite3.Connection,
    lead_id: int,
    section: str | LeadSection,
    *,
    now: datetime | None = None,
    window_days: int | None = None,
) -> bool:
    """Return True when a section is missing or older than its cache window.

    This is the predicate the deep-scraper calls before spending a profile
    visit. A section extracted exactly one window ago is stale, so the window is
    the age at which data stops counting as fresh.
    """
    section = normalise_section(section)
    fetched = _parse_timestamp(section_fetched_at(conn, lead_id, section))
    if fetched is None:
        return True

    moment = datetime.now(timezone.utc) if now is None else now
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    window = timedelta(days=cache_window_days(section, window_days=window_days))
    return moment - fetched >= window


def stale_sections(
    conn: sqlite3.Connection,
    lead_id: int,
    *,
    now: datetime | None = None,
) -> tuple[str, ...]:
    """Return every section of a lead that is due for another extraction."""
    return tuple(
        section
        for section in sorted(SECTION_COLUMNS)
        if needs_refresh(conn, lead_id, section, now=now)
    )


def leads_needing_refresh(
    conn: sqlite3.Connection,
    account_id: int,
    section: str | LeadSection,
    *,
    now: datetime | None = None,
    window_days: int | None = None,
    limit: int | None = None,
    offset: int = 0,
    include_blacklisted: bool = False,
) -> list[Lead]:
    """List an account's leads whose section is stale, oldest extraction first.

    Blacklisted leads are dropped by default so a scrape queue built from this
    never sends the browser to a profile nobody is allowed to contact.
    """
    column = SECTION_COLUMNS[normalise_section(section)]
    moment = datetime.now(timezone.utc) if now is None else now
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    cutoff = utc_timestamp(
        moment - timedelta(days=cache_window_days(section, window_days=window_days))
    )

    sql = (
        f"SELECT * FROM leads WHERE account_id = ? AND ({column} IS NULL OR {column} <= ?)"
    )
    params: list[Any] = [account_id, cutoff]
    if not include_blacklisted:
        sql += f" AND {BLACKLIST_EXCLUSION_SQL}"
    sql += f" ORDER BY {column}, leads.id"
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params += [limit, offset]

    rows = conn.execute(sql, params).fetchall()
    return [lead_from_row(row) for row in rows]


def identity_history(
    conn: sqlite3.Connection,
    lead_id: int | None = None,
    *,
    kind: str | None = None,
    value: str | None = None,
) -> list[IdentityChange]:
    """Read archived identifiers, oldest first.

    Nothing in this package matches on the archive, because a released vanity
    URL may since belong to somebody else. It is here so a human can see where
    an identifier went.
    """
    clauses: list[str] = []
    params: list[Any] = []
    if lead_id is not None:
        clauses.append("lead_id = ?")
        params.append(lead_id)
    if kind is not None:
        clauses.append("kind = ?")
        params.append(kind)
    if value is not None:
        clauses.append("value = ?")
        params.append(normalise_identifier(value))

    sql = "SELECT * FROM lead_identity_history"
    if clauses:
        sql += f" WHERE {' AND '.join(clauses)}"
    sql += " ORDER BY id"

    return [_identity_change(row) for row in conn.execute(sql, params).fetchall()]
