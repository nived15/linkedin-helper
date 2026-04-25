---
name: network-campaign
description: "Search for target profiles, generate personalised connection notes, and send connection requests. Use when: growing network, sending connection requests, running network campaign."
---

# Network Campaign

## When to Use

- Nived says "grow network", "send connection requests", or "run network campaign"
- Running a weekly network growth batch

## Procedure

1. Read `data/network_growth.json` and count requests sent in the current week (Monday–Sunday).
   - If 100 or more sent this week, **stop** and tell Nived: "Weekly cap reached (100 requests). Try again next week."
2. Calculate remaining budget: `100 - requests_sent_this_week`.
3. Call MCP `search_linkedin_profiles` with niche keywords:
   - `AI engineer`, `ML engineer`, `developer tools`, `LLM`, `software engineer`
   - Or use keywords provided by Nived as the prompt argument
4. Filter results:
   - Prefer 2nd-degree connections
   - Prefer profiles with 500+ followers
   - Exclude anyone already in `data/network_growth.json` history
5. For each candidate (max 20 per run), use MCP `view_linkedin_profile` to get profile details, then generate a personalised connection note using the formula in [note-template.md](./assets/note-template.md).
6. Display the full batch to Nived:
   - Name, headline, profile URL, and the personalised note for each
   - Ask to confirm, edit notes, or remove candidates
7. **Only after explicit approval**, send each request:
   - Log to `data/network_growth.json` with `status: pending`, `sent_at` timestamp, and the note text
8. Summarise: "Sent X connection requests. Y remaining this week."

## Error Handling

- If a profile is unavailable or the request fails, log it as `failed` and continue with the next.
- If LinkedIn shows a rate limit warning, stop immediately and report to Nived.
