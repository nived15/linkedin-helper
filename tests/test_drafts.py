"""SEQ-05: the `ai_drafts` queue, its lifecycle and the approval rule.

Every test here is offline and deterministic. Clocks are passed in, no model is
called, and one test disables sockets outright to prove that parking a draft
reaches nothing.
"""

import inspect
import json
import re
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from linkedin_mcp.core.db import initialize_database
from linkedin_mcp.drafts import (
    DRAFT_KINDS,
    DRAFT_STATUSES,
    MAX_TEXT_LENGTH,
    STATUS_APPROVED,
    STATUS_NEEDS_GENERATION,
    STATUS_PENDING_APPROVAL,
    STATUS_REJECTED,
    STATUS_SENT,
    TEXT_KINDS,
    VERDICT_KINDS,
    DraftChangedError,
    DraftNotApprovedError,
    DraftNotFoundError,
    DraftOwnershipError,
    DraftStateError,
    DraftStyleError,
    UnknownDraftKindError,
    approve_draft,
    approved_fragments,
    approved_text,
    auto_approves,
    count_drafts,
    defer_for_approval,
    draft_context,
    ensure_draft,
    ensure_fragment_drafts,
    fragment_source,
    get_draft,
    list_drafts,
    list_pending,
    mark_sent,
    open_draft_for,
    park_draft,
    pending_fragments,
    request_draft,
    require_draft,
    style_brief,
    submit_draft,
    validate_text,
)
from linkedin_mcp.leads import create_lead
from linkedin_mcp.sequences import (
    StepSpec,
    claim_step,
    create_campaign,
    define_steps,
    enrol_lead,
    get_campaign_lead,
    step_at_ord,
)
from linkedin_mcp.templating import (
    FORBIDDEN_DASHES,
    RenderRefusalReason,
    create_template,
    safe_render_template,
    style_violations,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
BASE_TIME = datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc)

GOOD_NOTE = (
    "Nived here, from the GitHub Copilot side at Microsoft. Saw your platform "
    "team is midway through a rollout. Happy to compare notes on what sticks."
)
EM_DASH_NOTE = (
    "Nived here \u2014 working with teams deploying Copilot at scale. "
    "Happy to compare notes."
)


def at(seconds: float) -> datetime:
    return BASE_TIME + timedelta(seconds=seconds)


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


@pytest.fixture()
def lead(conn, account):
    return create_lead(
        conn,
        account,
        "Ada Lovelace",
        public_id="ada-lovelace",
        first_name="Ada",
        headline="Platform engineering lead",
        organization_name="Contoso",
    ).id


def make_campaign(conn, account_id, name="Q3 platform teams", *, approval_mode="manual_drafts"):
    campaign = create_campaign(
        conn, account_id, name, status="active", approval_mode=approval_mode
    )
    define_steps(
        conn,
        campaign.id,
        [
            StepSpec("connection_request", config={"priority": 5}),
            StepSpec("message", config={"delay_seconds": 3600}),
        ],
    )
    return campaign


@pytest.fixture()
def campaign(conn, account):
    return make_campaign(conn, account)


@pytest.fixture()
def auto_campaign(conn, account):
    return make_campaign(conn, account, "Auto approved", approval_mode="auto")


# --------------------------------------------------------------------------
# The vocabulary is the schema's, not ours
# --------------------------------------------------------------------------


def check_values(table: str, column: str) -> set[str]:
    sql = (REPO_ROOT / "linkedin_mcp" / "core" / "migrations" / "0001_init.sql").read_text(
        encoding="utf-8"
    )
    table_body = re.search(
        rf"CREATE TABLE IF NOT EXISTS {table} \((.*?)\n\);", sql, re.DOTALL
    )
    assert table_body, f"{table} is missing from 0001_init.sql"
    constraint = re.search(
        rf"{column} TEXT NOT NULL CHECK \({column} IN \(([^)]*)\)\)", table_body.group(1)
    )
    assert constraint, f"{table}.{column} has no CHECK constraint"
    return set(re.findall(r"'([a-z_]+)'", constraint.group(1)))


def test_the_four_draft_kinds_are_exactly_the_schema_check_constraint():
    assert set(DRAFT_KINDS) == check_values("ai_drafts", "kind")
    assert set(DRAFT_KINDS) == {
        "connection_note",
        "message",
        "comment",
        "icp_evaluation",
    }


