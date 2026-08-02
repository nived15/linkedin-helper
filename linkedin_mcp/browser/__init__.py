"""Browser helpers for LinkedIn MCP."""

from .humanize import (
    FAST,
    SAFE,
    DelayRange,
    Humanizer,
    PacingProfile,
    TypingMode,
    cooldown,
    dwell_and_click,
    get_humanizer,
    pace,
    scroll_page,
    set_humanizer,
    settle,
    type_text,
)
from .navigate import (
    NavigationError,
    NavigationResult,
    SessionExpiredError,
    assert_session_alive,
    goto_profile,
    profile_slug,
    slug_to_query,
)
from .selectors import SELECTORS, selector_fallbacks, selector_union
from .session import BrowserSession, load_cookies, save_cookies

__all__ = [
    "BrowserSession",
    "DelayRange",
    "FAST",
    "Humanizer",
    "NavigationError",
    "NavigationResult",
    "PacingProfile",
    "SAFE",
    "SELECTORS",
    "SessionExpiredError",
    "TypingMode",
    "assert_session_alive",
    "cooldown",
    "dwell_and_click",
    "get_humanizer",
    "goto_profile",
    "load_cookies",
    "pace",
    "profile_slug",
    "save_cookies",
    "scroll_page",
    "selector_fallbacks",
    "selector_union",
    "set_humanizer",
    "settle",
    "slug_to_query",
    "type_text",
]
