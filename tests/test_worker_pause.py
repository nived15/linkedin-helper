"""MCP-05 (#28): the worker pause, and the lane that had no off switch.

The finding this file exists for
--------------------------------
"Pause the worker" was not possible with the tool surface `2a34682` shipped. Of
forty tools the only pause-shaped ones were `campaign_pause` and
`campaign_resume`, and each stops one campaign.

Pausing every campaign is not the same thing, and the difference is structural
rather than a bug someone could have fixed in a query. The campaign lane in
:func:`linkedin_mcp.sequences.jobs.due_jobs` inner-joins `campaigns` on
`RUNNABLE_STATUSES`, so it stops. The ad-hoc lane in
:func:`linkedin_mcp.worker.selection.ad_hoc_due_jobs` selects on
``campaign_id IS NULL`` and reads no campaign row at all, so it does not.

:func:`test_pausing_every_campaign_leaves_the_ad_hoc_lane_running` reproduces
exactly that, so the behaviour this issue had to fix is written down rather than
described. Everything else here is the fix.
"""

from __future__ import annotations

import pytest
from fastmcp import Client, FastMCP

from linkedin_mcp.core.config import (
    UNMETERED_ACTIONS,
    WORKER_CONTROL_ACTIONS,
    WORKER_PAUSE_ACTION,
    WORKER_RESUME_ACTION,
    is_metered,
)
from linkedin_mcp.sequences import StepSpec, set_campaign_status
from linkedin_mcp.tools.worker import register_worker_tools
from linkedin_mcp.worker import (
    PauseState,
    is_worker_paused,
    pause_worker,
    resume_worker,
    select_due_jobs,
    worker_pause_state,
    worker_status,
)
from linkedin_mcp.worker.heartbeat import STATUS_PAUSED