def test_the_five_statuses_are_exactly_the_schema_check_constraint():
    assert set(DRAFT_STATUSES) == check_values("ai_drafts", "status")


def test_text_kinds_and_verdict_kinds_partition_the_four():
    assert set(TEXT_KINDS) | set(VERDICT_KINDS) == set(DRAFT_KINDS)
    assert set(TEXT_KINDS).isdisjoint(VERDICT_KINDS)


def test_an_unknown_kind_is_refused(conn, account):
    with pytest.raises(UnknownDraftKindError):
        park_draft(conn, account_id=account, kind="carrier_pigeon")


# --------------------------------------------------------------------------
# The worker never blocks on a model, and nothing is connected
# --------------------------------------------------------------------------


def test_parking_every_kind_reaches_nothing_at_all(conn, account, campaign, lead, monkeypatch):
    """The central claim of this issue, asserted with the network disabled.

    If an LLM being absent can stall a campaign, the design is false. So this
    parks all four kinds with sockets raising on contact and asserts every row
    lands, immediately, in `needs_generation`.
    """

    def refuse(*args, **kwargs):  # pragma: no cover - only runs if the claim breaks
        raise AssertionError("a draft parked something over the network")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)

    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    parked = [
        park_draft(
            conn,
            account_id=account,
            kind=kind,
            campaign_id=campaign.id,
            lead_id=lead,
            context={"kind": kind},
            now=at(index),
        )
        for index, kind in enumerate(DRAFT_KINDS)
    ]

    assert [draft.kind for draft in parked] == list(DRAFT_KINDS)
    assert {draft.status for draft in parked} == {STATUS_NEEDS_GENERATION}
    assert all(draft.generated_text is None for draft in parked)
    assert all(draft.verdict is None for draft in parked)
    assert count_drafts(conn, account) == 4


def test_a_campaign_still_moves_while_every_draft_is_unwritten(conn, account, campaign, lead):
    """Nothing generated, nothing approved, and the lead is still in the flow."""
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    claim_step(conn, campaign.id, lead, worker_id="w1", now=at(1))

    gate = ensure_draft(conn, campaign.id, lead, "connection_note", now=at(2))
    assert gate.parked is True
    assert gate.ready is False

    record = defer_for_approval(conn, campaign.id, lead, now=at(3))

    assert record.sublist == "queue"
    assert record.last_outcome == "refused: approval_required"
    # A refusal is the gate declining, not the step failing, so the step keeps
    # every one of its attempts. Waiting on a human is free.
    assert record.attempts == 0
    assert record.next_run_at > "2026-06-01 09:00:03"


def test_deferring_over_and_over_never_exhausts_the_step(conn, account, campaign, lead):
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    for tick in range(5):
        claim_step(conn, campaign.id, lead, worker_id="w1", now=at(100 * tick))
        ensure_draft(conn, campaign.id, lead, "connection_note", now=at(100 * tick + 1))
        record = defer_for_approval(conn, campaign.id, lead, now=at(100 * tick + 2))

    assert record.sublist == "queue"
    assert record.attempts == 0
    assert get_campaign_lead(conn, campaign.id, lead).current_step_ord == 1
    # Five ticks, one draft. A retried step must not fill the review queue.
    assert count_drafts(conn, account) == 1


def test_an_open_draft_is_reused_rather_than_duplicated(conn, campaign, lead):
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    step = step_at_ord(conn, campaign.id, 1)

    first = ensure_draft(conn, campaign.id, lead, "connection_note", step=step)
    second = ensure_draft(conn, campaign.id, lead, "connection_note", step=step)

    assert first.parked is True
    assert second.parked is False
    assert first.draft.id == second.draft.id


def test_a_rejected_draft_does_not_block_a_fresh_one(conn, campaign, lead):
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    step = step_at_ord(conn, campaign.id, 1)

    first = ensure_draft(conn, campaign.id, lead, "connection_note", step=step)
    submit_draft(conn, first.draft.id, text=GOOD_NOTE)
    approve_draft(conn, first.draft.id, approved=False, note="too salesy")

    second = ensure_draft(conn, campaign.id, lead, "connection_note", step=step)

    assert second.parked is True
    assert second.draft.id != first.draft.id
    assert open_draft_for(conn, campaign.id, lead, "connection_note").id == second.draft.id


