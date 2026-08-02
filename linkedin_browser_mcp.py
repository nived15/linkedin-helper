import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from fastmcp import Context, FastMCP

from linkedin_mcp.audit import audit_linkedin_action, record_tool_result
from linkedin_mcp.browser.humanize import pace
from linkedin_mcp.browser.session import BrowserSession, load_cookies, save_cookies
from linkedin_mcp.core.config import ADHOC_ENQUEUE_ACTION, profile_view_action
from linkedin_mcp.executors.contract import DETAIL_SHAPE, SUMMARY_SHAPE
from linkedin_mcp.executors.support import fill_selector_fallback
from linkedin_mcp.safety import guard_action
from linkedin_mcp.tools import (
    enqueue_action,
    register_action_tools,
    register_lead_tools,
    validated_payload,
)

# Set up logging to stderr only
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stderr)]
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

# Load environment variables
env_path = Path(__file__).parent / '.env'
if env_path.exists():
    load_dotenv(env_path)
    logger.debug(f"Loaded environment from {env_path}")
else:
    logger.warning(f"No .env file found at {env_path}")

# Create MCP server
mcp = FastMCP("linkedin")

# MCP-02 (#25) registers the eleven lead extraction and CRM tools from
# linkedin_mcp.tools. They live in their own package because they open no
# browser: the harvest tools enqueue a job for SEQ-04's runner and the CRM
# tools read the local database. Registering them here rather than importing
# this module from there keeps the dependency one way.
register_lead_tools(mcp)

# MCP-03 (#26) registers action_enqueue_adhoc, action_status and action_cancel,
# the generic form of the eight action tools below. Everything this server can
# make LinkedIn do is a `jobs` row, and the worker in worker.py is the only
# thing that acts on one.
register_action_tools(mcp)

def report_progress(ctx, current, total, message=None):
    """Helper function to report progress with proper validation"""
    try:
        progress = min(1.0, current / total) if total > 0 else 0
        if message:
            ctx.info(message)
        logger.debug(f"Progress: {progress:.2%} - {message if message else ''}")
    except Exception as e:
        logger.error(f"Error reporting progress: {str(e)}")

def handle_notification(ctx, notification_type, params=None):
    """Helper function to handle notifications with proper validation"""
    try:
        if notification_type == "initialized":
            logger.info("MCP Server initialized")
            if ctx:  # Only call ctx.info if ctx is provided
                ctx.info("Server initialized and ready")
        elif notification_type == "cancelled":
            reason = params.get("reason", "Unknown reason")
            logger.warning(f"Operation cancelled: {reason}")
            if ctx:
                ctx.warning(f"Operation cancelled: {reason}")
        else:
            logger.debug(f"Notification: {notification_type} - {params}")
    except Exception as e:
        logger.error(f"Error handling notification: {str(e)}")

# MCP-03 (#26) moved the page-driving helpers this file used to own into
# `linkedin_mcp.executors.support`, and the Playwright bodies of the eight
# action tools into `linkedin_mcp.executors.linkedin`.
#
# They had to move rather than be copied. `tests/test_worker_support.py` forbids
# the worker from importing this module, so the executors could not have reached
# a helper that stayed here, and a second copy of `check_page_is_ours` would
# have meant a second detection check. A detection check that raises without
# flipping the account state passes its own tests while leaving a challenged
# account looking healthy to the safety gate, which is a bug this repository has
# shipped once already.
#
# Three tools still drive a browser here: `login_linkedin`,
# `login_linkedin_secure` and `close_browser`. They are session lifecycle, not
# LinkedIn actions. Queueing them would be circular, because the executors the
# queue runs need the session those tools create, and the design already treats
# them as a separate class: they are `login`, `login_secure` and `browser_close`
# in `UNMETERED_ACTIONS`. That exemption is named explicitly in
# `tests/test_actions.py::SESSION_LIFECYCLE_TOOLS` rather than left implicit, so
# it is visible to anyone auditing the guarantee.


def queue_linkedin_action(
    action: str,
    *,
    approved: bool = False,
    lead_id: int | None = None,
    **fields,
) -> dict:
    """Validate one action's arguments and write its `jobs` row.

    There is no `guard_action` call in here. Each tool asks the gate itself, so
    a reader can see which budget a tool spends without following an
    indirection and so `tests/test_limits.py` can still read the action names
    out of this source.

    That pre-flight answer is a preview, not the decision. The authoritative
    gate is the worker's, at the moment the action actually runs, because a job
    queued at nine in the morning and executed at three in the afternoon has to
    be judged against the budget at three. Asking here as well costs one read
    and preserves the immediate refusal an agent used to get.
    """
    try:
        payload = validated_payload(action, fields)
    except Exception as error:
        logger.error(f"Refusing to queue {action}: {error}")
        return {"status": "error", "message": str(error)}
    return enqueue_action(action, payload, lead_id=lead_id, approved=approved)


