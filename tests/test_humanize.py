import re
from pathlib import Path

import pytest

from linkedin_mcp.browser import humanize
from linkedin_mcp.browser.humanize import (
    FAST,
    SAFE,
    DelayRange,
    Humanizer,
    PacingProfile,
    TypingMode,
    profile_from_env,
    set_humanizer,
)
from linkedin_mcp.browser.navigate import (
    NavigationError,
    SessionExpiredError,
    goto_profile,
    profile_slug,
    slug_to_query,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
MANUAL_SLEEP_PATTERN = re.compile(r"\b(?:asyncio|time)\.sleep\s*\(|\bwait_for_timeout\s*\(")
PROFILE_URL = "https://www.linkedin.com/in/nived-velayudhan-123456/"
PROFILE_SLUG = "nived-velayudhan-123456"


class RecordingSleep:
    """Stand-in for asyncio.sleep that records durations instead of waiting."""

    def __init__(self):
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def build_humanizer(profile: PacingProfile = SAFE, seed: int = 1234):
    sleeper = RecordingSleep()
    return Humanizer(profile=profile, seed=seed, sleep=sleeper), sleeper


class FakeField:
    def __init__(self, sleeper: RecordingSleep | None = None, on_click=None):
        self.sleeper = sleeper
        self.on_click = on_click
        self.typed: list[str] = []
        self.filled: list[str] = []
        self.pressed: list[str] = []
        self.clicks: list[int] = []

    async def type(self, chunk: str) -> None:
        self.typed.append(chunk)

    async def fill(self, value: str) -> None:
        self.filled.append(value)

    async def press(self, key: str) -> None:
        self.pressed.append(key)

    async def click(self) -> None:
        self.clicks.append(len(self.sleeper.calls) if self.sleeper else 0)
        if self.on_click:
            self.on_click()

    @property
    def text(self) -> str:
        return "".join(self.typed)


class FillOnlyField:
    def __init__(self):
        self.filled: list[str] = []

    async def fill(self, value: str) -> None:
        self.filled.append(value)


class FakePage:
    def __init__(self, url: str = "https://www.linkedin.com/feed/"):
        self.url = url
        self.scrolls: list[int] = []

    async def evaluate(self, script: str, arg=None):
        if isinstance(arg, int):
            self.scrolls.append(arg)
        return None


class FakeKeyboard:
    def __init__(self):
        self.pressed: list[str] = []

    async def press(self, key: str) -> None:
        self.pressed.append(key)


class FakeSearchPage(FakePage):
    """Page that behaves like LinkedIn's global nav plus a search results list."""

    def __init__(self, url: str = "https://www.linkedin.com/feed/", result_slug: str | None = PROFILE_SLUG):
        super().__init__(url)
        self.result_slug = result_slug
        self.goto_calls: list[str] = []
        self.queried: list[str] = []
        self.waited_urls: list[str] = []
        self.keyboard = FakeKeyboard()
        self.search_input = FakeField()
        self.result_link = FakeField(on_click=self._land_on_profile)

    def _land_on_profile(self) -> None:
        self.url = f"https://www.linkedin.com/in/{self.result_slug}/"

    async def goto(self, url: str, wait_until: str | None = None, timeout: int | None = None) -> None:
        self.goto_calls.append(url)
        self.url = url

    async def query_selector(self, selector: str):
        self.queried.append(selector)
        if selector == "input.search-global-typeahead__input":
            return self.search_input
        if selector.startswith('a[href*="/in/'):
            if self.result_slug and f'/in/{self.result_slug}"' in selector:
                return self.result_link
            return None
        return None

    async def wait_for_selector(self, selector: str, timeout: int | None = None):
        return await self.query_selector(selector)

    async def wait_for_url(self, pattern: str, timeout: int | None = None) -> None:
        self.waited_urls.append(pattern)


@pytest.fixture(autouse=True)
def restore_default_humanizer():
    previous = set_humanizer(None)
    yield
    set_humanizer(previous)


def test_delay_range_validates_and_samples_deterministically():
    with pytest.raises(ValueError):
        DelayRange(-0.1, 1.0)
    with pytest.raises(ValueError):
        DelayRange(2.0, 1.0)

    window = DelayRange(0.5, 1.5)
    first, _ = build_humanizer(seed=99)
    second, _ = build_humanizer(seed=99)
    samples = [window.sample(first.rng) for _ in range(20)]

    assert samples == [window.sample(second.rng) for _ in range(20)]
    assert all(0.5 <= sample <= 1.5 for sample in samples)
    assert window.scaled(2.0) == DelayRange(1.0, 3.0)


def test_safe_is_the_default_preset():
    assert Humanizer().profile is SAFE
    assert profile_from_env({}) is SAFE
    assert profile_from_env({humanize.PACING_ENV_VAR: "fast"}) is FAST
    assert profile_from_env({humanize.PACING_ENV_VAR: "SAFE"}) is SAFE
    assert profile_from_env({humanize.PACING_ENV_VAR: "nonsense"}) is SAFE
    assert SAFE.typing_mode is TypingMode.TYPE


def test_fast_preset_is_never_slower_than_safe():
    windows = (
        "micro_step",
        "dwell_before_click",
        "page_settle",
        "keystroke",
        "word_pause",
        "punctuation_pause",
        "thinking_pause",
        "paste_compose",
        "paste_settle",
        "scroll_pause",
        "between_actions",
    )
    for name in windows:
        fast: DelayRange = getattr(FAST, name)
        safe: DelayRange = getattr(SAFE, name)
        assert fast.minimum <= safe.minimum, name
        assert fast.maximum <= safe.maximum, name

    assert FAST.wait_scale < SAFE.wait_scale


def test_safe_cooldown_matches_the_documented_rate_limit_window():
    assert SAFE.between_actions == DelayRange(10.0, 60.0)


@pytest.mark.asyncio
async def test_pause_helpers_sleep_within_their_windows():
    pacer, sleeper = build_humanizer()

    micro = await pacer.micro_pause()
    dwell = await pacer.dwell()
    settle = await pacer.settle()
    cooldown = await pacer.cooldown()

    assert SAFE.micro_step.minimum <= micro <= SAFE.micro_step.maximum
    assert SAFE.dwell_before_click.minimum <= dwell <= SAFE.dwell_before_click.maximum
    assert SAFE.page_settle.minimum <= settle <= SAFE.page_settle.maximum
    assert SAFE.between_actions.minimum <= cooldown <= SAFE.between_actions.maximum
    assert sleeper.calls == [micro, dwell, settle, cooldown]
    assert pacer.elapsed == pytest.approx(sum(sleeper.calls))


@pytest.mark.asyncio
async def test_linger_jitters_and_scales_by_preset():
    safe_pacer, _ = build_humanizer(SAFE, seed=7)
    fast_pacer, _ = build_humanizer(FAST, seed=7)

    safe_waits = [await safe_pacer.linger(4.0) for _ in range(30)]
    fast_waits = [await fast_pacer.linger(4.0) for _ in range(30)]

    assert all(4.0 * 0.65 <= wait <= 4.0 * 1.35 for wait in safe_waits)
    assert all(2.0 * 0.75 <= wait <= 2.0 * 1.25 for wait in fast_waits)
    assert len(set(safe_waits)) > 1

    with pytest.raises(ValueError):
        await safe_pacer.linger(-1.0)
    with pytest.raises(ValueError):
        await safe_pacer.linger(1.0, jitter=1.5)


@pytest.mark.asyncio
async def test_type_mode_sends_one_keystroke_at_a_time_with_jitter():
    pacer, sleeper = build_humanizer()
    field = FakeField(sleeper)
    text = "Enterprise Copilot rollouts, at scale."

    mode = await pacer.type_text(field, text, mode=TypingMode.TYPE)

    assert mode is TypingMode.TYPE
    assert field.typed == list(text)
    assert field.text == text
    assert field.filled == []
    assert len(sleeper.calls) >= len(text) + 1
    assert min(sleeper.calls) >= SAFE.keystroke.minimum
    assert len(set(sleeper.calls)) > 1


@pytest.mark.asyncio
async def test_paste_mode_fills_the_field_in_one_shot():
    pacer, sleeper = build_humanizer()
    field = FakeField(sleeper)

    mode = await pacer.type_text(field, "pasted note", mode="paste")

    assert mode is TypingMode.PASTE
    assert field.filled == ["pasted note"]
    assert field.typed == []
    assert len(sleeper.calls) == 4


@pytest.mark.asyncio
async def test_clear_wipes_the_field_before_typing():
    pacer, sleeper = build_humanizer()
    field = FakeField(sleeper)

    await pacer.type_text(field, "abc", mode=TypingMode.TYPE, clear=True)

    assert field.filled == [""]
    assert field.typed == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_fill_only_targets_accumulate_keystrokes():
    pacer, _ = build_humanizer()
    field = FillOnlyField()

    await pacer.type_text(field, "abc", mode=TypingMode.TYPE)

    assert field.filled == ["a", "ab", "abc"]


@pytest.mark.asyncio
async def test_random_mode_is_seeded_and_covers_both_modes():
    first, _ = build_humanizer(FAST, seed=42)
    second, _ = build_humanizer(FAST, seed=42)

    first_modes = [first.resolve_typing_mode() for _ in range(12)]
    second_modes = [second.resolve_typing_mode() for _ in range(12)]

    assert first_modes == second_modes
    assert set(first_modes) == {TypingMode.TYPE, TypingMode.PASTE}
    assert FAST.typing_mode is TypingMode.RANDOM


@pytest.mark.asyncio
async def test_click_dwells_before_pressing_the_control():
    pacer, sleeper = build_humanizer()
    field = FakeField(sleeper)

    await pacer.click(field)

    assert field.clicks == [1]
    assert len(sleeper.calls) == 2
    assert SAFE.dwell_before_click.minimum <= sleeper.calls[0] <= SAFE.dwell_before_click.maximum


@pytest.mark.asyncio
async def test_scroll_moves_in_uneven_paced_steps():
    pacer, sleeper = build_humanizer(seed=3)
    page = FakePage()

    scrolled = await pacer.scroll(page, 800)

    assert scrolled == 800
    assert len(page.scrolls) >= 3
    assert sum(page.scrolls) == 800
    assert all(delta > 0 for delta in page.scrolls)
    assert len(set(page.scrolls)) > 1
    assert len(sleeper.calls) >= len(page.scrolls)


@pytest.mark.asyncio
async def test_scroll_supports_upwards_and_no_op_distances():
    pacer, _ = build_humanizer(seed=11)
    page = FakePage()

    assert await pacer.scroll(page, 0) == 0
    assert page.scrolls == []

    scrolled = await pacer.scroll(page, -600, steps=3)
    assert scrolled == -600
    assert sum(page.scrolls) == -600
    assert all(delta < 0 for delta in page.scrolls)


@pytest.mark.asyncio
async def test_identical_seeds_produce_identical_delay_sequences():
    async def run(seed: int) -> list[float]:
        pacer, sleeper = build_humanizer(seed=seed)
        field = FakeField(sleeper)
        await pacer.type_text(field, "hello there", mode=TypingMode.TYPE)
        await pacer.click(field)
        await pacer.scroll(FakePage(), 500)
        await pacer.cooldown()
        return sleeper.calls

    assert await run(2024) == await run(2024)
    assert await run(2024) != await run(2025)


@pytest.mark.asyncio
async def test_module_level_helpers_use_the_configured_humanizer():
    pacer, sleeper = build_humanizer(FAST, seed=5)
    set_humanizer(pacer)
    field = FakeField(sleeper)
    page = FakePage()

    await humanize.pace(2.0)
    await humanize.settle()
    await humanize.cooldown()
    await humanize.dwell_and_click(field)
    await humanize.type_text(field, "hi", mode=TypingMode.TYPE)
    await humanize.scroll_page(page, 300, steps=2)

    assert humanize.get_humanizer() is pacer
    assert field.clicks and field.typed == ["h", "i"]
    assert sum(page.scrolls) == 300
    assert sleeper.calls and pacer.elapsed == pytest.approx(sum(sleeper.calls))


def test_profile_slug_and_search_query_derivation():
    assert profile_slug(PROFILE_URL) == PROFILE_SLUG
    assert profile_slug("https://www.linkedin.com/in/sundarpichai") == "sundarpichai"
    assert slug_to_query(PROFILE_SLUG) == "nived velayudhan"
    assert slug_to_query("sundarpichai") == "sundarpichai"

    with pytest.raises(NavigationError):
        profile_slug("https://www.linkedin.com/feed/")


@pytest.mark.asyncio
async def test_goto_profile_uses_the_search_bar_by_default():
    pacer, _ = build_humanizer(FAST, seed=8)
    page = FakeSearchPage()

    result = await goto_profile(page, PROFILE_URL, humanizer=pacer)

    assert result.method == "search_bar"
    assert result.slug == PROFILE_SLUG
    assert result.query == "nived velayudhan"
    assert page.goto_calls == []
    assert page.search_input.filled[0] == ""
    assert page.search_input.text == "nived velayudhan"
    assert page.keyboard.pressed == ["Enter"]
    assert page.result_link.clicks
    assert page.url == f"https://www.linkedin.com/in/{PROFILE_SLUG}/"


@pytest.mark.asyncio
async def test_goto_profile_opens_the_feed_when_off_linkedin():
    pacer, _ = build_humanizer(FAST, seed=8)
    page = FakeSearchPage(url="about:blank")

    await goto_profile(page, PROFILE_URL, humanizer=pacer)

    assert page.goto_calls == ["https://www.linkedin.com/feed/"]


@pytest.mark.asyncio
async def test_login_wall_is_reported_as_an_expired_session():
    pacer, _ = build_humanizer(FAST, seed=8)
    page = FakeSearchPage(url="https://www.linkedin.com/checkpoint/challenge/")

    with pytest.raises(SessionExpiredError, match="Session expired"):
        await goto_profile(page, PROFILE_URL, humanizer=pacer)

    with pytest.raises(SessionExpiredError):
        await goto_profile(page, PROFILE_URL, humanizer=pacer, allow_direct_fallback=True)

    assert page.goto_calls == []


@pytest.mark.asyncio
async def test_direct_profile_load_is_opt_in():
    pacer, _ = build_humanizer(FAST, seed=8)
    page = FakeSearchPage()

    result = await goto_profile(page, PROFILE_URL, humanizer=pacer, direct=True)

    assert result.method == "direct"
    assert page.goto_calls == [PROFILE_URL]
    assert page.keyboard.pressed == []


@pytest.mark.asyncio
async def test_missing_search_result_fails_loudly_without_direct_load():
    pacer, _ = build_humanizer(FAST, seed=8)
    page = FakeSearchPage(result_slug=None)

    with pytest.raises(NavigationError):
        await goto_profile(page, PROFILE_URL, humanizer=pacer)

    assert page.goto_calls == []


@pytest.mark.asyncio
async def test_direct_fallback_only_happens_when_allowed():
    pacer, _ = build_humanizer(FAST, seed=8)
    page = FakeSearchPage(result_slug=None)

    result = await goto_profile(page, PROFILE_URL, humanizer=pacer, allow_direct_fallback=True)

    assert result.method == "direct"
    assert page.goto_calls[-1] == PROFILE_URL


def test_no_manual_sleep_outside_humanize():
    offenders = []
    for path in sorted(REPO_ROOT.rglob("*.py")):
        parts = set(path.parts)
        if parts & {".git", ".venv", "venv", "__pycache__", "tests"}:
            continue
        if path.name.startswith("test_") or path.name == "humanize.py":
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if MANUAL_SLEEP_PATTERN.search(line):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}: {line.strip()}")

    assert offenders == [], "Pacing must route through linkedin_mcp.browser.humanize:\n" + "\n".join(offenders)


def test_mcp_server_routes_pacing_and_navigation_through_the_browser_package():
    source = (REPO_ROOT / "linkedin_browser_mcp.py").read_text(encoding="utf-8")

    assert "from linkedin_mcp.browser.humanize import" in source
    assert "from linkedin_mcp.browser.navigate import goto_profile" in source
    assert "import random" not in source
    assert "delay=25" not in source
    assert "await session.new_page(profile_url)" not in source
