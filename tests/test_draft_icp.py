"""SEQ-05: the ICP qualification gate, its verdict and where it sends a lead.

The gate is a `filter` step, so it reaches nothing on LinkedIn and spends no
budget. That is asserted structurally here rather than trusted, because the whole
argument for putting ICP filtering *before* the invite step is that qualifying
somebody is free and inviting them is not.

Every test is offline and deterministic. No model is called and no verdict is
guessed.
"""

import json
from datetime import datetime, timezone

import pytest

from linkedin_mcp.core.db import initialize_database
from linkedin_mcp.drafts import routing
from linkedin_mcp.drafts import (
    ICP_ACTION,
    ICP_FILTER_NAME,
    STATUS_APPROVED,
    STATUS_SENT,
    DraftStateError,
    DraftStyleError,
    MalformedVerdictError,
    Verdict,
    approve_draft,
    coerce_match,
    encode_verdict,
    ensure_draft,
    icp_gate_step,
    is_icp_gate,
    latest_verdict,
    park_draft,
    parse_verdict,
    register_icp_filter,
    request_draft,
    require_draft,
    route_icp_verdict,
    submit_draft,
)
from linkedin_mcp.leads import create_lead
from linkedin_mcp.sequences import (
    LOCAL_ACTIONS,
    StepSpec,
    apply_filter_step,
    claim_step,
    complete_step,
    create_campaign,
    define_steps,
    enrol_lead,
    evaluate_filter,
    get_campaign_lead,
    list_jobs,
    reset_filters,
    step_at_ord,
)

BASE_TIME = datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc)
ICP = {
    "who": "platform or developer experience leaders at 500+ engineer companies",
    "signals": ["owns internal tooling", "evaluating AI coding assistants"],
}
MATCH = {"match": True, "score": 0.82, "reason": "Runs platform engineering at Contoso."}
NO_MATCH = {"match": False, "score": 0.11, "reason": "Individual contributor, no tooling remit."}


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


@pytest.fixture(autouse=True)
def clean_filter_registry():
    reset_filters()
    yield
    reset_filters()


@pytest.fixture()
def campaign_lead(conn, account, lead):
    """A campaign with an ICP gate and a lead claimed onto it."""
    campaign = gate_only(conn, account)
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    claim_step(conn, campaign.id, lead, worker_id="w1", now=BASE_TIME)
    return campaign, lead


def gate_only(conn, account_id, *, approval_mode="auto", on_failure="fail"):
    """A campaign whose only step is the ICP gate."""
    campaign = create_campaign(
        conn, account_id, "ICP gate", status="active", approval_mode=approval_mode
    )
    define_steps(conn, campaign.id, [icp_gate_step(ICP, on_failure=on_failure)])
    return campaign


def gate_then_invite(conn, account_id, *, approval_mode="auto"):
    """The shape the issue argues for: qualify first, then spend an invitation."""
    campaign = create_campaign(
        conn, account_id, "Qualify then invite", status="active", approval_mode=approval_mode
    )
    define_steps(
        conn,
        campaign.id,
        [icp_gate_step(ICP), StepSpec("connection_request", config={"priority": 5})],
    )
    return campaign


def evaluate(conn, campaign, lead_id, verdict, *, now=BASE_TIME):
    """Run the gate end to end and return the routing result."""
    claim_step(conn, campaign.id, lead_id, worker_id="w1", now=now)
    gate = ensure_draft(conn, campaign.id, lead_id, "icp_evaluation", now=now)
    submit_draft(conn, gate.draft.id, verdict=verdict, model="claude-opus-5", now=now)
    return route_icp_verdict(conn, gate.draft.id, now=now, worker_id="w1")


# --------------------------------------------------------------------------
# The verdict shape, parsed strictly
# --------------------------------------------------------------------------


def test_a_well_formed_verdict_parses():
    verdict = parse_verdict(MATCH)

    assert verdict == Verdict(match=True, score=0.82, reason=MATCH["reason"])
    assert verdict.to_dict() == {"match": True, "score": 0.82, "reason": MATCH["reason"]}
    assert json.loads(encode_verdict(verdict)) == verdict.to_dict()


