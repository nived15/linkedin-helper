"""Challenge and interstitial detection, with a clean halt when one is served.

LinkedIn answers a session it does not trust by serving something other than the
page that was asked for. It can be a checkpoint, a captcha, an authwall, a login
redirect or a banner telling the member their account is restricted. Every one of
those means the automation has already been noticed, so the only safe move is to
stop rather than to retry.

`inspect_page` reads the page and returns a `Detection`. `assert_page_clear` does
the same and raises a typed `DetectionHalt` when the page is not the one we asked
for. On a hit the account row moves to `challenged` or `logged_out` and a
`safety_events` row lands on the safety timeline. That is the whole handover to
the rest of the safety layer: `linkedin_mcp.safety.gate` already maps
`challenged` and `logged_out` onto typed refusals through `STATE_REFUSALS`, so
every later action refuses itself without this module being involved again.

Nothing here ever writes a state back to `active`. A challenge is cleared by a
human who has logged in and satisfied LinkedIn. No number of later clean
navigations proves that happened, so there is no auto-recovery path in this
module and there should never be one.

`open_challenges` and `recent_safety_events` are the read side. MCP-01 wires them
into the `worker_status` tool, which does not exist yet.
"""

from __future__ import annotations

import inspect
import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, ClassVar, Sequence
from urllib.parse import urlsplit

from linkedin_mcp.audit.log import (
    AuditLog,
    RefusalReason,
    get_audit_log,
    utc_timestamp,
)
from linkedin_mcp.safety.gate import (
    AccountChallenged,
    AccountLoggedOut,
    SafetyError,
    UnknownAccountError,
)

logger = logging.getLogger(__name__)

__all__ = [
    "BANNER_MARKERS",
    "CAPTCHA_MARKERS",
    "CAPTCHA_SELECTOR",
    "CAPTCHA_SELECTORS",
    "CHALLENGE_KIND",
    "CHALLENGE_PATH_MARKERS",
    "CHALLENGE_STATE",
    "ChallengeDetected",
    "ChallengeSignal",
    "DETECTION_EVENT_KINDS",
    "Detection",
    "DetectionHalt",
    "HALTED_STATES",
    "HaltRecord",
    "InterstitialDetected",
    "LOGGED_OUT_KIND",
    "LOGGED_OUT_STATE",
    "LOGIN_PATH_MARKERS",
    "LoggedOutDetected",
    "MAX_EVIDENCE_LENGTH",
    "PageUnreadable",
    "STATE_RANK",
    "UNREADABLE_KIND",
    "assert_page_clear",
    "halt_for",
    "inspect_page",
    "open_challenges",
    "recent_safety_events",
    "record_halt",
]

CHALLENGE_KIND = "challenge"
LOGGED_OUT_KIND = "logged_out"
UNREADABLE_KIND = "unreadable"

CHALLENGE_STATE = "challenged"
LOGGED_OUT_STATE = "logged_out"
HALTED_STATES: tuple[str, ...] = (CHALLENGE_STATE, LOGGED_OUT_STATE)

MAX_EVIDENCE_LENGTH = 200

LOGIN_PATH_MARKERS: tuple[str, ...] = (
    "/uas/login",
    "/checkpoint/lg/",
    "/checkpoint/rp/",
    "/login",
)
"""URL paths that mean the session is simply signed out.

These are checked before the challenge markers because two of them sit under
`/checkpoint/`, which is otherwise a challenge marker. `/checkpoint/lg/` is the
login form and `/checkpoint/rp/` is the password reset flow, and neither is
LinkedIn asking the member to prove anything about their behaviour. Getting this
apart matters: the gate refuses a logged out account with `AccountLoggedOut`,
which a fresh login fixes, while `AccountChallenged` needs a human.
"""

CHALLENGE_PATH_MARKERS: tuple[str, ...] = (
    "/checkpoint/",
    "/challenge/",
    "/authwall",
    "/captcha",
)
"""URL paths that mean LinkedIn stopped us rather than signed us out.

An authwall counts as a challenge rather than a plain logout. It is LinkedIn
serving the anonymous visitor page to a request that carried our cookies, which
says the session was rejected rather than absent.
"""

