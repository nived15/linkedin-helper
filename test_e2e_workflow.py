#!/usr/bin/env python3
"""
End-to-end LinkedIn MCP workflow test.

Steps exercised:
  1. Session check — login_once.py already ran; we verify cookies exist and are fresh.
  2. Search "Solution Engineer Microsoft UAE" — page 1, then page 2.
  3. Scrape profile details for 3-4 users from the search results.
  4. Send one connection request with a personalised note → Velayudhan C P.
  5. Follow one profile → Parvathy S Raj.
  6. Like AND comment on one LinkedIn post → Perinbaraj Thangavel's latest post.

How it works
------------
MCP tools write rows to a SQLite jobs table and return a job_id immediately.
The worker (worker.py) is the only process that drives the browser; it leases a
pending job, runs the safety gate, waits out the jitter, acts, and writes the
result back onto the job payload.  This script:
  - Queues each job in the right order.
  - Starts the worker as a subprocess.
  - Polls the jobs table until every job reaches a terminal state.
  - Pretty-prints the results.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

# --------------------------------------------------------------------------- #
# Bootstrap: environment, DB, account                                         #
# --------------------------------------------------------------------------- #

load_dotenv(Path(__file__).parent / ".env")

from linkedin_mcp.audit import AuditLog, set_audit_log
from linkedin_mcp.audit.instrument import set_account_resolver
from linkedin_mcp.core.db import DEFAULT_DB_PATH
from linkedin_mcp.executors.contract import RESULT_KEY, adhoc_jobs
from linkedin_mcp.sequences import JobState
from linkedin_mcp.tools.actions import enqueue_action, validated_payload

DB_PATH = DEFAULT_DB_PATH
POLL_INTERVAL = 10   # seconds between status checks
JOB_TIMEOUT   = 300  # seconds before we give up on a job

DIVIDER = "-" * 72


def setup_db() -> tuple[AuditLog, int]:
    """Open / migrate the database, return (log, account_id)."""
    log = AuditLog.open(DB_PATH)
    set_audit_log(log)
    import os
    label = os.getenv("LINKEDIN_USERNAME", "linkedin")
    account_id = log.ensure_account(label)
    set_account_resolver(lambda: account_id)
    return log, account_id


def queue(action: str, approved: bool = False, **fields) -> dict:
    """Validate + enqueue one action, return the enqueue result."""
    payload = validated_payload(action, {k: v for k, v in fields.items() if v is not None})
    return enqueue_action(action, payload, approved=approved)


def poll_job(conn, account_id: int, job_id: int, timeout: int = JOB_TIMEOUT) -> dict | None:
    """Block until job reaches a terminal state; return the job payload result."""
    terminal = {JobState.DONE.value, "done", JobState.FAILED.value, "failed",
                JobState.CANCELLED.value, "cancelled"}
    deadline = time.time() + timeout
    last_state = None
    while time.time() < deadline:
        for job in adhoc_jobs(conn, account_id, limit=200):
            if job.id == job_id:
                if job.state != last_state:
                    print(f"    job {job_id} → {job.state}")
                    last_state = job.state
                if job.state in terminal:
                    return job.payload.get(RESULT_KEY)
        time.sleep(POLL_INTERVAL)
    print(f"  ⚠  job {job_id} timed out after {timeout}s")
    return None


def pretty(label: str, data) -> None:
    print(f"\n{'='*72}")
    print(f"  {label}")
    print("="*72)
    if data is None:
        print("  (no result / timed out)")
    elif isinstance(data, (dict, list)):
        print(json.dumps(data, indent=2, default=str)[:4000])
    else:
        print(str(data)[:4000])


def first_profile_urls(result, n: int = 4) -> list[str]:
    """Extract up to n profile URLs from a profile_search result."""
    urls: list[str] = []
    if not isinstance(result, dict):
        return urls
    profiles = result.get("profiles") or result.get("results") or []
    for p in profiles:
        url = p.get("profile_url") or p.get("url") or ""
        if "linkedin.com/in/" in url and url not in urls:
            urls.append(url)
        if len(urls) >= n:
            break
    return urls


def first_post_url(result) -> str | None:
    """Extract the first post URL from a post_search result."""
    if not isinstance(result, dict):
        return None
    posts = result.get("posts") or result.get("results") or []
    for p in posts:
        url = p.get("post_url") or p.get("url") or ""
        if "linkedin.com/" in url:
            return url
    return None


def find_profile_url_by_name(result, name_fragment: str) -> str | None:
    """Find a profile URL whose author name contains name_fragment."""
    if not isinstance(result, dict):
        return None
    for p in (result.get("profiles") or result.get("results") or []):
        full_name = p.get("name") or p.get("full_name") or ""
        if name_fragment.lower() in full_name.lower():
            url = p.get("profile_url") or p.get("url") or ""
            if "linkedin.com/in/" in url:
                return url
    return None


# --------------------------------------------------------------------------- #
# Main workflow                                                                #
# --------------------------------------------------------------------------- #

def main() -> None:
    print(DIVIDER)
    print("LinkedIn MCP end-to-end workflow test")
    print(DIVIDER)

    # ---------------------------------------------------------------------- #
    # Step 0: verify session                                                  #
    # ---------------------------------------------------------------------- #
    sessions_dir = Path(__file__).parent / "sessions"
    cookie_files = list(sessions_dir.glob("*_cookies.json")) + list(sessions_dir.glob("linkedin_cookies.json"))
    if not any(sessions_dir.glob("*.json")):
        print("\n✗  No saved session found.  Run `python login_once.py` first.")
        sys.exit(1)
    print(f"\n✓  Session cookie found in {sessions_dir}")

    # ---------------------------------------------------------------------- #
    # Step 0b: initialise DB and account                                      #
    # ---------------------------------------------------------------------- #
    log, account_id = setup_db()
    conn = log.connection
    print(f"✓  Database: {DB_PATH}")
    print(f"✓  Account id: {account_id}")

    # ---------------------------------------------------------------------- #
    # Step 0c: ensure worker can find the login_once cookies                  #
    # The worker uses account_seed=str(account_id) → different cookie path.  #
    # Copy the generic cookie file to the seed-specific path if needed.       #
    # ---------------------------------------------------------------------- #
    sessions_dir = Path(__file__).parent / "sessions"
    generic_cookie = sessions_dir / "linkedin_cookies.json"
    seed = str(account_id)
    suffix = hashlib.sha256(seed.encode()).hexdigest()[:12]
    seeded_cookie = sessions_dir / f"linkedin_{suffix}_cookies.json"
    if generic_cookie.exists() and not seeded_cookie.exists():
        shutil.copy(generic_cookie, seeded_cookie)
        print(f"✓  Copied session cookie → {seeded_cookie.name}")

    # ---------------------------------------------------------------------- #
    # Phase A — queue all read / search jobs up front                        #
    # ---------------------------------------------------------------------- #
    print(f"\n{DIVIDER}")
    print("Phase A — queuing search and profile-view jobs")
    print(DIVIDER)

    uae_p1 = queue("profile_search", query="Solution Engineer Microsoft UAE", count=10, page=1)
    print(f"  Queued search page 1    → job {uae_p1['job_id']}")

    uae_p2 = queue("profile_search", query="Solution Engineer Microsoft UAE", count=10, page=2)
    print(f"  Queued search page 2    → job {uae_p2['job_id']}")

    vc_search = queue("profile_search", query="Velayudhan C P Microsoft", count=5, page=1)
    print(f"  Queued Velayudhan search→ job {vc_search['job_id']}")

    psr_search = queue("profile_search", query="Parvathy S Raj", count=5, page=1)
    print(f"  Queued Parvathy search  → job {psr_search['job_id']}")

    pt_search = queue("profile_search", query="Perinbaraj Thangavel", count=5, page=1)
    print(f"  Queued Perinbaraj search→ job {pt_search['job_id']}")

    pt_posts = queue("post_search", query="Perinbaraj Thangavel", count=5, sort_by="date_posted")
    print(f"  Queued post search      → job {pt_posts['job_id']}")

    # ---------------------------------------------------------------------- #
    # Start the worker                                                         #
    # ---------------------------------------------------------------------- #
    print(f"\n{DIVIDER}")
    print("Starting worker (headless Chromium)…")
    print(DIVIDER)

    worker_cmd = [
        sys.executable, str(Path(__file__).parent / "worker.py"),
        "--account", str(account_id),
        "--headless",
        "--tick-seconds", "8",
        "--ad-hoc-actions-per-tick", "3",
    ]
    worker = subprocess.Popen(
        worker_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    print(f"  Worker PID {worker.pid} — polling for job results…")
    time.sleep(5)  # give worker a moment to start

    # ---------------------------------------------------------------------- #
    # Phase B — wait for search results                                        #
    # ---------------------------------------------------------------------- #
    print(f"\n{DIVIDER}")
    print("Phase B — waiting for searches to complete")
    print(DIVIDER)

    r_uae_p1  = poll_job(conn, account_id, uae_p1["job_id"])
    r_uae_p2  = poll_job(conn, account_id, uae_p2["job_id"])
    r_vc      = poll_job(conn, account_id, vc_search["job_id"])
    r_psr     = poll_job(conn, account_id, psr_search["job_id"])
    r_pt      = poll_job(conn, account_id, pt_search["job_id"])
    r_pt_posts = poll_job(conn, account_id, pt_posts["job_id"])

    pretty("Solution Engineer Microsoft UAE — page 1", r_uae_p1)
    pretty("Solution Engineer Microsoft UAE — page 2", r_uae_p2)

    # ---------------------------------------------------------------------- #
    # Phase C — view 3-4 profiles from UAE search                             #
    # ---------------------------------------------------------------------- #
    print(f"\n{DIVIDER}")
    print("Phase C — scraping profile details for 3-4 UAE profiles")
    print(DIVIDER)

    uae_urls = first_profile_urls(r_uae_p1, n=4)
    profile_jobs: list[tuple[str, int]] = []
    for url in uae_urls:
        j = queue("profile_view", profile_url=url)
        print(f"  Queued profile_view for {url} → job {j['job_id']}")
        profile_jobs.append((url, j["job_id"]))

    if not profile_jobs:
        print("  ⚠  No UAE profiles returned by search; skipping profile scrape.")

    # ---------------------------------------------------------------------- #
    # Phase D — find test-account profile URLs and queue write actions         #
    # ---------------------------------------------------------------------- #
    print(f"\n{DIVIDER}")
    print("Phase D — queuing connection request, follow, post like+comment")
    print(DIVIDER)

    # Velayudhan C P — connection request
    vc_url = find_profile_url_by_name(r_vc, "Velayudhan")
    if vc_url:
        note = (
            "Hi, I came across your profile and wanted to connect. "
            "I work as a Solution Engineer at Microsoft, focused on GitHub Copilot enterprise adoption. "
            "Would love to stay in touch."
        )[:300]
        conn_req = queue("connection_request", approved=True, profile_url=vc_url, note=note)
        print(f"  Queued connection_request → Velayudhan C P  (job {conn_req['job_id']})")
    else:
        conn_req = None
        print("  ⚠  Velayudhan C P not found in search; skipping connection request.")

    # Parvathy S Raj — follow
    psr_url = find_profile_url_by_name(r_psr, "Parvathy")
    if psr_url:
        follow_j = queue("profile_follow", approved=True, profile_url=psr_url)
        print(f"  Queued profile_follow → Parvathy S Raj (job {follow_j['job_id']})")
    else:
        follow_j = None
        print("  ⚠  Parvathy S Raj not found in search; skipping follow.")

    # Post interaction — like + comment
    post_url = first_post_url(r_pt_posts)
    if not post_url:
        # Fall back: try to find a post via Perinbaraj's profile name search result
        post_url = None
        print("  ⚠  No posts found for Perinbaraj Thangavel; skipping post actions.")

    like_j    = None
    comment_j = None
    if post_url:
        like_j = queue("post_like", approved=True, post_url=post_url)
        print(f"  Queued post_like    → {post_url[:80]}  (job {like_j['job_id']})")

        comment_text = (
            "Really interesting perspective here. "
            "From the enterprise adoption side, I keep seeing similar patterns when teams "
            "start integrating AI tooling at scale. Thanks for sharing."
        )[:300]
        comment_j = queue("post_comment", approved=True, post_url=post_url, comment=comment_text)
        print(f"  Queued post_comment → {post_url[:80]}  (job {comment_j['job_id']})")

    # ---------------------------------------------------------------------- #
    # Phase E — wait for all remaining jobs                                    #
    # ---------------------------------------------------------------------- #
    print(f"\n{DIVIDER}")
    print("Phase E — waiting for write jobs and profile scrapes to complete")
    print(DIVIDER)

    # Profile scrapes
    for url, jid in profile_jobs:
        r = poll_job(conn, account_id, jid)
        pretty(f"Profile detail: {url}", r)

    # Connection request
    if conn_req:
        r = poll_job(conn, account_id, conn_req["job_id"])
        pretty("Connection request → Velayudhan C P", r)

    # Follow
    if follow_j:
        r = poll_job(conn, account_id, follow_j["job_id"])
        pretty("Follow → Parvathy S Raj", r)

    # Like
    if like_j:
        r = poll_job(conn, account_id, like_j["job_id"])
        pretty("Post like", r)

    # Comment
    if comment_j:
        r = poll_job(conn, account_id, comment_j["job_id"])
        pretty("Post comment", r)

    # ---------------------------------------------------------------------- #
    # Done — shut down worker                                                  #
    # ---------------------------------------------------------------------- #
    print(f"\n{DIVIDER}")
    print("All jobs processed. Terminating worker…")
    try:
        worker.terminate()
        worker.wait(timeout=10)
    except Exception as e:
        print(f"  (worker shutdown: {e})")

    print("\n✓  End-to-end workflow complete.")
    print(DIVIDER)


if __name__ == "__main__":
    main()
