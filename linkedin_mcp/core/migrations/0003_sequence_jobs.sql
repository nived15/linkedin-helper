-- SEQ-01 sequence engine: make "one open job per lead" a database invariant.
-- Timestamp columns elsewhere in this schema use UTC text values compatible with
-- SQLite CURRENT_TIMESTAMP; this migration adds no columns.

-- A database written before this migration could already hold duplicate open
-- jobs for one lead, which is precisely the corruption the rebuild path exists
-- to repair. Creating the index first would fail on exactly those databases and
-- abort initialization before anything could repair them, so the duplicates are
-- quarantined here. The lowest id survives, the rest are cancelled with a note,
-- and `rebuild_jobs` regenerates whatever the campaign state actually implies.
UPDATE jobs
SET state = 'cancelled',
    last_error = 'superseded by the 0003 one-open-job-per-lead index; '
                 || 'rebuild the queue from campaign state',
    locked_by = NULL,
    locked_at = NULL
WHERE state IN ('pending', 'leased')
  AND campaign_id IS NOT NULL
  AND lead_id IS NOT NULL
  AND id NOT IN (
      SELECT MIN(id)
      FROM jobs
      WHERE state IN ('pending', 'leased')
        AND campaign_id IS NOT NULL
        AND lead_id IS NOT NULL
      GROUP BY campaign_id, lead_id
  );

-- `jobs` is a projection of `campaign_leads`, and the projection is one open job
-- per lead still in the flow. Enforcing that here means a duplicate cannot be
-- written by a racing worker or by a rebuild that ran twice: the second insert
-- fails and its whole transaction rolls back, rather than the lead quietly
-- acquiring two jobs that both send the same message.
--
-- The index is partial, so closed jobs are outside it and a lead accumulates as
-- much execution history as it likes. NULL campaign_id or lead_id rows are also
-- outside it, because SQLite treats NULLs as distinct in a unique index, which
-- leaves ad-hoc non-campaign jobs unconstrained.
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_one_open_per_lead
    ON jobs (campaign_id, lead_id)
    WHERE state IN ('pending', 'leased')
      AND campaign_id IS NOT NULL
      AND lead_id IS NOT NULL;
