---
description: "Execute engagement (like + comment) on approved trending posts from the queue"
agent: "trending-topics"
tools: [read, edit, linkedin/*]
---

Run Phase 2 of the trending workflow:

1. Read `data/trending_queue.md` and collect all entries whose checkbox is ticked (`- [x]`).
2. If none are approved, tell Nived to review the queue first and stop.
3. For each approved post:
   - Call MCP `interact_with_linkedin_post` with action `like`.
   - Call MCP `interact_with_linkedin_post` with action `comment` using the drafted (or edited) comment text.
   - Wait 15–45 seconds between each engagement action.
4. After each post, append `**Engaged at:** YYYY-MM-DD HH:MM` to that entry in the markdown file.
5. When all approved posts are done, call the `close_browser` tool.
6. Summarise what was done: how many posts liked, how many commented on.
