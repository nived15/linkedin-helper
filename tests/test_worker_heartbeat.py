"""`worker_heartbeat` and the honesty of `worker_status`.

The question `worker_status` answers is "is anything actually happening", and the
only wrong way to answer it is optimistically. A campaign row saying `active` and
a queue full of `pending` jobs are both still true an hour after the worker died,
so neither is evidence. The heartbeat is.
"""

from __future__ import annotations

import pytest

from linkedin_mcp.sequences import StepSpec
from linkedin_mcp.worker import (
    DEFAULT_STALLED_AFTER_SECONDS,
    LIVE_STATUSES,
    STATUS_IDLE,
    STATUS_RUNNING,
    STATUS_STOPPED,
    STATUSES,
    clear_heartbeat,
    list_heartbeats,
    read_heartbeat,
    seconds_since,
    worker_status,
    write_heartbeat,
)
from test_worker_support import BASE_TIME, at, env  # noqa: F401

pytestmark = pytest.mark.usefixtures("env")


def beat(env, worker_id: str, status: str, *, now, job_id: int | None = None):
    return write_heartbeat(
        env.conn,
        worker_id,
        env.account_id,
        status,
        current_job_id=job_id,
        now=now,
    )


def test_a_heartbeat_round_trips_with_its_age(env):
    beat(env, "w1", STATUS_RUNNING, now=BASE_TIME, job_id=None)

    row = read_heartbeat(env.conn, "w1", now=at(seconds=45))

    assert row.worker_id == "w1"
    assert row.status == STATUS_RUNNING
    assert row.age_seconds == pytest.approx(45)
    assert row.health(DEFAULT_STALLED_AFTER_SECONDS) == "running"


def test_writing_again_replaces_rather_than_accumulates(env):
    job_id = env.enqueue_ad_hoc("profile_search")
    beat(env, "w1", STATUS_RUNNING, now=BASE_TIME, job_id=job_id)
    beat(env, "w1", STATUS_IDLE, now=at(seconds=30))

    rows = list_heartbeats(env.conn, now=at(seconds=30))

    assert len(rows) == 1
    assert rows[0].status == STATUS_IDLE
    assert rows[0].current_job_id is None


def test_an_unknown_status_is_refused_rather_than_stored(env):
    """`worker_heartbeat.status` has no CHECK, so the vocabulary is enforced here.

    A typo that reached the column would produce a worker nothing could classify,
    and `worker_status` would have to guess.
    """
    with pytest.raises(ValueError):
        beat(env, "w1", "probably_fine", now=BASE_TIME)

    assert read_heartbeat(env.conn, "w1") is None


def test_every_status_except_stopped_can_go_stale(env):
    """A stopped worker is quiet on purpose. Everything else is quiet by fault."""
    assert set(LIVE_STATUSES) == set(STATUSES) - {STATUS_STOPPED}


# ----------------------------------------------------------------------
# Stalled
# ----------------------------------------------------------------------


def test_a_worker_wedged_mid_job_is_reported_stalled_not_running(env):
    """The case the DoD's word "honestly" is about.

    A worker that hangs inside a Playwright call never reaches any success path,
    so it never writes another row. Because the heartbeat was written *before*
    the job rather than after it, its last row ages, and it names the job it
    wedged on.
    """
    beat(env, "wedged", STATUS_RUNNING, now=BASE_TIME, job_id=env.enqueue_ad_hoc())

    report = worker_status(
        env.conn, account_id=env.account_id, now=at(minutes=1), stalled_after_seconds=180
    )
    assert report["stalled_workers"] == 0
    assert report["campaigns_running"] is True

    report = worker_status(
        env.conn, account_id=env.account_id, now=at(hours=2), stalled_after_seconds=180
    )

    assert report["stalled_workers"] == 1
    assert report["live_workers"] == 0
    assert report["campaigns_running"] is False
    assert report["workers"][0]["current_job_id"] is not None
    assert report["workers"][0]["health"] == "stalled"


def test_a_queue_with_due_work_and_no_live_worker_is_reported_as_not_moving(env):
    """The honest version of "campaign running".

    Everything about the database says this campaign is going: it is `active`, a
    lead is enrolled, its job is `pending` and overdue. Nothing is going, because
    the only worker died two hours ago.
    """
    campaign_id = env.campaign([StepSpec("connection_request")])
    env.enrol(campaign_id, [env.lead("Ada", public_id="ada")])
    beat(env, "dead", STATUS_IDLE, now=BASE_TIME)

    report = worker_status(
        env.conn, account_id=env.account_id, now=at(hours=2), stalled_after_seconds=180
    )

    assert report["due_jobs"] == 1
    assert report["campaigns_running"] is False
    assert report["queue_is_moving"] is False


