"""The state vocabulary: sub-lists, job states and the moves between them.

`campaign_leads.sublist` is the state machine. The seven values here are exactly
the `CHECK` constraint in `0001_init.sql`, so the enum and the database agree by
construction rather than by convention.

Where a lead can be
-------------------
Two sub-lists are *active*, meaning the lead is still in the flow:

- `queue` waits for its next step; `next_run_at` says when it is due.
- `processing` is executing one step right now, under a leased job.

Five are *terminal*, meaning the lead has left the flow:

- `successful` finished every step.
- `failed` exhausted its retries on a step, or hit a step configured to stop.
- `replied` answered, so the sequence stops out of courtesy. SEQ-03 (#21) writes
  this one.
- `skipped` did not match this campaign's conditions. The lead was eligible; the
  conditions simply did not apply. Re-enrolling it later is legitimate.
- `excluded` must not be contacted by this campaign at all: blacklisted, on the
  campaign's exclude list, or refused by a rule that will not change. This is a
  policy verdict rather than a flow outcome, so it is the one state with no way
  out. Every other terminal sub-list can return to `queue` through
  `reset_lead`.

That distinction is the whole reason both exist. `skipped` says "not this time",
`excluded` says "not ever, not here".
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from types import MappingProxyType

from linkedin_mcp.audit import RefusalReason

__all__ = [
    "ACTIVE_SUBLISTS",
    "CLOSED_JOB_STATES",
    "DEFAULT_RETRY_AFTER_SECONDS",
    "Disposition",
    "JobState",
    "OPEN_JOB_STATES",
    "REFUSAL_DISPOSITIONS",
    "RETRY_AFTER_SECONDS",
    "SUBLIST_TRANSITIONS",
    "TERMINAL_SUBLISTS",
    "Sublist",
    "can_transition",
    "coerce_job_state",
    "coerce_sublist",
    "disposition_for",
    "retry_after_seconds",
]


class Sublist(str, Enum):
    """Where a lead sits in a campaign. Mirrors the `campaign_leads` CHECK."""

    QUEUE = "queue"
    PROCESSING = "processing"
    SUCCESSFUL = "successful"
    FAILED = "failed"
    REPLIED = "replied"
    SKIPPED = "skipped"
    EXCLUDED = "excluded"


class JobState(str, Enum):
    """Lifecycle of a row in the `jobs` execution queue."""

    PENDING = "pending"
    LEASED = "leased"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REFUSED = "refused"


ACTIVE_SUBLISTS: tuple[Sublist, ...] = (Sublist.QUEUE, Sublist.PROCESSING)
"""Sub-lists whose leads are still in the flow and still derive a job."""

TERMINAL_SUBLISTS: tuple[Sublist, ...] = (
    Sublist.SUCCESSFUL,
    Sublist.FAILED,
    Sublist.REPLIED,
    Sublist.SKIPPED,
    Sublist.EXCLUDED,
)
"""Sub-lists whose leads have left the flow and derive no job."""

OPEN_JOB_STATES: tuple[JobState, ...] = (JobState.PENDING, JobState.LEASED)
"""Job states that still represent outstanding work."""

CLOSED_JOB_STATES: tuple[JobState, ...] = (
    JobState.DONE,
    JobState.FAILED,
    JobState.CANCELLED,
    JobState.REFUSED,
)
"""Job states that are history. Rebuilding the queue leaves these alone."""

_TRANSITIONS: dict[Sublist, frozenset[Sublist]] = {
    # A lead in the queue can be claimed, or can leave the flow without ever being
    # claimed: a reply lands, a sweep excludes it, or a refusal resolves the step
    # it was waiting on. Only `processing` is reachable by claiming, and only from
    # here, which is what makes the claim a real lease.
    Sublist.QUEUE: frozenset(
        {
            Sublist.PROCESSING,
            Sublist.SUCCESSFUL,
            Sublist.FAILED,
            Sublist.REPLIED,
            Sublist.SKIPPED,
            Sublist.EXCLUDED,
        }
    ),
    Sublist.PROCESSING: frozenset(
        {
            Sublist.QUEUE,
            Sublist.SUCCESSFUL,
            Sublist.FAILED,
            Sublist.REPLIED,
            Sublist.SKIPPED,
            Sublist.EXCLUDED,
        }
    ),
    # Re-enrolment is the only way back in, and it always restarts at `queue`.
    Sublist.SUCCESSFUL: frozenset({Sublist.QUEUE}),
    Sublist.FAILED: frozenset({Sublist.QUEUE}),
    Sublist.REPLIED: frozenset({Sublist.QUEUE}),
    Sublist.SKIPPED: frozenset({Sublist.QUEUE}),
    # `excluded` is final. A lead barred from this campaign stays barred.
    Sublist.EXCLUDED: frozenset(),
}

SUBLIST_TRANSITIONS: Mapping[Sublist, frozenset[Sublist]] = MappingProxyType(_TRANSITIONS)
"""Every move the state machine allows, keyed by the sub-list moved from."""


class Disposition(str, Enum):
    """What a refused step does to the lead that was going to run it."""

    RETRY_LATER = "retry_later"
    ADVANCE = "advance"
    SKIP = "skip"
    EXCLUDE = "exclude"
    FAIL = "fail"


_REFUSAL_DISPOSITIONS: dict[RefusalReason, Disposition] = {
    # Temporary: the account or the clock will allow this later.
    RefusalReason.DAILY_CAP_REACHED: Disposition.RETRY_LATER,
    RefusalReason.WEEKLY_CAP_REACHED: Disposition.RETRY_LATER,
    RefusalReason.HOURLY_CAP_REACHED: Disposition.RETRY_LATER,
    RefusalReason.OUTSIDE_WORKING_HOURS: Disposition.RETRY_LATER,
    RefusalReason.ACCOUNT_PAUSED: Disposition.RETRY_LATER,
    RefusalReason.ACCOUNT_COOLDOWN: Disposition.RETRY_LATER,
    RefusalReason.ACCOUNT_CHALLENGED: Disposition.RETRY_LATER,
    RefusalReason.ACCOUNT_LOGGED_OUT: Disposition.RETRY_LATER,
    RefusalReason.WARMUP_LIMIT: Disposition.RETRY_LATER,
    # A human has not signed the content off yet. Waiting is the correct answer.
    RefusalReason.APPROVAL_REQUIRED: Disposition.RETRY_LATER,
    # The step already happened inside its dedupe window, so it is satisfied and
    # the lead moves on rather than sitting on a step it can never repeat.
    RefusalReason.DUPLICATE_ACTION: Disposition.ADVANCE,
    # The operator turned this action off for the account. Retrying forever would
    # pin the lead on a step nothing will ever run, so it leaves the flow softly.
    RefusalReason.ACTION_DISABLED: Disposition.SKIP,
    # Never contact this person again, on any account.
    RefusalReason.LEAD_BLACKLISTED: Disposition.EXCLUDE,
}

REFUSAL_DISPOSITIONS: Mapping[RefusalReason, Disposition] = MappingProxyType(
    _REFUSAL_DISPOSITIONS
)
"""How each CORE-03 refusal reason resolves into a sub-list move.