# --------------------------------------------------------------------------
# The context is what makes parking and walking away possible
# --------------------------------------------------------------------------


def test_the_parked_context_carries_the_lead_and_the_voice_rules(conn, campaign, lead):
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    draft = request_draft(conn, campaign.id, lead, "connection_note")

    assert draft.context["lead"]["first_name"] == "Ada"
    assert draft.context["lead"]["organization_name"] == "Contoso"
    assert draft.context["tokens"]["firstName"] == "Ada"
    assert draft.context["campaign"]["name"] == "Q3 platform teams"
    assert draft.context["step"]["action_type"] == "connection_request"
    assert draft.context["voice"]["max_characters"] == MAX_TEXT_LENGTH["connection_note"]
    assert "\u2014" in draft.context["voice"]["no_dashes"]


def test_the_voice_brief_is_read_off_the_policy_that_will_judge_it():
    brief = style_brief("message")

    assert set(brief["no_dashes"]) == set(FORBIDDEN_DASHES)
    assert "in today's world" in brief["banned_openers"]
    assert brief["max_sentence_words"] == 30


def test_the_context_is_stored_as_json_not_as_a_repr(conn, campaign, lead):
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    draft = request_draft(conn, campaign.id, lead, "message")

    raw = conn.execute(
        "SELECT context_json FROM ai_drafts WHERE id = ?", (draft.id,)
    ).fetchone()["context_json"]

    assert json.loads(raw)["lead"]["full_name"] == "Ada Lovelace"


def test_an_icp_context_states_the_shape_the_client_must_return(conn, account, lead):
    context = draft_context(conn, "icp_evaluation", lead_id=lead)

    assert set(context["expected_output"]) == {"match", "score", "reason"}


# --------------------------------------------------------------------------
# Style: one validator, SEQ-02's, and an em dash cannot survive it
# --------------------------------------------------------------------------


def test_a_submitted_draft_containing_an_em_dash_is_rejected(conn, campaign, lead):
    """The single most important style test in this issue.

    Generated text is the likeliest source of an em dash anywhere in the system.
    It is refused rather than repaired, and the row keeps its old status so the
    client can regenerate against the same context.
    """
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    draft = request_draft(conn, campaign.id, lead, "connection_note")

    with pytest.raises(DraftStyleError) as error:
        submit_draft(conn, draft.id, text=EM_DASH_NOTE)

    assert "forbidden dash" in str(error.value)
    unchanged = require_draft(conn, draft.id)
    assert unchanged.status == STATUS_NEEDS_GENERATION
    assert unchanged.generated_text is None


@pytest.mark.parametrize("dash", FORBIDDEN_DASHES)
def test_no_dash_like_character_can_be_submitted(conn, campaign, lead, dash):
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    draft = request_draft(conn, campaign.id, lead, "message")

    with pytest.raises(DraftStyleError):
        submit_draft(conn, draft.id, text=f"Saw the rollout at Contoso {dash} worth a chat.")


def test_a_filler_opener_is_rejected(conn, campaign, lead):
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    draft = request_draft(conn, campaign.id, lead, "comment")

    with pytest.raises(DraftStyleError) as error:
        submit_draft(conn, draft.id, text="In today's world, every team ships with AI.")

    assert "filler" in str(error.value)


def test_a_long_sentence_is_rejected(conn, campaign, lead):
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    draft = request_draft(conn, campaign.id, lead, "message")
    runaway = "We " + " ".join(f"word{index}" for index in range(40)) + " ship."

    with pytest.raises(DraftStyleError) as error:
        submit_draft(conn, draft.id, text=runaway)

    assert "sentence" in str(error.value)


def test_an_empty_submission_is_rejected(conn, campaign, lead):
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    draft = request_draft(conn, campaign.id, lead, "message")

    with pytest.raises(DraftStyleError):
        submit_draft(conn, draft.id, text="   \n  ")