STATE_RANK: dict[str, int] = {LOGGED_OUT_STATE: 1, CHALLENGE_STATE: 2}
"""How far a halted account has escalated. A row only ever moves up this ladder.

Anything not listed ranks zero, which covers `active` and the soft states
`paused` and `cooldown`. A challenge found on a paused account still escalates,
because paused is a decision we made and challenged is one LinkedIn made. A
second detection of the same thing does not move the rank, so it writes nothing.
`active` is absent on purpose: it has no rank to climb back down to.
"""

BANNER_MARKERS: tuple[str, ...] = (
    "we've restricted your account",
    "your account has been restricted",
    "let's do a quick security check",
    "quick security check",
    "unusual activity",
    "please verify",
    "verify your identity",
    "help us confirm it's you",
)
"""Warning banners LinkedIn shows on a page that otherwise looks normal.

Matched against the page text with tags stripped, whitespace collapsed, case
folded and curly apostrophes rewritten as straight ones, because LinkedIn ships
both apostrophe characters.
"""

CAPTCHA_MARKERS: tuple[str, ...] = (
    "recaptcha",
    "arkoselabs",
    "funcaptcha",
    "hcaptcha",
    "arkose",
    "captcha",
)
"""Captcha vendors, most specific first so the reported marker is the useful one."""

CAPTCHA_SELECTORS: tuple[str, ...] = (
    'iframe[src*="recaptcha"]',
    'iframe[src*="arkoselabs"]',
    'iframe[src*="funcaptcha"]',
    'iframe[src*="hcaptcha"]',
    'iframe[title*="captcha"]',
    "#captcha-internal",
)
CAPTCHA_SELECTOR = ", ".join(CAPTCHA_SELECTORS)
"""One combined selector so the DOM probe costs a single round trip."""

TAG_PATTERN = re.compile(r"<[^>]+>")
IFRAME_PATTERN = re.compile(r"<iframe\b[^>]*>", re.IGNORECASE)
WHITESPACE_PATTERN = re.compile(r"\s+")
APOSTROPHES = str.maketrans({"\u2018": "'", "\u2019": "'", "\u02bc": "'"})


@dataclass(frozen=True, slots=True)
class ChallengeSignal:
    """One marker that matched, where it matched, and what it matched inside."""

    kind: str
    marker: str
    source: str
    evidence: str = ""

    def as_detail(self) -> dict[str, Any]:
        """Return the JSON-safe payload stored with the event."""
        return {
            "kind": self.kind,
            "marker": self.marker,
            "source": self.source,
            "evidence": self.evidence,
        }


@dataclass(frozen=True, slots=True)
class Detection:
    """What one page inspection saw.

    A bare boolean would not survive the trip to a tool result, so the markers
    that matched travel with the page they matched on. `probes` names the reads
    that produced an observation and `read_errors` names the reads that blew up,
    which is how a page nobody could read is told apart from a clean one.
    """

    url: str = ""
    signals: tuple[ChallengeSignal, ...] = ()
    probes: tuple[str, ...] = ()
    read_errors: tuple[str, ...] = ()

    @property
    def readable(self) -> bool:
        """Whether any probe returned something to look at."""
        return bool(self.probes)

    @property
    def clean(self) -> bool:
        """Whether the page was readable and carried no challenge marker."""
        return self.readable and not self.signals

    @property
    def signal(self) -> ChallengeSignal | None:
        """The marker the halt is named after."""
        return self.signals[0] if self.signals else None

    @property
    def kind(self) -> str:
        """`challenge`, `logged_out`, `unreadable`, or empty for a clean page."""
        if self.signals:
            return self.signals[0].kind
        return "" if self.readable else UNREADABLE_KIND

    def as_detail(self) -> dict[str, Any]:
        """Return the JSON-safe payload stored in `safety_events.detail_json`."""
        detail: dict[str, Any] = {
            "url": self.url,
            "kind": self.kind,
            "probes": list(self.probes),
        }
        if self.signals:
            detail["signals"] = [signal.as_detail() for signal in self.signals]
            detail.update(self.signals[0].as_detail())
        if self.read_errors:
            detail["read_errors"] = list(self.read_errors)
        return detail


