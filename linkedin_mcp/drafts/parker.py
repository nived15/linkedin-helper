"""The adapter that plugs this package into SEQ-04's runner.

SEQ-04 (#47, merged) defined its LLM seam as an injected
``Callable[[DraftRequest], int | None]`` with a no-op default, and wrote down
what #23 has to satisfy: park a `needs_generation` row, return its id, never
call a model synchronously, and be **idempotent for a lead and step**, because
the runner re-refuses that step every hour until the draft is approved. A parker
that inserted unconditionally would write a row an hour, forever.

:func:`draft_parker` is that callable. The idempotency requirement is not
special-cased here, it is simply
:func:`~linkedin_mcp.drafts.routing.ensure_draft`, which already reuses an open
draft for the same lead, step, kind and fragment. The two halves were designed
against the same contract from opposite sides and they meet without a shim doing
any real work, which is the outcome you want from a seam.

Why the request is duck-typed
-----------------------------
Nothing here imports :mod:`linkedin_mcp.worker`. The worker package deliberately
imports nothing from this one, and `worker.py` will import *this* module to wire
the parker up, so a runtime import in this direction would set up a cycle the
first time somebody moved that wiring into `linkedin_mcp/worker/__init__.py`.
The request is a plain data carrier; reading its attributes costs nothing and
keeps the dependency arrow pointing one way. The `TYPE_CHECKING` import gives
editors and type checkers the real class.

Wiring it up, in `worker.py`::

    from linkedin_mcp.drafts import draft_parker

    worker = build_worker(conn, account_id, draft_parker=draft_parker)

That line is still outstanding: this module supplies the callable, and the
worker entry point chooses to use it.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from linkedin_mcp.drafts.routing import ensure_draft
from linkedin_mcp.drafts.store import park_draft, validate_kind
from linkedin_mcp.sequences.steps import get_step

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from linkedin_mcp.worker.actions import DraftRequest


logger = logging.getLogger(__name__)

__all__ = [
    "draft_parker",
    "make_draft_parker",
]


def _kind_of(request: Any) -> str:
    """Return the `ai_drafts.kind` string a request names.

    SEQ-04's `DraftKind` is a `str` Enum over the same four values as
    :data:`~linkedin_mcp.drafts.store.DRAFT_KINDS`, so this accepts the enum
    member or a bare string and validates either against the schema's own
    CHECK constraint.
    """
    kind = getattr(request.kind, "value", request.kind)
    return validate_kind(str(kind))


def make_draft_parker(
    *,
    model: str | None = None,
) -> Callable[[DraftRequest], int | None]:
    """Build a parker, optionally stamping every row with a model name.

    `model` records which model is *expected* to fill the draft. It is metadata
    on a row that has not been generated yet, not a client and not a call.
    """

    def parker(request: DraftRequest) -> int | None:
        return _park(request, model=model)

    return parker


def draft_parker(request: DraftRequest) -> int | None:
    """Park one draft for the runner and return its id.

    The default parker. Writes a `needs_generation` row and returns; the runner
    then refuses the step with `APPROVAL_REQUIRED` and moves to the next lead.
    Nothing here waits, retries or reaches the network.

    Returns `None` only when the request carries no account to own the row,
    which the runner reads as "nothing was parked" and logs.
    """
    return _park(request, model=None)


def _park(request: DraftRequest, *, model: str | None) -> int | None:
    conn: sqlite3.Connection = request.conn
    account_id: int | None = getattr(request, "account_id", None)
    campaign_id: int | None = getattr(request, "campaign_id", None)
    lead_id: int | None = getattr(request, "lead_id", None)
    step_id: int | None = getattr(request, "step_id", None)
    context: Mapping[str, Any] = getattr(request, "context", None) or {}
    now = getattr(request, "now", None)
    kind = _kind_of(request)

    if campaign_id is not None and lead_id is not None:
        # The idempotent path, and the one the runner almost always takes. A
        # step refused every hour for a week leaves one row, not 168.
        step = get_step(conn, step_id) if step_id is not None else None
        gate = ensure_draft(
            conn,
            campaign_id,
            lead_id,
            kind,
            step=step,
            extras=context,
            model=model,
            now=now,
        )
        return gate.draft.id

    if account_id is None:
        # An ad hoc job with no campaign and no account has nothing to own the
        # row. Saying so beats inventing an owner.
        logger.warning("a draft request named neither a campaign nor an account")
        return None

    # An ad hoc job: no campaign, so no step to be idempotent against and no
    # campaign `approval_mode` to consult. It parks against the account and
    # waits for a human like everything else.
    return park_draft(
        conn,
        account_id=account_id,
        kind=kind,
        campaign_id=campaign_id,
        lead_id=lead_id,
        step_id=step_id,
        context=dict(context),
        model=model,
        now=now,
    ).id
