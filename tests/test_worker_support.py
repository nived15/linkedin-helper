"""Shared setup for the SEQ-04 worker tests, plus the guards on what it may import.

Everything here is offline and deterministic. No Playwright is launched, no
network is touched, and no test waits on a wall clock: the worker's clock is
injected, and the humanizer's sleep is replaced by a recorder so pacing is
observable without being slow.

The other `test_worker_*` modules import from this one. `tests/` has no
`__init__.py`, so pytest puts the directory on `sys.path` and a plain module
import works.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from linkedin_mcp.audit import instrument
from linkedin_mcp.audit.log import (
    AuditLog,
    reset_audit_log,
    set_audit_log,
)
from linkedin_mcp.browser.humanize import FAST, Humanizer, set_humanizer
from linkedin_mcp.leads.store import create_lead
from linkedin_mcp.safety.gate import SafetyGate, reset_gate, set_gate
from linkedin_mcp.sequences import (
    JobSpec,
    StepSpec,
    create_campaign,
    define_steps,
    enrol_leads,
    insert_job,
    now_timestamp,
    reset_filters,
    transaction,
)
from linkedin_mcp.worker import ActionResult

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKER_SOURCES = (
    REPO_ROOT / "worker.py",
    *sorted((REPO_ROOT / "linkedin_mcp" / "worker").glob("*.py")),
)

# A Monday at 09:30 UTC. Weekday-sensitive assertions then read as written and
# the account is inside an ordinary Monday-to-Friday schedule.
BASE_TIME = datetime(2026, 3, 9, 9, 30, tzinfo=timezone.utc)

WEEKDAYS = (0, 1, 2, 3, 4)
NINE_AM = 9 * 60
FIVE_PM = 17 * 60


def at(**delta: float) -> datetime:
    """Return a moment relative to :data:`BASE_TIME`."""
    return BASE_TIME + timedelta(**delta)


class RecordingSleep:
    """Stand-in for asyncio.sleep that records durations instead of waiting."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


class StepClock:
    """A clock the test advances by hand. The whole weekend fits in a loop."""

    def __init__(self, start: datetime = BASE_TIME) -> None:
        self.moment = start

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, **delta: float) -> datetime:
        self.moment = self.moment + timedelta(**delta)
        return self.moment


