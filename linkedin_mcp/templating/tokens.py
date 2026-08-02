"""The one place a public template token is mapped onto stored lead data.

Template authors write `{firstName}`. The database column is `leads.first_name`.
Every one of those pairings is declared here and nowhere else, so the mapping is
reviewable in a single table rather than scattered through the renderer.

Three token namespaces exist:

* **Fixed lead tokens** listed in :data:`LEAD_TOKEN_COLUMNS`, each backed by a
  real column on `leads`.
* **`{cs_*}` custom fields** from DB-02, resolved by
  :func:`linkedin_mcp.leads.custom_field_tokens`, which already returns keys in
  `cs_` form. This module does not re-derive that prefix.
* **`{ai_*}` fragments** supplied by SEQ-05 (#23) at render time. They never
  come from the database.

`{mutualTotal}` is the honest exception. The DB-01 schema has no
mutual-connections column: `leads.connection_count` is the lead's own total
connection count and mapping the token onto it would put a confidently wrong
number into a message. The profile deep-scraper (#16/#17) owns adding that
column and owns `linkedin_mcp/scrape/`, so this module resolves `{mutualTotal}`
from the `cs_mutual_total` custom field, or from an explicit `extras` override.
When a real column lands, :data:`LEAD_TOKEN_COLUMNS` gains one line and the
alias below goes away.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from linkedin_mcp.leads.store import (
    CUSTOM_FIELD_PREFIX,
    Lead,
    custom_field_tokens,
    get_lead,
)


__all__ = [
    "AI_FRAGMENT_PREFIX",
    "CUSTOM_FIELD_PREFIX",
    "IDENTIFIER_PATTERN",
    "LEAD_TOKEN_COLUMNS",
    "MUTUAL_TOTAL_CUSTOM_FIELD",
    "MUTUAL_TOTAL_TOKEN",
    "RESERVED_KEYWORDS",
    "coerce_value",
    "fragment_name",
    "is_ai_token",
    "is_custom_field_token",
    "is_identifier",
    "is_known_token",
    "known_token_names",
    "lead_context",
    "lead_tokens",
    "normalise_token",
]

AI_FRAGMENT_PREFIX = "ai_"

LEAD_TOKEN_COLUMNS: Mapping[str, str] = MappingProxyType(
    {
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
)
"""Public token name to `leads` column. Also the `Lead` attribute name, which is
identical to the column name for every field in the table."""

MUTUAL_TOTAL_TOKEN = "mutualTotal"
MUTUAL_TOTAL_CUSTOM_FIELD = f"{CUSTOM_FIELD_PREFIX}mutual_total"
"""Where `{mutualTotal}` reads from until the schema grows a real column."""

RESERVED_KEYWORDS: frozenset[str] = frozenset({"IF", "ELSE", "END"})
"""Words a brace group may open with to mean control flow, never a variable."""

IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def is_identifier(name: str) -> bool:
    return bool(IDENTIFIER_PATTERN.match(name))


def is_custom_field_token(name: str) -> bool:
    """True for `{cs_something}`, false for a bare `{cs_}`."""
    return name.lower().startswith(CUSTOM_FIELD_PREFIX) and len(name) > len(
        CUSTOM_FIELD_PREFIX
    )


def is_ai_token(name: str) -> bool:
    """True for `{ai_something}`, the SEQ-05 fragment namespace."""
    return name.lower().startswith(AI_FRAGMENT_PREFIX) and len(name) > len(
        AI_FRAGMENT_PREFIX
    )


def fragment_name(name: str) -> str:
    """Strip the `ai_` prefix from a fragment token, leaving the bare name."""
    lowered = name.lower()
    if lowered.startswith(AI_FRAGMENT_PREFIX):
        return lowered[len(AI_FRAGMENT_PREFIX) :]
    return lowered


def is_known_token(name: str) -> bool:
    """True when the renderer knows how a token could ever be resolved."""
    if name in LEAD_TOKEN_COLUMNS or name == MUTUAL_TOTAL_TOKEN:
        return True
    return is_custom_field_token(name) or is_ai_token(name)


def known_token_names() -> tuple[str, ...]:
    """The fixed token names, for error messages. Excludes the open namespaces."""
    return (*sorted(LEAD_TOKEN_COLUMNS), MUTUAL_TOTAL_TOKEN)


def normalise_token(name: str) -> str:
    """Return the lookup key for a token.

    Fixed lead tokens are case-sensitive camelCase, because `{firstName}` and
    `{firstname}` being different is a typo worth catching loudly. The `cs_` and
    `ai_` namespaces are lowercased, matching
    :func:`linkedin_mcp.leads.normalise_custom_field_key`, so `{cs_Industry}`
    finds the field stored as `industry`.
    """
    if name in LEAD_TOKEN_COLUMNS or name == MUTUAL_TOTAL_TOKEN:
        return name
    if is_custom_field_token(name) or is_ai_token(name):
        return name.lower()
    return name


def coerce_value(value: Any) -> str:
    """Render a stored value as template text.

    None becomes the empty string, which the renderer reads as absent. Integers
    such as a mutual count become their decimal form. Everything is stripped, so
    a whitespace-only scrape counts as absent rather than producing "Hi ,".
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


def lead_tokens(lead: Lead) -> dict[str, str]:
    """Return the fixed `{...}` tokens for a lead, without touching the database.

    Only columns are read here. `{cs_*}` and `{mutualTotal}` need a connection,
    so they live in :func:`lead_context`.
    """
    return {
        token: coerce_value(getattr(lead, column))
        for token, column in LEAD_TOKEN_COLUMNS.items()
    }


def lead_context(
    conn: sqlite3.Connection,
    lead: Lead | int,
    *,
    extras: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """Build the full token context for one lead.

    Layered lowest to highest: lead columns, then `{cs_*}` custom fields from
    DB-02, then the `{mutualTotal}` alias, then any caller `extras`. Extras win,
    so a campaign step that already counted mutuals in the page it scraped can
    pass the number straight in.

    Returns None-free, stripped strings. A key that is present but empty means
    absent, which is the same thing as a key that is not there at all.
    """
    resolved = lead if isinstance(lead, Lead) else get_lead(conn, lead)
    if resolved is None:
        raise LookupError(f"lead {lead!r} does not exist")

    context = lead_tokens(resolved)
    context.update(
        {
            token: coerce_value(value)
            for token, value in custom_field_tokens(conn, resolved.id).items()
        }
    )
    context[MUTUAL_TOTAL_TOKEN] = context.get(MUTUAL_TOTAL_CUSTOM_FIELD, "")

    for key, value in (extras or {}).items():
        context[normalise_token(key)] = coerce_value(value)
    return context
