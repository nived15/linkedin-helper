---
description: "Find the top 20 trending AI/dev posts on LinkedIn and draft comments for engagement"
agent: "trending-topics"
tools: [web, read, edit, linkedin/*]
---

Run Phase 1 of the trending workflow:

1. Use the MCP `browse_linkedin_feed` tool and web search to find the top 20 trending posts in the AI/developer tools space (GitHub Copilot, Claude, AI agents, LLMs, dev productivity, software engineering).
2. Rank posts by engagement (likes + comments in last 48h).
3. For each post, draft a comment following the `trending-workflow` skill's `assets/comment-template.md` tone guide.
4. Save all results to `data/trending_queue.json` with status `staged`.
5. Tell Nived: "20 posts staged. Review `data/trending_queue.json` and set status to `approved` for posts you want to engage with."

Do NOT engage (like/comment) on any posts in this step. That happens in `/trending-engage`.
