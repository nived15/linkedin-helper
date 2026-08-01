"""Instrumentation that writes an audit row for every LinkedIn DOM interaction.

The decorator is applied under `@mcp.tool()` so the MCP tool signature and
schema are untouched while every invocation, read or write, lands in
`actions_log`.
"""

from __future__ import annotations

import functools
import inspect
import logging
import os
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from linkedin_mcp.audit.log import (
    DEFAULT_ACCOUNT_LABEL,
    Outcome,
    get_audit_log,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ALREADY_LOGGED_KEY",
    "audit_linkedin_action",
    "current_account_id",
    "default_account_label",
    "outcome_for_result",
    "record_tool_result",
    "reset_account_resolver",
    "set_account_resolver",
]

CONTEXT_PARAM_NAMES = frozenset({"ctx", "context"})
ALREADY_LOGGED_KEY = "audit_logged"
STATUS_TO_OUTCOME: dict[str, Outcome] = {
    "success": Outcome.SUCCESS,
    "error": Outcome.FAILURE,
    "failed": Outcome.FAILURE,
    "failure": Outcome.FAILURE,
    "refused": Outcome.REFUSED,
    "skipped": Outcome.SKIPPED,
}

_account_resolver: Callable[[], int] | None = None


def default_account_label() -> str:
    """Return the label of the account these tools act as."""
    return os.getenv("LINKEDIN_USERNAME", "").strip() or DEFAULT_ACCOUNT_LABEL


def set_account_resolver(resolver: Callable[[], int] | None) -> None:
    """Override how instrumentation resolves the acting account id."""
    global _account_resolver
    _account_resolver = resolver


def reset_account_resolver() -> None:
    """Restore the default account resolution behaviour."""
    set_account_resolver(None)


def current_account_id() -> int:
    """Resolve the account id that owns the current tool invocation."""
    if _account_resolver is not None:
        return _account_resolver()
    return get_audit_log().ensure_account(default_account_label())


def outcome_for_result(result: Any) -> Outcome:
    """Map an MCP tool result onto an audit outcome."""
    if not isinstance(result, Mapping):
        return Outcome.SUCCESS
    status = str(result.get("status", "success")).strip().lower()
    return STATUS_TO_OUTCOME.get(status, Outcome.FAILURE)


def record_tool_result(
    action_type: str,
    result: Any,
    *,
    target: Any = None,
    detail: Mapping[str, Any] | None = None,
    lead_id: int | None = None,
    account_id: int | None = None,
) -> int | None:
    """Append the audit row for one tool result, returning its id or `None`.

    A failed write surfaces as an `audit_error` key on `result` when `result` is
    a mutable dict, so a lost row never passes silently. A result carrying
    `audit_logged: True` is skipped because its owner already appended the row.
    """
    if _already_logged(result):
        return None
    row_id, error = _record(
        action_type,
        outcome_for_result(result),
        _result_detail(result, target=target, extra=detail),
        lead_id=lead_id,
        account_id=account_id,
    )
    if error and isinstance(result, dict):
        result["audit_error"] = error
    return row_id


def audit_linkedin_action(
    action_type: str | Callable[[Mapping[str, Any]], str],
    *,
    target: str | Callable[[Mapping[str, Any]], Any] | None = None,
    capture: Sequence[str] = (),
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Write an `actions_log` row for every call of an async MCP tool.

    A result carrying `audit_logged: True` is skipped, so a `SafetyGate` that
    already called `log_refusal` owns its row and the decorator does not append
    a second one for the same decision.

    Args:
        action_type: Audit action name, or a callable over the bound arguments
            when the name depends on an argument (for example post like vs share).
        target: Bound parameter name, or a callable, identifying what the action
            touched. Stored in `detail_json` as `target`.
        capture: Extra bound parameter names to store in `detail_json`.
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        if not inspect.iscoroutinefunction(func):
            raise TypeError(
                f"audit_linkedin_action only wraps async tools; {func.__name__} is sync"
            )
        signature = inspect.signature(func)

        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            bound = _bind_arguments(signature, args, kwargs)
            resolved_action = _resolve(action_type, bound) or func.__name__
            detail = _call_detail(func.__name__, bound, target=target, capture=capture)
            started = time.monotonic()

            try:
                result = await func(*args, **kwargs)
            except BaseException as exc:
                detail["duration_ms"] = _elapsed_ms(started)
                detail["error"] = f"{type(exc).__name__}: {exc}"
                _record(resolved_action, Outcome.FAILURE, detail)
                raise

            detail["duration_ms"] = _elapsed_ms(started)
            detail.update(_result_summary(result))
            if _already_logged(result):
                return result
            _, error = _record(resolved_action, outcome_for_result(result), detail)
            if error and isinstance(result, dict):
                result["audit_error"] = error
            return result

        return wrapper

    return decorator


def _already_logged(result: Any) -> bool:
    return isinstance(result, Mapping) and bool(result.get(ALREADY_LOGGED_KEY))


def _record(
    action_type: str,
    outcome: Outcome,
    detail: Mapping[str, Any],
    *,
    lead_id: int | None = None,
    account_id: int | None = None,
) -> tuple[int | None, str | None]:
    """Append one row, never letting an audit failure break the caller."""
    try:
        log = get_audit_log()
        resolved_account = (
            account_id if account_id is not None else current_account_id()
        )
        if outcome is Outcome.REFUSED:
            row_id = log.record_refusal(
                resolved_account,
                action_type,
                detail.get("reason"),
                lead_id=lead_id,
                detail=detail,
            )
        else:
            row_id = log.record(
                resolved_account,
                action_type,
                outcome,
                lead_id=lead_id,
                detail=detail,
            )
        return row_id, None
    except Exception as exc:
        message = f"Failed to write audit row for {action_type}: {exc}"
        logger.error(message)
        return None, message


def _bind_arguments(
    signature: inspect.Signature,
    args: tuple[Any, ...],
    kwargs: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        bound = signature.bind_partial(*args, **dict(kwargs))
        bound.apply_defaults()
    except TypeError:
        return dict(kwargs)
    return {
        name: value
        for name, value in bound.arguments.items()
        if name not in CONTEXT_PARAM_NAMES
    }


def _resolve(
    value: str | Callable[[Mapping[str, Any]], Any] | None,
    bound: Mapping[str, Any],
) -> Any:
    if value is None:
        return None
    if callable(value):
        return value(bound)
    return value


def _call_detail(
    tool_name: str,
    bound: Mapping[str, Any],
    *,
    target: str | Callable[[Mapping[str, Any]], Any] | None,
    capture: Sequence[str],
) -> dict[str, Any]:
    detail: dict[str, Any] = {"tool": tool_name}
    if target is not None:
        resolved_target = (
            target(bound) if callable(target) else bound.get(target)
        )
        if resolved_target is not None:
            detail["target"] = resolved_target
    for name in capture:
        if name in bound and bound[name] is not None:
            detail[name] = bound[name]
    return detail


def _result_summary(result: Any) -> dict[str, Any]:
    if not isinstance(result, Mapping):
        return {}
    summary: dict[str, Any] = {}
    for key in ("status", "message", "count", "performed", "reason"):
        if key in result and result[key] is not None:
            summary[key] = result[key]
    return summary


def _result_detail(
    result: Any,
    *,
    target: Any,
    extra: Mapping[str, Any] | None,
) -> dict[str, Any]:
    detail: dict[str, Any] = {}
    if target is not None:
        detail["target"] = target
    detail.update(_result_summary(result))
    if extra:
        detail.update(extra)
    return detail


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
