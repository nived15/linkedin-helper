"""The inbox scan itself: one paginated pass over the conversation list.

What a run does
---------------
Walks the messaging list a slice at a time, opens every conversation that has
changed since the last scan, archives both directions into `messages`, and, when
the other person has said something, moves the lead to `replied` in every active
campaign queue so no queued follow-up survives.

First run and delta
-------------------
The first run for an account walks back through 200 threads. Every run after it
is a delta, and the delta is real rather than a filter applied after the fact:
the watermark from the previous run is handed to `paginate` as the keys it has
already seen, so a first slice in which nothing has changed produces no fresh
results and the loop stops on `no_new_results` after **one** fetch instead of
twenty. A thread with a new message presents a different key, because the key
carries a change signature as well as the thread's address, so it is fresh and
gets opened.

Budget: `feed_browse`
---------------------
Deliberate, and it is a trade-off worth stating. `message` is the semantically
obvious candidate and is the wrong answer: its 50/day and 250/week ceilings are a
*sending* budget, and spending it on reads would starve the campaigns this
feature exists to protect. A 200 thread first run would eat most of a day's
outreach. `post_read` is SCRAPE-04's read budget but there is no post here, and
it is the budget lead harvesting competes for.

`feed_browse` is what is left and it fits: the inbox is a first-party, logged-in,
lazily loaded list reached from the nav bar, which is exactly what the feed is.
Neither surface sends anything and neither generates leads. Its 40/day ceiling
comfortably covers eight scans a day of a slice or two each, and it makes a 200
thread backfill visibly expensive, which it should be. No new action type was
invented, so the guard test that fails the build on an unconfigured type stays
satisfied.

Thread opens are metered with their slice rather than one by one, which is the
precedent SCRAPE-04 set for a reactions modal: revealing more of a list already
on screen is one interaction, not one per row. They are counted onto the
`harvest_runs` row so a run's real cost is auditable, and never into
`actions_log`, because a second row per thread would double-meter the same
budget.

Boundaries
----------
No daemon, no thread and no timer live here. SEQ-04 (#22) owns the tick loop and
calls :func:`~linkedin_mcp.inbox.policy.scan_due` and then
:func:`run_inbox_scan`. Challenge detection is the navigation layer's, reached
through `paginate`'s session check, so this module keeps no marker list of its
own. Registering an MCP tool for any of this is a later issue.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from linkedin_mcp.browser.humanize import Humanizer, get_humanizer
from linkedin_mcp.inbox.archive import archive_thread
from linkedin_mcp.inbox.matching import resolve_lead, terminate_sequences
from linkedin_mcp.inbox.policy import (
    INBOX_SOURCE,
    InboxScanState,
    read_scan_state,
    resolve_poll_seconds,
    trim_watermark,
)
from linkedin_mcp.inbox.threads import (
    InboxThread,
    list_thread_rows,
    open_thread,
    read_thread_messages,
)
from linkedin_mcp.scrape.extract import query_first
from linkedin_mcp.scrape.paginate import (
    SCROLL_DISTANCE,
    GuardFn,
    RecordFn,
    SearchCursor,
    StopReason,
    assert_session_alive,
    paginate,
)
from linkedin_mcp.scrape.runs import finish_harvest_run, start_harvest_run
from linkedin_mcp.scrape.sources import PAGE_TIMEOUT_MS
from linkedin_mcp.sequences.transaction import now_timestamp

logger = logging.getLogger(__name__)

__all__ = [
    "INBOX_ACTION",
    "INBOX_MAX_SLICES",
    "INBOX_URL",
    "InboxScanReport",
    "run_inbox_scan",
]

INBOX_URL = "https://www.linkedin.com/messaging/"
"""Where the conversation list lives."""

INBOX_ACTION = "feed_browse"
"""Budget one slice of the conversation list spends. Configured at 40 a day.

