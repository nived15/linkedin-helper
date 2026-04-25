---
name: trending-workflow
description: "Discover trending AI/dev posts and execute staged engagement. Use when: finding trending posts, running engagement, staging comments for review."
---

# Trending Workflow

## When to Use

- Nived says "run trending", "find trending posts", or "stage engagement"
- Running the two-phase trending engagement cycle

## Phase 1 — Discover

1. Call MCP `search_linkedin_posts` (not `browse_linkedin_feed`) with targeted keywords:
   - `"GitHub Copilot AI developer tools"`
   - `"Claude AI LLM agents software engineering"`
   - `"AI developer productivity MCP model context protocol"`
   - Run more queries if needed to reach 20 unique posts.
2. Prefer posts that have actual engagement counts (likes or comments > 0). Skip job postings and purely promotional content.
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
