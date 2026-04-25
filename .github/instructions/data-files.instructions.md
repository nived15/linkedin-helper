---
description: "Use when reading, writing, or validating JSON data files under data/. Covers schema conventions for content_queue, trending_queue, top_voices, network_growth, and analytics files."
applyTo: "data/**"
---

# Data File Schema Conventions

All agent state lives in `data/` as JSON files. Every file must be human-readable and directly editable by Nived.

## `data/content_queue.json`

Array of post entries. Each entry:

```json
{
  "id": "uuid",
  "topic": "Short topic description",
  "body": "Full post text",
  "hashtags": ["#AI", "#GitHubCopilot"],
  "scheduled_time": "2026-04-28T09:00:00Z",
  "status": "draft | approved | posted | skipped",
  "created_at": "2026-04-25T14:00:00Z",
  "posted_at": null
}
```

## `data/trending_queue.json`

Array of trending post entries. Each entry:

```json
{
  "id": "uuid",
  "post_url": "https://www.linkedin.com/feed/update/...",
  "author": "Author Name",
  "snippet": "First 200 chars of the post",
  "engagement": {"likes": 150, "comments": 42},
  "drafted_comment": "AI-generated comment text",
  "status": "staged | approved | engaged | skipped",
  "discovered_at": "2026-04-25T14:00:00Z",
  "engaged_at": null
}
```

## `data/top_voices.json`

Array of tracked influencer profiles:

```json
{
  "name": "Person Name",
  "profile_url": "https://www.linkedin.com/in/username",
  "niche_tags": ["AI", "LLMs", "developer tools"],
  "last_checked": "2026-04-25T14:00:00Z"
}
```

## `data/network_growth.json`

Object with weekly tracking:

```json
{
  "weekly_cap": 100,
  "requests": [
    {
      "profile_url": "https://www.linkedin.com/in/username",
      "name": "Person Name",
      "note_sent": "Personalised connection note text",
      "sent_at": "2026-04-25T14:00:00Z",
      "status": "pending | accepted | ignored"
    }
  ]
}
```

## `data/analytics/YYYY-MM-DD.json`

Weekly raw data snapshot:

```json
{
  "report_date": "2026-04-25",
  "followers": {"current": 5200, "previous": 5050, "delta": 150},
  "profile_views": 320,
  "post_impressions": 12500,
  "posts_published": 4,
  "top_post": {"topic": "...", "impressions": 4200, "likes": 85, "comments": 23},
  "connections": {"sent": 18, "accepted": 12, "rate": 0.67},
  "recommended_focus": "Double down on AI agent content — highest engagement this week"
}
```

## Rules

- Always use ISO 8601 timestamps with timezone (`Z` for UTC).
- Use `null` for empty optional fields, never omit the key.
- Status fields are lowercase strings — valid values are documented per schema above.
- When updating a file, read it first, modify the relevant entry, then write back. Never overwrite the entire file.