@dataclass(frozen=True, slots=True)
class HaltRecord:
    """What the write side actually did, so a caller can report it honestly."""

    account_id: int
    previous_state: str
    state: str
    transitioned: bool
    event_written: bool
    refusal_logged: bool = False

    def as_detail(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "previous_state": self.previous_state,
            "account_state": self.state,
            "transitioned": self.transitioned,
            "event_written": self.event_written,
            "refusal_logged": self.refusal_logged,
        }


class DetectionHalt(SafetyError):
    """A navigation that must not continue.

    Subclasses bind themselves to the account state they imply and to the
    `RefusalReason` the audit log already defines, so the enum stays the single
    vocabulary for why work stopped.
    """

    kind: ClassVar[str] = ""
    headline: ClassVar[str] = "LinkedIn did not serve the page we asked for"
    account_state: ClassVar[str | None] = None
    event_kind: ClassVar[str] = "interstitial_detected"
    severity: ClassVar[str] = "warning"
    refusal_reason: ClassVar[RefusalReason | None] = None

    def __init__(self, detection: Detection, message: str | None = None) -> None:
        self.detection = detection
        self.record: HaltRecord | None = None
        self.record_error: str | None = None
        super().__init__(message or self._default_message())

    def _default_message(self) -> str:
        where = f" at {self.detection.url}" if self.detection.url else ""
        signal = self.detection.signal
        if signal is None:
            return f"{self.headline}{where}"
        return f"{self.headline}{where}: {signal.marker!r} matched in the {signal.source}"

    def as_detail(self) -> dict[str, Any]:
        """Return the payload written to `safety_events.detail_json`."""
        detail = self.detection.as_detail()
        detail["message"] = str(self)
        if self.refusal_reason is not None:
            detail["reason"] = self.refusal_reason.value
        if self.account_state is not None:
            detail["account_state"] = self.account_state
        return detail

    def to_result(self) -> dict[str, Any]:
        """Return the MCP tool result for this halt.

        Tools never raise, so this is the edge that turns the halt into an
        ordinary result. Returning it is not swallowing the halt: the account
        state is already flipped, so the gate refuses every later action on its
        own. `audit_logged` is only true when the refusal row already landed.
        """
        payload: dict[str, Any] = {
            "status": "error",
            "message": f"Session expired: {self}",
            "kind": self.kind,
            "detection": self.detection.as_detail(),
        }
        if self.refusal_reason is not None:
            payload["reason"] = self.refusal_reason.value
        if self.record is not None:
            payload.update(self.record.as_detail())
            payload["audit_logged"] = self.record.refusal_logged
        if self.record_error is not None:
            payload["audit_error"] = self.record_error
        return payload


class InterstitialDetected(DetectionHalt):
    """LinkedIn served an interstitial instead of the page we asked for."""


class ChallengeDetected(InterstitialDetected):
    """A checkpoint, captcha, authwall or restriction banner was served."""

    kind = CHALLENGE_KIND
    headline = "LinkedIn served a challenge"
    account_state = CHALLENGE_STATE
    event_kind = "challenge_detected"
    severity = AccountChallenged.severity
    refusal_reason = RefusalReason.ACCOUNT_CHALLENGED


class LoggedOutDetected(InterstitialDetected):
    """The session is signed out and LinkedIn asked for a login."""

    kind = LOGGED_OUT_KIND
    headline = "LinkedIn served a login page"
    account_state = LOGGED_OUT_STATE
    event_kind = "session_logged_out"
    severity = AccountLoggedOut.severity
    refusal_reason = RefusalReason.ACCOUNT_LOGGED_OUT


