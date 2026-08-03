"""
Log in to LinkedIn using the WORKER's persistent browser profile (account_seed="1").

Run this ONCE. After it completes the worker will reuse the authenticated profile
without ever needing to log in again (30-day session).

    python login_worker_profile.py
"""
import asyncio
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from linkedin_mcp.browser.session import BrowserSession, save_cookies


async def login():
    username = os.getenv("LINKEDIN_USERNAME", "").strip()
    password = os.getenv("LINKEDIN_PASSWORD", "").strip()

    print("[info] Opening LinkedIn in the worker browser profile (account_seed='1')...")
    print("[info] Credentials will be pre-filled. Complete any 2FA/CAPTCHA manually.")

    # account_seed="1" matches what worker.py passes to build_browser_supplier
    async with BrowserSession(platform="linkedin", headless=False, account_seed="1") as session:
        page = await session.new_page()
        await page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded", timeout=30000)

        if "feed" in page.url:
            print("[success] Already logged in — persistent profile is valid.")
            return

        # Pre-fill credentials
        try:
            if username:
                await page.fill("#username", username)
            if password:
                await page.fill("#password", password)
                await page.click('button[type="submit"]')
                print("[info] Credentials submitted. Waiting for feed (up to 5 min)...")
        except Exception as e:
            print(f"[warning] Auto-fill failed ({e}). Please log in manually in the browser.")

        try:
            await page.wait_for_url("**/feed/**", timeout=300_000)
            print("[success] Logged in! Persistent profile saved.")
            # Save encrypted fallback cookies too
            await save_cookies(page, "linkedin", "1")
            print("[done] Session saved. The worker will reuse this profile automatically.")
        except Exception as e:
            print(f"[error] Login timeout or failed: {e}")


if __name__ == "__main__":
    asyncio.run(login())