See the module docstring for why this is not `message` and not `post_read`.
"""

INBOX_MAX_SLICES = 60
"""Slices one run will ask for before it calls the list exhausted.

Two hundred threads at roughly twenty a slice is ten, so this is generous
headroom for a list that reveals fewer, and a hard stop for one that keeps
serving the same rows forever.
"""


@dataclass(frozen=True, slots=True)
class InboxScanReport:
    """What one inbox scan read, archived and stopped."""

    account_id: int
    first_run: bool
    poll_seconds: int
    stop_reason: StopReason
    cursor: SearchCursor = field(default_factory=SearchCursor)
    slices_fetched: int = 0
    threads_seen: int = 0
    threads_changed: int = 0
    threads_opened: int = 0
    threads_unresolved: tuple[str, ...] = ()
    threads_unreadable: tuple[str, ...] = ()
    messages_archived: int = 0
    messages_already_stored: int = 0
    replies_detected: int = 0
    leads_replied: tuple[int, ...] = ()
    campaigns_stopped: tuple[tuple[int, int], ...] = ()
    gate_refusal: Mapping[str, Any] | None = None
    harvest_run_id: int | None = None
    scanned_at: str | None = None

    @property
    def refused(self) -> bool:
        """True when the safety gate ended the run."""
        return self.stop_reason is StopReason.GATE_REFUSED

    @property
    def sequences_terminated(self) -> int:
        """Campaign queues this run stopped because the lead answered."""
        return len(self.campaigns_stopped)

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON friendly payload, suitable for an MCP tool result."""
        return {
            "status": "refused" if self.refused else "success",
            "account_id": self.account_id,
            "source": INBOX_SOURCE,
            "action_type": INBOX_ACTION,
            "first_run": self.first_run,
            "poll_seconds": self.poll_seconds,
            "stop_reason": self.stop_reason.value,
            "slices_fetched": self.slices_fetched,
            "threads_seen": self.threads_seen,
            "threads_changed": self.threads_changed,
            "threads_opened": self.threads_opened,
            "threads_unresolved": list(self.threads_unresolved),
            "threads_unreadable": list(self.threads_unreadable),
            "messages_archived": self.messages_archived,
            "messages_already_stored": self.messages_already_stored,
            "replies_detected": self.replies_detected,
            "leads_replied": list(self.leads_replied),
            "campaigns_stopped": [
                {"campaign_id": campaign_id, "lead_id": lead_id}
                for campaign_id, lead_id in self.campaigns_stopped
            ],
            "sequences_terminated": self.sequences_terminated,
            "gate_refusal": dict(self.gate_refusal) if self.gate_refusal else None,
            "cursor": self.cursor.as_dict(),
            "harvest_run_id": self.harvest_run_id,
            "scanned_at": self.scanned_at,
        }


async def _reveal_more(page: Any, pacer: Humanizer) -> None:
    """Advance the lazily loaded conversation list by one slice.

    Two gestures, neither required. LinkedIn reveals more conversations on
    scroll on some renders and only on a button on others. When neither reveals
    anything the paged loop stops on no-new-results, which is an ordinary
    outcome rather than an error.
    """
    await pacer.scroll(page, SCROLL_DISTANCE)
    try:
        control = await query_first(page, "inbox_thread_load_more")
    except Exception as error:  # noqa: BLE001 - a bad selector is not fatal
        logger.debug("Looking for the inbox load-more control failed: %s", error)
        return
    if control is None:
        return
    try:
        await pacer.click(control)
    except Exception as error:  # noqa: BLE001 - a stale control is not fatal
        logger.debug("Clicking the inbox load-more control failed: %s", error)