def record_comment_outcome(results: list, post_url: str, status: str, message: str, **extra) -> dict:
    """Append one batch comment result and write its own audit row."""
    entry = {"post_url": post_url, "status": status, "message": message, **extra}
    results.append(entry)
    record_tool_result("post_comment", entry, target=post_url)
    return entry


@mcp.tool()
@audit_linkedin_action("browser_close")
async def close_browser(ctx: Context) -> dict:
    """Close the persistent browser session when the workflow is finished."""
    await BrowserSession.shutdown()
    return {"status": "success", "message": "Browser closed"}


@mcp.tool()
@audit_linkedin_action("login", target="username")
async def login_linkedin(username: str | None = None, password: str | None = None, ctx: Context | None = None) -> dict:
    """Open LinkedIn login page in browser for manual login.
    Username and password are optional - if not provided, user will need to enter them manually."""
    
    logger.info("Starting LinkedIn login with browser for manual login")
    
    # Create browser session with explicit window size and position
    async with BrowserSession(platform='linkedin', headless=False) as session:
        try:
            # Configure browser window
            page = await session.new_page()
            await page.set_viewport_size({'width': 1280, 'height': 800})
            
            # Navigate to LinkedIn login
            await page.goto('https://www.linkedin.com/login', wait_until='domcontentloaded')
            
            # Check if already logged in
            if 'feed' in page.url:
                await session.save_session(page)
                return {"status": "success", "message": "Already logged in"}
            
            if ctx:
                ctx.info("Please log in manually through the browser window...")
                ctx.info("The browser will wait for up to 5 minutes for you to complete the login.")
            logger.info("Waiting for manual login...")
            
            # Pre-fill credentials if provided
            try:
                if username:
                    await fill_selector_fallback(page, 'login_username', username)
                if password:
                    await fill_selector_fallback(page, 'login_password', password)
            except Exception as e:
                logger.warning(f"Failed to pre-fill credentials: {str(e)}")
                # Continue anyway - user can enter manually
            
            # Wait for successful login (feed page)
            try:
                await page.wait_for_url('**/feed/**', timeout=300000)  # 5 minutes timeout
                if ctx:
                    ctx.info("Login successful!")
                logger.info("Manual login successful")
                await session.save_session(page)
                # Keep browser open for a moment to show success
                await pace(3.0)
                return {"status": "success", "message": "Manual login successful"}
            except Exception as e:
                logger.error(f"Login timeout: {str(e)}")
                return {
                    "status": "error",
                    "message": "Login timeout. Please try again and complete login within 5 minutes."
                }
                
        except Exception as e:
            logger.error(f"Login process error: {str(e)}")
            return {"status": "error", "message": f"Login process error: {str(e)}"}

@mcp.tool()
@audit_linkedin_action("login_secure")
async def login_linkedin_secure(ctx: Context | None = None) -> dict:
    """Open LinkedIn login page in browser for manual login using environment credentials as default values.
    
    Optional environment variables:
    - LINKEDIN_USERNAME: Your LinkedIn email/username (will be pre-filled if provided)
    - LINKEDIN_PASSWORD: Your LinkedIn password (will be pre-filled if provided)
    
    Returns:
        dict: Login status and message
    """
    logger.info("Starting secure LinkedIn login")
    username = os.getenv('LINKEDIN_USERNAME', '').strip()
    password = os.getenv('LINKEDIN_PASSWORD', '').strip()
    
    if not username and not password:
        return {"status": "error", "message": "Missing LinkedIn credentials in environment"}
    
    if '@' not in username:
        return {"status": "error", "message": "Invalid email format for LINKEDIN_USERNAME"}
    
    if len(password) < 8:
        return {"status": "error", "message": "Invalid credentials: password must be at least 8 characters"}
    
    return await login_linkedin(username, password, ctx)

@mcp.tool()
@audit_linkedin_action(ADHOC_ENQUEUE_ACTION, target="username", capture=("direct",))
async def get_linkedin_profile(username: str, ctx: Context, direct: bool = False) -> dict:
    """Queue a read of a profile's top card, including follower count.

    Returns a job id immediately. Nothing has reached LinkedIn when this
    returns: the worker leases the job, asks the safety gate, waits out the
    jitter and then loads the page. Poll `action_status(job_id=...)` for the
    profile itself.

    Set direct=True to load the profile URL straight away instead of navigating
    through the LinkedIn search bar. LinkedIn caps direct profile loads at
    roughly 40 a day against 100 for a view reached through the site, so the two
    spend separate budgets and this is an escape hatch rather than the normal
    path.
    """
    action = profile_view_action(direct)
    refusal = guard_action(action)
    if refusal:
        return refusal

    return queue_linkedin_action(
        action,
        profile_url=f"https://www.linkedin.com/in/{username}",
        shape=SUMMARY_SHAPE,
    )


