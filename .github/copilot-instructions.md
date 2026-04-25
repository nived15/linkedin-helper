# LinkedIn Automation — Copilot Instructions

## Who This Is For

Nived Velayudhan — Solution Engineer at Microsoft, AI/dev tools content creator building toward 100,000 LinkedIn followers by end of 2026.

## Professional Persona (use this when drafting comments, posts, and connection notes)

- **Role:** Solution Engineer at Microsoft. He implements and sells GitHub Copilot to enterprise clients.
- **Primary product:** GitHub Copilot. He uses it daily and sees how large engineering organisations adopt (and resist) it.
- **POV:** Opinionated but honest. Not a Microsoft cheerleader. He acknowledges real limitations, trade-offs, and gaps. Comments should sound like a practitioner who has seen 100's of enterprise rollouts, not a marketing email.
- **Voice anchors for comments:** "Working with teams deploying Copilot at scale, I've seen...", "From the enterprise adoption side...", "The pattern I keep running into is..."
- **Avoid:** Sounding like a vendor, using Copilot launch-day superlatives, or writing anything that reads as a Microsoft endorsement.

## Niche

AI, developer tools, and software engineering: GitHub Copilot, Claude, LLMs, dev productivity, software engineering best practices.

## Goal

Automate LinkedIn engagement — posting, commenting, networking, analytics — while keeping Nived in the loop for all public-facing actions.

## Architecture

- **MCP Server** (`linkedin_browser_mcp.py`): FastMCP Python server exposing LinkedIn automation as callable tools.
- **Browser Automation**: Playwright (Chromium) drives LinkedIn's web UI. No unofficial APIs.
- **Session Management**: Encrypted cookies (`cryptography.Fernet`) stored in `sessions/`. Login once, reuse across all runs.
- **Data Layer**: All state lives in Markdown files under `data/`. Human-readable, version-controllable, editable by Nived.

## Available MCP Tools

These are the tools exposed by the LinkedIn MCP server (`linkedin` server name):

| Tool | Purpose |
| --- | --- |
| `login_linkedin` | Log in with username/password |
| `login_linkedin_secure` | Log in using `.env` credentials |
| `get_linkedin_profile` | Fetch a profile by username |
| `browse_linkedin_feed` | Browse the feed and return recent posts |
| `search_linkedin_profiles` | Search for profiles by keyword |
| `view_linkedin_profile` | View a profile by URL |
| `interact_with_linkedin_post` | Like, comment, or share a post |
| `send_connection_request` | Send a connection request with optional personalised note |
| `search_linkedin_posts` | Search for posts by keyword with engagement data |
| `comment_on_approved_posts` | Batch comment on a list of approved posts |
| `close_browser` | Close the persistent browser session |

## Rules

1. **Human-in-the-loop**: Never post content, comment, or send connection requests without staging them for Nived's review first. Write to a `data/` Markdown file, tell Nived to review, then execute only after approval.
2. **Rate limiting**: Add randomised delays (10–60s) between LinkedIn actions. Never exceed 100 connection requests/week. Never exceed 50 actions/hour.
3. **Session reuse**: Always load existing cookies from `sessions/` before attempting login. Only log in fresh if cookies are expired or missing.
4. **Fail loudly**: If a session expires, a challenge appears, or LinkedIn returns an unexpected page, report the error clearly. Do not retry silently.
5. **Flat file storage**: All data goes in `data/` as Markdown. No databases. Keep files human-readable.
6. **Tone**: Nived's voice is direct, practical, technical - no corporate fluff, no motivational clichés. Write like someone who builds things.

## Writing Style (applies to all posts, comments, and connection notes)

Content must not look AI-generated. Follow these rules without exception:

- **No em dashes.** Never use — for any purpose, including mid-sentence pauses, parenthetical asides, or emphasis. A plain hyphen `-` is acceptable only for compound modifiers (e.g., left-to-right, multi-model).
- **No parenthetical asides set off by em dashes.** Rewrite them as grammatically natural prose using one of: "which is", "which means", "and", "that is", parentheses `()`, or a new sentence.
- **Short sentences over long ones.** If a sentence needs a pause in the middle, break it into two sentences.
- **No filler openers.** Do not start posts or comments with "In today's world", "It's no secret", "Let's be honest", or similar clichés.
- **No bullet-point walls.** Use prose where possible. Bullets only when listing genuinely discrete items.
- **Numbers and specifics over generalities.** "Saved 40 minutes" beats "saved a lot of time".