def test_a_verdict_names_the_sublist_it_routes_to():
    assert parse_verdict(MATCH).sublist == "successful"
    assert parse_verdict(NO_MATCH).sublist == "failed"


def test_a_stored_verdict_string_parses_the_same_way():
    assert parse_verdict(json.dumps(MATCH)) == parse_verdict(MATCH)


@pytest.mark.parametrize(
    "value, expected",
    [
        (True, True),
        (False, False),
        (1, True),
        (0, False),
        ("true", True),
        ("YES", True),
        ("no_match", False),
        ("  False ", False),
    ],
)
def test_match_accepts_the_shapes_a_model_actually_emits(value, expected):
    assert coerce_match(value) is expected


@pytest.mark.parametrize("value", [None, "", "maybe", "0.7", 2, [], {}])
def test_match_refuses_anything_that_did_not_answer_the_question(value):
    with pytest.raises(MalformedVerdictError):
        coerce_match(value)


@pytest.mark.parametrize(
    "payload",
    [
        {"score": 0.9, "reason": "looks right"},
        {"match": True, "reason": "looks right"},
        {"match": True, "score": 0.9},
        {},
    ],
)
def test_a_verdict_missing_any_key_is_malformed(payload):
    with pytest.raises(MalformedVerdictError) as error:
        parse_verdict(payload)

    assert "missing" in str(error.value)


@pytest.mark.parametrize("score", [-0.1, 1.1, "high", None, float("nan"), True])
def test_a_score_outside_zero_to_one_is_malformed(score):
    with pytest.raises(MalformedVerdictError):
        parse_verdict({"match": True, "score": score, "reason": "x"})


@pytest.mark.parametrize("reason", ["", "   ", 42, None])
def test_a_verdict_nobody_can_audit_is_malformed(reason):
    with pytest.raises(MalformedVerdictError):
        parse_verdict({"match": True, "score": 0.5, "reason": reason})


@pytest.mark.parametrize("raw", [None, "", "   ", "not json", "[1, 2]", '"a string"'])
def test_unparseable_verdicts_are_malformed(raw):
    with pytest.raises(MalformedVerdictError):
        parse_verdict(raw)


def test_a_long_reason_is_truncated_rather_than_refused():
    verdict = parse_verdict({"match": True, "score": 0.5, "reason": "x" * 900})

    assert len(verdict.reason) == 500


def test_a_malformed_verdict_cannot_even_be_submitted(conn, account, lead):
    campaign = gate_only(conn, account)
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    draft = request_draft(conn, campaign.id, lead, "icp_evaluation")

    with pytest.raises(MalformedVerdictError):
        submit_draft(conn, draft.id, verdict={"score": 0.9, "reason": "no match key"})

    assert require_draft(conn, draft.id).status == "needs_generation"


# --------------------------------------------------------------------------
# Routing: the verdict moves the lead, it does not merely record an opinion
# --------------------------------------------------------------------------


def test_a_match_routes_the_lead_to_successful(conn, account, lead):
    campaign = gate_only(conn, account)
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)

    routing = evaluate(conn, campaign, lead, MATCH)

    assert routing.matched is True
    assert routing.sublist == "successful"
    assert get_campaign_lead(conn, campaign.id, lead).sublist == "successful"
    assert routing.to_result()["score"] == 0.82


def test_a_no_match_routes_the_lead_to_failed(conn, account, lead):
    campaign = gate_only(conn, account)
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)

    routing = evaluate(conn, campaign, lead, NO_MATCH)

    assert routing.matched is False
    assert routing.sublist == "failed"
    record = get_campaign_lead(conn, campaign.id, lead)
    assert record.sublist == "failed"
    assert "icp_no_match" in record.last_outcome


def test_the_verdict_is_stored_where_the_schema_says_it_goes(conn, account, lead):
    campaign = gate_only(conn, account)
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)

    routing = evaluate(conn, campaign, lead, MATCH)

    stored = conn.execute(
        "SELECT verdict_json, generated_text FROM ai_drafts WHERE id = ?",
        (routing.draft.id,),
    ).fetchone()
    assert json.loads(stored["verdict_json"]) == {
        "match": True,
        "score": 0.82,
        "reason": MATCH["reason"],
    }
    assert stored["generated_text"] is None


