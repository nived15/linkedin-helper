"""Structured records for one deep profile scrape.

A search card carries a name, a headline and a link. A profile carries a career.
:class:`~linkedin_mcp.scrape.records.PersonResult` still models the person,
because a person is a person wherever they were sighted, and the richer parts of
a profile get their own records here rather than being bolted onto it.

Everything except the identity stays optional. LinkedIn renders a profile
differently depending on who is looking, what the member has filled in and which
A/B bucket the session landed in, so a missing section is the normal case rather
than an error. A field that does not resolve comes back as ``None`` and an
absent section comes back as an empty tuple.

Why the parsers live here
-------------------------
Dates, endorsement counts and mutual-connection blurbs arrive as display text,
and turning that text into storable values is the part most likely to be wrong.
Keeping it in plain functions next to the records means it is tested directly
against strings rather than only through a fake DOM.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from linkedin_mcp.scrape.records import PersonResult, parse_count

__all__ = [
    "BADGE_NAMES",
    "CONTACT_KINDS",
    "MONTHS",
    "ContactEntry",
    "EducationEntry",
    "ExperienceEntry",
    "MutualConnections",
    "ProfileDetail",
    "SectionOutcome",
    "SkillEntry",
    "company_id_from",
    "contact_kind",
    "endorsement_count",
    "parse_date_range",
    "parse_mutual_connections",
    "parse_year_range",
]


class SectionOutcome(str, Enum):
    """How reading one section of a profile turned out.

    The cache windows hang off this. A boolean cannot tell "this member lists no
    positions" apart from "the section selector stopped matching", and stamping
    a section fresh on the second of those would suppress the retry that would
    have noticed. Only :attr:`READ` and :attr:`EMPTY` are stamped, because only
    they mean the page was understood.
    """

    READ = "read"
    """The section rendered and produced rows."""

    EMPTY = "empty"
    """The section rendered and genuinely holds nothing."""

    MISSING = "missing"
    """The section did not resolve, so this visit learned nothing about it."""

    SKIPPED = "skipped"
    """The section was deliberately not read, for instance a fresh cache."""

    def was_seen(self) -> bool:
        """True when the page was understood and the cache may be stamped."""
        return self in {SectionOutcome.READ, SectionOutcome.EMPTY}

CONTACT_KINDS: tuple[str, ...] = (
    "email",
    "work_email",
    "personal_email",
    "phone",
    "website",
    "twitter",
)
"""Contact kinds ``lead_contacts`` accepts. Its CHECK constraint enforces these."""

BADGE_NAMES: tuple[str, ...] = (
    "premium",
    "influencer",
    "open_link",
    "job_seeker",
    "hiring",
)
"""Badges a profile page can show, in the order the issue lists them."""

MONTHS: dict[str, int] = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "sept": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

PRESENT_WORDS: tuple[str, ...] = ("present", "current", "now", "today")
PRESENT_PATTERN = re.compile(
    r"\b(?:%s)\b" % "|".join(PRESENT_WORDS), re.IGNORECASE
)
RANGE_SEPARATOR = re.compile(r"\s*(?:[-\u2010-\u2015\u2212]|\bto\b)\s*", re.IGNORECASE)
DURATION_SEPARATOR = re.compile(r"[\u00b7\u2022|]")
YEAR_PATTERN = re.compile(r"\b(19\d{2}|20\d{2})\b")
MONTH_YEAR_PATTERN = re.compile(
    r"\b(?P<month>[A-Za-z]{3,9})\.?\s+(?P<year>19\d{2}|20\d{2})\b"
)
COMPANY_ID_PATTERN = re.compile(r"/company/(?P<company>[^/?#]+)")
MUTUAL_OTHERS_PATTERN = re.compile(r"\band\s+(?P<count>\d[\d,\u00a0]*)\s+other", re.IGNORECASE)
MUTUAL_PLAIN_PATTERN = re.compile(r"(?P<count>\d[\d,\u00a0]*)\s+mutual", re.IGNORECASE)
MUTUAL_TAIL_PATTERN = re.compile(
    r"\s*\b(?:is|are)\s+(?:a\s+)?mutual\s+connections?\b.*$", re.IGNORECASE
)
NAME_SEPARATOR = re.compile(r",\s*|\s+and\s+", re.IGNORECASE)

CONTACT_LABELS: tuple[tuple[str, str], ...] = (
    ("work email", "work_email"),
    ("business email", "work_email"),
    ("personal email", "personal_email"),
    ("home email", "personal_email"),
    ("email", "email"),
    ("e-mail", "email"),
    ("phone", "phone"),
    ("mobile", "phone"),
    ("website", "website"),
    ("blog", "website"),
    ("portfolio", "website"),
    ("twitter", "twitter"),
    ("x (twitter)", "twitter"),
)
"""Contact modal headers mapped onto storable kinds, longest label first.

