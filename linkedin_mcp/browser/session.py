"""Browser session wrapper with deterministic fingerprinting and stealth."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path

from cryptography.fernet import Fernet
from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
SESSIONS_DIR = REPO_ROOT / "sessions"
ENV_PATH = REPO_ROOT / ".env"
SESSION_COOKIE_TTL_SECONDS = 86400


@dataclass(frozen=True)
class FingerprintProfile:
    """Stable browser fingerprint details for one account."""

    user_agent: str
    locale: str
    timezone_id: str
    accept_language: str
    languages: tuple[str, ...]
    navigator_platform: str


FINGERPRINT_CATALOG: tuple[FingerprintProfile, ...] = (
    FingerprintProfile(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/138.0.7204.169 Safari/537.36"
        ),
        locale="en-US",
        timezone_id="America/New_York",
        accept_language="en-US,en;q=0.9",
        languages=("en-US", "en"),
        navigator_platform="Win32",
    ),
    FingerprintProfile(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/139.0.7258.155 Safari/537.36"
        ),
        locale="en-GB",
        timezone_id="Europe/London",
        accept_language="en-GB,en;q=0.9",
        languages=("en-GB", "en"),
        navigator_platform="Win32",
    ),
    FingerprintProfile(
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/138.0.7204.157 Safari/537.36"
        ),
        locale="en-US",
        timezone_id="America/Los_Angeles",
        accept_language="en-US,en;q=0.9",
        languages=("en-US", "en"),
        navigator_platform="MacIntel",
    ),
)


def setup_sessions_directory() -> bool:
    """Set up the sessions directory with proper permissions."""
    try:
        SESSIONS_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(SESSIONS_DIR, 0o700)
        logger.debug("Sessions directory set up at %s with owner-only permissions", SESSIONS_DIR)
        return True
    except Exception as exc:
        logger.error("Failed to set up sessions directory: %s", exc)
        return False


def get_or_create_encryption_key() -> bytes:
    """Return the encryption key, generating and persisting one to .env if missing."""
    key = os.getenv("COOKIE_ENCRYPTION_KEY", "").strip()
    if key:
        return key.encode() if isinstance(key, str) else key

    new_key = Fernet.generate_key()
    try:
        existing = ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.exists() else ""
        if "COOKIE_ENCRYPTION_KEY=" in existing:
            lines = []
            for line in existing.splitlines():
                if line.startswith("COOKIE_ENCRYPTION_KEY="):
                    lines.append(f"COOKIE_ENCRYPTION_KEY={new_key.decode()}")
                else:
                    lines.append(line)
            ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
        else:
            with open(ENV_PATH, "a", encoding="utf-8") as env_file:
                env_file.write(f"\nCOOKIE_ENCRYPTION_KEY={new_key.decode()}\n")
        os.environ["COOKIE_ENCRYPTION_KEY"] = new_key.decode()
        logger.info("Generated and saved new COOKIE_ENCRYPTION_KEY to .env")
    except Exception as exc:
        logger.warning("Could not persist COOKIE_ENCRYPTION_KEY to .env: %s", exc)
    return new_key


def session_cookie_path(platform: str, account_seed: str | None = None) -> Path:
    """Return the encrypted cookie path for a platform."""
    if not account_seed:
        return SESSIONS_DIR / f"{platform}_cookies.json"
    return SESSIONS_DIR / f"{platform}_{account_session_suffix(platform, account_seed)}_cookies.json"


def account_session_suffix(platform: str, account_seed: str | None = None) -> str:
    """Return a stable per-account suffix for persisted session artifacts."""
    seed_source = account_seed or os.getenv(f"{platform.upper()}_USERNAME", "").strip() or platform
    return hashlib.sha256(seed_source.encode("utf-8")).hexdigest()[:12]


def persistent_profile_dir(platform: str, account_seed: str | None = None) -> Path:
    """Return the per-account persistent profile directory."""
    return SESSIONS_DIR / f"{platform}_{account_session_suffix(platform, account_seed)}_profile"


def resolve_proxy_url(
    platform: str,
    account_seed: str | None = None,
    explicit_proxy_url: str | None = None,
) -> str | None:
    """Resolve proxy URL, preferring explicit value and account-scoped env vars."""
    if explicit_proxy_url:
        return explicit_proxy_url.strip() or None

    if account_seed:
        account_key = re.sub(r"[^A-Z0-9]+", "_", account_seed.upper()).strip("_")
        env_proxy = os.getenv(f"{platform.upper()}_PROXY_{account_key}", "").strip()
        if env_proxy:
            return env_proxy

    proxy_url = os.getenv(f"{platform.upper()}_PROXY", "").strip()
    return proxy_url or None


def build_fingerprint_profile(platform: str, account_seed: str | None = None) -> FingerprintProfile:
    """Choose a stable fingerprint profile for the current account."""
    seed_source = account_seed or os.getenv(f"{platform.upper()}_USERNAME", "").strip() or platform
    seed_bytes = hashlib.sha256(seed_source.encode("utf-8")).digest()
    seeded_random = random.Random(int.from_bytes(seed_bytes[:8], "big"))
    return FINGERPRINT_CATALOG[seeded_random.randrange(len(FINGERPRINT_CATALOG))]


def build_stealth_script(profile: FingerprintProfile) -> str:
    """Return an init script that patches obvious automation fingerprints."""
    locale = json.dumps(profile.locale)
    languages = json.dumps(list(profile.languages))
    platform = json.dumps(profile.navigator_platform)
    return f"""
