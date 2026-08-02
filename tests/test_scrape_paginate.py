"""The paged fetch loop: four stops, one gate call per fetch, no tight loop."""

import asyncio

import pytest
from test_scrape_fakes import FakeGate, FakePage, FakeRecorder, RecordingSleep

from linkedin_mcp.browser.humanize import FAST, Humanizer
from linkedin_mcp.browser.navigate import SessionExpiredError
from linkedin_mcp.scrape.paginate import (
    MAX_SEARCH_PAGE,
    PLATFORM_RESULT_CEILING,
    RESULTS_PER_PAGE,
    SearchCursor,
    StopReason,
    paginate,
)

ACCOUNT = 7
ACTION = "profile_search"


def run(coroutine):
    return asyncio.run(coroutine)


class Surface:
    """A paginated source of numbered strings, used to drive the loop."""

    def __init__(self, per_page: int = RESULTS_PER_PAGE, total_pages: int = 500):
        self.per_page = per_page
        self.total_pages = total_pages
        self.fetched: list[int] = []
        self.current = 1
        self.frozen: list[str] | None = None

    async def fetch(self, page, page_number):
        self.fetched.append(page_number)
        self.current = page_number

    async def extract(self, page):
        if self.frozen is not None:
            return list(self.frozen)
        if self.current > self.total_pages:
            return []
        start = (self.current - 1) * self.per_page
        return [f"item-{start + offset}" for offset in range(self.per_page)]


def pacer():
    return Humanizer(FAST, seed=11, sleep=RecordingSleep())


def walk(surface, *, limit, page=None, gate=None, recorder=None, cursor=None, **kwargs):
    return run(
        paginate(
            page or FakePage(),
            action_type=ACTION,
            account_id=ACCOUNT,
            fetch=surface.fetch,
            extract=surface.extract,
            key=lambda item: item,
            limit=limit,
            cursor=cursor,
            humanizer=pacer(),
            guard=gate or FakeGate(),
            record=recorder or FakeRecorder(),
            **kwargs,
        )
    )


def test_the_run_stops_when_the_requested_count_is_reached():
    surface = Surface()

    result = walk(surface, limit=25)

    assert result.stop_reason is StopReason.COUNT_REACHED
    assert len(result.results) == 25
    assert result.pages_fetched == 3
    assert result.cursor.collected == 25


def test_the_run_stops_at_the_platform_ceiling_of_about_a_thousand():
    surface = Surface(total_pages=10_000)

    result = walk(surface, limit=5000)

    assert result.stop_reason is StopReason.PLATFORM_CEILING
    assert len(result.results) == PLATFORM_RESULT_CEILING
    assert result.pages_fetched == MAX_SEARCH_PAGE
    assert PLATFORM_RESULT_CEILING == 1000


def test_the_run_stops_when_the_page_number_passes_the_last_useful_page():
    surface = Surface(per_page=1, total_pages=10_000)

    result = walk(surface, limit=5000)

    assert result.stop_reason is StopReason.PLATFORM_CEILING
    assert result.pages_fetched == MAX_SEARCH_PAGE
    assert len(result.results) == MAX_SEARCH_PAGE


def test_the_run_stops_when_a_page_yields_nothing_new():
    surface = Surface(total_pages=2)

    result = walk(surface, limit=500)

    assert result.stop_reason is StopReason.NO_NEW_RESULTS
    assert len(result.results) == 20
    assert result.pages_fetched == 3


def test_a_page_that_repeats_the_previous_one_stops_the_run():
    surface = Surface()
    surface.frozen = ["item-0", "item-1"]

    result = walk(surface, limit=100)

    assert result.stop_reason is StopReason.NO_NEW_RESULTS
    assert len(result.results) == 2
    assert result.duplicates_skipped == 2


def test_the_run_stops_cleanly_when_the_gate_refuses():
    surface = Surface()
    gate = FakeGate(allow=2)

    result = walk(surface, limit=500, gate=gate)

    assert result.stop_reason is StopReason.GATE_REFUSED
    assert result.pages_fetched == 2
    assert len(result.results) == 20
    assert result.gate_refusal["reason"] == "daily_cap_reached"


def test_a_gate_that_refuses_immediately_never_touches_the_page():
    surface = Surface()
    gate = FakeGate(allow=0)

    result = walk(surface, limit=10, gate=gate)

    assert result.stop_reason is StopReason.GATE_REFUSED
    assert surface.fetched == []
    assert result.results == ()


def test_the_gate_is_asked_once_before_every_fetch():
    surface = Surface()
    gate = FakeGate()

    result = walk(surface, limit=25, gate=gate)

    assert len(gate.calls) == result.pages_fetched
    assert {call["action_type"] for call in gate.calls} == {ACTION}
    assert {call["account_id"] for call in gate.calls} == {ACCOUNT}


