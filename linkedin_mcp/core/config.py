"""Hard safety ceilings for every metered LinkedIn action.

These numbers are the last word. `account_limits` rows may tighten any of them,
which is what a cautious operator or a warming account wants, but nothing
loosens them: `linkedin_mcp.safety.limits` clamps every configured cap against
the ceiling here before a `SafetyGate` grants a lease. An MCP tool therefore
cannot talk its way past a ceiling by writing a bigger number into the database,
and an LLM driving the tools cannot raise one at all.

The defaults come from the two places the caps used to live as prose: the
roadmap (30 invites/day, 100/week, 150 actions/day, 40 direct profile loads/day)
and `.github/copilot-instructions.md` (50 actions/hour).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

__all__ = [
    "APPROVAL_REQUIRED_ACTIONS",
    "ActionCeiling",
    "CONNECTION_ACCEPTED_ACTION",
    "DEDUPE_WINDOW_DAYS",
    "DEFAULT_CEILING",
    "DRAFT_ACTIONS",
    "GLOBAL_DAILY_CEILING",
    "GLOBAL_HOURLY_CEILING",
    "HARD_CEILINGS",
    "INVITE_ACTION",
    "JITTER_MAX_SHRINK",
    "METERED_ACTIONS",
    "PENDING_INVITE_CEILING",
    "PENDING_INVITE_WINDOW_DAYS",
    "PROFILE_VIEW_ACTION",
    "PROFILE_VIEW_DIRECT_ACTION",
    "RAMP_UP_DAYS",
    "RAMP_UP_START_FRACTION",
    "UNMETERED_ACTIONS",
    "ceiling_for",
    "dedupe_window_days",
    "is_metered",
    "profile_view_action",
]


@dataclass(frozen=True, slots=True)
class ActionCeiling:
    """The most one action type may ever run, per rolling window."""

    daily: int
    weekly: int | None = None
    hourly: int | None = None

    def __post_init__(self) -> None:
        if self.daily < 0:
            raise ValueError(f"daily ceiling must be >= 0, got {self.daily}")
        for name in ("weekly", "hourly"):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"{name} ceiling must be >= 0, got {value}")
        if self.weekly is not None and self.weekly < self.daily:
            raise ValueError(
                f"weekly ceiling {self.weekly} is below the daily ceiling {self.daily}, "
                "which would make the daily number unreachable"
            )

    def clamp_daily(self, requested: int | None) -> int:
        """Return the configured daily cap, never above the ceiling."""
        return self.daily if requested is None else max(0, min(int(requested), self.daily))

    def clamp_weekly(self, requested: int | None) -> int | None:
        """Return the configured weekly cap, never above the ceiling."""
        if requested is None:
            return self.weekly
        bounded = max(0, int(requested))
        return bounded if self.weekly is None else min(bounded, self.weekly)

    def clamp_hourly(self, requested: int | None) -> int | None:
        """Return the configured hourly cap, never above the ceiling."""
        if requested is None:
            return self.hourly
        bounded = max(0, int(requested))
        return bounded if self.hourly is None else min(bounded, self.hourly)


INVITE_ACTION = "connection_request"
CONNECTION_ACCEPTED_ACTION = "connection_accepted"
PROFILE_VIEW_ACTION = "profile_view"
PROFILE_VIEW_DIRECT_ACTION = "profile_view_direct"

GLOBAL_DAILY_CEILING = 150
"""Every metered action an account may take in any rolling 24 hours."""

GLOBAL_HOURLY_CEILING = 50
"""Every metered action an account may take in any rolling hour."""

PENDING_INVITE_CEILING = 400
"""Outstanding invitations allowed before the gate stops sending new ones."""

PENDING_INVITE_WINDOW_DAYS = 21
"""How far back an unanswered invitation still counts as pending."""

JITTER_MAX_SHRINK = 0.10
"""Largest slice a day's deterministic jitter may take off a cap."""

RAMP_UP_DAYS = 14
"""Days a fresh account takes to earn its full caps.

Warm-up is counted from `accounts.account_age_days`, whose schema default of
zero means the age was never recorded. Zero therefore reads as an established
account; a genuinely new one is registered with an age of one on its first day.
"""

RAMP_UP_START_FRACTION = 0.2
"""Share of the cap an account gets on the first day of its warm-up."""

