import ast
import asyncio
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from linkedin_mcp.audit import instrument
from linkedin_mcp.audit.log import (
    ATTEMPTED_OUTCOMES,
    ROLLING_WINDOW_INDEX,
    AuditLog,
    Outcome,
    RefusalReason,
    count_actions_in_window,
    log_action,
    log_refusal,
    reset_audit_log,
    set_audit_log,
    utc_timestamp,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SCANNED_SUFFIXES = (".py", ".sql")
SKIPPED_DIRECTORIES = {".git", "__pycache__", ".venv", "venv", "env", "node_modules"}
MUTATION_PATTERN = re.compile(
    r"(?:\b(?:UPDATE(?:\s+OR\s+(?:REPLACE|ROLLBACK|ABORT|FAIL|IGNORE))?"
    r"|DELETE\s+FROM|DROP\s+TABLE(?:\s+IF\s+EXISTS)?|TRUNCATE(?:\s+TABLE)?"
    r"|REPLACE\s+INTO|INSERT\s+OR\s+(?:REPLACE|IGNORE)\s+INTO|ALTER\s+TABLE)\s+"
    r"""(?:["'`\[]?\w+["'`\]]?\s*\.\s*)?["'`\[]?actions_log\b"""
    r"|\bactions_log\b[\s\S]{0,200}?\bON\s+CONFLICT\b[\s\S]{0,80}?\bDO\s+UPDATE\b)",
    re.IGNORECASE,
)
COUNTER_COLUMN_PATTERN = re.compile(
    r"^(?:daily|weekly|hourly|monthly|rolling|total|running)_"
    r"(?:count|counter|used|sent|actions|total)$"
    r"|^actions_(?:today|this_week|count|counter)$"
    r"|_counter$",
    re.IGNORECASE,
)
BASE_TIME = datetime(2026, 3, 14, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def audit(tmp_path):
    log = AuditLog.open(tmp_path / "linkedin-helper.db")
    set_audit_log(log)
    try:
        yield log
    finally:
        reset_audit_log()
        instrument.reset_account_resolver()
        log.close()


@pytest.fixture
def account_id(audit):
    resolved = audit.ensure_account("nived@example.com")
    instrument.set_account_resolver(lambda: resolved)
    return resolved


def scanned_files() -> list[Path]:
    files: list[Path] = []
    for path in REPO_ROOT.rglob("*"):
        if path.suffix not in SCANNED_SUFFIXES or not path.is_file():
            continue
        if SKIPPED_DIRECTORIES.intersection(path.relative_to(REPO_ROOT).parts):
            continue
        files.append(path)
    return files


def schema_columns(conn: sqlite3.Connection) -> dict[str, set[str]]:
    tables = [
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    ]
    return {
        table: {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for table in tables
    }


def decorator_names(node: ast.AST) -> list[str]:
    names = []
    for decorator in getattr(node, "decorator_list", []):
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(target, ast.Attribute):
            prefix = target.value.id if isinstance(target.value, ast.Name) else ""
            names.append(f"{prefix}.{target.attr}" if prefix else target.attr)
        elif isinstance(target, ast.Name):
            names.append(target.id)
    return names


def mcp_tools_in(source: str) -> dict[str, list[str]]:
    """Return every `@mcp.tool()` function in a module, with its decorators.

    `ast.walk` reaches nested definitions, so a tool registered inside a
    `register_*(mcp)` function counts exactly like one at module level. That
    matters: MCP-02 registers eleven tools that way and SEQ-05 will register
    more, and a guard that only saw module level would miss all of them.
    """
    tree = ast.parse(source)
    return {
        node.name: decorator_names(node)
        for node in ast.walk(tree)
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
        and "mcp.tool" in decorator_names(node)
    }


def unaudited_tools_in(source: str) -> list[str]:
    """Return the tools in a module that carry no `@audit_linkedin_action`."""
    return sorted(
        name
        for name, decorators in mcp_tools_in(source).items()
        if "audit_linkedin_action" not in decorators
    )


# MCP-04 (#27) ---------------------------------------------------------------
#
# `mcp_tools_in` keys on the string "mcp.tool", so the twelve `@mcp.resource`
# functions added in #27 were invisible to this file. That silence is the thing
# worth fixing. It read as "resources are audited" when it actually meant "this
# guard has never looked at a resource", and those two states are impossible to
# tell apart from the outside.
#
# The decision, written down so it is reviewable rather than accidental:
# resources do NOT carry `@audit_linkedin_action`, on purpose.
#
# `actions_log` is not a log of everything that happened. It is the ledger the
# rate limiter counts. `linkedin_mcp.safety.daily_budget` and `weekly_budget`
# derive `used` and `remaining` straight from `count_actions_in_window`, so a
# row in that table is a claim that some of today's LinkedIn budget was spent.
# A resource spends none: it opens no browser (`tests/test_actions.py` fails the
# build if one ever tries), touches no LinkedIn account, and changes nothing.
#
# Auditing them would therefore be actively harmful, not merely noisy. Every
# read of `linkedin://safety/today` would write a row that made the next read of
# `linkedin://safety/today` report less headroom than the account really has, a
# monitoring dashboard polling once a minute would starve the worker of budget
# it never used, and the resource whose entire job is reporting the budget would
# be the one corrupting it.
#
# What is checked below instead: that resources exist and are found, that none
# of them carries the decorator (so the exemption stays deliberate), and that
# none of them calls anything that spends a LinkedIn action. The runtime half of
# that promise is in `tests/test_resources.py`, which reads all twelve through
# the shipped server and asserts `actions_log` is still empty afterwards.


def mcp_resources_in(source: str) -> dict[str, list[str]]:
    """Return every `@mcp.resource()` function in a module, with its decorators."""
    tree = ast.parse(source)
    return {
        node.name: decorator_names(node)
        for node in ast.walk(tree)
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
        and "mcp.resource" in decorator_names(node)
    }


ACTION_SPENDING_CALLS = frozenset(
    {
        "audit_linkedin_action",
        "enqueue_action",
        "enqueue_job",
        "claim_next_job",
        "run_job",
        "execute_action",
        "send_connection_request",
        "record_outcome",
    }
)
"""Names that mean a LinkedIn action was taken or queued, not merely read.

`record` is deliberately absent: it is a common enough method name that
matching it would flag unrelated code, and the runtime assertion in
`tests/test_resources.py` covers the case this would.
"""


def resource_modules() -> dict[str, dict[str, list[str]]]:
    """Every shipped module that registers MCP resources, keyed by path."""
    modules: dict[str, dict[str, list[str]]] = {}
    for path in scanned_files():
        if path.suffix != ".py":
            continue
        relative = path.relative_to(REPO_ROOT)
        if relative.parts and relative.parts[0] == "tests":
            continue
        resources = mcp_resources_in(path.read_text(encoding="utf-8"))
        if resources:
            modules[relative.as_posix()] = resources
    return modules


def tool_modules() -> dict[str, dict[str, list[str]]]:
    """Return every shipped module that registers MCP tools, keyed by path.

    Scans the whole repository rather than the entry point alone. A tool
    registered from any module is part of the MCP surface and needs an audit
    row, and the surface is now spread across packages: `linkedin_mcp/tools/`
    for MCP-02 and `linkedin_mcp/drafts/` for SEQ-05. Tests are excluded
    because they ship nothing.
    """
    modules: dict[str, dict[str, list[str]]] = {}
    for path in scanned_files():
        if path.suffix != ".py":
            continue
        relative = path.relative_to(REPO_ROOT)
        if relative.parts and relative.parts[0] == "tests":
            continue
        tools = mcp_tools_in(path.read_text(encoding="utf-8"))
        if tools:
            modules[relative.as_posix()] = tools
    return modules


def seed(audit: AuditLog, account_id: int, action_type: str, outcome, minutes_ago: int) -> int:
    return audit.record(
        account_id,
        action_type,
        outcome,
        occurred_at=BASE_TIME - timedelta(minutes=minutes_ago),
    )


def test_record_appends_row_with_schema_columns(audit, account_id):
    row_id = audit.record(
        account_id,
        "connection_request",
        Outcome.SUCCESS,
        lead_id=None,
        detail={"target": "https://www.linkedin.com/in/someone", "note": "hi"},
        occurred_at=BASE_TIME,
    )

    row = audit.connection.execute(
        "SELECT * FROM actions_log WHERE id = ?", (row_id,)
    ).fetchone()
    assert row["account_id"] == account_id
    assert row["action_type"] == "connection_request"
    assert row["outcome"] == "success"
    assert row["occurred_at"] == "2026-03-14 09:00:00"
    assert json.loads(row["detail_json"])["note"] == "hi"


def test_record_rejects_unknown_outcome(audit, account_id):
    with pytest.raises(ValueError, match="Unknown outcome"):
        audit.record(account_id, "profile_view", "maybe")


def test_record_redacts_credentials_and_truncates_long_values(audit, account_id):
    row_id = audit.record(
        account_id,
        "login",
        Outcome.SUCCESS,
        detail={"password": "hunter2hunter2", "comment": "x" * 900},
    )

    detail = json.loads(
        audit.connection.execute(
            "SELECT detail_json FROM actions_log WHERE id = ?", (row_id,)
        ).fetchone()["detail_json"]
    )
    assert detail["password"] == "***"
    assert detail["comment"].endswith("...")
    assert len(detail["comment"]) == 503


def test_log_refusal_stores_typed_reason(audit, account_id):
    row_id = log_refusal(
        account_id,
        "connection_request",
        RefusalReason.WEEKLY_CAP_REACHED,
        lead_id=None,
        detail={"cap": 100},
    )

    row = audit.connection.execute(
        "SELECT * FROM actions_log WHERE id = ?", (row_id,)
    ).fetchone()
    assert row["outcome"] == Outcome.REFUSED.value
    detail = json.loads(row["detail_json"])
    assert detail["reason"] == "weekly_cap_reached"
    assert detail["cap"] == 100


def test_log_refusal_accepts_reason_string(audit, account_id):
    row_id = log_refusal(account_id, "post_like", "outside_working_hours")

    detail = json.loads(
        audit.connection.execute(
            "SELECT detail_json FROM actions_log WHERE id = ?", (row_id,)
        ).fetchone()["detail_json"]
    )
    assert detail["reason"] == "outside_working_hours"


def test_log_refusal_rejects_untyped_reason(audit, account_id):
    with pytest.raises(ValueError, match="Unknown refusal reason"):
        log_refusal(account_id, "post_like", "felt like it")


def test_refused_rows_cannot_be_written_without_a_typed_reason(audit, account_id):
    with pytest.raises(ValueError, match="typed reason"):
        audit.record(account_id, "post_like", Outcome.REFUSED)

    with pytest.raises(ValueError, match="Unknown refusal reason"):
        audit.record(
            account_id, "post_like", Outcome.REFUSED, detail={"reason": "just because"}
        )

    assert audit.entries(account_id) == []


def test_refusals_explain_a_quiet_night(audit, account_id):
    log_refusal(account_id, "connection_request", RefusalReason.DAILY_CAP_REACHED)
    log_refusal(account_id, "post_comment", RefusalReason.OUTSIDE_WORKING_HOURS)
    log_action(account_id, "profile_view", Outcome.SUCCESS)

    reasons = {
        json.loads(row["detail_json"])["reason"] for row in audit.refusals(account_id)
    }
    assert reasons == {"daily_cap_reached", "outside_working_hours"}


def test_rolling_window_count_respects_window_bounds(audit, account_id):
    seed(audit, account_id, "connection_request", Outcome.SUCCESS, minutes_ago=10)
    seed(audit, account_id, "connection_request", Outcome.SUCCESS, minutes_ago=30)
    seed(audit, account_id, "connection_request", Outcome.SUCCESS, minutes_ago=90)

    assert (
        audit.count_in_rolling_window(
            account_id,
            "connection_request",
            window=timedelta(hours=1),
            now=BASE_TIME,
        )
        == 2
    )


def test_rolling_window_count_includes_rows_written_this_second(audit, account_id):
    audit.record(account_id, "connection_request", Outcome.SUCCESS)

    assert (
        audit.count_in_rolling_window(
            account_id, "connection_request", window=timedelta(hours=1)
        )
        == 1
    )


def test_rolling_window_count_excludes_rows_dated_after_now(audit, account_id):
    seed(audit, account_id, "connection_request", Outcome.SUCCESS, minutes_ago=5)
    seed(audit, account_id, "connection_request", Outcome.SUCCESS, minutes_ago=-5)

    assert (
        audit.count_in_rolling_window(
            account_id,
            "connection_request",
            window=timedelta(hours=1),
            now=BASE_TIME,
        )
        == 1
    )


def test_rolling_window_count_ignores_other_accounts_and_actions(audit, account_id):
    other_account = audit.ensure_account("someone-else@example.com")
    seed(audit, account_id, "connection_request", Outcome.SUCCESS, minutes_ago=5)
    seed(audit, other_account, "connection_request", Outcome.SUCCESS, minutes_ago=5)
    seed(audit, account_id, "post_like", Outcome.SUCCESS, minutes_ago=5)

    assert (
        audit.count_in_window(
            account_id,
            "connection_request",
            since=BASE_TIME - timedelta(hours=1),
        )
        == 1
    )


def test_rolling_window_count_excludes_refusals_but_counts_failed_attempts(audit, account_id):
    seed(audit, account_id, "connection_request", Outcome.SUCCESS, minutes_ago=5)
    seed(audit, account_id, "connection_request", Outcome.FAILURE, minutes_ago=6)
    audit.record_refusal(
        account_id,
        "connection_request",
        RefusalReason.DAILY_CAP_REACHED,
        occurred_at=BASE_TIME - timedelta(minutes=7),
    )

    since = BASE_TIME - timedelta(hours=1)
    assert audit.count_in_window(account_id, "connection_request", since=since) == 2
    assert (
        audit.count_in_window(
            account_id, "connection_request", since=since, outcomes=()
        )
        == 3
    )


def test_adjacent_windows_do_not_double_count_the_boundary(audit, account_id):
    seed(audit, account_id, "connection_request", Outcome.SUCCESS, minutes_ago=0)
    seed(audit, account_id, "connection_request", Outcome.SUCCESS, minutes_ago=60)

    earlier = audit.count_in_window(
        account_id,
        "connection_request",
        since=BASE_TIME - timedelta(hours=2),
        until=BASE_TIME - timedelta(hours=1),
    )
    later = audit.count_in_window(
        account_id,
        "connection_request",
        since=BASE_TIME - timedelta(hours=1),
        until=BASE_TIME + timedelta(seconds=1),
    )
    assert (earlier, later) == (0, 2)


def test_windows_partition_a_microsecond_boundary_exactly(audit, account_id):
    seed(audit, account_id, "connection_request", Outcome.SUCCESS, minutes_ago=0)
    boundary = BASE_TIME + timedelta(microseconds=500_000)

    earlier = audit.count_in_window(
        account_id,
        "connection_request",
        since=BASE_TIME - timedelta(hours=1),
        until=boundary,
    )
    later = audit.count_in_window(
        account_id,
        "connection_request",
        since=boundary,
        until=BASE_TIME + timedelta(minutes=1),
    )
    assert earlier + later == 1


def test_module_level_window_count_uses_process_wide_log(audit, account_id):
    log_action(account_id, "post_like", Outcome.SUCCESS, occurred_at=BASE_TIME)

    assert (
        count_actions_in_window(
            account_id, "post_like", since=BASE_TIME - timedelta(minutes=1)
        )
        == 1
    )


def test_rolling_window_query_uses_the_account_action_time_index(audit, account_id):
    seed(audit, account_id, "connection_request", Outcome.SUCCESS, minutes_ago=5)

    plan = " ".join(
        audit.window_query_plan(
            account_id,
            "connection_request",
            since=BASE_TIME - timedelta(days=1),
            until=BASE_TIME,
        )
    )
    assert ROLLING_WINDOW_INDEX in plan
    assert "SCAN actions_log" not in plan


def test_rolling_window_index_is_declared_on_the_expected_columns(audit):
    columns = [
        row["name"]
        for row in audit.connection.execute(
            f"PRAGMA index_info({ROLLING_WINDOW_INDEX})"
        ).fetchall()
    ]
    assert columns == ["account_id", "action_type", "occurred_at"]


def test_actions_log_has_no_update_or_delete_path_in_the_codebase():
    offenders = []
    for path in scanned_files():
        if path == Path(__file__).resolve():
            continue
        if MUTATION_PATTERN.search(path.read_text(encoding="utf-8")):
            offenders.append(str(path.relative_to(REPO_ROOT)))

    assert offenders == []


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE actions_log SET outcome = 'success'",
        "delete from actions_log where id = 1",
        "DROP TABLE IF EXISTS actions_log",
        "TRUNCATE TABLE actions_log",
        "REPLACE INTO actions_log (id) VALUES (1)",
        "INSERT OR REPLACE INTO actions_log (id) VALUES (1)",
        "INSERT OR IGNORE INTO actions_log (id) VALUES (1)",
        'UPDATE "actions_log" SET outcome = 1',
        "UPDATE `actions_log` SET outcome = 1",
        "UPDATE [actions_log] SET outcome = 1",
        "UPDATE OR REPLACE actions_log SET outcome = 1",
        "UPDATE OR IGNORE actions_log SET outcome = 1",
        "DELETE FROM main.actions_log WHERE id = 1",
        'UPDATE main."actions_log" SET outcome = 1',
        "ALTER TABLE actions_log DROP COLUMN outcome",
        "INSERT INTO actions_log (id) VALUES (1) ON CONFLICT (id) DO UPDATE SET outcome = 'x'",
    ],
)
def test_append_only_scan_detects_every_mutation_form(statement):
    assert MUTATION_PATTERN.search(statement)


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT COUNT(*) FROM actions_log WHERE account_id = ?",
        "INSERT INTO actions_log (account_id) VALUES (?)",
        "CREATE INDEX idx ON actions_log (account_id)",
    ],
)
def test_append_only_scan_allows_reads_and_appends(statement):
    assert not MUTATION_PATTERN.search(statement)


