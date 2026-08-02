"""SEQ-03: inbox scanning and reply detection.

Why this exists
---------------
A sequence that keeps sending after somebody has answered is the single most
visible way this system can embarrass its owner. Everything in this package
exists to make sure that a reply stops the follow-up *before* it lands, not
after somebody notices.

What a scan does, in order
--------------------------
1. Walks LinkedIn's conversation list a slice at a time, asking the safety gate
   before every slice, pacing between them and letting the navigation layer
   detect a checkpoint.
2. Opens every conversation that has changed since the last scan and reads both
   directions out of it.
3. Archives what it read into `messages`, deduplicated as a multiset so the same
   message is never stored twice and a genuine repeat is never lost.
4. Resolves the conversation to a lead, and that lead to every runnable campaign
   queue it is still sitting in.
5. Calls :func:`~linkedin_mcp.sequences.transitions.mark_replied`, which moves
   the lead to `replied` and cancels its open job in the same transaction, so
   the follow-up already sitting in the queue never runs.

Step 5 is the point of the whole issue. A scanner that detects a reply but
leaves a queued message behind has failed even with every other box ticked, so
the acceptance test asserts on the surviving job rather than on the sub-list.

This package never
------------------
- schedules anything. SEQ-04 (#22) owns the tick loop. What is offered here is
  :func:`~linkedin_mcp.inbox.policy.scan_due`, a plain due check, and
  :func:`~linkedin_mcp.inbox.scan.run_inbox_scan`, a plain callable.
- keeps its own list of challenge or authwall markers. CORE-05 owns detection
  and this package reaches it through `paginate`'s session check.
- invents an action type. Slices are metered as `feed_browse`; see
  :mod:`linkedin_mcp.inbox.scan` for why that and not `message`.
- redefines "active". `RUNNABLE_STATUSES` and `ACTIVE_SUBLISTS` already say what
  that means and this package joins on them.
- creates a lead. A message from somebody the database has never seen is
  reported as unresolved, not harvested and not forced into a campaign.

Known gaps
----------
The `inbox_*` selectors are hypotheses about LinkedIn's markup. They cannot be
verified without a live logged-in session, which no offline test can stand in
for. Every group has a fallback chain ending in a structural selector, every
optional field reads as None rather than raising, and a thread that cannot be
opened or read is reported and left out of the watermark so the next scan tries
it again. What that buys is a wrong guess costing a thread instead of the run;
it does not buy certainty that the first guess is right.

Relative timestamps ("2h", "Aug 1") are not converted into instants. `sent_at`
is filled only from machine readable markup, because an archive that invents
times is worse than one that admits it does not know them.

Any reply stops the sequence, including an old one. The first run walks back
through two hundred conversations, so a lead who answered eighteen months ago
and was enrolled in a campaign yesterday is moved to `replied` on that first
scan. That is the behaviour the issue asks for, and on its own terms it is
right: somebody with an open conversation should not be receiving automated
sequence messages. It is still a surprise the first time, and because the
relative timestamps cannot be parsed there is no reliable way to tell an old
reply from a new one. The lever for it is
`run_inbox_scan(..., terminate_on_reply=False)`, which archives the history and
builds the watermark without moving anybody, so a subsequent scan only ever acts
on conversations that have changed since.
"""

from linkedin_mcp.inbox.archive import (
    ArchiveResult,
    archive_thread,
    existing_message_keys,
)
from linkedin_mcp.inbox.errors import InboxError, PollIntervalTooShortError
from linkedin_mcp.inbox.matching import (
    REPLY_DETAIL,
    ThreadMatch,
    active_campaign_leads,
    match_thread,
    resolve_lead,
    terminate_sequences,
)
from linkedin_mcp.inbox.policy import (
    DEFAULT_POLL_SECONDS,
    DELTA_THREAD_LIMIT,
    FIRST_RUN_THREAD_LIMIT,
    INBOX_SOURCE,
    MIN_POLL_SECONDS,
    WATERMARK_LIMIT,
    InboxScanState,
    next_scan_at,
    read_scan_state,
    resolve_poll_seconds,
    scan_due,
    thread_key,
    trim_watermark,
)
from linkedin_mcp.inbox.scan import (
    INBOX_ACTION,
    INBOX_MAX_SLICES,
    INBOX_URL,
    InboxScanReport,
    run_inbox_scan,
)
from linkedin_mcp.inbox.threads import (
    INBOUND,
    OUTBOUND,
    InboxThread,
    ThreadMessage,
    extract_threads,
    list_thread_rows,
    open_thread,
    read_thread_messages,
    thread_urn_from,
)

__all__ = [
    "ArchiveResult",
    "DEFAULT_POLL_SECONDS",
    "DELTA_THREAD_LIMIT",
    "FIRST_RUN_THREAD_LIMIT",
    "INBOUND",
    "INBOX_ACTION",
    "INBOX_MAX_SLICES",
    "INBOX_SOURCE",
    "INBOX_URL",
    "InboxError",
    "InboxScanReport",
    "InboxScanState",
    "InboxThread",
    "MIN_POLL_SECONDS",
    "OUTBOUND",
    "PollIntervalTooShortError",
    "REPLY_DETAIL",
    "ThreadMatch",
    "ThreadMessage",
    "WATERMARK_LIMIT",
    "active_campaign_leads",
    "archive_thread",
    "existing_message_keys",
    "extract_threads",
    "list_thread_rows",
    "match_thread",
    "next_scan_at",
    "open_thread",
    "read_scan_state",
    "read_thread_messages",
    "resolve_lead",
    "resolve_poll_seconds",
    "run_inbox_scan",
    "scan_due",
    "terminate_sequences",
    "thread_key",
    "thread_urn_from",
    "trim_watermark",
]
