"""`notifications/resources/updated`, and the polling fallback for everyone else.

The requirement
---------------
MCP-04 (#27) asks for `notifications/resources/updated` "where the client
supports it", with a polling fallback elsewhere. The substance is the detection:
emitting unconditionally is a protocol violation against a client that never
asked, and skipping the fallback leaves that client with no way to notice a
change at all.

Which capability field this keys on
-----------------------------------
`mcp.types.ClientCapabilities` has exactly five fields in the version this repo
pins: `experimental`, `sampling`, `elicitation`, `roots` and `tasks`. There is no
`resources` field on the *client* side, because in the base MCP spec resource
updates are a server capability (`ServerCapabilities.resources.subscribe`) that a
client opts into by calling `resources/subscribe`. FastMCP 3.4.5 exposes no
subscription hook, so there is no `resources/subscribe` handler this server could
hang a subscriber list off.

That leaves exactly one field in the handshake where a client can say it wants
resource updates, so that is what this keys on:

    client_params.capabilities.experimental["resources"]["subscribe"]

:func:`client_supports_resource_updates` also checks
`client_params.capabilities.resources.subscribe` first, so that if a later MCP
revision promotes this out of `experimental` the check starts reading the
standard field with no change here. Nothing else counts as support. In
particular, the presence of `session.send_resource_updated` does not: every
`ServerSession` has that method whether or not the client on the other end will
do anything sensible with what it sends.

The fallback
------------
A client that did not declare support is told, in the body of every resource it
reads, that it will not be pushed to, how long to wait before re-reading
(:data:`~linkedin_mcp.resources.contract.DEFAULT_POLL_AFTER_SECONDS`), and which
of the URIs it has previously read have changed since it last looked. That last
part is the useful half: a poller that has to re-read twelve URIs to find the one
that moved is a poller nobody will run at thirty second intervals.

Revisions
---------
"Changed" is decided by a cheap SQL fingerprint per URI rather than by
re-rendering the resource and diffing it. Rendering `linkedin://safety/today`
runs eleven pairs of budget queries; doing that on every read of every other
resource to find out whether it moved would make the read surface quadratic in
its own size.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from pydantic import AnyUrl

from linkedin_mcp.resources.contract import (
    ANALYTICS_WEEKLY_URI,
    CAMPAIGNS_URI,
    DEFAULT_POLL_AFTER_SECONDS,
    DRAFTS_PENDING_URI,
    INBOX_UNREAD_URI,
    LEADS_ACTIVE_URI,
    SAFETY_TODAY_URI,
    STATS_DAILY_URI,
    TEMPLATES_URI,
    WORKER_STATUS_URI,
    campaign_funnel_uri,
    campaign_uri,
    lead_uri,
)

logger = logging.getLogger(__name__)

__all__ = [
    "CLIENT_CAPABILITY_KEY",
    "CLIENT_CAPABILITY_PATH",
    "Delivery",
    "ResourceUpdateNotifier",
    "as_notification_uri",
    "client_supports_resource_updates",
    "resource_revisions",
    "session_key",
    "templated_revisions",
]

CLIENT_CAPABILITY_KEY = "resources"
"""The `ClientCapabilities.experimental` key a client declares support under."""

CLIENT_CAPABILITY_PATH = "capabilities.experimental.resources.subscribe"
"""Human-readable form of the field, quoted in every `updates.reason`.

Written down so a client author reading a payload that says `push: false` can
see what to send in `initialize` to change that, without reading this module.
"""

REASON_PUSH = f"client declared {CLIENT_CAPABILITY_PATH}"
REASON_NO_SESSION = "no live MCP session; resource was read in-process"
REASON_NO_CAPABILITY = f"client did not declare {CLIENT_CAPABILITY_PATH}"
REASON_SEND_FAILED = "notification could not be delivered on this session"


def _capability_block(session: Any) -> tuple[bool, str]:
    """Return whether a session's client asked for resource updates, and why."""
    params = getattr(session, "client_params", None)
    capabilities = getattr(params, "capabilities", None)
    if capabilities is None:
        return False, REASON_NO_CAPABILITY

    # Standard field first, so this starts working on its own the day a later
    # MCP revision moves resource subscription onto ClientCapabilities.
    standard = getattr(capabilities, "resources", None)
    if standard is not None:
        return bool(getattr(standard, "subscribe", False)), (
            REASON_PUSH
            if bool(getattr(standard, "subscribe", False))
            else REASON_NO_CAPABILITY
        )

    experimental = getattr(capabilities, "experimental", None) or {}
    try:
        block = experimental.get(CLIENT_CAPABILITY_KEY) or {}
        declared = bool(block.get("subscribe"))
    except AttributeError:
        return False, REASON_NO_CAPABILITY
    return declared, REASON_PUSH if declared else REASON_NO_CAPABILITY