def test_a_connection_note_over_linkedins_own_limit_is_rejected(conn, campaign, lead):
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    draft = request_draft(conn, campaign.id, lead, "connection_note")
    too_long = "Great work at Contoso. " * 20

    assert len(too_long) > MAX_TEXT_LENGTH["connection_note"]
    with pytest.raises(DraftStyleError) as error:
        submit_draft(conn, draft.id, text=too_long)

    assert "over the limit" in str(error.value)


def test_validation_defers_to_seq_02_rather_than_re_deciding():
    """Whatever SEQ-02 flags, this package refuses. No second opinion."""
    samples = [
        EM_DASH_NOTE,
        "Let's be honest, nobody reads these.",
        "We " + " ".join(f"word{index}" for index in range(40)) + " ship.",
    ]
    for text in samples:
        assert style_violations(text)
        with pytest.raises(DraftStyleError):
            validate_text("message", text)

    assert style_violations(GOOD_NOTE) == []
    assert validate_text("message", GOOD_NOTE) == GOOD_NOTE


def test_the_repository_holds_exactly_one_style_validator():
    """A second style checker would drift, and the weaker one would win."""
    package = REPO_ROOT / "linkedin_mcp" / "drafts"
    for path in package.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "FORBIDDEN_DASHES = " not in source
        assert "def style_violations" not in source


# --------------------------------------------------------------------------
# The lifecycle, and the approval rule that guards its exit
# --------------------------------------------------------------------------


def test_a_submitted_draft_waits_at_pending_approval(conn, campaign, lead):
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    draft = request_draft(conn, campaign.id, lead, "connection_note")

    submitted = submit_draft(conn, draft.id, text=GOOD_NOTE, model="claude-opus-5")

    assert submitted.status == STATUS_PENDING_APPROVAL
    assert submitted.generated_text == GOOD_NOTE
    assert submitted.model == "claude-opus-5"
    assert submitted.decided_at is None


def test_unapproved_text_cannot_be_released(conn, campaign, lead):
    """The negative case the definition of done names. Text exists; it cannot go."""
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    draft = request_draft(conn, campaign.id, lead, "connection_note")
    submit_draft(conn, draft.id, text=GOOD_NOTE)

    with pytest.raises(DraftNotApprovedError):
        approved_text(conn, draft.id)
    with pytest.raises(DraftNotApprovedError):
        mark_sent(conn, draft.id)

    assert require_draft(conn, draft.id).status == STATUS_PENDING_APPROVAL


def test_approval_is_what_releases_the_text(conn, campaign, lead):
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    draft = request_draft(conn, campaign.id, lead, "connection_note")
    submit_draft(conn, draft.id, text=GOOD_NOTE)

    released = approve_draft(conn, draft.id, now=at(60))

    assert released.status == STATUS_APPROVED
    assert released.decided_at == "2026-06-01 09:01:00"
    assert approved_text(conn, draft.id) == GOOD_NOTE
    assert mark_sent(conn, draft.id, now=at(61)).status == STATUS_SENT


def test_a_rejected_draft_is_never_releasable(conn, campaign, lead):
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    draft = request_draft(conn, campaign.id, lead, "connection_note")
    submit_draft(conn, draft.id, text=GOOD_NOTE)

    rejected = approve_draft(conn, draft.id, approved=False, note="reads like a vendor")

    assert rejected.status == STATUS_REJECTED
    assert rejected.context["approval_note"] == "reads like a vendor"
    with pytest.raises(DraftNotApprovedError):
        approved_text(conn, draft.id)
    with pytest.raises(DraftStateError):
        approve_draft(conn, draft.id)


def test_an_approval_can_be_revoked_before_anything_is_sent(conn, campaign, lead):
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    draft = request_draft(conn, campaign.id, lead, "connection_note")
    submit_draft(conn, draft.id, text=GOOD_NOTE)
    approve_draft(conn, draft.id)

    revoked = approve_draft(conn, draft.id, approved=False)

    assert revoked.status == STATUS_REJECTED
    with pytest.raises(DraftNotApprovedError):
        approved_text(conn, draft.id)


def test_re_approving_is_a_no_op_so_a_retried_call_is_safe(conn, campaign, lead):
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    draft = request_draft(conn, campaign.id, lead, "connection_note")
    submit_draft(conn, draft.id, text=GOOD_NOTE)
    first = approve_draft(conn, draft.id, now=at(10))
    second = approve_draft(conn, draft.id, now=at(20))

    assert first.decided_at == second.decided_at == "2026-06-01 09:00:10"