def test_audit_service_exposes_no_mutation_helpers():
    forbidden = {"update", "delete", "purge", "prune", "reset_counts", "clear"}
    methods = {name for name in vars(AuditLog) if not name.startswith("_")}

    assert methods.isdisjoint(forbidden)
    assert not any(method.startswith(("update_", "delete_")) for method in methods)


def test_attempted_outcomes_exclude_refusals_and_skips():
    assert set(ATTEMPTED_OUTCOMES) == {Outcome.SUCCESS.value, Outcome.FAILURE.value}
    assert Outcome.REFUSED.value not in ATTEMPTED_OUTCOMES
    assert Outcome.SKIPPED.value not in ATTEMPTED_OUTCOMES


def test_limiter_arithmetic_reads_only_actions_log(audit, account_id):
    seed(audit, account_id, "connection_request", Outcome.SUCCESS, minutes_ago=5)
    other_tables = set(schema_columns(audit.connection)) - {"actions_log"}

    plan = audit.window_query_plan(
        account_id,
        "connection_request",
        since=BASE_TIME - timedelta(days=1),
        until=BASE_TIME,
    )

    assert plan
    assert all("actions_log" in line for line in plan)
    joined = " ".join(plan)
    assert not [table for table in other_tables if re.search(rf"\b{table}\b", joined)]