@mcp.tool()
@audit_linkedin_action(ADHOC_ENQUEUE_ACTION, capture=("count",))
async def browse_linkedin_feed(ctx: Context, count: int = 5) -> dict:
    """Queue a read of the LinkedIn feed.

    Args:
        ctx: MCP context for logging
        count: Number of posts to retrieve (default: 5)

    Returns:
        dict: The queued job. Poll action_status(job_id=...) for the posts.
    """
    refusal = guard_action("feed_browse")
    if refusal:
        return refusal

    return queue_linkedin_action("feed_browse", count=count)


@mcp.tool()
@audit_linkedin_action(ADHOC_ENQUEUE_ACTION, target="query", capture=("count",))
async def search_linkedin_profiles(query: str, ctx: Context, count: int = 5) -> dict:
    """Queue a People search. Poll action_status(job_id=...) for the profiles."""
    refusal = guard_action("profile_search")
    if refusal:
        return refusal

    return queue_linkedin_action("profile_search", query=query, count=count)


@mcp.tool()
@audit_linkedin_action(ADHOC_ENQUEUE_ACTION, target="profile_url", capture=("direct",))
async def view_linkedin_profile(profile_url: str, ctx: Context, direct: bool = False) -> dict:
    """Queue a full read of one profile, with experience and education.

    Navigation goes through the LinkedIn search bar by default. Set direct=True
    to load the URL straight away, which LinkedIn caps at roughly 40 per 24h.

    Returns a job id. Poll `action_status(job_id=...)` for the profile.
    """
    if not ('linkedin.com/in/' in profile_url):
        return {
            "status": "error",
            "message": "Invalid LinkedIn profile URL. Should contain 'linkedin.com/in/'"
        }

    action = profile_view_action(direct)
    refusal = guard_action(action)
    if refusal:
        return refusal

    return queue_linkedin_action(action, profile_url=profile_url, shape=DETAIL_SHAPE)


@mcp.tool()
@audit_linkedin_action(
    ADHOC_ENQUEUE_ACTION,
    target="post_url",
    capture=("action", "comment"),
)
async def interact_with_linkedin_post(post_url: str, ctx: Context, action: str = "like", comment: str = None) -> dict:
    """Queue an interaction with a LinkedIn post (read, like, comment, share).

    Returns a job id. Nothing is liked, posted or shared when this returns. The
    worker does that once the safety gate agrees, so a comment queued now is
    judged against the cap that applies when it actually goes out.
    """
    if not ('linkedin.com/posts/' in post_url or 'linkedin.com/feed/update/' in post_url):
        return {
            "status": "error",
            "message": "Invalid LinkedIn post URL"
        }

    valid_actions = ["like", "comment", "read", "share"]
    if action not in valid_actions:
        return {
            "status": "error",
            "message": f"Invalid action. Choose from: {', '.join(valid_actions)}"
        }

    refusal = guard_action(f"post_{action}", approved=True)
    if refusal:
        return refusal

    fields = {"post_url": post_url}
    if action == "comment":
        # Refused here rather than quietly downgraded to a read, which is what
        # the inline version did when the comment was missing.
        fields["comment"] = comment
    return queue_linkedin_action(f"post_{action}", approved=True, **fields)


@mcp.tool()
@audit_linkedin_action(ADHOC_ENQUEUE_ACTION, target="profile_url", capture=("note",))
async def send_connection_request(
    profile_url: str,
    ctx: Context,
    note: str | None = None,
    direct: bool = False,
    lead_id: int | None = None,
) -> dict:
    """Queue a connection request with an optional personalised note.

    Returns a job id. No invitation has been sent when this returns. The worker
    sends it once the safety gate agrees, which is what keeps the 30-a-day and
    100-a-week caps enforced rather than merely remembered.

    Args:
        profile_url: The LinkedIn profile URL (must contain 'linkedin.com/in/')
        ctx: MCP context for logging
        note: Optional personalised connection note (max 300 characters)
        direct: Load the profile URL directly instead of navigating via the
            LinkedIn search bar. LinkedIn caps direct profile loads, so leave
            this off unless in-page navigation is unavailable.
        lead_id: The stored lead this targets, when there is one. Supplying it
            is what lets the blacklist and the 90 day dedupe window see the
            invitation, so a second invite to somebody already invited refuses
            instead of going out.

    Returns:
        dict: The queued job, its id and the action type it will spend.
    """
    if not ('linkedin.com/in/' in profile_url):
        return {
            "status": "error",
            "message": "Invalid LinkedIn profile URL. Should contain 'linkedin.com/in/'"
        }

    if note and len(note) > 300:
        return {
            "status": "error",
            "message": f"Connection note too long ({len(note)} chars). Max 300 characters."
        }

    refusal = guard_action("connection_request", approved=True, lead_id=lead_id)
    if refusal:
        return refusal

    return queue_linkedin_action(
        "connection_request",
        approved=True,
        lead_id=lead_id,
        profile_url=profile_url,
        note=note,
        direct=direct,
    )


