"""Executors: the code that performs a LinkedIn action once the queue allows it.

MCP-03 (#26) split the eleven original MCP tools in two. Session lifecycle
(`login_linkedin`, `login_linkedin_secure`, `close_browser`) still drives the
browser inline, because the queue's executors need the session those tools
create and queueing them would be circular. Every actual LinkedIn action moved
here, behind `linkedin_mcp.worker.actions.ActionRegistry`, so the tool that used
to click now writes a `jobs` row and the worker does the clicking after
`SafetyGate` has agreed.

The package is deliberately importable without `fastmcp`. The worker must never
import the MCP server (`tests/test_worker_support.py` enforces that), so nothing
under here may reach for `linkedin_browser_mcp` or `linkedin_mcp.tools`.
"""

from __future__ import annotations

from linkedin_mcp.executors.contract import (
    ACTION_KEY,
    ADHOC_ACTIONS,
    ADHOC_JOB_PRIORITY,
    APPROVED_KEY,
    DETAIL_SHAPE,
    PROFILE_SHAPES,
    RESULT_KEY,
    SUMMARY_SHAPE,
    AdHocAction,
    adhoc_action,
    adhoc_action_name,
    adhoc_job_spec,
    adhoc_jobs,
    is_adhoc_action_job,
    job_result,
    record_job_result,
)

__all__ = [
    "ACTION_KEY",
    "ADHOC_ACTIONS",
    "ADHOC_JOB_PRIORITY",
    "APPROVED_KEY",
    "DETAIL_SHAPE",
    "PROFILE_SHAPES",
    "RESULT_KEY",
    "SUMMARY_SHAPE",
    "AdHocAction",
    "adhoc_action",
    "adhoc_action_name",
    "adhoc_job_spec",
    "adhoc_jobs",
    "build_executors",
    "is_adhoc_action_job",
    "job_result",
    "record_job_result",
]


def build_executors() -> dict:
    """Return the `action_type` to executor mapping the worker registers.

    Imported lazily. `linkedin_mcp.executors.linkedin` pulls in the browser
    package, and a caller that only wants the job contract (an MCP tool
    enqueuing work, say) should not pay for that.
    """
    from linkedin_mcp.executors.linkedin import build_executors as _build

    return _build()
