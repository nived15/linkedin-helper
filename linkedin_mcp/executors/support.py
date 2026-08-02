"""The page-driving helpers the MCP server used to keep to itself.

These moved here rather than staying in `linkedin_browser_mcp.py` for one
structural reason: `tests/test_worker_support.py` forbids the worker from
importing the MCP server, and rightly so, because the dependency runs one way.
The executors need the same selector fallbacks, the same detection check and the
same pacing the tools used, so the helpers have to live somewhere both sides may
import. Copying them instead would have produced two detection checks, and a
second detection check that raises without recording anything is the exact bug
`linkedin_mcp.browser.navigate.assert_session_alive` was written to make
impossible.

Nothing here decides whether an action may happen. That is `SafetyGate`'s, and
by the time an executor runs the gate has already said yes.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from linkedin_mcp.audit import current_account_id
from linkedin_mcp.browser.humanize import dwell_and_click, type_text
from linkedin_mcp.browser.navigate import SessionExpiredError
from linkedin_mcp.browser.selectors import selector_fallbacks
from linkedin_mcp.safety import DetectionHalt, assert_page_clear

logger = logging.getLogger(__name__)

__all__ = [
    "acting_account_id",
    "check_page_is_ours",
    "click_selector_fallback",
    "fill_selector_fallback",
    "query_selector_fallback",
    "session_expired_result",
    "wait_for_selector_fallback",
]


def acting_account_id() -> int | None:
    """Resolve the account these helpers act as, or None when the log is down.

    A caller that cannot name its account still has to halt on a challenge. It
    just cannot write the halt down, and saying so beats guessing an id and
    stopping a session nobody challenged.
    """
    try:
        return current_account_id()
    except Exception as exc:  # noqa: BLE001 - the audit log may be unavailable
        logger.error(f"Could not resolve the acting account: {exc}")
        return None


async def check_page_is_ours(page: Any, action_type: str | None = None):
    """Return a result when LinkedIn served an interstitial, else None.

    This runs after every navigation. `assert_page_clear` flips the account out
    of `active` and writes the `safety_events` row before it raises, so
    returning the halt as an ordinary result is not swallowing it:
    `guard_action` refuses every later action on its own once the state has
    moved. There is deliberately no second copy of the detection rules here.
    """
    try:
        await assert_page_clear(
            page,
            account_id=acting_account_id(),
            action_type=action_type,
        )
    except DetectionHalt as halt:
        logger.error(f"Halting {action_type or 'navigation'}: {halt}")
        return halt.to_result()
    return None


def session_expired_result(error: SessionExpiredError) -> dict:
    """Turn a halted profile navigation into a result, detail intact."""
    if error.halt is not None:
        return error.halt.to_result()
    return {"status": "error", "message": str(error)}


async def wait_for_selector_fallback(page: Any, name: str, timeout: int = 10000):
    """Wait for the first matching selector in the configured fallback order."""
    fallbacks = selector_fallbacks(name)
    deadline = time.monotonic() + (timeout / 1000)
    last_error = None
    for fallback in fallbacks:
        remaining_ms = int((deadline - time.monotonic()) * 1000)
        if remaining_ms <= 0:
            break
        try:
            return await page.wait_for_selector(fallback, timeout=max(1, remaining_ms))
        except Exception as exc:  # noqa: BLE001 - try the next fallback
            last_error = exc
    if last_error:
        raise last_error
    raise ValueError(f"No selector fallbacks configured for {name}")


async def query_selector_fallback(page: Any, name: str):
    """Query for the first matching selector in the configured fallback order."""
    for fallback in selector_fallbacks(name):
        handle = await page.query_selector(fallback)
        if handle:
            return handle
    return None


async def click_selector_fallback(page: Any, name: str, timeout: int = 10000):
    """Click the first matching selector in the configured fallback order."""
    handle = await wait_for_selector_fallback(page, name, timeout=timeout)
    await dwell_and_click(handle)
    return handle


async def fill_selector_fallback(page: Any, name: str, value: str, timeout: int = 10000):
    """Fill the first matching selector in the configured fallback order."""
    handle = await wait_for_selector_fallback(page, name, timeout=timeout)
    await type_text(handle, value, clear=True)
    return handle
