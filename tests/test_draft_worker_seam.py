"""SEQ-05 plugged into SEQ-04's real runner.

Not a mock of the worker. These tests build the merged `Worker` from
`linkedin_mcp.worker`, hand it `draft_parker`, and run real ticks. The point is
the claim both issues make from opposite sides: a campaign needing generated
text keeps running with no model attached, parks exactly one draft per step, and
sends only once a human approves.
"""

import socket
from datetime import datetime, timedelta, timezone

import pytest

from linkedin_mcp.audit.log import AuditLog, reset_audit_log, set_audit_log
from linkedin_mcp.drafts import (
    STATUS_APPROVED,
    STATUS_NEEDS_GENERATION,
    approve_draft,
    approved_text,
    count_drafts,
    draft_parker,
    list_pending,
    make_draft_parker,
    submit_draft,
)
from linkedin_mcp.drafts.errors import DraftNotApprovedError
from linkedin_mcp.leads import create_lead
from linkedin_mcp.sequences import (
    StepSpec,
    create_campaign,
    define_steps,
    enrol_lead,
    get_campaign_lead,
)
from linkedin_mcp.worker.actions import DraftKind, DraftRequest, no_draft_parker
from linkedin_mcp.worker.runner import build_worker

BASE_TIME = datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc)
GOOD_NOTE = (
    "Nived here, from the GitHub Copilot side at Microsoft. Saw your platform "
    "team is midway through a rollout. Happy to compare notes on what sticks."
)


@pytest.fixture()
def audit(tmp_path):
    log = AuditLog.open(tmp_path / "linkedin-helper.db")
    set_audit_log(log)
    try:
        yield log
    finally:
        reset_audit_log()
        log.close()


@pytest.fixture()
def conn(audit):
    return audit.connection


@pytest.fixture()
def account(audit):
    return audit.ensure_account("nived@example.com")


@pytest.fixture()
def lead(conn, account):
    return create_lead(
        conn,
        account,
        "Ada Lovelace",
        public_id="ada-lovelace",
        first_name="Ada",
        organization_name="Contoso",
    ).id


@pytest.fixture()
def campaign(conn, account):
    """A manual-approval campaign whose first step needs a connection note."""
    created = create_campaign(conn, account, "Q3 platform teams", status="active")
    define_steps(conn, created.id, [StepSpec("connection_request")])
    return created


def worker_for(conn, account, *, parker=draft_parker, clock=None):
    return build_worker(
        conn,
        account,
        worker_id="w-drafts",
        draft_parker=parker,
        clock=clock or (lambda: BASE_TIME),
    )


# --------------------------------------------------------------------------
# The contract SEQ-04 wrote down for SEQ-05
# --------------------------------------------------------------------------


def test_the_parker_satisfies_the_signature_seq_04_specified(conn, account, campaign, lead):
    """``Callable[[DraftRequest], int | None]`` returning the row id it wrote."""
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    request = DraftRequest(
        conn=conn,
        account_id=account,
        kind=DraftKind.CONNECTION_NOTE,
        campaign_id=campaign.id,
        lead_id=lead,
        context={"action_type": "connection_request", "source": "campaign"},
        now=BASE_TIME,
    )

    draft_id = draft_parker(request)

    assert isinstance(draft_id, int)
    parked = list_pending(conn, account)
    assert [draft.id for draft in parked] == [draft_id]
    assert parked[0].status == STATUS_NEEDS_GENERATION
    assert parked[0].kind == "connection_note"
    assert parked[0].context["source"] == "campaign"


def test_the_parker_is_idempotent_for_a_lead_and_step(conn, account, campaign, lead):
    """SEQ-04 re-refuses the step every hour, so this is the load-bearing one.

    A parker that inserted unconditionally would write a row an hour, forever.
    """
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)

    def park(hour):
        return draft_parker(
            DraftRequest(
                conn=conn,
                account_id=account,
                kind=DraftKind.CONNECTION_NOTE,
                campaign_id=campaign.id,
                lead_id=lead,
                context={"attempt": hour},
                now=BASE_TIME + timedelta(hours=hour),
            )
        )

    ids = {park(hour) for hour in range(24)}

    assert len(ids) == 1
    assert count_drafts(conn, account) == 1


