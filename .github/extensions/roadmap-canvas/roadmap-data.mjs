// Seed model for the Linked Helper parity roadmap.
// This is the DEFAULT shape. Live status lives in docs/roadmap.json and is
// merged over this seed on load, so editing text here is safe and non-destructive.

export const STATUSES = ["pending", "in-progress", "done", "blocked", "deferred"];

export const STATUS_LABELS = {
    pending: "Pending",
    "in-progress": "In progress",
    done: "Done",
    blocked: "Blocked",
    deferred: "Deferred",
};

/** Section 1 - non-negotiable system requirements and safety constraints. */
export const CONSTRAINTS = [
    {
        id: "REQ-01",
        title: "Protocol: 100% MCP standard",
        detail:
            "Tools, resources and prompts all exposed over the Model Context Protocol. No bespoke RPC. Hard invariant: no MCP tool ever drives Playwright directly. Agent-initiated work is enqueued, never executed inline.",
        evidence: "server.py registers tools + resources + prompts; every mutating tool writes a jobs row.",
        status: "pending",
    },
    {
        id: "REQ-02",
        title: "Safety: hard daily rate-limit caps",
        detail:
            "Invites/day, messages/day, profile views/day enforced in code, not prose. Counters derive from an append-only actions_log. Limits live in the DB clamped by hard ceilings the agent cannot raise. Defaults: 30 invites/day, 100/week, 150 actions/day, 40 direct-URL visits/day.",
        evidence: "safety/gate.py acquire() runs 10 checks in one SQL transaction and returns typed refusals.",
        status: "pending",
    },
    {
        id: "REQ-03",
        title: "Human emulation: delays, warming, natural navigation",
        detail:
            "Randomised micro-delays, keystroke-level typing, session warming ramp-up for new accounts, natural scrolling, working-hours windows, and in-page search-bar navigation instead of direct profile URL loads.",
        evidence: "browser/humanize.py + browser/navigate.py; ramp schedule in safety/limits.py.",
        status: "pending",
    },
    {
        id: "REQ-04",
        title: "Persistence: local SQLite system of record",
        detail:
            "SQLite in WAL mode holds campaigns, leads, jobs, action log, drafts and counters. Markdown under data/ becomes a generated review view, never the source of truth.",
        evidence: "core/db.py + core/migrations/0001_init.sql; render/markdown.py regenerates data/*.md.",
        status: "pending",
    },
];