def client_supports_resource_updates(session: Any) -> tuple[bool, str]:
    """Return `(supported, reason)` for one MCP session.

    A missing session is not an error. Resources are read in-process by tests
    and by anything that imports the server as a library, and neither has a
    client to notify; both get the polling answer.
    """
    if session is None:
        return False, REASON_NO_SESSION
    if not callable(getattr(session, "send_resource_updated", None)):
        return False, REASON_NO_SESSION
    return _capability_block(session)


def as_notification_uri(uri: str) -> AnyUrl:
    """Coerce a resource URI to the `AnyUrl` `send_resource_updated` requires.

    `ResourceUpdatedNotificationParams.uri` is typed as `AnyUrl`, and pydantic
    rejects a bare string there. Doing the coercion in one named place means a
    URI that pydantic cannot parse fails here, in the notifier, rather than in
    the middle of a resource read.
    """
    return AnyUrl(uri)


def session_key(session: Any) -> str:
    """Return a stable per-session key for the notifier's revision memory.

    Falls back to the identity of the session object, which is enough to keep
    two concurrent clients from being told about each other's changes.
    """
    if session is None:
        return "in-process"
    for attribute in ("session_id", "client_id"):
        value = getattr(session, attribute, None)
        if isinstance(value, str) and value:
            return value
    return f"session-{id(session):x}"


# --------------------------------------------------------------------------
# Revision fingerprints
# --------------------------------------------------------------------------


