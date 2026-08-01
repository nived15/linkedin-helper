"""Append-only audit trail for LinkedIn actions and safety refusals."""

from linkedin_mcp.audit.instrument import (
    audit_linkedin_action,
    current_account_id,
    default_account_label,
    outcome_for_result,
    record_tool_result,
    reset_account_resolver,
    set_account_resolver,
)
from linkedin_mcp.audit.log import (
    ATTEMPTED_OUTCOMES,
    AUDIT_TABLE,
    ROLLING_WINDOW_INDEX,
    AuditLog,
    Outcome,
    RefusalReason,
    count_actions_in_window,
    get_audit_log,
    log_action,
    log_refusal,
    reset_audit_log,
    set_audit_log,
    utc_timestamp,
)

__all__ = [
    "ATTEMPTED_OUTCOMES",
    "AUDIT_TABLE",
    "AuditLog",
    "Outcome",
    "ROLLING_WINDOW_INDEX",
    "RefusalReason",
    "audit_linkedin_action",
    "count_actions_in_window",
    "current_account_id",
    "default_account_label",
    "get_audit_log",
    "log_action",
    "log_refusal",
    "outcome_for_result",
    "record_tool_result",
    "reset_account_resolver",
    "reset_audit_log",
    "set_account_resolver",
    "set_audit_log",
    "utc_timestamp",
]