HARD_CEILINGS: Mapping[str, ActionCeiling] = MappingProxyType(
    {
        INVITE_ACTION: ActionCeiling(daily=30, weekly=100),
        "message": ActionCeiling(daily=50, weekly=250),
        PROFILE_VIEW_ACTION: ActionCeiling(daily=100),
        PROFILE_VIEW_DIRECT_ACTION: ActionCeiling(daily=40),
        "profile_search": ActionCeiling(daily=50),
        "post_search": ActionCeiling(daily=50),
        "feed_browse": ActionCeiling(daily=40),
        "post_read": ActionCeiling(daily=100),
        "post_like": ActionCeiling(daily=60),
        "post_comment": ActionCeiling(daily=40),
        "post_share": ActionCeiling(daily=10),
    }
)

DEFAULT_CEILING = ActionCeiling(daily=50)
"""Ceiling for an action type nobody has configured yet, so new tools start safe."""

METERED_ACTIONS: frozenset[str] = frozenset(HARD_CEILINGS)
"""Action types that consume the global daily and hourly budgets."""

DRAFT_ACTIONS: frozenset[str] = frozenset(
    {"draft_list_pending", "draft_submit", "draft_approve"}
)
"""SEQ-05 draft bookkeeping. These reach SQLite and nothing else.

Listed separately because the reason they are unmetered is different from
everything else in `UNMETERED_ACTIONS`: those touch LinkedIn but are not
outreach, while these never touch LinkedIn at all. Generating and approving a
draft must cost zero LinkedIn budget, which is the whole reason ICP
qualification is affordable enough to run before the invite step.
"""

UNMETERED_ACTIONS: frozenset[str] = (
    frozenset(
        {
            "login",
            "login_secure",
            "browser_close",
            "post_comment_batch",
            "harvest_enqueue",
            "harvest_status",
            "csv_import",
            "lead_read",
            "lead_export",
            CONNECTION_ACCEPTED_ACTION,
            # MCP-01 (#24): the twelve campaign control tools. Every one of them
            # writes a definition row or flips a status column in the local
            # database, so metering them would spend the account's LinkedIn
            # budget on work LinkedIn never sees. `campaign_start` in particular
            # runs nothing: it sets `campaigns.status` and the worker notices.
            "campaign_create",
            "campaign_add_step",
            "campaign_set_template",
            "campaign_set_icp",
            "campaign_preview",
            "campaign_approve",
            "campaign_start",
            "campaign_pause",
            "campaign_resume",
            "campaign_archive",
            "campaign_status",
            "campaign_add_leads",
        }
    )
    | DRAFT_ACTIONS
)
"""Bookkeeping that is not an outreach action.

Logging in cannot be rate limited without deadlocking recovery, closing the
browser touches nobody, a batch wrapper is metered through the individual
comments it posts, and an accepted invitation is something the other person did.

The five MCP-02 added reach LinkedIn not at all. Its harvest tools write a
`jobs` row and return, `harvest_status` and the CRM reads read the local
database, and a CSV import reads a local file. Metering any of them would spend
a LinkedIn budget on work LinkedIn never sees, and metering the enqueue would
charge one harvest twice: once when it was queued and again when the runner
walked the pages under `profile_search` or `post_read`.

The SEQ-05 draft actions in `DRAFT_ACTIONS` never leave the database either, and
they are named as their own set because that reason is the stronger one.

The metered universe is closed by exclusion, so anything left out of this set
spends the account's daily and hourly LinkedIn budget from its first logged row.
"""

APPROVAL_REQUIRED_ACTIONS: frozenset[str] = frozenset(
    {INVITE_ACTION, "message", "post_comment", "post_share"}
)
"""Actions the gate refuses unless its caller asserts a human signed them off.

The gate enforces the flag it is handed; it cannot see into a caller's head.
The single-shot MCP tools assert approval at the call site because an agent only
reaches them on Nived's direct instruction, after the content was staged for
review. The value of the check is the unattended path: SEQ-04's campaign runner
passes the campaign's real `approval_mode`, so a sequence that was never
approved refuses here instead of quietly inviting people overnight.
"""

DEDUPE_WINDOW_DAYS: Mapping[str, int] = MappingProxyType(
    {
        INVITE_ACTION: 90,
        "message": 7,
        "post_like": 30,
        "post_comment": 30,
        "post_share": 30,
    }
)
"""How long the same action against the same lead counts as a duplicate.

Action types absent from this mapping are never deduplicated, because repeating
them is harmless: viewing a profile twice in a week is normal behaviour.
"""


def ceiling_for(action_type: str) -> ActionCeiling:
    """Return the ceiling for an action type, falling back to a safe default."""
    return HARD_CEILINGS.get(action_type, DEFAULT_CEILING)


