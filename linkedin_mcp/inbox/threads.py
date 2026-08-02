"""Reading LinkedIn's messaging surface into records.

Two panes, one page
-------------------
LinkedIn messaging is one route with a lazily loaded conversation list on the
left and the open conversation on the right. Clicking a list row swaps the right
pane without a navigation, so the scanner reads the list and the thread off the
same page object rather than navigating per thread. That is also why a thread
open is metered as part of its list slice rather than as a page load of its own.

Direction is decided on evidence, never on a guess
--------------------------------------------------
Getting direction wrong in one direction is far worse than the other. A message
of Nived's misread as inbound would mark the lead replied and cancel the
sequence the moment the first message went out, on every lead, silently. A reply
misread as outbound merely fails to stop a follow-up, which is the behaviour the
system has today without this feature.

So :func:`read_thread_messages` marks a message inbound only on positive
evidence that the sender is the person on the other side of the thread, and
everything else is outbound. LinkedIn groups consecutive messages from one
sender and renders the name once, so a row with no sender of its own inherits
the direction of the row above it.

Known gaps
----------
None of the selectors in the `inbox_*` registry group has been checked against a
live logged-in session, which cannot be done offline. Every group leads with an
attribute or role hook and falls back to structure, and every optional field
reads as None rather than raising, so a wrong guess costs a field or a thread
rather than the run. A thread whose participant carries no `/in/` link and no
readable name is reported unreadable rather than being invented.

The relative timestamps LinkedIn renders in the list ("2h", "Aug 1") are not
parsed into instants. `sent_at` holds a stored-format timestamp only when the
markup carries a machine readable one, and the rendered label is kept beside it
as `sent_at_text`. Inventing an instant from "2h" would put a wrong time in an
archive that is supposed to be evidence.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import unquote

from linkedin_mcp.inbox.policy import thread_key
from linkedin_mcp.scrape.extract import attr_of, query_all, query_first, text_of
from linkedin_mcp.scrape.records import (
    canonical_profile_url,
    member_urn_from,
    name_from_slug,
    public_id_from,
)

logger = logging.getLogger(__name__)

__all__ = [
    "INBOUND",
    "OUTBOUND",
    "PARTICIPANT_PREFIX",
    "InboxThread",
    "ThreadMessage",
    "extract_threads",
    "list_thread_rows",
    "open_thread",
    "opened_the_right_thread",
    "participant_alias",
    "read_thread_messages",
    "thread_urn_from",
]

INBOUND = "inbound"
OUTBOUND = "outbound"

THREAD_URN_PATTERN = re.compile(r"/messaging/thread/(?P<thread>[^/?#]+)")
STORED_FORMAT = "%Y-%m-%d %H:%M:%S"
WHITESPACE = re.compile(r"\s+")


def thread_urn_from(text: str | None) -> str | None:
    """Return the thread key from a messaging URL or a bare URN.

    LinkedIn addresses a conversation by a `urn:li:msg_conversation:...` value
    that appears both in the row's `href` and, on newer markup, in a data
    attribute. Either form resolves to the same key, so a list that changes
    which one it renders does not split one thread into two.

    The key is percent-decoded and stripped of a trailing slash for the same
    reason. `urn%3Ali%3Amsg_conversation%3A7` and `urn:li:msg_conversation:7`
    are one conversation, and archiving is keyed on this value, so letting them
    differ would duplicate every message in the thread.
    """
    if not text:
        return None
    candidate = unquote(text.strip())
    if not candidate:
        return None
    match = THREAD_URN_PATTERN.search(candidate)
    if match:
        return match.group("thread").rstrip("/") or None
    if candidate.startswith("urn:li:"):
        return candidate.rstrip("/")
    return None


PARTICIPANT_PREFIX = "participant:"
"""Prefix of the identity a conversation with no address of its own falls back to."""


def participant_alias(
    public_id: str | None, member_id: str | None = None
) -> str | None:
    """Return the fallback address a conversation with no thread URN gets."""
    key = public_id or member_id
    return None if not key else f"{PARTICIPANT_PREFIX}{key}"


def _stamp(value: str | None) -> str | None:
    """Return a stored-format timestamp from a machine readable value, or None."""
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    if text.isdigit() and len(text) >= 10:
        # LinkedIn hangs epoch milliseconds off some time elements.
        seconds = int(text) / (1000 if len(text) > 10 else 1)
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime(STORED_FORMAT)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc)
    return parsed.strftime(STORED_FORMAT)


def _signature_part(value: Any) -> str:
    if value is None:
        return ""
    return WHITESPACE.sub(" ", str(value)).strip()


@dataclass(frozen=True, slots=True)
class ThreadMessage:
    """One message in a conversation, in whichever direction it travelled."""

    direction: str
    body: str
    sent_at: str | None = None
    sent_at_text: str | None = None
    sender_name: str | None = None
    sender_public_id: str | None = None

    @property
    def inbound(self) -> bool:
        return self.direction == INBOUND

    @property
    def archive_key(self) -> tuple[str, str, str | None]:
        """The identity an archived row is deduplicated on."""
        return (self.direction, self.body, self.sent_at)


@dataclass(frozen=True, slots=True)
class InboxThread:
    """One conversation as the list renders it, plus its messages once opened."""

    thread_urn: str
    participant_name: str | None = None
    participant_public_id: str | None = None
    participant_member_id: str | None = None
    participant_profile_url: str | None = None
    preview: str | None = None
    unread: bool = False
    last_activity_text: str | None = None
    messages: tuple[ThreadMessage, ...] = ()

    @property
    def signature(self) -> str:
        """A change detector readable off the list row without opening the thread.

        The preview snippet and the unread badge, and deliberately *not* the
        rendered activity label. That label is relative: an untouched thread
        reading "2h" today reads "5h" three hours later, so folding it in would
        make every thread look changed on every scan and turn the delta path
        back into a full re-read of the inbox. The bug would not show up in a
        test with fixed labels either, which is why it is called out here.

        What is left changes exactly when it should. The snippet is the last
        message in the conversation, so it moves when a message arrives, and the
        unread badge moves when one arrives unread.
        """
        return "|".join(
            (
                _signature_part(self.preview),
                "unread" if self.unread else "read",
            )
        )

    @property
    def dedupe_key(self) -> str:
        return thread_key(self.thread_urn, self.signature)

    @property
    def inbound_messages(self) -> tuple[ThreadMessage, ...]:
        return tuple(message for message in self.messages if message.inbound)

    @property
    def has_reply(self) -> bool:
        """True when the other person has said something in this thread."""
        return bool(self.inbound_messages)

    def with_messages(self, messages: tuple[ThreadMessage, ...]) -> "InboxThread":
        """Return a copy carrying the messages read out of the open conversation.

        The participant's public id is filled in from the messages when the list
        row did not carry a `/in/` link but did carry a name. Without this a
        known lead whose row renders no link would be reported as a stranger and
        its queued follow-up would survive, which is the failure this whole
        issue exists to prevent.

        Only a name match is trusted. Taking any sender's slug would risk
        picking up Nived's own, resolving the thread to his own lead row and
        stopping his sequences.
        """
        public_id = self.participant_public_id
        if public_id is None and self.participant_name:
            wanted = self.participant_name.strip().casefold()
            for message in messages:
                name = (message.sender_name or "").strip().casefold()
                if name and name == wanted and message.sender_public_id:
                    public_id = message.sender_public_id
                    break
        return InboxThread(
            thread_urn=self.thread_urn,
            participant_name=self.participant_name,
            participant_public_id=public_id,
            participant_member_id=self.participant_member_id,
            participant_profile_url=self.participant_profile_url,
            preview=self.preview,
            unread=self.unread,
            last_activity_text=self.last_activity_text,
            messages=tuple(messages),
        )


async def _thread_from_row(item: Any) -> InboxThread | None:
    """Build a thread from one list row, tolerating every missing field but identity."""
    href = await attr_of(item, "inbox_thread_link", "href")
    thread_urn = thread_urn_from(href)
    if thread_urn is None:
        getter = getattr(item, "get_attribute", None)
        if getter is not None:
            for attribute in ("data-thread-urn", "data-conversation-urn", "data-urn"):
                try:
                    thread_urn = thread_urn_from(await getter(attribute))
                except Exception as error:  # noqa: BLE001 - a dead handle is not fatal
                    logger.debug("Reading @%s off a thread row failed: %s", attribute, error)
                    continue
                if thread_urn:
                    break

    profile_url = await attr_of(item, "inbox_thread_participant_link", "href")
    public_id = public_id_from(profile_url)
    member_id = member_urn_from(profile_url)

    if thread_urn is None:
        # A conversation with no address of its own can still be identified by
        # who it is with, which is enough to archive against and to deduplicate.
        thread_urn = participant_alias(public_id, member_id)
    if thread_urn is None:
        logger.debug("Skipping an inbox row with neither a thread address nor a participant")
        return None

    name = await text_of(item, "inbox_thread_participant_name")
    if not name and public_id:
        name = name_from_slug(public_id)

    unread_badge = await query_first(item, "inbox_thread_unread_badge")

    return InboxThread(
        thread_urn=thread_urn,
        participant_name=name or None,
        participant_public_id=public_id,
        participant_member_id=member_id,
        participant_profile_url=canonical_profile_url(profile_url),
        preview=await text_of(item, "inbox_thread_preview"),
        unread=unread_badge is not None,
        last_activity_text=await text_of(item, "inbox_thread_timestamp"),
    )


async def list_thread_rows(page: Any) -> list[tuple[InboxThread, Any]]:
    """Return every conversation on the rendered list, paired with its element.

    The element travels with the record because opening a thread is a click on
    that row. One unreadable row never takes the slice down with it: the inbox
    mixes person conversations with InMail, group threads and sponsored
    messages, and only the first of those has a `/in/` link to read.
    """
    rows: list[tuple[InboxThread, Any]] = []
    for item in await query_all(page, "inbox_thread_item"):
        try:
            thread = await _thread_from_row(item)
        except Exception as error:  # noqa: BLE001 - one bad row is not a bad slice
            logger.warning("Skipping an unreadable inbox row: %s", error)
            continue
        if thread is not None:
            rows.append((thread, item))
    return rows


async def extract_threads(page: Any) -> list[InboxThread]:
    """Return every conversation on the rendered list."""
    return [thread for thread, _ in await list_thread_rows(page)]


async def open_thread(page: Any, handle: Any, pacer: Any) -> bool:
    """Click a conversation open, reporting whether the click landed.

    A row that has scrolled out of the DOM since it was read is a miss rather
    than a failure: the next scan sees the thread again, because nothing was
    written for it and so nothing entered the watermark.

    A click that did not throw is not the same as the right conversation being
    on screen, which is what :func:`opened_the_right_thread` is for.
    """
    if handle is None:
        return False
    reveal = getattr(handle, "scroll_into_view_if_needed", None)
    if reveal is not None:
        try:
            await reveal()
        except Exception as error:  # noqa: BLE001 - a detached row is not fatal
            logger.debug("Scrolling an inbox row into view failed: %s", error)
    try:
        await pacer.click(handle)
    except Exception as error:  # noqa: BLE001 - a stale row is not fatal
        logger.warning("Opening an inbox thread failed: %s", error)
        return False
    return True


def opened_the_right_thread(page: Any, thread: "InboxThread") -> bool:
    """Return False when the route names a conversation other than this one.

    Messaging is a single page application, so the right hand pane can still be
    showing the previous conversation when the click returns. Reading it then
    would archive one person's messages against another person's lead, which is
    the worst thing this module could do quietly.

    The address bar is the check. When it names a conversation and that is not
    the one that was clicked, this reports a miss and the caller leaves the
    thread out of the watermark so the next scan tries it again. When the route
    carries no conversation address at all there is nothing to check against and
    the pane is accepted, which is a documented gap rather than a silent one.
    """
    url = getattr(page, "url", None)
    if not isinstance(url, str):
        return True
    showing = thread_urn_from(url)
    if showing is None or showing == thread.thread_urn:
        return True
    logger.warning(
        "Asked for conversation %s but the page is showing %s; leaving it for "
        "the next scan rather than archiving somebody else's messages",
        thread.thread_urn,
        showing,
    )
    return False


async def _sender_of(item: Any) -> tuple[str | None, str | None]:
    """Return the name and public id the message row attributes itself to."""
    href = await attr_of(item, "inbox_message_sender_link", "href")
    public_id = public_id_from(href)
    name = await text_of(item, "inbox_message_sender_name")
    if not name and public_id:
        name = name_from_slug(public_id)
    return name or None, public_id


def _is_participant(
    thread: InboxThread,
    sender_name: str | None,
    sender_public_id: str | None,
) -> bool:
    """Return True when this sender is the person on the other side of the thread."""
    if sender_public_id and thread.participant_public_id:
        return sender_public_id.casefold() == thread.participant_public_id.casefold()
    if sender_name and thread.participant_name:
        return sender_name.strip().casefold() == thread.participant_name.strip().casefold()
    return False


async def read_thread_messages(page: Any, thread: InboxThread) -> tuple[ThreadMessage, ...]:
    """Read both sides of the open conversation.

    Direction is positive evidence only. A row whose sender resolves to the
    thread's participant is inbound; anything else is outbound. A row carrying
    no sender of its own inherits the row above it, which is how LinkedIn
    renders a run of consecutive messages from the same person.
    """
    messages: list[ThreadMessage] = []
    inherited = OUTBOUND
    inherited_name: str | None = None
    inherited_public_id: str | None = None

    for item in await query_all(page, "inbox_message_item"):
        try:
            body = await text_of(item, "inbox_message_body")
            if not body:
                continue
            sender_name, sender_public_id = await _sender_of(item)
            if sender_name or sender_public_id:
                inherited = (
                    INBOUND
                    if _is_participant(thread, sender_name, sender_public_id)
                    else OUTBOUND
                )
                inherited_name = sender_name
                inherited_public_id = sender_public_id
            stamp_attr = await attr_of(item, "inbox_message_timestamp", "datetime")
            stamp_text = await text_of(item, "inbox_message_timestamp")
            messages.append(
                ThreadMessage(
                    direction=inherited,
                    body=body,
                    sent_at=_stamp(stamp_attr) or _stamp(stamp_text),
                    sent_at_text=stamp_text,
                    sender_name=sender_name or inherited_name,
                    sender_public_id=sender_public_id or inherited_public_id,
                )
            )
        except Exception as error:  # noqa: BLE001 - one bad row is not a bad thread
            logger.warning(
                "Skipping an unreadable message in thread %s: %s", thread.thread_urn, error
            )
    return tuple(messages)
