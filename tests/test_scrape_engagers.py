"""Post likers and commenters, the centrepiece source of SCRAPE-04.

The fake post below is the only place in the suite where two lists live on one
page: a reactions modal that is not in the DOM until its count button is
clicked, and a comment list that is always there. That is what makes the
combined run worth testing rather than assuming, because the interesting
behaviour is at the seam between the two phases.

Nothing here reaches LinkedIn, sleeps, or asks a real safety gate.
"""

import asyncio
from datetime import datetime, timezone

import pytest
from test_scrape_fakes import FakeElement, FakeGate, FakePage, FakeRecorder, RecordingSleep
from test_scrape_sources import ListRow, list_row

from linkedin_mcp.browser.humanize import FAST, Humanizer
from linkedin_mcp.browser.selectors import selector_fallbacks
from linkedin_mcp.core.config import METERED_ACTIONS
from linkedin_mcp.core.db import initialize_database
from linkedin_mcp.leads import blacklist_identity, count_leads, get_lead_by_public_id
from linkedin_mcp.scrape import (
    SOURCE_POST_COMMENTS,
    SOURCE_POST_ENGAGERS,
    SOURCE_POST_REACTIONS,
    PostEngagement,
    StopReason,
    harvest_run,
    run_post_comment_harvest,
    run_post_engager_harvest,
    run_post_reaction_harvest,
)
from linkedin_mcp.scrape.engagers import (
    COMMENTS_SURFACE,
    POST_ENGAGERS_ACTION,
    REACTIONS_SURFACE,
)

NOW = datetime(2026, 8, 2, 9, 0, tzinfo=timezone.utc)
POST = "urn:li:activity:7123456789"
PERMALINK = "https://www.linkedin.com/feed/update/urn:li:activity:7123456789/"


def run(coroutine):
    return asyncio.run(coroutine)


def pacer():
    return Humanizer(FAST, seed=11, sleep=RecordingSleep())


def clock():
    return NOW


@pytest.fixture()
def conn(tmp_path):
    connection = initialize_database(tmp_path / "linkedin-helper.db")
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture()
def account(conn):
    cursor = conn.execute(
        "INSERT INTO accounts (label, timezone, state) VALUES (?, ?, ?)",
        ("primary", "Asia/Kolkata", "active"),
    )
    conn.commit()
    return int(cursor.lastrowid)


def harvest_kwargs(**overrides):
    kwargs = {
        "humanizer": pacer(),
        "guard": FakeGate(),
        "record": FakeRecorder(),
        "clock": clock,
    }
    kwargs.update(overrides)
    return kwargs


class PostPage(FakePage):
    """A post permalink carrying a reactions modal and a comment list.

    The reactors are absent from the page until the count button is clicked,
    which is the behaviour that makes the modal worth a fake at all: a run that
    forgets to open it reads an empty list and reports finding nobody, and that
    is a bug a test has to be able to see.
    """

    def __init__(
        self,
        reactors: list[ListRow] | None = None,
        commenters: list[ListRow] | None = None,
        *,
        step: int = 3,
    ) -> None:
        super().__init__(url="https://www.linkedin.com/feed/")
        self.reactors = list(reactors or [])
        self.commenters = list(commenters or [])
        self.step = step
        self.reactors_shown = step
        self.comments_shown = step
        self.opened = False
        self.opener = FakeElement(
            selectors=(selector_fallbacks("post_reactions_trigger")[0],),
            on_click=self._open,
        )
        self.reactions_button = FakeElement(
            selectors=(selector_fallbacks("post_reactions_load_more")[0],),
            on_click=self._more_reactors,
        )
        self.comments_button = FakeElement(
            selectors=(selector_fallbacks("post_comments_load_more")[0],),
            on_click=self._more_comments,
        )

    def _open(self) -> None:
        self.opened = True

    def _more_reactors(self) -> None:
        self.reactors_shown = min(len(self.reactors), self.reactors_shown + self.step)

    def _more_comments(self) -> None:
        self.comments_shown = min(len(self.commenters), self.comments_shown + self.step)

    async def goto(self, url: str, **kwargs) -> None:
        await super().goto(url, **kwargs)
        self.opened = False
        self.reactors_shown = self.step
        self.comments_shown = self.step

    @property
    def visible(self) -> list[FakeElement]:
        shown: list[FakeElement] = [self.opener]
        if self.opened:
            shown.extend(self.reactors[: self.reactors_shown])
            if self.reactors_shown < len(self.reactors):
                shown.append(self.reactions_button)
        shown.extend(self.commenters[: self.comments_shown])
        if self.comments_shown < len(self.commenters):
            shown.append(self.comments_button)
        return shown


