---
name: review-and-post
description: "Review the content queue and publish approved LinkedIn posts. Use when: posting content, publishing from queue, reviewing draft posts."
---

# Review and Post

## When to Use

- Nived says "post content", "post from queue", or "publish a post"
- There are `approved` entries in `data/content_queue.json` ready to publish

## Procedure

1. Read `data/content_queue.md` and list all entries with an unchecked checkbox `- [ ]`.
2. For each draft, display to Nived:
   - **Topic**: the topic field
   - **Body**: the full post text
   - **Scheduled time**: when it's set to post
3. Ask Nived which entries to **approve**, **edit**, or **skip**.
4. For approved entries, apply the formatting from [post-template.md](./assets/post-template.md) if the post doesn't already follow the structure.
5. Call MCP `interact_with_linkedin_post` (action: "share") with the final text.
6. Update the entry in `data/content_queue.md`:
   - Tick the checkbox: `- [x] Posted`
   - Add `**Posted at:** <timestamp>` below the checkbox
7. Confirm to Nived what was posted and when.

## Error Handling

- If the session is expired, call `login_linkedin_secure` first and retry.
- If posting fails, keep the entry as `approved` (don't mark `posted`) and report the error.
