"""Duck-typed stand-ins for a Playwright page, used by the scrape tests.

Nothing in the scrape test suite touches a browser or a LinkedIn session. These
fakes implement the small surface the extractors actually use: `url`, `goto`,
`content`, `query_selector`, `query_selector_all`, `evaluate` and `keyboard` on
the page, and `get_attribute`, `inner_text`, `query_selector`,
`query_selector_all` and `click` on an element.

Selector matching is exact string membership rather than a CSS engine. That
keeps the fakes honest: a test has to name the selector it expects the code to
try, so a rebuild that changes the registry shows up as a failing test rather
than as a fake that quietly matches anything.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

from linkedin_mcp.browser.selectors import selector_fallbacks

PAGE_PARAM_PATTERN = re.compile(r"[?&]page=(\d+)")


class FakeElement:
    """An element handle that answers a fixed set of selector strings."""

    def __init__(
        self,
        *,
        selectors: tuple[str, ...] = (),
        attrs: dict[str, str] | None = None,
        text: str | None = None,
        children: list["FakeElement"] | None = None,
        explode: bool = False,
        on_click: Any = None,
    ) -> None:
        self.selectors = tuple(selectors)
        self.attrs = dict(attrs or {})
        self.text = text
        self.children = list(children or [])
        self.explode = explode
        self.on_click = on_click
        self.clicks = 0

    async def get_attribute(self, name: str) -> str | None:
        if self.explode:
            raise RuntimeError("this element is detached")
        return self.attrs.get(name)

    async def inner_text(self) -> str:
        if self.explode:
            raise RuntimeError("this element is detached")
        return self.text or ""

    async def click(self) -> None:
        self.clicks += 1
        if self.on_click is not None:
            self.on_click()

    def _matches(self, selector: str) -> list["FakeElement"]:
        found: list[FakeElement] = []
        for child in self.children:
            if selector in child.selectors:
                found.append(child)
            found.extend(child._matches(selector))
        return found

    async def query_selector(self, selector: str) -> "FakeElement | None":
        if self.explode:
            raise RuntimeError("this element is detached")
        matches = self._matches(selector)
        return matches[0] if matches else None

    async def query_selector_all(self, selector: str) -> list["FakeElement"]:
        if self.explode:
            raise RuntimeError("this element is detached")
        return self._matches(selector)


class FakeKeyboard:
    def __init__(self) -> None:
        self.pressed: list[str] = []

    async def press(self, key: str) -> None:
        self.pressed.append(key)


class FakePage:
    """A results page whose visible cards depend on the `page` query parameter."""

    def __init__(
        self,
        pages: dict[int, list[FakeElement]] | None = None,
        *,
        url: str = "https://www.linkedin.com/feed/",
    ) -> None:
        self.pages = dict(pages or {})
        self.url = url
        self.goto_urls: list[str] = []
        self.goto_kwargs: list[dict[str, Any]] = []
        self.evaluate_calls: list[Any] = []
        self.current_page = 1
        self.keyboard = FakeKeyboard()

    async def goto(self, url: str, **kwargs: Any) -> None:
        self.goto_urls.append(url)
        self.goto_kwargs.append(kwargs)
        self.url = url
        self.current_page = page_number_of(url)

    async def content(self) -> str:
        return "<html></html>"

    async def evaluate(self, script: str, arg: Any = None) -> Any:
        self.evaluate_calls.append(arg)
        return None

    @property
    def visible(self) -> list[FakeElement]:
        return self.pages.get(self.current_page, [])

    async def query_selector(self, selector: str) -> FakeElement | None:
        for element in self.visible:
            if selector in element.selectors:
                return element
            if element.explode:
                continue
            nested = await element.query_selector(selector)
            if nested is not None:
                return nested
        return None

    async def query_selector_all(self, selector: str) -> list[FakeElement]:
        found: list[FakeElement] = []
        for element in self.visible:
            if selector in element.selectors:
                found.append(element)
            if element.explode:
                continue
            found.extend(await element.query_selector_all(selector))
        return found


class FakeScrollPage(FakePage):
    """A lazily loaded list that reveals another slice on each load-more step.

    `reveal_on` picks which gesture reveals the next slice. LinkedIn uses both:
    some group lists lazy load on scroll, others need a button.
    """

    def __init__(
        self,
        cards: list[FakeElement],
        *,
        step: int = 3,
        url: str = "https://www.linkedin.com/feed/",
        load_more_button: FakeElement | None = None,
        reveal_on: str = "scroll",
    ) -> None:
        super().__init__(url=url)
        self.cards = list(cards)
        self.step = step
        self.revealed = step
        self.reveal_on = reveal_on
        self.load_more_button = load_more_button
        if load_more_button is not None and reveal_on == "click":
            load_more_button.on_click = self.reveal_more

    def reveal_more(self) -> None:
        self.revealed = min(len(self.cards), self.revealed + self.step)

    async def goto(self, url: str, **kwargs: Any) -> None:
        await super().goto(url, **kwargs)
        self.revealed = self.step

    async def evaluate(self, script: str, arg: Any = None) -> Any:
        self.evaluate_calls.append(arg)
        if self.reveal_on == "scroll":
            self.reveal_more()
        return None

    @property
    def visible(self) -> list[FakeElement]:
        shown: list[FakeElement] = list(self.cards[: self.revealed])
        if self.load_more_button is not None and self.revealed < len(self.cards):
            shown.append(self.load_more_button)
        return shown


class FakeGate:
    """A stand-in safety gate that allows a fixed number of fetches."""

    def __init__(self, allow: int = 1000, refusal: dict[str, Any] | None = None) -> None:
        self.allow = allow
        self.refusal = refusal or {
            "status": "refused",
            "reason": "daily_cap_reached",
            "message": "the rolling 24 hour cap is spent",
        }
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        action_type: str,
        *,
        lead_id: int | None = None,
        account_id: int | None = None,
        approved: bool | None = None,
        now: Any = None,
    ) -> dict[str, Any] | None:
        self.calls.append(
            {
                "action_type": action_type,
                "account_id": account_id,
                "lead_id": lead_id,
                "now": now,
            }
        )
        if len(self.calls) > self.allow:
            return dict(self.refusal)
        return None


class FakeRecorder:
    """A stand-in audit writer that keeps the rows instead of storing them."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def __call__(
        self,
        account_id: int,
        action_type: str,
        outcome: Any,
        *,
        lead_id: int | None = None,
        detail: Any = None,
        occurred_at: Any = None,
    ) -> int:
        self.rows.append(
            {
                "account_id": account_id,
                "action_type": action_type,
                "outcome": outcome,
                "detail": dict(detail or {}),
                "occurred_at": occurred_at,
            }
        )
        return len(self.rows)


