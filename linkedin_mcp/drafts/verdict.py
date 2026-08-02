"""The ICP verdict: `{match, score, reason}` and nothing else.

`ai_drafts.verdict_json` is commented in `0001_init.sql` as the slot for an
Ideal Customer Profile evaluation, and this module is the only thing that reads
or writes it.

Why parsing is strict
---------------------
A verdict decides whether a real person receives a real invitation, and an
invitation is a scarce, rate-limited, unrecallable action. The single failure
mode worth engineering against is a truncated or reshaped model response being
read as "yes". So every one of `match`, `score` and `reason` must be present and
well formed, and anything else raises :class:`MalformedVerdictError` rather than
defaulting. There is deliberately no "assume no match and carry on" path either:
failing closed in silence would hide a broken client for weeks.

`match` accepts the shapes a language model actually emits: real booleans, 1/0,
and the strings true/false, yes/no, match/no_match. It does not accept `None`,
an empty string, or a bare score, because each of those is a client that did not
answer the question.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from linkedin_mcp.drafts.errors import MalformedVerdictError


__all__ = [
    "FALSE_WORDS",
    "MAX_REASON_LENGTH",
    "TRUE_WORDS",
    "VERDICT_KEYS",
    "Verdict",
    "coerce_match",
    "encode_verdict",
    "parse_verdict",
]

VERDICT_KEYS: tuple[str, ...] = ("match", "score", "reason")
"""Every key a verdict must carry. All three are required."""

TRUE_WORDS: frozenset[str] = frozenset({"true", "yes", "y", "1", "match", "matched"})
FALSE_WORDS: frozenset[str] = frozenset(
    {"false", "no", "n", "0", "no_match", "nomatch", "not_a_match"}
)

MAX_REASON_LENGTH = 500
"""Longest reason stored. Matches the audit log's per-value cap."""


@dataclass(frozen=True, slots=True)
class Verdict:
    """One ICP decision about one lead."""

    match: bool
    score: float
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"match": self.match, "score": self.score, "reason": self.reason}

    @property
    def sublist(self) -> str:
        """Where this verdict sends the lead: `successful` or `failed`.

        Spelled as the plain strings the `campaign_leads` CHECK constraint uses.
        :mod:`linkedin_mcp.drafts.routing` performs the move through SEQ-01's
        public transitions; this property only names the destination.
        """
        return "successful" if self.match else "failed"


def coerce_match(value: Any) -> bool:
    """Return the boolean a `match` field means, or raise.

    `None`, missing and unrecognised values all raise. A verdict that did not
    answer the question is not a "no", it is a broken client.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if value in (0, 1):
            return bool(value)
        raise MalformedVerdictError(f"'match' must be a boolean; got the number {value!r}")
    if isinstance(value, str):
        folded = value.strip().lower()
        if folded in TRUE_WORDS:
            return True
        if folded in FALSE_WORDS:
            return False
    raise MalformedVerdictError(f"'match' must be a boolean; got {value!r}")


def _coerce_score(value: Any) -> float:
    if isinstance(value, bool) or value is None:
        raise MalformedVerdictError(
            f"'score' must be a number between 0 and 1; got {value!r}"
        )
    try:
        score = float(value)
    except (TypeError, ValueError):
        raise MalformedVerdictError(
            f"'score' must be a number between 0 and 1; got {value!r}"
        ) from None
    if not math.isfinite(score):
        raise MalformedVerdictError(f"'score' must be a finite number; got {value!r}")
    if not 0.0 <= score <= 1.0:
        raise MalformedVerdictError(
            f"'score' must be between 0 and 1 inclusive; got {score!r}"
        )
    return score


def _coerce_reason(value: Any) -> str:
    if not isinstance(value, str):
        raise MalformedVerdictError(f"'reason' must be a string; got {value!r}")
    reason = value.strip()
    if not reason:
        raise MalformedVerdictError(
            "'reason' is empty; a verdict nobody can audit is not a verdict"
        )
    return reason[:MAX_REASON_LENGTH]


def parse_verdict(
    raw: Mapping[str, Any] | str | None,
    *,
    draft_id: int | None = None,
) -> Verdict:
    """Return the :class:`Verdict` a mapping or JSON string carries, or raise.

    Accepts both the mapping an MCP client submits and the JSON string read back
    out of `ai_drafts.verdict_json`, so one set of rules binds the write path and
    the read path. A row hand-edited in the database therefore fails here rather
    than routing a lead on a shape nothing validated.
    """
    if raw is None:
        raise MalformedVerdictError("no verdict was supplied", draft_id=draft_id)

    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            raise MalformedVerdictError("no verdict was supplied", draft_id=draft_id)
        try:
            decoded: Any = json.loads(text)
        except json.JSONDecodeError as error:
            raise MalformedVerdictError(
                f"verdict is not valid JSON: {error}", draft_id=draft_id
            ) from None
    else:
        decoded = raw

    if not isinstance(decoded, Mapping):
        raise MalformedVerdictError(
            f"verdict must be an object carrying {list(VERDICT_KEYS)}; "
            f"got {type(decoded).__name__}",
            draft_id=draft_id,
        )

    missing = [key for key in VERDICT_KEYS if key not in decoded]
    if missing:
        raise MalformedVerdictError(
            f"verdict is missing {missing}; all of {list(VERDICT_KEYS)} are required",
            draft_id=draft_id,
        )

    try:
        return Verdict(
            match=coerce_match(decoded["match"]),
            score=_coerce_score(decoded["score"]),
            reason=_coerce_reason(decoded["reason"]),
        )
    except MalformedVerdictError as error:
        if error.draft_id is None and draft_id is not None:
            raise MalformedVerdictError(error.detail, draft_id=draft_id) from None
        raise


def encode_verdict(verdict: Verdict) -> str:
    """Return the JSON stored in `ai_drafts.verdict_json`."""
    return json.dumps(verdict.to_dict(), sort_keys=True)