def reactors(count: int, *, start: int = 0) -> list[ListRow]:
    return [
        list_row(REACTIONS_SURFACE, slug=f"reactor-{index}", name=f"Reactor {index}")
        for index in range(start, start + count)
    ]


def commenters(count: int, *, start: int = 0) -> list[ListRow]:
    return [
        list_row(
            COMMENTS_SURFACE,
            slug=f"commenter-{index}",
            name=f"Commenter {index}",
            distance=None,
        )
        for index in range(start, start + count)
    ]


# --- Reactions ------------------------------------------------------------


def test_a_reaction_run_opens_the_modal_before_reading_it(conn, account):
    page = PostPage(reactors(6))

    summary = run(
        run_post_reaction_harvest(page, conn, account, POST, limit=6, **harvest_kwargs())
    )

    assert page.opener.clicks == 1, "the modal is not in the DOM until it is opened"
    assert summary.source == SOURCE_POST_REACTIONS
    assert summary.results_new == 6
    assert summary.leads_created == 6
    assert page.goto_urls == [PERMALINK]


def test_a_reaction_run_that_never_opened_the_modal_would_find_nobody():
    """Guard the guard: prove the fake really hides the reactors."""
    page = PostPage(reactors(6))

    assert run(page.query_selector_all(selector_fallbacks("post_reactor_item")[0])) == []


def test_the_modal_pages_by_clicking_its_own_load_more(conn, account):
    page = PostPage(reactors(9), step=3)

    summary = run(
        run_post_reaction_harvest(page, conn, account, POST, limit=50, **harvest_kwargs())
    )

    assert summary.results_new == 9
    assert summary.stop_reason is StopReason.NO_NEW_RESULTS
    assert page.reactions_button.clicks >= 2


def test_a_reactor_reads_into_a_lead_with_a_headline(conn, account):
    page = PostPage([list_row(REACTIONS_SURFACE, slug="nived-velayudhan")])

    run(run_post_reaction_harvest(page, conn, account, POST, limit=1, **harvest_kwargs()))

    stored = get_lead_by_public_id(conn, account, "nived-velayudhan")
    assert stored is not None
    assert stored.full_name == "Nived Velayudhan"
    assert stored.headline == "Solution Engineer at Microsoft"
    assert stored.member_distance == "2nd"


# --- Comments -------------------------------------------------------------


def test_a_comment_run_reads_the_inline_list_without_opening_anything(conn, account):
    page = PostPage(reactors(5), commenters(4))

    summary = run(
        run_post_comment_harvest(page, conn, account, POST, limit=4, **harvest_kwargs())
    )

    assert page.opener.clicks == 0, "comments are already on the page"
    assert summary.source == SOURCE_POST_COMMENTS
    assert summary.leads_created == 4
    assert get_lead_by_public_id(conn, account, "reactor-0") is None


def test_the_comment_list_pages_by_clicking_load_more(conn, account):
    page = PostPage(commenters=commenters(9), step=3)

    summary = run(
        run_post_comment_harvest(page, conn, account, POST, limit=50, **harvest_kwargs())
    )

    assert summary.results_new == 9
    assert page.comments_button.clicks >= 2


# --- Both phases ----------------------------------------------------------


def test_a_combined_run_harvests_reactors_and_commenters(conn, account):
    page = PostPage(reactors(6), commenters(6))

    summary = run(
        run_post_engager_harvest(
            page, conn, account, POST, limit=20, **harvest_kwargs()
        )
    )

    assert summary.source == SOURCE_POST_ENGAGERS
    assert summary.results_new == 12
    assert summary.leads_created == 12
    assert count_leads(conn, account) == 12
    assert page.goto_urls == [PERMALINK, PERMALINK]