def test_nothing_can_be_approved_before_it_is_generated(conn, campaign, lead):
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    draft = request_draft(conn, campaign.id, lead, "connection_note")

    with pytest.raises(DraftStateError) as error:
        approve_draft(conn, draft.id)

    assert "nothing has been generated" in str(error.value)


def test_text_cannot_be_rewritten_after_a_human_signed_it_off(conn, campaign, lead):
    """The approved thing and the sent thing must be the same thing."""
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    draft = request_draft(conn, campaign.id, lead, "connection_note")
    submit_draft(conn, draft.id, text=GOOD_NOTE)
    approve_draft(conn, draft.id)

    with pytest.raises(DraftStateError):
        submit_draft(conn, draft.id, text="Something else entirely.")

    assert approved_text(conn, draft.id) == GOOD_NOTE


def test_a_sent_draft_is_finished(conn, campaign, lead):
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    draft = request_draft(conn, campaign.id, lead, "connection_note")
    submit_draft(conn, draft.id, text=GOOD_NOTE)
    approve_draft(conn, draft.id)
    mark_sent(conn, draft.id)

    with pytest.raises(DraftNotApprovedError):
        mark_sent(conn, draft.id)
    with pytest.raises(DraftStateError):
        approve_draft(conn, draft.id, approved=False)


# --------------------------------------------------------------------------
# Auto-approve is the campaign flag that already existed
# --------------------------------------------------------------------------


def test_auto_approve_is_the_existing_per_campaign_flag(conn, auto_campaign, lead):
    enrol_lead(conn, auto_campaign.id, lead, now=BASE_TIME)
    draft = request_draft(conn, auto_campaign.id, lead, "connection_note")

    submitted = submit_draft(conn, draft.id, text=GOOD_NOTE, now=at(5))

    assert auto_approves(conn, auto_campaign.id) is True
    assert submitted.status == STATUS_APPROVED
    assert approved_text(conn, draft.id) == GOOD_NOTE


def test_a_campaign_left_at_the_default_can_never_auto_send(conn, campaign, lead):
    assert campaign.approval_mode == "manual_drafts"
    assert auto_approves(conn, campaign.id) is False

    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    draft = request_draft(conn, campaign.id, lead, "connection_note")
    submitted = submit_draft(conn, draft.id, text=GOOD_NOTE)

    assert submitted.status == STATUS_PENDING_APPROVAL
    with pytest.raises(DraftNotApprovedError):
        approved_text(conn, draft.id)


def test_auto_approve_still_refuses_text_that_breaks_the_voice_rules(
    conn, auto_campaign, lead
):
    """Opting into auto-approve opts out of review, not out of the style rules."""
    enrol_lead(conn, auto_campaign.id, lead, now=BASE_TIME)
    draft = request_draft(conn, auto_campaign.id, lead, "connection_note")

    with pytest.raises(DraftStyleError):
        submit_draft(conn, draft.id, text=EM_DASH_NOTE)

    assert require_draft(conn, draft.id).status == STATUS_NEEDS_GENERATION


def test_a_draft_with_no_campaign_is_never_auto_approved(conn, account):
    draft = park_draft(conn, account_id=account, kind="comment")

    assert auto_approves(conn, None) is False
    assert submit_draft(conn, draft.id, text=GOOD_NOTE).status == STATUS_PENDING_APPROVAL


# --------------------------------------------------------------------------
# The SEQ-02 seam: an unapproved fragment is indistinguishable from no fragment
# --------------------------------------------------------------------------


HYBRID_BODY = "{IF firstName}Hi {firstName},{ELSE}Hi there,{END} {ai_opener}"


def test_a_pending_fragment_makes_the_whole_render_refuse(conn, account, campaign, lead):
    """The strongest form of the safety rule: the renderer never sees the text."""
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    template = create_template(conn, account, "hybrid invite", HYBRID_BODY, kind="hybrid")
    step = step_at_ord(conn, campaign.id, 1)

    draft = request_draft(conn, campaign.id, lead, "message", step=step, fragment="opener")
    submit_draft(conn, draft.id, text="Saw the platform rollout at Contoso.")

    result = safe_render_template(
        template,
        {"firstName": "Ada"},
        fragments=fragment_source(conn, campaign.id, lead, step_id=step.id),
    )

    assert result.ok is False
    assert result.refusal.reason is RenderRefusalReason.MISSING_AI_FRAGMENT
    assert result.refusal.is_awaiting_ai is True
    assert pending_fragments(conn, campaign.id, lead) == {"opener": STATUS_PENDING_APPROVAL}
    assert approved_fragments(conn, campaign.id, lead) == {}


