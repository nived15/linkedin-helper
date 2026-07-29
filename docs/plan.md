# Master Plan: MCP-Native Linked Helper Equivalent

**Repo:** `nived15/linkedin-helper`
**Objective:** Turn a prompt-orchestrated, 11-tool Playwright wrapper into a durable campaign automation platform with Linked Helper 2 feature parity, controlled through MCP.

## Confirmed Scope Decisions

| Decision | Choice |
| --- | --- |
| Account model | Single account now, multi-account-ready schema (Accounts table + per-account limits from day one) |
| Storage | SQLite is the system of record; `data/*.md` become generated review views |
| Approval gate | Approve the campaign definition once, then autonomous execution inside safety caps. Per-lead approval only for AI-generated free text |
| Execution | Separate long-lived `worker.py` daemon owns the browser; MCP server is a thin control plane over SQLite |
| External services | Free LinkedIn only. Webhooks out. No Sales Navigator, Recruiter, or paid email enrichment |

---

# STEP 1 — Current Codebase Audit

## 1.1 MCP Architecture

Everything lives in one 1,446-line file, `linkedin_browser_mcp.py`.

- **Server:** `FastMCP("linkedin")`, `stdio` transport only. No HTTP/SSE, no auth.
- **Tools (11):** `close_browser`, `login_linkedin`, `login_linkedin_secure`, `get_linkedin_profile`, `browse_linkedin_feed`, `search_linkedin_profiles`, `view_linkedin_profile`, `interact_with_linkedin_post`, `send_connection_request`, `search_linkedin_posts`, `comment_on_approved_posts`.
- **Resources: zero. Prompts: zero.** The `.github/prompts/*.md` slash commands are Copilot-side files, not MCP prompts, so any other MCP client sees nothing.
- **Orchestration lives in Markdown, not code.** Five agents, five skills, three instruction files. The LLM is simultaneously the scheduler, the state machine, and the safety enforcer.
- **`report_progress()` is decorative.** It computes a progress fraction, logs it, and never calls `ctx.report_progress()`. Clients receive no progress notifications.
- **Context calls are not awaited.** `ctx.info(...)` and `ctx.error(...)` are called synchronously throughout. On FastMCP versions where these are coroutines this silently produces un-awaited coroutine warnings and no client-visible logging.
- **No tool returns typed errors.** Every failure is a free-text `{"status": "error", "message": str(e)}`, so a caller cannot distinguish "session expired" from "selector not found" from "rate limited" without string matching.

## 1.2 Automation Mechanism

- Playwright async Chromium via a class-level singleton `BrowserSession` (`_playwright`, `_browser`, `_context`, `_page` are class attributes). `__aexit__` is deliberately a no-op so the browser survives across tool calls; `close_browser` tears it down.
- One browser, one context, one page, always visible (`headless=False`).
- **Anti-detection is essentially absent:**
    - Single hard-coded user agent `Chrome/96.0.4664.110`, a late-2021 build. Advertising a four-year-old Chrome from a current Chromium binary is itself a fingerprint mismatch.
    - `--disable-blink-features=AutomationControlled` is the only stealth flag. No `navigator.webdriver` patch, no canvas/WebGL noise, no locale/timezone alignment, no proxy support.
    - `new_context()` is ephemeral, so localStorage and IndexedDB are wiped every launch while cookies are restored. That mismatch is itself detectable.
- **Direct URL navigation everywhere.** `page.goto(profile_url)` is used for every profile visit. Linked Helper explicitly avoids this and types into the LinkedIn search bar instead, because direct URL loads are the pattern most strongly associated with forced logout.
- **Selectors are stale.** `.reusable-search__result-container`, `.pv-top-card--list .text-heading-xlarge`, `#experience-section .pv-entity__summary-info`, `.pv-education-entity` are all 2021-era LinkedIn class names. The newest tool, `search_linkedin_posts`, already abandons selectors for a heuristic DOM-walking extractor that climbs 25 parent levels looking for a text block between 300 and 12,000 characters. That is direct evidence the selector layer has already rotted.
- **Challenge handling is a substring check.** `'login' in page.url` or `'authwall' in page.url`. Checkpoint, captcha, and "unusual activity" interstitials are not detected.

## 1.3 State & Data Persistence

- **Credentials:** plaintext `LINKEDIN_USERNAME` / `LINKEDIN_PASSWORD` in `.env`.
- **Cookies:** `sessions/linkedin_cookies.json`, Fernet-encrypted, hard 24-hour TTL. `COOKIE_ENCRYPTION_KEY` is auto-generated into the same `.env` that sits beside the ciphertext, so this is obfuscation, not security.
- **Real bug:** `save_cookies()` writes to `Path(__file__).parent / 'sessions'` (absolute) while `load_cookies()` reads `sessions/{platform}_cookies.json` (relative to cwd). Whenever the MCP client launches the server from a different working directory, saves succeed and loads silently fail, and the error handler then deletes the cookie file. Symptom: unexplained repeated logins.
- **Everything else: nothing.** The server writes no data files at all. `data/` contains only `analytics/.gitkeep`. Every Markdown file the README documents is authored by the LLM at runtime.
- `data/` is in `.gitignore`, so there is no committed state of any kind.
- No lead store, no action ledger, no campaign state, no dedupe index, no counters, no message history.

## 1.4 Safety & Rate Limiting

**Enforced in code:**
- `asyncio.sleep(4)` between batch comments.
- `editor.type(comment_text, delay=25)`.
- A 300-character check on connection notes.
- `import random` at the top of the file. `random` is never used anywhere.

**Not enforced anywhere:**
- Randomised inter-action delays, daily caps, weekly caps, hourly caps, working hours, ramp-up for new accounts, dedupe, blacklist, pending-invite ceiling, reply detection, auto-pause on LinkedIn warnings.

**This is the single most important finding.** The "100 connection requests per week" and "50 actions per hour" limits exist only as English sentences in `copilot-instructions.md` and `network-campaign/SKILL.md`. The enforcement mechanism is: ask the LLM to count entries in a Markdown file that the same LLM writes. A compacted context window, a fresh session, a skipped skill, or a user who says "just send them all" bypasses every cap with zero friction. The safety story is currently a prompt, and prompts are not a control.

