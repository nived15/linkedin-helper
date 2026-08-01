CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL,
    linkedin_public_id TEXT,
    timezone TEXT NOT NULL,
    proxy_url TEXT,
    user_agent TEXT,
    fingerprint_json TEXT NOT NULL DEFAULT '{}',
    browser_profile_dir TEXT,
    state TEXT NOT NULL CHECK (state IN ('active', 'paused', 'cooldown', 'challenged', 'logged_out')),
    account_age_days INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS account_limits (
    account_id INTEGER NOT NULL,
    action_type TEXT NOT NULL,
    daily_cap INTEGER,
    weekly_cap INTEGER,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    PRIMARY KEY (account_id, action_type),
    FOREIGN KEY (account_id) REFERENCES accounts (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS working_hours (
    account_id INTEGER NOT NULL,
    weekday INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 6),
    start_minute INTEGER NOT NULL CHECK (start_minute BETWEEN 0 AND 1439),
    end_minute INTEGER NOT NULL CHECK (end_minute BETWEEN 0 AND 1439),
    PRIMARY KEY (account_id, weekday),
    FOREIGN KEY (account_id) REFERENCES accounts (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    member_id TEXT,
    public_id TEXT,
    hash_id TEXT,
    full_name TEXT NOT NULL,
    first_name TEXT,
    last_name TEXT,
    headline TEXT,
    summary TEXT,
    organization_name TEXT,
    organization_title TEXT,
    location_name TEXT,
    member_distance TEXT,
    connection_count INTEGER,
    follower_count INTEGER,
    connected_at TEXT,
    badges_json TEXT NOT NULL DEFAULT '{}',
    avatar_url TEXT,
    first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_visited_at TEXT,
    FOREIGN KEY (account_id) REFERENCES accounts (id) ON DELETE CASCADE,
    UNIQUE (account_id, member_id),
    UNIQUE (account_id, public_id)
);

CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    color TEXT,
    FOREIGN KEY (account_id) REFERENCES accounts (id) ON DELETE CASCADE,
    UNIQUE (account_id, name)
);

CREATE TABLE IF NOT EXISTS campaigns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('draft', 'pending_approval', 'active', 'paused', 'completed', 'archived')),
    approval_mode TEXT NOT NULL CHECK (approval_mode IN ('auto', 'manual_drafts')),
    exclude_list_id INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at TEXT,
    paused_at TEXT,
    FOREIGN KEY (account_id) REFERENCES accounts (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    body TEXT NOT NULL,
    variations_json TEXT NOT NULL DEFAULT '[]',
    kind TEXT NOT NULL CHECK (kind IN ('static', 'ai', 'hybrid')),
    ai_spec_json TEXT NOT NULL DEFAULT '{}',
    is_ai_generated INTEGER NOT NULL DEFAULT 0 CHECK (is_ai_generated IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (account_id) REFERENCES accounts (id) ON DELETE CASCADE,
    UNIQUE (account_id, name)
);

CREATE TABLE IF NOT EXISTS campaign_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL,
    ord INTEGER NOT NULL,
    action_type TEXT NOT NULL,
    config_json TEXT NOT NULL DEFAULT '{}',
    template_id INTEGER,
    bunch_size INTEGER NOT NULL DEFAULT 1,
    on_failure TEXT,
    on_missing_data TEXT CHECK (on_missing_data IN ('visit_extract', 'skip')),
    FOREIGN KEY (campaign_id) REFERENCES campaigns (id) ON DELETE CASCADE,
    FOREIGN KEY (template_id) REFERENCES templates (id) ON DELETE SET NULL,
    UNIQUE (campaign_id, ord)
);