def test_a_routed_verdict_is_marked_used_so_it_cannot_route_twice(conn, account, lead):
    campaign = gate_only(conn, account)
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)

    routing = evaluate(conn, campaign, lead, MATCH)

    assert routing.draft.status == STATUS_SENT
    with pytest.raises(DraftStateError):
        route_icp_verdict(conn, routing.draft.id)


# --------------------------------------------------------------------------
# The gate belongs before the invite step, and it costs nothing to run
# --------------------------------------------------------------------------


def test_an_icp_gate_reaches_nothing_on_linkedin(conn, account):
    """Requirement made structural: `filter` is already a local action.

    A worker must not take a safety-gate lease for a local step, so an ICP
    evaluation cannot consume the daily invite budget even by accident.
    """
    campaign = gate_only(conn, account)
    step = step_at_ord(conn, campaign.id, 1)

    assert ICP_ACTION in LOCAL_ACTIONS
    assert step.action_type == ICP_ACTION
    assert step.is_local is True
    assert step.filter_name == ICP_FILTER_NAME
    assert step.config["icp"] == ICP


def test_a_full_qualification_writes_no_metered_action(conn, account, lead):
    campaign = gate_only(conn, account)
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)

    evaluate(conn, campaign, lead, MATCH)

    rows = conn.execute("SELECT action_type FROM actions_log").fetchall()
    assert [row["action_type"] for row in rows] == []


def test_a_disqualified_lead_never_reaches_the_invite_step(conn, account, lead):
    """The point of the ordering: a no-match costs zero invitations."""
    campaign = gate_then_invite(conn, account)
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)

    routing = evaluate(conn, campaign, lead, NO_MATCH)

    record = get_campaign_lead(conn, campaign.id, lead)
    assert routing.sublist == "failed"
    assert record.current_step_ord == 1
    assert [job.state for job in list_jobs(conn, campaign_id=campaign.id)] == ["failed"]
    assert not [
        job
        for job in list_jobs(conn, campaign_id=campaign.id)
        if job.action_type == "connection_request"
    ]


def test_a_qualified_lead_advances_to_the_invite_step(conn, account, lead):
    campaign = gate_then_invite(conn, account)
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)

    routing = evaluate(conn, campaign, lead, MATCH)

    record = get_campaign_lead(conn, campaign.id, lead)
    assert routing.matched is True
    assert record.sublist == "queue"
    assert record.current_step_ord == 2
    open_jobs = [job for job in list_jobs(conn, campaign_id=campaign.id) if job.state == "pending"]
    assert [job.action_type for job in open_jobs] == ["connection_request"]


def test_the_gate_defaults_to_failing_rather_than_re_asking(conn, account):
    """A no-match is a verdict, not a transient error, so it does not retry."""
    spec = icp_gate_step(ICP)

    assert spec.on_failure == "fail"
    assert spec.action_type == "filter"


# --------------------------------------------------------------------------
# A broken verdict routes nothing, in either direction
# --------------------------------------------------------------------------


def corrupt_verdict(conn, draft_id, raw):
    conn.execute("UPDATE ai_drafts SET verdict_json = ? WHERE id = ?", (raw, draft_id))
    conn.commit()


@pytest.mark.parametrize(
    "raw",
    [
        '{"score": 0.9, "reason": "missing the match key"}',
        '{"match": "probably", "score": 0.9, "reason": "hedged"}',
        '{"match": true, "score": 4, "reason": "out of range"}',
        "not json at all",
        None,
    ],
)
def test_a_broken_verdict_leaves_the_lead_exactly_where_it_was(conn, account, lead, raw):
    """Never silently a match, and never silently a no-match either.

    Reading a truncated response as a match would invite somebody nobody
    qualified. Reading it as a no-match would quietly bin good leads every time
    a client regressed. So the lead is not moved at all and the caller decides.
    """
    campaign = gate_only(conn, account)
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    claim_step(conn, campaign.id, lead, worker_id="w1", now=BASE_TIME)
    gate = ensure_draft(conn, campaign.id, lead, "icp_evaluation")
    submit_draft(conn, gate.draft.id, verdict=MATCH)
    corrupt_verdict(conn, gate.draft.id, raw)

    with pytest.raises(MalformedVerdictError):
        route_icp_verdict(conn, gate.draft.id, worker_id="w1")

    record = get_campaign_lead(conn, campaign.id, lead)
    assert record.sublist == "processing"
    assert record.current_step_ord == 1
    assert require_draft(conn, gate.draft.id).status == STATUS_APPROVED