class RecordingSleep:
    """Records requested delays instead of waiting for them."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def page_number_of(url: str) -> int:
    """Return the `page` query parameter of a search URL, defaulting to 1."""
    query = parse_qs(urlsplit(url).query)
    values = query.get("page")
    if values and values[0].isdigit():
        return int(values[0])
    match = PAGE_PARAM_PATTERN.search(url)
    return int(match.group(1)) if match else 1


def node(name: str, *, index: int = 0, text: str | None = None, **attrs: str) -> FakeElement:
    """Build an element tagged with one fallback of a named selector group."""
    return FakeElement(
        selectors=(selector_fallbacks(name)[index],), text=text, attrs=attrs
    )


def person_card(
    *,
    slug: str | None = "nived-velayudhan",
    name: str | None = "Nived Velayudhan",
    headline: str | None = "Solution Engineer at Microsoft",
    location: str | None = "Bengaluru, Karnataka, India",
    distance: str | None = "2nd degree connection",
    avatar: str | None = "https://media.licdn.com/avatar.jpg",
    summary: str | None = None,
    current: str | None = None,
    premium: bool = False,
    urn: str | None = "urn:li:fsd_profile:ACoAAB1nived",
    item_index: int = 0,
) -> FakeElement:
    """Build a People search result card with only the fields given."""
    children: list[FakeElement] = []
    if slug is not None:
        children.append(
            node(
                "people_result_profile_link",
                href=f"https://www.linkedin.com/in/{slug}?miniProfileUrn=x",
            )
        )
    if name is not None:
        children.append(node("people_result_name", text=name))
    if headline is not None:
        children.append(node("people_result_headline", text=headline))
    if location is not None:
        children.append(node("people_result_location", text=location))
    if distance is not None:
        children.append(node("people_result_distance", text=distance))
    if avatar is not None:
        children.append(node("people_result_avatar", src=avatar))
    if summary is not None:
        children.append(node("people_result_summary", text=summary))
    if current is not None:
        children.append(node("people_result_current_position", text=current))
    if premium:
        children.append(node("people_result_premium_badge"))

    attrs = {"data-chameleon-result-urn": urn} if urn else {}
    return FakeElement(
        selectors=(selector_fallbacks("people_result_item")[item_index],),
        attrs=attrs,
        children=children,
    )


def post_card(
    *,
    activity: str | None = "urn:li:activity:7123456789",
    permalink: str | None = "https://www.linkedin.com/feed/update/urn:li:activity:7123456789/?utm=x",
    author_slug: str | None = "nived-velayudhan",
    author_name: str | None = "Nived Velayudhan",
    author_headline: str | None = "Solution Engineer at Microsoft",
    content: str | None = "Copilot rollouts fail on review culture, not on tooling.",
    timestamp: str | None = "2d",
    reactions: str | None = "1,240 reactions",
    comments: str | None = "88 comments",
    reposts: str | None = "12 reposts",
) -> FakeElement:
    """Build a content search result card with only the fields given."""
    children: list[FakeElement] = []
    if permalink is not None:
        children.append(node("post_result_permalink", href=permalink))
    if author_slug is not None:
        children.append(
            node(
                "post_result_author_link",
                href=f"https://www.linkedin.com/in/{author_slug}/",
            )
        )
    if author_name is not None:
        children.append(node("post_result_author_name", text=author_name))
    if author_headline is not None:
        children.append(node("post_result_author_headline", text=author_headline))
    if content is not None:
        children.append(node("post_result_content", text=content))
    if timestamp is not None:
        children.append(node("post_result_timestamp", text=timestamp))
    if reactions is not None:
        children.append(node("post_result_reactions", text=reactions))
    if comments is not None:
        children.append(node("post_result_comments", text=comments))
    if reposts is not None:
        children.append(node("post_result_reposts", text=reposts))

    attrs = {"data-chameleon-result-urn": activity} if activity else {}
    return FakeElement(
        selectors=(selector_fallbacks("post_result_item")[0],),
        attrs=attrs,
        children=children,
    )


def group_member_card(
    *,
    slug: str = "group-member",
    name: str | None = "Group Member",
    headline: str | None = "Platform Engineer",
) -> FakeElement:
    """Build a group member list row."""
    children = [node("group_member_profile_link", href=f"https://www.linkedin.com/in/{slug}/")]
    if name is not None:
        children.append(node("group_member_name", text=name))
    if headline is not None:
        children.append(node("group_member_headline", text=headline))
    return FakeElement(
        selectors=(selector_fallbacks("group_member_item")[0],), children=children
    )


def people_pages(count: int, *, per_page: int = 10, start: int = 0) -> dict[int, list[FakeElement]]:
    """Build `count` unique people spread across pages of `per_page`."""
    pages: dict[int, list[FakeElement]] = {}
    for offset in range(count):
        index = start + offset
        page_number = offset // per_page + 1
        pages.setdefault(page_number, []).append(
            person_card(
                slug=f"person-{index}",
                name=f"Person {index}",
                urn=f"urn:li:member:{1000 + index}",
            )
        )
    return pages


def test_fake_page_routes_on_the_page_parameter():
    page = FakePage(people_pages(20))
    assert page_number_of("https://example.com/search?page=3") == 3
    assert page_number_of("https://example.com/search") == 1
    assert len(page.pages) == 2
