-- MCP-05 (#28): a pause both job lanes honour.
--
-- Why this needs a table at all
-- ----------------------------
-- `campaign_pause` writes `campaigns.status = 'paused'` and
-- `linkedin_mcp.sequences.jobs.due_jobs` inner-joins `campaigns` on
-- `RUNNABLE_STATUSES`, so a paused campaign yields no campaign work. The ad-hoc
-- lane in `linkedin_mcp.worker.selection.ad_hoc_due_jobs` keys on
-- `campaign_id IS NULL` and never consults a campaign at all, so pausing every
-- campaign leaves it running. Measured on `2a34682`: one campaign, paused, one
-- ad-hoc `profile_view` enqueued, and `select_due_jobs` still returned the
-- ad-hoc job. There was no worker-level off switch anywhere in the 40 tools.
--
-- Why not reuse `accounts.state = 'paused'`
-- ----------------------------------------
-- That column is the detection subsystem's. `linkedin_mcp.safety.detect` writes
-- `challenged` and `logged_out` into it and ranks the transitions, so an
-- operator pause written there would race challenge escalation and an operator
-- resume could clear a challenge nobody has actually resolved. It is also the
-- wrong shape for the job: the safety gate reads it only for metered actions, so
-- an unmetered local step would still run, and nothing in job selection reads it
-- at all. A pause that lets work be selected, leased and refused one job at a
-- time is not a pause, it is churn that writes refusal rows and safety events.
--
-- One row per account. `paused` is the gate; the rest is provenance, so a client
-- reading `linkedin://worker/status` can say who stopped the worker and why
-- rather than only that it is stopped.
CREATE TABLE IF NOT EXISTS worker_control (
    account_id INTEGER PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
    paused INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0, 1)),
    reason TEXT,
    paused_by TEXT,
    paused_at TEXT,
    resumed_at TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
