"""Validated LinkedIn search filters, mapped onto real URL parameters.

LinkedIn's search filters live in the query string, and the query string is the
only sane way to apply the whole set at once. Driving the filter panel by clicks
would mean a dozen fragile menu interactions per run for a result that one URL
expresses exactly.

That is not an invitation to accept a raw query blob from a caller. A blob
cannot be checked, so a typo in a facet name reaches LinkedIn as a silently
ignored parameter and the run quietly returns the wrong people. Every filter is
an explicit named field here, validated on construction and encoded on the way
out.

Facet values
------------
LinkedIn identifies companies, schools, industries, geographies and service
categories by numeric id, not by name. `geoUrn=["103644278"]` is the United
States. The ids are visible in the URL after you apply a filter by hand, and
this module accepts either the bare id or the full URN it appears in, so
`urn:li:fsd_geo:103644278` and `103644278` mean the same thing.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "CONNECTION_DEGREES",
    "DATE_POSTED_WINDOWS",
    "POST_SORT_ORDERS",
    "FilterError",
    "PeopleSearchFilters",
    "PostSearchFilters",
    "encode_params",
    "normalise_facet_id",
]

MAX_FREE_TEXT_LENGTH = 200

FACET_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.\-]+$")
URN_TAIL_PATTERN = re.compile(r"^urn:li:[A-Za-z_]+:(?P<value>.+)$")
LANGUAGE_PATTERN = re.compile(r"^[a-z]{2}$")

CONNECTION_DEGREES: Mapping[str, str] = {
    "1st": "F",
    "2nd": "S",
    "3rd+": "O",
}
"""Human labels for LinkedIn's `network` facet codes."""

_DEGREE_CODES = frozenset(CONNECTION_DEGREES.values())
_DEGREE_ALIASES = {
    "1": "F",
    "1st": "F",
    "first": "F",
    "f": "F",
    "2": "S",
    "2nd": "S",
    "second": "S",
    "s": "S",
    "3": "O",
    "3rd": "O",
    "3rd+": "O",
    "third": "O",
    "o": "O",
    "out_of_network": "O",
}

DATE_POSTED_WINDOWS: frozenset[str] = frozenset(
    {"past-24h", "past-week", "past-month"}
)
"""Values LinkedIn accepts for the content search `datePosted` facet."""

POST_SORT_ORDERS: frozenset[str] = frozenset({"relevance", "date_posted"})
"""Values LinkedIn accepts for the content search `sortBy` facet."""


class FilterError(ValueError):
    """Raised when a filter value would not survive a round trip to LinkedIn."""


def _clean_text(name: str, value: str | None) -> str | None:
    """Return trimmed free text, rejecting a value that says nothing."""
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        raise FilterError(f"{name} was given but is blank; leave it unset instead")
    if len(cleaned) > MAX_FREE_TEXT_LENGTH:
        raise FilterError(
            f"{name} is {len(cleaned)} characters, above the {MAX_FREE_TEXT_LENGTH} "
            "character limit LinkedIn search accepts"
        )
    return cleaned


def normalise_facet_id(name: str, value: Any) -> str:
    """Return the bare LinkedIn id for a facet value.

    A full URN is accepted and reduced to its tail, because that is what the
    address bar shows after you apply a filter by hand and it is the form a
    caller is most likely to copy.
    """
    text = str(value).strip()
    if not text:
        raise FilterError(f"{name} contains an empty id")
    urn = URN_TAIL_PATTERN.match(text)
    if urn:
        text = urn.group("value").strip()
    if not FACET_ID_PATTERN.match(text):
        raise FilterError(
            f"{name} id {value!r} is not a LinkedIn facet id; expected something "
            "like 103644278 or urn:li:fsd_geo:103644278"
        )
    return text


def _facet_ids(name: str, values: Iterable[Any] | None) -> tuple[str, ...]:
    """Normalise a facet list, dropping duplicates but keeping caller order."""
    if not values:
        return ()
    if isinstance(values, (str, bytes)):
        raise FilterError(f"{name} takes a sequence of ids, not a single string")
    seen: list[str] = []
    for value in values:
        identifier = normalise_facet_id(name, value)
        if identifier not in seen:
            seen.append(identifier)
    return tuple(seen)


