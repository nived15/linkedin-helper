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

- Add `await asyncio.sleep(random.uniform(10, 60))` between consecutive LinkedIn actions (likes, comments, connection requests).
- Never exceed 50 actions per hour or 100 connection requests per week.
- Log every action with a timestamp so rate limits can be verified from `data/` files.

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
