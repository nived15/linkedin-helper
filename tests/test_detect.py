import json
import re
import sqlite3
from datetime import datetime, timezone
from inspect import Parameter, signature
from pathlib import Path

import pytest

from linkedin_mcp import browser
from linkedin_mcp.audit import instrument
from linkedin_mcp.audit.log import (
    AuditLog,
    Outcome,
    RefusalReason,
    reset_audit_log,
    set_audit_log,
)
from linkedin_mcp.browser.humanize import FAST, Humanizer
from linkedin_mcp.browser.navigate import (
    SessionExpiredError,
    assert_session_alive,
    goto_profile,
)
from linkedin_mcp.safety import detect
from linkedin_mcp.safety.detect import (
    CAPTCHA_SELECTOR,
    CHALLENGE_KIND,
    DETECTION_EVENT_KINDS,
    LOGGED_OUT_KIND,
    UNREADABLE_KIND,
    ChallengeDetected,
    Detection,
    DetectionHalt,
    LoggedOutDetected,
    PageUnreadable,
    assert_page_clear,
    inspect_page,
    open_challenges,
    recent_safety_events,
    record_halt,
)
from linkedin_mcp.safety.gate import (
    ACTIVE_STATE,
    AccountChallenged,
    AccountLoggedOut,
    SafetyError,
    SafetyGate,
    reset_gate,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
NOON = datetime(2026, 3, 14, 12, 0, tzinfo=timezone.utc)
FEED_URL = "https://www.linkedin.com/feed/"
AUTHWALL_URL = "https://www.linkedin.com/authwall?trk=qf&trkInfo=idx%3D1"
CHECKPOINT_URL = "https://www.linkedin.com/checkpoint/challenge/AgH7Xm"
PROFILE_URL = "https://www.linkedin.com/in/nived-velayudhan-123456/"

AUTHWALL_HTML = """
<html><head><title>Sign Up | LinkedIn</title></head>
<body><main><h1>Join LinkedIn to see this page</h1></main></body></html>
"""
RESTRICTED_HTML = """
<html><body><div class="alert"><p>We\u2019ve restricted your account</p>
<span>Let\u2019s do a quick security check before you continue.</span></div></body></html>
"""
RECAPTCHA_HTML = """
<html><body><iframe src="https://www.google.com/recaptcha/api2/anchor?k=abc"
title="reCAPTCHA"></iframe></body></html>
"""


class UrlOnlyPage:
    """The smallest page a caller can hand us: a URL and nothing else."""

    def __init__(self, url: str = FEED_URL):
        self.url = url


class FakeFrame:
    def __init__(self, url: str = "", name: str = ""):
        self.url = url
        self.name = name


class FramePage(UrlOnlyPage):
    def __init__(self, url: str = FEED_URL, frames: list | None = None):
        super().__init__(url)
        self.frames = frames or []


class DomPage(UrlOnlyPage):
    """Page exposing the DOM probes: query_selector, content and title."""

    def __init__(
        self,
        url: str = FEED_URL,
        *,
        html: str = "<html><body><main>Feed</main></body></html>",
        page_title: str = "Feed | LinkedIn",
        captcha: bool = False,
    ):
        super().__init__(url)
        self.html = html
        self.page_title = page_title
        self.captcha = captcha
        self.queried: list[str] = []
        self.content_calls = 0

    async def query_selector(self, selector: str):
        self.queried.append(selector)
        return object() if self.captcha else None

    async def content(self) -> str:
        self.content_calls += 1
        return self.html

    async def title(self) -> str:
        return self.page_title


class BlindPage:
    """Every probe fails, which is not the same thing as a clean page."""

    @property
    def url(self) -> str:
        raise RuntimeError("Target page, context or browser has been closed")

    async def content(self) -> str:
        raise RuntimeError("Target page, context or browser has been closed")


class DirectLoadPage(UrlOnlyPage):
    def __init__(self, landing: str):
        super().__init__("about:blank")
        self.landing = landing
        self.goto_calls: list[str] = []

    async def goto(self, url: str, wait_until: str | None = None, timeout: int | None = None):
        self.goto_calls.append(url)
        self.url = self.landing


class RecordingSleep:
    def __init__(self):
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def fast_humanizer() -> Humanizer:
    return Humanizer(profile=FAST, seed=3, sleep=RecordingSleep())


@pytest.fixture
def audit(tmp_path):
    log = AuditLog.open(tmp_path / "linkedin-helper.db")
    set_audit_log(log)
    try:
        yield log
    finally:
        reset_audit_log()
        reset_gate()
        instrument.reset_account_resolver()
        log.close()


@pytest.fixture
def account_id(audit):
    return audit.ensure_account("detect@example.com")


def account_state(audit, account_id) -> str:
    row = audit.connection.execute(
        "SELECT state FROM accounts WHERE id = ?", (account_id,)
    ).fetchone()
    return str(row["state"])


def set_account_state(audit, account_id, state):
    audit.connection.execute(
        "UPDATE accounts SET state = ? WHERE id = ?", (state, account_id)
    )
    audit.connection.commit()


def safety_events(audit, account_id):
    return audit.connection.execute(
        "SELECT kind, severity, detail_json, occurred_at FROM safety_events "
        "WHERE account_id = ? ORDER BY id",
        (account_id,),
    ).fetchall()


# --- what one inspection sees -----------------------------------------------


@pytest.mark.asyncio
async def test_a_page_offering_only_a_url_is_still_inspected():
    detection = await inspect_page(UrlOnlyPage())

    assert detection.clean
    assert detection.readable
    assert detection.probes == ("url",)
    assert detection.read_errors == ()
    assert detection.kind == ""


@pytest.mark.asyncio
async def test_the_url_settles_a_checkpoint_without_reading_the_page():
    page = DomPage(CHECKPOINT_URL)

    detection = await inspect_page(page)

    assert detection.kind == CHALLENGE_KIND
    assert detection.signal.marker == "/checkpoint/"
    assert detection.signal.source == "url"
    assert detection.signal.evidence == CHECKPOINT_URL
    assert detection.probes == ("url",)
    assert page.queried == []
    assert page.content_calls == 0


@pytest.mark.parametrize(
    "url, kind, marker",
    [
        (AUTHWALL_URL, CHALLENGE_KIND, "/authwall"),
        (CHECKPOINT_URL, CHALLENGE_KIND, "/checkpoint/"),
        ("https://www.linkedin.com/challenge/verify", CHALLENGE_KIND, "/challenge/"),
        ("https://www.linkedin.com/uas/login", LOGGED_OUT_KIND, "/uas/login"),
        ("https://www.linkedin.com/login?fromSignIn=true", LOGGED_OUT_KIND, "/login"),
        (
            "https://www.linkedin.com/checkpoint/lg/login-submit",
            LOGGED_OUT_KIND,
            "/checkpoint/lg/",
        ),
    ],
)
@pytest.mark.asyncio
async def test_a_login_redirect_is_told_apart_from_a_real_challenge(url, kind, marker):
    detection = await inspect_page(UrlOnlyPage(url))

    assert detection.kind == kind
    assert detection.signal.marker == marker


@pytest.mark.asyncio
async def test_a_redirect_target_in_the_query_string_does_not_stop_a_run():
    """Only the path is matched, so a link to a checkpoint is not a checkpoint."""
    page = UrlOnlyPage(f"{FEED_URL}?session_redirect=%2Fcheckpoint%2Fchallenge")

    assert (await inspect_page(page)).clean


@pytest.mark.parametrize(
    "url",
    [
        "https://www.linkedin.com/in/loginov-dmitry/",
        "https://www.linkedin.com/in/captchai-labs/",
        "https://www.linkedin.com/company/login-business-solutions/",
        "https://www.linkedin.com/in/anna-challenger/",
        "https://www.linkedin.com/company/authwall-security/",
        "https://www.linkedin.com/in/checkpointer/",
    ],
)
@pytest.mark.asyncio
async def test_a_member_whose_name_contains_a_marker_is_not_a_challenge(url):
    """Markers match whole path segments, so Dmitry Loginov keeps his profile.

    A substring test would flag `/in/loginov-dmitry` on `/login` and flip the
    account into a state only a human can clear. The cost of a loose match is a
    stopped worker, so the match is anchored to segment boundaries.
    """
    detection = await inspect_page(UrlOnlyPage(url))

    assert detection.clean, detection.signal


@pytest.mark.asyncio
async def test_a_captcha_frame_is_found_in_the_frame_list():
    page = FramePage(
        FEED_URL,
        [FakeFrame(FEED_URL), FakeFrame("https://client-api.arkoselabs.com/v2", "arkose")],
    )

    detection = await inspect_page(page)

    assert detection.kind == CHALLENGE_KIND
    assert detection.signal.marker == "arkoselabs"
    assert detection.signal.source == "frame"
    assert detection.probes == ("url", "frames")


@pytest.mark.asyncio
async def test_a_captcha_frame_is_found_in_the_dom_when_the_frame_list_is_empty():
    page = DomPage(FEED_URL, captcha=True)

    detection = await inspect_page(page)

    assert detection.kind == CHALLENGE_KIND
    assert detection.signal.source == "frame"
    assert page.queried == [CAPTCHA_SELECTOR]
    assert page.content_calls == 0


@pytest.mark.asyncio
async def test_a_captcha_iframe_in_the_markup_counts_even_without_a_frame_list():
    detection = await inspect_page(DomPage(FEED_URL, html=RECAPTCHA_HTML))

    assert detection.kind == CHALLENGE_KIND
    assert detection.signal.marker == "recaptcha"
    assert detection.signal.source == "frame"


@pytest.mark.asyncio
async def test_a_warning_banner_is_found_on_a_page_whose_url_looks_fine():
    """LinkedIn ships curly apostrophes, so the text is normalized before matching."""
    detection = await inspect_page(DomPage(FEED_URL, html=RESTRICTED_HTML))

    assert detection.kind == CHALLENGE_KIND
    assert detection.signal.marker == "we've restricted your account"
    assert detection.signal.source == "banner"
    assert "restricted your account" in detection.signal.evidence


@pytest.mark.asyncio
async def test_a_page_nobody_could_read_is_not_reported_clean():
    detection = await inspect_page(BlindPage())

    assert not detection.clean
    assert not detection.readable
    assert detection.kind == UNREADABLE_KIND
    assert detection.probes == ()
    assert any("url" in error for error in detection.read_errors)
    assert any("content" in error for error in detection.read_errors)


@pytest.mark.asyncio
async def test_evidence_is_trimmed_so_one_page_cannot_flood_the_timeline():
    page = DomPage(FEED_URL, html="<p>" + ("x" * 5000) + " unusual activity</p>")

    detection = await inspect_page(page)

    assert detection.kind == CHALLENGE_KIND
    assert len(detection.signal.evidence) <= detect.MAX_EVIDENCE_LENGTH + 3


# --- what a hit writes down --------------------------------------------------


@pytest.mark.asyncio
async def test_an_authwall_flips_the_account_and_lands_on_the_safety_timeline(
    audit, account_id
):
    page = DomPage(AUTHWALL_URL, html=AUTHWALL_HTML)

    with pytest.raises(ChallengeDetected) as halted:
        await assert_page_clear(
            page, account_id=account_id, action_type="profile_view", moment=NOON
        )

    halt = halted.value
    assert halt.detection.signal.marker == "/authwall"
    assert halt.record.transitioned is True
    assert halt.record.previous_state == "active"
    assert halt.record.state == "challenged"
    assert halt.record_error is None
    assert account_state(audit, account_id) == "challenged"
    assert not audit.connection.in_transaction

    events = safety_events(audit, account_id)
    assert len(events) == 1
    assert events[0]["kind"] == "challenge_detected"
    assert events[0]["severity"] == "critical"
    detail = json.loads(events[0]["detail_json"])
    assert detail["reason"] == RefusalReason.ACCOUNT_CHALLENGED.value
    assert detail["marker"] == "/authwall"
    assert detail["source"] == "url"
    assert detail["url"] == AUTHWALL_URL

    refusals = audit.refusals(account_id)
    assert len(refusals) == 1
    assert refusals[0]["action_type"] == "profile_view"
    assert refusals[0]["outcome"] == Outcome.REFUSED.value
    assert json.loads(refusals[0]["detail_json"])["reason"] == (
        RefusalReason.ACCOUNT_CHALLENGED.value
    )


@pytest.mark.asyncio
async def test_the_gate_refuses_every_later_action_once_a_challenge_lands(
    audit, account_id
):
    """Setting the state is the whole handover; the gate does the rest."""
    gate = SafetyGate(clock=lambda: NOON)
    gate.acquire(account_id, "post_like")

    with pytest.raises(ChallengeDetected):
        await assert_page_clear(UrlOnlyPage(AUTHWALL_URL), account_id=account_id)

    with pytest.raises(AccountChallenged):
        gate.acquire(account_id, "post_like")


@pytest.mark.asyncio
async def test_a_logged_out_session_lands_as_logged_out_rather_than_challenged(
    audit, account_id
):
    with pytest.raises(LoggedOutDetected) as halted:
        await assert_page_clear(
            UrlOnlyPage("https://www.linkedin.com/uas/login"),
            account_id=account_id,
            action_type="profile_view",
            moment=NOON,
        )

    assert halted.value.refusal_reason is RefusalReason.ACCOUNT_LOGGED_OUT
    assert account_state(audit, account_id) == "logged_out"
    assert safety_events(audit, account_id)[0]["kind"] == "session_logged_out"
    assert safety_events(audit, account_id)[0]["severity"] == AccountLoggedOut.severity

    with pytest.raises(AccountLoggedOut):
        SafetyGate(clock=lambda: NOON).acquire(account_id, "post_like")


@pytest.mark.asyncio
async def test_detecting_the_same_challenge_twice_does_not_thrash_the_account_row(
    audit, account_id
):
    page = UrlOnlyPage(CHECKPOINT_URL)

    with pytest.raises(ChallengeDetected):
        await assert_page_clear(page, account_id=account_id, moment=NOON)
    with pytest.raises(ChallengeDetected) as second:
        await assert_page_clear(page, account_id=account_id, moment=NOON)

    assert second.value.record.transitioned is False
    assert second.value.record.event_written is False
    assert second.value.record.previous_state == "challenged"
    assert account_state(audit, account_id) == "challenged"
    assert len(safety_events(audit, account_id)) == 1
    assert not audit.connection.in_transaction


@pytest.mark.asyncio
async def test_an_unreadable_page_halts_without_flipping_the_account(audit, account_id):
    """Failing to read a page is not evidence that anyone was challenged."""
    with pytest.raises(PageUnreadable) as halted:
        await assert_page_clear(BlindPage(), account_id=account_id, moment=NOON)

    assert halted.value.record.transitioned is False
    assert halted.value.record.event_written is True
    assert account_state(audit, account_id) == "active"
    assert safety_events(audit, account_id)[0]["kind"] == "page_unreadable"


def test_a_softer_signal_never_overwrites_a_challenge(audit, account_id):
    set_account_state(audit, account_id, "challenged")
    halt = LoggedOutDetected(Detection(url="https://www.linkedin.com/login"))

    record = record_halt(halt, account_id=account_id, moment=NOON)

    assert record.transitioned is False
    assert record.event_written is False
    assert account_state(audit, account_id) == "challenged"
    assert safety_events(audit, account_id) == []


@pytest.mark.parametrize("state", ["paused", "cooldown"])
def test_a_challenge_found_on_a_resting_account_still_escalates(
    audit, account_id, state
):
    """Paused is a decision we made. Challenged is one LinkedIn made.

    Both stop the worker, so it is tempting to skip the write. Skipping it hides
    the challenge from `open_challenges`, and the account then resumes into it
    the moment someone unpauses it.
    """
    set_account_state(audit, account_id, state)

    record = record_halt(
        ChallengeDetected(Detection(url=CHECKPOINT_URL)),
        account_id=account_id,
        moment=NOON,
    )

    assert record.previous_state == state
    assert record.transitioned is True
    assert record.event_written is True
    assert account_state(audit, account_id) == "challenged"
    assert open_challenges(audit.connection)[0]["kind"] == "challenge_detected"


def test_a_challenge_on_a_logged_out_account_escalates_but_not_the_reverse(
    audit, account_id
):
    set_account_state(audit, account_id, "logged_out")

    up = record_halt(
        ChallengeDetected(Detection(url=AUTHWALL_URL)),
        account_id=account_id,
        moment=NOON,
    )
    down = record_halt(
        LoggedOutDetected(Detection(url="https://www.linkedin.com/uas/login")),
        account_id=account_id,
        moment=NOON,
    )

    assert up.transitioned is True
    assert down.transitioned is False
    assert account_state(audit, account_id) == "challenged"
    assert len(safety_events(audit, account_id)) == 1


def test_an_unreadable_page_adds_nothing_to_an_account_already_stopped(
    audit, account_id
):
    set_account_state(audit, account_id, "challenged")

    record = record_halt(
        PageUnreadable(Detection(url="")), account_id=account_id, moment=NOON
    )

    assert record.transitioned is False
    assert record.event_written is False
    assert safety_events(audit, account_id) == []


def test_no_code_path_in_this_module_clears_a_challenge():
    source = Path(detect.__file__).read_text(encoding="utf-8")

    assert source.count("UPDATE accounts") == 1
    assert re.search(r"SET\s+state\s*=\s*'", source) is None
    # The one UPDATE writes `halt.account_state`, and the ladder is the only
    # thing that lets a write through. `active` is not on the ladder, and no
    # halt claims it, so there is no route back to active from here.
    assert ACTIVE_STATE not in detect.STATE_RANK
    assert set(detect.STATE_RANK) == set(detect.HALTED_STATES)
    for halt_class in detect.HALT_CLASSES.values():
        assert halt_class.account_state in (None, *detect.HALTED_STATES)


def test_a_transaction_left_in_flight_is_refused_rather_than_joined(audit, account_id):
    audit.connection.execute("BEGIN")
    try:
        with pytest.raises(SafetyError, match="transaction in flight"):
            record_halt(
                ChallengeDetected(Detection(url=AUTHWALL_URL)),
                account_id=account_id,
            )
    finally:
        audit.connection.rollback()


@pytest.mark.asyncio
async def test_a_halt_the_database_could_not_record_is_still_a_halt(audit, account_id):
    audit.connection.execute(
        "CREATE TRIGGER block_safety_events AFTER INSERT ON safety_events "
        "BEGIN SELECT RAISE(ABORT, 'no safety events today'); END"
    )
    audit.connection.commit()

    with pytest.raises(ChallengeDetected) as halted:
        await assert_page_clear(UrlOnlyPage(AUTHWALL_URL), account_id=account_id)

    assert halted.value.record is None
    assert "no safety events today" in halted.value.record_error
    assert account_state(audit, account_id) == "active"
    assert not audit.connection.in_transaction


@pytest.mark.asyncio
async def test_a_halt_without_an_account_still_stops_the_run(audit, account_id):
    with pytest.raises(ChallengeDetected) as halted:
        await assert_page_clear(UrlOnlyPage(AUTHWALL_URL))

    assert halted.value.record is None
    assert account_state(audit, account_id) == "active"
    assert safety_events(audit, account_id) == []


# --- the delegation contract other navigation paths depend on ----------------


@pytest.mark.asyncio
async def test_one_call_with_a_page_and_an_account_does_the_whole_job(
    audit, account_id
):
    """The delegation target has to be cheap or it will be copied instead.

    `scrape/paginate.py` walks up to 100 pages per run, which makes it the
    highest volume navigation path in the codebase. If checking a page there
    meant threading a connection through the pagination loop, it would keep its
    own substring check, and a copy raises without recording anything. So this
    pins the contract: a page, an account id, and everything else is handled.
    """
    page = UrlOnlyPage(FEED_URL)
    page.url = "https://www.linkedin.com/authwall?trk=x"

    with pytest.raises(SessionExpiredError, match="Session expired") as expired:
        await assert_session_alive(
            page, account_id=account_id, action_type="profile_search"
        )

    assert account_state(audit, account_id) == "challenged"
    assert safety_events(audit, account_id)[0]["kind"] == "challenge_detected"
    assert open_challenges(audit.connection)[0]["account_id"] == account_id
    assert isinstance(expired.value.halt, ChallengeDetected)
    assert expired.value.detection.signal.marker == "/authwall"

    # The gate now refuses on its own, which is the point of writing the state.
    with pytest.raises(AccountChallenged):
        SafetyGate(clock=lambda: NOON).acquire(account_id, "profile_search")


@pytest.mark.asyncio
async def test_the_delegation_target_takes_no_connection_and_no_gate(audit, account_id):
    """A caller only has to know the page and the account.

    Anything else in the signature has to be optional, or a call site with just
    those two things in scope cannot delegate.
    """
    required = [
        name
        for name, parameter in signature(assert_session_alive).parameters.items()
        if parameter.default is Parameter.empty
    ]

    assert required == ["page"]
    with pytest.raises(SessionExpiredError):
        await assert_session_alive(UrlOnlyPage(AUTHWALL_URL), account_id=account_id)
    assert account_state(audit, account_id) == "challenged"


def test_the_detection_helper_is_reachable_from_the_browser_package():
    assert browser.assert_session_alive is assert_session_alive
    assert "assert_session_alive" in browser.__all__


@pytest.mark.asyncio
async def test_open_challenges_names_the_account_and_the_event_behind_it(
    audit, account_id
):
    assert open_challenges(audit.connection) == []

    with pytest.raises(ChallengeDetected):
        await assert_page_clear(
            DomPage(FEED_URL, html=RESTRICTED_HTML),
            account_id=account_id,
            moment=NOON,
        )

    rows = open_challenges(audit.connection)
    assert len(rows) == 1
    assert rows[0]["account_id"] == account_id
    assert rows[0]["label"] == "detect@example.com"
    assert rows[0]["state"] == "challenged"
    assert rows[0]["kind"] == "challenge_detected"
    assert rows[0]["severity"] == "critical"
    assert rows[0]["detail"]["marker"] == "we've restricted your account"
    assert rows[0]["detail"]["source"] == "banner"
    assert "detail_json" not in rows[0]


@pytest.mark.asyncio
async def test_recent_safety_events_reads_newest_first_and_filters_by_kind(
    audit, account_id
):
    with pytest.raises(PageUnreadable):
        await assert_page_clear(BlindPage(), account_id=account_id, moment=NOON)
    with pytest.raises(ChallengeDetected):
        await assert_page_clear(
            UrlOnlyPage(CHECKPOINT_URL), account_id=account_id, moment=NOON
        )

    events = recent_safety_events(audit.connection, account_id)
    assert [event["kind"] for event in events] == [
        "challenge_detected",
        "page_unreadable",
    ]

    only_challenges = recent_safety_events(
        audit.connection, account_id, kinds=("challenge_detected",)
    )
    assert len(only_challenges) == 1
    assert only_challenges[0]["detail"]["url"] == CHECKPOINT_URL
    assert recent_safety_events(audit.connection, account_id, limit=1) == events[:1]


def test_the_read_side_works_on_a_connection_with_no_row_factory(audit, account_id):
    record_halt(
        ChallengeDetected(Detection(url=AUTHWALL_URL)),
        account_id=account_id,
        moment=NOON,
    )
    plain = sqlite3.connect(audit.connection.execute("PRAGMA database_list").fetchone()["file"])
    try:
        rows = open_challenges(plain)
    finally:
        plain.close()

    assert [row["state"] for row in rows] == ["challenged"]


def test_every_kind_this_module_writes_is_declared_for_the_read_side():
    assert set(DETECTION_EVENT_KINDS) == {
        halt.event_kind for halt in detect.HALT_CLASSES.values()
    }


# --- navigation is where this actually runs ----------------------------------


@pytest.mark.asyncio
async def test_navigation_halts_on_an_authwall_and_records_it(audit, account_id):
    page = UrlOnlyPage(AUTHWALL_URL)

    with pytest.raises(SessionExpiredError, match="Session expired") as expired:
        await goto_profile(
            page, PROFILE_URL, humanizer=fast_humanizer(), account_id=account_id
        )

    assert isinstance(expired.value.halt, ChallengeDetected)
    assert expired.value.detection.signal.marker == "/authwall"
    assert account_state(audit, account_id) == "challenged"
    assert len(safety_events(audit, account_id)) == 1


@pytest.mark.asyncio
async def test_a_direct_load_is_checked_after_it_lands(audit, account_id):
    page = DirectLoadPage(CHECKPOINT_URL)

    with pytest.raises(SessionExpiredError) as expired:
        await goto_profile(
            page,
            PROFILE_URL,
            humanizer=fast_humanizer(),
            direct=True,
            account_id=account_id,
        )

    assert page.goto_calls == [PROFILE_URL]
    assert isinstance(expired.value.halt, ChallengeDetected)
    assert account_state(audit, account_id) == "challenged"


@pytest.mark.asyncio
async def test_navigation_without_an_account_halts_but_writes_nothing(
    audit, account_id
):
    with pytest.raises(SessionExpiredError):
        await goto_profile(
            UrlOnlyPage(CHECKPOINT_URL), PROFILE_URL, humanizer=fast_humanizer()
        )

    assert account_state(audit, account_id) == "active"
    assert safety_events(audit, account_id) == []


def test_no_tool_hand_rolls_the_login_wall_check_any_more():
    # MCP-03 (#26) moved the page-driving bodies out of linkedin_browser_mcp.py
    # into linkedin_mcp/executors/, so this now scans both. Scanning only the
    # server would have kept passing while the hand-rolled check was reborn one
    # import away, and a second detection check that raises without flipping
    # account state is invisible to a test that only watches the exception.
    sources = {
        path: path.read_text(encoding="utf-8")
        for path in [REPO_ROOT / "linkedin_browser_mcp.py"]
        + sorted((REPO_ROOT / "linkedin_mcp" / "executors").glob("*.py"))
    }

    for path, source in sources.items():
        assert "'login' in page.url" not in source, path
        assert "'authwall' in page.url" not in source, path
        assert '"login" in page.url' not in source, path
        assert '"authwall" in page.url' not in source, path

    callers = [
        path
        for path, source in sources.items()
        if "await check_page_is_ours(" in source
    ]
    assert callers, "the shared login-wall check has no caller left anywhere"

    helper = (
        REPO_ROOT / "linkedin_mcp" / "executors" / "support.py"
    ).read_text(encoding="utf-8")
    assert "assert_session_alive" in helper


# --- what a tool renders ------------------------------------------------------


@pytest.mark.asyncio
async def test_the_halt_renders_a_tool_result_that_keeps_the_marker(audit, account_id):
    with pytest.raises(ChallengeDetected) as halted:
        await assert_page_clear(
            DomPage(FEED_URL, html=RECAPTCHA_HTML),
            account_id=account_id,
            action_type="post_like",
            moment=NOON,
        )

    result = halted.value.to_result()
    assert result["status"] == "error"
    assert result["kind"] == CHALLENGE_KIND
    assert result["reason"] == RefusalReason.ACCOUNT_CHALLENGED.value
    assert result["account_state"] == "challenged"
    assert result["transitioned"] is True
    assert result["audit_logged"] is True
    assert result["detection"]["marker"] == "recaptcha"
    assert result["message"].startswith("Session expired:")


def test_every_halt_class_binds_itself_to_one_existing_refusal_reason():
    reasons = {
        halt.refusal_reason
        for halt in detect.HALT_CLASSES.values()
        if halt.refusal_reason is not None
    }

    assert reasons == {
        RefusalReason.ACCOUNT_CHALLENGED,
        RefusalReason.ACCOUNT_LOGGED_OUT,
    }
    assert all(issubclass(halt, DetectionHalt) for halt in detect.HALT_CLASSES.values())
