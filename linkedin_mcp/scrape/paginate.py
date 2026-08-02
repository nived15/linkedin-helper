"""The paged fetch loop every extractor shares.

One loop, four ways to stop
---------------------------
LinkedIn stops serving search results at roughly 1,000 per query, which is ten
results across a hundred pages. Walking past that returns the same page over
and over, so the ceiling is a stop condition rather than an error. The loop
stops when the caller's requested count is reached, when the platform ceiling is
reached, when a page turns up nothing the run has not already seen, and when the
safety gate refuses. Every one of those is an ordinary outcome recorded in the
summary, and none of them raises.

Pacing and permission
---------------------
The gate is asked before every fetch, not once at the start, so a run that
crosses a cap boundary halfway through stops there rather than finishing the
job it had already started. Between two fetches the loop takes a humanizer
cooldown, and it scrolls the results before reading them, because a page that
appears and is scraped in the same instant is the clearest possible automation
signal. Nothing here sleeps directly.

Resumability
------------
The loop takes a cursor and returns one. A caller that stops at the gate can
resume from the same page later with the keys it has already seen, which is what
SEQ-04's background runner needs to drive this without a scheduler living here.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Generic, Protocol, TypeVar

from linkedin_mcp.audit import Outcome, log_action
from linkedin_mcp.browser.humanize import Humanizer, get_humanizer
from linkedin_mcp.browser.navigate import SessionExpiredError
from linkedin_mcp.safety import guard_action

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_SEARCH_PAGE",
    "PLATFORM_RESULT_CEILING",
    "RESULTS_PER_PAGE",
    "SCROLL_DISTANCE",
    "GuardFn",
    "PageRun",
    "RecordFn",
    "SearchCursor",
    "StopReason",
    "assert_session_alive",
    "paginate",
]

PLATFORM_RESULT_CEILING = 1000
"""Results LinkedIn will serve for one search before it stops paginating."""

RESULTS_PER_PAGE = 10
"""Results on one search page, which is what the ceiling divides into."""

MAX_SEARCH_PAGE = PLATFORM_RESULT_CEILING // RESULTS_PER_PAGE
"""Highest page number worth asking for. Page 101 repeats page 100."""

SCROLL_DISTANCE = 900
"""Pixels to scroll a results page before reading it, so lazy cards render."""

AUTHWALL_MARKERS = ("/login", "/authwall", "/checkpoint", "/uas/login")

T = TypeVar("T")


class GuardFn(Protocol):
    """The shape of `linkedin_mcp.safety.guard_action`."""

    def __call__(
        self,
        action_type: str,
        *,
        lead_id: int | None = None,
        account_id: int | None = None,
        approved: bool | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any] | None: ...


class RecordFn(Protocol):
    """The shape of `linkedin_mcp.audit.log_action`."""

    def __call__(
        self,
        account_id: int,
        action_type: str,
        outcome: Any,
        *,
        lead_id: int | None = None,
        detail: Mapping[str, Any] | None = None,
        occurred_at: datetime | str | None = None,
    ) -> int: ...


class StopReason(str, Enum):
    """Why a paged run ended."""

    COUNT_REACHED = "count_reached"
    PLATFORM_CEILING = "platform_ceiling"
    NO_NEW_RESULTS = "no_new_results"
    GATE_REFUSED = "gate_refused"


EXHAUSTING_REASONS = frozenset(
    {StopReason.PLATFORM_CEILING.value, StopReason.NO_NEW_RESULTS.value}
)
"""Stops that mean the search is finished, rather than merely paused."""


@dataclass(frozen=True, slots=True)
class SearchCursor:
    """Where a search got to, and what it had already seen.

    Handing this back into `paginate` resumes the same search without
    re-harvesting the results the previous run already stored.
    """

    page: int = 1
    collected: int = 0
    seen_keys: tuple[str, ...] = ()
    stop_reason: str | None = None

    def __post_init__(self) -> None:
        if self.page < 1:
            raise ValueError(f"search pages start at 1, got {self.page}")
        if self.collected < 0:
            raise ValueError(f"collected cannot be negative, got {self.collected}")

    @property
    def exhausted(self) -> bool:
        """True when the search has nothing left to give."""
        return self.stop_reason in EXHAUSTING_REASONS

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON friendly cursor, for storage between runs."""
        return {
            "page": self.page,
            "collected": self.collected,
            "seen_keys": list(self.seen_keys),
            "stop_reason": self.stop_reason,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> "SearchCursor":
        """Rebuild a cursor stored by :meth:`as_dict`."""
        if not payload:
            return cls()
        return cls(
            page=int(payload.get("page", 1)),
            collected=int(payload.get("collected", 0)),
            seen_keys=tuple(payload.get("seen_keys") or ()),
            stop_reason=payload.get("stop_reason"),
        )


@dataclass(frozen=True, slots=True)
class PageRun(Generic[T]):
    """What one paged run collected, and why it stopped."""

    results: tuple[T, ...] = ()
    stop_reason: StopReason = StopReason.NO_NEW_RESULTS
    cursor: SearchCursor = field(default_factory=SearchCursor)
    pages_fetched: int = 0
    results_seen: int = 0
    duplicates_skipped: int = 0
    gate_refusal: Mapping[str, Any] | None = None


def assert_session_alive(page: Any) -> None:
    """Raise when LinkedIn has bounced the session to a wall or a challenge.

    This propagates on purpose. A run that quietly returns zero results because
    the cookies expired looks exactly like a search with no matches, and the
    difference matters.
    """
    current = getattr(page, "url", "") or ""
    if any(marker in current for marker in AUTHWALL_MARKERS):
        raise SessionExpiredError(f"Session expired: LinkedIn redirected to {current}")


async def paginate(
    page: Any,
    *,
    action_type: str,
    account_id: int,
    fetch: Callable[[Any, int], Awaitable[None]],
    extract: Callable[[Any], Awaitable[Sequence[T]]],
    key: Callable[[T], str],
    limit: int,
    cursor: SearchCursor | None = None,
    humanizer: Humanizer | None = None,
    guard: GuardFn | None = None,
    record: RecordFn | None = None,
    clock: Callable[[], datetime] | None = None,
    on_page: Callable[[Sequence[T], int], Awaitable[None]] | None = None,
    result_ceiling: int = PLATFORM_RESULT_CEILING,
    max_page: int = MAX_SEARCH_PAGE,
    scroll_before_extract: bool = True,
    detail: Mapping[str, Any] | None = None,
) -> PageRun[T]:
    """Walk a paginated LinkedIn surface, stopping at the first reason to stop.

    Args:
        page: Playwright page, or anything with the same surface.
        action_type: Budget the fetch spends, such as `profile_search`.
        account_id: Account the run belongs to.
        fetch: Loads one page of results. Takes the page and the page number.
        extract: Reads the loaded page into records.
        key: Stable dedupe key for one record.
        limit: How many new results this run wants.
        cursor: Where to resume from, if this is a continuation.
        humanizer: Pacing. Defaults to the process-wide humanizer.
        guard: Safety gate. Defaults to `guard_action`.
        record: Audit writer. Defaults to `log_action`.
        clock: Decision time source, so a runner can drive this deterministically.
        on_page: Called with each page's accepted results before the next fetch,
            so persistence happens per page and a crash keeps what it got.
        result_ceiling: Platform ceiling for the whole search.
        max_page: Highest page number worth requesting.
        scroll_before_extract: Scroll the results before reading them.
        detail: Extra fields to attach to each audit row.
    """
    if limit < 1:
        raise ValueError(f"limit must be at least 1, got {limit}")

    pacer = humanizer or get_humanizer()
    ask = guard or guard_action
    write = record or log_action
    tick = clock or (lambda: datetime.now(timezone.utc))
    start = cursor or SearchCursor()

    seen_order = list(start.seen_keys)
    seen = set(seen_order)
    collected: list[T] = []
    page_number = start.page
    pages_fetched = 0
    results_seen = 0
    duplicates = 0
    refusal: Mapping[str, Any] | None = None
    stop: StopReason | None = None

    while True:
        if len(collected) >= limit:
            stop = StopReason.COUNT_REACHED
            break
        if start.collected + len(collected) >= result_ceiling:
            stop = StopReason.PLATFORM_CEILING
            break
        if page_number > max_page:
            stop = StopReason.PLATFORM_CEILING
            break

        refusal = ask(action_type, account_id=account_id, now=tick())
        if refusal is not None:
            logger.info(
                "Safety gate stopped %s at page %d: %s",
                action_type,
                page_number,
                refusal.get("message", refusal.get("reason")),
            )
            stop = StopReason.GATE_REFUSED
            break

        if pages_fetched:
            await pacer.cooldown()

        await fetch(page, page_number)
        await pacer.settle()
        assert_session_alive(page)
        pages_fetched += 1

        write(
            account_id,
            action_type,
            Outcome.SUCCESS,
            detail={**(detail or {}), "page": page_number},
            occurred_at=tick(),
        )

        if scroll_before_extract:
            await pacer.scroll(page, SCROLL_DISTANCE)

        items = list(await extract(page))
        results_seen += len(items)

        fresh: list[T] = []
        for item in items:
            item_key = key(item)
            if item_key in seen:
                duplicates += 1
                continue
            seen.add(item_key)
            fresh.append(item)

        if not fresh:
            stop = StopReason.NO_NEW_RESULTS
            break

        room = min(
            limit - len(collected),
            result_ceiling - start.collected - len(collected),
        )
        accepted = fresh[:room]
        for item in fresh[room:]:
            seen.discard(key(item))

        if on_page is not None and accepted:
            await on_page(accepted, page_number)

        collected.extend(accepted)
        seen_order.extend(key(item) for item in accepted)

        if len(accepted) == len(fresh):
            page_number += 1

    return PageRun(
        results=tuple(collected),
        stop_reason=stop or StopReason.NO_NEW_RESULTS,
        cursor=SearchCursor(
            page=page_number,
            collected=start.collected + len(collected),
            seen_keys=tuple(seen_order),
            stop_reason=(stop or StopReason.NO_NEW_RESULTS).value,
        ),
        pages_fetched=pages_fetched,
        results_seen=results_seen,
        duplicates_skipped=duplicates,
        gate_refusal=refusal,
    )