def test_an_unapproved_verdict_cannot_route_a_lead(conn, account, lead):
    """A verdict removes a real person from a campaign, so it is released too."""
    campaign = gate_only(conn, account, approval_mode="manual_drafts")
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    claim_step(conn, campaign.id, lead, worker_id="w1", now=BASE_TIME)
    gate = ensure_draft(conn, campaign.id, lead, "icp_evaluation")
    submit_draft(conn, gate.draft.id, verdict=NO_MATCH)

    with pytest.raises(DraftStateError) as error:
        route_icp_verdict(conn, gate.draft.id, worker_id="w1")

    assert "approved" in str(error.value)
    assert get_campaign_lead(conn, campaign.id, lead).sublist == "processing"


def test_approving_a_manual_verdict_then_routing_works(conn, account, lead):
    campaign = gate_only(conn, account, approval_mode="manual_drafts")
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    claim_step(conn, campaign.id, lead, worker_id="w1", now=BASE_TIME)
    gate = ensure_draft(conn, campaign.id, lead, "icp_evaluation")
    submit_draft(conn, gate.draft.id, verdict=NO_MATCH)
    approve_draft(conn, gate.draft.id)

    routing = route_icp_verdict(conn, gate.draft.id, worker_id="w1")

    assert routing.sublist == "failed"


def test_a_text_draft_cannot_be_routed_as_a_verdict(conn, account, lead):
    campaign = gate_only(conn, account)
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    draft = request_draft(conn, campaign.id, lead, "connection_note")

    with pytest.raises(DraftStateError):
        route_icp_verdict(conn, draft.id)


# --------------------------------------------------------------------------
# The filter registry seam, for campaigns that would rather skip than fail
# --------------------------------------------------------------------------


def test_the_icp_filter_reads_the_released_verdict_and_generates_nothing(
    conn, account, lead
):
    campaign = gate_only(conn, account)
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    claim_step(conn, campaign.id, lead, worker_id="w1", now=BASE_TIME)
    gate = ensure_draft(conn, campaign.id, lead, "icp_evaluation")
    submit_draft(conn, gate.draft.id, verdict=MATCH)

    register_icp_filter()
    step = step_at_ord(conn, campaign.id, 1)

    assert evaluate_filter(conn, account, campaign.id, lead, step) is True
    assert latest_verdict(conn, campaign.id, lead).score == 0.82


def test_the_filter_refuses_when_no_verdict_has_been_released(conn, account, lead):
    """Returning False would silently drop every lead merely awaiting a human."""
    campaign = gate_only(conn, account, approval_mode="manual_drafts")
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    claim_step(conn, campaign.id, lead, worker_id="w1", now=BASE_TIME)
    gate = ensure_draft(conn, campaign.id, lead, "icp_evaluation")
    submit_draft(conn, gate.draft.id, verdict=MATCH)

    register_icp_filter()
    step = step_at_ord(conn, campaign.id, 1)

    assert latest_verdict(conn, campaign.id, lead) is None
    with pytest.raises(LookupError):
        evaluate_filter(conn, account, campaign.id, lead, step)


def test_the_filter_honours_a_minimum_score(conn, account, lead):
    campaign = create_campaign(conn, account, "Strict ICP", status="active", approval_mode="auto")
    define_steps(conn, campaign.id, [icp_gate_step(ICP, config={"min_score": 0.9})])
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    claim_step(conn, campaign.id, lead, worker_id="w1", now=BASE_TIME)
    gate = ensure_draft(conn, campaign.id, lead, "icp_evaluation")
    submit_draft(conn, gate.draft.id, verdict=MATCH)

    register_icp_filter()
    step = step_at_ord(conn, campaign.id, 1)

    assert evaluate_filter(conn, account, campaign.id, lead, step) is False


