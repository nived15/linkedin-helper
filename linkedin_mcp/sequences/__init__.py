"""Multi-step outreach sequences: the campaign state machine and its job queue.

This package is a plain Python API. It registers no MCP tools and starts no
background work: MCP-01 (#24) and MCP-03 (#26) own the tool surface, SEQ-04 (#22)
owns the runner. What lives here is the model those three drive.

The model in one paragraph
--------------------------
A campaign owns an ordered list of steps in `campaign_steps`. Every lead enrolled
in it gets one row in `campaign_leads`, and that row *is* the state machine: a
sub-list saying where the lead is and a `current_step_ord` saying what it will do
next. `jobs` is a projection of those rows, one open job per lead still in the
flow. Because the queue is derived rather than authoritative, it can be deleted
whole and rebuilt from campaign state, which is the corruption recovery path.

The seven sub-lists
-------------------
`queue` and `processing` are active; `successful`, `failed`, `replied`, `skipped`
and `excluded` are terminal. The last two are both "left the flow" but they are
not the same thing: `skipped` means the lead did not match this campaign's
conditions and can be re-enrolled, `excluded` means it must not be contacted here
at all and has no transition out. See :mod:`linkedin_mcp.sequences.states`.

Branching without a graph
-------------------------
A filter step is an ordinary step in the linear list. It never forks the flow: a
match advances the lead exactly like any other completed step, and a no-match
drops it into `skipped` or `excluded`. That is the entire branching mechanism, so
there is never a second path to schedule or reconcile.

Atomicity
---------
Every transition in :mod:`linkedin_mcp.sequences.transitions` is one transaction
covering both the `campaign_leads` write and the `jobs` write. An interrupted
transition rolls back whole, so a crash cannot leave a lead half-moved into
`processing`. A worker that *committed* a claim and then died is a different
problem, and :func:`~linkedin_mcp.sequences.transitions.recover_stranded` is its
answer.

Seams the blocked issues plug into
----------------------------------
- **SEQ-03 (#21), inbox scanning and reply detection.** Call
  :func:`~linkedin_mcp.sequences.transitions.mark_replied` when an inbound
  message is found. It fires from `queue` or `processing` and cancels the open
  job, so a message already queued for tonight does not go out on top of a reply.
  This package never reads the inbox.
- **SEQ-04 (#22), the scheduled runner.** Lease work with
  :func:`~linkedin_mcp.sequences.jobs.due_jobs`, take the lead with
  :func:`~linkedin_mcp.sequences.transitions.claim_step`, run the action, then
  land exactly one of :func:`~linkedin_mcp.sequences.transitions.complete_step`,
  :func:`~linkedin_mcp.sequences.transitions.fail_step`,
  :func:`~linkedin_mcp.sequences.transitions.refuse_step` or
  :func:`~linkedin_mcp.sequences.transitions.apply_filter_step`. Pass the same
  `worker_id` to the finalising call as to `claim_step`: that is the fence that
  stops a worker which stalled past its lease from finishing a lead the sweep
  already handed to somebody else. Sweep with
  :func:`~linkedin_mcp.sequences.transitions.recover_all_stranded` on startup and
  on a timer. `worker_heartbeat` is untouched here and is yours. Steps whose
  `action_type` is in :data:`~linkedin_mcp.sequences.steps.LOCAL_ACTIONS` reach
  nothing on LinkedIn and must not consume a safety-gate lease; everything else
  goes through `linkedin_mcp.safety.guard_action` and, when refused, through
  :func:`~linkedin_mcp.sequences.transitions.refuse_step` with the gate's own
  `RefusalReason`. Several transitions can be batched inside one
  :func:`~linkedin_mcp.sequences.transaction.transaction` block; a failure in one
  unwinds only that one.
- **SEQ-05 (#23), AI drafts and ICP qualification.** Register a predicate with
  :func:`~linkedin_mcp.sequences.filters.register_filter` and a campaign gains
  ICP filtering by naming it in a step's config. Drafts belong in `ai_drafts`;
  a step waiting on approval is a `refuse_step` with
  `RefusalReason.APPROVAL_REQUIRED`, which re-queues rather than failing.
- **SEQ-02 (#20), the template engine.** A job's payload is a pointer to a step,
  never rendered content, so rendering happens when the job runs. Nothing in this
  package imports the templating package.
"""