Anything absent, such as Birthday, Address, IM or "Connected", is dropped rather
than coerced. ``lead_contacts.kind`` has a CHECK constraint, so a guessed kind
would abort the whole write for the sake of a field nobody asked for.
"""


def contact_kind(label: str | None) -> str | None:
    """Return the storable contact kind for a modal header, or None to skip it."""
    if not label:
        return None
    cleaned = label.strip().lower()
    if not cleaned:
        return None
    for prefix, kind in CONTACT_LABELS:
        if prefix in cleaned:
            return kind
    return None


def _strip_duration(text: str) -> str:
    """Drop LinkedIn's trailing duration, as in 'Jan 2020 - Present . 3 yrs'."""
    return DURATION_SEPARATOR.split(text, 1)[0].strip()


def _normalise_point(text: str) -> tuple[str | None, bool]:
    """Turn one end of a range into a storable value and a 'still here' flag.

    A fragment with no year in it yields nothing. The date line shares its
    selector chain with LinkedIn's employment-type caption, so a chain that
    slips one row over hands this "Full-time", and storing ``Full`` as a start
    date would be worse than storing nothing at all.
    """
    cleaned = text.strip().strip(",").strip()
    if not cleaned:
        return None, False
    if PRESENT_PATTERN.search(cleaned):
        return None, True

    month_year = MONTH_YEAR_PATTERN.search(cleaned)
    if month_year is not None:
        month = MONTHS.get(month_year.group("month")[:4].lower()) or MONTHS.get(
            month_year.group("month")[:3].lower()
        )
        if month is not None:
            return f"{month_year.group('year')}-{month:02d}", False

    year = YEAR_PATTERN.search(cleaned)
    if year is not None:
        return year.group(1), False
    return None, False


def parse_date_range(text: str | None) -> tuple[str | None, str | None, bool]:
    """Split a position's date line into a start, an end and a 'current' flag.

    Returns ISO-ish text rather than dates. ``Jan 2020`` becomes ``2020-01`` and a
    bare ``2020`` stays ``2020``, because a position that only names a year has
    no month to invent. An open-ended range yields ``None`` for the end and
    ``True`` for current, which is how the writer picks the present employer.

    A line carrying neither a year nor a present marker is not a date line and
    yields nothing, so a selector that drifted onto the wrong caption stores no
    dates rather than nonsense ones.
    """
    if not text:
        return None, None, False

    body = _strip_duration(str(text))
    if not body:
        return None, None, False

    parts = [part for part in RANGE_SEPARATOR.split(body) if part.strip()]
    if not parts:
        return None, None, False

    start, start_present = _normalise_point(parts[0])
    if len(parts) == 1:
        return (None, None, True) if start_present else (start, None, False)

    end, end_present = _normalise_point(parts[1])
    return start, (None if end_present else end), end_present or start_present


def parse_year_range(text: str | None) -> tuple[int | None, int | None]:
    """Split an education date line into start and end years."""
    if not text:
        return None, None
    years = [int(match) for match in YEAR_PATTERN.findall(_strip_duration(str(text)))]
    if not years:
        return None, None
    if len(years) == 1:
        return years[0], None
    return years[0], years[1]


def company_id_from(company_url: str | None) -> str | None:
    """Return the company slug from a company link, or None when there is none."""
    if not company_url:
        return None
    match = COMPANY_ID_PATTERN.search(company_url)
    if not match:
        return None
    slug = match.group("company").strip().strip("/")
    return slug or None


