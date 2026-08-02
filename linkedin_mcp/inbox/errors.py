"""Errors raised by the inbox scanner."""

from __future__ import annotations

__all__ = [
    "InboxError",
    "PollIntervalTooShortError",
]


class InboxError(RuntimeError):
    """Base class for every error this package raises."""


class PollIntervalTooShortError(InboxError, ValueError):
    """A caller asked to poll the inbox faster than the floor allows.

    Raised only when a caller opts into strict handling. The default is to clamp
    to the floor and carry on, because a runner that crashes on a bad config
    value stops scanning entirely, which is worse than scanning more slowly than
    it asked for.
    """

    def __init__(self, requested: int, minimum: int) -> None:
        self.requested = int(requested)
        self.minimum = int(minimum)
        super().__init__(
            f"a poll interval of {self.requested}s is below the {self.minimum}s "
            "floor; polling the LinkedIn inbox faster than that is the behaviour "
            "that gets an account flagged"
        )
