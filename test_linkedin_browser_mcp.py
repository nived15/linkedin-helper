import pytest
import os
import inspect
import re
from pathlib import Path

import linkedin_browser_mcp
import linkedin_mcp.browser.session as browser_session_module
from linkedin_browser_mcp import (
    BrowserSession,
    save_cookies,
    load_cookies,
    login_linkedin,
    login_linkedin_secure,
    browse_linkedin_feed,
    search_linkedin_profiles,
    view_linkedin_profile,
    interact_with_linkedin_post
)
from linkedin_mcp.browser.selectors import selector_fallbacks
from linkedin_mcp.browser.session import FINGERPRINT_CATALOG, build_fingerprint_profile

class MockContext:
    def info(self, message):
        print(f"INFO: {message}")
        
    def error(self, message):
        print(f"ERROR: {message}")
        
    async def report_progress(self, current, total):
        print(f"Progress: {current}/{total}")

@pytest.mark.asyncio
async def test_browser_session(monkeypatch):
    class FakePage:
        def __init__(self, context):
            self.context = context
            self.url = ""
            self.goto_calls = []

        def is_closed(self):
            return False

        async def goto(self, url, wait_until, timeout):
            self.url = url
            self.goto_calls.append(
                {"url": url, "wait_until": wait_until, "timeout": timeout}
            )

    class FakeContext:
        def __init__(self, browser=None, **kwargs):
            self.browser = browser
            self.kwargs = kwargs
            self.init_scripts = []
            self.pages = []
            self.closed = False

        async def add_init_script(self, script):
            self.init_scripts.append(script)

        async def add_cookies(self, _cookies):
            return None

        async def new_page(self):
            page = FakePage(self)
            self.pages.append(page)
            return page

        async def close(self):
            self.closed = True

    class FakeBrowser:
        def __init__(self):
            self.closed = False

        def is_connected(self):
            return not self.closed

        async def close(self):
            self.closed = True

    class FakeChromium:
        def __init__(self):
            self.browser = FakeBrowser()
            self.user_data_dir = None
            self.launch_kwargs = None

        async def launch_persistent_context(self, user_data_dir, **kwargs):
            self.user_data_dir = user_data_dir
            self.launch_kwargs = kwargs
            return FakeContext(**kwargs)

    class FakePlaywright:
        def __init__(self):
            self.chromium = FakeChromium()
            self.stopped = False

        async def stop(self):
            self.stopped = True

    class FakePlaywrightStarter:
        def __init__(self):
            self.instance = FakePlaywright()

        async def start(self):
            return self.instance

    monkeypatch.setattr(
        "linkedin_mcp.browser.session.async_playwright",
        lambda: FakePlaywrightStarter(),
    )
    monkeypatch.setattr(
        "linkedin_mcp.browser.session.setup_sessions_directory",
        lambda: True,
    )

    async def fake_load_cookies(_context, _platform, _account_seed=None):
        return False

    monkeypatch.setattr(
        "linkedin_mcp.browser.session.load_cookies",
        fake_load_cookies,
    )

    await BrowserSession.shutdown()
    profile = build_fingerprint_profile("linkedin", "seed@example.com")

    async with BrowserSession(
        platform='linkedin',
        headless=True,
        account_seed="seed@example.com",
    ) as session:
        page = await session.new_page()
        assert page is not None
        assert BrowserSession._context is not None
        assert BrowserSession._context.kwargs["user_agent"] == profile.user_agent
        assert BrowserSession._context.kwargs["locale"] == profile.locale
        assert BrowserSession._context.kwargs["timezone_id"] == profile.timezone_id
        assert (
            BrowserSession._context.kwargs["extra_http_headers"]["Accept-Language"]
            == profile.accept_language
        )
        assert "navigator, 'webdriver'" in BrowserSession._context.init_scripts[0]
        assert "linkedin_" in str(BrowserSession._driver.user_data_dir)

        page = await session.new_page("https://www.linkedin.com/feed/")
        assert page.goto_calls[0]["wait_until"] == "networkidle"

    await BrowserSession.shutdown()


