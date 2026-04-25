---
description: "Find target profiles and send personalised connection requests"
agent: "network-growth"
tools: [read, edit, linkedin/*]
argument-hint: "Optional: specific keywords or niche to target (defaults to AI/dev tools)"
---

Run a network growth batch:

1. Read `data/network_growth.json` and check how many requests were sent this week. Enforce the 100/week cap.
2. Use MCP `search_linkedin_profiles` with keywords: AI engineer, ML engineer, developer tools, LLM, software engineer (or the provided argument).
3. Filter to relevant profiles. Exclude anyone already in `data/network_growth.json`.
4. For each candidate (max 20 per run), generate a personalised connection note using the `network-campaign` skill's `assets/note-template.md` formula.
5. Show the batch to Nived for approval before sending anything.
6. Only after approval, send connection requests via the MCP tools.
7. Log each sent request to `data/network_growth.json`.