def test_an_approved_fragment_renders(conn, account, campaign, lead):
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    template = create_template(conn, account, "hybrid invite", HYBRID_BODY, kind="hybrid")
    step = step_at_ord(conn, campaign.id, 1)

    draft = request_draft(conn, campaign.id, lead, "message", step=step, fragment="opener")
    submit_draft(conn, draft.id, text="Saw the platform rollout at Contoso.")
    approve_draft(conn, draft.id)

    result = safe_render_template(
        template,
        {"firstName": "Ada"},
        fragments=fragment_source(conn, campaign.id, lead, step_id=step.id),
    )

    assert result.ok is True
    assert result.text == "Hi Ada, Saw the platform rollout at Contoso."
    assert approved_fragments(conn, campaign.id, lead) == {
        "opener": "Saw the platform rollout at Contoso."
    }


def test_revoking_an_approval_pulls_the_fragment_back_out_of_the_render(
    conn, account, campaign, lead
):
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    template = create_template(conn, account, "hybrid invite", HYBRID_BODY, kind="hybrid")
    step = step_at_ord(conn, campaign.id, 1)
    draft = request_draft(conn, campaign.id, lead, "message", step=step, fragment="opener")
    submit_draft(conn, draft.id, text="Saw the platform rollout at Contoso.")
    approve_draft(conn, draft.id)
    assert safe_render_template(
        template,
        {"firstName": "Ada"},
        fragments=fragment_source(conn, campaign.id, lead, step_id=step.id),
    ).ok

    approve_draft(conn, draft.id, approved=False)

    result = safe_render_template(
        template,
        {"firstName": "Ada"},
        fragments=fragment_source(conn, campaign.id, lead, step_id=step.id),
    )
    assert result.ok is False
    assert result.refusal.reason is RenderRefusalReason.MISSING_AI_FRAGMENT


def test_one_draft_is_parked_per_fragment_the_template_is_missing(
    conn, account, lead
):
    campaign = create_campaign(conn, account, "Two fragments", status="active")
    template = create_template(
        conn,
        account,
        "two fragment note",
        "Hi there. {ai_opener} {ai_closer}",
        kind="hybrid",
    )
    define_steps(conn, campaign.id, [StepSpec("message", template_id=template.id)])
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)

    gates = ensure_fragment_drafts(conn, campaign.id, lead)

    assert sorted(gate.draft.context["fragment"] for gate in gates) == ["closer", "opener"]
    assert all(gate.parked for gate in gates)
    assert all(gate.ready is False for gate in gates)


def test_a_second_tick_reuses_every_fragment_draft(conn, account, lead):
    """Two open drafts at once, so reuse has to match on the fragment name."""
    campaign = create_campaign(conn, account, "Two fragments", status="active")
    template = create_template(
        conn,
        account,
        "two fragment note",
        "Hi there. {ai_opener} {ai_closer}",
        kind="hybrid",
    )
    define_steps(conn, campaign.id, [StepSpec("message", template_id=template.id)])
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)

    first = ensure_fragment_drafts(conn, campaign.id, lead)
    second = ensure_fragment_drafts(conn, campaign.id, lead)

    assert [gate.draft.id for gate in first] == [gate.draft.id for gate in second]
    assert all(gate.parked is False for gate in second)
    assert count_drafts(conn, account) == 2