async def run_inbox_scan(
    page: Any,
    conn: sqlite3.Connection,
    account_id: int,
    *,
    poll_seconds: int | None = None,
    strict_interval: bool = False,
    limit: int | None = None,
    terminate_on_reply: bool = True,
    cursor: SearchCursor | None = None,
    humanizer: Humanizer | None = None,
    guard: GuardFn | None = None,
    record: RecordFn | None = None,
    clock: Any = None,
    state: InboxScanState | None = None,
    manage_run: bool = True,
    run_id: int | None = None,
    timeout: int = PAGE_TIMEOUT_MS,
) -> InboxScanReport:
    """Scan the messaging inbox once and act on every reply it finds.

    This is a plain callable. It schedules nothing; SEQ-04's runner decides when
    to call it, using :func:`~linkedin_mcp.inbox.policy.scan_due`.

    Args:
        page: Playwright page already signed in to LinkedIn.
        conn: Open connection to the MCP database.
        account_id: Account whose inbox is being read.
        poll_seconds: Interval to store for the next due check. Clamped to the
            one hour floor, or refused when `strict_interval` is set.
        strict_interval: Raise on an interval below the floor instead of clamping.
        limit: Threads this run will look at. Defaults to 200 on a first run and
            to the delta limit afterwards.
        terminate_on_reply: Stop the sequences of a lead who has replied. On by
            default, because that is the point of the feature. Turning it off
            archives and builds the watermark without moving anybody, which is
            how an operator seeds an inbox full of years-old conversations
            before letting a first scan act on them.
        cursor: Resume point. Defaults to the watermark of the last scan.
        humanizer: Pacing. Defaults to the process-wide humanizer.
        guard: Safety gate. Defaults to `guard_action`.
        record: Audit writer. Defaults to `log_action`.
        clock: Decision time source, injected so a runner stays deterministic.
        state: Pre-read scan state, for a caller that already has it.
        manage_run: Open and close the `harvest_runs` row here.
        run_id: Existing run row this scan belongs to.
        timeout: Navigation timeout in milliseconds.
    """
    pacer = humanizer or get_humanizer()
    tick = clock or (lambda: datetime.now(timezone.utc))
    started = tick()

    previous = state if state is not None else read_scan_state(conn, account_id)
    interval = resolve_poll_seconds(
        previous.poll_seconds if poll_seconds is None else poll_seconds,
        strict=strict_interval,
    )
    first_run = previous.first_run
    wanted = int(limit) if limit is not None else previous.thread_limit
    if wanted < 1:
        raise ValueError(f"an inbox scan needs to look at at least one thread, got {wanted}")

    resume = cursor if cursor is not None else SearchCursor(seen_keys=previous.seen_keys)

    watermark: dict[str, str] = {}
    handles: dict[str, Any] = {}
    unresolved: list[str] = []
    unreadable: list[str] = []
    replied_leads: list[int] = []
    stopped: list[tuple[int, int]] = []
    archived = 0
    already_stored = 0
    opened = 0
    replies = 0
    loaded = False

    async def fetch(target: Any, step: int) -> None:
        nonlocal loaded
        if not loaded:
            # The messaging route is an ordinary application page rather than an
            # `/in/` profile, so CORE-04's direct profile load cap does not
            # apply and a plain goto is the honest way to reach it.
            await target.goto(INBOX_URL, wait_until="domcontentloaded", timeout=timeout)
            loaded = True
            for _ in range(step - 1):
                await _reveal_more(target, pacer)
            return
        await _reveal_more(target, pacer)

    async def extract(target: Any) -> list[InboxThread]:
        rows = await list_thread_rows(target)
        handles.clear()
        for thread, handle in rows:
            handles[thread.dedupe_key] = handle
        return [thread for thread, _ in rows]

    async def on_page(threads: list[InboxThread], step: int) -> None:
        nonlocal archived, already_stored, opened, replies
        for thread in threads:
            handle = handles.get(thread.dedupe_key)
            if not await open_thread(page, handle, pacer):
                # Not watermarked. A row that would not open is a transient
                # miss, and marking it seen would retire it forever unread.
                unreadable.append(thread.thread_urn)
                continue
            await pacer.settle()
            await assert_session_alive(
                page, account_id=account_id, action_type=INBOX_ACTION
            )
            opened += 1

            messages = await read_thread_messages(page, thread)
            if not messages:
                unreadable.append(thread.thread_urn)
                continue

            # From here the thread has been read, so it is safe to remember.
            watermark[thread.thread_urn] = thread.signature
            opened_thread = thread.with_messages(messages)

            lead = resolve_lead(conn, account_id, opened_thread)
            if lead is None:
                # A recruiter, a colleague or a newsletter. Reported, never
                # forced into a campaign and never invented as a lead.
                unresolved.append(thread.thread_urn)
                logger.debug(
                    "Inbox thread %s is with somebody who is not a known lead",
                    thread.thread_urn,
                )
                continue

            result = archive_thread(
                conn,
                account_id,
                lead.id,
                opened_thread,
                detected_at=tick(),
            )
            archived += result.inserted
            already_stored += result.skipped

            if not opened_thread.has_reply:
                continue
            replies += 1
            replied_leads.append(lead.id)
            if not terminate_on_reply:
                continue
            stopped.extend(
                (campaign_id, lead.id)
                for campaign_id in terminate_sequences(
                    conn, account_id, lead.id, now=tick()
                )
            )
        logger.debug("Inbox slice %d handled %d changed thread(s)", step, len(threads))

    run_params: dict[str, Any] = {
        "url": INBOX_URL,
        "poll_seconds": interval,
        "first_run": first_run,
        "terminate_on_reply": terminate_on_reply,
    }
    if manage_run and run_id is None:
        run_id = start_harvest_run(
            conn,
            account_id,
            INBOX_SOURCE,
            run_params,
            cursor=resume,
            started_at=started,
        )

    run = await paginate(
        page,
        action_type=INBOX_ACTION,
        account_id=account_id,
        fetch=fetch,
        extract=extract,
        key=lambda thread: thread.dedupe_key,
        limit=wanted,
        cursor=resume,
        humanizer=pacer,
        guard=guard,
        record=record,
        clock=tick,
        on_page=on_page,
        result_ceiling=resume.collected + wanted,
        max_page=INBOX_MAX_SLICES,
        scroll_before_extract=False,
        detail={"source": INBOX_SOURCE},
    )

    # Newly seen threads first, so the bounded watermark keeps the live end of
    # the inbox and forgets the quiet tail. A thread seen again this run keeps
    # its *new* signature, which is what makes the next delta correct.
    merged_source = dict(watermark)
    for urn, signature in previous.watermark.items():
        merged_source.setdefault(urn, signature)
    merged = trim_watermark(merged_source)
    finished = now_timestamp(tick())
    run_params.update(
        {
            "watermark": merged,
            "threads_opened": opened,
            "threads_changed": len(run.results),
            "messages_archived": archived,
            "replies_detected": replies,
            "stop_reason": run.stop_reason.value,
        }
    )

    if manage_run and run_id is not None:
        finish_harvest_run(
            conn,
            run_id,
            found=run.results_seen,
            new=len(run.results),
            cursor=run.cursor,
            params=run_params,
            finished_at=finished,
        )

    return InboxScanReport(
        account_id=account_id,
        first_run=first_run,
        poll_seconds=interval,
        stop_reason=run.stop_reason,
        cursor=run.cursor,
        slices_fetched=run.pages_fetched,
        threads_seen=run.results_seen,
        threads_changed=len(run.results),
        threads_opened=opened,
        threads_unresolved=tuple(unresolved),
        threads_unreadable=tuple(unreadable),
        messages_archived=archived,
        messages_already_stored=already_stored,
        replies_detected=replies,
        leads_replied=tuple(dict.fromkeys(replied_leads)),
        campaigns_stopped=tuple(stopped),
        gate_refusal=run.gate_refusal,
        harvest_run_id=run_id,
        scanned_at=finished,
    )