def test_schema_has_no_denormalised_rate_limit_counter(audit):
    columns_by_table = schema_columns(audit.connection)

    assert not [table for table in columns_by_table if "counter" in table.lower()]

    offenders = [
        f"{table}.{column}"
        for table, columns in columns_by_table.items()
        for column in columns
        if COUNTER_COLUMN_PATTERN.search(column)
    ]
    assert offenders == []

    assert columns_by_table["account_limits"] == {
        "account_id",
        "action_type",
        "daily_cap",
        "weekly_cap",
        "enabled",
    }


def test_ensure_account_is_idempotent(audit):
    first = audit.ensure_account("repeat@example.com")
    second = audit.ensure_account("repeat@example.com")

    assert first == second
    assert (
        audit.connection.execute(
            "SELECT COUNT(*) FROM accounts WHERE label = ?", ("repeat@example.com",)
        ).fetchone()[0]
        == 1
    )


def test_ensure_account_never_leaves_a_transaction_open(audit):
    audit.ensure_account("fresh@example.com")
    assert audit.connection.in_transaction is False

    audit.ensure_account("fresh@example.com")
    assert audit.connection.in_transaction is False


def test_ensure_account_releases_the_lock_when_another_worker_wins(audit, monkeypatch):
    winner = audit.ensure_account("contended@example.com")
    calls = {"count": 0}
    real_select = audit._select_account_id

    def racing_select(label):
        calls["count"] += 1
        if calls["count"] == 1:
            return None
        return real_select(label)

    monkeypatch.setattr(audit, "_select_account_id", racing_select)

    assert audit.ensure_account("contended@example.com") == winner
    assert audit.connection.in_transaction is False
    assert audit.ensure_account("another@example.com") != winner


