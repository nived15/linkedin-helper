import os

import pytest
from cryptography.fernet import Fernet

from linkedin_mcp.browser import session as session_module


@pytest.mark.asyncio
async def test_cookie_persistence_works_when_cwd_changes(tmp_path, monkeypatch):
    sessions_dir = tmp_path / "sessions"
    fixed_key = Fernet.generate_key()

    monkeypatch.setattr(session_module, "SESSIONS_DIR", sessions_dir)
    monkeypatch.setattr(session_module, "get_or_create_encryption_key", lambda: fixed_key)

    class FakePageContext:
        async def cookies(self):
            return [{"name": "li_at", "value": "token", "domain": ".linkedin.com", "path": "/"}]

    class FakePage:
        context = FakePageContext()

    class FakeContext:
        def __init__(self):
            self.cookies = None

        async def add_cookies(self, cookies):
            self.cookies = cookies

    await session_module.save_cookies(FakePage(), "linkedin")
    assert (sessions_dir / "linkedin_cookies.json").exists()

    previous_cwd = os.getcwd()
    off_repo_cwd = tmp_path / "other-working-dir"
    off_repo_cwd.mkdir()
    os.chdir(off_repo_cwd)
    try:
        target_context = FakeContext()
        loaded = await session_module.load_cookies(target_context, "linkedin")
    finally:
        os.chdir(previous_cwd)

    assert loaded is True
    assert target_context.cookies[0]["name"] == "li_at"


def test_resolve_proxy_url_prefers_per_account_env(monkeypatch):
    monkeypatch.setenv("LINKEDIN_PROXY", "http://fallback:8080")
    monkeypatch.setenv("LINKEDIN_PROXY_TEST_EXAMPLE_COM", "http://account-proxy:9000")

    assert (
        session_module.resolve_proxy_url("linkedin", account_seed="test@example.com")
        == "http://account-proxy:9000"
    )
    assert session_module.resolve_proxy_url("linkedin", account_seed="other@example.com") == "http://fallback:8080"