def test_a_multi_fragment_message_renders_only_when_every_part_is_approved(
    conn, account, lead
):
    campaign = create_campaign(conn, account, "Two fragments", status="active")
    template = create_template(
        conn,
        account,
        "two fragment note",
        "Hi there. {ai_opener} {ai_closer}",
        kind="hybrid",
    )
    define_steps(conn, campaign.id, [StepSpec("message", template_id=template.id)])
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    step = step_at_ord(conn, campaign.id, 1)
    gates = {
        gate.draft.context["fragment"]: gate.draft.id
        for gate in ensure_fragment_drafts(conn, campaign.id, lead)
    }

    submit_draft(conn, gates["opener"], text="Saw the platform rollout at Contoso.")
    submit_draft(conn, gates["closer"], text="Worth twenty minutes?")
    approve_draft(conn, gates["opener"])

    half = safe_render_template(
        template, {}, fragments=fragment_source(conn, campaign.id, lead, step_id=step.id)
    )
    assert half.ok is False
    assert half.refusal.reason is RenderRefusalReason.MISSING_AI_FRAGMENT

    approve_draft(conn, gates["closer"])
    whole = safe_render_template(
        template, {}, fragments=fragment_source(conn, campaign.id, lead, step_id=step.id)
    )
    assert whole.ok is True
    assert whole.text == "Hi there. Saw the platform rollout at Contoso. Worth twenty minutes?"


# --------------------------------------------------------------------------
# Reading the queues
# --------------------------------------------------------------------------


def test_list_pending_reads_the_generation_queue_by_default(conn, account, campaign, lead):
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    first = request_draft(conn, campaign.id, lead, "connection_note")
    second = request_draft(conn, campaign.id, lead, "comment")
    submit_draft(conn, second.id, text=GOOD_NOTE)

    generation = list_pending(conn, account)
    review = list_pending(conn, account, status=STATUS_PENDING_APPROVAL)

    assert [draft.id for draft in generation] == [first.id]
    assert [draft.id for draft in review] == [second.id]


def test_the_queue_is_oldest_first_so_nothing_starves(conn, account, campaign, lead):
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    ids = [request_draft(conn, campaign.id, lead, "comment").id for _ in range(3)]

    assert [draft.id for draft in list_pending(conn, account)] == ids


def test_listing_filters_by_kind_and_campaign(conn, account, lead):
    first = make_campaign(conn, account, "One")
    second = make_campaign(conn, account, "Two")
    enrol_lead(conn, first.id, lead, now=BASE_TIME)
    enrol_lead(conn, second.id, lead, now=BASE_TIME)
    request_draft(conn, first.id, lead, "connection_note")
    request_draft(conn, second.id, lead, "comment")

    assert len(list_pending(conn, account, campaign_id=first.id)) == 1
    assert len(list_pending(conn, account, kind="comment")) == 1
    assert len(list_drafts(conn, account, limit=None)) == 2


def test_a_missing_draft_is_reported_not_guessed(conn):
    assert get_draft(conn, 999) is None
    with pytest.raises(DraftNotFoundError):
        require_draft(conn, 999)


def test_a_draft_cannot_be_written_by_an_account_that_does_not_own_it(
    conn, account, campaign, lead
):
    """Draft ids are small integers, so guessing one must not be enough."""
    intruder = int(
        conn.execute(
            "INSERT INTO accounts (label, timezone, state) VALUES ('intruder', 'UTC', 'active')"
        ).lastrowid
    )
    conn.commit()
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    draft = request_draft(conn, campaign.id, lead, "connection_note")

    with pytest.raises(DraftOwnershipError):
        submit_draft(conn, draft.id, text=GOOD_NOTE, account_id=intruder)

    submit_draft(conn, draft.id, text=GOOD_NOTE, account_id=account)
    with pytest.raises(DraftOwnershipError):
        approve_draft(conn, draft.id, account_id=intruder)

    assert require_draft(conn, draft.id).status == STATUS_PENDING_APPROVAL


def test_text_in_the_review_queue_cannot_be_swapped_underneath_the_reviewer(
    conn, campaign, lead
):
    """The window where a human reads A and approves B is closed by construction.

    Submitting is legal only from `needs_generation`, so regenerating means
    rejecting the draft, which is an audited decision, and parking a new one.
    """
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    draft = request_draft(conn, campaign.id, lead, "connection_note")
    submit_draft(conn, draft.id, text=GOOD_NOTE)

    with pytest.raises(DraftStateError):
        submit_draft(conn, draft.id, text="Rewritten after the human looked away.")

    assert require_draft(conn, draft.id).generated_text == GOOD_NOTE
    assert approve_draft(conn, draft.id).status == STATUS_APPROVED
    assert approved_text(conn, draft.id) == GOOD_NOTE