from linkedin_mcp.sequences.campaigns import (
    APPROVAL_MODES,
    CAMPAIGN_STATUSES,
    RUNNABLE_STATUSES,
    Campaign,
    create_campaign,
    get_campaign,
    is_runnable,
    list_campaigns,
    require_campaign,
    set_campaign_status,
)
from linkedin_mcp.sequences.enrollment import (
    BLACKLIST_OUTCOME,
    EXCLUDE_LIST_OUTCOME,
    CampaignLead,
    EnrolmentSummary,
    enrol_lead,
    enrol_leads,
    get_campaign_lead,
    list_campaign_leads,
    on_exclude_list,
    require_campaign_lead,
    sublist_counts,
    withdraw_lead,
)
from linkedin_mcp.sequences.errors import (
    CampaignInFlightError,
    CampaignLeadNotFoundError,
    CampaignNotFoundError,
    FilterNotRegisteredError,
    InvalidTransitionError,
    SequenceError,
    StepDefinitionError,
    StepNotFoundError,
)
from linkedin_mcp.sequences.filters import (
    BUILT_IN_FILTERS,
    FilterContext,
    FilterPredicate,
    evaluate_filter,
    get_filter,
    register_filter,
    registered_filters,
    reset_filters,
    unregister_filter,
)
from linkedin_mcp.sequences.jobs import (
    DERIVED_COLUMNS,
    Job,
    JobSpec,
    RebuildReport,
    close_open_jobs,
    derive_jobs,
    due_jobs,
    insert_job,
    list_jobs,
    open_job_for_lead,
    open_job_specs,
    orphan_open_jobs,
    queue_matches_state,
    rebuild_jobs,
    step_payload,
)
from linkedin_mcp.sequences.states import (
    ACTIVE_SUBLISTS,
    CLOSED_JOB_STATES,
    DEFAULT_RETRY_AFTER_SECONDS,
    OPEN_JOB_STATES,
    REFUSAL_DISPOSITIONS,
    RETRY_AFTER_SECONDS,
    SUBLIST_TRANSITIONS,
    TERMINAL_SUBLISTS,
    Disposition,
    JobState,
    Sublist,
    can_transition,
    coerce_job_state,
    coerce_sublist,
    disposition_for,
    retry_after_seconds,
)
from linkedin_mcp.sequences.steps import (
    DEFAULT_MAX_ATTEMPTS,
    FILTER_ACTION,
    LOCAL_ACTIONS,
    MISSING_DATA_DISPOSITIONS,
    ON_FAILURE_FAIL,
    ON_FAILURE_MODES,
    ON_FAILURE_RETRY,
    ON_FAILURE_SKIP,
    Step,
    StepSpec,
    add_step,
    define_steps,
    find_step_at_ord,
    first_step_ord,
    get_step,
    last_step_ord,
    list_steps,
    next_step_ord,
    step_at_ord,
)
from linkedin_mcp.sequences.transaction import (
    as_timestamp,
    now_timestamp,
    shift_timestamp,
    transaction,
    utc_now,
)
from linkedin_mcp.sequences.transitions import (
    DEFAULT_LEASE_SECONDS,
    apply_filter_step,
    claim_step,
    complete_step,
    current_step,
    exclude_lead,
    fail_step,
    mark_replied,
    recover_all_stranded,
    recover_stranded,
    refuse_step,
    reset_lead,
    skip_lead,
)

__all__ = [
    "ACTIVE_SUBLISTS",
    "APPROVAL_MODES",
    "BLACKLIST_OUTCOME",
    "BUILT_IN_FILTERS",
    "CAMPAIGN_STATUSES",
    "CLOSED_JOB_STATES",
    "DEFAULT_LEASE_SECONDS",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_RETRY_AFTER_SECONDS",
    "DERIVED_COLUMNS",
    "EXCLUDE_LIST_OUTCOME",
    "FILTER_ACTION",
    "LOCAL_ACTIONS",
    "MISSING_DATA_DISPOSITIONS",
    "ON_FAILURE_FAIL",
    "ON_FAILURE_MODES",
    "ON_FAILURE_RETRY",
    "ON_FAILURE_SKIP",
    "OPEN_JOB_STATES",
    "REFUSAL_DISPOSITIONS",
    "RETRY_AFTER_SECONDS",
    "RUNNABLE_STATUSES",
    "SUBLIST_TRANSITIONS",
    "TERMINAL_SUBLISTS",
    "Campaign",
    "CampaignInFlightError",
    "CampaignLead",
    "CampaignLeadNotFoundError",
    "CampaignNotFoundError",
    "Disposition",
    "EnrolmentSummary",
    "FilterContext",
    "FilterNotRegisteredError",
    "FilterPredicate",
    "InvalidTransitionError",
    "Job",
    "JobSpec",
    "JobState",
    "RebuildReport",
    "SequenceError",
    "Step",
    "StepDefinitionError",
    "StepNotFoundError",
    "StepSpec",
    "Sublist",
    "add_step",
    "apply_filter_step",
    "as_timestamp",
    "can_transition",
    "claim_step",
    "close_open_jobs",
    "coerce_job_state",
    "coerce_sublist",
    "complete_step",
    "create_campaign",
    "current_step",
    "define_steps",
    "derive_jobs",
    "disposition_for",
    "due_jobs",
    "enrol_lead",
    "enrol_leads",
    "evaluate_filter",
    "exclude_lead",
    "fail_step",
    "find_step_at_ord",
    "first_step_ord",
    "get_campaign",
    "get_campaign_lead",
    "get_filter",
    "get_step",
    "insert_job",
    "is_runnable",
    "last_step_ord",
    "list_campaign_leads",
    "list_campaigns",
    "list_jobs",
    "list_steps",
    "mark_replied",
    "next_step_ord",
    "now_timestamp",
    "on_exclude_list",
    "open_job_for_lead",
    "open_job_specs",
    "orphan_open_jobs",
    "queue_matches_state",
    "rebuild_jobs",
    "recover_all_stranded",
    "recover_stranded",
    "refuse_step",
    "register_filter",
    "registered_filters",
    "require_campaign",
    "require_campaign_lead",
    "reset_filters",
    "reset_lead",
    "retry_after_seconds",
    "set_campaign_status",
    "shift_timestamp",
    "skip_lead",
    "step_at_ord",
    "step_payload",
    "sublist_counts",
    "transaction",
    "unregister_filter",
    "utc_now",
    "withdraw_lead",
]
