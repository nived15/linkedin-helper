-- DB-03 deduplication engine: section cache bookkeeping and displaced identifier history.
-- Timestamp columns use UTC text values compatible with SQLite CURRENT_TIMESTAMP.

ALTER TABLE leads ADD COLUMN contact_info_fetched_at TEXT;

ALTER TABLE leads ADD COLUMN positions_fetched_at TEXT;

CREATE TABLE IF NOT EXISTS lead_identity_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('member_id', 'public_id', 'hash_id')),
    value TEXT NOT NULL, -- Identifier the lead used to hold, kept so a merge never loses one.
    replaced_by TEXT, -- New value on the same lead, or NULL when another lead claimed it.
    claimed_by_lead_id INTEGER,
    observed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (lead_id) REFERENCES leads (id) ON DELETE CASCADE,
    FOREIGN KEY (claimed_by_lead_id) REFERENCES leads (id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_lead_identity_history_lead
    ON lead_identity_history (lead_id, observed_at);

CREATE INDEX IF NOT EXISTS idx_lead_identity_history_value
    ON lead_identity_history (kind, value);