/** Section 2 - the five architectural modules and their trackable work items. */
export const MODULES = [
    {
        id: "M1",
        name: "Core Automation & Session Layer",
        blurb: "Browser driver, session persistence, and the safety machinery every other module depends on.",
        tasks: [
            {
                id: "CORE-01",
                title: "Abstract browser driver wrapper with stealth",
                description:
                    "Wrap Playwright Chromium behind a driver interface. Replace the hard-coded Chrome/96.0.4664.110 user agent (a 2021 build advertised from a modern binary, which is itself a fingerprint mismatch). Add per-account fingerprint seeding, navigator.webdriver patching, and locale/timezone alignment. Centralise every LinkedIn CSS selector in browser/selectors.py with ordered fallbacks.",
                deps: [],
                phase: "P1",
                planRefs: ["p1-browser", "p1-humanize"],
                origin: "spec",
                status: "pending",
                notes: "",
            },
            {
                id: "CORE-02",
                title: "Persistent cookie, session and proxy management per account",
                description:
                    "Switch to launch_persistent_context per account so cookies, localStorage and IndexedDB stay consistent instead of only cookies being restored. FIX THE EXISTING BUG: save_cookies() writes an absolute Path(__file__).parent/sessions while load_cookies() reads a relative sessions/ path; when cwd differs the load fails and the error handler deletes the cookie file, causing unexplained re-logins. Add an optional per-account proxy binding.",
                deps: ["CORE-01"],
                phase: "P1",
                planRefs: ["p1-browser"],
                origin: "spec",
                status: "pending",
                notes: "Confirmed bug in linkedin_browser_mcp.py lines 129 vs 141.",
            },
            {
                id: "CORE-03",
                title: "Rate-limiter service enforcing hard caps independently of LLM calls",
                description:
                    "Build safety/limits.py and safety/gate.py. All counters are COUNT(*) over actions_log time windows, never stored counters that can drift. SafetyGate.acquire() checks account state, working hours, per-type 24h cap, global 24h cap, 7d invite cap, pending-invite ceiling, ramp-up, dedupe, blacklist, and applies deterministic per-day jitter shrinking the cap 0-10%. Returns a lease or a typed refusal.",
                deps: ["DB-01"],
                phase: "P1",
                planRefs: ["p1-config", "p1-limits", "p1-gate", "p1-tests"],
                origin: "spec",
                status: "pending",
                notes: "Highest-priority item. Today limits exist only as English sentences in copilot-instructions.md.",
            },
            {
                id: "CORE-04",
                title: "Action delay simulator for humanised keystrokes and micro-delays",
                description:
                    "browser/humanize.py: typing modes (type / paste / random), per-keystroke jitter, dwell times before clicks, natural scroll, and randomised micro-delays between every sub-step. Presets FAST and SAFE mirroring the benchmark. Callers never sleep manually; pacing is owned by this module.",
                deps: ["CORE-01"],
                phase: "P1",
                planRefs: ["p1-humanize"],
                origin: "spec",
                status: "pending",
                notes: "random is currently imported and never used anywhere in the codebase.",
            },
            {
                id: "CORE-05",
                title: "Challenge and interstitial detection with auto-halt",
                description:
                    "safety/detect.py runs after every navigation, checking for /checkpoint/, /challenge/, captcha frames, authwall and known warning banners. On a hit it sets the account to challenged, halts the worker, and writes a safety_events alert surfaced through worker_status. Today detection is a substring check for 'login' or 'authwall' in the URL.",
                deps: ["CORE-01"],
                phase: "P1",
                planRefs: ["p1-detect"],
                origin: "audit",
                status: "pending",
                notes: "Added from the codebase audit, not in the original brief.",
            },
        ],
    },
    {
        id: "M2",
        name: "Data Persistence & CRM Database Engine",
        blurb: "The system of record. Nothing above this line can be trusted without it.",
        tasks: [
            {
                id: "DB-01",
                title: "Design schema for Accounts, Campaigns, Leads, Workflows, ActionLogs",
                description:
                    "Ship the complete schema in one migration so later phases add data, not tables: accounts, account_limits, working_hours, actions_log, safety_events, worker_heartbeat, leads, lead_contacts, lead_experience, lead_education, lead_skills, tags, lead_tags, lead_custom_fields, blacklist, campaigns, campaign_steps, templates, campaign_leads, jobs, ai_drafts, messages, webhooks, webhook_deliveries, harvest_runs, schema_migrations. WAL mode, forward-only numbered migrations.",
                deps: [],
                phase: "P1",
                planRefs: ["p1-schema", "p1-db"],
                origin: "spec",
                status: "pending",
                notes: "Blocks CORE-03. Do this first.",
            },
            {
                id: "DB-02",
                title: "Local lead storage engine with tagging, custom fields and blacklist",
                description:
                    "Lead CRUD plus tags, {cs_*} custom fields, and a GLOBAL do-not-contact blacklist. The benchmark only offers per-campaign exclude lists, so a global blacklist closes a real gap rather than copying one.",
                deps: ["DB-01"],
                phase: "P2",
                planRefs: ["p2-crm"],
                origin: "spec",
                status: "pending",
                notes: "",
            },
            {
                id: "DB-03",
                title: "Lead deduplication engine across multiple extractions",
                description:
                    "Dedupe and merge on member_id and public_id with UNIQUE(account_id, member_id) and UNIQUE(account_id, public_id). Honour cache windows: contact info 21 days, positions 14 days. Re-running the same harvest must produce zero duplicates and correct incremental counts.",
                deps: ["DB-02"],
                phase: "P2",
                planRefs: ["p2-extract"],
                origin: "spec",
                status: "pending",
                notes: "",
            },
            {
                id: "DB-04",
                title: "Audit logging service for every LinkedIn DOM interaction",
                description:
                    "actions_log is append-only and never updated or deleted. It is simultaneously the audit trail and the sole input to every rate-limit calculation, so correctness here is a safety property, not just observability. Index on (account_id, action_type, occurred_at) drives all counter queries. Refusals log too, so 'why did nothing happen last night' is answerable.",
                deps: ["DB-01"],
                phase: "P2",
                planRefs: ["p1-gate", "p2-crm"],
                origin: "spec",
                status: "pending",
                notes: "",
            },
        ],
    },
    {
        id: "M3",
        name: "Lead Harvesting & Scraping Primitives",
        blurb: "Turning LinkedIn surfaces into rows. Gate-paced, paginated, resumable.",
        tasks: [
            {
                id: "SCRAPE-01",
                title: "Standard search result extractor (People, Posts, Groups)",
                description:
                    "People search with all filters, post search, and group members. Runs as a gate-paced worker job with pagination up to the platform ceiling of roughly 1,000 results per search. Note the current search_linkedin_posts already abandoned CSS selectors for a DOM-walking heuristic, which is direct evidence the 2021-era selector layer has rotted.",
                deps: ["CORE-01", "CORE-03", "DB-02"],
                phase: "P2",
                planRefs: ["p2-harvest"],
                origin: "spec",
                status: "pending",
                notes: "",
            },
            {
                id: "SCRAPE-02",
                title: "Sales Navigator lead list and account search scraper",
                description:
                    "Sales Navigator lead search, saved lists, saved searches and account search. Requires a paid Sales Navigator subscription and a separate selector surface. Explicitly descoped during planning in favour of free LinkedIn sources; kept on the board so the decision stays visible and can be reversed.",
                deps: ["SCRAPE-01"],
                phase: "P2",
                planRefs: [],
                origin: "spec",
                status: "deferred",
                notes: "Descoped by decision: free LinkedIn sources only, no paid subscriptions.",
            },
            {
                id: "SCRAPE-03",
                title: "Profile deep-scraper (experience, skills, contact info, mutuals)",
                description:
                    "Full field extraction into leads, lead_contacts, lead_experience, lead_education, lead_skills. Identity, platform IDs, professional history, network status, badges (Premium, Influencer, OpenLink, JobSeeker, Hiring), mutual connections. Contact info is only available for 1st-degree connections. Must use in-page navigation, not direct URL loads, which are capped at 40 per 24 hours.",
                deps: ["SCRAPE-01", "DB-03"],
                phase: "P2",
                planRefs: ["p2-extract"],
                origin: "spec",
                status: "pending",
                notes: "",
            },
            {
                id: "SCRAPE-04",
                title: "Event attendees and post liker/commenter harvester",
                description:
                    "Event attendees via the Networking tab, plus likers and commenters of any post URL. Also company employees, your own connections, followers, and CSV import. Post engagers are the highest-intent free source and feed the evergreen warm-up campaign directly.",
                deps: ["SCRAPE-01"],
                phase: "P2",
                planRefs: ["p2-harvest"],
                origin: "spec",
                status: "pending",
                notes: "",
            },
        ],
    },
    {
        id: "M4",
        name: "Campaign & Multi-Step Sequence Engine",
        blurb: "The part that runs for days with no LLM attached.",
        tasks: [
            {
                id: "SEQ-01",
                title: "State machine for multi-step outreach sequences",
                description:
                    "Linear workflow matching the benchmark: sub-lists queue, processing, successful, failed, replied, skipped, excluded. campaign_leads holds the state machine row; jobs is the execution queue derived from it, so the queue can be rebuilt from campaign state after corruption. Filter steps provide branching by dropping leads out of the flow.",
                deps: ["DB-01", "DB-02"],
                phase: "P3",
                planRefs: ["p3-engine", "p3-actions"],
                origin: "spec",
                status: "pending",
                notes: "",
            },
            {
                id: "SEQ-02",
                title: "Dynamic messaging template engine",
                description:
                    "Variable insertion ({firstName}, {company}, {position}, {mutualTotal} and {cs_*} custom fields), spintax expansion {a|b|c}, whole-message variations split evenly across the queue, IF/THEN/ELSE on variable presence, and hybrid templates combining a static skeleton with AI-generated fragments.",
                deps: ["DB-02"],
                phase: "P3",
                planRefs: ["p3-templates"],
                origin: "spec",
                status: "pending",
                notes: "",
            },
            {
                id: "SEQ-03",
                title: "Inbox scanner and reply detection engine",
                description:
                    "Poll the messaging inbox on a configurable interval (minimum 1 hour, default 3). Scrape the last 200 threads on first run, deltas after. Match threads to leads in active campaign queues; on a match move the lead to the Replied sub-list and terminate its sequence so no awkward follow-up ever lands. Archive both directions into messages.",
                deps: ["SEQ-01", "DB-04"],
                phase: "P3",
                planRefs: ["p3-replies"],
                origin: "spec",
                status: "pending",
                notes: "Directly satisfies VAL-02.",
            },
            {
                id: "SEQ-04",
                title: "Scheduled background runner for safe batched execution",
                description:
                    "worker.py daemon owning Playwright and the clock. Tick loop selects due jobs, asks the gate, executes, logs, schedules the next step. Bottom-up action priority and bunching mirror the benchmark. Lease-based job locking with heartbeat means a crashed worker's jobs are reclaimable. Survives VS Code restarts and does not care whether an LLM is connected.",
                deps: ["SEQ-01", "CORE-03"],
                phase: "P3",
                planRefs: ["p3-worker"],
                origin: "spec",
                status: "pending",
                notes: "",
            },
            {
                id: "SEQ-05",
                title: "AI drafts queue and ICP qualification gate",
                description:
                    "The worker never calls an LLM. It parks an ai_drafts row with lead context and moves on; the MCP client generates and submits. Four kinds: connection_note, message, comment, and icp_evaluation which returns {match, score, reason} and routes the lead to Success or Failed. Drafts sit at pending_approval until released, with opt-in auto-approve per campaign. The benchmark meters this against a credit pool and exposes no API; here it is free at the margin and fully auditable.",
                deps: ["SEQ-01", "SEQ-02"],
                phase: "P3",
                planRefs: ["p3-drafts", "p3-icp"],
                origin: "audit",
                status: "pending",
                notes: "Added from the benchmark analysis. ICP filtering costs zero LinkedIn actions and protects scarce daily invites.",
            },
        ],
    },
    {
        id: "M5",
        name: "MCP Tool & Resource Exposure Layer",
        blurb: "The control plane. Fast, side-effect-light, never touches a browser.",
        tasks: [
            {
                id: "MCP-01",
                title: "Register campaign control tools",
                description:
                    "campaign_create, campaign_add_step, campaign_set_template, campaign_set_icp, campaign_preview, campaign_approve, campaign_start, campaign_pause, campaign_resume, campaign_archive, campaign_status, campaign_add_leads. campaign_approve is the single human gate: approve the definition once, then autonomous execution inside safety caps.",
                deps: ["SEQ-04"],
                phase: "P4",
                planRefs: ["p3-campaign-tools"],
                origin: "spec",
                status: "pending",
                notes: "",
            },
            {
                id: "MCP-02",
                title: "Register lead extraction tools",
                description:
                    "harvest_people_search, harvest_post_engagers, harvest_group_members, harvest_event_attendees, harvest_company_employees, harvest_connections, harvest_import_csv, harvest_status, plus the CRM read tools lead_search, lead_get, lead_export_csv. Each harvest tool enqueues a job rather than scraping inline.",
                deps: ["SCRAPE-01", "SCRAPE-03", "SCRAPE-04"],
                phase: "P4",
                planRefs: ["p2-crm"],
                origin: "spec",
                status: "pending",
                notes: "harvest_sales_nav intentionally omitted, see SCRAPE-02.",
            },
            {
                id: "MCP-03",
                title: "Register sequence execution tools",
                description:
                    "send_invite, send_message, endorse_skills and friends are exposed as action_enqueue_adhoc rather than direct executors. Even a one-off manual action writes a jobs row so it passes through the same gate, the same jitter and the same ledger. This is what makes the safety guarantee hold rather than being advisory.",
                deps: ["SEQ-04", "CORE-03"],
                phase: "P4",
                planRefs: ["p3-campaign-tools"],
                origin: "spec",
                status: "pending",
                notes: "Deliberate design choice: enqueue, never execute inline.",
            },
            {
                id: "MCP-04",
                title: "Expose MCP Resources",
                description:
                    "linkedin://campaigns, linkedin://campaigns/{id}, linkedin://campaigns/{id}/funnel, linkedin://leads/active, linkedin://leads/{id}, linkedin://drafts/pending, linkedin://inbox/unread, linkedin://worker/status, linkedin://stats/daily, linkedin://safety/today, linkedin://analytics/weekly, linkedin://templates. Emit notifications/resources/updated where the client supports it, fall back to polling elsewhere.",
                deps: ["MCP-01"],
                phase: "P4",
                planRefs: ["p4-resources"],
                origin: "spec",
                status: "pending",
                notes: "Directly satisfies VAL-03.",
            },
            {
                id: "MCP-05",
                title: "Create MCP Prompts for guided workflow setup",
                description:
                    "new_campaign (audience, sequence, templates, limits, then approve), review_drafts, triage_replies, weekly_report, safety_check, harvest_audience. These replace the .github slash commands with client-agnostic MCP prompts so any MCP client gets the same guided flows, not just Copilot in VS Code.",
                deps: ["MCP-01", "MCP-04"],
                phase: "P4",
                planRefs: ["p4-resources", "p4-migrate-agents"],
                origin: "spec",
                status: "pending",
                notes: "",
            },
        ],
    },
];

