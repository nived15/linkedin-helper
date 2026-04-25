---
name: growth-report
description: "Generate a weekly LinkedIn growth report with follower stats, post performance, and network metrics. Use when: checking growth, weekly report, analytics, how am I growing."
---

# Growth Report

## When to Use

- Nived says "weekly report", "how am I growing", "analytics", or "growth summary"
- End of each week to track progress toward 100k followers

## Procedure

1. **Fetch current stats**: Call MCP `get_linkedin_profile` for Nived's own profile to get:
   - Current follower count
   - Profile view count (if available)
2. **Load previous report**: Read the most recent `data/analytics/YYYY-MM-DD.json` file to compute week-over-week deltas. If no previous report exists, treat all values as baseline.
3. **Post performance**: Read `data/content_queue.md` and find posted entries (marked with ~~strikethrough~~ or a `Posted` status line) within the last 7 days. For each, note the topic and any available engagement numbers.
4. **Network growth**: Read `data/network_growth.md` and compute:
   - Requests sent this week
   - Requests accepted (status changed to `accepted`)
   - Acceptance rate: `accepted / sent`
5. **Build the report**: Load [report-template.md](./assets/report-template.md) and populate each section with the data gathered.
6. **Save**:
   - `data/analytics/YYYY-MM-DD.json` — raw data as JSON
   - `data/analytics/YYYY-MM-DD-summary.md` — human-readable markdown
7. **Display**: Show the summary in chat. Call out:
   - **Top post of the week** (highest engagement)
   - **One recommended focus** for next week (data-driven — e.g. "AI agent posts got 3x more comments, double down")

## Report Sections

See [report-template.md](./assets/report-template.md) for the full structure.