class PageUnreadable(DetectionHalt):
    """Nothing on the page could be read, so it cannot be called clean.

    This halts like a challenge but deliberately leaves the account state alone.
    A page we failed to read is not evidence that LinkedIn challenged anyone, and
    flipping the row on that would need a human to clear a challenge that may
    never have happened.
    """

    kind = UNREADABLE_KIND
    headline = "the page could not be read, so it cannot be cleared"
    event_kind = "page_unreadable"


HALT_CLASSES: dict[str, type[DetectionHalt]] = {
    CHALLENGE_KIND: ChallengeDetected,
    LOGGED_OUT_KIND: LoggedOutDetected,
    UNREADABLE_KIND: PageUnreadable,
}

DETECTION_EVENT_KINDS: tuple[str, ...] = tuple(
    halt.event_kind for halt in HALT_CLASSES.values()
)
"""The `safety_events.kind` values this module writes."""


def halt_for(detection: Detection) -> DetectionHalt | None:
    """Return the typed halt a detection implies, or None when it is clean."""
    if detection.clean:
        return None
    return HALT_CLASSES[detection.kind](detection)


async def inspect_page(page: Any) -> Detection:
    """Read the page and report every challenge marker that matched.

    This runs after every navigation, so it is ordered by cost. The URL settles
    almost every real case and costs nothing. Frames are a plain attribute. Only
    when the URL is inconclusive does this reach into the DOM, and then it makes
    one selector call and one `content()` call rather than one per marker.

    Every probe is optional. A stand-in page exposing nothing but `url` is
    inspected from the URL alone, and a probe that raises is recorded in
    `read_errors` rather than being treated as an all clear.
    """
    probes: list[str] = []
    errors: list[str] = []

    url, error = _read_url(page)
    if error:
        errors.append(error)
    if url:
        probes.append("url")
        signal = _url_signal(url)
        if signal is not None:
            return Detection(url, (signal,), tuple(probes), tuple(errors))

    signal, ran, error = _frame_signal(page)
    _note(probes, errors, "frames", ran, error)
    if signal is not None:
        return Detection(url, (signal,), tuple(probes), tuple(errors))

    signal, ran, error = await _selector_signal(page)
    _note(probes, errors, "captcha_selector", ran, error)
    if signal is not None:
        return Detection(url, (signal,), tuple(probes), tuple(errors))

    signals, ran, error = await _content_signals(page)
    _note(probes, errors, "content", ran, error)
    if signals:
        return Detection(url, signals, tuple(probes), tuple(errors))

    if not ran:
        signal, ran, error = await _title_signal(page)
        _note(probes, errors, "title", ran, error)
        if signal is not None:
            return Detection(url, (signal,), tuple(probes), tuple(errors))

    return Detection(url, (), tuple(probes), tuple(errors))


async def assert_page_clear(
    page: Any,
    *,
    account_id: int | None = None,
    conn: sqlite3.Connection | None = None,
    action_type: str | None = None,
    moment: datetime | None = None,
) -> Detection:
    """Inspect the page and raise the typed halt when it is not ours.

    Callers must not swallow the halt. Catching it to return a tool result is
    fine, because the account state is already flipped by then and the gate
    refuses everything that follows. Catching it to carry on with the run is not.

    Args:
        page: Anything exposing `url`, and optionally `frames`, `query_selector`,
            `content` and `title`.
        account_id: Account the navigation ran as. The halt is only recorded when
            this is supplied. Navigation helpers do not guess it, because writing
            a challenge against the wrong account would stop a session nobody
            challenged.
        conn: Connection to write through. Defaults to the process-wide audit
            log's connection.
        action_type: Action the navigation was part of. When given, the halt also
            appends a refused row to `actions_log` so a stopped run is explained
            by a row rather than by silence.
        moment: Detection time, defaulting to now.
    """
    detection = await inspect_page(page)
    halt = halt_for(detection)
    if halt is None:
        return detection

    if account_id is None:
        logger.error(
            "Halting on %s at %s without recording it: no account context was given",
            halt.kind or "an unreadable page",
            detection.url,
        )
    else:
        try:
            halt.record = record_halt(
                halt,
                account_id=account_id,
                conn=conn,
                action_type=action_type,
                moment=moment,
            )
        except Exception as exc:
            # A halt we could not write down is still a halt. Losing the row
            # costs an explanation, and continuing would cost the account.
            logger.error("Failed to record the %s halt: %s", halt.kind, exc)
            halt.record_error = str(exc)
    raise halt