---

# STEP 2 — Target Benchmark: Linked Helper 2

Researched against `support.linkedhelper.com` and `linkedhelper.com`. Items that could not be confirmed from a primary source are marked `[unverified]`.

## 2.1 Campaign & Sequence Engine

A campaign is a named container holding an ordered **Workflow** of Action steps. Each action owns sub-lists: Queue, Processing, Successful, Failed, Replied, Skipped, Excluded.

**Verified action catalogue (from the Plug-in Store):**

| Category | Actions |
| --- | --- |
| Outreach | Invite 2nd/3rd contacts, Message to 1st connections, InMail to 2nd/3rd, Message to group members, Message to event attendees |
| Data | Visit and extract profiles, Data Enrichment, Find Profile Emails, Scrape messaging history, Organizations extractor, Employees extractor |
| Flow control | Check for replies, Filter contacts out of my network (keep 1st only), Delay between actions, **AI ICP Detection** (routes matched to Success, unmatched to Failed) |
| Engagement | Follow / unfollow, Like and comment posts and articles, Boost post (@-mention), Endorse my contacts |
| Growth | Invite to follow organization, Invite person to event, Invite to group, Accept invites |
| Cleanup | Remove from 1st connections, Sent invites canceller |
| Egress | Send person to webhook, Send organization to webhook, Send person to external CRM, Send person to Snov.io campaign |

**Sequence structure:** strictly linear, top-to-bottom. There is **no visual branching**. Conditional behaviour is achieved by filter-style steps that drop leads out of the flow:
- `Check for replies` moves repliers to a terminal Replied sub-list and advances non-repliers after a configurable wait or never.
- `Filter contacts out of my network` passes only 1st-degree connections, so it acts as the accept-gate between Invite and Message.
- `IF | THEN | ELSE` exists only inside message templates and only tests variable presence, not values. It changes text, never routing.

**Scheduling:** the desktop app must stay open. Priority is bottom-up, meaning the action furthest along the workflow with queued profiles runs first. Actions interleave in configurable "bunches" (for example 10 profiles) before yielding to the next action.

**Personalisation:** `{firstName}`, `{lastName}`, `{company}`, `{position}`, `{industry}`, `{mutualFirstFullName}`, `{mutualSecondFullName}`, `{mutualTotal}`, `{memberId}`, `{publicId}`, `{lhid}`; unlimited `{cs_*}` custom variables importable from CSV; spintax `{a|b|c}`; whole-message Variations split evenly across the queue; image attachments and Hyperise personalised images.

## 2.2 Lead Harvesting

**Free LinkedIn sources:** People Search with all filters, My Network, School Alumni, Company employees page, Event attendees, Group members, Who viewed your profile, Sent invitations, Post likers and commenters, Your followers, Profiles you follow, Company page followers, Companies search.

**Gated sources (out of scope for us):** Sales Navigator lead search, saved lists, saved searches, account search; Recruiter search and projects.

**External:** CSV/TXT of profile URLs, LH-format CSV with checksum, saved HTML page, pasted URLs, another campaign's list.

**Extracted fields:** identity (`fullName`, `firstName`, `lastName`, `memberId`, `publicId`, `hashId`, `avatar`), platform IDs, contact info for 1st-degree only (`email`, `workEmail`, `personalEmail`, `birthday`), professional (`headline`, `organizationTitle`, `organizationName`, `organizationStart/End`, `industry`, `summary`, `locationName`), network (`memberDistance`, `connectionCount`, `followerCount`, connection date), badges (`Premium`, `Influencer`, `OpenLink`, `JobSeeker`, `Hiring`), plus full experience, education, skills, languages. Contact info cached 21 days, positions 14 days.

**Limits:** basic search caps at roughly 1,000 results (100 pages x 10). Profile visits by direct URL are hard-capped at 40 per 24 hours.

**Enrichment:** LH's own credit-based database (620 credits/month Standard, 3,100 Pro), plus Snov.io and Apollo.io integrations. Fallback chain tries LH first to conserve third-party credits.

## 2.3 Safety & Compliance Architecture

**Documented recommended limits (account older than one year):**

| Activity | Recommended per 24h |
| --- | --- |
| Invites | 50 |
| Messages to 1st | 150 |
| Overall actions | 150 (global ceiling) |
| Endorsements | 60 |
| Load profile by direct URL | 40 (hard-coded) |
| Boost post mentions | 100 |
| Follows | 150 |
| Weekly invites | under 200 |
| Pending invites | 200 to 500 max |
| New accounts | 10 to 15 invites/day, ramp +5 to +10 every 10 days |

**Randomisation:** Smart Daily Limit Adjustment applies a default ±10% band to the configured cap, so a 50-invite limit produces 45 to 50 on any given day. Daily start time gets a random offset. Micro-step delays come from configurable ranges with FAST and SAFE presets.

