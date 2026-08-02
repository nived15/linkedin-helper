"""Structured records for one extracted search result.

A search result carries far less than a profile scrape, and LinkedIn is
inconsistent about which parts of a card it renders. Every field except the
identity is therefore optional, and `as_lead_fields` drops the empty ones so a
thin sighting never blanks out a richer stored record. The merge rules in
`linkedin_mcp.leads.dedupe` do the same thing on the other side; sending only
what was actually observed keeps the two honest.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urlsplit

__all__ = [
    "MEMBER_URN_PATTERN",
    "PROFILE_URN_PATTERN",
    "PersonResult",
    "PostResult",
    "member_urn_from",
    "parse_count",
    "profile_hash_from",
    "public_id_from",
]

PROFILE_PATH_PATTERN = re.compile(r"/in/(?P<slug>[^/?#]+)")
MEMBER_URN_PATTERN = re.compile(r"urn:li:member:(?P<member>\d+)")
PROFILE_URN_PATTERN = re.compile(r"urn:li:fsd_profile:(?P<hash>[A-Za-z0-9_-]+)")
ACTIVITY_URN_PATTERN = re.compile(r"urn:li:(?:activity|ugcPost|share):(?P<id>\d+)")
COUNT_PATTERN = re.compile(r"(?P<number>\d[\d,.\u00a0\s]*)\s*(?P<suffix>[KkMm])?")
DISTANCE_PATTERN = re.compile(r"\b(1st|2nd|3rd\+?|3rd)\b", re.IGNORECASE)


def public_id_from(profile_url: str | None) -> str | None:
    """Return the vanity slug from a profile URL, or None when there is none."""
    if not profile_url:
        return None
    match = PROFILE_PATH_PATTERN.search(profile_url)
    if not match:
        return None
    slug = unquote(match.group("slug")).strip().strip("/")
    return slug or None


def member_urn_from(text: str | None) -> str | None:
    """Return the durable `urn:li:member:...` identifier when one is present."""
    if not text:
        return None
    match = MEMBER_URN_PATTERN.search(text)
    return f"urn:li:member:{match.group('member')}" if match else None


def profile_hash_from(text: str | None) -> str | None:
    """Return the `ACoAA...` profile hash from an fsd_profile URN.

    LinkedIn's search cards carry `urn:li:fsd_profile:ACoAA...` rather than a
    member id. The tail is the same identifier Linked Helper calls a hash id, so
    it is stored in `leads.hash_id` and never mistaken for a member id.
    """
    if not text:
        return None
    match = PROFILE_URN_PATTERN.search(text)
    return match.group("hash") if match else None


def activity_id_from(text: str | None) -> str | None:
    """Return the numeric activity id from a post URN or permalink."""
    if not text:
        return None
    match = ACTIVITY_URN_PATTERN.search(text)
    return match.group("id") if match else None


def parse_count(text: str | None) -> int | None:
    """Turn '1,234 reactions' or '2K' into an integer, or None when unreadable.

    LinkedIn abbreviates large counts. A rounded number is better than no
    number, so 2K reads as 2000 rather than being dropped.
    """
    if not text:
        return None
    match = COUNT_PATTERN.search(text)
    if not match:
        return None
    digits = re.sub(r"[^\d]", "", match.group("number"))
    if not digits:
        return None
    value = int(digits)
    suffix = (match.group("suffix") or "").lower()
    if suffix == "k":
        value *= 1000
    elif suffix == "m":
        value *= 1_000_000
    return value


def parse_distance(text: str | None) -> str | None:
    """Return the connection degree label from a badge, such as '2nd'."""
    if not text:
        return None
    match = DISTANCE_PATTERN.search(text)
    if not match:
        return None
    label = match.group(1).lower()
    return "3rd+" if label.startswith("3rd") else label


def split_name(full_name: str) -> tuple[str | None, str | None]:
    """Split a display name into first and last, leaving middles on the last."""
    parts = [part for part in full_name.split() if part]
    if len(parts) < 2:
        return (parts[0] if parts else None), None
    return parts[0], " ".join(parts[1:])


def name_from_slug(slug: str) -> str:
    """Build a readable name from a vanity slug, for a card with no name node.

    A result whose name did not render is still a lead. Dropping it would lose
    a person the search genuinely returned, so the slug supplies a label and the
    next sighting overwrites it with the real name.
    """
    tokens = [token for token in re.split(r"[-_]+", slug) if token]
    words = [token for token in tokens if not _looks_like_an_id(token)]
    return " ".join(word.capitalize() for word in words) or slug


def _looks_like_an_id(token: str) -> bool:
    return len(token) >= 6 and any(character.isdigit() for character in token)


def canonical_profile_url(profile_url: str | None) -> str | None:
    """Strip tracking parameters off a profile URL so it dedupes cleanly."""
    slug = public_id_from(profile_url)
    return f"https://www.linkedin.com/in/{slug}/" if slug else None


def canonical_post_url(post_url: str | None) -> str | None:
    """Strip the query string off a post permalink."""
    if not post_url:
        return None
    parts = urlsplit(post_url)
    if not parts.path:
        return None
    if parts.netloc:
        return f"{parts.scheme or 'https'}://{parts.netloc}{parts.path}"
    return f"https://www.linkedin.com{parts.path}"


@dataclass(frozen=True, slots=True)
class PersonResult:
    """One person as a search result page describes them."""

    full_name: str
    public_id: str | None = None
    member_id: str | None = None
    hash_id: str | None = None
    headline: str | None = None
    location_name: str | None = None
    organization_name: str | None = None
    organization_title: str | None = None
    member_distance: str | None = None
    avatar_url: str | None = None
    summary: str | None = None
    profile_url: str | None = None
    badges: dict[str, Any] = field(default_factory=dict)

    @property
    def dedupe_key(self) -> str:
        """Stable key for one person within a run."""
        return self.member_id or self.public_id or self.hash_id or self.full_name

    def is_identifiable(self) -> bool:
        """True when the lead store can resolve this sighting onto a row."""
        return bool(self.member_id or self.public_id)

    def as_lead_fields(self) -> dict[str, Any]:
        """Return the `upsert_lead` keyword arguments for this sighting."""
        first_name, last_name = split_name(self.full_name)
        fields: dict[str, Any] = {
            "full_name": self.full_name,
            "member_id": self.member_id,
            "public_id": self.public_id,
            "hash_id": self.hash_id,
            "first_name": first_name,
            "last_name": last_name,
            "headline": self.headline,
            "location_name": self.location_name,
            "organization_name": self.organization_name,
            "organization_title": self.organization_title,
            "member_distance": self.member_distance,
            "avatar_url": self.avatar_url,
            "summary": self.summary,
        }
        payload = {name: value for name, value in fields.items() if value is not None}
        if self.badges:
            payload["badges"] = dict(self.badges)
        return payload


@dataclass(frozen=True, slots=True)
class PostResult:
    """One post as a content search result page describes it."""

    post_url: str | None = None
    activity_id: str | None = None
    content: str | None = None
    posted_at_text: str | None = None
    reactions: int | None = None
    comments: int | None = None
    reposts: int | None = None
    author: PersonResult | None = None

    @property
    def dedupe_key(self) -> str:
        """Stable key for one post within a run."""
        return self.activity_id or self.post_url or (self.content or "")[:120]

    def is_identifiable(self) -> bool:
        """True when the post can be pointed at again later."""
        return bool(self.activity_id or self.post_url)

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON friendly record of the post and its author."""
        return {
            "post_url": self.post_url,
            "activity_id": self.activity_id,
            "content": self.content,
            "posted_at_text": self.posted_at_text,
            "reactions": self.reactions,
            "comments": self.comments,
            "reposts": self.reposts,
            "author_public_id": None if self.author is None else self.author.public_id,
            "author_name": None if self.author is None else self.author.full_name,
        }
