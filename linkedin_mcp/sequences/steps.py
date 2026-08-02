"""Step definitions: the ordered list a campaign runs, stored per campaign.

Steps live in `campaign_steps`, one row per position, keyed by `(campaign_id,
ord)`. Nothing about a step lives in memory, so a restart reads the same list
back and a lead's `current_step_ord` still points at the same action.

Ords and what `current_step_ord` means
--------------------------------------
Ords are 1-based and contiguous. `campaign_leads.current_step_ord` always names
the step the lead will run **next**, never the one it just ran. A lead that
finishes the final step is parked at `last_ord + 1`, which is past the end and
therefore unambiguous.

Filter steps
------------
A filter is an ordinary step with `action_type = 'filter'`. It reaches nothing on
LinkedIn: it evaluates a predicate and either lets the lead continue to the next
ord or drops it out of the flow entirely. There is no second branch and no fork,
which is how a linear engine gets conditional behaviour. `config.on_no_match`
picks the exit, `skipped` by default. See :mod:`linkedin_mcp.sequences.states`
for why `skipped` and `excluded` are different exits.

Delays
------
A delay is a property of a step rather than a step of its own: `config.delay_seconds`
is how long after the previous step this one becomes due. It is applied when the
lead is scheduled onto the step, so no job is burnt on waiting.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from linkedin_mcp.sequences import jobs as jobs_module
from linkedin_mcp.sequences.errors import (
    CampaignInFlightError,
    StepDefinitionError,
    StepNotFoundError,
)
from linkedin_mcp.sequences.states import ACTIVE_SUBLISTS, Sublist
from linkedin_mcp.sequences.transaction import transaction

__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_RETRY_BACKOFF_SECONDS",
    "FILTER_ACTION",
    "LOCAL_ACTIONS",
    "MISSING_DATA_DISPOSITIONS",
    "ON_FAILURE_FAIL",
    "ON_FAILURE_MODES",
    "ON_FAILURE_RETRY",
    "ON_FAILURE_SKIP",
    "Step",
    "StepSpec",
    "add_step",
    "define_steps",
    "find_step_at_ord",
    "first_step_ord",
    "get_step",
    "last_step_ord",
    "list_steps",
    "next_step_ord",
    "step_at_ord",
    "step_row",
]

FILTER_ACTION = "filter"
"""Action type of a step that decides whether a lead stays in the flow."""

LOCAL_ACTIONS: frozenset[str] = frozenset({FILTER_ACTION, "tag", "webhook", "custom_field"})
"""Step actions that touch nothing on LinkedIn.

A runner must not spend a safety-gate lease on these, because they consume no
LinkedIn budget. Every other action type is an outreach action and goes through
`linkedin_mcp.safety.guard_action` before it runs.
"""

ON_FAILURE_RETRY = "retry"
ON_FAILURE_SKIP = "skip"
ON_FAILURE_FAIL = "fail"

ON_FAILURE_MODES: tuple[str, ...] = (ON_FAILURE_RETRY, ON_FAILURE_SKIP, ON_FAILURE_FAIL)
"""What happens when a step's action fails.

`retry` re-queues until `max_attempts`, then falls through to `fail`. `skip`
drops the lead out of the flow into `skipped` on the first failure. `fail` sends
it to `failed` immediately.
"""

MISSING_DATA_DISPOSITIONS: Mapping[str, Sublist | None] = {
    # A profile visit can fill the gap, so the lead stays in the flow.
    "visit_extract": None,
    "skip": Sublist.SKIPPED,
}
"""How `campaign_steps.on_missing_data` resolves when a token cannot be filled.