/** Section 3 - phased roadmap. */
export const PHASES = [
    {
        id: "P1",
        name: "Phase 1 - Foundation, Session & Safety Caps",
        goal: "Make it impossible to exceed a limit, and make sessions actually work.",
        exit: "The 51st invite in 24 hours refuses with a typed DailyCapReached error, refusals outside working hours are logged, duplicates on the same lead are rejected, and every attempt is a row in actions_log.",
        taskIds: ["DB-01", "CORE-01", "CORE-02", "CORE-03", "CORE-04", "CORE-05"],
    },
    {
        id: "P2",
        name: "Phase 2 - Lead Extraction & Database Persistence",
        goal: "A real lead database with real deduplication.",
        exit: "Harvest 500 profiles from a search, run it twice, and get zero duplicates with correct incremental counts.",
        taskIds: ["DB-02", "DB-03", "DB-04", "SCRAPE-01", "SCRAPE-02", "SCRAPE-03", "SCRAPE-04"],
    },
    {
        id: "P3",
        name: "Phase 3 - Multi-Step Campaign Sequence Engine",
        goal: "Sequences that run for days without an LLM attached.",
        exit: "A four-step campaign (ICP filter, Invite, wait 3 days + filter to 1st, Message) runs unattended across a weekend, respects working hours and caps, stops for anyone who replies, and reports a correct funnel.",
        taskIds: ["SEQ-01", "SEQ-02", "SEQ-03", "SEQ-04", "SEQ-05"],
    },
    {
        id: "P4",
        name: "Phase 4 - Full MCP Interface & Tool Binding",
        goal: "Complete the control plane and close the loop with the outside world.",
        exit: "A fresh MCP client with no Copilot-specific files can discover campaigns, inspect the funnel, review drafts and pause the worker using tools, resources and prompts alone.",
        taskIds: ["MCP-01", "MCP-02", "MCP-03", "MCP-04", "MCP-05"],
    },
];

