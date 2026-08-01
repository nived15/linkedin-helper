---
description: "Use when writing or editing Python MCP tool handlers, browser automation code, or session management logic. Covers Playwright patterns, rate limiting, error handling, and data file conventions."
applyTo: "**/*.py"
---

# LinkedIn MCP Tool Patterns

## Session Reuse

- Always call `load_cookies()` before navigating to LinkedIn. Only call `login_linkedin_secure()` if cookies are missing or expired.
- After every successful action, call `save_cookies()` to persist the session.
- If a challenge page or unexpected URL appears, raise an error immediately — do not retry silently.

## Rate Limiting

- Caps are enforced in code, never by prose or by an LLM. Call `guard_action(...)` from `linkedin_mcp.safety` at the top of every action tool, before a browser is opened, and return its result when it refuses.
- Never write a cap into a tool. `HARD_CEILINGS` in `linkedin_mcp/core/config.py` is the only place a ceiling is set, and `account_limits` rows may only tighten it.
- Never store a usage counter. Every count is a `COUNT(*)` over `actions_log` through `linkedin_mcp.audit`, which is append-only.
- Never sleep directly. Pacing between actions routes through `linkedin_mcp.browser.humanize`, so `await cooldown()` rather than `asyncio.sleep(...)`.
- A refusal is an ordinary tool result carrying a typed `RefusalReason`, and the gate has already written it to `actions_log`. Do not raise, and do not log it a second time.

## Browser Automation

- Use `headless=True` for all automated runs (non-interactive). Only use `headless=False` for debugging.
- Set a realistic user agent string. Avoid default Playwright signatures.
- Use `wait_until='networkidle'` for page loads. Set explicit timeouts (30s for navigation, 10s for element waits).
- Use CSS selectors over XPath. Prefer `data-*` attributes and ARIA labels when available.

## Error Handling

- Wrap all Playwright operations in try/except. Catch `TimeoutError` and `Error` from Playwright specifically.
- On failure, return a dict with `{"status": "error", "message": "descriptive message"}` — never raise unhandled exceptions from MCP tools.
- If LinkedIn shows a login wall or challenge, return `{"status": "error", "message": "Session expired: ..."}` so agents know to re-auth.

## Data Files

- All tool results that produce state (posts queued, comments staged, connections sent) must be written to the appropriate `data/*.md` file in markdown format.
- Always read the existing file first, append/update, then write back — never overwrite the entire file blindly.
- Use checkbox format (`- [ ]` / `- [x]`) for approval states. Add `**Posted at:**`, `**Sent at:**`, and `**Engaged at:**` timestamps inline after completing actions.

## MCP Tool Structure

Every tool handler should follow this pattern:

```python
@mcp.tool()
async def tool_name(param: str, ctx: Context) -> dict:
    """One-line description of what this tool does."""
    try:
        # 1. Load session
        # 2. Perform action with delays
        # 3. Write results to data/ file
        # 4. Save session
        return {"status": "success", "data": result}
    except Exception as e:
        return {"status": "error", "message": str(e)}
```
