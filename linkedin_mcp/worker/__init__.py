"""SEQ-04: the scheduled background runner.

`worker.py` at the repo root is the daemon. This package is what it runs, split
so that every piece can be tested without a browser, without a network and
without waiting for a real clock.

- :mod:`~linkedin_mcp.worker.selection` decides what is due, including the
  campaign-less jobs `sequences.due_jobs` cannot see.
- :mod:`~linkedin_mcp.worker.control` is the worker-level pause both lanes obey,
  which is a different thing from `campaign_pause` stopping one campaign.
- :mod:`~linkedin_mcp.worker.heartbeat` is how `worker_status` can say "stalled"
  and mean it.
- :mod:`~linkedin_mcp.worker.actions` holds the seams: executors, the browser and
  the drafts queue all arrive injected.
- :mod:`~linkedin_mcp.worker.runner` is the loop itself.

Nothing here imports `linkedin_browser_mcp`, Playwright or an LLM client. The MCP
server is a control plane over SQLite and this is the only thing that acts.
"""

from linkedin_mcp.worker.actions import (
    ActionContext,
    ActionRegistry,
    ActionResult,
    ActionStatus,
    BrowserSupplier,
    DraftKind,
    DraftParker,
    DraftRequest,
    Executor,
    no_browser,
    no_draft_parker,
)
from linkedin_mcp.worker.control import (
    PauseState,
    is_worker_paused,
    pause_worker,
    resume_worker,
    worker_pause_state,
)
from linkedin_mcp.worker.heartbeat import (
    DEFAULT_STALLED_AFTER_SECONDS,
    LIVE_STATUSES,
    STATUS_CLOSED,
    STATUS_ERROR,
    STATUS_IDLE,
    STATUS_PAUSED,
    STATUS_RUNNING,
    STATUS_SELECTING,
    STATUS_STARTING,
    STATUS_STOPPED,
    STATUS_SWEEPING,
    STATUSES,
    WorkerHeartbeat,
    active_campaign_count,
    clear_heartbeat,
    list_heartbeats,
    read_heartbeat,
    seconds_since,
    worker_status,
    write_heartbeat,
)
from linkedin_mcp.worker.runner import (
    AD_HOC_MAX_ATTEMPTS,
    AD_HOC_RETRY_BACKOFF_SECONDS,
    JobReport,
    TickReport,
    Worker,
    WorkerConfig,
    build_worker,
    campaign_funnel,
)
from linkedin_mcp.worker.selection import (
    AD_HOC_ORD,
    SELECTION_WINDOW,
    Bunch,
    Selection,
    ad_hoc_due_jobs,
    bunch_jobs,
    is_ad_hoc,
    is_campaign_work,
    is_unroutable,
    job_step,
    reclaim_stranded_ad_hoc,
    select_due_jobs,
    sort_key,
    unroutable_open_jobs,
)

__all__ = [
    "AD_HOC_MAX_ATTEMPTS",
    "AD_HOC_ORD",
    "AD_HOC_RETRY_BACKOFF_SECONDS",
    "DEFAULT_STALLED_AFTER_SECONDS",
    "LIVE_STATUSES",
    "SELECTION_WINDOW",
    "STATUSES",
    "STATUS_CLOSED",
    "STATUS_ERROR",
    "STATUS_IDLE",
    "STATUS_PAUSED",
    "STATUS_RUNNING",
    "STATUS_SELECTING",
    "STATUS_STARTING",
    "STATUS_STOPPED",
    "STATUS_SWEEPING",
    "ActionContext",
    "ActionRegistry",
    "ActionResult",
    "ActionStatus",
    "BrowserSupplier",
    "Bunch",
    "DraftKind",
    "DraftParker",
    "DraftRequest",
    "Executor",
    "JobReport",
    "PauseState",
    "Selection",
    "TickReport",
    "Worker",
    "WorkerConfig",
    "WorkerHeartbeat",
    "active_campaign_count",
    "ad_hoc_due_jobs",
    "build_worker",
    "bunch_jobs",
    "campaign_funnel",
    "clear_heartbeat",
    "is_ad_hoc",
    "is_campaign_work",
    "is_unroutable",
    "is_worker_paused",
    "job_step",
    "list_heartbeats",
    "no_browser",
    "no_draft_parker",
    "pause_worker",
    "read_heartbeat",
    "reclaim_stranded_ad_hoc",
    "resume_worker",
    "seconds_since",
    "select_due_jobs",
    "sort_key",
    "unroutable_open_jobs",
    "worker_pause_state",
    "worker_status",
    "write_heartbeat",
]