def test_a_filter_step_can_skip_instead_of_fail(conn, account, lead):
    """The alternative routing shape, for campaigns that re-enrol later."""
    campaign = create_campaign(conn, account, "Skip ICP", status="active", approval_mode="auto")
    define_steps(
        conn,
        campaign.id,
        [icp_gate_step(ICP, config={"on_no_match": "skipped"})],
    )
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    claim_step(conn, campaign.id, lead, worker_id="w1", now=BASE_TIME)
    gate = ensure_draft(conn, campaign.id, lead, "icp_evaluation")
    submit_draft(conn, gate.draft.id, verdict=NO_MATCH)
    register_icp_filter()
    step = step_at_ord(conn, campaign.id, 1)

    matched = evaluate_filter(conn, account, campaign.id, lead, step)
    record = apply_filter_step(conn, campaign.id, lead, matched=matched, worker_id="w1")

    assert matched is False
    assert record.sublist == "skipped"


def test_the_latest_released_verdict_wins(conn, account, lead):
    campaign = gate_only(conn, account)
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    first = request_draft(conn, campaign.id, lead, "icp_evaluation")
    submit_draft(conn, first.id, verdict=NO_MATCH)
    second = request_draft(conn, campaign.id, lead, "icp_evaluation")
    submit_draft(conn, second.id, verdict=MATCH)

    assert latest_verdict(conn, campaign.id, lead).match is True


def test_a_malformed_newest_verdict_never_falls_back_to_an_older_one(conn, account, lead):
    """"The client regressed" and "the client said no" are different facts."""
    campaign = gate_only(conn, account)
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    old = request_draft(conn, campaign.id, lead, "icp_evaluation")
    submit_draft(conn, old.id, verdict=MATCH)
    new = request_draft(conn, campaign.id, lead, "icp_evaluation")
    submit_draft(conn, new.id, verdict=NO_MATCH)
    corrupt_verdict(conn, new.id, '{"score": 0.5, "reason": "truncated"}')

    with pytest.raises(MalformedVerdictError):
        latest_verdict(conn, campaign.id, lead)


def test_one_gates_verdict_is_never_reused_by_another_gate(conn, account, lead):
    """Two ICP gates in one campaign are two questions, not one."""
    campaign = create_campaign(
        conn, account, "Two gates", status="active", approval_mode="auto"
    )
    define_steps(
        conn,
        campaign.id,
        [icp_gate_step(ICP), icp_gate_step({"who": "and also a budget holder"})],
    )
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    first_step = step_at_ord(conn, campaign.id, 1)
    second_step = step_at_ord(conn, campaign.id, 2)

    gate = ensure_draft(conn, campaign.id, lead, "icp_evaluation", step=first_step)
    submit_draft(conn, gate.draft.id, verdict=MATCH)
    register_icp_filter()

    assert latest_verdict(conn, campaign.id, lead, step_id=first_step.id).match is True
    assert latest_verdict(conn, campaign.id, lead, step_id=second_step.id) is None
    with pytest.raises(LookupError):
        evaluate_filter(conn, account, campaign.id, lead, second_step)


def test_a_verdict_cannot_resolve_a_step_it_never_evaluated(conn, account, lead):
    """The lead moved on, so this verdict is about a step that already happened."""
    campaign = gate_then_invite(conn, account)
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    claim_step(conn, campaign.id, lead, worker_id="w1", now=BASE_TIME)
    gate = ensure_draft(conn, campaign.id, lead, "icp_evaluation")
    submit_draft(conn, gate.draft.id, verdict=NO_MATCH)
    # Something else advanced the lead onto the invite step in the meantime.
    complete_step(conn, campaign.id, lead, worker_id="w1", now=BASE_TIME)

    with pytest.raises(DraftStateError) as error:
        route_icp_verdict(conn, gate.draft.id)

    assert "parked for step" in str(error.value)
    record = get_campaign_lead(conn, campaign.id, lead)
    assert record.current_step_ord == 2
    assert record.sublist == "queue"


