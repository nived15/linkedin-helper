"""Read one rendered LinkedIn profile page into structured records.

This is SCRAPE-01's extraction approach applied to a profile: Python over
element handles, an ordered selector fallback per field, and a missing field
that resolves to ``None`` instead of taking the visit down. Nothing here
navigates, nothing here writes and nothing here sleeps outside the humanizer.
:mod:`linkedin_mcp.scrape.profile` owns the gate, the navigation and the clock.

Sections are read inside their own container
--------------------------------------------
Experience, education and skills all render as ``li.artdeco-list__item`` rows in
the modern DOM, so a page-wide query for a row would mix somebody's last job in
with their degree. Every list is therefore read strictly inside its own section
element, and a section whose container does not resolve yields nothing rather
than guessing. That is the safer failure: an empty section is visible in the
result and never overwrites stored data, while a mixed one would be stored as
fact.

Contact info
------------
Contact details only exist for 1st-degree connections. Any other degree returns
an empty tuple with a reason attached and never touches the page, because
clicking a control that is not there is an error LinkedIn does not need to see.
The overlay is opened by clicking the top card link, never by loading
``/overlay/contact-info/``, which would spend the direct profile-load budget
CORE-04 exists to protect.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

from linkedin_mcp.browser.humanize import Humanizer, get_humanizer
from linkedin_mcp.scrape.extract import attr_of, handle_text, query_all, query_first, text_of
from linkedin_mcp.scrape.profile_records import (
    BADGE_NAMES,
    ContactEntry,
    EducationEntry,
    ExperienceEntry,
    MutualConnections,
    ProfileDetail,
    SectionOutcome,
    SkillEntry,
    company_id_from,
    contact_kind,
    endorsement_count,
    parse_date_range,
    parse_mutual_connections,
    parse_year_range,
)
from linkedin_mcp.scrape.records import (
    PersonResult,
    canonical_profile_url,
    member_urn_from,
    name_from_slug,
    parse_count,
    parse_distance,
    profile_hash_from,
    public_id_from,
)

logger = logging.getLogger(__name__)

__all__ = [
    "BADGE_SELECTORS",
    "CONTACT_INFO_FRESH",
    "IDENTITY_ATTRIBUTES",
    "NOT_FIRST_DEGREE",
    "NO_CONTACT_LINK",
    "NO_CONTACT_MODAL",
    "UNKNOWN_DEGREE",
    "extract_contact_info",
    "extract_education",
    "extract_experience",
    "extract_profile",
    "extract_skills",
]

IDENTITY_ATTRIBUTES: tuple[str, ...] = (
    "data-member-id",
    "data-urn",
    "data-entity-urn",
    "data-chameleon-result-urn",
)
"""Attributes a profile page hangs its member identity on, newest first."""

BADGE_SELECTORS: dict[str, str] = {
    "premium": "profile_detail_premium_badge",
    "influencer": "profile_detail_influencer_badge",
    "open_link": "profile_detail_openlink_badge",
    "job_seeker": "profile_detail_jobseeker_badge",
    "hiring": "profile_detail_hiring_badge",
}
"""Badge name to selector group. Keys match :data:`BADGE_NAMES`."""

NOT_FIRST_DEGREE = "not_first_degree"
UNKNOWN_DEGREE = "unknown_degree"
NO_CONTACT_LINK = "no_contact_info_link"
NO_CONTACT_MODAL = "contact_info_overlay_unreadable"
CONTACT_INFO_FRESH = "contact_info_still_fresh"

FOLLOWER_WORDS = ("follower",)
CONNECTION_WORDS = ("connection",)


async def _scope(page: Any, name: str) -> Any:
    """Return the named container, falling back to the page when it is missing."""
    handle = await query_first(page, name)
    return page if handle is None else handle


async def _identity(page: Any, profile_url: str | None) -> tuple[str | None, str | None, str | None]:
    """Return (public_id, member_id, hash_id) for the profile on screen."""
    public_id = public_id_from(profile_url)
    member_id: str | None = None
    hash_id: str | None = None

    for attribute in IDENTITY_ATTRIBUTES:
        value = await attr_of(page, "profile_detail_member_urn", attribute)
        if not value:
            continue
        if member_id is None:
            member_id = (
                f"urn:li:member:{value}" if value.isdigit() else member_urn_from(value)
            )
        if hash_id is None:
            hash_id = profile_hash_from(value)

    if member_id is None:
        member_id = member_urn_from(profile_url)
    if hash_id is None:
        hash_id = profile_hash_from(profile_url)
    return public_id, member_id, hash_id


async def _network_counts(scope: Any) -> tuple[int | None, int | None]:
    """Return (connection_count, follower_count) from the top card's stat line.

    The two numbers render as sibling nodes with identical classes, so they are
    told apart by their own text. A line mentioning mutual connections is
    skipped: it counts shared connections, not the member's own.
    """
    connections: int | None = None
    followers: int | None = None

    for handle in await query_all(scope, "profile_detail_network_stats"):
        text = await handle_text(handle)
        if not text:
            continue
        lowered = text.lower()
        if "mutual" in lowered:
            continue
        if followers is None and any(word in lowered for word in FOLLOWER_WORDS):
            followers = parse_count(text)
        elif connections is None and any(word in lowered for word in CONNECTION_WORDS):
            connections = parse_count(text)

    return connections, followers


async def _badges(scope: Any) -> dict[str, Any]:
    """Return the badges this page shows, recording presence only.

    An absent badge is not written as ``False``. The DB-03 badge merge treats a
    missing key as "not observed here", and with selectors that are hypotheses
    about LinkedIn's markup, a selector that stopped matching would otherwise
    strip a badge a previous sighting saw for real.
    """
    badges: dict[str, Any] = {}
    for name in BADGE_NAMES:
        selector = BADGE_SELECTORS[name]
        try:
            if await query_first(scope, selector) is not None:
                badges[name] = True
        except Exception as error:  # noqa: BLE001 - a badge is never worth failing on
            logger.debug("Badge %s could not be read: %s", name, error)
    return badges


async def _mutual_connections(scope: Any) -> MutualConnections:
    handle = await query_first(scope, "profile_detail_mutual_connections")
    if handle is None:
        return MutualConnections()
    text = await handle_text(handle)
    url: str | None = None
    getter = getattr(handle, "get_attribute", None)
    if getter is not None:
        try:
            url = await getter("href")
        except Exception as error:  # noqa: BLE001 - a dead handle is not fatal
            logger.debug("Reading the mutual connections href failed: %s", error)
    return parse_mutual_connections(text, url=url)


async def extract_experience(page: Any) -> tuple[tuple[ExperienceEntry, ...], SectionOutcome]:
    """Extract the positions listed on a profile, and say how the read went.

    The outcome matters for the cache windows. A member with no positions listed
    has a section that was read successfully and is empty, and marking that
    fresh stops the scraper spending a profile visit on them every fortnight
    forever. A section that did not resolve at all is a different thing and is
    never marked, because the next visit should still try.
    """
    section = await query_first(page, "profile_experience_section")
    if section is None:
        logger.warning(
            "No experience section resolved on this profile; positions stay stale"
        )
        return (), SectionOutcome.MISSING

    entries, rows_seen = await _experience_entries(section)
    if entries:
        return tuple(entries), SectionOutcome.READ
    if rows_seen:
        # The section rendered and so did its rows, but not one of them could be
        # read. That is a row selector that has moved, not a member with no
        # career, so nothing is marked fresh and the next visit tries again.
        logger.warning(
            "The experience section held %d unreadable row(s); positions stay stale",
            rows_seen,
        )
        return (), SectionOutcome.MISSING
    return (), SectionOutcome.EMPTY


async def _experience_entries(section: Any) -> tuple[list[ExperienceEntry], int]:
    """Read a positions list, flattening the grouped roles of one employer.

    Several roles at the same company render as an outer row holding the
    company, with the individual roles nested inside it as rows matching the
    same selector. A flat read would store the group header as if it were a job
    and then store every role a second time. The list comes back in document
    order, so a row that contains matching rows is a group: its roles are read
    with the company inherited, and the run steps over the rows it just
    consumed.

    Returns the entries and how many raw rows were on the page, so the caller
    can tell an empty section apart from one whose rows would not read.
    """
    items = await query_all(section, "profile_experience_entry")
    entries: list[ExperienceEntry] = []
    index = 0

    while index < len(items):
        item = items[index]
        try:
            nested = await query_all(item, "profile_experience_entry")
        except Exception as error:  # noqa: BLE001 - one bad row is not a bad section
            logger.warning("Skipping an unreadable experience row: %s", error)
            index += 1
            continue

        if not nested:
            entry = await _safe_experience(item)
            if entry is not None:
                entries.append(entry)
            index += 1
            continue

        company = await _text(item, "profile_experience_entry_title")
        company_id = company_id_from(
            await _attr(item, "profile_experience_entry_company_link", "href")
        )
        for role in nested:
            entry = await _safe_experience(item=role, company=company, company_id=company_id)
            if entry is not None:
                entries.append(entry)
        index += 1 + len(nested)

    return entries, len(items)

async def _safe_experience(
    item: Any,
    *,
    company: str | None = None,
    company_id: str | None = None,
) -> ExperienceEntry | None:
    """Build one position, dropping the row rather than the section on error."""
    try:
        entry = await _experience_from(item)
    except Exception as error:  # noqa: BLE001 - one bad row is not a bad section
        logger.warning("Skipping an unreadable experience row: %s", error)
        return None

    if company and not entry.company:
        entry = replace(entry, company=company)
    if company_id and not entry.company_id:
        entry = replace(entry, company_id=company_id)
    return None if entry.is_empty() else entry


async def _text(scope: Any, name: str) -> str | None:
    try:
        return await text_of(scope, name)
    except Exception as error:  # noqa: BLE001 - a dead handle is not fatal
        logger.debug("Reading %s failed: %s", name, error)
        return None


async def _attr(scope: Any, name: str, attribute: str) -> str | None:
    try:
        return await attr_of(scope, name, attribute)
    except Exception as error:  # noqa: BLE001 - a dead handle is not fatal
        logger.debug("Reading @%s off %s failed: %s", attribute, name, error)
        return None


async def _experience_from(item: Any) -> ExperienceEntry:
    start, end, is_current = parse_date_range(
        await text_of(item, "profile_experience_entry_dates")
    )
    return ExperienceEntry(
        title=await text_of(item, "profile_experience_entry_title"),
        company=await text_of(item, "profile_experience_entry_company"),
        company_id=company_id_from(
            await attr_of(item, "profile_experience_entry_company_link", "href")
        ),
        start_date=start,
        end_date=end,
        location=await text_of(item, "profile_experience_entry_location"),
        is_current=is_current,
    )


async def extract_education(page: Any) -> tuple[EducationEntry, ...]:
    """Extract the schools listed on a profile."""
    section = await query_first(page, "profile_education_section")
    if section is None:
        logger.debug("No education section on this profile")
        return ()

    entries: list[EducationEntry] = []
    for item in await query_all(section, "profile_education_entry"):
        try:
            entry = await _education_from(item)
        except Exception as error:  # noqa: BLE001 - one bad row is not a bad section
            logger.warning("Skipping an unreadable education row: %s", error)
            continue
        if entry is not None and not entry.is_empty():
            entries.append(entry)
    return tuple(entries)


async def _education_from(item: Any) -> EducationEntry | None:
    degree_line = await text_of(item, "profile_education_entry_degree")
    degree, field_of_study = _split_degree(degree_line)
    start_year, end_year = parse_year_range(
        await text_of(item, "profile_education_entry_dates")
    )
    return EducationEntry(
        school=await text_of(item, "profile_education_entry_school"),
        degree=degree,
        field_of_study=field_of_study,
        start_year=start_year,
        end_year=end_year,
    )


def _split_degree(text: str | None) -> tuple[str | None, str | None]:
    """Split 'Bachelor of Engineering, Computer Science' into its two halves.

    LinkedIn renders the degree and the field of study on one line separated by
    a comma. A line with no comma is kept whole as the degree, because guessing
    which half is missing would be worse than storing what was shown.
    """
    if not text:
        return None, None
    cleaned = text.strip()
    if not cleaned:
        return None, None
    if "," not in cleaned:
        return cleaned, None
    degree, _, field_of_study = cleaned.partition(",")
    return degree.strip() or None, field_of_study.strip() or None


async def extract_skills(page: Any) -> tuple[SkillEntry, ...]:
    """Extract the skills listed on a profile, with endorsement counts."""
    section = await query_first(page, "profile_skills_section")
    if section is None:
        logger.debug("No skills section on this profile")
        return ()

    skills: list[SkillEntry] = []
    seen: set[str] = set()
    for item in await query_all(section, "profile_skills_entry"):
        try:
            name = await text_of(item, "profile_skills_entry_name")
            if not name or name.lower() in seen:
                continue
            seen.add(name.lower())
            skills.append(
                SkillEntry(
                    skill=name,
                    endorsement_count=endorsement_count(
                        await text_of(item, "profile_skills_entry_endorsements")
                    ),
                )
            )
        except Exception as error:  # noqa: BLE001 - one bad row is not a bad section
            logger.warning("Skipping an unreadable skill row: %s", error)
    return tuple(skills)


async def extract_contact_info(
    page: Any,
    *,
    member_distance: str | None,
    humanizer: Humanizer | None = None,
) -> tuple[tuple[ContactEntry, ...], SectionOutcome, str | None]:
    """Open the contact overlay for a 1st-degree connection and read it.

    Returns the contacts, how the read went and, when it was skipped, why. A
    member who is not 1st degree is skipped cleanly: no click, no exception, no
    error log. LinkedIn does not show their contact details to anyone, so
    attempting it would be a request that can only fail.

    The outcome is deliberately not "did we click". An overlay that opens and
    then does not render a readable body comes back as
    :attr:`SectionOutcome.MISSING`, so nothing marks contact info fresh on the
    strength of a click alone. An overlay that opens and holds nothing storable,
    which is what a profile showing only a birthday looks like, is
    :attr:`SectionOutcome.EMPTY` and may be stamped.
    """
    if member_distance is None:
        return (), SectionOutcome.SKIPPED, UNKNOWN_DEGREE
    if member_distance != "1st":
        return (), SectionOutcome.SKIPPED, NOT_FIRST_DEGREE

    pacer = humanizer or get_humanizer()

    trigger = await query_first(page, "profile_contact_info_trigger")
    if trigger is None:
        logger.warning(
            "A 1st-degree profile showed no contact info link; the selector may have moved"
        )
        return (), SectionOutcome.MISSING, NO_CONTACT_LINK

    try:
        await pacer.click(trigger)
        await pacer.settle()
    except Exception as error:  # noqa: BLE001 - an unopenable overlay is not fatal
        logger.warning("The contact info overlay would not open: %s", error)
        return (), SectionOutcome.MISSING, NO_CONTACT_LINK

    contacts, outcome = await _read_contact_modal(page)
    await _close_contact_modal(page, pacer)
    return contacts, outcome, None if outcome.was_seen() else NO_CONTACT_MODAL


async def _read_contact_modal(page: Any) -> tuple[tuple[ContactEntry, ...], SectionOutcome]:
    modal = await query_first(page, "profile_contact_info_modal")
    if modal is None:
        logger.warning(
            "The contact info overlay opened without a readable body; nothing is marked fresh"
        )
        return (), SectionOutcome.MISSING

    contacts: list[ContactEntry] = []
    seen: set[tuple[str, str]] = set()
    for section in await query_all(modal, "profile_contact_info_section"):
        try:
            kind = contact_kind(
                await text_of(section, "profile_contact_info_section_header")
            )
            if kind is None:
                continue
            for value in await _contact_values(section):
                entry = ContactEntry(kind=kind, value=value)
                key = (entry.kind, entry.value)
                if key in seen or not entry.is_valid():
                    continue
                seen.add(key)
                contacts.append(entry)
        except Exception as error:  # noqa: BLE001 - one bad row is not a bad overlay
            logger.warning("Skipping an unreadable contact info row: %s", error)
    return tuple(contacts), SectionOutcome.READ if contacts else SectionOutcome.EMPTY


async def _contact_values(section: Any) -> list[str]:
    values: list[str] = []
    for handle in await query_all(section, "profile_contact_info_section_value"):
        text = await handle_text(handle)
        if not text:
            getter = getattr(handle, "get_attribute", None)
            if getter is not None:
                try:
                    href = await getter("href")
                except Exception as error:  # noqa: BLE001 - a dead handle is not fatal
                    logger.debug("Reading a contact href failed: %s", error)
                    href = None
                text = _strip_scheme(href)
        if text:
            values.append(text)
    return values


def _strip_scheme(href: str | None) -> str | None:
    """Turn 'mailto:a@b.com' or 'tel:+1' into the value behind it."""
    if not href:
        return None
    for scheme in ("mailto:", "tel:"):
        if href.lower().startswith(scheme):
            return href[len(scheme) :].strip() or None
    return href.strip() or None


async def _close_contact_modal(page: Any, pacer: Humanizer) -> None:
    """Dismiss the overlay so the rest of the profile stays reachable."""
    try:
        close = await query_first(page, "profile_contact_info_close")
        if close is not None:
            await pacer.click(close)
    except Exception as error:  # noqa: BLE001 - a stuck overlay is not fatal here
        logger.debug("The contact info overlay would not close: %s", error)


async def extract_profile(
    page: Any,
    *,
    profile_url: str | None = None,
    humanizer: Humanizer | None = None,
    read_contact_info: bool = True,
) -> ProfileDetail:
    """Read the profile currently on screen into a :class:`ProfileDetail`.

    Args:
        page: A page already showing a ``/in/`` profile.
        profile_url: The URL that was navigated to. Defaults to ``page.url``,
            and is only used for the identifiers it carries.
        humanizer: Pacing for the contact overlay click.
        read_contact_info: Set False to read the page without opening the
            overlay, for instance when the cache window says contact info is
            still fresh.
    """
    url = profile_url or getattr(page, "url", None)
    top_card = await _scope(page, "profile_detail_top_card")

    public_id, member_id, hash_id = await _identity(page, url)
    full_name = await text_of(top_card, "profile_detail_name")
    if not full_name and public_id:
        full_name = name_from_slug(public_id)
    if not full_name:
        full_name = member_id or ""

    member_distance = parse_distance(
        await text_of(top_card, "profile_detail_distance")
    )
    connection_count, follower_count = await _network_counts(top_card)

    person = PersonResult(
        full_name=full_name,
        public_id=public_id,
        member_id=member_id,
        hash_id=hash_id,
        headline=await text_of(top_card, "profile_detail_headline"),
        location_name=await text_of(top_card, "profile_detail_location"),
        member_distance=member_distance,
        avatar_url=await attr_of(top_card, "profile_detail_avatar", "src"),
        summary=await text_of(page, "profile_detail_about"),
        profile_url=canonical_profile_url(url),
        badges=await _badges(page),
    )

    experience, experience_outcome = await extract_experience(page)
    education = await extract_education(page)
    skills = await extract_skills(page)
    mutual = await _mutual_connections(top_card)

    # Contact info goes last on purpose. It is the only step that changes the
    # page, and an overlay that refuses to close would otherwise sit on top of
    # the sections still waiting to be read.
    contacts: tuple[ContactEntry, ...] = ()
    contact_outcome = SectionOutcome.SKIPPED
    skipped_reason: str | None = CONTACT_INFO_FRESH
    if read_contact_info:
        contacts, contact_outcome, skipped_reason = await extract_contact_info(
            page,
            member_distance=member_distance,
            humanizer=humanizer,
        )

    return ProfileDetail(
        person=person,
        connection_count=connection_count,
        follower_count=follower_count,
        experience=experience,
        education=education,
        skills=skills,
        contacts=contacts,
        mutual_connections=mutual,
        experience_outcome=experience_outcome,
        contact_info_outcome=contact_outcome,
        contact_info_skipped_reason=skipped_reason,
    )