def test_approving_checks_the_text_that_was_read(conn, campaign, lead):
    """Belt and braces against anything that edits the table from outside."""
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    draft = request_draft(conn, campaign.id, lead, "connection_note")
    submit_draft(conn, draft.id, text=GOOD_NOTE)
    reviewed = require_draft(conn, draft.id).generated_text

    conn.execute(
        "UPDATE ai_drafts SET generated_text = ? WHERE id = ?",
        ("Something nobody reviewed.", draft.id),
    )
    conn.commit()

    with pytest.raises(DraftChangedError):
        approve_draft(conn, draft.id, expected_text=reviewed)

    assert require_draft(conn, draft.id).status == STATUS_PENDING_APPROVAL


def test_a_refused_submission_leaves_the_row_byte_identical(conn, campaign, lead):
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    draft = request_draft(conn, campaign.id, lead, "connection_note")
    before = dict(conn.execute("SELECT * FROM ai_drafts WHERE id = ?", (draft.id,)).fetchone())

    with pytest.raises(DraftStyleError):
        submit_draft(conn, draft.id, text=EM_DASH_NOTE, model="a-worse-model")

    after = dict(conn.execute("SELECT * FROM ai_drafts WHERE id = ?", (draft.id,)).fetchone())
    assert after == before

    submit_draft(conn, draft.id, text=GOOD_NOTE, model="claude-opus-5")
    assert require_draft(conn, draft.id).status == STATUS_PENDING_APPROVAL


def test_the_fragment_source_offers_no_way_to_inject_unapproved_text(conn, campaign, lead):
    """No override argument, and no optional scope, because both are holes."""
    signature = inspect.signature(fragment_source)

    assert set(signature.parameters) == {"conn", "campaign_id", "lead_id", "step_id"}
    assert signature.parameters["step_id"].default is inspect.Parameter.empty


def test_an_approval_for_one_step_is_not_reused_by_another(conn, account, lead):
    """Approving a message for step 2 must not silently fill step 4's fragment."""
    campaign = create_campaign(conn, account, "Two messages", status="active")
    template = create_template(conn, account, "hybrid", HYBRID_BODY, kind="hybrid")
    define_steps(
        conn,
        campaign.id,
        [
            StepSpec("message", template_id=template.id),
            StepSpec("message", template_id=template.id),
        ],
    )
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    first_step = step_at_ord(conn, campaign.id, 1)
    second_step = step_at_ord(conn, campaign.id, 2)

    gate = ensure_draft(conn, campaign.id, lead, "message", step=first_step, fragment="opener")
    submit_draft(conn, gate.draft.id, text="Saw the platform rollout at Contoso.")
    approve_draft(conn, gate.draft.id)

    assert approved_fragments(conn, campaign.id, lead, step_id=first_step.id) != {}
    assert approved_fragments(conn, campaign.id, lead, step_id=second_step.id) == {}

    result = safe_render_template(
        template,
        {"firstName": "Ada"},
        fragments=fragment_source(conn, campaign.id, lead, step_id=second_step.id),
    )
    assert result.ok is False
    assert result.refusal.reason is RenderRefusalReason.MISSING_AI_FRAGMENT


# --------------------------------------------------------------------------
# Drafting is bookkeeping, and bookkeeping is not a LinkedIn action
# --------------------------------------------------------------------------


def test_the_whole_draft_lifecycle_writes_no_linkedin_action(conn, campaign, lead):
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    draft = request_draft(conn, campaign.id, lead, "connection_note")
    submit_draft(conn, draft.id, text=GOOD_NOTE)
    approve_draft(conn, draft.id)
    mark_sent(conn, draft.id)

    total = conn.execute("SELECT COUNT(*) AS n FROM actions_log").fetchone()["n"]
    assert total == 0


def test_drafts_are_scoped_to_the_account_that_owns_them(conn, account, campaign, lead):
    other = int(
        conn.execute(
            "INSERT INTO accounts (label, timezone, state) VALUES ('second', 'UTC', 'active')"
        ).lastrowid
    )
    conn.commit()
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    request_draft(conn, campaign.id, lead, "connection_note")

    assert len(list_pending(conn, account)) == 1
    assert list_pending(conn, other) == []