def record_halt(
    halt: DetectionHalt,
    *,
    account_id: int,
    conn: sqlite3.Connection | None = None,
    action_type: str | None = None,
    moment: datetime | None = None,
) -> HaltRecord:
    """Flip the account up the halt ladder and put the halt on the safety timeline.

    The state change and the `safety_events` row are one transaction, because an
    account stopped with nothing on the timeline is unexplainable and a timeline
    entry against a still-active account is a lie. The insert opens a write
    transaction, so a failure rolls back rather than leaving one in flight: a
    wedged transaction on this shared connection would block every later gate
    call and hold the write lock against other processes.

    The row only ever climbs `STATE_RANK`. A repeated detection is a no-op rather
    than a second flip and a second alert. A challenge found on a `paused` or
    `cooldown` account still escalates, because a soft state we chose must not
    hide a hard one LinkedIn chose. A logged out account that turns out to be
    challenged escalates too, and never the other way round. Nothing here writes
    `active` back.
    """
    log = AuditLog(conn) if conn is not None else get_audit_log()
    when = moment or datetime.now(timezone.utc)
    record = _flag_account(log.connection, account_id, halt, when)

    if action_type and halt.refusal_reason is not None:
        detail = halt.as_detail()
        detail.update(record.as_detail())
        log.record_refusal(
            account_id,
            action_type,
            halt.refusal_reason,
            detail=detail,
            occurred_at=when,
        )
        record = HaltRecord(
            record.account_id,
            record.previous_state,
            record.state,
            record.transitioned,
            record.event_written,
            refusal_logged=True,
        )
    return record


def open_challenges(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return every account LinkedIn has stopped, newest event first.

    This is the read side MCP-01 wires into `worker_status`. Each row carries the
    account state and the most recent detection event behind it, so the answer to
    "why is this worker doing nothing" is one query rather than a join the caller
    has to remember to write.
    """
    placeholders = ", ".join("?" for _ in HALTED_STATES)
    kinds = ", ".join("?" for _ in DETECTION_EVENT_KINDS)
    rows = _rows(
        conn,
        f"""
        SELECT
            accounts.id AS account_id,
            accounts.label AS label,
            accounts.state AS state,
            accounts.updated_at AS state_changed_at,
            events.id AS event_id,
            events.kind AS kind,
            events.severity AS severity,
            events.detail_json AS detail_json,
            events.occurred_at AS occurred_at
        FROM accounts
        LEFT JOIN safety_events AS events ON events.id = (
            SELECT id FROM safety_events
            WHERE account_id = accounts.id AND kind IN ({kinds})
            ORDER BY occurred_at DESC, id DESC
            LIMIT 1
        )
        WHERE accounts.state IN ({placeholders})
        ORDER BY accounts.id
        """,
        (*DETECTION_EVENT_KINDS, *HALTED_STATES),
    )
    return [_decode_detail(row) for row in rows]


def recent_safety_events(
    conn: sqlite3.Connection,
    account_id: int,
    *,
    limit: int = 20,
    kinds: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """Return an account's recent safety timeline, newest first.

    `kinds` defaults to every kind, so a caller sees gate refusals and detection
    halts on the same timeline. Pass `DETECTION_EVENT_KINDS` for detections only.
    """
    sql = (
        "SELECT id, account_id, kind, severity, detail_json, occurred_at "
        "FROM safety_events WHERE account_id = ?"
    )
    params: list[Any] = [account_id]
    if kinds:
        sql += f" AND kind IN ({', '.join('?' for _ in kinds)})"
        params.extend(kinds)
    sql += " ORDER BY occurred_at DESC, id DESC LIMIT ?"
    params.append(limit)
    return [_decode_detail(row) for row in _rows(conn, sql, params)]


def _flag_account(
    conn: sqlite3.Connection,
    account_id: int,
    halt: DetectionHalt,
    moment: datetime,
) -> HaltRecord:
    if conn.in_transaction:
        raise SafetyError(
            "recording a challenge needs a connection with no transaction in "
            "flight; another writer left one open"
        )

    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT state FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()
        if row is None:
            raise UnknownAccountError(account_id)

        previous = str(row[0])
        previous_rank = STATE_RANK.get(previous, 0)
        target_rank = STATE_RANK.get(halt.account_state or "", 0)
        transitioned = target_rank > previous_rank
        state = halt.account_state if transitioned else previous
        # An unreadable page has no target state, so it never moves the row. It
        # still belongs on the timeline the first time, and it is only noise once
        # the account is already stopped.
        escalated = transitioned or (
            halt.account_state is None and previous not in HALTED_STATES
        )

        if transitioned:
            conn.execute(
                "UPDATE accounts SET state = ?, updated_at = ? WHERE id = ?",
                (state, utc_timestamp(moment), account_id),
            )
        if escalated:
            conn.execute(
                """
                INSERT INTO safety_events (account_id, kind, severity, detail_json, occurred_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    account_id,
                    halt.event_kind,
                    halt.severity,
                    json.dumps(halt.as_detail(), default=str, sort_keys=True),
                    utc_timestamp(moment),
                ),
            )
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()

    if transitioned:
        logger.error(
            "Account %s moved to %s after %s; only a human clears this",
            account_id,
            state,
            halt,
        )
    return HaltRecord(account_id, previous, str(state), transitioned, escalated)