def test_ensure_account_rolls_back_and_releases_on_failure(audit):
    with pytest.raises(sqlite3.IntegrityError):
        audit.ensure_account("bad@example.com", timezone_name=None)

    assert audit.connection.in_transaction is False
    assert audit.ensure_account("good@example.com") > 0


def test_utc_timestamp_is_comparable_with_sqlite_current_timestamp(audit, account_id):
    audit.connection.execute(
        """
        INSERT INTO actions_log (account_id, action_type, outcome)
        VALUES (?, 'profile_view', 'success')
        """,
        (account_id,),
    )
    audit.connection.commit()
    audit.record(account_id, "profile_view", Outcome.SUCCESS)

    since = utc_timestamp(datetime.now(timezone.utc) - timedelta(minutes=5))
    assert audit.count_in_window(account_id, "profile_view", since=since) == 2


def test_decorator_logs_successful_reads(audit, account_id):
    @instrument.audit_linkedin_action("profile_view", target="username")
    async def fake_tool(username: str, ctx=None) -> dict:
        return {"status": "success", "profile": {"name": "Someone"}}

    asyncio.run(fake_tool("nived15", ctx=object()))

    rows = audit.entries(account_id)
    assert len(rows) == 1
    assert rows[0]["action_type"] == "profile_view"
    assert rows[0]["outcome"] == "success"
    detail = json.loads(rows[0]["detail_json"])
    assert detail["target"] == "nived15"
    assert detail["tool"] == "fake_tool"
    assert "duration_ms" in detail


