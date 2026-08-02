"""The paged fetch loop: four stops, one gate call per fetch, no tight loop."""

import asyncio
from pathlib import Path

import pytest
from test_scrape_fakes import FakeGate, FakePage, FakeRecorder, RecordingSleep

from linkedin_mcp.audit.log import AuditLog, reset_audit_log, set_audit_log
from linkedin_mcp.browser.humanize import FAST, Humanizer
from linkedin_mcp.browser.navigate import SessionExpiredError
from linkedin_mcp.safety.detect import ChallengeDetected, open_challenges
from linkedin_mcp.safety.gate import guard_action, reset_gate
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
# The package re-exports the `paginate` function under the module's own name, so
# the file is reached by path rather than through `paginate_module.__file__`.
PAGINATE_SOURCE = (
    Path(__file__).resolve().parents[1] / "linkedin_mcp" / "scrape" / "paginate.py"
)


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


def test_a_challenge_mid_pagination_stops_the_account_and_not_just_the_run(tmp_path):
    """Raising is not the job. Recording is.

    A search run walks up to a hundred pages, which makes this the path most
    likely to meet a checkpoint first. Stopping the run and leaving the account
    `active` means the very next tool call walks straight back into the same
    checkpoint, because the gate has no idea anything happened. So this asserts
    the handover rather than the exception: state flipped, event on the timeline,
    and the next `guard_action` refusing on its own.
    """
    log = AuditLog.open(tmp_path / "linkedin-helper.db")
    set_audit_log(log)
    reset_gate()
    try:
        account_id = log.ensure_account("paginate@example.com")
        surface = Surface()
        page = FakePage()

        async def fetch(target, page_number):
            if page_number >= 3:
                target.url = "https://www.linkedin.com/checkpoint/challenge/AgH7Xm"
            await surface.fetch(target, page_number)

        with pytest.raises(SessionExpiredError, match="Session expired") as expired:
            run(
                paginate(
                    page,
                    action_type=ACTION,
                    account_id=account_id,
                    fetch=fetch,
                    extract=surface.extract,
                    key=lambda item: item,
                    limit=100,
                    humanizer=pacer(),
                    guard=FakeGate(),
                    record=FakeRecorder(),
                )
            )

        assert isinstance(expired.value.halt, ChallengeDetected)
        assert expired.value.detection.signal.marker == "/checkpoint/"
        assert surface.fetched == [1, 2, 3]

        state = log.connection.execute(
            "SELECT state FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()[0]
        assert state == "challenged"

        events = log.connection.execute(
            "SELECT kind, severity FROM safety_events WHERE account_id = ?",
            (account_id,),
        ).fetchall()
        assert [tuple(event) for event in events] == [("challenge_detected", "critical")]

        assert open_challenges(log.connection)[0]["account_id"] == account_id

        refusal = guard_action(ACTION, account_id=account_id)
        assert refusal is not None
        assert refusal["reason"] == "account_challenged"
    finally:
        reset_audit_log()
        reset_gate()
        log.close()


def test_the_halt_is_filed_under_the_search_that_was_actually_running(tmp_path):
    """A post search that gets challenged must not be filed as a profile search.

    The paged loop serves both people search and post search. Hardcoding one
    action type in the delegation reads as harmless, because the account still
    flips and the run still stops. The damage shows up later, when the timeline
    is used to work out which budget tripped a checkpoint and every post-search
    halt is sitting under the wrong one.
    """
    log = AuditLog.open(tmp_path / "linkedin-helper.db")
    set_audit_log(log)
    reset_gate()
    try:
        account_id = log.ensure_account("posts@example.com")
        surface = Surface()
        page = FakePage()

        async def fetch(target, page_number):
            if page_number >= 2:
                target.url = "https://www.linkedin.com/checkpoint/challenge/AgH7Xm"
            await surface.fetch(target, page_number)

        with pytest.raises(SessionExpiredError):
            run(
                paginate(
                    page,
                    action_type="post_search",
                    account_id=account_id,
                    fetch=fetch,
                    extract=surface.extract,
                    key=lambda item: item,
                    limit=100,
                    humanizer=pacer(),
                    guard=FakeGate(),
                    record=FakeRecorder(),
                )
            )

        refusals = log.connection.execute(
            "SELECT action_type, outcome FROM actions_log WHERE account_id = ?",
            (account_id,),
        ).fetchall()
        assert ("post_search", "refused") in [tuple(row) for row in refusals]
        assert "profile_search" not in {row[0] for row in refusals}
    finally:
        reset_audit_log()
        reset_gate()
        log.close()


def test_the_paged_loop_keeps_no_challenge_markers_of_its_own():
    """One list of markers, in `detect`. A second copy drifts out of date.

    A local copy is also the shape of the original bug. It raises, which looks
    like it works, while recording nothing.
    """
    source = PAGINATE_SOURCE.read_text(encoding="utf-8")

    assert "AUTHWALL_MARKERS" not in source
    for marker in ('"/authwall"', '"/uas/login"', '"/checkpoint"', '"/login"'):
        assert marker not in source
    assert "navigate_assert_session_alive" in source


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
