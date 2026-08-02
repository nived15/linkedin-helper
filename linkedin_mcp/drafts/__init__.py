"""SEQ-05: the AI drafts queue and the ICP qualification gate.

The inversion in one paragraph
------------------------------
A campaign worker running unattended at 2am never calls a language model. It
writes a row into `ai_drafts` carrying everything a model would need about the
lead, then moves to the next lead. Later, an MCP client, which *is* a language
model, lists the open rows, generates text, and submits it. A human approves it.
Only then can anything be sent. That inversion is what lets a passive worker use
an LLM without ever depending on one being connected, and every design decision
in this package protects it. If an LLM being absent can stall a campaign, this
package is broken.

Four kinds, exactly the ones the schema allows
----------------------------------------------
`connection_note`, `message` and `comment` produce free text. `icp_evaluation`
produces `{match, score, reason}` in `verdict_json` and routes the lead to
`successful` or `failed`. No migration was needed: `0001_init.sql` already had
the kinds, the statuses and the verdict column.

The safety rule
---------------
AI-generated free text never reaches a send path without approval, and there are
two independent locks rather than one:

* :func:`~linkedin_mcp.drafts.store.approved_text` and
  :func:`~linkedin_mcp.drafts.store.mark_sent` both refuse any status but
  `approved`.
* :func:`~linkedin_mcp.drafts.fragments.fragment_source` only ever yields
  approved text, so a hybrid template whose fragment is still `pending_approval`
  refuses the whole render with SEQ-02's `MISSING_AI_FRAGMENT`. The unapproved
  text is not withheld at the send step; the renderer never sees it.

Auto-approve is `campaigns.approval_mode = 'auto'`, the flag that already
existed. It is per campaign and opt-in, and a campaign left at the default
`manual_drafts` can never auto-send.

Voice
-----
Every piece of generated text goes through SEQ-02's
:func:`~linkedin_mcp.templating.style.style_violations`, the same validator that
binds template bodies and spintax branches. There is no second style checker
here, on purpose: two would drift and the weaker one would win.

ICP filtering costs zero LinkedIn actions
-----------------------------------------
An ICP gate is a `filter` step, which is already in
:data:`~linkedin_mcp.sequences.steps.LOCAL_ACTIONS`, so a worker must not spend a
safety-gate lease on it. The three MCP tools write unmetered `actions_log` rows.
Qualifying a lead therefore costs nothing from the account's daily budget, which
is precisely why the gate belongs before the invite step rather than after it.

Seams other issues plug into
----------------------------
* **SEQ-04 (#22), the runner.** Call
  :func:`~linkedin_mcp.drafts.routing.ensure_draft`; if the returned gate is not
  `ready`, call :func:`~linkedin_mcp.drafts.routing.defer_for_approval` and move
  on. That is the whole integration. Nothing here blocks, sleeps or retries.
* **MCP-02 (#25) / MCP-03 (#26), the tool surface.** Call
  :func:`~linkedin_mcp.drafts.tools.register_draft_tools` with the server's
  FastMCP instance. That one line is the only wiring outstanding.
"""

from linkedin_mcp.drafts.context import (
    ICP_CONFIG_KEY,
    draft_context,
    icp_criteria,
    lead_summary,
    style_brief,
    template_brief,
)
from linkedin_mcp.drafts.errors import (
    DraftError,
    DraftNotApprovedError,
    DraftNotFoundError,
    DraftStateError,
    DraftStyleError,
    MalformedVerdictError,
    UnknownDraftKindError,
)
from linkedin_mcp.drafts.fragments import (
    approved_fragments,
    fragment_source,
    pending_fragments,
)
from linkedin_mcp.drafts.routing import (
    ICP_ACTION,
    ICP_FILTER_NAME,
    ICP_MATCH_OUTCOME,
    ICP_NO_MATCH_OUTCOME,
    DraftGate,
    IcpRouting,
    defer_for_approval,
    ensure_draft,
    ensure_fragment_drafts,
    icp_gate_step,
    latest_verdict,
    register_icp_filter,
    request_draft,
    route_icp_verdict,
)
from linkedin_mcp.drafts.session import (
    get_draft_connection,
    reset_draft_connection,
    set_draft_connection,
)
from linkedin_mcp.drafts.store import (
    AUTO_APPROVAL_MODE,
    DRAFT_KINDS,
    DRAFT_STATUSES,
    MAX_TEXT_LENGTH,
    OPEN_STATUSES,
    STATUS_APPROVED,
    STATUS_NEEDS_GENERATION,
    STATUS_PENDING_APPROVAL,
    STATUS_REJECTED,
    STATUS_SENT,
    TEXT_KINDS,
    VERDICT_KINDS,
    Draft,
    approve_draft,
    approved_text,
    auto_approves,
    count_drafts,
    draft_from_row,
    get_draft,
    list_drafts,
    list_pending,
    mark_sent,
    open_draft_for,
    park_draft,
    require_draft,
    submit_draft,
    validate_kind,
    validate_text,
)
from linkedin_mcp.drafts.tools import (
    DRAFT_ACTION_TYPES,
    DRAFT_APPROVE_ACTION,
    DRAFT_LIST_ACTION,
    DRAFT_SUBMIT_ACTION,
    register_draft_tools,
)
from linkedin_mcp.drafts.verdict import (
    VERDICT_KEYS,
    Verdict,
    coerce_match,
    encode_verdict,
    parse_verdict,
)


__all__ = [
    "AUTO_APPROVAL_MODE",
    "DRAFT_ACTION_TYPES",
    "DRAFT_APPROVE_ACTION",
    "DRAFT_KINDS",
    "DRAFT_LIST_ACTION",
    "DRAFT_STATUSES",
    "DRAFT_SUBMIT_ACTION",
    "ICP_ACTION",
    "ICP_CONFIG_KEY",
    "ICP_FILTER_NAME",
    "ICP_MATCH_OUTCOME",
    "ICP_NO_MATCH_OUTCOME",
    "MAX_TEXT_LENGTH",
    "OPEN_STATUSES",
    "STATUS_APPROVED",
    "STATUS_NEEDS_GENERATION",
    "STATUS_PENDING_APPROVAL",
    "STATUS_REJECTED",
    "STATUS_SENT",
    "TEXT_KINDS",
    "VERDICT_KEYS",
    "VERDICT_KINDS",
    "Draft",
    "DraftError",
    "DraftGate",
    "DraftNotApprovedError",
    "DraftNotFoundError",
    "DraftStateError",
    "DraftStyleError",
    "IcpRouting",
    "MalformedVerdictError",
    "UnknownDraftKindError",
    "Verdict",
    "approve_draft",
    "approved_fragments",
    "approved_text",
    "auto_approves",
    "coerce_match",
    "count_drafts",
    "defer_for_approval",
    "draft_context",
    "draft_from_row",
    "encode_verdict",
    "ensure_draft",
    "ensure_fragment_drafts",
    "fragment_source",
    "get_draft",
    "get_draft_connection",
    "icp_criteria",
    "icp_gate_step",
    "latest_verdict",
    "lead_summary",
    "list_drafts",
    "list_pending",
    "mark_sent",
    "open_draft_for",
    "park_draft",
    "parse_verdict",
    "pending_fragments",
    "register_draft_tools",
    "register_icp_filter",
    "request_draft",
    "require_draft",
    "reset_draft_connection",
    "route_icp_verdict",
    "set_draft_connection",
    "style_brief",
    "submit_draft",
    "template_brief",
    "validate_kind",
    "validate_text",
]