def test_someone_who_both_reacted_and_commented_is_one_lead(conn, account):
    """The whole point of seeding the second phase with the first phase's keys."""
    both_reacted = list_row(REACTIONS_SURFACE, slug="double-dipper", name="Double Dipper")
    both_commented = list_row(
        COMMENTS_SURFACE, slug="double-dipper", name="Double Dipper", distance=None
    )
    page = PostPage(
        [both_reacted, *reactors(2)],
        [both_commented, *commenters(2)],
    )

    summary = run(
        run_post_engager_harvest(
            page, conn, account, POST, limit=20, **harvest_kwargs()
        )
    )

    assert summary.results_new == 5, "the person was counted once"
    assert [person.public_id for person in summary.people].count("double-dipper") == 1
    assert summary.duplicates_skipped >= 1
    assert count_leads(conn, account) == 5


def test_the_limit_covers_the_post_rather_than_each_list(conn, account):
    page = PostPage(reactors(10), commenters(10), step=5)

    summary = run(
        run_post_engager_harvest(
            page, conn, account, POST, limit=12, **harvest_kwargs()
        )
    )

    assert summary.results_new == 12
    assert summary.stop_reason is StopReason.COUNT_REACHED
    assert count_leads(conn, account) == 12


def test_reactions_filling_the_limit_means_no_comments_phase(conn, account):
    page = PostPage(reactors(10), commenters(10), step=5)

    summary = run(
        run_post_engager_harvest(page, conn, account, POST, limit=5, **harvest_kwargs())
    )

    assert summary.results_new == 5
    assert page.goto_urls == [PERMALINK], "the comments phase never loaded the post"


def test_a_gate_refusal_in_the_reactions_phase_stops_the_run_there(conn, account):
    page = PostPage(reactors(9), commenters(9), step=3)

    summary = run(
        run_post_engager_harvest(
            page, conn, account, POST, limit=50,
            **harvest_kwargs(guard=FakeGate(allow=1)),
        )
    )

    assert summary.refused
    assert summary.stop_reason is StopReason.GATE_REFUSED
    assert summary.gate_refusal["reason"] == "daily_cap_reached"
    assert page.goto_urls == [PERMALINK], (
        "asking the gate again for a budget it just refused is the bug the "
        "per-fetch gate exists to prevent"
    )
    assert summary.leads_created == 3, "what it got before the refusal is kept"


def test_a_combined_run_writes_one_harvest_run_row_not_two(conn, account):
    page = PostPage(reactors(3), commenters(3))

    summary = run(
        run_post_engager_harvest(
            page, conn, account, POST, limit=20, **harvest_kwargs()
        )
    )

    assert conn.execute("SELECT COUNT(*) FROM harvest_runs").fetchone()[0] == 1
    stored = harvest_run(conn, summary.harvest_run_id)
    assert stored["source_type"] == SOURCE_POST_ENGAGERS
    assert stored["params"]["filters"]["post_url"] == PERMALINK
    assert stored["params"]["filters"]["activity_id"] == "7123456789"
    assert stored["params"]["filters"]["engagement"] == "all"
    assert stored["new_count"] == 6
    assert stored["finished_at"] is not None


# --- Selection, safety and blacklist --------------------------------------


@pytest.mark.parametrize(
    ("engagement", "expected"),
    [
        (PostEngagement.REACTIONS, SOURCE_POST_REACTIONS),
        ("reactions", SOURCE_POST_REACTIONS),
        (PostEngagement.COMMENTS, SOURCE_POST_COMMENTS),
        ("comments", SOURCE_POST_COMMENTS),
    ],
)
def test_a_single_phase_run_reports_its_own_source(conn, account, engagement, expected):
    page = PostPage(reactors(3), commenters(3))

    summary = run(
        run_post_engager_harvest(
            page, conn, account, POST, engagement=engagement, limit=3,
            **harvest_kwargs(),
        )
    )

    assert summary.source == expected


def test_an_unknown_engagement_is_refused(conn, account):
    with pytest.raises(ValueError):
        run(
            run_post_engager_harvest(
                PostPage(), conn, account, POST, engagement="likes",
                **harvest_kwargs(),
            )
        )


def test_a_limit_below_one_is_refused(conn, account):
    with pytest.raises(ValueError):
        run(
            run_post_engager_harvest(
                PostPage(), conn, account, POST, limit=0, **harvest_kwargs()
            )
        )


