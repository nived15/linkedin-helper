"""MCP tool surface for lead extraction and CRM reads (MCP-02, #25).

Eleven tools live here: the eight harvest tools and the three CRM read tools.
They are registered onto the server `linkedin_browser_mcp.py` owns, through
:func:`register_lead_tools`, rather than importing that module back, so there
is no import cycle and the tools can be exercised against a bare `FastMCP` in a
test.

Every tool in this package is decorated with `@audit_linkedin_action`. That is
not optional and it is not a convention: an MCP tool with no audit row is a
LinkedIn action with no trail, and `tests/test_audit_log.py` fails the build for
any `@mcp.tool()` in any module that lacks it.

None of these tools reach LinkedIn. The harvest tools write a `jobs` row and
return; the CRM tools read the local database. The action types they audit are
therefore unmetered in `linkedin_mcp.core.config`, because spending an
account's LinkedIn budget on a local write would both throttle the wrong thing
and double-count the harvest, once when it was queued and again when SEQ-04
actually walked the pages.
"""

from fastmcp import FastMCP

from linkedin_mcp.tools.contract import (
    CSV_IMPORT_ACTION,
    HARVEST_ACTIONS,
    HARVEST_ENQUEUE_ACTION,
    HARVEST_JOB_PRIORITY,
    HARVEST_KEY,
    HARVEST_STATUS_ACTION,
    LEAD_EXPORT_ACTION,
    LEAD_READ_ACTION,
    LOCAL_HARVEST_ACTIONS,
    RUN_ID_KEY,
    HarvestAction,
    harvest_action,
    harvest_job_spec,
    harvest_jobs,
    is_harvest_job,
    job_harvest_name,
    job_run_id,
)
from linkedin_mcp.tools.crm import EXPORT_COLUMNS, register_crm_tools
from linkedin_mcp.tools.harvest import MAX_HARVEST_LIMIT, register_harvest_tools

__all__ = [
    "CSV_IMPORT_ACTION",
    "EXPORT_COLUMNS",
    "HARVEST_ACTIONS",
    "HARVEST_ENQUEUE_ACTION",
    "HARVEST_JOB_PRIORITY",
    "HARVEST_KEY",
    "HARVEST_STATUS_ACTION",
    "LEAD_EXPORT_ACTION",
    "LEAD_READ_ACTION",
    "LOCAL_HARVEST_ACTIONS",
    "MAX_HARVEST_LIMIT",
    "RUN_ID_KEY",
    "HarvestAction",
    "harvest_action",
    "harvest_job_spec",
    "harvest_jobs",
    "is_harvest_job",
    "job_harvest_name",
    "job_run_id",
    "register_crm_tools",
    "register_harvest_tools",
    "register_lead_tools",
]


def register_lead_tools(mcp: FastMCP) -> None:
    """Register every MCP-02 tool on `mcp`.

    `harvest_sales_nav` is not among them, deliberately. SCRAPE-02 is descoped
    because Sales Navigator needs a paid subscription, so there is no extractor
    behind it and a tool would be a promise the codebase cannot keep.
    """
    register_harvest_tools(mcp)
    register_crm_tools(mcp)


# MCP-03 (#26) ---------------------------------------------------------------
#
# Appended as one contiguous block. Issue #24 is changing this same file in a
# parallel branch, so nothing above this line is touched: the import list, the
# `__all__` literal and `register_lead_tools` are all left exactly as MCP-02
# wrote them.
#
# MCP-03's tools are a different thing from MCP-02's. A harvest tool queues an
# extraction that only ever reads. These queue the actions that write: an
# invitation, a comment, a share. That is why they are registered separately and
# why `linkedin_browser_mcp.py` calls both.

from linkedin_mcp.tools.actions import (  # noqa: E402
    MAX_NOTE_CHARS,
    enqueue_action,
    register_action_tools,
    validated_payload,
)

__all__ += [
    "MAX_NOTE_CHARS",
    "enqueue_action",
    "register_action_tools",
    "validated_payload",
]