def test_an_empty_queue_with_no_worker_is_quiet_rather_than_stuck(env):
    """Two kinds of silence, and they are not the same fault.

    Nothing due and nobody running is a system with nothing to do. Work due and
    nobody running is a system that has stopped. Collapsing them would make the
    report useless in exactly the case somebody consults it.
    """
    beat(env, "dead", STATUS_IDLE, now=BASE_TIME)

    report = worker_status(
        env.conn, account_id=env.account_id, now=at(hours=2), stalled_after_seconds=180
    )

    assert report["due_jobs"] == 0
    assert report["campaigns_running"] is False
    assert report["queue_is_moving"] is True


def test_a_cleanly_stopped_worker_never_reads_as_stalled(env):
    beat(env, "w1", STATUS_STOPPED, now=BASE_TIME)

    report = worker_status(env.conn, account_id=env.account_id, now=at(days=3))

    assert report["stalled_workers"] == 0
    assert report["stopped_workers"] == 1
    assert report["campaigns_running"] is False
    assert report["workers"][0]["health"] == "stopped"


def test_one_live_worker_is_enough_for_the_queue_to_be_moving(env):
    beat(env, "wedged", STATUS_RUNNING, now=BASE_TIME, job_id=env.enqueue_ad_hoc())
    beat(env, "alive", STATUS_IDLE, now=at(hours=2))

    report = worker_status(
        env.conn, account_id=env.account_id, now=at(hours=2), stalled_after_seconds=180
    )

    assert report["live_workers"] == 1
    assert report["stalled_workers"] == 1
    assert report["campaigns_running"] is True


def test_status_can_be_narrowed_to_one_account(env):
    other = env.log.ensure_account("second@example.com")
    beat(env, "mine", STATUS_IDLE, now=BASE_TIME)
    write_heartbeat(env.conn, "theirs", other, STATUS_IDLE, now=BASE_TIME)

    mine = worker_status(env.conn, account_id=env.account_id, now=BASE_TIME)
    everyone = worker_status(env.conn, now=BASE_TIME)

    assert [row["worker_id"] for row in mine["workers"]] == ["mine"]
    assert sorted(row["worker_id"] for row in everyone["workers"]) == ["mine", "theirs"]


def test_an_unparseable_timestamp_is_not_reported_as_fresh(env):
    """A heartbeat whose age cannot be computed must not read as healthy.

    Asserted through `health` and `worker_status`, not only through the helper,
    because the report is where the wrong answer would be believed.
    """
    assert seconds_since("not a timestamp", BASE_TIME) is None

    beat(env, "w1", STATUS_RUNNING, now=BASE_TIME)
    with env.conn:
        env.conn.execute(
            "UPDATE worker_heartbeat SET last_tick_at = ? WHERE worker_id = ?",
            ("corrupt", "w1"),
        )

    row = read_heartbeat(env.conn, "w1", now=BASE_TIME)
    assert row.age_seconds is None
    assert row.health(DEFAULT_STALLED_AFTER_SECONDS) == "stalled"

    report = worker_status(env.conn, account_id=env.account_id, now=BASE_TIME)
    assert report["stalled_workers"] == 1
    assert report["campaigns_running"] is False


def test_a_job_neither_lane_can_route_is_counted_rather_than_hidden(env):
    """A number that will not go down is the signal somebody needs.

    A job naming a campaign but no lead points at a `campaign_leads` row that
    does not exist. The runner logs a warning each tick, which nobody reads, so
    the count belongs in the report people actually consult.
    """
    campaign_id = env.campaign([StepSpec("message")])
    env.enqueue_ad_hoc("message", campaign_id=campaign_id)
    beat(env, "w1", STATUS_IDLE, now=BASE_TIME)

    report = worker_status(env.conn, account_id=env.account_id, now=BASE_TIME)

    assert report["unroutable_jobs"] == 1
    assert report["due_jobs"] == 1


def test_clearing_a_heartbeat_is_for_decommissioning_not_for_shutdown(env):
    beat(env, "w1", STATUS_STOPPED, now=BASE_TIME)

    assert clear_heartbeat(env.conn, "w1") is True
    assert clear_heartbeat(env.conn, "w1") is False
    assert list_heartbeats(env.conn) == []
