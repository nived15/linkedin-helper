---
name: voice-engagement
description: "Monitor and engage with top AI/dev influencers on LinkedIn. Use when: checking influencer posts, engaging niche leaders, building visibility with top voices."
---

# Voice Engagement

## When to Use

- Nived says "engage with top voices", "check influencers", or "engage niche leaders"
- Running the daily influencer engagement cycle

## Procedure

1. Load `data/top_voices.json` — the list of tracked profiles. If the file doesn't exist, create it from [voices-template.json](./assets/voices-template.json).
2. Filter to voices where `last_checked` is more than 24 hours ago (or null).
3. For each voice:
   - Call MCP `view_linkedin_profile` with their `profile_url`
   - Fetch their last 3 posts (via profile or feed browsing)
   - Filter to posts from the last 7 days where comment count < 50
4. For qualifying posts, draft a comment:
   - Reference the voice's specific niche tags from `data/top_voices.json`
   - Follow the comment formula: acknowledge their point → add a concrete insight → ask a question
   - Keep it to 2–3 sentences
5. Display all staged comments to Nived for approval.
6. On approval:
   - Call MCP `interact_with_linkedin_post` (action: `like`) for each post
   - Call MCP `interact_with_linkedin_post` (action: `comment`) with the approved text
   - Add a randomised delay (15–45s) between each action
7. Update `data/top_voices.json` — set `last_checked` to the current timestamp for each processed voice.

## Managing the Voices List

Nived can edit `data/top_voices.json` directly to add or remove people. The starter list in `assets/voices-template.json` is only used for initial setup.