def test_decorator_logs_error_results_as_failure(audit, account_id):
    @instrument.audit_linkedin_action("connection_request", target="profile_url")
    async def fake_tool(profile_url: str, ctx=None) -> dict:
        return {"status": "error", "message": "Connect button not found"}

    asyncio.run(fake_tool("https://www.linkedin.com/in/someone"))

    row = audit.entries(account_id)[0]
    assert row["outcome"] == "failure"
    assert json.loads(row["detail_json"])["message"] == "Connect button not found"


def test_decorator_derives_action_type_from_arguments(audit, account_id):
    @instrument.audit_linkedin_action(
        lambda bound: f"post_{bound.get('action') or 'like'}",
        target="post_url",
    )
    async def fake_tool(post_url: str, ctx=None, action: str = "like") -> dict:
        return {"status": "success"}

    asyncio.run(fake_tool("https://www.linkedin.com/posts/abc", action="share"))
    asyncio.run(fake_tool("https://www.linkedin.com/posts/abc"))

    assert {row["action_type"] for row in audit.entries(account_id)} == {
        "post_share",
        "post_like",
    }


def test_decorator_logs_refused_results_with_their_typed_reason(audit, account_id):
    @instrument.audit_linkedin_action("connection_request", target="profile_url")
    async def fake_tool(profile_url: str, ctx=None) -> dict:
        return {
            "status": "refused",
            "reason": RefusalReason.DAILY_CAP_REACHED.value,
            "message": "Daily cap reached",
        }

    asyncio.run(fake_tool("https://www.linkedin.com/in/someone"))

    row = audit.entries(account_id)[0]
    assert row["outcome"] == "refused"
    assert json.loads(row["detail_json"])["reason"] == "daily_cap_reached"


