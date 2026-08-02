"""Duck-typed stand-ins for LinkedIn's messaging surface.

Nothing in the inbox test suite touches a browser, a LinkedIn session or the
wall clock. The page here implements the small surface the scanner really uses:
`goto`, `content`, `evaluate`, `query_selector` and `query_selector_all` on the
page, and `get_attribute`, `inner_text`, `click`, `scroll_into_view_if_needed`
and the two query methods on an element.

Two things make the fake honest rather than merely convenient:

- Selector matching is exact string membership, inherited from
  `test_scrape_fakes`. A test has to name the selector the production code will
  try, so a registry change shows up as a failing test instead of as a fake that
  matches anything.
- The list reveals its next slice only when the load-more control is clicked,
  and it records every conversation that is opened. That makes the cost of a run
  countable, which is what the delta assertions are actually about: a delta that
  quietly re-reads all two hundred threads still produces the right message
  counts, and only a fetch count catches it.
"""

from __future__ import annotations

from typing import Any

from test_scrape_fakes import FakeElement, FakePage

from linkedin_mcp.browser.selectors import selector_fallbacks

MESSAGING_URL = "https://www.linkedin.com/messaging/"


def node(name: str, *, index: int = 0, text: str | None = None, **attrs: str) -> FakeElement:
    """Build an element tagged with one fallback of a named selector group."""
    return FakeElement(
        selectors=(selector_fallbacks(name)[index],), text=text, attrs=attrs
    )


