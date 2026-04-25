---
name: trending-workflow
description: "Discover trending AI/dev posts and execute staged engagement. Use when: finding trending posts, running engagement, staging comments for review."
---

# Trending Workflow

## When to Use

- Nived says "run trending", "find trending posts", or "stage engagement"
- Running the two-phase trending engagement cycle

## Phase 1 — Discover

### How to design queries

LinkedIn content search is not Google. Long keyword-stuffed queries act as AND filters and shrink the result set. Short, specific queries that match the exact language people use in posts work far better.

Rules:
- Keep queries to 2-4 words
- Use exact product names or feature names people actually type in posts (`GitHub Copilot`, `Copilot agent mode`, not `GitHub Copilot AI coding productivity tool`)
- Mix query types: broad (product name) + specific feature + debate/comparison + audience angle
- One query per topic, no redundancy

Example query set for **GitHub Copilot**:
  - `"GitHub Copilot"` — catches all Copilot discussion, broad
  - `"Copilot agent mode"` — hot 2026 feature
  - `"GitHub Copilot Cursor"` — comparison/debate posts (highest engagement)
  - `"Copilot enterprise"` — enterprise angle, matches Nived's SE@Microsoft role

Example query set for **AI agents / MCP**:
  - `"MCP server"` — short, matches actual post language
  - `"model context protocol"` — catches explainers and opinion posts
  - `"AI agent workflow"` — practical posts on agentic dev
  - `"Claude Code"` — Claude's coding agent, often discussed alongside MCP

1. Call MCP `search_linkedin_posts` with `sort_by="relevance"` (default). Run **exactly 4 queries**, one at a time. Stop at 4 regardless of result count — more queries waste tokens and hit LinkedIn rate limits.
2. Prefer posts that have actual engagement counts (likes or comments > 0). Skip job postings, certification announcements, and purely promotional content.
3. For each post, draft a comment following the tone guide in [comment-template.md](./assets/comment-template.md).
4. Write all results to **`data/trending_queue.md`**. Each entry in the markdown has:
   - A checkbox `- [ ] Approve for engagement` (Nived ticks to approve)
   - Author, URL, snippet, and drafted comment
5. Tell Nived: "X posts staged. Review `data/trending_queue.md`, tick the checkboxes for posts you want to engage with, then tell me to run Phase 2."
6. **Stop here. Do not engage yet.**

## Phase 2 — Engage

Only run this after Nived has reviewed the queue.

1. Read `data/trending_queue.md` and collect entries where the checkbox is **ticked** (`- [x] Approve for engagement`).
2. If no checkboxes are ticked, tell Nived to review `data/trending_queue.md` first and stop.
3. Like each approved post:
   - Call MCP `interact_with_linkedin_post` with action `like`
   - Wait `random.uniform(15, 45)` seconds between likes
4. Post all approved comments in one batch:
   - Call MCP `comment_on_approved_posts` with the list of `{post_url, comment}` pairs parsed from `data/trending_queue.md`
5. Update `data/trending_queue.md` for each engaged entry:
   - Change `- [x] Approve for engagement` to `- [x] ~~Engaged~~`
   - Add `**Engaged at:** <timestamp>` below the checkbox
6. Summarise: "Engaged with X posts — Y liked, Z commented."

## Error Handling

- If rate limit is approached (50 actions/hour), pause and warn Nived.
- If a post is no longer available, mark it as `skipped` and move on.
