---
description: "Draft a LinkedIn post on a given topic and add it to the content queue for review"
agent: "content-posting"
tools: [edit, read, linkedin/*]
argument-hint: "Topic or idea for the post (e.g. 'GitHub Copilot agent mode tips')"
---

Draft a LinkedIn post based on the provided topic.

1. Use the topic provided as the argument to generate a post following the format in the `review-and-post` skill's `assets/post-template.md`.
2. The post should match Nived's tone: direct, practical, technical. No corporate fluff.
3. Focus on the AI/developer tools niche — GitHub Copilot, Claude, LLMs, dev productivity.
4. Add the post to `data/content_queue.json` with status `draft` and a suggested schedule time (next Tue–Thu, 8–10am or 12pm).
5. Show the draft to Nived for review before any further action.