def _degrees(values: Iterable[Any] | None) -> tuple[str, ...]:
    """Normalise connection degrees onto LinkedIn's F, S and O codes."""
    if not values:
        return ()
    if isinstance(values, (str, bytes)):
        raise FilterError(
            "connection_degrees takes a sequence such as ('1st', '2nd'), not a "
            "single string"
        )
    codes: list[str] = []
    for value in values:
        text = str(value).strip()
        code = text if text in _DEGREE_CODES else _DEGREE_ALIASES.get(text.lower())
        if code is None:
            raise FilterError(
                f"connection degree {value!r} is not one of "
                f"{sorted(CONNECTION_DEGREES)}"
            )
        if code not in codes:
            codes.append(code)
    return tuple(codes)


def _languages(values: Iterable[Any] | None) -> tuple[str, ...]:
    """Normalise profile languages onto two letter ISO codes."""
    if not values:
        return ()
    if isinstance(values, (str, bytes)):
        raise FilterError(
            "profile_languages takes a sequence such as ('en',), not a single string"
        )
    codes: list[str] = []
    for value in values:
        code = str(value).strip().lower()
        if not LANGUAGE_PATTERN.match(code):
            raise FilterError(
                f"profile language {value!r} is not a two letter code such as 'en'"
            )
        if code not in codes:
            codes.append(code)
    return tuple(codes)


def _list_param(values: Sequence[str]) -> str:
    """Encode a facet list the way LinkedIn writes it into the query string."""
    return json.dumps(list(values), separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class PeopleSearchFilters:
    """The People search filter set, validated on construction.

    Every field maps onto one LinkedIn URL parameter. The list fields take a
    sequence of facet ids; the free text fields take a string.
    """

    keywords: str | None = None
    connection_degrees: tuple[str, ...] = ()
    geo_urns: tuple[str, ...] = ()
    current_companies: tuple[str, ...] = ()
    past_companies: tuple[str, ...] = ()
    industries: tuple[str, ...] = ()
    schools: tuple[str, ...] = ()
    title: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    service_categories: tuple[str, ...] = ()
    profile_languages: tuple[str, ...] = ()
    extra: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "keywords", _clean_text("keywords", self.keywords))
        object.__setattr__(self, "title", _clean_text("title", self.title))
        object.__setattr__(
            self, "first_name", _clean_text("first_name", self.first_name)
        )
        object.__setattr__(self, "last_name", _clean_text("last_name", self.last_name))
        object.__setattr__(
            self, "connection_degrees", _degrees(self.connection_degrees)
        )
        object.__setattr__(self, "geo_urns", _facet_ids("geo_urns", self.geo_urns))
        object.__setattr__(
            self,
            "current_companies",
            _facet_ids("current_companies", self.current_companies),
        )
        object.__setattr__(
            self, "past_companies", _facet_ids("past_companies", self.past_companies)
        )
        object.__setattr__(
            self, "industries", _facet_ids("industries", self.industries)
        )
        object.__setattr__(self, "schools", _facet_ids("schools", self.schools))
        object.__setattr__(
            self,
            "service_categories",
            _facet_ids("service_categories", self.service_categories),
        )
        object.__setattr__(
            self, "profile_languages", _languages(self.profile_languages)
        )
        object.__setattr__(self, "extra", dict(self.extra))
        if not self.is_populated():
            raise FilterError(
                "a People search needs at least one filter, otherwise LinkedIn "
                "returns an unbounded result set"
            )

    def is_populated(self) -> bool:
        """True when at least one filter would reach LinkedIn."""
        return bool(self.to_params())

    def to_params(self) -> dict[str, str]:
        """Return the LinkedIn query parameters this filter set expresses."""
        params: dict[str, str] = {}
        if self.keywords:
            params["keywords"] = self.keywords
        if self.connection_degrees:
            params["network"] = _list_param(self.connection_degrees)
        if self.geo_urns:
            params["geoUrn"] = _list_param(self.geo_urns)
        if self.current_companies:
            params["currentCompany"] = _list_param(self.current_companies)
        if self.past_companies:
            params["pastCompany"] = _list_param(self.past_companies)
        if self.industries:
            params["industry"] = _list_param(self.industries)
        if self.schools:
            params["schoolFilter"] = _list_param(self.schools)
        if self.service_categories:
            params["serviceCategory"] = _list_param(self.service_categories)
        if self.profile_languages:
            params["profileLanguage"] = _list_param(self.profile_languages)
        if self.title:
            params["titleFreeText"] = self.title
        if self.first_name:
            params["firstName"] = self.first_name
        if self.last_name:
            params["lastName"] = self.last_name
        params.update(self.extra)
        return params

    def describe(self) -> dict[str, Any]:
        """Return a JSON friendly record of the filters, for run bookkeeping."""
        return {name: value for name, value in self.to_params().items()}


