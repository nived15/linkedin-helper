---
description: "Use when reading, writing, or validating data files under data/. Covers format conventions for content_queue, trending_queue, top_voices, network_growth, and analytics files."
applyTo: "data/**"
---

# Data File Conventions

All data lives in `data/` as **Markdown files** (`.md`). There are no JSON data files — markdown is the single source of truth.

## Approval Workflow

Nived ticks checkboxes (`- [ ]` → `- [x]`) directly in the markdown file, then tells the agent to execute. The agent reads the markdown, finds checked items, and acts on them.

## `data/trending_queue.md`

Each discovered post is an entry:

```markdown
## tq-NNN · Author Name

- [ ] Approve for engagement
- **URL:** <https://www.linkedin.com/in/username/>
- **Snippet:** First 200 chars of the post
- **Drafted Comment:**
  > Your drafted comment here

---
```

**States:**
- `- [ ] Approve for engagement` — not yet approved
- `- [x] Approve for engagement` — approved, ready for Phase 2
- `- [x] ~~Engaged~~` + `**Engaged at:** <timestamp>` — done

## `data/content_queue.md`

Each draft post is an entry:

```markdown
## Post: Short topic title

- [ ] Approved to post
- **Scheduled:** 2026-04-28 09:00 UTC

Full post body here, formatted as it will appear on LinkedIn.

#Hashtag1 #Hashtag2

---
```

**States:**
- `- [ ] Approved to post` — draft, not yet approved
- `- [x] Approved to post` — approved, ready to publish
- `- [x] Posted` + `**Posted at:** <timestamp>` — published

## `data/network_growth.md`

Header section shows weekly cap and count. Each connection request is an entry:

```markdown
# Network Growth

Weekly cap: 100 | Sent this week: N

---

## [Person Name](<https://www.linkedin.com/in/username/>)

- **Headline:** Their job title
- **Note sent:** Personalised note text
- **Sent at:** 2026-04-25 14:00 UTC
- **Status:** pending | accepted | ignored

---
```

## `data/top_voices.md`

Each tracked influencer is a section:

```markdown
## [Person Name](<https://www.linkedin.com/in/username/>)

- **Niche tags:** AI, LLMs, developer tools
- **Last checked:** 2026-04-25 14:00 UTC

---
```

## `data/analytics/YYYY-MM-DD-summary.md`

Human-readable weekly growth report. See the growth-report skill for the template.

## Rules

- Always use ISO 8601 timestamps in a readable format: `2026-04-25 14:00 UTC`.
- When updating a file, read it first, find the relevant entry, modify it in-place. Never overwrite the entire file.
- Wrap all URLs in angle brackets `<url>` to avoid bare URL lint errors.
- Use `---` as a horizontal rule separator between entries.