SEQ-02 (#20) decides *whether* data is missing when it renders a template. This
package only says what the lead does about it.
"""

DEFAULT_MAX_ATTEMPTS = 3
"""Attempts a `retry` step gets before the lead is marked failed."""

DEFAULT_RETRY_BACKOFF_SECONDS = 900
"""Base wait between attempts. Doubled per attempt by the transition layer."""


@dataclass(frozen=True, slots=True)
class Step:
    """One position in a campaign's sequence, read back from `campaign_steps`."""

    id: int
    campaign_id: int
    ord: int
    action_type: str
    config: Mapping[str, Any] = field(default_factory=dict)
    template_id: int | None = None
    bunch_size: int = 1
    on_failure: str = ON_FAILURE_RETRY
    on_missing_data: str | None = None

    @property
    def is_filter(self) -> bool:
        return self.action_type == FILTER_ACTION

    @property
    def is_local(self) -> bool:
        """True when running this step reaches nothing on LinkedIn."""
        return self.action_type in LOCAL_ACTIONS

    @property
    def delay_seconds(self) -> int:
        """Wait after the previous step before this one becomes due."""
        return max(0, int(self.config.get("delay_seconds", 0)))

    @property
    def max_attempts(self) -> int:
        return max(1, int(self.config.get("max_attempts", DEFAULT_MAX_ATTEMPTS)))

    @property
    def retry_backoff_seconds(self) -> int:
        return max(
            0, int(self.config.get("retry_backoff_seconds", DEFAULT_RETRY_BACKOFF_SECONDS))
        )

    @property
    def priority(self) -> int:
        """Queue priority of the job this step derives. Higher runs first."""
        return int(self.config.get("priority", 0))

    @property
    def filter_name(self) -> str | None:
        """Name of the registered predicate a filter step evaluates."""
        if not self.is_filter:
            return None
        name = self.config.get("filter")
        return None if name is None else str(name)

    @property
    def no_match_sublist(self) -> Sublist:
        """Where a filter step sends a lead whose predicate did not match."""
        raw = str(self.config.get("on_no_match", Sublist.SKIPPED.value))
        exit_sublist = Sublist(raw)
        if exit_sublist not in (Sublist.SKIPPED, Sublist.EXCLUDED):
            raise StepDefinitionError(
                f"step {self.ord} of campaign {self.campaign_id} has "
                f"on_no_match={raw!r}; a filter may only drop a lead into "
                f"{Sublist.SKIPPED.value!r} or {Sublist.EXCLUDED.value!r}"
            )
        return exit_sublist

    @property
    def missing_data_sublist(self) -> Sublist | None:
        """Where the lead goes when this step's data cannot be filled in."""
        if self.on_missing_data is None:
            return None
        return MISSING_DATA_DISPOSITIONS[self.on_missing_data]


@dataclass(frozen=True, slots=True)
class StepSpec:
    """A step to define. `ord` is assigned by position in the list."""

    action_type: str
    config: Mapping[str, Any] = field(default_factory=dict)
    template_id: int | None = None
    bunch_size: int = 1
    on_failure: str = ON_FAILURE_RETRY
    on_missing_data: str | None = None


def step_row(row: sqlite3.Row) -> Step:
    config = json.loads(row["config_json"] or "{}")
    if not isinstance(config, dict):
        config = {}
    return Step(
        id=row["id"],
        campaign_id=row["campaign_id"],
        ord=row["ord"],
        action_type=row["action_type"],
        config=config,
        template_id=row["template_id"],
        bunch_size=row["bunch_size"],
        on_failure=row["on_failure"] or ON_FAILURE_RETRY,
        on_missing_data=row["on_missing_data"],
    )


def _validate_spec(spec: StepSpec, position: int) -> None:
    action_type = (spec.action_type or "").strip()
    if not action_type:
        raise StepDefinitionError(f"step {position} has no action_type")
    if spec.on_failure not in ON_FAILURE_MODES:
        raise StepDefinitionError(
            f"step {position} has on_failure={spec.on_failure!r}; "
            f"expected one of {list(ON_FAILURE_MODES)}"
        )
    if spec.on_missing_data is not None and spec.on_missing_data not in MISSING_DATA_DISPOSITIONS:
        raise StepDefinitionError(
            f"step {position} has on_missing_data={spec.on_missing_data!r}; "
            f"expected one of {sorted(MISSING_DATA_DISPOSITIONS)}"
        )
    if int(spec.bunch_size) < 1:
        raise StepDefinitionError(f"step {position} has bunch_size below 1")
    if action_type == FILTER_ACTION and not str(spec.config.get("filter", "")).strip():
        raise StepDefinitionError(
            f"step {position} is a filter step but names no filter in its config"
        )


def _in_flight_count(conn: sqlite3.Connection, campaign_id: int) -> int:
    placeholders = ", ".join("?" for _ in ACTIVE_SUBLISTS)
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS total
        FROM campaign_leads
        WHERE campaign_id = ? AND sublist IN ({placeholders})
        """,
        (campaign_id, *(sublist.value for sublist in ACTIVE_SUBLISTS)),
    ).fetchone()
    return int(row["total"])


def define_steps(
    conn: sqlite3.Connection,
    campaign_id: int,
    specs: Sequence[StepSpec] | Iterable[StepSpec],
    *,
    replace: bool = False,
) -> list[Step]:
    """Replace a campaign's step list with `specs`, numbered from 1.

    Refused while leads are still in the flow unless `replace=True`, because
    renumbering the ords under a lead would resume it on a different action than
    the one it stopped at.

    The derived queue is rebuilt in the same transaction, so open jobs never
    outlive the steps they pointed at. With `replace=True` that has a visible
    consequence worth knowing about: an in-flight lead keeps its
    `current_step_ord`, so a lead sitting past the end of a now-shorter list is
    closed out as `successful`. There is nothing left for it to do.
    """
    ordered = list(specs)
    if not ordered:
        raise StepDefinitionError("a campaign needs at least one step")
    for position, spec in enumerate(ordered, start=1):
        _validate_spec(spec, position)

    with transaction(conn):
        if not replace:
            in_flight = _in_flight_count(conn, campaign_id)
            if in_flight:
                raise CampaignInFlightError(campaign_id, in_flight)
        conn.execute("DELETE FROM campaign_steps WHERE campaign_id = ?", (campaign_id,))
        for position, spec in enumerate(ordered, start=1):
            conn.execute(
                """
                INSERT INTO campaign_steps
                    (campaign_id, ord, action_type, config_json, template_id, bunch_size,
                     on_failure, on_missing_data)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    campaign_id,
                    position,
                    spec.action_type.strip(),
                    json.dumps(dict(spec.config), sort_keys=True),
                    spec.template_id,
                    int(spec.bunch_size),
                    spec.on_failure,
                    spec.on_missing_data,
                ),
            )
        jobs_module.rebuild_jobs(conn, campaign_id)
    return list_steps(conn, campaign_id)


