---
description: "Find the top 20 trending AI/dev posts on LinkedIn and draft comments for engagement"
agent: "trending-topics"
tools: [web, read, edit, linkedin/*]
---

Run Phase 1 of the trending workflow:

1. Ensure session is active: call `login_linkedin_secure`.
2. Use MCP `search_linkedin_posts` (NOT `browse_linkedin_feed`) with these queries in sequence:
   - `"GitHub Copilot AI developer tools"`
   - `"Claude AI LLM agents software engineering"`
   - `"AI developer productivity MCP model context protocol"`
   - `"software engineering best practices AI"`
   - Adjust or add queries until you have at least 20 unique posts.
3. Prefer posts with engagement (likes or comments > 0). Skip purely promotional or job-posting content.
4. For each selected post, draft a comment following `trending-workflow` skill's `assets/comment-template.md` (Acknowledge → Insight → Question, max 3 sentences).
5. Save all results to `data/trending_queue.md` using the markdown checkbox format (see `data-files.instructions.md`).
6. Tell Nived: "X posts staged. Review `data/trending_queue.md` and tick the checkboxes for posts you want to engage with."

Do NOT engage (like/comment) on any posts in this step. That happens in `/trending-engage`.