def _read_url(page: Any) -> tuple[str, str | None]:
    try:
        value = getattr(page, "url", "") or ""
    except Exception as exc:
        return "", f"url: {exc}"
    if callable(value):
        return "", "url: exposed as a callable rather than a string"
    return str(value), None


def _url_signal(url: str) -> ChallengeSignal | None:
    """Classify the URL, checking the login paths before the challenge ones.

    Only the path is matched. A redirect target parked in the query string names
    a page we are not on, and matching it would stop a run over a link.

    Markers match whole path segments, never bare substrings. A member called
    Loginov has the profile `/in/loginov-dmitry`, and a substring test for
    `/login` would flag it. That would flip a healthy account into a state only a
    human can clear, so the cost of a loose match here is an evening of downtime.
    """
    segments = _path_segments((urlsplit(url).path or "").lower())
    for marker in LOGIN_PATH_MARKERS:
        if _path_contains(segments, marker):
            return ChallengeSignal(LOGGED_OUT_KIND, marker, "url", _trim(url))
    for marker in CHALLENGE_PATH_MARKERS:
        if _path_contains(segments, marker):
            return ChallengeSignal(CHALLENGE_KIND, marker, "url", _trim(url))
    return None


def _path_segments(path: str) -> tuple[str, ...]:
    return tuple(segment for segment in path.split("/") if segment)


def _path_contains(segments: tuple[str, ...], marker: str) -> bool:
    """Report whether the marker's segments appear in order inside the path."""
    wanted = _path_segments(marker)
    if not wanted:
        return False
    span = len(wanted)
    return any(
        segments[start : start + span] == wanted
        for start in range(len(segments) - span + 1)
    )


def _frame_signal(page: Any) -> tuple[ChallengeSignal | None, bool, str | None]:
    try:
        frames = getattr(page, "frames", None)
    except Exception as exc:
        return None, False, f"frames: {exc}"
    if frames is None or callable(frames):
        return None, False, None

    try:
        candidates = list(frames)
    except Exception as exc:
        return None, False, f"frames: {exc}"

    for frame in candidates:
        haystack = " ".join(
            str(value)
            for value in (getattr(frame, "url", ""), getattr(frame, "name", ""))
            if isinstance(value, str)
        ).lower()
        marker = _first_marker(haystack, CAPTCHA_MARKERS)
        if marker is not None:
            return (
                ChallengeSignal(CHALLENGE_KIND, marker, "frame", _trim(haystack)),
                True,
                None,
            )
    return None, True, None