def is_metered(action_type: str) -> bool:
    """Return True when the action consumes budget."""
    return action_type not in UNMETERED_ACTIONS


def dedupe_window_days(action_type: str) -> int | None:
    """Return the dedupe window in days, or None when repeats are allowed."""
    return DEDUPE_WINDOW_DAYS.get(action_type)


def profile_view_action(direct: bool) -> str:
    """Return the action type a profile load consumes.

    Loading a profile URL straight from the address bar is the pattern LinkedIn
    throttles hardest, so it gets its own much smaller budget than a view
    reached by navigating through the site.
    """
    return PROFILE_VIEW_DIRECT_ACTION if direct else PROFILE_VIEW_ACTION


# MCP-03 (#26) ---------------------------------------------------------------
#
# Appended as one contiguous block rather than edited inline. Issue #24 is
# changing this same file in a parallel branch, and the only merge conflict of
# the previous wave came from one session restructuring a set literal while
# another added names to it. Nothing above this line is touched.

ADHOC_ENQUEUE_ACTION = "action_enqueue"
"""Audit action type for a tool that queues a one-off LinkedIn action.

Unmetered, and that is the whole point. The tool writes a `jobs` row and
returns; LinkedIn is not contacted until the worker leases the job, and the
worker's `actions_log` row spends the real budget under the action's own type.
Metering the enqueue as well would charge a single invitation twice, once when
an agent asked for it and again when it was actually sent, which is the same
double-count MCP-02 avoided for harvests.
"""

ADHOC_STATUS_ACTION = "action_status"
"""Audit action type for reading a queued action's progress. Local only."""

ADHOC_CANCEL_ACTION = "action_cancel"
"""Audit action type for cancelling a queued action before it runs.

Local only, and deliberately never gated. An operator who wants to stop a
pending action must always be able to, so refusing a cancellation because the
day's budget is spent would be exactly backwards.
"""

ADHOC_QUEUE_ACTIONS: frozenset[str] = frozenset(
    {ADHOC_ENQUEUE_ACTION, ADHOC_STATUS_ACTION, ADHOC_CANCEL_ACTION}
)
"""MCP-03's queue bookkeeping. None of these three reaches LinkedIn."""

UNMETERED_ACTIONS = UNMETERED_ACTIONS | ADHOC_QUEUE_ACTIONS
"""Extended rather than rewritten, for the file-ownership reason above.

`is_metered` reads this name when it is called rather than when the module is
imported, so widening it here is indistinguishable from having listed the three
names in the literal, and it leaves that literal untouched for #24.
"""

__all__ += [
    "ADHOC_CANCEL_ACTION",
    "ADHOC_ENQUEUE_ACTION",
    "ADHOC_QUEUE_ACTIONS",
    "ADHOC_STATUS_ACTION",
]


# MCP-05 (#28) ---------------------------------------------------------------
#
# Appended as one contiguous block for the same file-ownership reason MCP-03
# gave above. Nothing before this line is touched.
#
# The worker-level pause. `campaign_pause` stops one campaign and the ad-hoc
# lane in `linkedin_mcp.worker.selection` never consults a campaign at all, so
# before this there was no way to stop the worker as a whole. These two tools
# write one `worker_control` row and return; LinkedIn is not contacted, so
# metering them would spend an outreach budget on an operator flipping a switch.
#
# Pausing must also never be refused. A caller who wants to stop the worker
# because something is going wrong is exactly the caller whose budget is most
# likely to be spent, and "you have run out of actions, so you may not stop"
# would be the worst possible time to say no. That is the same reasoning
# `ADHOC_CANCEL_ACTION` records for cancelling a queued action.

WORKER_PAUSE_ACTION = "worker_pause"
"""Audit action type for stopping both job lanes. Local only, never metered."""

WORKER_RESUME_ACTION = "worker_resume"
"""Audit action type for letting both job lanes select work again."""

WORKER_CONTROL_ACTIONS: frozenset[str] = frozenset(
    {WORKER_PAUSE_ACTION, WORKER_RESUME_ACTION}
)
"""MCP-05's worker control. Neither of these reaches LinkedIn."""

UNMETERED_ACTIONS = UNMETERED_ACTIONS | WORKER_CONTROL_ACTIONS
"""Extended rather than rewritten, exactly as MCP-03 did directly above."""

__all__ += [
    "WORKER_CONTROL_ACTIONS",
    "WORKER_PAUSE_ACTION",
    "WORKER_RESUME_ACTION",
]