@dataclass(frozen=True, slots=True)
class MutualConnections:
    """Shared connections as the profile's network line describes them.

    ``count`` is what LinkedIn claims, which is not always what ``names`` holds.
    The blurb names one or two people and counts the rest, so both are kept
    rather than pretending the named ones are the whole set.
    """

    count: int | None = None
    names: tuple[str, ...] = ()
    url: str | None = None

    def is_empty(self) -> bool:
        return self.count is None and not self.names


def parse_mutual_connections(
    text: str | None,
    *,
    url: str | None = None,
) -> MutualConnections:
    """Read a mutual connections blurb into a count and the names it mentions."""
    if not text:
        return MutualConnections(url=url)

    body = str(text).replace("\u00a0", " ").strip()
    if not body:
        return MutualConnections(url=url)

    others = MUTUAL_OTHERS_PATTERN.search(body)
    if others is not None:
        named = _mutual_names(body[: others.start()])
        return MutualConnections(
            count=_digits(others.group("count")) + len(named),
            names=named,
            url=url,
        )

    plain = MUTUAL_PLAIN_PATTERN.search(body)
    if plain is not None:
        return MutualConnections(count=_digits(plain.group("count")), names=(), url=url)

    if "mutual" not in body.lower():
        return MutualConnections(url=url)

    named = _mutual_names(MUTUAL_TAIL_PATTERN.sub("", body))
    return MutualConnections(count=len(named) or None, names=named, url=url)


def _digits(text: str) -> int:
    return int(re.sub(r"[^\d]", "", text) or 0)


def _mutual_names(segment: str) -> tuple[str, ...]:
    names = [part.strip() for part in NAME_SEPARATOR.split(segment)]
    return tuple(name for name in names if name and "mutual" not in name.lower())


@dataclass(frozen=True, slots=True)
class ExperienceEntry:
    """One position, as it lands in ``lead_experience``.

    ``is_current`` is not a stored column. It comes from an open-ended date
    range and only decides which position becomes the lead's headline employer.
    """

    title: str | None = None
    company: str | None = None
    company_id: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    location: str | None = None
    is_current: bool = False

    def is_empty(self) -> bool:
        """True when the row would carry nothing worth storing."""
        return not (self.title or self.company)


@dataclass(frozen=True, slots=True)
class EducationEntry:
    """One school, as it lands in ``lead_education``.

    ``field_of_study`` is stored in the ``field`` column. The attribute is named
    in full because ``field`` is also ``dataclasses.field``.
    """

    school: str | None = None
    degree: str | None = None
    field_of_study: str | None = None
    start_year: int | None = None
    end_year: int | None = None

    def is_empty(self) -> bool:
        return not (self.school or self.degree or self.field_of_study)


@dataclass(frozen=True, slots=True)
class SkillEntry:
    """One skill and its endorsement count, as it lands in ``lead_skills``."""

    skill: str
    endorsement_count: int | None = None

    def is_empty(self) -> bool:
        return not self.skill.strip()


@dataclass(frozen=True, slots=True)
class ContactEntry:
    """One contact detail, as it lands in ``lead_contacts``."""

    kind: str
    value: str
    source: str | None = "profile_contact_info"

    def is_valid(self) -> bool:
        """True when the row satisfies the table's CHECK constraint."""
        return bool(self.value.strip()) and self.kind in CONTACT_KINDS


