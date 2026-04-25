---
description: "Find the top 20 trending AI/dev posts on LinkedIn and draft comments for engagement"
agent: "trending-topics"
tools: [web, read, edit, linkedin/*]
---

Run Phase 1 of the trending workflow. Follow these steps exactly — no extra queries, no loops.

## Step 1 — Ensure session is active
Call `login_linkedin_secure`. If it fails, stop and report the error.

## Step 2 — Search for posts (exactly 4 queries, no more)

Call `search_linkedin_posts` with `sort_by="relevance"` (the default) for each query below. Run them **one at a time** (not in parallel — LinkedIn rate-limits concurrent browser tabs). Each call returns up to 10 posts sorted by LinkedIn's relevance algorithm, which surfaces higher-engagement content first.

Required queries, in order:
1. `"GitHub Copilot AI developer tools"`
2. `"Claude AI LLM agents software engineering"`
3. `"AI developer productivity MCP model context protocol"`
4. `"software engineering best practices AI"`

**Stop after 4 queries regardless of how many posts you have.** Do not add extra queries to reach 20 — the 4 queries above yield enough candidates. Running more queries burns tokens and hits LinkedIn rate limits.

## Step 3 — Filter and rank posts

From all posts returned across the 4 queries:
- Deduplicate by `post_url`.
- **Prefer posts where `likes` or `comments` fields are non-empty** (these are the high-engagement ones the relevance sort surfaced).
- Skip posts that are: purely promotional, job postings, certification announcements, or event invites.
- Select the best 20 (or fewer if fewer pass the filter). Rank engagement signals: posts with both likes and comments > posts with only one signal > posts with no engagement data.

## Step 4 — Draft comments

For each selected post, draft a comment using the Acknowledge → Insight → Question formula from `assets/comment-template.md`:
- Max 3 sentences.
- Reference something specific from the post snippet.
- Add a concrete insight from real experience (name a tool, a number, a real outcome).
- End with a focused question that invites a reply.
- No em dashes. No "Great post!" openers. No emojis unless the original post uses them heavily.

## Step 5 — Write to data/trending_queue.md

**Read the existing file first.** Find the highest existing `tq-NNN` number, then number new entries starting from `tq-(N+1)`.

Write each entry in this exact format:
```markdown
## tq-NNN · Author Name

- [ ] Approve for engagement
- **URL:** <post_url>
- **Snippet:** First 200 chars of post content
- **Drafted Comment:**
  > Your drafted comment here

---
```

Append the new entries at the bottom of the file. Do not overwrite or remove existing entries.

## Step 6 — Report to Nived

Tell Nived: "X posts staged (Y with engagement signals, Z without). Review `data/trending_queue.md` and tick the checkboxes for posts you want to engage with, then run Phase 2."

Do NOT engage (like/comment) on any posts in this step.
