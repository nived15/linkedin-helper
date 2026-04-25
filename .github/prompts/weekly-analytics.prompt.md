---
description: "Generate a weekly growth report with follower stats, post performance, and network growth"
agent: "analytics"
tools: [read, edit, linkedin/*]
---

Generate this week's growth report:

1. Use MCP tools to fetch current follower count, profile views, and post impressions for the past 7 days.
2. Read the most recent file from `data/analytics/` to compute week-over-week deltas.
3. Read `data/content_queue.json` to summarise posts published this week and their engagement.
4. Read `data/network_growth.json` to get connection requests sent, accepted, and acceptance rate.
5. Populate the report using the `growth-report` skill's `assets/report-template.md` structure.
6. Save to `data/analytics/YYYY-MM-DD.json` (raw) and `data/analytics/YYYY-MM-DD-summary.md` (readable).
7. Display the summary in chat. Highlight the top-performing post and one recommended focus for next week.