def test_decorator_surfaces_a_refusal_missing_its_reason(audit, account_id):
    @instrument.audit_linkedin_action("connection_request")
    async def fake_tool(ctx=None) -> dict:
        return {"status": "refused", "message": "nope"}

    result = asyncio.run(fake_tool())

    assert "typed reason" in result["audit_error"]
    assert audit.entries(account_id) == []


def test_decorator_skips_results_the_gate_already_logged(audit, account_id):
    @instrument.audit_linkedin_action("connection_request")
    async def fake_tool(ctx=None) -> dict:
        log_refusal(account_id, "connection_request", RefusalReason.DAILY_CAP_REACHED)
        return {
            "status": "refused",
            "reason": RefusalReason.DAILY_CAP_REACHED.value,
            "audit_logged": True,
        }

    asyncio.run(fake_tool())

    rows = audit.entries(account_id)
    assert len(rows) == 1
    assert json.loads(rows[0]["detail_json"])["reason"] == "daily_cap_reached"


def test_decorator_logs_and_reraises_cancellation(audit, account_id):
    @instrument.audit_linkedin_action("feed_browse")
    async def fake_tool(ctx=None) -> dict:
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(fake_tool())

    row = audit.entries(account_id)[0]
    assert row["outcome"] == "failure"
    assert "CancelledError" in json.loads(row["detail_json"])["error"]


def test_decorator_logs_and_reraises_unexpected_exceptions(audit, account_id):
    @instrument.audit_linkedin_action("feed_browse")
    async def fake_tool(ctx=None) -> dict:
        raise RuntimeError("browser crashed")

    with pytest.raises(RuntimeError, match="browser crashed"):
        asyncio.run(fake_tool())

    row = audit.entries(account_id)[0]
    assert row["outcome"] == "failure"
    assert "browser crashed" in json.loads(row["detail_json"])["error"]


def test_decorator_redacts_captured_credentials(audit, account_id):
    @instrument.audit_linkedin_action("login", target="username", capture=("password",))
    async def fake_tool(username: str, password: str, ctx=None) -> dict:
        return {"status": "success"}

    asyncio.run(fake_tool("nived@example.com", "sup3r-secret"))

    detail = json.loads(audit.entries(account_id)[0]["detail_json"])
    assert detail["password"] == "***"
    assert detail["target"] == "nived@example.com"