from test_worker_support import (  # noqa: F401
    BASE_TIME,
    RecordingExecutor,
    at,
    env,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture()
def tools():
    """A server carrying only the two MCP-05 tools.

    The shipped server registers them too, and
    `tests/test_tool_registration.py` is what asserts that. This one exists so a
    call here cannot accidentally depend on some other package's registration
    order.
    """
    server = FastMCP("worker-control")
    register_worker_tools(server)
    return server


async def call(server: FastMCP, name: str, **arguments):
    async with Client(server) as client:
        result = await client.call_tool(name, arguments)
    return result.data


def running_campaign(env, action_type: str = "connection_request") -> int:
    campaign_id = env.campaign([StepSpec(action_type)])
    env.enrol(campaign_id, [env.lead("Ada", public_id="ada")])
    return campaign_id


# ----------------------------------------------------------------------
# The finding, reproduced
# ----------------------------------------------------------------------


async def test_pausing_every_campaign_leaves_the_ad_hoc_lane_running(env):
    """What `campaign_pause` alone actually does, kept as a regression.

    A campaign, an ad-hoc job, then the campaign paused. The campaign lane goes
    quiet and the ad-hoc job is still selected, because nothing in that lane
    consults a campaign. This is why #28 needed a worker-level pause rather than
    a loop over `campaign_pause`.
    """
    campaign_id = running_campaign(env)
    env.enqueue_ad_hoc("profile_search")

    before = select_due_jobs(env.conn, env.account_id, now=at(minutes=1))
    assert len(before.campaign) == 1
    assert len(before.ad_hoc) == 1

    set_campaign_status(env.conn, campaign_id, "paused", now=BASE_TIME)

    after = select_due_jobs(env.conn, env.account_id, now=at(minutes=1))
    assert after.campaign == (), "a paused campaign yields no campaign work"
    assert len(after.ad_hoc) == 1, (
        "the ad-hoc lane keys on campaign_id IS NULL, so pausing every campaign "
        "does not stop it; this is the gap worker_pause closes"
    )
    assert after.paused is False


# ----------------------------------------------------------------------
# The pause itself
# ----------------------------------------------------------------------


async def test_an_account_that_was_never_paused_reads_as_running(env):
    """Absence of a row is the default, not an error."""
    state = worker_pause_state(env.conn, env.account_id)

    assert state == PauseState(account_id=env.account_id)
    assert state.paused is False
    assert is_worker_paused(env.conn, env.account_id) is False


async def test_a_pause_stops_both_lanes(env):
    """The property `campaign_pause` cannot give, at the selection layer."""
    running_campaign(env)
    env.enqueue_ad_hoc("profile_search")

    pause_worker(env.conn, env.account_id, reason="rewriting the invite template")

    selection = select_due_jobs(env.conn, env.account_id, now=at(minutes=1))

    assert selection.paused is True
    assert selection.campaign == ()
    assert selection.ad_hoc == ()
    assert len(selection) == 0


async def test_a_resume_puts_both_lanes_back(env):
    running_campaign(env)
    env.enqueue_ad_hoc("profile_search")
    pause_worker(env.conn, env.account_id)

    resume_worker(env.conn, env.account_id)

    selection = select_due_jobs(env.conn, env.account_id, now=at(minutes=1))
    assert selection.paused is False
    assert len(selection.campaign) == 1
    assert len(selection.ad_hoc) == 1


async def test_a_pause_cancels_nothing_and_the_queue_resumes_where_it_stopped(env):
    """A pause is not a cancel. The queue keeps its due times."""
    job_id = env.enqueue_ad_hoc("profile_search")
    pause_worker(env.conn, env.account_id)

    assert env.job(job_id)["state"] == "pending"
    assert select_due_jobs(env.conn, env.account_id, now=at(hours=4)).ad_hoc == ()

    resume_worker(env.conn, env.account_id)

    selection = select_due_jobs(env.conn, env.account_id, now=at(hours=4))
    assert [job.id for job in selection.jobs()] == [job_id]


async def test_a_pause_is_idempotent_and_refreshes_its_reason(env):
    pause_worker(env.conn, env.account_id, reason="first", paused_by="nived")
    second = pause_worker(env.conn, env.account_id, reason="second")

    assert second.paused is True
    assert second.reason == "second"
    assert second.paused_by is None
    assert second.resumed_at is None


async def test_a_pause_is_per_account(env):
    """One account's pause must not stop another's worker."""
    other = env.log.ensure_account("second@example.com")
    pause_worker(env.conn, env.account_id)

    assert is_worker_paused(env.conn, env.account_id) is True
    assert is_worker_paused(env.conn, other) is False


async def test_a_paused_selection_still_reports_unroutable_jobs(env):
    """A pause must not make a fault disappear from the report.

    An unroutable job is a job neither lane can execute. It is a bug to fix
    rather than work to run, so hiding it while paused would mean pausing the
    worker also silenced the count that says something is wrong.
    """
    campaign_id = env.campaign([StepSpec("message")])
    env.enqueue_ad_hoc("message", campaign_id=campaign_id)
    pause_worker(env.conn, env.account_id)

    selection = select_due_jobs(env.conn, env.account_id, now=at(minutes=1))

    assert selection.paused is True
    assert len(selection.unroutable) == 1


# ----------------------------------------------------------------------
# The runner honours it
# ----------------------------------------------------------------------


async def test_an_ad_hoc_job_does_not_execute_while_the_worker_is_paused(env):
    """The clause the exit criterion turns on, asserted through the runner.

    Selection returning nothing is necessary but not sufficient. This runs a
    real tick with a real executor registered and asserts the executor was never
    called, so a pause that stopped selection while some other path still
    reached the job would fail here.
    """
    from linkedin_mcp.worker import build_worker

    executor = RecordingExecutor()
    job_id = env.enqueue_ad_hoc("profile_search")
    pause_worker(env.conn, env.account_id, reason="stop everything")

    worker = build_worker(
        env.conn,
        env.account_id,
        worker_id="w1",
        executors={"profile_search": executor},
        pace_between_actions=False,
    )
    report = await worker.tick(now=at(minutes=1))

    assert report.paused is True
    assert report.jobs == ()
    assert len(executor) == 0
    assert env.job(job_id)["state"] == "pending"
    assert env.logged() == [], "a paused tick spends no LinkedIn budget"

    resume_worker(env.conn, env.account_id)
    resumed = await worker.tick(now=at(minutes=2))

    assert resumed.paused is False
    assert len(executor) == 1
    assert env.job(job_id)["state"] == "done"


async def test_a_paused_tick_says_paused_in_its_heartbeat(env):
    """`idle` and `paused` are different silences and the row says which."""
    from linkedin_mcp.worker import build_worker, read_heartbeat

    pause_worker(env.conn, env.account_id)
    worker = build_worker(
        env.conn,
        env.account_id,
        worker_id="w1",
        executors={},
        pace_between_actions=False,
    )

    await worker.tick(now=at(minutes=1))

    assert read_heartbeat(env.conn, "w1").status == STATUS_PAUSED


# ----------------------------------------------------------------------
# The tools
# ----------------------------------------------------------------------


async def test_the_pause_tool_stops_the_worker_and_says_so(env, tools):
    running_campaign(env)
    env.enqueue_ad_hoc("profile_search")

    result = await call(
        tools, "worker_pause", reason="template rewrite", paused_by="nived"
    )

    assert result["status"] == "success"
    assert result["paused"] is True
    assert result["reason"] == "template rewrite"
    assert result["paused_by"] == "nived"
    assert result["campaigns_running"] is False
    assert is_worker_paused(env.conn, env.account_id) is True
    assert select_due_jobs(env.conn, env.account_id, now=at(minutes=1)).paused is True


async def test_the_resume_tool_puts_it_back(env, tools):
    running_campaign(env)
    await call(tools, "worker_pause")

    result = await call(tools, "worker_resume")

    assert result["status"] == "success"
    assert result["paused"] is False
    assert is_worker_paused(env.conn, env.account_id) is False


async def test_the_pause_tools_write_an_audit_row_and_spend_no_budget(env, tools):
    """Both carry `@audit_linkedin_action`, and both are unmetered.

    The row matters: stopping the worker is an operator decision and a trail of
    who stopped it is worth having. The metering matters more. A pause must
    never be refused because the day's budget is spent, since the caller who
    wants to stop is exactly the caller whose budget is most likely gone.
    """
    await call(tools, "worker_pause", reason="checking")
    await call(tools, "worker_resume")

    logged = [(row["action_type"], row["outcome"]) for row in env.logged()]

    assert (WORKER_PAUSE_ACTION, "success") in logged
    assert (WORKER_RESUME_ACTION, "success") in logged
    assert WORKER_CONTROL_ACTIONS <= UNMETERED_ACTIONS
    assert not is_metered(WORKER_PAUSE_ACTION)
    assert not is_metered(WORKER_RESUME_ACTION)


async def test_a_pause_is_visible_in_the_status_report(env, tools):
    """A client must be able to confirm a pause by reading, not by trusting."""
    running_campaign(env)

    await call(tools, "worker_pause", reason="checking")

    report = worker_status(env.conn, account_id=env.account_id, now=at(minutes=1))

    assert report["paused"] is True
    assert report["pause"]["reason"] == "checking"
    assert report["campaigns_running"] is False