@dataclass
class WorkerEnv:
    """One account, one database, and the process-wide singletons pinned to it."""

    conn: Any
    account_id: int
    log: AuditLog
    clock: StepClock
    sleeper: RecordingSleep
    db_path: Path

    def set_working_hours(
        self,
        weekdays: tuple[int, ...] = WEEKDAYS,
        start_minute: int = NINE_AM,
        end_minute: int = FIVE_PM,
    ) -> None:
        """Give the account a schedule. Without rows it is open all the time."""
        with transaction(self.conn):
            self.conn.execute(
                "DELETE FROM working_hours WHERE account_id = ?", (self.account_id,)
            )
            for weekday in weekdays:
                self.conn.execute(
                    """
                    INSERT INTO working_hours
                        (account_id, weekday, start_minute, end_minute)
                    VALUES (?, ?, ?, ?)
                    """,
                    (self.account_id, weekday, start_minute, end_minute),
                )

    def set_cap(
        self,
        action_type: str,
        *,
        daily_cap: int | None = None,
        weekly_cap: int | None = None,
        enabled: bool = True,
    ) -> None:
        with transaction(self.conn):
            self.conn.execute(
                """
                INSERT INTO account_limits
                    (account_id, action_type, daily_cap, weekly_cap, enabled)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (account_id, action_type) DO UPDATE SET
                    daily_cap = excluded.daily_cap,
                    weekly_cap = excluded.weekly_cap,
                    enabled = excluded.enabled
                """,
                (
                    self.account_id,
                    action_type,
                    daily_cap,
                    weekly_cap,
                    1 if enabled else 0,
                ),
            )

    def set_account_state(self, state: str) -> None:
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE accounts SET state = ? WHERE id = ?", (state, self.account_id)
            )

    def lead(self, name: str, **fields: Any) -> int:
        return create_lead(self.conn, self.account_id, name, **fields).id

    def leads(self, count: int, **fields: Any) -> list[int]:
        return [
            self.lead(f"Lead {index}", public_id=f"lead-{index}", **fields)
            for index in range(1, count + 1)
        ]

    def campaign(
        self,
        steps: list[StepSpec],
        *,
        name: str = "Campaign",
        status: str = "active",
        approval_mode: str = "auto",
    ) -> int:
        created = create_campaign(
            self.conn,
            self.account_id,
            name,
            status=status,
            approval_mode=approval_mode,
        )
        define_steps(self.conn, created.id, steps)
        return created.id

    def enrol(self, campaign_id: int, lead_ids: list[int], *, now=None) -> None:
        enrol_leads(self.conn, campaign_id, lead_ids, now=now or BASE_TIME)

    def enqueue_ad_hoc(
        self,
        action_type: str = "profile_search",
        *,
        payload: str = '{"query": "solution engineer"}',
        scheduled_for: datetime | str | None = None,
        priority: int = 0,
        lead_id: int | None = None,
        campaign_id: int | None = None,
        step_id: int | None = None,
    ) -> int:
        """Write the job shape MCP-02 (#25) enqueues: no campaign, no lead, no step."""
        spec = JobSpec(
            campaign_id=campaign_id,
            lead_id=lead_id,
            step_id=step_id,
            account_id=self.account_id,
            action_type=action_type,
            payload_json=payload,
            scheduled_for=now_timestamp(scheduled_for or BASE_TIME),
            priority=priority,
        )
        with transaction(self.conn):
            return insert_job(self.conn, spec)

    def logged(
        self,
        *,
        action_type: str | None = None,
        outcome: str | None = None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM actions_log WHERE account_id = ?"
        params: list[Any] = [self.account_id]
        if action_type is not None:
            sql += " AND action_type = ?"
            params.append(action_type)
        if outcome is not None:
            sql += " AND outcome = ?"
            params.append(outcome)
        sql += " ORDER BY occurred_at, id"
        return [dict(row) for row in self.conn.execute(sql, params).fetchall()]

    def job(self, job_id: int) -> dict[str, Any]:
        return dict(
            self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        )

    def lead_state(self, campaign_id: int, lead_id: int) -> dict[str, Any]:
        return dict(
            self.conn.execute(
                "SELECT * FROM campaign_leads WHERE campaign_id = ? AND lead_id = ?",
                (campaign_id, lead_id),
            ).fetchone()
        )


@pytest.fixture
def env(tmp_path):
    """A database, an account, and every process-wide singleton pinned to it.

    The gate's jitter is pinned to zero so a cap test states the exact cap rather
    than a range, exactly as `tests/test_gate.py` does. The gate's own clock is
    never used, because the worker always passes `now=`.
    """
    db_path = tmp_path / "linkedin-helper.db"
    log = AuditLog.open(db_path)
    set_audit_log(log)
    account_id = log.ensure_account("worker@example.com")
    instrument.set_account_resolver(lambda: account_id)
    set_gate(SafetyGate(clock=lambda: BASE_TIME, jitter=lambda _a, _m: 0.0))

    sleeper = RecordingSleep()
    set_humanizer(Humanizer(profile=FAST, seed=7, sleep=sleeper))

    try:
        yield WorkerEnv(
            conn=log.connection,
            account_id=account_id,
            log=log,
            clock=StepClock(),
            sleeper=sleeper,
            db_path=db_path,
        )
    finally:
        reset_gate()
        reset_audit_log()
        reset_filters()
        set_humanizer(None)
        instrument.reset_account_resolver()
        log.close()


class RecordingExecutor:
    """An executor that records every call and returns a scripted result."""

    def __init__(self, result: ActionResult | None = None) -> None:
        self.result = result or ActionResult.ok(sent=True)
        self.calls: list[Any] = []

    async def __call__(self, context) -> ActionResult:
        self.calls.append(context)
        return self.result

    @property
    def lead_ids(self) -> list[int | None]:
        return [call.lead_id for call in self.calls]

    def __len__(self) -> int:
        return len(self.calls)


class CrashingExecutor:
    """Simulates a worker process dying mid-action.

    It raises `BaseException` rather than `Exception` on purpose. The runner
    catches `Exception` so that one bad job cannot end a tick, and a test that
    wants to model a *process death* has to get past that: the claim must stay
    committed and the job must stay leased, with no finalising transition, which
    is exactly the state a `kill -9` leaves behind.
    """

    class Death(BaseException):
        pass

    def __init__(self, *, reached_linkedin: bool = True) -> None:
        self.calls: list[Any] = []
        self.reached_linkedin = reached_linkedin

    async def __call__(self, context) -> ActionResult:
        self.calls.append(context)
        raise CrashingExecutor.Death("the worker process died mid-action")

    def __len__(self) -> int:
        return len(self.calls)


# ----------------------------------------------------------------------
# Guards on what the worker is allowed to depend on
# ----------------------------------------------------------------------

MCP_IMPORT = re.compile(r"^\s*(?:import\s+linkedin_browser_mcp|from\s+linkedin_browser_mcp)", re.M)
LLM_IMPORT = re.compile(
    r"^\s*(?:import|from)\s+(?:openai|anthropic|litellm|langchain|ollama|transformers)\b",
    re.M,
)
PLAYWRIGHT_IMPORT = re.compile(r"^\s*(?:import|from)\s+playwright\b", re.M)


def test_the_worker_never_imports_the_mcp_server():
    """The dependency runs one way only.

    The MCP server is a control plane over SQLite. If the worker imported it, a
    tool module's import side effects would run inside the daemon and the
    separation the whole architecture rests on would be a naming convention
    rather than a fact.
    """
    offenders = [
        path.name
        for path in WORKER_SOURCES
        if MCP_IMPORT.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_the_worker_never_imports_a_model_client():
    """"Runs unattended with no LLM connected" has to be structural.

    A worker that imports a model client can be made to wait on one by a later
    change. A worker that cannot import one cannot.
    """
    offenders = [
        path.name
        for path in WORKER_SOURCES
        if LLM_IMPORT.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_only_the_daemon_entry_point_reaches_playwright():
    """Playwright lives in `worker.py`, and even there only inside a function.

    The scheduling package must import cleanly on a machine with no browser
    installed, otherwise none of these tests could be trusted to be offline.
    """
    package_offenders = [
        path.name
        for path in WORKER_SOURCES
        if path.parent.name == "worker"
        and PLAYWRIGHT_IMPORT.search(path.read_text(encoding="utf-8"))
    ]
    assert package_offenders == []

    entry_point = (REPO_ROOT / "worker.py").read_text(encoding="utf-8")
    assert "BrowserSession" in entry_point
    # Module level would launch the import on every `--status` call and on every
    # machine without Playwright installed.
    assert not PLAYWRIGHT_IMPORT.search(entry_point)


def test_the_worker_package_owns_no_migration():
    """`worker_heartbeat` already exists in 0001, so SEQ-04 adds no schema."""
    migrations = sorted(
        (REPO_ROOT / "linkedin_mcp" / "core" / "migrations").glob("*.sql")
    )
    assert [path.name for path in migrations] == [
        "0001_init.sql",
        "0002_lead_dedupe.sql",
        "0003_sequence_jobs.sql",
    ]