def add_step(
    conn: sqlite3.Connection,
    campaign_id: int,
    spec: StepSpec,
    *,
    ord_: int | None = None,
) -> Step:
    """Append a step, or insert it at `ord_` and shift the rest down.

    Appending changes nothing about the steps a lead has left to run, so the
    queue and any live lease are left completely alone.

    Inserting in the middle renumbers ords. Every lead at or after the insertion
    point is shifted with them, in the same transaction, so each lead stays
    pointed at the same *action* it was pointed at before and the new step lands
    in front of it. The queue is then refreshed for the leads sitting in `queue`;
    a lead mid-step keeps its lease, because its job still names the same step
    row and interrupting live work to renumber it would be worse than the stale
    `step_ord` in its payload.
    """
    _validate_spec(spec, ord_ or (last_step_ord(conn, campaign_id) or 0) + 1)
    with transaction(conn):
        last = last_step_ord(conn, campaign_id) or 0
        position = last + 1 if ord_ is None else int(ord_)
        if position < 1 or position > last + 1:
            raise StepDefinitionError(
                f"ord {position} is outside 1..{last + 1} for campaign {campaign_id}"
            )
        shifts = position <= last
        if shifts:
            # Walk backwards so the UNIQUE (campaign_id, ord) index never collides.
            for existing in range(last, position - 1, -1):
                conn.execute(
                    "UPDATE campaign_steps SET ord = ? WHERE campaign_id = ? AND ord = ?",
                    (existing + 1, campaign_id, existing),
                )
            active = ", ".join("?" for _ in ACTIVE_SUBLISTS)
            conn.execute(
                f"""
                UPDATE campaign_leads
                SET current_step_ord = current_step_ord + 1
                WHERE campaign_id = ? AND current_step_ord >= ? AND sublist IN ({active})
                """,
                (campaign_id, position, *(sublist.value for sublist in ACTIVE_SUBLISTS)),
            )
        conn.execute(
            """
            INSERT INTO campaign_steps
                (campaign_id, ord, action_type, config_json, template_id, bunch_size,
                 on_failure, on_missing_data)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                campaign_id,
                position,
                spec.action_type.strip(),
                json.dumps(dict(spec.config), sort_keys=True),
                spec.template_id,
                int(spec.bunch_size),
                spec.on_failure,
                spec.on_missing_data,
            ),
        )
        if shifts:
            jobs_module.rebuild_jobs(conn, campaign_id, recover_processing=False)
    return step_at_ord(conn, campaign_id, position)


def list_steps(conn: sqlite3.Connection, campaign_id: int) -> list[Step]:
    """Read a campaign's steps in execution order."""
    rows = conn.execute(
        "SELECT * FROM campaign_steps WHERE campaign_id = ? ORDER BY ord",
        (campaign_id,),
    ).fetchall()
    return [step_row(row) for row in rows]


def get_step(conn: sqlite3.Connection, step_id: int) -> Step | None:
    row = conn.execute("SELECT * FROM campaign_steps WHERE id = ?", (step_id,)).fetchone()
    return None if row is None else step_row(row)


def step_at_ord(conn: sqlite3.Connection, campaign_id: int, ord_: int) -> Step:
    """Read the step at one position, raising when the sequence has ended."""
    row = conn.execute(
        "SELECT * FROM campaign_steps WHERE campaign_id = ? AND ord = ?",
        (campaign_id, ord_),
    ).fetchone()
    if row is None:
        raise StepNotFoundError(campaign_id, ord_)
    return step_row(row)


def find_step_at_ord(conn: sqlite3.Connection, campaign_id: int, ord_: int) -> Step | None:
    """Read the step at one position, or None when there is none."""
    row = conn.execute(
        "SELECT * FROM campaign_steps WHERE campaign_id = ? AND ord = ?",
        (campaign_id, ord_),
    ).fetchone()
    return None if row is None else step_row(row)


def first_step_ord(conn: sqlite3.Connection, campaign_id: int) -> int | None:
    row = conn.execute(
        "SELECT MIN(ord) AS ord FROM campaign_steps WHERE campaign_id = ?",
        (campaign_id,),
    ).fetchone()
    return None if row["ord"] is None else int(row["ord"])


def last_step_ord(conn: sqlite3.Connection, campaign_id: int) -> int | None:
    row = conn.execute(
        "SELECT MAX(ord) AS ord FROM campaign_steps WHERE campaign_id = ?",
        (campaign_id,),
    ).fetchone()
    return None if row["ord"] is None else int(row["ord"])


def next_step_ord(conn: sqlite3.Connection, campaign_id: int, ord_: int) -> int | None:
    """Return the ord after `ord_`, or None when the sequence has ended."""
    row = conn.execute(
        "SELECT MIN(ord) AS ord FROM campaign_steps WHERE campaign_id = ? AND ord > ?",
        (campaign_id, ord_),
    ).fetchone()
    return None if row["ord"] is None else int(row["ord"])
