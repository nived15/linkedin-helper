---
name: trending-workflow
description: "Discover trending AI/dev posts and execute staged engagement. Use when: finding trending posts, running engagement, staging comments for review."
---

# Trending Workflow

## When to Use

- Nived says "run trending", "find trending posts", or "stage engagement"
- Running the two-phase trending engagement cycle

## Phase 1 — Discover

1. Call MCP `browse_linkedin_feed` and use web search for keywords:
   - `GitHub Copilot`, `Claude`, `AI agents`, `LLMs`, `developer tools`, `software engineering`
2. Collect the **top 20 posts** ranked by engagement (likes + comments) from the last 48 hours.
3. For each post, draft a comment following the tone guide in [comment-template.md](./assets/comment-template.md).
4. Write all results to `data/trending_queue.json` with:
   - `status`: `staged`
   - `discovered_at`: current ISO 8601 timestamp
   - `drafted_comment`: the AI-generated comment
5. Tell Nived: "20 posts staged. Review `data/trending_queue.json` and set status to `approved` for posts you want to engage with."
6. **Stop here. Do not engage yet.**

## Phase 2 — Engage

Only run this after Nived has reviewed the queue.

1. Read `data/trending_queue.json` and collect entries with status `approved`.
2. If none are approved, tell Nived to review first and stop.
3. For each approved post:
   - Call MCP `interact_with_linkedin_post` with action `like`
   - Call MCP `interact_with_linkedin_post` with action `comment` using the drafted (or edited) comment
   - Wait `random.uniform(15, 45)` seconds between each action
4. Update each entry:
   - Set `status` to `engaged`
   - Set `engaged_at` to current ISO 8601 timestamp
5. Summarise: "Engaged with X posts — Y liked, Z commented."

## Error Handling

- If rate limit is approached (50 actions/hour), pause and warn Nived.
- If a post is no longer available, mark it as `skipped` and move on.
