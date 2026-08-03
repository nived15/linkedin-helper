# Smoke Test — Error Log & Fix Tracker

Tested: 2026-08-03  
Branch: `nived15-smoke-test-error-log`

---

## Test Scenarios

| # | Scenario | Status |
|---|----------|--------|
| 1 | Login once, session persists beyond 3 days | ❌ Bug fixed (BUG-01) |
| 2 | Search "Solution Engineers at Microsoft in UAE", go to next page | ❌ Bug fixed (BUG-04) |
| 3 | Scrape profile data for 3-4 users | ✅ Works |
| 4 | Send 1 connection request with a message | ✅ Works |
| 5 | Follow 1 profile | ❌ Bug fixed (BUG-03) |
| 6 | Like and comment on 1 post | ✅ Works |

---

## BUG-01 — Session cookie TTL is 24 hours (blocks "no re-login after 3 days")

**File:** `linkedin_mcp/browser/session.py`, line 24  
**Root cause:** `SESSION_COOKIE_TTL_SECONDS = 86400` (24 hours). Any call to
`load_cookies()` after the 24-hour mark deletes the cookie file and returns
`False`, forcing a full re-login even on a fresh token.  
**Impact:** Login required every day. Completely blocks the "login once, valid for
weeks" requirement.  
**Fix:** Raise TTL to 30 days (`2_592_000` seconds). LinkedIn session cookies
(particularly `li_at`) are valid for around 1 year in practice; 30 days is a
conservative cap that still satisfies the "3 days no re-login" requirement.  
**Also fixed:** `login_once.py` line 95 printed "valid 24h" — updated to "valid
~30 days" so it no longer misleads.  
**Status:** ✅ Fixed in this branch.

---

## BUG-02 — `login_once.py` says "valid 24h" (misleading documentation)

**File:** `login_once.py`, line 95  
**Root cause:** Hard-coded string `"valid 24h"` in the success message.  
**Impact:** Misleads Nived into thinking sessions expire daily even after BUG-01
is resolved.  
**Fix:** Updated print statement to match the new 30-day TTL.  
**Status:** ✅ Fixed alongside BUG-01.

---

## BUG-03 — "Follow profile" action entirely missing

**Root cause:** The `profile_follow` verb was never implemented. It is absent from:
- `linkedin_mcp/browser/selectors.py` — no follow button selector
- `linkedin_mcp/core/config.py` — not in `HARD_CEILINGS`
- `linkedin_mcp/executors/contract.py` — not in `_ACTIONS`
- `linkedin_mcp/executors/linkedin.py` — no executor function
- `linkedin_browser_mcp.py` — no `follow_linkedin_profile` tool

**Impact:** Calling `action_enqueue_adhoc(action="profile_follow", ...)` returns
`"'profile_follow' is not a registered action"`. There is no way to follow a
profile at all.  

**Fix:** Added all five missing pieces:
1. `follow_button` / `follow_button_more_menu` selectors in `selectors.py`
2. `profile_follow` ceiling (daily: 80) in `config.py`
3. `AdHocAction` entry in `contract.py`
4. `profile_follow()` executor in `executors/linkedin.py`
5. `follow_linkedin_profile()` MCP tool in `linkedin_browser_mcp.py`

**Status:** ✅ Fixed in this branch.

---

## BUG-04 — `profile_search` has no pagination ("next page" impossible)

**Root cause:** The search URL is built as:
```
https://www.linkedin.com/search/results/people/?keywords=<query>
```
No `start` parameter is appended, so only page 1 (up to 10 results) is ever
loaded. The `count` parameter just clips those 10; it never scrolls to page 2.

**Impact:** Searching "Solution Engineers at Microsoft in UAE" and asking for the
next page silently returns the same 10 results, or fewer if the DOM has under 10
cards. There is no way to walk through pages of results.

**Fix:**
- Added optional `page` field to `profile_search` in `contract.py` (default `1`).
- `validated_payload()` in `tools/actions.py` now passes `page` through.
- The `profile_search` executor in `executors/linkedin.py` appends
  `&start={(page-1)*10}` to the search URL.

**Status:** ✅ Fixed in this branch.

---

## BUG-05 — No `.env` file (setup / credentials)

**Root cause:** No `.env` file exists in the repo root. The server warns on
startup: `No .env file found`.  
**Impact:** `login_linkedin_secure()` returns `"Missing LinkedIn credentials in
environment"`. `get_or_create_encryption_key()` silently falls back to a freshly
generated key on every process start, which makes saved cookies unreadable across
restarts.  
**Fix (manual setup required by Nived):**
```
# .env
LINKEDIN_USERNAME=your@email.com
LINKEDIN_PASSWORD=your_password
COOKIE_ENCRYPTION_KEY=<key printed by login_once.py on first run>
```
Run `python login_once.py` once from the terminal. It opens a visible browser,
pre-fills credentials, waits up to 5 minutes for 2FA, then writes the encrypted
cookie file and prints the encryption key to save in `.env`.  
**Status:** 📋 Documented — manual step, no code change needed.

---

## Summary of code changes

| File | Change |
|------|--------|
| `linkedin_mcp/browser/session.py` | TTL: 86400 → 2592000 (30 days) |
| `login_once.py` | Print message: "valid 24h" → "valid ~30 days" |
| `linkedin_mcp/browser/selectors.py` | Added `follow_button`, `follow_button_more_menu` |
| `linkedin_mcp/core/config.py` | Added `profile_follow` to `HARD_CEILINGS` |
| `linkedin_mcp/executors/contract.py` | Added `profile_follow` `AdHocAction`; added `page` to `profile_search` |
| `linkedin_mcp/executors/linkedin.py` | Added `profile_follow()` executor; `profile_search` uses `&start=` for pagination |
| `linkedin_mcp/tools/actions.py` | `validated_payload` passes `page` for `profile_search` |
| `linkedin_browser_mcp.py` | Added `follow_linkedin_profile` MCP tool |