def test_the_parker_never_reaches_the_network(conn, account, campaign, lead, monkeypatch):
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)

    def refuse(*args, **kwargs):  # pragma: no cover - only runs if the claim breaks
        raise AssertionError("the parker opened a socket")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)

    for kind in DraftKind:
        assert isinstance(
            draft_parker(
                DraftRequest(
                    conn=conn,
                    account_id=account,
                    kind=kind,
                    campaign_id=campaign.id,
                    lead_id=lead,
                    now=BASE_TIME,
                )
            ),
            int,
        )


def test_a_parker_can_stamp_the_model_it_expects(conn, account, campaign, lead):
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    parker = make_draft_parker(model="claude-opus-5")

    draft_id = parker(
        DraftRequest(
            conn=conn,
            account_id=account,
            kind=DraftKind.MESSAGE,
            campaign_id=campaign.id,
            lead_id=lead,
            now=BASE_TIME,
        )
    )

    assert list_pending(conn, account, kind="message")[0].model == "claude-opus-5"
    assert draft_id is not None


def test_an_ad_hoc_request_with_no_campaign_still_parks(conn, account):
    draft_id = draft_parker(
        DraftRequest(
            conn=conn,
            account_id=account,
            kind=DraftKind.COMMENT,
            context={"post_url": "https://www.linkedin.com/feed/update/urn:li:activity:1"},
            now=BASE_TIME,
        )
    )

    parked = list_pending(conn, account, kind="comment")
    assert [draft.id for draft in parked] == [draft_id]
    assert parked[0].campaign_id is None


def test_a_request_owned_by_nobody_parks_nothing_rather_than_guessing(conn):
    request = DraftRequest(conn=conn, account_id=None, kind=DraftKind.COMMENT)

    assert draft_parker(request) is None


# --------------------------------------------------------------------------
# The real runner, ticking, with no model attached
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_tick_parks_a_draft_and_the_campaign_keeps_moving(
    conn, account, campaign, lead
):
    """The whole claim of both issues, run through the merged worker."""
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    worker = worker_for(conn, account)

    await worker.tick()

    record = get_campaign_lead(conn, campaign.id, lead)
    parked = list_pending(conn, account)

    assert len(parked) == 1
    assert parked[0].status == STATUS_NEEDS_GENERATION
    # Refused, not failed: the lead is back in the queue with its attempts intact.
    assert record.sublist == "queue"
    assert record.last_outcome == "refused: approval_required"
    assert record.attempts == 0


@pytest.mark.asyncio
async def test_ticking_all_week_leaves_one_draft_not_a_pile(conn, account, campaign, lead):
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    clock = {"now": BASE_TIME}
    worker = worker_for(conn, account, clock=lambda: clock["now"])

    for hour in range(12):
        clock["now"] = BASE_TIME + timedelta(hours=hour * 2)
        await worker.tick()

    assert count_drafts(conn, account) == 1
    assert get_campaign_lead(conn, campaign.id, lead).sublist == "queue"


@pytest.mark.asyncio
async def test_the_worker_still_parks_cleanly_with_no_parker_at_all(
    conn, account, campaign, lead
):
    """SEQ-04's no-op default. No drafts package, and still nothing unapproved."""
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    worker = worker_for(conn, account, parker=no_draft_parker)

    await worker.tick()

    assert count_drafts(conn, account) == 0
    assert get_campaign_lead(conn, campaign.id, lead).sublist == "queue"


@pytest.mark.asyncio
async def test_a_broken_parker_cannot_stop_the_loop(conn, account, campaign, lead):
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)

    def explode(request):
        raise RuntimeError("the drafts package fell over")

    worker = worker_for(conn, account, parker=explode)

    await worker.tick()

    assert get_campaign_lead(conn, campaign.id, lead).sublist == "queue"


@pytest.mark.asyncio
async def test_nothing_is_sendable_until_a_human_approves(conn, account, campaign, lead):
    """End to end across both packages: tick, generate, approve, release."""
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    worker = worker_for(conn, account)
    await worker.tick()

    parked = list_pending(conn, account)[0]
    submit_draft(conn, parked.id, text=GOOD_NOTE, model="claude-opus-5")

    # Generated, reviewed by nobody, and therefore unusable.
    with pytest.raises(DraftNotApprovedError):
        approved_text(conn, parked.id)

    approve_draft(conn, parked.id)

    assert approved_text(conn, parked.id) == GOOD_NOTE
    assert list_pending(conn, account, status=STATUS_APPROVED)[0].id == parked.id