The sequence engine reuses `RefusalReason` rather than inventing a second
vocabulary, so a refusal raised by the gate, its row in the audit log and the
lead's `last_outcome` all use the same word.
"""

DEFAULT_RETRY_AFTER_SECONDS = 3600
"""Wait before a retried step is due again, when the reason has no own delay."""

_RETRY_AFTER_SECONDS: dict[RefusalReason, int] = {
    RefusalReason.HOURLY_CAP_REACHED: 3600,
    RefusalReason.DAILY_CAP_REACHED: 6 * 3600,
    RefusalReason.WEEKLY_CAP_REACHED: 24 * 3600,
    RefusalReason.OUTSIDE_WORKING_HOURS: 3600,
    RefusalReason.ACCOUNT_PAUSED: 6 * 3600,
    RefusalReason.ACCOUNT_COOLDOWN: 6 * 3600,
    RefusalReason.ACCOUNT_CHALLENGED: 12 * 3600,
    RefusalReason.ACCOUNT_LOGGED_OUT: 3600,
    RefusalReason.WARMUP_LIMIT: 24 * 3600,
    RefusalReason.APPROVAL_REQUIRED: 3600,
}

RETRY_AFTER_SECONDS: Mapping[RefusalReason, int] = MappingProxyType(_RETRY_AFTER_SECONDS)
"""How long a `RETRY_LATER` refusal pushes the lead's next attempt out.

A weekly cap waits a day rather than an hour, because retrying at the old rate
would burn the queue on refusals that cannot succeed yet.
"""


def coerce_sublist(value: Sublist | str) -> Sublist:
    """Return the `Sublist` member for a value, rejecting anything else."""
    if isinstance(value, Sublist):
        return value
    try:
        return Sublist(value)
    except ValueError:
        raise ValueError(
            f"unknown sublist {value!r}; expected one of "
            f"{sorted(member.value for member in Sublist)}"
        ) from None


def coerce_job_state(value: JobState | str) -> JobState:
    """Return the `JobState` member for a value, rejecting anything else."""
    if isinstance(value, JobState):
        return value
    try:
        return JobState(value)
    except ValueError:
        raise ValueError(
            f"unknown job state {value!r}; expected one of "
            f"{sorted(member.value for member in JobState)}"
        ) from None


def can_transition(source: Sublist | str, target: Sublist | str) -> bool:
    """Return True when the state machine allows this move.

    Staying put is always allowed: re-running a transition that already landed is
    how an interrupted worker recovers, so it must not raise.
    """
    origin = coerce_sublist(source)
    destination = coerce_sublist(target)
    if origin is destination:
        return True
    return destination in SUBLIST_TRANSITIONS[origin]


def disposition_for(reason: RefusalReason | str) -> Disposition:
    """Return how a refusal reason resolves for a lead mid-sequence."""
    if not isinstance(reason, RefusalReason):
        reason = RefusalReason(reason)
    return REFUSAL_DISPOSITIONS[reason]


def retry_after_seconds(reason: RefusalReason | str) -> int:
    """Return the delay a `RETRY_LATER` refusal imposes before the next attempt."""
    if not isinstance(reason, RefusalReason):
        reason = RefusalReason(reason)
    return RETRY_AFTER_SECONDS.get(reason, DEFAULT_RETRY_AFTER_SECONDS)