CREATE TABLE IF NOT EXISTS actions_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    lead_id INTEGER,
    campaign_id INTEGER,
    step_id INTEGER,
    action_type TEXT NOT NULL,
    outcome TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    occurred_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (account_id) REFERENCES accounts (id) ON DELETE CASCADE,
    FOREIGN KEY (lead_id) REFERENCES leads (id) ON DELETE SET NULL,
    FOREIGN KEY (campaign_id) REFERENCES campaigns (id) ON DELETE SET NULL,
    FOREIGN KEY (step_id) REFERENCES campaign_steps (id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS safety_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    severity TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    occurred_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (account_id) REFERENCES accounts (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS worker_heartbeat (
    worker_id TEXT PRIMARY KEY,
    account_id INTEGER NOT NULL,
    last_tick_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    status TEXT NOT NULL,
    current_job_id INTEGER,
    FOREIGN KEY (account_id) REFERENCES accounts (id) ON DELETE CASCADE,
    FOREIGN KEY (current_job_id) REFERENCES jobs (id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS lead_contacts (
    lead_id INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('email', 'work_email', 'personal_email', 'phone', 'website', 'twitter')),
    value TEXT NOT NULL,
    source TEXT,
    verified_at TEXT,
    PRIMARY KEY (lead_id, kind, value),
    FOREIGN KEY (lead_id) REFERENCES leads (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS lead_experience (
    lead_id INTEGER NOT NULL,
    ord INTEGER NOT NULL,
    title TEXT,
    company TEXT,
    company_id TEXT,
    start_date TEXT,
    end_date TEXT,
    location TEXT,
    PRIMARY KEY (lead_id, ord),
    FOREIGN KEY (lead_id) REFERENCES leads (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS lead_education (
    lead_id INTEGER NOT NULL,
    ord INTEGER NOT NULL,
    school TEXT,
    degree TEXT,
    field TEXT,
    start_year INTEGER,
    end_year INTEGER,
    PRIMARY KEY (lead_id, ord),
    FOREIGN KEY (lead_id) REFERENCES leads (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS lead_skills (
    lead_id INTEGER NOT NULL,
    skill TEXT NOT NULL,
    endorsement_count INTEGER,
    PRIMARY KEY (lead_id, skill),
    FOREIGN KEY (lead_id) REFERENCES leads (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS lead_tags (
    lead_id INTEGER NOT NULL,
    tag_id INTEGER NOT NULL,
    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    applied_by TEXT,
    PRIMARY KEY (lead_id, tag_id),
    FOREIGN KEY (lead_id) REFERENCES leads (id) ON DELETE CASCADE,
    FOREIGN KEY (tag_id) REFERENCES tags (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS lead_custom_fields (
    lead_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    value TEXT,
    PRIMARY KEY (lead_id, key),
    FOREIGN KEY (lead_id) REFERENCES leads (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS blacklist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    member_id TEXT,
    public_id TEXT,
    reason TEXT,
    added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (account_id) REFERENCES accounts (id) ON DELETE CASCADE,
    UNIQUE (account_id, member_id),
    UNIQUE (account_id, public_id)
);

CREATE TABLE IF NOT EXISTS campaign_leads (
    campaign_id INTEGER NOT NULL,
    lead_id INTEGER NOT NULL,
    current_step_ord INTEGER NOT NULL,
    sublist TEXT NOT NULL CHECK (sublist IN ('queue', 'processing', 'successful', 'failed', 'replied', 'skipped', 'excluded')),
    next_run_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_outcome TEXT,
    entered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (campaign_id, lead_id),
    FOREIGN KEY (campaign_id) REFERENCES campaigns (id) ON DELETE CASCADE,
    FOREIGN KEY (lead_id) REFERENCES leads (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    campaign_id INTEGER,
    lead_id INTEGER,
    step_id INTEGER,
    action_type TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    scheduled_for TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL CHECK (state IN ('pending', 'leased', 'done', 'failed', 'cancelled', 'refused')),
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    locked_by TEXT,
    locked_at TEXT,
    FOREIGN KEY (account_id) REFERENCES accounts (id) ON DELETE CASCADE,
    FOREIGN KEY (campaign_id) REFERENCES campaigns (id) ON DELETE SET NULL,
    FOREIGN KEY (lead_id) REFERENCES leads (id) ON DELETE SET NULL,
    FOREIGN KEY (step_id) REFERENCES campaign_steps (id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS ai_drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    campaign_id INTEGER,
    lead_id INTEGER,
    step_id INTEGER,
    kind TEXT NOT NULL CHECK (kind IN ('connection_note', 'message', 'comment', 'icp_evaluation')),
    context_json TEXT NOT NULL DEFAULT '{}',
    generated_text TEXT,
    verdict_json TEXT,
    status TEXT NOT NULL CHECK (status IN ('needs_generation', 'pending_approval', 'approved', 'rejected', 'sent')),
    model TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    decided_at TEXT,
    FOREIGN KEY (account_id) REFERENCES accounts (id) ON DELETE CASCADE,
    FOREIGN KEY (campaign_id) REFERENCES campaigns (id) ON DELETE SET NULL,
    FOREIGN KEY (lead_id) REFERENCES leads (id) ON DELETE SET NULL,
    FOREIGN KEY (step_id) REFERENCES campaign_steps (id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    lead_id INTEGER NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('outbound', 'inbound')),
    body TEXT NOT NULL,
    thread_urn TEXT,
    sent_at TEXT,
    detected_at TEXT,
    FOREIGN KEY (account_id) REFERENCES accounts (id) ON DELETE CASCADE,
    FOREIGN KEY (lead_id) REFERENCES leads (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS webhooks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    trigger TEXT NOT NULL CHECK (trigger IN ('step_reached', 'reply_detected', 'lead_harvested', 'invite_accepted')),
    secret TEXT,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    FOREIGN KEY (account_id) REFERENCES accounts (id) ON DELETE CASCADE,
    UNIQUE (account_id, name)
);

CREATE TABLE IF NOT EXISTS webhook_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    webhook_id INTEGER NOT NULL,
    lead_id INTEGER,
    payload_json TEXT NOT NULL,
    status_code INTEGER,
    attempts INTEGER NOT NULL DEFAULT 0,
    delivered_at TEXT,
    FOREIGN KEY (webhook_id) REFERENCES webhooks (id) ON DELETE CASCADE,
    FOREIGN KEY (lead_id) REFERENCES leads (id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS harvest_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    source_type TEXT NOT NULL,
    params_json TEXT NOT NULL DEFAULT '{}',
    found_count INTEGER NOT NULL DEFAULT 0,
    new_count INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TEXT,
    FOREIGN KEY (account_id) REFERENCES accounts (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_actions_log_account_action_time
    ON actions_log (account_id, action_type, occurred_at);

CREATE INDEX IF NOT EXISTS idx_safety_events_account_time
    ON safety_events (account_id, occurred_at);

CREATE INDEX IF NOT EXISTS idx_campaign_leads_scheduler
    ON campaign_leads (campaign_id, sublist, next_run_at);

CREATE INDEX IF NOT EXISTS idx_jobs_scheduler
    ON jobs (account_id, state, scheduled_for, priority);

CREATE INDEX IF NOT EXISTS idx_messages_lead_detected
    ON messages (lead_id, detected_at);

CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_webhook_time
    ON webhook_deliveries (webhook_id, delivered_at);
