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


async def login():
    username = os.getenv('LINKEDIN_USERNAME', '').strip()
    password = os.getenv('LINKEDIN_PASSWORD', '').strip()

    if not username or not password:
        print('[error] LINKEDIN_USERNAME or LINKEDIN_PASSWORD not set in .env')
        sys.exit(1)

    sessions_dir = Path(__file__).parent / 'sessions'
    sessions_dir.mkdir(exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, slow_mo=100)
        context = await browser.new_context(viewport={'width': 1280, 'height': 800})
        page = await context.new_page()

        print('[info] Navigating to LinkedIn login...')
        await page.goto('https://www.linkedin.com/login', wait_until='domcontentloaded')

        if 'feed' in page.url:
            print('[info] Already logged in!')
        else:
            try:
                await page.fill('#username', username)
                await page.fill('#password', password)
                print('[info] Credentials pre-filled. Submitting...')
                await page.click('button[type="submit"]')
            except Exception as e:
                print(f'[warning] Could not auto-fill: {e}. Please log in manually in the browser.')

            print('[info] Waiting for LinkedIn feed (up to 5 minutes). Complete any 2FA in the browser...')
            try:
                await page.wait_for_url('**/feed/**', timeout=300_000)
                print('[success] Logged in!')
            except Exception as e:
                print(f'[error] Login timed out or failed: {e}')
                await browser.close()
                sys.exit(1)

        # Save session
        cookies = await context.cookies()
        cookie_data = {'timestamp': int(time.time()), 'cookies': cookies}
        key = get_or_create_encryption_key()
        fernet = Fernet(key)
        encrypted = fernet.encrypt(json.dumps(cookie_data).encode())
        cookie_file = sessions_dir / 'linkedin_cookies.json'
        cookie_file.write_bytes(encrypted)
        print(f'[success] Session saved to {cookie_file}')
        print('[done] You can now use the MCP tools without logging in again (valid 24h).')

        await browser.close()


if __name__ == '__main__':
    asyncio.run(login())