async def _selector_signal(page: Any) -> tuple[ChallengeSignal | None, bool, str | None]:
    query = getattr(page, "query_selector", None)
    if not callable(query):
        return None, False, None
    try:
        handle = await _resolve(query(CAPTCHA_SELECTOR))
    except Exception as exc:
        return None, False, f"query_selector: {exc}"
    if handle is None:
        return None, True, None
    return (
        ChallengeSignal(CHALLENGE_KIND, "captcha", "frame", CAPTCHA_SELECTOR),
        True,
        None,
    )


async def _content_signals(
    page: Any,
) -> tuple[tuple[ChallengeSignal, ...], bool, str | None]:
    content = getattr(page, "content", None)
    if not callable(content):
        return (), False, None
    try:
        html = await _resolve(content())
    except Exception as exc:
        return (), False, f"content: {exc}"
    if not isinstance(html, str) or not html:
        return (), False, "content: returned nothing to read"

    signals: list[ChallengeSignal] = []
    for tag in IFRAME_PATTERN.findall(html):
        marker = _first_marker(tag.lower(), CAPTCHA_MARKERS)
        if marker is not None:
            signals.append(
                ChallengeSignal(CHALLENGE_KIND, marker, "frame", _trim(tag))
            )
            break

    text = _normalize(html)
    for marker in BANNER_MARKERS:
        if marker in text:
            signals.append(
                ChallengeSignal(CHALLENGE_KIND, marker, "banner", _excerpt(text, marker))
            )
            break
    return tuple(signals), True, None


async def _title_signal(page: Any) -> tuple[ChallengeSignal | None, bool, str | None]:
    title = getattr(page, "title", None)
    if not callable(title):
        return None, False, None
    try:
        text = await _resolve(title())
    except Exception as exc:
        return None, False, f"title: {exc}"
    if not isinstance(text, str) or not text:
        return None, False, "title: returned nothing to read"

    normalized = _normalize(text)
    marker = _first_marker(normalized, BANNER_MARKERS)
    if marker is not None:
        return (
            ChallengeSignal(CHALLENGE_KIND, marker, "title", _trim(normalized)),
            True,
            None,
        )
    return None, True, None


async def _resolve(value: Any) -> Any:
    """Await a probe result when the page is async, and take it as is when not."""
    if inspect.isawaitable(value):
        return await value
    return value


def _note(
    probes: list[str],
    errors: list[str],
    name: str,
    ran: bool,
    error: str | None,
) -> None:
    if ran:
        probes.append(name)
    if error:
        errors.append(error)


def _first_marker(haystack: str, markers: Sequence[str]) -> str | None:
    for marker in markers:
        if marker in haystack:
            return marker
    return None


def _normalize(text: str) -> str:
    stripped = TAG_PATTERN.sub(" ", text).translate(APOSTROPHES)
    return WHITESPACE_PATTERN.sub(" ", stripped).strip().lower()


def _excerpt(text: str, marker: str) -> str:
    start = max(0, text.find(marker) - 40)
    return _trim(text[start:])


def _trim(value: str) -> str:
    if len(value) <= MAX_EVIDENCE_LENGTH:
        return value
    return value[:MAX_EVIDENCE_LENGTH] + "..."


def _rows(
    conn: sqlite3.Connection,
    sql: str,
    params: Sequence[Any],
) -> list[dict[str, Any]]:
    """Return dict rows regardless of the connection's row factory."""
    cursor = conn.execute(sql, tuple(params))
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _decode_detail(row: dict[str, Any]) -> dict[str, Any]:
    raw = row.pop("detail_json", None)
    try:
        row["detail"] = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        row["detail"] = {"detail_json": raw}
    return row
