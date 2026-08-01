"""Human-like pacing for browser automation.

Every delay in this codebase routes through this module. Callers never sleep
directly, so pacing stays observable, tunable and deterministic under test.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

__all__ = [
    "FAST",
    "PACING_ENV_VAR",
    "SAFE",
    "DelayRange",
    "Humanizer",
    "PacingProfile",
    "TypingMode",
    "cooldown",
    "dwell_and_click",
    "get_humanizer",
    "pace",
    "profile_from_env",
    "scroll_page",
    "set_humanizer",
    "settle",
    "type_text",
]

PACING_ENV_VAR = "LINKEDIN_PACING"
PUNCTUATION_CHARS = frozenset(".!?,;:\n")

SleepFn = Callable[[float], Awaitable[Any]]
EmitFn = Callable[[str], Awaitable[Any]]


@dataclass(frozen=True)
class DelayRange:
    """Inclusive delay window in seconds."""

    minimum: float
    maximum: float

    def __post_init__(self) -> None:
        if self.minimum < 0:
            raise ValueError(f"DelayRange minimum must be >= 0, got {self.minimum}")
        if self.maximum < self.minimum:
            raise ValueError(
                f"DelayRange maximum must be >= minimum, got {self.minimum}..{self.maximum}"
            )

    def sample(self, rng: random.Random) -> float:
        """Return one uniformly sampled delay from this window."""
        return rng.uniform(self.minimum, self.maximum)

    def scaled(self, factor: float) -> "DelayRange":
        """Return the same window stretched or compressed by a factor."""
        if factor < 0:
            raise ValueError(f"DelayRange scale factor must be >= 0, got {factor}")
        return DelayRange(self.minimum * factor, self.maximum * factor)


class TypingMode(str, Enum):
    """Text entry modes mirroring the Linked Helper benchmark."""

    TYPE = "type"
    PASTE = "paste"
    RANDOM = "random"


@dataclass(frozen=True)
class PacingProfile:
    """Delay windows for one pacing preset."""

    name: str
    typing_mode: TypingMode
    micro_step: DelayRange
    dwell_before_click: DelayRange
    page_settle: DelayRange
    keystroke: DelayRange
    word_pause: DelayRange
    word_pause_chance: float
    punctuation_pause: DelayRange
    thinking_pause: DelayRange
    thinking_pause_chance: float
    paste_compose: DelayRange
    paste_settle: DelayRange
    scroll_steps: tuple[int, int]
    scroll_step_spread: tuple[float, float]
    scroll_pause: DelayRange
    scroll_backtrack_chance: float
    between_actions: DelayRange
    wait_scale: float
    wait_jitter: float

    def __post_init__(self) -> None:
        for chance in (
            self.word_pause_chance,
            self.thinking_pause_chance,
            self.scroll_backtrack_chance,
        ):
            if not 0.0 <= chance <= 1.0:
                raise ValueError(f"Probability must be within 0..1, got {chance}")
        low, high = self.scroll_steps
        if low < 1 or high < low:
            raise ValueError(f"scroll_steps must be an ascending range >= 1, got {self.scroll_steps}")
        spread_low, spread_high = self.scroll_step_spread
        if spread_low <= 0 or spread_high < spread_low:
            raise ValueError(f"scroll_step_spread must be an ascending positive range, got {self.scroll_step_spread}")
        if self.wait_scale <= 0:
            raise ValueError(f"wait_scale must be > 0, got {self.wait_scale}")
        if not 0.0 <= self.wait_jitter < 1.0:
            raise ValueError(f"wait_jitter must be within 0..1, got {self.wait_jitter}")


SAFE = PacingProfile(
    name="SAFE",
    typing_mode=TypingMode.TYPE,
    micro_step=DelayRange(0.4, 1.2),
    dwell_before_click=DelayRange(0.6, 1.8),
    page_settle=DelayRange(2.0, 4.5),
    keystroke=DelayRange(0.07, 0.21),
    word_pause=DelayRange(0.15, 0.45),
    word_pause_chance=0.25,
    punctuation_pause=DelayRange(0.2, 0.6),
    thinking_pause=DelayRange(0.6, 1.8),
    thinking_pause_chance=0.06,
    paste_compose=DelayRange(0.8, 2.2),
    paste_settle=DelayRange(0.4, 1.1),
    scroll_steps=(3, 6),
    scroll_step_spread=(0.7, 1.3),
    scroll_pause=DelayRange(0.4, 1.1),
    scroll_backtrack_chance=0.15,
    between_actions=DelayRange(10.0, 60.0),
    wait_scale=1.0,
    wait_jitter=0.35,
)

FAST = PacingProfile(
    name="FAST",
    typing_mode=TypingMode.RANDOM,
    micro_step=DelayRange(0.12, 0.4),
    dwell_before_click=DelayRange(0.15, 0.5),
    page_settle=DelayRange(0.8, 1.8),
    keystroke=DelayRange(0.02, 0.07),
    word_pause=DelayRange(0.05, 0.15),
    word_pause_chance=0.15,
    punctuation_pause=DelayRange(0.06, 0.2),
    thinking_pause=DelayRange(0.2, 0.6),
    thinking_pause_chance=0.03,
    paste_compose=DelayRange(0.2, 0.6),
    paste_settle=DelayRange(0.1, 0.35),
    scroll_steps=(2, 4),
    scroll_step_spread=(0.7, 1.3),
    scroll_pause=DelayRange(0.15, 0.4),
    scroll_backtrack_chance=0.05,
    between_actions=DelayRange(4.0, 12.0),
    wait_scale=0.5,
    wait_jitter=0.25,
)

PRESETS: dict[str, PacingProfile] = {"fast": FAST, "safe": SAFE}


class Humanizer:
    """Owns every delay an automated LinkedIn action takes."""

    def __init__(
        self,
        profile: PacingProfile = SAFE,
        rng: random.Random | None = None,
        seed: int | None = None,
        sleep: SleepFn | None = None,
    ):
        if rng is not None and seed is not None:
            raise ValueError("Pass either rng or seed, not both")
        self.profile = profile
        self.rng = rng if rng is not None else random.Random(seed)
        self._sleep = sleep if sleep is not None else asyncio.sleep
        self.elapsed = 0.0

    async def sleep(self, seconds: float) -> float:
        """Sleep for an exact duration through the injected sleep function."""
        delay = max(0.0, float(seconds))
        self.elapsed += delay
        await self._sleep(delay)
        return delay

    async def pause(self, window: DelayRange) -> float:
        """Sleep for one sample of an arbitrary delay window."""
        return await self.sleep(window.sample(self.rng))

    async def micro_pause(self) -> float:
        """Randomised micro-delay between two sub-steps of the same action."""
        return await self.pause(self.profile.micro_step)

    async def dwell(self) -> float:
        """Dwell time a human spends on a control before clicking it."""
        return await self.pause(self.profile.dwell_before_click)

    async def settle(self) -> float:
        """Wait for a freshly loaded page to settle before reading it."""
        return await self.pause(self.profile.page_settle)

    async def cooldown(self) -> float:
        """Delay between two consecutive LinkedIn actions."""
        return await self.pause(self.profile.between_actions)

    async def linger(self, seconds: float, jitter: float | None = None) -> float:
        """Replace a fixed timeout with a jittered, preset-scaled wait."""
        if seconds < 0:
            raise ValueError(f"linger seconds must be >= 0, got {seconds}")
        spread = self.profile.wait_jitter if jitter is None else jitter
        if not 0.0 <= spread < 1.0:
            raise ValueError(f"linger jitter must be within 0..1, got {spread}")
        scaled = seconds * self.profile.wait_scale
        return await self.sleep(self.rng.uniform(scaled * (1.0 - spread), scaled * (1.0 + spread)))

    async def click(self, target: Any, dwell: bool = True) -> None:
        """Dwell on a control, click it, then take a micro-delay."""
        if dwell:
            await self.dwell()
        await target.click()
        await self.micro_pause()

    def resolve_typing_mode(self, mode: TypingMode | str | None = None) -> TypingMode:
        """Resolve the effective typing mode, collapsing RANDOM to a concrete choice."""
        requested = TypingMode(mode) if mode is not None else self.profile.typing_mode
        if requested is TypingMode.RANDOM:
            return self.rng.choice((TypingMode.TYPE, TypingMode.PASTE))
        return requested

    async def type_text(
        self,
        target: Any,
        text: str,
        mode: TypingMode | str | None = None,
        clear: bool = False,
    ) -> TypingMode:
        """Enter text into a field using the resolved typing mode."""
        resolved = self.resolve_typing_mode(mode)
        await self.dwell()
        if clear:
            await self._clear(target)
        if resolved is TypingMode.PASTE:
            await self._paste(target, text)
        else:
            await self._keystrokes(target, text)
        await self.micro_pause()
        return resolved

    async def scroll(self, page: Any, distance: int = 800, steps: int | None = None) -> int:
        """Scroll a page in uneven steps with pauses, the way a reader does."""
        total = abs(int(distance))
        if total == 0:
            return 0

        direction = 1 if distance > 0 else -1
        chunk_count = self.rng.randint(*self.profile.scroll_steps) if steps is None else int(steps)
        if chunk_count < 1:
            raise ValueError(f"scroll steps must be >= 1, got {steps}")
        chunk_count = min(chunk_count, total)

        scrolled = 0
        for index in range(chunk_count):
            remaining_steps = chunk_count - index
            remaining_px = total - scrolled
            if remaining_steps == 1:
                delta = remaining_px
            else:
                base = remaining_px / remaining_steps
                delta = int(base * self.rng.uniform(*self.profile.scroll_step_spread))
                delta = max(1, min(delta, remaining_px - remaining_steps + 1))
            await self._scroll_by(page, delta * direction)
            scrolled += delta
            if remaining_steps == 1:
                break
            await self.pause(self.profile.scroll_pause)
            if self.rng.random() < self.profile.scroll_backtrack_chance:
                backtrack = max(1, int(delta * self.rng.uniform(0.1, 0.3)))
                await self._scroll_by(page, -backtrack * direction)
                await self.pause(self.profile.scroll_pause)
                await self._scroll_by(page, backtrack * direction)
                await self.pause(self.profile.scroll_pause)

        await self.micro_pause()
        return scrolled * direction

    async def _scroll_by(self, page: Any, delta: int) -> None:
        await page.evaluate("(delta) => window.scrollBy(0, delta)", delta)

    async def _clear(self, target: Any) -> None:
        filler = getattr(target, "fill", None)
        if filler is None:
            return
        await filler("")
        await self.micro_pause()

    async def _paste(self, target: Any, text: str) -> None:
        await self.pause(self.profile.paste_compose)
        filler = getattr(target, "fill", None)
        if filler is not None:
            await filler(text)
        else:
            await self._resolve_emitter(target)(text)
        await self.pause(self.profile.paste_settle)

    async def _keystrokes(self, target: Any, text: str) -> None:
        emit = self._resolve_emitter(target)
        last_index = len(text) - 1
        for index, char in enumerate(text):
            await emit(char)
            if index == last_index:
                break
            await self.sleep(self.profile.keystroke.sample(self.rng))
            if char in PUNCTUATION_CHARS:
                await self.pause(self.profile.punctuation_pause)
            elif char == " " and self.rng.random() < self.profile.word_pause_chance:
                await self.pause(self.profile.word_pause)
            elif self.rng.random() < self.profile.thinking_pause_chance:
                await self.pause(self.profile.thinking_pause)

    @staticmethod
    def _resolve_emitter(target: Any) -> EmitFn:
        typer = getattr(target, "type", None)
        if callable(typer):
            return typer

        sequential = getattr(target, "press_sequentially", None)
        if callable(sequential):
            return sequential

        filler = getattr(target, "fill", None)
        if not callable(filler):
            raise TypeError("Typing target must expose type(), press_sequentially() or fill()")

        buffer: list[str] = []

        async def emit(chunk: str) -> None:
            buffer.append(chunk)
            await filler("".join(buffer))

        return emit


def profile_from_env(env: Mapping[str, str] | None = None) -> PacingProfile:
    """Resolve the pacing preset from the environment, defaulting to SAFE."""
    raw = (env if env is not None else os.environ).get(PACING_ENV_VAR, "").strip().lower()
    if not raw:
        return SAFE
    preset = PRESETS.get(raw)
    if preset is None:
        logger.warning("Unknown %s value %r; falling back to SAFE pacing", PACING_ENV_VAR, raw)
        return SAFE
    return preset


_humanizer: Humanizer | None = None


def get_humanizer() -> Humanizer:
    """Return the process-wide humanizer, building a SAFE one on first use."""
    global _humanizer
    if _humanizer is None:
        _humanizer = Humanizer(profile_from_env())
    return _humanizer


def set_humanizer(humanizer: Humanizer | None) -> Humanizer | None:
    """Replace the process-wide humanizer and return the previous one."""
    global _humanizer
    previous = _humanizer
    _humanizer = humanizer
    return previous


async def pace(seconds: float, jitter: float | None = None) -> float:
    """Jittered replacement for a fixed timeout."""
    return await get_humanizer().linger(seconds, jitter)


async def settle() -> float:
    """Wait for a freshly loaded page to settle."""
    return await get_humanizer().settle()


async def cooldown() -> float:
    """Delay between two consecutive LinkedIn actions."""
    return await get_humanizer().cooldown()


async def dwell_and_click(target: Any) -> None:
    """Dwell on a control before clicking it."""
    await get_humanizer().click(target)


async def type_text(
    target: Any,
    text: str,
    mode: TypingMode | str | None = None,
    clear: bool = False,
) -> TypingMode:
    """Enter text into a field with humanised pacing."""
    return await get_humanizer().type_text(target, text, mode=mode, clear=clear)


async def scroll_page(page: Any, distance: int = 800, steps: int | None = None) -> int:
    """Scroll a page naturally, in uneven steps with pauses."""
    return await get_humanizer().scroll(page, distance, steps=steps)