def test_the_gate_is_consulted_before_the_page_is_touched():
    surface = Surface()
    order: list[str] = []

    class OrderedGate(FakeGate):
        def __call__(self, *args, **kwargs):
            order.append("gate")
            return super().__call__(*args, **kwargs)

    async def fetch(target, page_number):
        order.append("fetch")
        await surface.fetch(target, page_number)

    run(
        paginate(
            FakePage(),
            action_type=ACTION,
            account_id=ACCOUNT,
            fetch=fetch,
            extract=surface.extract,
            key=lambda item: item,
            limit=25,
            humanizer=pacer(),
            guard=OrderedGate(),
            record=FakeRecorder(),
        )
    )

    assert order == ["gate", "fetch", "gate", "fetch", "gate", "fetch"]


def test_every_page_turn_is_paced_so_the_loop_is_never_tight():
    surface = Surface()
    sleeper = RecordingSleep()
    human = Humanizer(FAST, seed=3, sleep=sleeper)

    result = run(
        paginate(
            FakePage(),
            action_type=ACTION,
            account_id=ACCOUNT,
            fetch=surface.fetch,
            extract=surface.extract,
            key=lambda item: item,
            limit=30,
            humanizer=human,
            guard=FakeGate(),
            record=FakeRecorder(),
        )
    )

    assert result.pages_fetched == 3
    assert human.elapsed > 0
    assert len(sleeper.delays) > result.pages_fetched


def test_every_fetch_writes_one_audit_row():
    surface = Surface()
    recorder = FakeRecorder()

    result = walk(surface, limit=25, recorder=recorder)

    assert len(recorder.rows) == result.pages_fetched
    assert [row["detail"]["page"] for row in recorder.rows] == [1, 2, 3]
    assert {row["action_type"] for row in recorder.rows} == {ACTION}


def test_a_cursor_resumes_the_search_without_re_collecting():
    surface = Surface()

    first = walk(surface, limit=15)
    second = walk(surface, limit=15, cursor=first.cursor)

    assert first.stop_reason is StopReason.COUNT_REACHED
    assert set(first.results) & set(second.results) == set()
    assert second.cursor.collected == 30
    assert surface.fetched == [1, 2, 2, 3]


def test_a_truncated_page_leaves_the_cursor_on_that_page():
    surface = Surface()

    result = walk(surface, limit=15)

    assert result.cursor.page == 2
    assert len(result.cursor.seen_keys) == 15


def test_a_cursor_round_trips_through_its_dictionary_form():
    cursor = SearchCursor(page=4, collected=33, seen_keys=("a", "b"), stop_reason="count_reached")

    assert SearchCursor.from_dict(cursor.as_dict()) == cursor
    assert SearchCursor.from_dict(None) == SearchCursor()


def test_an_exhausted_cursor_says_so():
    assert SearchCursor(stop_reason="no_new_results").exhausted
    assert SearchCursor(stop_reason="platform_ceiling").exhausted
    assert not SearchCursor(stop_reason="count_reached").exhausted
    assert not SearchCursor(stop_reason="gate_refused").exhausted


def test_a_cursor_cannot_start_before_the_first_page():
    with pytest.raises(ValueError, match="pages start at 1"):
        SearchCursor(page=0)


def test_a_limit_below_one_is_refused():
    with pytest.raises(ValueError, match="limit must be at least 1"):
        walk(Surface(), limit=0)


def test_an_authwall_redirect_propagates_instead_of_looking_like_no_results():
    surface = Surface()
    page = FakePage()

    async def fetch(target, page_number):
        target.url = "https://www.linkedin.com/authwall?trk=x"
        await surface.fetch(target, page_number)

    with pytest.raises(SessionExpiredError, match="Session expired"):
        run(
            paginate(
                page,
                action_type=ACTION,
                account_id=ACCOUNT,
                fetch=fetch,
                extract=surface.extract,
                key=lambda item: item,
                limit=10,
                humanizer=pacer(),
                guard=FakeGate(),
                record=FakeRecorder(),
            )
        )


def test_each_page_is_handed_to_the_caller_before_the_next_fetch():
    surface = Surface()
    order: list[str] = []

    async def fetch(target, page_number):
        order.append(f"fetch-{page_number}")
        await surface.fetch(target, page_number)

    async def on_page(items, page_number):
        order.append(f"store-{page_number}-{len(items)}")

    run(
        paginate(
            FakePage(),
            action_type=ACTION,
            account_id=ACCOUNT,
            fetch=fetch,
            extract=surface.extract,
            key=lambda item: item,
            limit=25,
            humanizer=pacer(),
            guard=FakeGate(),
            record=FakeRecorder(),
            on_page=on_page,
        )
    )

    assert order == [
        "fetch-1",
        "store-1-10",
        "fetch-2",
        "store-2-10",
        "fetch-3",
        "store-3-5",
    ]