def test_engagers_spend_a_budget_the_config_has_heard_of():
    assert POST_ENGAGERS_ACTION == "post_read"
    assert POST_ENGAGERS_ACTION in METERED_ACTIONS
    assert REACTIONS_SURFACE.action_type == POST_ENGAGERS_ACTION
    assert COMMENTS_SURFACE.action_type == POST_ENGAGERS_ACTION


@pytest.mark.parametrize(
    "surface", [REACTIONS_SURFACE, COMMENTS_SURFACE], ids=lambda s: s.source
)
def test_every_engager_selector_has_a_spare(surface):
    """LinkedIn's social detail markup churns hard. One selector is not a plan."""
    named = [
        surface.item,
        surface.link,
        surface.name,
        surface.headline,
        surface.avatar,
        surface.distance,
        surface.load_more,
        surface.opener,
    ]
    for name in [entry for entry in named if entry]:
        assert len(selector_fallbacks(name)) >= 2, f"{name} has no fallback"


@pytest.mark.parametrize(
    "surface", [REACTIONS_SURFACE, COMMENTS_SURFACE], ids=lambda s: s.source
)
def test_engager_markup_only_a_later_fallback_matches_still_resolves(surface):
    from linkedin_mcp.scrape.sources import extract_people_list

    page = FakePage({1: [list_row(surface, slug="old-markup", fallback=1)]})

    people = run(extract_people_list(page, surface))

    assert [person.public_id for person in people] == ["old-markup"]


def test_the_gate_is_asked_before_every_slice_of_both_phases(conn, account):
    page = PostPage(reactors(6), commenters(6), step=3)
    gate = FakeGate()

    run(
        run_post_engager_harvest(
            page, conn, account, POST, limit=50, **harvest_kwargs(guard=gate)
        )
    )

    assert len(gate.calls) >= 4
    assert {call["action_type"] for call in gate.calls} == {POST_ENGAGERS_ACTION}


def test_a_blacklisted_engager_is_refused_rather_than_resurrected(conn, account):
    blacklist_identity(conn, account, public_id="reactor-1", reason="asked to stop")
    page = PostPage(reactors(3), commenters(3))

    summary = run(
        run_post_engager_harvest(
            page, conn, account, POST, limit=20, **harvest_kwargs()
        )
    )

    assert get_lead_by_public_id(conn, account, "reactor-1") is None
    assert summary.leads_created == 5
    assert {refusal.reason for refusal in summary.harvest.refusals} == {"blacklisted"}


def test_a_dry_run_reads_both_lists_without_writing(conn, account):
    page = PostPage(reactors(4), commenters(4))

    summary = run(
        run_post_engager_harvest(
            page, conn, account, POST, limit=20, harvest=False, **harvest_kwargs()
        )
    )

    assert summary.results_new == 8
    assert count_leads(conn, account) == 0
    assert conn.execute("SELECT COUNT(*) FROM harvest_runs").fetchone()[0] == 0


def test_a_post_engager_and_an_event_attendee_resolve_onto_one_lead(conn, account):
    """Requirement two of the issue, proved across two genuinely different sources."""
    from test_scrape_sources import PeopleListPage
    from linkedin_mcp.scrape import run_event_attendee_harvest
    from linkedin_mcp.scrape.events import ATTENDEE_SURFACE

    shared = "shared-human"
    post_page = PostPage(
        [
            list_row(REACTIONS_SURFACE, slug=shared, name="Shared Human"),
            *reactors(2),
        ]
    )
    event_page = PeopleListPage(
        [
            list_row(ATTENDEE_SURFACE, slug=shared, name="Shared Human"),
            list_row(ATTENDEE_SURFACE, slug="attendee-only", name="Attendee Only"),
        ],
        ATTENDEE_SURFACE,
    )

    engagers = run(
        run_post_reaction_harvest(
            post_page, conn, account, POST, limit=20, **harvest_kwargs()
        )
    )
    attendees = run(
        run_event_attendee_harvest(
            event_page, conn, account, "1234567890", limit=20, **harvest_kwargs()
        )
    )

    assert engagers.leads_created == 3
    assert attendees.leads_created == 1
    assert count_leads(conn, account) == 4