**Human emulation:** per-day working-hours windows with timezone; **in-page navigation** (type the name into LinkedIn's search bar and click the result rather than loading the URL); Type / Paste / Random text entry modes; randomised micro-step timeouts; spintax and variations; randomised daily volume; per-instance browser fingerprint randomisation; isolated cookie and cache store per account.

**Browser:** desktop Electron app with an embedded Chromium. Not a Chrome extension, so there is no extension ID to scan for. No JavaScript injection into LinkedIn's DOM. No Voyager API calls. `li_at` and `JSESSIONID` stay local and are never sent to LH servers.

**Proxy:** HTTP/HTTPS/SOCKS/SOCKS5, IPv4, bound per account, with a built-in IPQualityScore fraud check and per-account timezone matching. ISP, mobile, or residential recommended; data-center explicitly discouraged.

**Anti-ban features:** duplicate action prevention, per-campaign exclude lists, "reject if messaged after connection date", reply detection terminating a sequence, automatic withdrawal of pending invites older than N days (default 30), and a 4-hour campaign pause after 4 consecutive InMail credit errors.

## 2.4 CRM & Lead Management

Local **SQLite** database, one per instance, never cloud-synced, manual backups. Contains every profile ever collected regardless of campaign. Deduplication by platform ID, with merge on profile visit once all IDs are scraped. Profiles cannot be deleted, only excluded.

Record contents: general info, full campaign history with per-step timestamps and message text, all platform IDs, industry and summary, personal info and badges, tags and custom variables, up to two mutual connections, full experience/education/skills/languages.

Tagging and `{cs_*}` custom variables are plug-ins. There is **no dedicated pipeline stage model**; status is implied by which action sub-list a lead currently sits in. No global do-not-contact list, only per-campaign exclude lists `[unverified]`.

**Reply detection:** the `Check for replies` action polls the inbox, scraping the last 200 chats on first run and deltas thereafter. Minimum interval 1 hour, default 3. A match moves the lead to Replied and terminates its sequence. A `Send replied to Webhook` plug-in fires immediately on detection.

**Reporting:** raw sub-list counts only. No computed acceptance or reply percentages, no charts. CSV export is the reporting escape hatch.

## 2.5 Integrations

Outbound webhooks (`Send person to webhook`, `Send organization to webhook`, `Send replied to Webhook`) POST JSON with the full CSV field set, optionally flattened, optionally including messaging history (Pro only). Not counted against daily limits. Zapier, Make, and n8n are supported purely as webhook receivers; there is no native Zapier app. Eleven direct CRM connectors: HubSpot, Pipedrive, Salesforce, Close.io, Zoho CRM, Zoho Recruit, ActiveCampaign, HighLevel, Streak, Capsule, Instantly, with configurable field mapping and non-destructive upsert.

**There is no public API, CLI, or SDK.** Inbound integration is CSV upload only.

## 2.6 AI Features (2026)

This is the newest and most architecturally relevant layer, verified from the `/features/*` JSON-LD.

**AI Messages.** Quick mode (short prompt to full draft) and Advanced mode (structured goal, offer, audience, value proposition). Hyper-personalisation reads each prospect's job title, About section, and skills and writes genuinely unique text rather than substituting variables into a template. **Hybrid templates** combine a static skeleton with AI-generated fragments in one message. An AI Text Editor fixes grammar, shortens, expands, and translates. A Tone of Voice switcher offers Professional, Casual, Optimistic, Confident, Inspirational.

**AI Comments.** Lives inside the like-and-comment action. Generates from post content plus post author profile plus a plain-language **comment goal** ("ask a thoughtful question", "challenge the point politely"), with configurable tone, language, and length. Positioned explicitly as a **pre-invite warm-up** so prospects have seen you engage before a cold invite lands.

**AI ICP Detection.** Not a setting, a **workflow action**. The user describes their ideal lead in plain language. Selected profile fields (summary, industry, company, position, experience, skills, location, connection count) are marked required, optional, or excluded. A match-strictness threshold routes leads: matched to **Success** and onward through the funnel, unmatched to **Failed** and the campaign ends for them. It reads cached data only, so it costs zero LinkedIn actions. Recommended placement: collect, enrich, ICP filter, personalise, then invite.

**AI Reply Assistant.** Reads the full prior thread from the LH Inbox and drafts a context-aware reply, optionally steered by a short instruction such as "propose a 15-minute call next Tuesday".

**Metering and control.** A separate AI credit pool from email credits (250/month Standard, 500/month Pro), roughly 30 generations/day. Missing profile data triggers either an automatic Visit and Extract or an outright skip, never a generic fallback message.

**Two architecturally decisive details.**

1. Every AI-generated message lands in an **AI Drafts queue** requiring manual approve, edit, or regenerate, with auto-approve as an opt-in toggle. Linked Helper independently arrived at the same human-in-the-loop pattern this repo already uses, which validates keeping it.
2. **The AI is sealed inside the desktop app with no API.** It cannot be inspected, steered, or swapped. That is precisely the seam an MCP-native design opens: our LLM client is the generator, it is free at the margin, it already carries Nived's voice rules, and it is fully auditable.

---

# STEP 3 — Gap Analysis & MCP Re-Architecting

## 3.1 Feature Gap Matrix

| Capability | Linked Helper 2 | This repo today | Gap |
| --- | --- | --- | --- |
| Multi-step sequences | Linear workflow, 25+ action types | None. Prompts describe steps | **Total** |
| Persistent lead DB | SQLite CRM, full history | None | **Total** |
| Reply detection | Inbox polling, auto-terminate | None | **Total** |
| Daily/weekly limits | Enforced in app, ±10% jitter | Prose in a prompt file | **Total** |
| Working hours | Per-day windows + timezone | None | **Total** |
| Dedupe / blacklist | Platform-ID dedupe, exclude lists | None | **Total** |
| Pending invite hygiene | Auto-withdraw after N days | None | **Total** |
| Human emulation | In-page nav, typing modes, micro-delays | `sleep(4)`, `type(delay=25)` | **Severe** |
| Fingerprint / proxy | Per-account randomisation + proxy | Fixed 2021 UA, no proxy | **Severe** |
| Lead harvesting | 13 free sources + CSV | People search, post search | **Large** |
| Templating | Variables, spintax, variations, IF/THEN/ELSE | None | **Large** |
| Tagging / custom fields | Both | None | **Large** |
| Webhooks | 3 webhook actions, JSON payloads | None | **Large** |
| Analytics | Sub-list counts, CSV export | LLM reads Markdown | **Moderate** |
| Engagement (like/comment) | Batch action with AI comments | `comment_on_approved_posts` | **Small, already ahead on quality** |
| Human review of AI text | AI Drafts queue, opt-in auto-approve | Markdown approval queue | **Parity** |
| AI ICP qualification | Workflow action, routes Success/Failed | None, though the LLM could do it today | **Moderate, cheap for us** |
| AI text generation | Sealed engine, metered credits, no API | LLM client with Nived's voice rules | **We are ahead** |
| Programmatic control | None. No API, CLI, or SDK at all | MCP tools | **We are ahead** |

The last three rows are the strategic point. Linked Helper had to build and meter its own AI engine and still exposes no API. Here the MCP client is the AI engine: zero marginal cost, better voice control, fully auditable, and drivable by an agent. An MCP-native equivalent is not a clone, it is the version of Linked Helper that an agent can actually operate.

## 3.2 Tool Abstraction: Atomic vs Workflow

Neither extreme works. Pure atomic tools re-create today's problem, where the LLM becomes the scheduler and the limiter. Pure workflow tools make the system opaque and untestable.

**Three layers, with a hard rule between them.**

**Layer 0 — Action primitives. Not MCP tools.**
`invite`, `send_message`, `visit_and_extract`, `like_post`, `comment_on_post`, `endorse_skills`, `follow`, `withdraw_invite`, `check_replies`. Plain Python in `actions/`, callable only by the worker, each implementing a common `Action` protocol with `precheck`, `execute`, `classify_result`. Exposing these over MCP is the mistake to avoid: it hands the LLM a way around the queue and the limiter.

**Layer 1 — Control plane. These are the MCP tools.**
CRUD and control over campaigns, leads, drafts, limits, and the worker. They touch SQLite only. They never open a browser, so they return in milliseconds and can never hang an agent turn.

**Layer 2 — Supervised escape hatch.**
`action_enqueue_adhoc` for "like this specific post now". It still writes a row to `jobs` rather than executing inline, so even a one-off manual action passes through the same gate, the same jitter, and the same ledger.

**The invariant: no MCP tool ever drives Playwright.** Everything the LLM initiates is enqueued, never executed. That one rule is what makes safety enforceable.

## 3.3 Autonomous Execution vs LLM Control

```
Copilot / any MCP client
        │  stdio
┌───────▼────────────┐        ┌──────────────────────┐
│  MCP Server        │        │  Worker Daemon       │
│  (control plane)   │        │  (execution plane)   │
│                    │        │                      │
│  tools/resources   │        │  tick loop           │
│  read+write SQLite │        │  SafetyGate          │
│  never touches web │        │  owns Playwright     │
└───────┬────────────┘        └───────┬──────────────┘
        │                             │
        └──────►  SQLite (WAL)  ◄─────┘
                  jobs, leads, actions_log,
                  campaigns, ai_drafts, limits
```

- The worker runs an independent tick loop: select due jobs, ask the gate, execute, log, schedule the next step. It survives VS Code restarts and does not care whether an LLM is connected.
- The MCP server is stateless with respect to execution. `campaign_start` flips a status column; it does not run anything.
- Coordination is SQLite in WAL mode plus a `worker_heartbeat` table, so `worker_status` can honestly report "worker has not checked in for 6 minutes" instead of silently pretending campaigns are running.
- Progress is observed through resources (`linkedin://campaigns/{id}/funnel`, `linkedin://worker/status`), emitting `notifications/resources/updated` on clients that support it and falling back to polling elsewhere.

**The AI loop is the interesting inversion.** When the worker reaches a step needing generated text or an ICP verdict, it does not call an LLM. It writes a row to `ai_drafts` with status `needs_generation` and the lead's scraped context, then moves on. The LLM client polls `drafts_list_pending`, generates in Nived's voice using the existing `.github` voice rules, and calls `drafts_submit`. The draft sits at `pending_approval` until `drafts_approve` releases it into `jobs`.

The same queue carries four kinds:

| `kind` | Context handed to the LLM | Result |
| --- | --- | --- |
| `connection_note` | Headline, About, current role, mutuals | Note under 300 chars |
| `message` | Full profile plus prior thread | Message body |
| `comment` | Post text, author profile, comment goal, tone, length | Comment text |
| `icp_evaluation` | Profile fields marked required/optional, plus the plain-language ICP | `{match, score, reason}` routing the lead to Success or Failed |

`icp_evaluation` is the standout. Linked Helper meters this against a credit pool and hides the reasoning. Here it is an ordinary LLM classification with a written rationale stored on the lead, costing nothing and fully auditable. It becomes the cheapest possible quality gate: qualify hard before spending a scarce daily invite.

Steps also carry `on_missing_data: visit_extract | skip`, matching Linked Helper's behaviour of never sending a generic fallback when personalisation data is absent.

## 3.4 Safety Architecture That Does Not Rely on the LLM

A single chokepoint module, `safety/gate.py`. Every browser-touching action calls `gate.acquire(account_id, action_type, lead_id)` inside one SQL transaction:

1. Account state is `active` (not `paused`, `cooldown`, or `challenged`).
2. Now falls inside a configured working-hours window for the account timezone.
3. Rolling 24-hour count for this `action_type` is below its per-account cap.
4. Rolling 24-hour count across all types is below the global ceiling (default 150).
5. Rolling 7-day invite count is below the weekly cap (default 100, hard ceiling 200).
6. Open pending invites are below the pending ceiling (default 200).
7. Ramp-up schedule for account age is satisfied.
8. Dedupe: this account has not already performed this `action_type` on this lead.
9. Lead is not blacklisted and not on a campaign exclude list.
10. Daily jitter: a per-day deterministic seed derived from `(account_id, date)` shrinks the effective cap by 0 to 10%, so the real ceiling differs every day without being random per call.

It returns a lease or a typed refusal (`WorkingHoursClosed`, `DailyCapReached`, `WeeklyCapReached`, `DuplicateAction`, `Blacklisted`, `AccountChallenged`). Refusals are logged, so "why did nothing happen last night" is answerable.

Three properties make this hold:

- **Counters derive from `actions_log`, an append-only ledger.** Nothing is a mutable counter that can drift or be reset.
- **Limits live in the database, not in a prompt.** The LLM cannot raise a cap by reasoning about it. `limits_set` exists but is itself clamped by hard-coded ceilings in `core/config.py` that no tool can exceed.
- **Detection is post-navigation, not post-hoc.** `safety/detect.py` runs after every navigation, checking for `/checkpoint/`, `/challenge/`, captcha frames, authwall, and known warning banners. On a hit it sets the account to `challenged`, halts the worker, and writes an alert row that surfaces through `linkedin://worker/status`.

---

# STEP 4 — Deliverable: Implementation Plan

## 4.1 Executive Architectural Blueprint

```
linkedin_mcp/
├── server.py                  # FastMCP entrypoint: tools, resources, prompts
├── worker.py                  # daemon entrypoint: tick loop
├── core/
│   ├── db.py                  # connection pool, WAL, migration runner
│   ├── migrations/            # 0001_init.sql, 0002_*.sql
│   ├── models.py              # row mappers / dataclasses
│   ├── config.py              # settings + HARD CEILINGS (not user-editable)
│   └── clock.py               # tz-aware now(), working-hours arithmetic
├── safety/
│   ├── gate.py                # SafetyGate.acquire() - the single chokepoint
│   ├── limits.py              # rolling counters, jitter, ramp-up schedule
│   └── detect.py              # challenge / captcha / authwall / warning detection
├── browser/
│   ├── session.py             # per-account persistent context, fingerprint, proxy
│   ├── navigate.py            # in-page search-bar navigation (no direct URL loads)
│   └── humanize.py            # typing modes, dwell, scroll, micro-delays
├── actions/                   # Layer 0 primitives, one module per action type
│   ├── base.py                # Action protocol: precheck / execute / classify
│   ├── invite.py  message.py  visit_extract.py  like_comment.py
│   ├── endorse.py  follow.py  withdraw_invite.py  check_replies.py
│   └── filter_network.py  delay.py  webhook_send.py
├── harvest/
│   ├── people_search.py  post_engagers.py  group_members.py
│   ├── event_attendees.py  company_employees.py  connections.py
│   └── csv_import.py
├── engine/
│   ├── scheduler.py           # job selection, bunching, action priority
│   ├── state_machine.py       # step advancement, sub-list transitions
│   └── templates.py           # variables, spintax, variations, IF/THEN/ELSE
├── mcp/
│   ├── tools_campaign.py  tools_leads.py  tools_harvest.py
│   ├── tools_drafts.py  tools_safety.py  tools_inbox.py
│   ├── resources.py  prompts.py
└── render/
    └── markdown.py            # SQLite -> data/*.md review views
```

Selector fragility is contained by keeping every LinkedIn CSS selector in one `browser/selectors.py` map with ordered fallback lists, so LinkedIn DOM churn is a single-file fix rather than a grep across 1,400 lines.

## 4.2 Database Schema

SQLite, WAL mode, forward-only numbered migrations.

**Identity and safety**

```sql
accounts(id, label, linkedin_public_id, timezone, proxy_url, user_agent,
         fingerprint_json, browser_profile_dir, state, account_age_days,
         created_at, updated_at)
-- state: active | paused | cooldown | challenged | logged_out

account_limits(account_id, action_type, daily_cap, weekly_cap, enabled)
-- one row per (account, action_type); seeded from config defaults

working_hours(account_id, weekday, start_minute, end_minute)

actions_log(id, account_id, lead_id, campaign_id, step_id, action_type,
            outcome, detail_json, occurred_at)
-- APPEND ONLY. The sole source of truth for every rate-limit calculation.
-- Index (account_id, action_type, occurred_at) drives all counter queries.

safety_events(id, account_id, kind, severity, detail_json, occurred_at)
-- refusals, challenges, warning banners, forced logouts

worker_heartbeat(worker_id, account_id, last_tick_at, status, current_job_id)
```

**Leads and CRM**

```sql
leads(id, account_id, member_id, public_id, hash_id, full_name, first_name,
      last_name, headline, summary, organization_name, organization_title,
      location_name, member_distance, connection_count, follower_count,
      connected_at, badges_json, avatar_url, first_seen_at, last_visited_at)
-- UNIQUE(account_id, member_id) and UNIQUE(account_id, public_id) for dedupe

lead_contacts(lead_id, kind, value, source, verified_at)
-- kind: email | work_email | personal_email | phone | website | twitter

lead_experience(lead_id, ord, title, company, company_id, start_date, end_date, location)
lead_education(lead_id, ord, school, degree, field, start_year, end_year)
lead_skills(lead_id, skill, endorsement_count)

tags(id, account_id, name, color)
lead_tags(lead_id, tag_id, applied_at, applied_by)
lead_custom_fields(lead_id, key, value)     -- the {cs_*} equivalent

blacklist(id, account_id, member_id, public_id, reason, added_at)
-- GLOBAL do-not-contact. Closes a gap Linked Helper leaves open.
```

**Campaigns and execution**

```sql
campaigns(id, account_id, name, status, approval_mode, exclude_list_id,
          created_at, started_at, paused_at)
-- status: draft | pending_approval | active | paused | completed | archived
-- approval_mode: auto | manual_drafts

campaign_steps(id, campaign_id, ord, action_type, config_json,
               template_id, bunch_size, on_failure, on_missing_data)
-- on_missing_data: visit_extract | skip  (never send a generic fallback)

templates(id, account_id, name, body, variations_json, kind,
          ai_spec_json, is_ai_generated, created_at)
-- kind: static | ai | hybrid   (hybrid = static skeleton + AI fragments)
-- ai_spec_json: goal, tone, length, language, required profile fields

campaign_leads(campaign_id, lead_id, current_step_ord, sublist, next_run_at,
               attempts, last_outcome, entered_at, updated_at)
-- sublist: queue | processing | successful | failed | replied | skipped | excluded
-- Index (campaign_id, sublist, next_run_at) drives the scheduler.

jobs(id, account_id, campaign_id, lead_id, step_id, action_type, payload_json,
     scheduled_for, priority, state, attempts, last_error, locked_by, locked_at)
-- state: pending | leased | done | failed | cancelled | refused

ai_drafts(id, account_id, campaign_id, lead_id, step_id, kind, context_json,
          generated_text, verdict_json, status, model, created_at, decided_at)
-- status: needs_generation | pending_approval | approved | rejected | sent
-- kind: connection_note | message | comment | icp_evaluation
-- verdict_json holds {match, score, reason} for icp_evaluation rows

messages(id, account_id, lead_id, direction, body, thread_urn, sent_at, detected_at)
-- direction: outbound | inbound. Powers reply detection and the inbox view.
```

**Integration**

```sql
webhooks(id, account_id, name, url, trigger, secret, enabled)
-- trigger: step_reached | reply_detected | lead_harvested | invite_accepted

webhook_deliveries(id, webhook_id, lead_id, payload_json, status_code,
                   attempts, delivered_at)

harvest_runs(id, account_id, source_type, params_json, found_count,
             new_count, started_at, finished_at)

schema_migrations(version, applied_at)
```

**State management rules**

- `actions_log` is never updated or deleted. Limits are always a `COUNT(*)` over a time window, never a stored counter.
- `jobs` uses lease-based locking (`locked_by`, `locked_at`) so a crashed worker's jobs are reclaimable after a lease timeout.
- `campaign_leads` is the state machine row. `jobs` is the execution queue derived from it. Keeping them separate means the queue can be rebuilt from campaign state after any corruption.
- `render/markdown.py` regenerates `data/*.md` from SQLite. Markdown becomes a read view plus an approval surface, never the source of truth.

## 4.3 MCP Tool Specification

**Campaigns**

| Tool | Purpose |
| --- | --- |
| `campaign_create` | Create a campaign with an ordered step list. Returns `status: draft` |
| `campaign_add_step` | Append or insert a step with action type, config, template, bunch size |
| `campaign_set_template` | Attach a message template with variables, spintax, variations |
| `campaign_preview` | Render the workflow plus 3 sample personalised messages against real leads. No side effects |
| `campaign_set_icp` | Attach a plain-language ICP description plus required/optional/excluded field weights and a match threshold |
| `campaign_approve` | The single human gate. Moves `draft` to `active` after Nived confirms |
| `campaign_start` / `campaign_pause` / `campaign_resume` / `campaign_archive` | Lifecycle control. Writes status only |
| `campaign_status` | Per-step sub-list counts, next scheduled run, computed acceptance and reply rates |
| `campaign_add_leads` | Move leads into a campaign queue from a harvest run, another campaign, or a filter |

**Leads and CRM**

| Tool | Purpose |
| --- | --- |
| `lead_search` | Query the local CRM by tag, degree, company, campaign, status, free text |
| `lead_get` | Full CRM card including campaign history and message thread |
| `lead_tag` / `lead_untag` | Tag management |
| `lead_set_custom_field` | Set a `{cs_*}` variable |
| `lead_blacklist` | Global do-not-contact |
| `lead_export_csv` | Configurable field set and delimiter |

**Harvesting**

| Tool | Purpose |
| --- | --- |
| `harvest_people_search` | Enqueue a harvest job from a LinkedIn people-search URL or keyword plus filters |
| `harvest_post_engagers` | Likers and commenters of a given post URL |
| `harvest_group_members` | Members of a group |
| `harvest_event_attendees` | Attendees of an event |
| `harvest_company_employees` | Employees from a company People tab |
| `harvest_connections` | Your own 1st-degree list |
| `harvest_import_csv` | Import profile URLs from a CSV |
| `harvest_status` | Progress of a harvest run |

**AI drafts (the human-in-the-loop surface)**

| Tool | Purpose |
| --- | --- |
| `drafts_list_pending` | Drafts needing generation, with lead context attached |
| `drafts_submit` | LLM writes the generated text back. Lands at `pending_approval` |
| `icp_submit_verdict` | LLM returns `{match, score, reason}` for an `icp_evaluation` row, routing the lead |
| `drafts_approve` / `drafts_reject` | Nived's decision. Approve releases to `jobs` |
| `drafts_bulk_approve` | Approve a filtered batch after review |
| `drafts_set_auto_approve` | Opt-in auto-approve per campaign and per draft kind, matching Linked Helper's toggle |

**Safety and worker**

| Tool | Purpose |
| --- | --- |
| `limits_get` | Current caps, 24h and 7d usage, remaining budget, next reset |
| `limits_set` | Adjust caps. Clamped by hard ceilings in `core/config.py` |
| `working_hours_set` | Per-weekday windows and timezone |
| `account_status` | State, age, pending invites, last challenge event |
| `account_pause` / `account_resume` | Emergency stop |
| `worker_status` | Heartbeat age, current job, queue depth, recent refusals |
| `worker_start` / `worker_stop` | Supervise the daemon |
| `session_login` | Interactive login, writes to the per-account browser profile |
| `action_enqueue_adhoc` | One-off supervised action. Enqueued, never executed inline |

**Inbox and integrations**

| Tool | Purpose |
| --- | --- |
| `inbox_list_replies` | New inbound messages since a timestamp, with lead context |
| `inbox_send_reply` | Enqueue a reply. Goes through the gate like everything else |
| `webhook_register` / `webhook_list` / `webhook_test` | Outbound webhook management |
| `analytics_report` | Funnel, acceptance rate, reply rate, follower delta over a period |

**MCP Resources**

```
linkedin://accounts                      linkedin://accounts/{id}/limits
linkedin://campaigns                     linkedin://campaigns/{id}
linkedin://campaigns/{id}/funnel         linkedin://campaigns/{id}/leads/{sublist}
linkedin://leads/{id}                    linkedin://leads/recent
linkedin://drafts/pending                linkedin://inbox/unread
linkedin://worker/status                 linkedin://safety/today
linkedin://analytics/weekly              linkedin://templates
```

**MCP Prompts**

| Prompt | Purpose |
| --- | --- |
| `new_campaign` | Guided campaign build: audience, sequence, templates, limits, then `campaign_approve` |
| `review_drafts` | Walk pending AI drafts, show lead context, approve/edit/reject |
| `triage_replies` | Summarise new replies, suggest responses in Nived's voice |
| `weekly_report` | Funnel plus follower growth against the 100k goal |
| `safety_check` | Limit usage, challenge events, pending-invite hygiene |
| `harvest_audience` | Build and preview a target audience before committing it to a campaign |

## 4.4 Phased Roadmap

### Phase 1 — Core Foundation & Safety Refactoring

Goal: make it impossible to exceed a limit, and make sessions actually work.

- SQLite layer with migration runner. Ship `0001_init.sql` with the full schema so later phases add data, not tables.
- `core/config.py` with hard ceilings that no tool can exceed.
- `safety/limits.py` rolling-window counters over `actions_log`, plus deterministic per-day jitter and the new-account ramp-up schedule.
- `safety/gate.py` as the single chokepoint, with typed refusals.
- `safety/detect.py` for checkpoint, challenge, captcha, authwall, and warning banners.
- `browser/session.py`: per-account persistent context (`launch_persistent_context`) so cookies, localStorage, and IndexedDB stay consistent; **fix the save/load path divergence bug**; modern matched user agent; per-account fingerprint seed; optional proxy.
- `browser/humanize.py` and `browser/navigate.py`: in-page search-bar navigation replacing direct `goto` for profiles, plus typing modes and micro-delays.
- `browser/selectors.py`: every selector in one place with ordered fallbacks.
- Migrate the existing 11 tools onto the package. Mutating tools route through the gate. `search_*` and `view_*` become harvest primitives.
- `render/markdown.py` so `data/*.md` keeps working as a review surface.
- Tests: limiter arithmetic, working-hours boundaries, dedupe, jitter determinism, refusal types. These need no network.

**Exit criteria:** `send_connection_request` refuses with `DailyCapReached` on the 51st call in 24 hours, refuses outside working hours, refuses a duplicate on the same lead, and every attempt is a row in `actions_log`.

### Phase 2 — Data Extraction & Lead CRM Engine

Goal: a real lead database with real dedupe.

- `harvest/` modules for people search, post engagers, group members, event attendees, company employees, own connections, CSV import.
- Harvest runs execute as worker jobs with pagination and gate-controlled pacing.
- Full field extraction into `leads`, `lead_contacts`, `lead_experience`, `lead_education`, `lead_skills`, with the 21-day contact and 14-day position cache windows.
- Dedupe and merge on `member_id` and `public_id`.
- Tagging, custom fields, global blacklist.
- `lead_search`, `lead_get`, `lead_export_csv`.
- Resources: `linkedin://leads/*`.

**Exit criteria:** harvest 500 profiles from a search, run it twice, and get zero duplicates and correct incremental counts.

### Phase 3 — Campaign & Sequence Execution Engine

Goal: multi-step sequences that run for days without an LLM attached.

- `engine/templates.py`: variable substitution, spintax expansion, message variations, IF/THEN/ELSE on variable presence, and hybrid templates (static skeleton plus AI-generated fragments).
- `engine/state_machine.py`: sub-list transitions and step advancement matching the Linked Helper model.
- `engine/scheduler.py`: tick loop, bottom-up action priority, bunching, lease-based job locking, exponential backoff.
- `actions/` primitives: invite, message, visit_extract, like_comment, endorse, follow, withdraw_invite, filter_network, delay, check_replies.
- `actions/ai_icp_filter.py`: a zero-LinkedIn-cost step that enqueues an `icp_evaluation` draft and routes the lead on the LLM's verdict. Placed before invite steps so scarce daily invites are only spent on qualified leads.
- `check_replies` polling with configurable interval, thread matching, auto-terminate into the Replied sub-list, and `messages` archival.
- `withdraw_invite` for pending-invite hygiene with a default 30-day threshold.
- `worker.py` daemon with heartbeat, graceful shutdown, and crash-safe lease reclamation.
- AI drafts loop wired in: `needs_generation` to `pending_approval` to `approved` to `jobs`, with per-campaign auto-approve.
- Campaign tools and the `campaign_approve` gate.

**Exit criteria:** a four-step campaign (AI ICP filter, Invite, wait 3 days, Filter to 1st, Message) runs unattended across a weekend, respects working hours and caps, stops for anyone who replies, and shows a correct funnel in `campaign_status`.

### Phase 4 — Full MCP Integration & Autonomous Agent Controls

Goal: complete the control plane and close the loop with the outside world.

- All resources registered with change notifications where supported.
- All six MCP prompts.
- Webhooks with retry and delivery log, plus a `webhook_send` campaign action.
- `analytics_report` with computed acceptance and reply rates, a capability Linked Helper does not have.
- Inbox tools: `inbox_list_replies`, `inbox_send_reply`, with AI reply drafting through the same drafts queue.
- Migrate the five existing `.github` agents and skills onto the new tools so `/grow-network` becomes "create a campaign" instead of "send 20 invites right now". The trending and top-voices workflows become an **evergreen warm-up campaign**: harvest post engagers, AI ICP filter, AI comment as the warm-up touch, wait, then invite. That is exactly the pattern Linked Helper markets for AI Comments, and it serves the 100k follower goal better than one-off engagement batches.
- Consider `boost_post` (@-mention selected 1st-degree connections in a comment under Nived's own post, capped at 100 per 24 hours). It is the one Linked Helper action aimed squarely at reach rather than outreach.
- Rewrite `README.md`, `.github/copilot-instructions.md`, and `.github/instructions/*.md` to state that safety is enforced in code, and remove the prose limits that currently masquerade as controls.
- Optional: HTTP/SSE transport so the server is reachable from clients other than local VS Code. Linked Helper shipped a browser-based campaign runner for the same reason.

**Exit criteria:** a fresh MCP client with no Copilot-specific files can discover campaigns, inspect the funnel, review drafts, and pause the worker using tools, resources, and prompts alone.

## 4.5 Next Immediate Steps (start of Phase 1)

Create, in order:

1. `linkedin_mcp/__init__.py`, `linkedin_mcp/core/__init__.py`
2. `linkedin_mcp/core/config.py` — settings plus `HARD_CEILINGS` (invites/day 100, invites/week 200, global actions/day 200, direct URL visits/day 40)
3. `linkedin_mcp/core/migrations/0001_init.sql` — the full schema from 4.2
4. `linkedin_mcp/core/db.py` — connection, WAL pragmas, migration runner, transaction helper
5. `linkedin_mcp/core/clock.py` — tz-aware now, working-hours arithmetic
6. `linkedin_mcp/safety/limits.py` — rolling counters, deterministic jitter, ramp-up
7. `linkedin_mcp/safety/gate.py` — `SafetyGate.acquire()` and the refusal types
8. `linkedin_mcp/safety/detect.py` — interstitial detection
9. `tests/test_limits.py`, `tests/test_gate.py` — no network required

Then modify:

10. `linkedin_mcp/browser/session.py` — port `BrowserSession`, switch to `launch_persistent_context` per account, **fix the relative/absolute cookie path bug**, modern UA, fingerprint seed, proxy hook
11. `linkedin_mcp/browser/selectors.py`, `humanize.py`, `navigate.py`
12. `linkedin_browser_mcp.py` — thin shim importing from the package; `send_connection_request`, `interact_with_linkedin_post`, and `comment_on_approved_posts` route through the gate
13. `requirements.txt` — add `pytest`, `pytest-asyncio`, `freezegun`; pin `fastmcp`
14. `.gitignore` — replace the blanket `data/` with `data/*.md` and `data/analytics/*.json` so committed templates and schema survive; add `*.db`, `*.db-wal`, `*.db-shm`
15. `.github/instructions/linkedin-mcp.instructions.md` — replace "add `asyncio.sleep(random.uniform(10,60))`" with "call `SafetyGate.acquire()`; never sleep manually"

## 4.6 Risks and Notes

- **Terms of service.** Automated scraping and messaging violate LinkedIn's User Agreement. The repo already carries a disclaimer; scaling from ad-hoc engagement to unattended multi-day campaigns raises the account-restriction risk materially. Conservative defaults (30 invites/day rather than 50) are the right starting point.
- **Selector rot is the top operational risk.** `search_linkedin_posts` already had to abandon selectors for heuristics. Centralising selectors with ordered fallbacks plus a `selftest` job that verifies each critical selector still resolves is worth building in Phase 1.
- **Credential handling.** Password in `.env` plus an encryption key beside the ciphertext is weak. Recommend dropping stored passwords entirely in favour of an interactive `session_login` writing to a persistent browser profile, so only the session survives, not the password.
- **Scope creep.** The action catalogue is 25+ items. Phase 3 should ship 10. Endorse, follow, boost post, group invites, and event invites can follow once the engine is proven.
- **Existing workflows must not regress.** The five agents and skills are working today. Phase 1 keeps the current 11 tools functional; Phase 4 migrates them. Nived should never have a window where he cannot post or engage.

---

# Execution Tracking

The plan above is the approved design. Live execution state lives in two places
that stay in sync automatically:

- **Roadmap canvas** — `docs/roadmap.json`, rendered by the `roadmap` canvas
  extension. Source of truth for task status.
- **GitHub issues** — one issue per task, one PR per task. When a PR containing
  `Closes #N` merges, `.github/workflows/roadmap-sync.yml` marks the matching
  task done on the board and stamps the PR number.

## Issue map

| Phase | Task | Issue | Status |
| --- | --- | --- | --- |
| P1 | DB-01 Schema for Accounts, Campaigns, Leads, Workflows, ActionLogs | #4 | pending |
| P1 | CORE-01 Browser driver wrapper with stealth | #5 | pending |
| P1 | CORE-02 Cookie, session and proxy management | #6 | pending |
| P1 | CORE-03 Rate limiter enforcing hard caps | #7 | pending |
| P1 | CORE-04 Action delay simulator | #8 | pending |
| P1 | CORE-05 Challenge detection with auto-halt | #9 | pending |
| P2 | DB-02 Lead storage with tagging and blacklist | #12 | pending |
| P2 | DB-03 Lead deduplication engine | #13 | pending |
| P2 | DB-04 Audit logging service | #14 | pending |
| P2 | SCRAPE-01 Standard search extractor | #15 | pending |
| P2 | SCRAPE-02 Sales Navigator scraper | none | deferred |
| P2 | SCRAPE-03 Profile deep-scraper | #16 | pending |
| P2 | SCRAPE-04 Event attendees and post engagers | #17 | pending |
| P3 | SEQ-01 State machine for multi-step sequences | #19 | pending |
| P3 | SEQ-02 Dynamic messaging template engine | #20 | pending |
| P3 | SEQ-03 Inbox scanner and reply detection | #21 | pending |
| P3 | SEQ-04 Scheduled background runner | #22 | pending |
| P3 | SEQ-05 AI drafts queue and ICP gate | #23 | pending |
| P4 | MCP-01 Campaign control tools | #24 | pending |
| P4 | MCP-02 Lead extraction tools | #25 | pending |
| P4 | MCP-03 Sequence execution tools | #26 | pending |
| P4 | MCP-04 MCP Resources | #27 | pending |
| P4 | MCP-05 MCP Prompts | #28 | pending |

All 22 active tasks across all four phases now have an issue. SCRAPE-02 is the
only task without one, deliberately: it needs a paid Sales Navigator
subscription and was descoped in favour of free LinkedIn sources. File one if
that decision is reversed.

## Tracking infrastructure

Built and merged in PR #11:

- `linkTask()` in `store.mjs` persists `issueNumber`, `prNumber` and `prMerged`
  per task.
- `link_task` canvas action attaches an issue or PR to a task.
- Task cards render clickable issue and PR badges.
- `scripts/roadmap-sync.mjs` parses `Closes/Fixes/Resolves #N` from a merged
  PR body, finds the task carrying that issue number, and marks it done.
- `fs.watch` on `docs/roadmap.json` refreshes an open canvas panel the moment
  the file changes on disk. Note this reacts to local disk changes only; a
  `git pull` is still needed to bring the bot commit down from the remote.
- `.github/PULL_REQUEST_TEMPLATE.md` requires a Task ID and a `Closes #N` line.

Validated in live CI when PR #11 merged: the workflow parsed `Closes #10`,
correctly found no roadmap task carrying that issue number, and no-op'd
without committing.

## Next up

Every task on the board is filed and linked, so tracking is no longer the
bottleneck. Only two tasks are unblocked right now: DB-01 (#4) and CORE-01 (#5).

Dependency fan-out, computed from the board rather than estimated:

| Task | Issue | Transitively blocks |
| --- | --- | --- |
| DB-01 | #4 | 17 of 22 tasks |
| DB-02 | #12 | 14 |
| CORE-03 | #7 | 9 |
| CORE-01 | #5 | 7 |
| SEQ-01 | #19 | 7 |
| SEQ-04 | #22 | 4 |

Recommended first PR: **DB-01**. It blocks 17 of the 22 active tasks, and
nothing else can be built correctly against a schema that does not exist yet.
CORE-01 is the only other task that can start in parallel today.

Longest chain on the board, 7 tasks and 6 dependency hops:

    DB-01 (#4) -> DB-02 (#12) -> SEQ-01 (#19) -> SEQ-04 (#22) -> MCP-01 (#24) -> MCP-04 (#27) -> MCP-05 (#28)

Note the shape of the graph: Phase 4 is almost entirely downstream of SEQ-04
(#22), the worker daemon. Nothing in the MCP layer can be finished until the
execution plane runs unattended, so SEQ-04 is the real gate on shipping, not
the MCP tasks themselves.