@mcp.tool()
@audit_linkedin_action(ADHOC_ENQUEUE_ACTION, target="query", capture=("count", "sort_by"))
async def search_linkedin_posts(query: str, ctx: Context, count: int = 10, sort_by: str = "relevance") -> dict:
    """Queue a post search. Poll action_status(job_id=...) for the posts.

    Args:
        query: Search keyword, e.g. "GitHub Copilot"
        ctx: MCP context
        count: Number of posts to retrieve (default: 10)
        sort_by: Sort order, "relevance" (default, surfaces high-engagement
            posts) or "date_posted"

    Returns:
        dict: The queued job. Each post in the eventual result carries
              post_number, post_url, author, content, timestamp, likes, comments
    """
    refusal = guard_action("post_search")
    if refusal:
        return refusal

    return queue_linkedin_action(
        "post_search", query=query, count=count, sort_by=sort_by
    )


@mcp.tool()
@audit_linkedin_action("post_comment_batch")
async def comment_on_approved_posts(approved_posts: list, ctx: Context) -> dict:
    """Queue comments on a list of user-approved LinkedIn posts.

    Call this ONLY after the user has reviewed and approved the posts and their
    comments.

    One job per post rather than one job for the batch. A batch that half
    succeeded used to leave no way to retry the rest, and a job per post is also
    what makes the comment cap bind in the middle of a long list rather than
    only at its start.

    Args:
        approved_posts: List of dicts, each with 'post_url' and 'comment' keys.
        ctx: MCP context

    Returns:
        dict: status, summary (total/queued/refused/failed), and per-post results
    """
    if not approved_posts:
        return {"status": "error", "message": "No posts provided"}

    refusal = guard_action("post_comment", approved=True)
    if refusal:
        return {**refusal, "results": []}

    results = []
    for item in approved_posts:
        post_url = item.get('post_url', '').strip()
        comment_text = item.get('comment', '').strip()

        if not post_url or not comment_text:
            record_comment_outcome(
                results,
                post_url,
                "skipped",
                "Missing post_url or comment",
            )
            continue

        refusal = guard_action("post_comment", approved=True)
        if refusal:
            results.append({"post_url": post_url, **refusal})
            continue

        queued = queue_linkedin_action(
            "post_comment",
            approved=True,
            post_url=post_url,
            comment=comment_text,
        )
        if queued.get("status") == "queued":
            record_comment_outcome(
                results,
                post_url,
                "queued",
                queued["message"],
                comment=comment_text,
                job_id=queued["job_id"],
            )
        else:
            record_comment_outcome(
                results, post_url, "error", queued.get("message", "unknown error")
            )

    total = len(approved_posts)
    queued_count = sum(1 for r in results if r['status'] == 'queued')
    refused_count = sum(1 for r in results if r['status'] == 'refused')

    return {
        "status": "success",
        "summary": {
            "total": total,
            "queued": queued_count,
            "refused": refused_count,
            "failed": total - queued_count - refused_count,
        },
        "results": results,
        "message": (
            f"Queued {queued_count} of {total} comment(s). Nothing has been "
            "posted yet; the worker posts them once the gate agrees."
        ),
    }


if __name__ == "__main__":
    try:
        logger.debug("Starting LinkedIn MCP Server with debug logging")
        
        # Initialize MCP server with simple configuration
        try:
            handle_notification(None, "initialized")  # Pass None for ctx during initialization
            mcp.run(transport='stdio')
        except KeyboardInterrupt:
            handle_notification(None, "cancelled", {"reason": "Server stopped by user"})
            logger.info("Server stopped by user")
        except Exception as e:
            handle_notification(None, "cancelled", {"reason": str(e)})
            logger.error(f"Server error: {str(e)}", exc_info=True)
            sys.exit(1)
            
    except Exception as e:
        logger.error(f"Startup error: {str(e)}", exc_info=True)
        sys.exit(1)
        