@dataclass(frozen=True, slots=True)
class PostSearchFilters:
    """The content search filter set, validated on construction."""

    keywords: str | None = None
    date_posted: str | None = None
    sort_by: str | None = None
    author_industries: tuple[str, ...] = ()
    author_companies: tuple[str, ...] = ()
    from_member: tuple[str, ...] = ()
    extra: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "keywords", _clean_text("keywords", self.keywords))
        if self.date_posted is not None:
            window = str(self.date_posted).strip()
            if window not in DATE_POSTED_WINDOWS:
                raise FilterError(
                    f"date_posted {self.date_posted!r} is not one of "
                    f"{sorted(DATE_POSTED_WINDOWS)}"
                )
            object.__setattr__(self, "date_posted", window)
        if self.sort_by is not None:
            order = str(self.sort_by).strip()
            if order not in POST_SORT_ORDERS:
                raise FilterError(
                    f"sort_by {self.sort_by!r} is not one of {sorted(POST_SORT_ORDERS)}"
                )
            object.__setattr__(self, "sort_by", order)
        object.__setattr__(
            self,
            "author_industries",
            _facet_ids("author_industries", self.author_industries),
        )
        object.__setattr__(
            self,
            "author_companies",
            _facet_ids("author_companies", self.author_companies),
        )
        object.__setattr__(
            self, "from_member", _facet_ids("from_member", self.from_member)
        )
        object.__setattr__(self, "extra", dict(self.extra))
        if not self.is_populated():
            raise FilterError(
                "a post search needs at least one filter, otherwise LinkedIn "
                "returns an unbounded result set"
            )

    def is_populated(self) -> bool:
        """True when at least one filter would reach LinkedIn."""
        return bool(self.to_params())

    def to_params(self) -> dict[str, str]:
        """Return the LinkedIn query parameters this filter set expresses."""
        params: dict[str, str] = {}
        if self.keywords:
            params["keywords"] = self.keywords
        if self.date_posted:
            params["datePosted"] = _list_param((self.date_posted,))
        if self.sort_by:
            params["sortBy"] = f'"{self.sort_by}"'
        if self.author_industries:
            params["authorIndustry"] = _list_param(self.author_industries)
        if self.author_companies:
            params["authorCompany"] = _list_param(self.author_companies)
        if self.from_member:
            params["fromMember"] = _list_param(self.from_member)
        params.update(self.extra)
        return params

    def describe(self) -> dict[str, Any]:
        """Return a JSON friendly record of the filters, for run bookkeeping."""
        return {name: value for name, value in self.to_params().items()}


def encode_params(params: Mapping[str, str]) -> str:
    """Percent-encode a parameter mapping the way LinkedIn's own links do.

    `urllib.parse.urlencode` leaves brackets and quotes alone by default, and
    LinkedIn rejects the result. Encoding every reserved character keeps the
    JSON list values intact through the round trip.
    """
    from urllib.parse import quote

    return "&".join(
        f"{quote(str(key), safe='')}={quote(str(value), safe='')}"
        for key, value in params.items()
    )