@dataclass(frozen=True, slots=True)
class ProfileDetail:
    """Everything one profile visit read, ready to be stored."""

    person: PersonResult
    connection_count: int | None = None
    follower_count: int | None = None
    experience: tuple[ExperienceEntry, ...] = ()
    education: tuple[EducationEntry, ...] = ()
    skills: tuple[SkillEntry, ...] = ()
    contacts: tuple[ContactEntry, ...] = ()
    mutual_connections: MutualConnections = field(default_factory=MutualConnections)
    experience_outcome: SectionOutcome = SectionOutcome.MISSING
    contact_info_outcome: SectionOutcome = SectionOutcome.SKIPPED
    contact_info_skipped_reason: str | None = None

    @property
    def member_distance(self) -> str | None:
        """The connection degree, such as ``1st``, or None when unreadable."""
        return self.person.member_distance

    @property
    def badges(self) -> dict[str, Any]:
        return dict(self.person.badges)

    @property
    def experience_section_seen(self) -> bool:
        """True when the positions section rendered, empty or not."""
        return self.experience_outcome.was_seen()

    @property
    def contact_info_attempted(self) -> bool:
        """True when the contact overlay was opened and read."""
        return self.contact_info_outcome.was_seen()

    def is_first_degree(self) -> bool:
        """True when LinkedIn will show this member's contact info."""
        return self.person.member_distance == "1st"

    def is_identifiable(self) -> bool:
        """True when the lead store can resolve this profile onto a row."""
        return self.person.is_identifiable()

    def current_position(self) -> ExperienceEntry | None:
        """The position the lead holds now, or the most recent one listed."""
        for entry in self.experience:
            if entry.is_current:
                return entry
        return self.experience[0] if self.experience else None

    def as_lead_fields(self) -> dict[str, Any]:
        """Return the ``upsert_lead`` keyword arguments for this visit.

        Only observed values are sent. The DB-03 merge rules refuse to blank a
        stored field with an empty incoming one, and sending nothing at all for
        a field that did not render keeps the two halves of that contract
        honest.
        """
        fields = self.person.as_lead_fields()

        position = self.current_position()
        if position is not None:
            if position.company and not fields.get("organization_name"):
                fields["organization_name"] = position.company
            if position.title and not fields.get("organization_title"):
                fields["organization_title"] = position.title

        if self.connection_count is not None:
            fields["connection_count"] = self.connection_count
        if self.follower_count is not None:
            fields["follower_count"] = self.follower_count
        return fields

    def custom_fields(self) -> dict[str, Any]:
        """Return the ``{cs_*}`` fields this visit learned.

        Mutual connections live here rather than in a new ``leads`` column. They
        are a fact about the relationship between two accounts rather than about
        the person, and a new writable column would need a merge rule of its own
        for something the template engine can already read as ``{cs_*}``.
        """
        mutual = self.mutual_connections
        if mutual.is_empty():
            return {}
        values: dict[str, Any] = {}
        if mutual.count is not None:
            values["mutual_connections"] = mutual.count
        if mutual.names:
            values["mutual_connection_names"] = ", ".join(mutual.names)
        return values

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON friendly record of the visit."""
        return {
            "full_name": self.person.full_name,
            "public_id": self.person.public_id,
            "member_id": self.person.member_id,
            "hash_id": self.person.hash_id,
            "headline": self.person.headline,
            "location_name": self.person.location_name,
            "member_distance": self.person.member_distance,
            "profile_url": self.person.profile_url,
            "connection_count": self.connection_count,
            "follower_count": self.follower_count,
            "badges": self.badges,
            "experience": [
                {
                    "title": entry.title,
                    "company": entry.company,
                    "company_id": entry.company_id,
                    "start_date": entry.start_date,
                    "end_date": entry.end_date,
                    "location": entry.location,
                    "is_current": entry.is_current,
                }
                for entry in self.experience
            ],
            "education": [
                {
                    "school": entry.school,
                    "degree": entry.degree,
                    "field": entry.field_of_study,
                    "start_year": entry.start_year,
                    "end_year": entry.end_year,
                }
                for entry in self.education
            ],
            "skills": [
                {"skill": entry.skill, "endorsement_count": entry.endorsement_count}
                for entry in self.skills
            ],
            "contacts": [
                {"kind": entry.kind, "value": entry.value, "source": entry.source}
                for entry in self.contacts
            ],
            "mutual_connections": {
                "count": self.mutual_connections.count,
                "names": list(self.mutual_connections.names),
                "url": self.mutual_connections.url,
            },
            "contact_info_attempted": self.contact_info_attempted,
            "experience_outcome": self.experience_outcome.value,
            "contact_info_outcome": self.contact_info_outcome.value,
            "contact_info_skipped_reason": self.contact_info_skipped_reason,
        }


def endorsement_count(text: str | None) -> int | None:
    """Return the endorsement count from a skill row, or None when unreadable."""
    return parse_count(text)