def test_fingerprint_profile_is_stable_per_account():
    first = build_fingerprint_profile("linkedin", "seed@example.com")
    second = build_fingerprint_profile("linkedin", "seed@example.com")
    catalog_user_agents = {profile.user_agent for profile in FINGERPRINT_CATALOG}
    major_versions = [
        int(re.search(r"Chrome/(\d+)\.", user_agent).group(1))
        for user_agent in catalog_user_agents
    ]

    assert first == second
    assert first.user_agent in catalog_user_agents
    assert re.search(r"Chrome/\d{3}\.\d+\.\d+\.\d+", first.user_agent)
    assert int(re.search(r"Chrome/(\d+)\.", first.user_agent).group(1)) >= 120
    assert all(
        re.search(r"Chrome/\d{3}\.\d+\.\d+\.\d+", user_agent)
        for user_agent in catalog_user_agents
    )
    assert all(version >= 120 for version in major_versions)


def test_selectors_are_centralized_in_browser_module():
    main_source = inspect.getsource(linkedin_browser_mcp)
    session_source = inspect.getsource(browser_session_module)
    # MCP-03 (#26): the selector lookups moved with the page-driving bodies into
    # linkedin_mcp/executors/, so the guard checks the whole set. Leaving it on
    # the server alone would have declared victory over a file that no longer
    # touches a selector either way.
    executor_dir = Path(inspect.getfile(linkedin_browser_mcp)).parent / "linkedin_mcp" / "executors"
    executor_sources = {
        path: path.read_text(encoding="utf-8")
        for path in sorted(executor_dir.glob("*.py"))
    }

    for path, source in {
        Path("linkedin_browser_mcp.py"): main_source,
        **executor_sources,
    }.items():
        assert not re.search(r"Chrome/\d{3}\.\d+\.\d+\.\d+", source), path
        assert ".pv-top-card" not in source, path
        assert ".feed-shared-update-v2" not in source, path

    users = [
        path
        for path, source in executor_sources.items()
        if "selector_fallback" in source
    ]
    assert users, "no executor resolves a selector through the catalog"
    assert "FINGERPRINT_CATALOG" in session_source
    assert selector_fallbacks("feed_post_container")[0] == '[data-urn*="urn:li:activity"]'

@pytest.mark.asyncio
async def test_login_linkedin_secure_missing_credentials():
    ctx = MockContext()
    # Clear environment variables
    if 'LINKEDIN_USERNAME' in os.environ:
        del os.environ['LINKEDIN_USERNAME']
    if 'LINKEDIN_PASSWORD' in os.environ:
        del os.environ['LINKEDIN_PASSWORD']
    result = await login_linkedin_secure(ctx)
    assert result["status"] == "error"
    assert "Missing LinkedIn credentials" in result["message"]

@pytest.mark.asyncio
async def test_login_linkedin_secure_invalid_email():
    ctx = MockContext()
    os.environ['LINKEDIN_USERNAME'] = 'invalid-email'
    os.environ['LINKEDIN_PASSWORD'] = 'password123'
    result = await login_linkedin_secure(ctx)
    assert result["status"] == "error"
    assert "Invalid email format" in result["message"]

@pytest.mark.asyncio
async def test_login_linkedin_secure_short_password():
    ctx = MockContext()
    os.environ['LINKEDIN_USERNAME'] = 'test@example.com'
    os.environ['LINKEDIN_PASSWORD'] = 'short'
    result = await login_linkedin_secure(ctx)
    assert result["status"] == "error"
    assert "password must be at least 8 characters" in result["message"]

@pytest.mark.asyncio
async def test_view_linkedin_profile_invalid_url():
    ctx = MockContext()
    result = await view_linkedin_profile("https://invalid-url.com", ctx)
    assert result["status"] == "error"
    assert "Invalid LinkedIn profile URL" in result["message"]

@pytest.mark.asyncio
async def test_interact_with_linkedin_post_invalid_url():
    ctx = MockContext()
    result = await interact_with_linkedin_post("https://invalid-url.com", ctx)
    assert result["status"] == "error"
    assert "Invalid LinkedIn post URL" in result["message"]

@pytest.mark.asyncio
async def test_interact_with_linkedin_post_invalid_action():
    ctx = MockContext()
    result = await interact_with_linkedin_post(
        "https://linkedin.com/posts/123",
        ctx,
        action="invalid"
    )
    assert result["status"] == "error"
    assert "Invalid action" in result["message"] 