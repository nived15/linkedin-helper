---
description: "Execute engagement (like + comment) on approved trending posts from the queue"
agent: "trending-topics"
tools: [read, edit, linkedin/*]
---

Run Phase 2 of the trending workflow:

1. Read `data/trending_queue.json` and collect all entries with status `approved`.
2. If none are approved, tell Nived to review the queue first and stop.
3. For each approved post:
   - Call MCP `interact_with_linkedin_post` with action `like`.
   - Call MCP `interact_with_linkedin_post` with action `comment` using the drafted (or edited) comment text.
   - Wait 15–45 seconds between each engagement action.
4. Update each entry's status to `engaged` with the current timestamp.
5. Summarise what was done: how many posts liked, how many commented on.