/** Section 4 - validation suite. */
export const VALIDATIONS = [
    {
        id: "VAL-01",
        title: "Daily rate limits block extra calls without crashing the server",
        criteria:
            "Drive the configured daily invite cap to zero, then request one more. Expect a structured DailyCapReached refusal returned as a normal tool result, a corresponding safety_events row, and the MCP server still responsive to every other tool call. No exception escapes, no browser is opened.",
        covers: ["CORE-03", "DB-04"],
        status: "pending",
        result: "",
    },
    {
        id: "VAL-02",
        title: "Sequence halts when a reply is detected",
        criteria:
            "Seed a two-step campaign with a lead, simulate an inbound reply on that thread, run the inbox scanner. Expect the lead to move to the Replied sub-list, its pending job to be cancelled, no follow-up message sent, and the campaign funnel to reflect the change.",
        covers: ["SEQ-03", "SEQ-01"],
        status: "pending",
        result: "",
    },
    {
        id: "VAL-03",
        title: "MCP client reads accurate live campaign state",
        criteria:
            "From an MCP client (Copilot, Claude or Cursor) read linkedin://campaigns/{id} while the worker is mid-run. Expect current step, per-sub-list counts and next scheduled run to match the database exactly, with no stale cache and no server-side mutation caused by the read.",
        covers: ["MCP-04", "MCP-01"],
        status: "pending",
        result: "",
    },
    {
        id: "VAL-04",
        title: "Session cookies persist across restarts without re-login",
        criteria:
            "Log in once, stop the worker and the MCP server, restart both, and perform a read action. Expect no login prompt and no challenge. This is the regression test for the absolute-vs-relative cookie path bug, so run it with the process launched from a working directory other than the repo root.",
        covers: ["CORE-02"],
        status: "pending",
        result: "",
    },
];

