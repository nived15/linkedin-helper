"""The seams between the tick loop and everything it must not depend on.

The runner decides *when* something happens and *whether* it is allowed to. It
never decides *how*, because "how" is a Playwright call, a template render or an
LLM, and a scheduler that imports any of those cannot be tested without them.
Everything on the far side of that line arrives here as an injected callable.

Three seams
-----------
**Executors** do the work. One coroutine per `action_type`, registered on an
:class:`ActionRegistry`. The registry is deliberately empty by default: a worker
with nothing registered is a valid, testable worker, and the campaign it runs
still advances through its local steps and parks the rest cleanly rather than
spinning. Playwright therefore lives entirely in whatever registers an executor,
which in production is `worker.py` at the repo root.

**The browser** is supplied lazily. The runner holds a
:data:`BrowserSupplier`, calls it at most once per process, and hands the result
to executors through :attr:`ActionContext.browser`. A worker whose steps are all
local never calls it, so no browser is launched to run a filter.

**Drafts** are parked, never generated. SEQ-05 (#23) owns `ai_drafts` and is not
merged, so nothing here imports it. When a step needs text a human or a model has
not produced yet, the runner calls a :data:`DraftParker` with a
:class:`DraftRequest` and then refuses the step with `APPROVAL_REQUIRED`, which
SEQ-01 maps to "wait an hour and try again". The worker never blocks on the
draft, never calls a model, and does not care whether one exists.

What #23 has to satisfy
-----------------------
A parker is ``Callable[[DraftRequest], int | None]`` returning the `ai_drafts`
row id it wrote, or None when it wrote nothing. It must be **idempotent for a
lead and step**: the runner re-refuses that step every hour until the draft is
approved, so a parker that inserts unconditionally would write a row an hour
forever. It must not call a model synchronously; parking means writing a
`needs_generation` row and returning. Once the draft is approved, #23 makes the
gate see the approval, and the next retry of that step runs normally.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from linkedin_mcp.sequences import Campaign, Job, Step

__all__ = [
    "ActionContext",
    "ActionRegistry",
    "ActionResult",
    "ActionStatus",
    "BrowserSupplier",
    "DraftKind",
    "DraftParker",
    "DraftRequest",
    "Executor",
    "no_browser",
    "no_draft_parker",
]


class ActionStatus(str, Enum):
    """How an executor finished, in the vocabulary the runner acts on."""

    OK = "ok"
    FAILED = "failed"
    NEEDS_DRAFT = "needs_draft"
    SKIPPED = "skipped"


class DraftKind(str, Enum):
    """The `ai_drafts.kind` values the schema accepts."""

    CONNECTION_NOTE = "connection_note"
    MESSAGE = "message"
    COMMENT = "comment"
    ICP_EVALUATION = "icp_evaluation"


@dataclass(frozen=True, slots=True)
class ActionContext:
    """Everything an executor is handed. Read-mostly by convention.

    `conn` is the worker's own connection and is **not** inside a transaction
    when an executor runs. Holding a write lock open across a browser call would
    block every other writer for as long as LinkedIn takes to answer, so the
    runner deliberately commits its claim before executing and opens a fresh
    transaction to record the outcome.
    """

    conn: sqlite3.Connection
    account_id: int
    worker_id: str
    job: Job
    now: datetime
    campaign: Campaign | None = None
    step: Step | None = None
    lead_id: int | None = None
    browser: Any | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)

    @property
    def action_type(self) -> str:
        return self.job.action_type

    @property
    def config(self) -> Mapping[str, Any]:
        return {} if self.step is None else self.step.config


@dataclass(frozen=True, slots=True)
class ActionResult:
    """What an executor reports back.

    An executor may also just raise. The runner catches the exception and treats
    it as :meth:`failed`, so an executor is never obliged to handle its own
    errors defensively; it is obliged not to swallow them.
    """

    status: ActionStatus
    detail: Mapping[str, Any] = field(default_factory=dict)
    error: str | None = None
    draft_kind: DraftKind | None = None
    outcome: str | None = None

    @classmethod
    def ok(cls, **detail: Any) -> ActionResult:
        return cls(ActionStatus.OK, detail=detail)

    @classmethod
    def failed(cls, error: str, **detail: Any) -> ActionResult:
        return cls(ActionStatus.FAILED, detail=detail, error=error)

    @classmethod
    def needs_draft(
        cls,
        kind: DraftKind | str = DraftKind.MESSAGE,
        **detail: Any,
    ) -> ActionResult:
        """Report that this step cannot run until a draft exists.

        The runner parks the draft and re-queues the lead. Nothing here waits.
        """
        return cls(
            ActionStatus.NEEDS_DRAFT,
            detail=detail,
            draft_kind=kind if isinstance(kind, DraftKind) else DraftKind(kind),
        )

    @classmethod
    def skipped(cls, reason: str, **detail: Any) -> ActionResult:
        """Report that the lead should leave the flow softly, not as a failure."""
        return cls(ActionStatus.SKIPPED, detail=detail, outcome=reason)

    @property
    def succeeded(self) -> bool:
        return self.status is ActionStatus.OK


Executor = Callable[[ActionContext], Awaitable[ActionResult]]
"""One coroutine per action type. Given a context, does the thing."""

BrowserSupplier = Callable[[], Awaitable[Any]]
"""Builds the browser the executors share. Called at most once per worker."""


@dataclass(frozen=True, slots=True)
class DraftRequest:
    """What SEQ-05 (#23) needs in order to write an `ai_drafts` row."""

    conn: sqlite3.Connection
    account_id: int
    kind: DraftKind
    campaign_id: int | None = None
    lead_id: int | None = None
    step_id: int | None = None
    context: Mapping[str, Any] = field(default_factory=dict)
    now: datetime | None = None


DraftParker = Callable[[DraftRequest], Any]
"""Parks a draft and returns immediately. Never generates anything."""


def no_draft_parker(request: DraftRequest) -> None:
    """The default parker: record nothing, and say so.

    A worker with no drafts package still refuses the step and re-queues the
    lead, so the campaign parks rather than sending unapproved content. That is
    the behaviour "runs unattended with no LLM connected" actually requires.
    """
    return None


async def no_browser() -> None:
    """The default browser supplier: there is no browser.

    Executors that need one receive None and should fail loudly. A worker that
    quietly pretended to send an invitation would be far worse than one that
    reported it could not.
    """
    return None


class ActionRegistry:
    """Maps an `action_type` to the coroutine that performs it.

    Per worker rather than process-wide. Two workers on two accounts can then run
    different capabilities in one process, and a test can register a fake without
    reaching into global state that another test also owns.
    """

    def __init__(self, executors: Mapping[str, Executor] | None = None) -> None:
        self._executors: dict[str, Executor] = dict(executors or {})

    def register(
        self,
        action_type: str,
        executor: Executor,
        *,
        replace: bool = False,
    ) -> None:
        key = (action_type or "").strip()
        if not key:
            raise ValueError("an action type is required")
        if key in self._executors and not replace:
            raise ValueError(f"an executor for {key!r} is already registered")
        self._executors[key] = executor

    def unregister(self, action_type: str) -> bool:
        return self._executors.pop(action_type, None) is not None

    def get(self, action_type: str) -> Executor | None:
        return self._executors.get(action_type)

    def handles(self, action_type: str) -> bool:
        return action_type in self._executors

    def registered(self) -> tuple[str, ...]:
        return tuple(sorted(self._executors))

    def __len__(self) -> int:
        return len(self._executors)