class Row(FakeElement):
    """A conversation row that can scroll itself into view, as Playwright can."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.reveals = 0

    async def scroll_into_view_if_needed(self) -> None:
        self.reveals += 1


class Thread:
    """One conversation: how it looks in the list and what is inside it."""

    def __init__(
        self,
        thread_id: str,
        *,
        slug: str | None = "ada-lovelace",
        name: str | None = "Ada Lovelace",
        preview: str = "Sounds good, let us talk Thursday",
        timestamp: str = "2h",
        unread: bool = False,
        messages: list[dict[str, Any]] | None = None,
        thread_href: str | None = None,
        openable: bool = True,
    ) -> None:
        self.thread_id = thread_id
        self.slug = slug
        self.name = name
        self.preview = preview
        self.timestamp = timestamp
        self.unread = unread
        self.messages = list(messages or [])
        self.thread_href = thread_href
        self.openable = openable

    def touch(self, preview: str, *, timestamp: str | None = None) -> "Thread":
        """Return the same conversation with new activity in it."""
        return Thread(
            self.thread_id,
            slug=self.slug,
            name=self.name,
            preview=preview,
            timestamp=timestamp or self.timestamp,
            unread=self.unread,
            messages=list(self.messages),
            thread_href=self.thread_href,
            openable=self.openable,
        )

    def row(self, page: "InboxPage") -> Row:
        children: list[FakeElement] = []
        href = self.thread_href
        if href is None:
            href = f"https://www.linkedin.com/messaging/thread/{self.thread_id}/"
        if href:
            children.append(node("inbox_thread_link", href=href))
        if self.slug is not None:
            children.append(
                node(
                    "inbox_thread_participant_link",
                    href=f"https://www.linkedin.com/in/{self.slug}/",
                )
            )
        if self.name is not None:
            children.append(node("inbox_thread_participant_name", text=self.name))
        children.append(node("inbox_thread_preview", text=self.preview))
        children.append(node("inbox_thread_timestamp", text=self.timestamp))
        if self.unread:
            children.append(node("inbox_thread_unread_badge", text="1"))

        row = Row(
            selectors=(selector_fallbacks("inbox_thread_item")[0],),
            children=children,
        )
        if self.openable:
            row.on_click = lambda: page.open(self.thread_id)
        else:

            def refuse() -> None:
                raise RuntimeError("this conversation row is detached")

            row.on_click = refuse
        return row

    def message_rows(self) -> list[FakeElement]:
        rows: list[FakeElement] = []
        for message in self.messages:
            children: list[FakeElement] = [
                node("inbox_message_body", text=message["body"])
            ]
            sender_slug = message.get("sender_slug")
            if sender_slug:
                children.append(
                    node(
                        "inbox_message_sender_link",
                        href=f"https://www.linkedin.com/in/{sender_slug}/",
                    )
                )
            sender_name = message.get("sender_name")
            if sender_name:
                children.append(node("inbox_message_sender_name", text=sender_name))
            stamp_text = message.get("timestamp")
            stamp_attr = message.get("datetime")
            if stamp_text is not None or stamp_attr is not None:
                attrs = {} if stamp_attr is None else {"datetime": stamp_attr}
                children.append(
                    node("inbox_message_timestamp", text=stamp_text, **attrs)
                )
            rows.append(
                FakeElement(
                    selectors=(selector_fallbacks("inbox_message_item")[0],),
                    children=children,
                )
            )
        return rows


class InboxPage(FakePage):
    """LinkedIn messaging: a lazily revealed conversation list and one open thread.

    The list grows only when the load-more control is clicked, which is what
    makes the number of slices a run costs exactly countable. Scrolling records
    itself and reveals nothing, so a run cannot accidentally pass a delta
    assertion by scrolling its way to the bottom.
    """

    def __init__(
        self,
        threads: list[Thread],
        *,
        per_slice: int = 10,
        url: str = "https://www.linkedin.com/feed/",
    ) -> None:
        super().__init__(url=url)
        self.threads = list(threads)
        self.per_slice = max(1, per_slice)
        self.revealed = self.per_slice
        self.opens: list[str] = []
        self.open_id: str | None = None
        self.scrolls = 0
        self.reveals = 0

    # -- the surface the scanner drives -----------------------------------

    async def goto(self, url: str, **kwargs: Any) -> None:
        await super().goto(url, **kwargs)
        self.revealed = self.per_slice
        self.open_id = None

    async def evaluate(self, script: str, arg: Any = None) -> Any:
        self.evaluate_calls.append(arg)
        self.scrolls += 1
        return None

    def reveal(self) -> None:
        self.reveals += 1
        self.revealed = min(len(self.threads), self.revealed + self.per_slice)

    def open(self, thread_id: str) -> None:
        self.open_id = thread_id
        self.opens.append(thread_id)

    def thread(self, thread_id: str) -> Thread | None:
        for candidate in self.threads:
            if candidate.thread_id == thread_id:
                return candidate
        return None

    @property
    def visible(self) -> list[FakeElement]:
        shown: list[FakeElement] = [
            thread.row(self) for thread in self.threads[: self.revealed]
        ]
        if self.revealed < len(self.threads):
            shown.append(
                FakeElement(
                    selectors=(selector_fallbacks("inbox_thread_load_more")[0],),
                    on_click=self.reveal,
                )
            )
        opened = self.thread(self.open_id) if self.open_id else None
        if opened is not None:
            shown.extend(opened.message_rows())
        return shown


def conversation(
    *,
    lead_slug: str = "ada-lovelace",
    lead_name: str = "Ada Lovelace",
    reply: str | None = "Yes, happy to chat. Thursday works.",
    outbound: str = "Hi Ada, saw your post on review culture.",
) -> list[dict[str, Any]]:
    """Build an outbound message and, optionally, the reply to it."""
    thread: list[dict[str, Any]] = [
        {
            "body": outbound,
            "sender_slug": "nived-velayudhan",
            "sender_name": "Nived Velayudhan",
            "datetime": "2026-08-01T09:00:00Z",
            "timestamp": "9:00 AM",
        }
    ]
    if reply is not None:
        thread.append(
            {
                "body": reply,
                "sender_slug": lead_slug,
                "sender_name": lead_name,
                "datetime": "2026-08-01T10:04:00Z",
                "timestamp": "10:04 AM",
            }
        )
    return thread


def quiet_threads(count: int, *, start: int = 0) -> list[Thread]:
    """Build `count` conversations that nobody has replied to."""
    return [
        Thread(
            f"thread-{start + offset}",
            slug=f"person-{start + offset}",
            name=f"Person {start + offset}",
            preview="Sent an invitation",
            messages=conversation(
                lead_slug=f"person-{start + offset}",
                lead_name=f"Person {start + offset}",
                reply=None,
                outbound=f"Hello number {start + offset}",
            ),
        )
        for offset in range(count)
    ]


def test_the_fake_list_only_grows_when_the_load_more_control_is_clicked():
    """Guard the fake. A list that reveals itself would hide a broken delta."""
    page = InboxPage(quiet_threads(25), per_slice=10)

    assert len(page.visible) == 11  # ten rows plus the load-more control
    page.reveal()
    assert len(page.visible) == 21
    page.reveal()
    assert len(page.visible) == 25  # no control left, the list is exhausted