/**
 * Board task -> session todo IDs.
 *
 * The session `todos` table is finer-grained than the board (28 todos across 23
 * board tasks), so this is deliberately many-to-many. A todo may appear under
 * more than one task when it genuinely delivers both, for example p1-browser
 * covers the driver wrapper and the per-account persistent context.
 *
 * Rollup rules are defined in todos.mjs:
 *   push (board wins): a todo takes the least advanced status of every task
 *                      that claims it, so it is only done when all of them are.
 *   pull (todos win):  a task is done only when all its todos are done.
 *
 * SCRAPE-02 maps to nothing on purpose. Sales Navigator was descoped, so it
 * never got a todo.
 */
export const TASK_TODOS = {
    "CORE-01": ["p1-browser"],
    "CORE-02": ["p1-browser"],
    "CORE-03": ["p1-config", "p1-gate", "p1-limits", "p1-migrate-tools", "p1-tests", "p1-docs"],
    "CORE-04": ["p1-humanize"],
    "CORE-05": ["p1-detect"],
    "DB-01": ["p1-db", "p1-schema"],
    "DB-02": ["p2-crm", "p1-render"],
    "DB-03": ["p2-extract"],
    "DB-04": ["p1-schema"],
    "SCRAPE-01": ["p2-harvest"],
    "SCRAPE-02": [],
    "SCRAPE-03": ["p2-extract"],
    "SCRAPE-04": ["p2-harvest"],
    "SEQ-01": ["p3-engine", "p3-actions"],
    "SEQ-02": ["p3-templates"],
    "SEQ-03": ["p3-replies"],
    "SEQ-04": ["p3-worker"],
    "SEQ-05": ["p3-drafts", "p3-icp"],
    "MCP-01": ["p3-campaign-tools"],
    "MCP-02": ["p2-harvest"],
    "MCP-03": ["p3-actions", "p4-inbox"],
    "MCP-04": ["p4-resources", "p4-analytics", "p4-webhooks"],
    "MCP-05": ["p4-migrate-agents", "p4-resources"],
};

export function seedState() {
    return {
        version: 1,
        title: "linkedin-helper: MCP-native Linked Helper parity",
        repo: "nived15/linkedin-helper",
        updatedAt: null,
        constraints: CONSTRAINTS,
        modules: MODULES,
        phases: PHASES,
        validations: VALIDATIONS,
    };
}