def _scalar(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> str:
    """Return one aggregate as a string, treating a missing table as empty.

    A resource must not fail because a fingerprint could not be taken. The worst
    a swallowed error can do here is make a URI look unchanged, which degrades
    to the polling behaviour a client without the capability already has.
    """
    try:
        row = conn.execute(sql, params).fetchone()
    except sqlite3.Error as error:  # pragma: no cover - defensive
        logger.debug("revision query failed (%s): %s", error, sql)
        return "?"
    if row is None:
        return "0"
    return "|".join("" if value is None else str(value) for value in tuple(row))


def _table_revision(
    conn: sqlite3.Connection,
    table: str,
    *,
    account_id: int | None = None,
    extra: str = "",
) -> str:
    """Count plus highest id for one table, scoped to an account when it has one."""
    columns = f"COUNT(*), COALESCE(MAX(id), 0){(', ' + extra) if extra else ''}"
    if account_id is None:
        return _scalar(conn, f"SELECT {columns} FROM {table}")
    return _scalar(
        conn,
        f"SELECT {columns} FROM {table} WHERE account_id = ?",
        (account_id,),
    )


def _campaigns_revision(conn: sqlite3.Connection, account_id: int) -> str:
    """Fingerprint every campaign this account owns, statuses included.

    A status flip writes no new row and moves no id, so a count-and-max
    fingerprint would report a campaign that had just been started as unchanged.
    That is the single most important transition on this whole surface.
    """
    return _scalar(
        conn,
        """
        SELECT COUNT(*), COALESCE(GROUP_CONCAT(marker, ','), '')
        FROM (
            SELECT id || ':' || status || ':' || approval_mode AS marker
            FROM campaigns
            WHERE account_id = ?
            ORDER BY id
        )
        """,
        (account_id,),
    )


def _campaign_leads_revision(
    conn: sqlite3.Connection, campaign_id: int | None = None
) -> str:
    """Fingerprint the funnel: sublist populations move without new rows."""
    where = "" if campaign_id is None else " WHERE campaign_id = ?"
    params: tuple[Any, ...] = () if campaign_id is None else (campaign_id,)
    return _scalar(
        conn,
        f"""
        SELECT COUNT(*), COALESCE(GROUP_CONCAT(marker, ','), '')
        FROM (
            SELECT sublist || '=' || COUNT(*) AS marker
            FROM campaign_leads{where}
            GROUP BY sublist
            ORDER BY sublist
        )
        """,
        params,
    )


def _drafts_revision(conn: sqlite3.Connection, account_id: int) -> str:
    """Fingerprint `ai_drafts` by status, because approval rewrites a row."""
    return _scalar(
        conn,
        """
        SELECT COUNT(*), COALESCE(GROUP_CONCAT(marker, ','), '')
        FROM (
            SELECT status || '=' || COUNT(*) AS marker
            FROM ai_drafts
            WHERE account_id = ?
            GROUP BY status
            ORDER BY status
        )
        """,
        (account_id,),
    )


def _worker_revision(conn: sqlite3.Connection, account_id: int) -> str:
    """Fingerprint the heartbeat and the queue depths behind `worker/status`.

    `last_tick_at` is in here deliberately. A worker that is ticking normally
    changes this fingerprint every tick, which is the point: `worker/status` is
    the one resource whose answer changes purely with the passage of time.
    """
    heartbeat = _scalar(
        conn,
        """
        SELECT COUNT(*), COALESCE(MAX(last_tick_at), ''), COALESCE(
            GROUP_CONCAT(worker_id || ':' || status, ','), ''
        )
        FROM worker_heartbeat
        WHERE account_id = ?
        """,
        (account_id,),
    )
    jobs = _scalar(
        conn,
        """
        SELECT COUNT(*), COALESCE(GROUP_CONCAT(marker, ','), '')
        FROM (
            SELECT state || '=' || COUNT(*) AS marker
            FROM jobs
            WHERE account_id = ?
            GROUP BY state
            ORDER BY state
        )
        """,
        (account_id,),
    )
    return f"{heartbeat}/{jobs}"


def _leads_revision(conn: sqlite3.Connection, account_id: int) -> str:
    leads = _table_revision(conn, "leads", account_id=account_id)
    blocked = _scalar(conn, "SELECT COUNT(*) FROM blacklist")
    return f"{leads}/{blocked}"


def _actions_revision(conn: sqlite3.Connection, account_id: int) -> str:
    """Fingerprint `actions_log`, which is append-only, so count and max suffice."""
    return _table_revision(conn, "actions_log", account_id=account_id)


def _safety_revision(conn: sqlite3.Connection, account_id: int) -> str:
    """Budgets move with the audit log; a halt moves the account state too."""
    actions = _actions_revision(conn, account_id)
    events = _table_revision(conn, "safety_events", account_id=account_id)
    account = _scalar(
        conn,
        "SELECT state, account_age_days, updated_at FROM accounts WHERE id = ?",
        (account_id,),
    )
    limits = _scalar(
        conn,
        """
        SELECT COUNT(*), COALESCE(GROUP_CONCAT(marker, ','), '')
        FROM (
            SELECT action_type || ':' || COALESCE(daily_cap, -1) || ':'
                || COALESCE(weekly_cap, -1) || ':' || enabled AS marker
            FROM account_limits
            WHERE account_id = ?
            ORDER BY action_type
        )
        """,
        (account_id,),
    )
    return f"{actions}/{events}/{account}/{limits}"


def resource_revisions(conn: sqlite3.Connection, account_id: int) -> dict[str, str]:
    """Return a fingerprint for each of the nine static URIs.

    Two URIs that read the same rows deliberately share a fingerprint:
    `linkedin://stats/daily` and `linkedin://analytics/weekly` are both views of
    `actions_log`, so a row appended there changes both, which is true.
    """
    campaigns = _campaigns_revision(conn, account_id)
    funnels = _campaign_leads_revision(conn)
    actions = _actions_revision(conn, account_id)
    return {
        CAMPAIGNS_URI: f"{campaigns}/{funnels}",
        LEADS_ACTIVE_URI: _leads_revision(conn, account_id),
        DRAFTS_PENDING_URI: _drafts_revision(conn, account_id),
        INBOX_UNREAD_URI: _table_revision(conn, "messages", account_id=account_id),
        WORKER_STATUS_URI: _worker_revision(conn, account_id),
        STATS_DAILY_URI: actions,
        SAFETY_TODAY_URI: _safety_revision(conn, account_id),
        ANALYTICS_WEEKLY_URI: f"{actions}/{funnels}",
        TEMPLATES_URI: _table_revision(conn, "templates", account_id=account_id),
    }


def templated_revisions(
    conn: sqlite3.Connection,
    account_id: int,
    *,
    campaign_ids: tuple[int, ...] = (),
    lead_ids: tuple[int, ...] = (),
) -> dict[str, str]:
    """Return fingerprints for the concrete templated URIs a session has read.

    A templated URI has no fingerprint until somebody names an id, so the
    notifier only tracks the ones it has actually served.
    """
    revisions: dict[str, str] = {}
    campaigns = _campaigns_revision(conn, account_id)
    for campaign_id in campaign_ids:
        funnel = _campaign_leads_revision(conn, campaign_id)
        revisions[campaign_uri(campaign_id)] = f"{campaigns}/{funnel}"
        revisions[campaign_funnel_uri(campaign_id)] = funnel
    for lead_id in lead_ids:
        revisions[lead_uri(lead_id)] = _scalar(
            conn,
            """
            SELECT
                COALESCE((SELECT last_visited_at FROM leads WHERE id = ?), ''),
                (SELECT COUNT(*) FROM lead_tags WHERE lead_id = ?),
                (SELECT COUNT(*) FROM campaign_leads WHERE lead_id = ?),
                (SELECT COUNT(*) FROM messages WHERE lead_id = ?)
            """,
            (lead_id, lead_id, lead_id, lead_id),
        )
    return revisions


# --------------------------------------------------------------------------
# Delivery
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Delivery:
    """What happened to the update notifications for one resource read."""

    push: bool
    reason: str
    poll_after_seconds: int | None = None
    notified: tuple[str, ...] = ()
    changed: tuple[str, ...] = ()

    def as_payload(self) -> dict[str, Any]:
        """The `updates` block that goes into every resource envelope."""
        payload: dict[str, Any] = {
            "push": self.push,
            "method": "notifications/resources/updated" if self.push else "poll",
            "reason": self.reason,
            "client_capability": CLIENT_CAPABILITY_PATH,
        }
        if self.push:
            payload["notified"] = list(self.notified)
        else:
            payload["poll_after_seconds"] = self.poll_after_seconds
            payload["changed_since_last_read"] = list(self.changed)
        return payload


@dataclass
class ResourceUpdateNotifier:
    """Remembers what each session has been shown, and tells it what moved.

    One instance is created per registration, so the shipped server has exactly
    one and a test that builds its own `FastMCP` gets a clean one.
    """

    poll_after_seconds: int = DEFAULT_POLL_AFTER_SECONDS
    _seen: dict[str, dict[str, str]] = field(default_factory=dict, repr=False)

    def forget(self, key: str) -> None:
        """Drop one session's memory. For tests and for disconnects."""
        self._seen.pop(key, None)

    def known(self, key: str) -> dict[str, str]:
        """Return the revisions a session was last shown."""
        return dict(self._seen.get(key, {}))

    def changed_uris(self, key: str, revisions: dict[str, str]) -> tuple[str, ...]:
        """Return which of `revisions` differ from what this session last saw.

        A URI the session has never seen is not "changed". A first read would
        otherwise report all nine as new, which is noise rather than news.
        """
        seen = self._seen.get(key)
        if seen is None:
            return ()
        return tuple(
            uri
            for uri, revision in sorted(revisions.items())
            if uri in seen and seen[uri] != revision
        )

    def remember(self, key: str, revisions: dict[str, str]) -> None:
        self._seen.setdefault(key, {}).update(revisions)

    async def announce(
        self,
        *,
        session: Any,
        revisions: dict[str, str],
        exclude: tuple[str, ...] = (),
    ) -> Delivery:
        """Push or advertise the URIs that moved since this session last read.

        `exclude` is the URI being read right now. Telling a client that the
        thing it is holding a fresh copy of has changed is noise, and a client
        that re-read on every notification would loop.
        """
        key = session_key(session)
        supported, reason = client_supports_resource_updates(session)
        changed = tuple(uri for uri in self.changed_uris(key, revisions) if uri not in exclude)
        self.remember(key, revisions)

        if not supported:
            return Delivery(
                push=False,
                reason=reason,
                poll_after_seconds=self.poll_after_seconds,
                changed=changed,
            )

        notified: list[str] = []
        for uri in changed:
            try:
                await session.send_resource_updated(as_notification_uri(uri))
            except Exception as error:  # noqa: BLE001 - a dead client is not our bug
                logger.warning("resource update for %s not delivered: %s", uri, error)
                return Delivery(
                    push=False,
                    reason=REASON_SEND_FAILED,
                    poll_after_seconds=self.poll_after_seconds,
                    changed=changed,
                )
            notified.append(uri)
        return Delivery(push=True, reason=reason, notified=tuple(notified), changed=changed)