def test_tool_still_returns_when_the_audit_write_fails(audit, account_id):
    class BrokenLog:
        def record(self, *args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

    set_audit_log(BrokenLog())

    @instrument.audit_linkedin_action("profile_view")
    async def fake_tool(ctx=None) -> dict:
        return {"status": "success"}

    result = asyncio.run(fake_tool())
    assert result["status"] == "success"
    assert "database is locked" in result["audit_error"]


def test_record_tool_result_writes_one_row_per_batch_item(audit, account_id):
    instrument.record_tool_result(
        "post_comment",
        {"status": "success", "message": "Comment posted successfully"},
        target="https://www.linkedin.com/posts/one",
    )
    instrument.record_tool_result(
        "post_comment",
        {"status": "skipped", "message": "Missing post_url or comment"},
        target="",
    )

    rows = audit.entries(account_id, action_type="post_comment")
    assert {row["outcome"] for row in rows} == {"success", "skipped"}


def test_record_tool_result_surfaces_a_failed_write_on_the_entry(audit, account_id):
    class BrokenLog:
        def record(self, *args, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")

    set_audit_log(BrokenLog())
    entry = {"status": "success", "message": "Comment posted successfully"}

    assert instrument.record_tool_result("post_comment", entry, target="x") is None
    assert "disk I/O error" in entry["audit_error"]


def test_batch_comment_helper_records_every_item(audit, account_id):
    pytest.importorskip("fastmcp")
    from linkedin_browser_mcp import record_comment_outcome

    results = []
    record_comment_outcome(
        results, "https://www.linkedin.com/posts/one", "success", "Comment posted successfully"
    )
    record_comment_outcome(results, "", "skipped", "Missing post_url or comment")
    record_comment_outcome(
        results, "https://www.linkedin.com/posts/two", "error", "Submit button not found"
    )

    assert [entry["status"] for entry in results] == ["success", "skipped", "error"]
    assert {
        row["outcome"] for row in audit.entries(account_id, action_type="post_comment")
    } == {"success", "skipped", "failure"}


def test_every_linkedin_mcp_tool_is_instrumented():
    """Every `@mcp.tool()` in the repository, in any module, is audited.

    Reading only `linkedin_browser_mcp.py` used to be enough because that was
    the only file registering tools. It is not any more, and the narrow version
    of this test passed vacuously for a tool defined anywhere else. An
    unaudited tool is a LinkedIn action with no trail, which breaks the
    human-in-the-loop rule the whole project runs on, so the scan is now the
    whole tree.
    """
    modules = tool_modules()

    assert "linkedin_browser_mcp.py" in modules, "the entry point registers no tools"
    unaudited = {
        module: unaudited
        for module, tools in modules.items()
        if (unaudited := sorted(
            name
            for name, decorators in tools.items()
            if "audit_linkedin_action" not in decorators
        ))
    }
    assert unaudited == {}


def test_the_instrumentation_guard_would_catch_an_unaudited_tool():
    """The guard is not vacuous: feed it a bad module and it says so.

    Without this, a refactor that broke `mcp_tools_in` would turn the guard
    above into a test that always passes, and nobody would notice until an
    action went out with no audit row.
    """
    audited = (
        "@mcp.tool()\n"
        "@audit_linkedin_action('profile_view')\n"
        "async def good_tool():\n"
        "    return {}\n"
    )
    unaudited = "@mcp.tool()\nasync def bad_tool():\n    return {}\n"
    nested = (
        "def register(mcp):\n"
        "    @mcp.tool()\n"
        "    async def nested_tool():\n"
        "        return {}\n"
    )

    assert unaudited_tools_in(audited) == []
    assert unaudited_tools_in(unaudited) == ["bad_tool"]
    assert unaudited_tools_in(audited + unaudited) == ["bad_tool"]
    assert unaudited_tools_in(nested) == ["nested_tool"]
    assert sorted(mcp_tools_in(audited + unaudited)) == ["bad_tool", "good_tool"]


def test_the_instrumentation_guard_covers_more_than_the_entry_point():
    """Tools registered outside `linkedin_browser_mcp.py` are inside the guard.

    MCP-02 put eleven tools in `linkedin_mcp/tools/`. If this ever drops back
    to one module, the scan has been narrowed and the packages that register
    tools are no longer protected.
    """
    modules = tool_modules()
    beyond_entry_point = {
        module: sorted(tools)
        for module, tools in modules.items()
        if module != "linkedin_browser_mcp.py"
    }

    assert beyond_entry_point
    registered = {name for tools in modules.values() for name in tools}
    assert {"harvest_people_search", "harvest_status", "lead_search"} <= registered


def test_read_only_tools_are_audited_too():
    source = (REPO_ROOT / "linkedin_browser_mcp.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    read_tools = {
        "get_linkedin_profile",
        "browse_linkedin_feed",
        "search_linkedin_profiles",
        "view_linkedin_profile",
        "search_linkedin_posts",
    }
    audited = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
        and "audit_linkedin_action" in decorator_names(node)
    }

    assert read_tools.issubset(audited)


# MCP-04 (#27) ---------------------------------------------------------------


def test_the_instrumentation_guard_can_see_mcp_resources_at_all():
    """The guard knows resources exist. It used to have no idea.

    `mcp_tools_in` matches on "mcp.tool", so every `@mcp.resource` slipped past
    it, and this whole file would have stayed green whatever those twelve
    functions did. The count is asserted against the source so that deleting
    the resource package cannot quietly turn the two tests below into no-ops.
    """
    modules = resource_modules()

    assert modules, "no module registers MCP resources; the walk found nothing"
    assert "linkedin_mcp/resources/server.py" in modules
    found = {name for resources in modules.values() for name in resources}
    assert len(found) >= 12, sorted(found)


def test_mcp_resources_are_deliberately_exempt_from_the_audit_decorator():
    """Resources carry no `@audit_linkedin_action`, and that is the intent.

    The reasoning is in the block comment above `mcp_resources_in`, and the
    short version is that `actions_log` is the rate limiter's ledger rather than
    a diary. A read that spends no LinkedIn budget must not write a row that
    claims it did, or `linkedin://safety/today` ends up reporting headroom that
    its own readers consumed.

    This is written as an assertion rather than left implicit so that anyone who
    disagrees has something concrete to delete, and so that a resource which
    quietly grew the decorator has to argue with a failing test first.
    """
    audited = {
        f"{module}::{name}"
        for module, resources in resource_modules().items()
        for name, decorators in resources.items()
        if "audit_linkedin_action" in decorators
    }

    assert audited == set(), (
        "these MCP resources are audited; a resource spends no LinkedIn budget, "
        "so an actions_log row from one corrupts the budget arithmetic that "
        "linkedin://safety/today reports:\n  " + "\n  ".join(sorted(audited))
    )


def test_no_mcp_resource_takes_a_linkedin_action():
    """The exemption is only safe while resources stay reads.

    Exempting resources from the audit decorator is defensible because they do
    nothing worth auditing. That premise needs its own guard, otherwise the
    exemption becomes a hole: a resource that queued a connection request would
    be both unaudited and unnoticed.

    The walk follows helpers defined in the same module, for the same reason
    `tests/test_actions.py` does. A resource that called `_enqueue()` which
    called `enqueue_action()` would look innocent from the decorated body.
    """
    offenders: dict[str, str] = {}

    for module in resource_modules():
        path = REPO_ROOT / module
        tree = ast.parse(path.read_text(encoding="utf-8"))
        local = {
            node.name: node
            for node in ast.walk(tree)
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
        }

        def spends(node: ast.AST, seen: set[str]) -> str | None:
            for inner in ast.walk(node):
                if not isinstance(inner, ast.Call):
                    continue
                callee = inner.func
                name = (
                    callee.id
                    if isinstance(callee, ast.Name)
                    else callee.attr
                    if isinstance(callee, ast.Attribute)
                    else None
                )
                if name is None:
                    continue
                if name in ACTION_SPENDING_CALLS:
                    return name
                if name in seen or name not in local:
                    continue
                seen.add(name)
                if (deeper := spends(local[name], seen)) is not None:
                    return f"{name}() -> {deeper}"
            return None

        for name in mcp_resources_in(path.read_text(encoding="utf-8")):
            if (why := spends(local[name], {name})) is not None:
                offenders[f"{module}::{name}"] = why

    assert offenders == {}, (
        "these MCP resources take or queue a LinkedIn action; a resource is a "
        "read, and one that acts must be a tool with an audit decorator:\n  "
        + "\n  ".join(f"{where}: {why}" for where, why in sorted(offenders.items()))
    )


def test_the_resource_exemption_guard_would_catch_an_acting_resource():
    """The two tests above are not vacuous.

    A resource that carried the decorator is found, and a resource that queued
    an action through a helper is found. Both are checked against the same
    functions the real guards use.
    """
    audited_resource = (
        "@mcp.resource('linkedin://campaigns')\n"
        "@audit_linkedin_action('profile_view')\n"
        "async def campaigns_resource():\n"
        "    return '{}'\n"
    )
    found = mcp_resources_in(audited_resource)
    assert list(found) == ["campaigns_resource"]
    assert "audit_linkedin_action" in found["campaigns_resource"]

    nested = (
        "def register(mcp):\n"
        "    @mcp.resource('linkedin://campaigns')\n"
        "    async def nested_resource():\n"
        "        return '{}'\n"
    )
    assert list(mcp_resources_in(nested)) == ["nested_resource"]

    plain_tool = "@mcp.tool()\nasync def a_tool():\n    return {}\n"
    assert mcp_resources_in(plain_tool) == {}
    assert mcp_tools_in(audited_resource) == {}