(() => {{
  const define = (target, key, value) => {{
    try {{
      Object.defineProperty(target, key, {{
        configurable: true,
        get: () => value,
      }});
    }} catch (_error) {{}}
  }};

  define(navigator, 'webdriver', undefined);
  define(navigator, 'language', {locale});
  define(navigator, 'languages', {languages});
  define(navigator, 'platform', {platform});
  const plugins = [
    {{
      name: 'Chrome PDF Viewer',
      filename: 'internal-pdf-viewer',
      description: 'Portable Document Format',
    }},
    {{
      name: 'Chromium PDF Viewer',
      filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai',
      description: 'Portable Document Format',
    }},
    {{
      name: 'Microsoft Edge PDF Viewer',
      filename: 'internal-pdf-viewer',
      description: 'Portable Document Format',
    }},
  ];
  plugins.item = (index) => plugins[index] ?? null;
  plugins.namedItem = (name) => plugins.find((plugin) => plugin.name === name) ?? null;
  define(navigator, 'plugins', plugins);

  if (!window.chrome) {{
    Object.defineProperty(window, 'chrome', {{
      configurable: true,
      value: {{ runtime: {{}} }},
    }});
  }}
}})();
"""


async def save_cookies(page, platform, account_seed: str | None = None):
    """Save cookies with proper directory permissions."""
    try:
        cookies = await page.context.cookies()
        if not cookies or not isinstance(cookies, list):
            raise ValueError("Invalid cookie format")

        cookie_data = {
            "timestamp": int(time.time()),
            "cookies": cookies,
        }

        if not setup_sessions_directory():
            raise RuntimeError("Failed to set up sessions directory for cookie persistence")

        encrypted_data = Fernet(get_or_create_encryption_key()).encrypt(
            json.dumps(cookie_data).encode(),
        )

        cookie_file = session_cookie_path(platform, account_seed)
        with open(cookie_file, "wb") as cookie_handle:
            cookie_handle.write(encrypted_data)
        os.chmod(cookie_file, 0o600)
    except Exception as exc:
        raise RuntimeError(f"Failed to save cookies: {exc}") from exc


async def load_cookies(context, platform, account_seed: str | None = None):
    """Load encrypted cookies for a platform if available and fresh."""
    cookie_file = session_cookie_path(platform, account_seed)
    try:
        with open(cookie_file, "rb") as cookie_handle:
            encrypted_data = cookie_handle.read()

        key = get_or_create_encryption_key()
        if not key:
            return False

        cookie_data = json.loads(Fernet(key).decrypt(encrypted_data))
        age_seconds = int(time.time()) - cookie_data["timestamp"]
        if age_seconds > SESSION_COOKIE_TTL_SECONDS:
            cookie_file.unlink(missing_ok=True)
            return False

        await context.add_cookies(cookie_data["cookies"])
        return True
    except FileNotFoundError:
        return False
    except Exception:
        cookie_file.unlink(missing_ok=True)
        return False


class PlaywrightChromiumDriver:
    """Small driver wrapper around Playwright Chromium."""

    def __init__(
        self,
        platform: str,
        headless: bool,
        account_seed: str | None = None,
        proxy_url: str | None = None,
    ):
        self.platform = platform
        self.headless = headless
        self.account_seed = account_seed
        self.proxy_url = resolve_proxy_url(platform, account_seed, proxy_url)
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.user_data_dir = persistent_profile_dir(platform, account_seed)
        self.fingerprint = build_fingerprint_profile(platform, account_seed)

    async def launch(self):
        if not setup_sessions_directory():
            raise RuntimeError("Failed to set up sessions directory for browser session")

        logger.info("Launching Chromium driver for %s", self.platform)
        self.playwright = await asyncio.wait_for(async_playwright().start(), timeout=30)
        self.user_data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.user_data_dir, 0o700)
        launch_kwargs = {
            "headless": self.headless,
            "timeout": 30000,
            "args": [
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--start-maximized",
            ],
            "viewport": {"width": 1280, "height": 800},
            "user_agent": self.fingerprint.user_agent,
            "locale": self.fingerprint.locale,
            "timezone_id": self.fingerprint.timezone_id,
            "extra_http_headers": {"Accept-Language": self.fingerprint.accept_language},
        }
        if self.proxy_url:
            launch_kwargs["proxy"] = {"server": self.proxy_url}

        self.context = await self.playwright.chromium.launch_persistent_context(
            str(self.user_data_dir),
            **launch_kwargs,
        )
        self.browser = self.context.browser
        await self.context.add_init_script(build_stealth_script(self.fingerprint))

        try:
            loaded = await load_cookies(self.context, self.platform, self.account_seed)
            logger.info("Session cookies loaded" if loaded else "No saved session")
        except Exception as exc:
            logger.warning("Cookie load error: %s", exc)

        self.page = await self.context.new_page()

    async def new_page(self, url: str | None = None):
        """Return a shared page, optionally navigating to a URL."""
        if self.page is None or self.page.is_closed():
            self.page = await self.context.new_page()
        if url:
            await self.page.goto(url, wait_until="networkidle", timeout=30000)
        return self.page

    async def close(self):
        """Close all Playwright resources."""
        if self.context:
            try:
                await self.context.close()
            except Exception:
                pass
        if self.browser:
            try:
                await self.browser.close()
            except Exception:
                pass
        if self.playwright:
            try:
                await self.playwright.stop()
            except Exception:
                pass
        self.browser = None
        self.playwright = None
        self.context = None
        self.page = None


class BrowserSession:
    """Persistent browser session shared across MCP tool calls."""

    _driver = None
    _playwright = None
    _browser = None
    _context = None
    _page = None

    def __init__(
        self,
        platform="linkedin",
        headless=False,
        account_seed: str | None = None,
        proxy_url: str | None = None,
        **_kwargs,
    ):
        self.platform = platform
        self.headless = headless
        self.account_seed = account_seed
        self.proxy_url = proxy_url

    async def __aenter__(self):
        if (
            BrowserSession._driver is None
            or BrowserSession._context is None
            or (
                BrowserSession._browser is not None
                and not BrowserSession._browser.is_connected()
            )
        ):
            await self._launch()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    async def new_page(self, url=None):
        page = await BrowserSession._driver.new_page(url)
        self._sync_state(page=page)
        return page

    async def save_session(self, page):
        try:
            await save_cookies(page, self.platform, self.account_seed)
        except Exception as exc:
            logger.error("Error saving session: %s", exc)

    async def _launch(self):
        BrowserSession._driver = PlaywrightChromiumDriver(
            platform=self.platform,
            headless=self.headless,
            account_seed=self.account_seed,
            proxy_url=self.proxy_url,
        )
        await BrowserSession._driver.launch()
        self._sync_state(page=BrowserSession._driver.page)

    @classmethod
    def _sync_state(cls, page=None):
        if cls._driver is None:
            cls._playwright = None
            cls._browser = None
            cls._context = None
            cls._page = None
            return

        cls._playwright = cls._driver.playwright
        cls._browser = cls._driver.browser
        cls._context = cls._driver.context
        cls._page = page or cls._driver.page

    @classmethod
    async def shutdown(cls):
        """Close the browser and release all resources."""
        if cls._driver:
            await cls._driver.close()
        cls._driver = None
        cls._sync_state()
        logger.info("Browser session closed")