def test_routing_moves_the_lead_and_spends_the_verdict_together(
    conn, account, lead, monkeypatch
):
    """One transaction: if spending the verdict fails, the lead never moved.

    Forced rather than argued. `mark_sent` is made to raise after the sub-list
    write, and the lead must come back out of the rollback exactly where it was.
    """
    campaign = gate_only(conn, account)
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    claim_step(conn, campaign.id, lead, worker_id="w1", now=BASE_TIME)
    gate = ensure_draft(conn, campaign.id, lead, "icp_evaluation")
    submit_draft(conn, gate.draft.id, verdict=NO_MATCH)

    def explode(*args, **kwargs):
        raise RuntimeError("the draft store fell over mid-route")

    monkeypatch.setattr(routing, "mark_sent", explode)

    with pytest.raises(RuntimeError):
        route_icp_verdict(conn, gate.draft.id, worker_id="w1")

    record = get_campaign_lead(conn, campaign.id, lead)
    assert record.sublist == "processing"
    assert record.current_step_ord == 1
    assert require_draft(conn, gate.draft.id).status == STATUS_APPROVED
    assert conn.in_transaction is False

    # And with the store working again the same verdict still routes cleanly.
    monkeypatch.undo()
    assert route_icp_verdict(conn, gate.draft.id, worker_id="w1").sublist == "failed"


def test_a_verdict_parked_without_a_step_resolves_nothing(conn, account, campaign_lead):
    campaign, lead_id = campaign_lead
    draft = park_draft(
        conn,
        account_id=account,
        kind="icp_evaluation",
        campaign_id=campaign.id,
        lead_id=lead_id,
    )
    submit_draft(conn, draft.id, verdict=NO_MATCH)

    with pytest.raises(DraftStateError) as error:
        route_icp_verdict(conn, draft.id, worker_id="w1")

    assert "without a step" in str(error.value)
    assert get_campaign_lead(conn, campaign.id, lead_id).sublist == "processing"


def test_a_verdict_can_never_resolve_an_outreach_step(conn, account, lead):
    """An ICP score must not be able to complete or fail an invite."""
    campaign = create_campaign(
        conn, account, "Invite first", status="active", approval_mode="auto"
    )
    define_steps(conn, campaign.id, [StepSpec("connection_request")])
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    claim_step(conn, campaign.id, lead, worker_id="w1", now=BASE_TIME)
    invite_step = step_at_ord(conn, campaign.id, 1)

    draft = request_draft(conn, campaign.id, lead, "icp_evaluation", step=invite_step)
    submit_draft(conn, draft.id, verdict=MATCH)

    assert is_icp_gate(invite_step) is False
    with pytest.raises(DraftStateError) as error:
        route_icp_verdict(conn, draft.id, worker_id="w1")

    assert "not an ICP gate" in str(error.value)
    assert get_campaign_lead(conn, campaign.id, lead).sublist == "processing"


def test_a_newest_verdict_with_no_verdict_at_all_never_falls_back(conn, account, lead):
    campaign = gate_only(conn, account)
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    old = request_draft(conn, campaign.id, lead, "icp_evaluation")
    submit_draft(conn, old.id, verdict=MATCH)
    new = request_draft(conn, campaign.id, lead, "icp_evaluation")
    submit_draft(conn, new.id, verdict=NO_MATCH)
    corrupt_verdict(conn, new.id, None)

    with pytest.raises(MalformedVerdictError):
        latest_verdict(conn, campaign.id, lead)


def test_a_verdict_reason_is_held_to_the_same_voice_rules(conn, account, lead):
    """All generated text, not only the text LinkedIn will show."""
    campaign = gate_only(conn, account)
    enrol_lead(conn, campaign.id, lead, now=BASE_TIME)
    draft = request_draft(conn, campaign.id, lead, "icp_evaluation")

    with pytest.raises(DraftStyleError) as error:
        submit_draft(
            conn,
            draft.id,
            verdict={
                "match": True,
                "score": 0.9,
                "reason": "Platform lead \u2014 owns the tooling budget.",
            },
        )

    assert "forbidden dash" in str(error.value)
    assert require_draft(conn, draft.id).status == "needs_generation"
