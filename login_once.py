"""
Run this once from the terminal to log in to LinkedIn and save your session.
After this completes, the MCP server tools will reuse the saved session.

Usage:
    python login_once.py
"""
import asyncio
import os
import sys
from pathlib import Path
from dotenv import load_dotenv
from cryptography.fernet import Fernet
from playwright.async_api import async_playwright
import json
import time

load_dotenv(Path(__file__).parent / '.env')

# Username field selectors, tried in order
USERNAME_SELECTORS = ['#username', 'input[name="session_key"]', 'input[type="email"]']
PASSWORD_SELECTORS = ['#password', 'input[name="session_password"]', 'input[type="password"]']


def get_or_create_encryption_key() -> bytes:
    key = os.getenv('COOKIE_ENCRYPTION_KEY', '').strip()
    if key:
        return key.encode()
    new_key = Fernet.generate_key()
    env_path = Path(__file__).parent / '.env'
    try:
        existing = env_path.read_text(encoding='utf-8') if env_path.exists() else ''
        if 'COOKIE_ENCRYPTION_KEY=' in existing:
            lines = []
            for line in existing.splitlines():
                if line.startswith('COOKIE_ENCRYPTION_KEY='):
                    lines.append(f'COOKIE_ENCRYPTION_KEY={new_key.decode()}')
                else:
                    lines.append(line)
            env_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        else:
            with open(env_path, 'a', encoding='utf-8') as f:
                f.write(f'\nCOOKIE_ENCRYPTION_KEY={new_key.decode()}\n')
        os.environ['COOKIE_ENCRYPTION_KEY'] = new_key.decode()
        print(f'[setup] Generated and saved COOKIE_ENCRYPTION_KEY to .env')
    except Exception as e:
        print(f'[warning] Could not persist key to .env: {e}')
    return new_key


async def try_fill(page, selectors, value, label):
    """Try each selector in turn, return True on first success."""
    for sel in selectors:
        try:
            await page.wait_for_selector(sel, timeout=5000, state='visible')
            await page.fill(sel, value)
            print(f'[info] Filled {label} using {sel}')
            return True
        except Exception:
            continue
    return False


async def login():
    username = os.getenv('LINKEDIN_USERNAME', '').strip()
    password = os.getenv('LINKEDIN_PASSWORD', '').strip()

    if not username or not password:
        print('[error] LINKEDIN_USERNAME or LINKEDIN_PASSWORD not set in .env')
        sys.exit(1)

    sessions_dir = Path(__file__).parent / 'sessions'
    sessions_dir.mkdir(exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            slow_mo=80,
            args=['--start-maximized', '--disable-blink-features=AutomationControlled'],
        )
        context = await browser.new_context(
            viewport={'width': 1280, 'height': 800},
            user_agent=(
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/138.0.7204.169 Safari/537.36'
            ),
        )
        page = await context.new_page()

        print('[info] Navigating to LinkedIn login...')
        try:
            await page.goto(
                'https://www.linkedin.com/login',
                wait_until='domcontentloaded',
                timeout=60_000,
            )
        except Exception as e:
            print(f'[warning] Navigation warning (continuing): {e}')

        # Allow page to settle
        await asyncio.sleep(3)

        if 'feed' in page.url or 'mynetwork' in page.url or 'jobs' in page.url:
            print('[info] Already logged in!')
        else:
            filled_user = await try_fill(page, USERNAME_SELECTORS, username, 'username')
            filled_pass = await try_fill(page, PASSWORD_SELECTORS, password, 'password')

            if filled_user and filled_pass:
                print('[info] Credentials pre-filled. Submitting...')
                try:
                    await page.click('button[type="submit"]', timeout=5000)
                except Exception:
                    await page.keyboard.press('Enter')
            else:
                print('[warning] Could not auto-fill credentials. Please log in manually in the browser window.')

        print('[info] Waiting up to 5 minutes for LinkedIn feed. Complete any 2FA now...')
        try:
            # Wait until we land on a page that is NOT the login page
            await page.wait_for_function(
                """() => {
                    const url = window.location.href;
                    return url.includes('/feed') ||
                           url.includes('/mynetwork') ||
                           url.includes('/jobs') ||
                           url.includes('/messaging') ||
                           (url.includes('linkedin.com/in/') && !url.includes('/login'));
                }""",
                timeout=300_000,
                polling=2000,
            )
            print('[success] Logged in! Current page:', page.url)
        except Exception as e:
            print(f'[error] Login timed out or failed: {e}')
            await browser.close()
            sys.exit(1)

        # Save session cookies
        cookies = await context.cookies()
        cookie_data = {'timestamp': int(time.time()), 'cookies': cookies}
        key = get_or_create_encryption_key()
        fernet = Fernet(key)
        encrypted = fernet.encrypt(json.dumps(cookie_data).encode())
        cookie_file = sessions_dir / 'linkedin_cookies.json'
        cookie_file.write_bytes(encrypted)
        print(f'[success] Session saved to {cookie_file}')
        print('[done] You can now use the MCP tools without logging in again (valid ~30 days).')

        await browser.close()


if __name__ == '__main__':
    asyncio.run(login())
