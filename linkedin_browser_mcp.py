import json
import logging
import os
import sys
import time
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv
from fastmcp import Context, FastMCP

from linkedin_mcp.browser.humanize import (
    cooldown,
    dwell_and_click,
    pace,
    scroll_page,
    settle,
    type_text,
)
from linkedin_mcp.browser.navigate import goto_profile
from linkedin_mcp.browser.selectors import (
    selector_fallbacks,
    selector_payload,
)
from linkedin_mcp.browser.session import BrowserSession, load_cookies, save_cookies

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

async def wait_for_selector_fallback(page, name: str, timeout: int = 10000):
    """Wait for the first matching selector in the configured fallback order."""
    fallbacks = selector_fallbacks(name)
    deadline = time.monotonic() + (timeout / 1000)
    last_error = None
    for fallback in fallbacks:
        remaining_ms = int((deadline - time.monotonic()) * 1000)
        if remaining_ms <= 0:
            break
        try:
            return await page.wait_for_selector(fallback, timeout=max(1, remaining_ms))
        except Exception as exc:
            last_error = exc
    if last_error:
        raise last_error
    raise ValueError(f"No selector fallbacks configured for {name}")


async def query_selector_fallback(page, name: str):
    """Query for the first matching selector in the configured fallback order."""
    for fallback in selector_fallbacks(name):
        handle = await page.query_selector(fallback)
        if handle:
            return handle
    return None


async def click_selector_fallback(page, name: str, timeout: int = 10000):
    """Click the first matching selector in the configured fallback order."""
    handle = await wait_for_selector_fallback(page, name, timeout=timeout)
    await dwell_and_click(handle)
    return handle


async def fill_selector_fallback(page, name: str, value: str, timeout: int = 10000):
    """Fill the first matching selector in the configured fallback order."""
    handle = await wait_for_selector_fallback(page, name, timeout=timeout)
    await type_text(handle, value, clear=True)
    return handle


@mcp.tool()
async def close_browser(ctx: Context) -> dict:
    """Close the persistent browser session when the workflow is finished."""
    await BrowserSession.shutdown()
    return {"status": "success", "message": "Browser closed"}


@mcp.tool()
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
async def get_linkedin_profile(username: str, ctx: Context, direct: bool = False) -> dict:
    """Get LinkedIn profile information including follower count and profile views.

    Set direct=True to load the profile URL straight away instead of navigating
    through the LinkedIn search bar. LinkedIn caps direct profile loads, so this
    is an escape hatch rather than the normal path.
    """
    async with BrowserSession(platform='linkedin', headless=False) as session:
        try:
            page = await session.new_page()
            await goto_profile(page, f'https://www.linkedin.com/in/{username}', direct=direct)
            
            # Check if we're logged in
            if 'login' in page.url:
                return {
                    "status": "error",
                    "message": "Not logged in. Please run login_linkedin tool first"
                }
            
            # Check if profile page loaded
            if '/in/' not in page.url:
                return {"status": "error", "message": "Profile page not found"}
            
            ctx.info(f"Loading profile for {username}...")
            
            # Wait for profile to load
            await wait_for_selector_fallback(page, 'profile_top_card', timeout=10000)
            
            # Extract profile information
            profile_data = await page.evaluate('''(selectors) => {
                const getData = (selectorList) => {
                    for (const selector of selectorList) {
                        const el = document.querySelector(selector);
                        if (el && el.innerText.trim()) return el.innerText.trim();
                    }
                    return null;
                };
                
                // Try to get follower count from multiple possible locations
                let followerCount = null;
                const followerElements = document.querySelectorAll(selectors.profile_follower_items.join(', '));
                for (const el of followerElements) {
                    const text = el.innerText.trim().toLowerCase();
                    if (text.includes('follower')) {
                        const match = text.match(/([0-9,]+)/);
                        if (match) followerCount = parseInt(match[1].replace(/,/g, ''));
                    }
                }
                // Also check the connections/followers section
                if (!followerCount) {
                    const allSpans = document.querySelectorAll(selectors.profile_text_spans.join(', '));
                    for (const span of allSpans) {
                        const text = span.innerText.trim().toLowerCase();
                        if (text.includes('follower')) {
                            const match = text.match(/([0-9,]+)/);
                            if (match) followerCount = parseInt(match[1].replace(/,/g, ''));
                            break;
                        }
                    }
                }
                
                return {
                    name: getData(selectors.profile_name),
                    headline: getData(selectors.profile_headline),
                    location: getData(selectors.profile_location),
                    follower_count: followerCount,
                    connection_count: (() => {
                        const elements = document.querySelectorAll(selectors.profile_connection_count.join(', '));
                        for (const el of elements) {
                            const text = el.innerText.trim();
                            const match = text.match(/([0-9,]+)/);
                            if (match && !text.toLowerCase().includes('follower')) return parseInt(match[1].replace(/,/g, ''));
                        }
                        return null;
                    })(),
                    about: getData(selectors.profile_about),
                    profile_url: window.location.href
                };
            }''', selector_payload(
                'profile_about',
                'profile_connection_count',
                'profile_follower_items',
                'profile_headline',
                'profile_location',
                'profile_name',
                'profile_text_spans',
            ))
            
            await session.save_session(page)
            
            return {
                "status": "success",
                "profile": profile_data,
                "url": f"https://www.linkedin.com/in/{username}"
            }
            
        except Exception as e:
            return {"status": "error", "message": f"Failed to get profile: {str(e)}"}

@mcp.tool()
async def browse_linkedin_feed(ctx: Context, count: int = 5) -> dict:
    """Browse LinkedIn feed and return recent posts
    
    Args:
        ctx: MCP context for logging and progress reporting
        count: Number of posts to retrieve (default: 5)
        
    Returns:
        dict: Contains status, posts array, and any errors
    """
    posts = []
    errors = []
    
    async with BrowserSession(platform='linkedin') as session:
        try:
            page = await session.new_page('https://www.linkedin.com/feed/')
            
            # Check if we're logged in
            if 'login' in page.url:
                return {
                    "status": "error", 
                    "message": "Not logged in. Please run login_linkedin tool first"
                }
                
            ctx.info(f"Browsing feed for {count} posts...")
            
            # Wait for the feed to load before starting scroll loop
            await settle()

            # Scroll to load content
            for i in range(min(count, 20)):  # Limit to reasonable number
                report_progress(ctx, i, count, f"Loading post {i+1}/{count}")
                
                try:
                    # Try multiple selectors — LinkedIn updates class names frequently
                    post_selector = None
                    for selector in selector_fallbacks('feed_post_container'):
                        try:
                            await page.wait_for_selector(selector, timeout=4000)
                            post_selector = selector
                            break
                        except Exception:
                            continue

                    if not post_selector:
                        errors.append(f"Error during scroll {i}: No feed post elements found on page")
                        await scroll_page(page, 800)
                        continue
                    
                    # Extract visible posts
                    new_posts = await page.evaluate('''(selectors) => {
                        let postElements = [];
                        for (const sel of selectors.feed_post_container) {
                            const els = document.querySelectorAll(sel);
                            if (els.length > 0) { postElements = Array.from(els); break; }
                        }

                        const getText = (el, ...sels) => {
                            for (const s of sels) {
                                const found = el.querySelector(s);
                                if (found && found.innerText.trim()) return found.innerText.trim();
                            }
                            return '';
                        };

                        return postElements.map(post => {
                            try {
                                // Post URL: prefer data-urn attribute
                                let postUrl = null;
                                const urn = post.getAttribute('data-urn') || post.getAttribute('data-id') || '';
                                if (urn.includes('activity')) {
                                    const activityId = urn.split(':').pop();
                                    postUrl = 'https://www.linkedin.com/feed/update/urn:li:activity:' + activityId;
                                }
                                if (!postUrl) {
                                    const shareLink = post.querySelector(selectors.feed_post_share_link.join(', '));
                                    if (shareLink) postUrl = shareLink.href.split('?')[0];
                                }

                                // Author profile URL
                                const authorLink = post.querySelector(
                                    selectors.feed_post_author_link.join(', ')
                                );
                                const authorProfileUrl = authorLink ? authorLink.href.split('?')[0] : null;

                                // Likes
                                const likesText = getText(post, ...selectors.feed_post_likes);
                                const likesCount = parseInt((likesText || '0').replace(/[^0-9]/g, '')) || 0;

                                // Comments
                                let commentsCount = 0;
                                for (const btn of post.querySelectorAll(
                                    selectors.feed_post_comments.join(', ')
                                )) {
                                    const t = btn.innerText?.trim() || btn.getAttribute('aria-label') || '';
                                    const m = t.match(/([0-9]+)/);
                                    if (m) { commentsCount = parseInt(m[1]); break; }
                                }

                                return {
                                    author: getText(post, ...selectors.feed_post_author_name) || 'Unknown',
                                    author_headline: getText(post, ...selectors.feed_post_author_headline),
                                    author_profile_url: authorProfileUrl,
                                    content: getText(post, ...selectors.feed_post_content),
                                    timestamp: getText(post, ...selectors.feed_post_timestamp),
                                    post_url: postUrl,
                                    likes_count: likesCount,
                                    comments_count: commentsCount
                                };
                            } catch (e) {
                                return null;
                            }
                        }).filter(p => p !== null && (p.content || p.author !== 'Unknown'));
                    }''', selector_payload(
                        'feed_post_author_headline',
                        'feed_post_author_link',
                        'feed_post_author_name',
                        'feed_post_comments',
                        'feed_post_container',
                        'feed_post_content',
                        'feed_post_likes',
                        'feed_post_share_link',
                        'feed_post_timestamp',
                    ))
                    
                    # Add new posts to our collection, avoiding duplicates
                    for post in new_posts:
                        if post not in posts:
                            posts.append(post)
                            
                    if len(posts) >= count:
                        break
                        
                    # Scroll down to load more content
                    await scroll_page(page, 800)
                    
                except Exception as scroll_error:
                    errors.append(f"Error during scroll {i}: {str(scroll_error)}")
                    continue
            
            # Save session cookies
            await session.save_session(page)
            
            return {
                "status": "success",
                "posts": posts[:count],
                "count": len(posts),
                "errors": errors if errors else None
            }
            
        except Exception as e:
            return {
                "status": "error",
                "message": f"Failed to browse feed: {str(e)}",
                "posts": posts,
                "errors": errors
            }
        

@mcp.tool()
async def search_linkedin_profiles(query: str, ctx: Context, count: int = 5) -> dict:
    """Search for LinkedIn profiles matching a query"""
    async with BrowserSession(platform='linkedin') as session:
        try:
            search_url = f'https://www.linkedin.com/search/results/people/?keywords={quote(query)}'
            page = await session.new_page(search_url)
            
            # Check if we're logged in
            if 'login' in page.url:
                return {
                    "status": "error", 
                    "message": "Not logged in. Please run login_linkedin tool first"
                }
            
            ctx.info(f"Searching for profiles matching: {query}")
            report_progress(ctx, 20, 100, "Loading search results...")
            
            # Wait for search results
            await wait_for_selector_fallback(page, 'search_result_container', timeout=10000)
            ctx.info("Search results loaded")
            report_progress(ctx, 50, 100, "Extracting profile data...")
            
            # Extract profile data
            profiles = await page.evaluate('''({ count, selectors }) => {
                const results = [];
                const profileCards = document.querySelectorAll(selectors.search_result_container.join(', '));
                
                for (let i = 0; i < Math.min(profileCards.length, count); i++) {
                    const card = profileCards[i];
                    try {
                        const profile = {
                            name: card.querySelector(selectors.search_result_title_link.join(', '))?.innerText?.trim() || 'Unknown',
                            headline: card.querySelector(selectors.search_result_headline.join(', '))?.innerText?.trim() || '',
                            location: card.querySelector(selectors.search_result_location.join(', '))?.innerText?.trim() || '',
                            profileUrl: card.querySelector(selectors.search_result_profile_link.join(', '))?.href || '',
                            connectionDegree: card.querySelector(selectors.search_result_distance.join(', '))?.innerText?.trim() || '',
                            snippet: card.querySelector(selectors.search_result_snippet.join(', '))?.innerText?.trim() || ''
                        };
                        results.push(profile);
                    } catch (e) {
                        console.error("Error extracting profile", e);
                    }
                }
                return results;
            }''', {
                "count": count,
                "selectors": selector_payload(
                    'search_result_container',
                    'search_result_distance',
                    'search_result_headline',
                    'search_result_location',
                    'search_result_profile_link',
                    'search_result_snippet',
                    'search_result_title_link',
                ),
            })
            
            report_progress(ctx, 90, 100, "Saving session...")
            await session.save_session(page)
            report_progress(ctx, 100, 100, "Search complete")
            
            return {
                "status": "success",
                "profiles": profiles,
                "count": len(profiles),
                "query": query
            }
            
        except Exception as e:
            ctx.error(f"Profile search failed: {str(e)}")
            return {
                "status": "error",
                "message": f"Failed to search profiles: {str(e)}"
            }
        
@mcp.tool() 
async def view_linkedin_profile(profile_url: str, ctx: Context, direct: bool = False) -> dict:
    """Visit and extract data from a specific LinkedIn profile.

    Navigation goes through the LinkedIn search bar by default. Set direct=True
    to load the URL straight away, which LinkedIn caps at roughly 40 per 24h.
    """
    if not ('linkedin.com/in/' in profile_url):
        return {
            "status": "error",
            "message": "Invalid LinkedIn profile URL. Should contain 'linkedin.com/in/'"
        }
        
    async with BrowserSession(platform='linkedin') as session:
        try:
            page = await session.new_page()
            await goto_profile(page, profile_url, direct=direct)
            
            # Check if we're logged in
            if 'login' in page.url:
                return {
                    "status": "error", 
                    "message": "Not logged in. Please run login_linkedin tool first"
                }
                
            ctx.info(f"Viewing profile: {profile_url}")
            
            # Wait for profile to load
            await wait_for_selector_fallback(page, 'profile_top_card', timeout=10000)
            report_progress(ctx, 50, 100, "Extracting profile data...")
            
            # Extract profile information
            profile_data = await page.evaluate('''(selectors) => {
                const getData = (selectorList, scope = document, property = 'innerText') => {
                    for (const selector of selectorList) {
                        const element = scope.querySelector(selector);
                        if (element && element[property]?.trim()) return element[property].trim();
                    }
                    return null;
                };
                
                return {
                    name: getData(selectors.profile_name),
                    headline: getData(selectors.profile_headline),
                    location: getData(selectors.profile_location),
                    connectionDegree: getData(selectors.profile_connection_count),
                    about: getData(selectors.profile_about),
                    experience: Array.from(document.querySelectorAll(selectors.profile_experience_item.join(', ')))
                        .map(exp => ({
                            title: getData(selectors.profile_experience_title, exp) || '',
                            company: getData(selectors.profile_experience_company, exp) || '',
                            duration: getData(selectors.profile_experience_duration, exp) || ''
                        })),
                    education: Array.from(document.querySelectorAll(selectors.profile_education_item.join(', ')))
                        .map(edu => ({
                            school: getData(selectors.profile_education_school, edu) || '',
                            degree: getData(selectors.profile_education_degree, edu) || '',
                            field: getData(selectors.profile_education_field, edu) || '',
                            dates: getData(selectors.profile_education_dates, edu) || ''
                        }))
                };
            }''', selector_payload(
                'profile_about',
                'profile_connection_count',
                'profile_education_dates',
                'profile_education_degree',
                'profile_education_field',
                'profile_education_item',
                'profile_education_school',
                'profile_experience_company',
                'profile_experience_duration',
                'profile_experience_item',
                'profile_experience_title',
                'profile_headline',
                'profile_location',
                'profile_name',
            ))
            
            report_progress(ctx, 100, 100, "Profile extraction complete")
            await session.save_session(page)
            
            return {
                "status": "success",
                "profile": profile_data,
                "url": profile_url
            }
            
        except Exception as e:
            ctx.error(f"Profile viewing failed: {str(e)}")
            return {
                "status": "error", 
                "message": f"Failed to extract profile data: {str(e)}"
            }
        

@mcp.tool()
async def interact_with_linkedin_post(post_url: str, ctx: Context, action: str = "like", comment: str = None) -> dict:
    """Interact with a LinkedIn post (like, comment)"""
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
        
    async with BrowserSession(platform='linkedin', headless=False) as session:
        try:
            page = await session.new_page(post_url)
            
            # Check if we're logged in
            if 'login' in page.url:
                return {
                    "status": "error", 
                    "message": "Not logged in. Please run login_linkedin tool first"
                }
                
            # Wait for post to load
            await wait_for_selector_fallback(page, 'post_detail_container', timeout=10000)
            ctx.info(f"Post loaded, performing action: {action}")
            
            # Read post content
            post_content = await page.evaluate('''(selectors) => {
                const post = document.querySelector(selectors.post_detail_container.join(', '));
                return {
                    author: post?.querySelector(selectors.post_detail_author.join(', '))?.innerText?.trim() || 'Unknown',
                    content: post?.querySelector(selectors.post_detail_content.join(', '))?.innerText?.trim() || '',
                    engagementCount: post?.querySelector(selectors.post_detail_engagement.join(', '))?.innerText?.trim() || '0'
                };
            }''', selector_payload(
                'post_detail_author',
                'post_detail_container',
                'post_detail_content',
                'post_detail_engagement',
            ))
            
            # Perform the requested action
            if action == "like":
                # Find and click like button if not already liked
                liked = await page.evaluate('''(selectors) => {
                    const likeButton = document.querySelector(selectors.post_like_button.join(', '));
                    if (!likeButton) return false;
                    const isLiked = likeButton.getAttribute('aria-pressed') === 'true';
                    if (!isLiked) {
                        likeButton.click();
                        return true;
                    }
                    return false;
                }''', selector_payload('post_like_button'))
                
                result = {
                    "status": "success",
                    "action": "like",
                    "performed": liked,
                    "message": "Successfully liked the post" if liked else "Post was already liked"
                }
                
            elif action == "comment" and comment:
                # Add comment to the post
                await click_selector_fallback(page, 'post_comment_trigger')
                await fill_selector_fallback(page, 'post_comment_editor', comment)
                await click_selector_fallback(page, 'post_comment_submit')
                
                # Wait for comment to appear
                await pace(2.0)
                
                result = {
                    "status": "success",
                    "action": "comment",
                    "message": "Comment posted successfully"
                }
                
            elif action == "share":
                # Repost/share the post
                try:
                    # Click the repost button
                    await click_selector_fallback(page, 'post_repost_button', timeout=5000)
                    await pace(1.0)
                    
                    # Click "Repost" option (instant repost without comment)
                    await click_selector_fallback(page, 'post_repost_option', timeout=5000)
                    await pace(2.0)
                    
                    result = {
                        "status": "success",
                        "action": "share",
                        "message": "Post shared successfully"
                    }
                except Exception as share_error:
                    result = {
                        "status": "error",
                        "action": "share",
                        "message": f"Failed to share post: {str(share_error)}"
                    }
                
            else:  # action == "read"
                result = {
                    "status": "success",
                    "action": "read",
                    "post": post_content
                }
                
            await session.save_session(page)
            return result
            
        except Exception as e:
            ctx.error(f"Post interaction failed: {str(e)}")
            return {
                "status": "error",
                "message": f"Failed to interact with post: {str(e)}"
            }
        
        

@mcp.tool()
async def send_connection_request(profile_url: str, ctx: Context, note: str | None = None, direct: bool = False) -> dict:
    """Send a connection request to a LinkedIn profile with an optional personalised note.
    
    Args:
        profile_url: The LinkedIn profile URL (must contain 'linkedin.com/in/')
        ctx: MCP context for logging and progress reporting
        note: Optional personalised connection note (max 300 characters)
        direct: Load the profile URL directly instead of navigating via the
            LinkedIn search bar. LinkedIn caps direct profile loads, so leave
            this off unless in-page navigation is unavailable.
        
    Returns:
        dict: Contains status, message, and request details
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
    
    async with BrowserSession(platform='linkedin', headless=False) as session:
        try:
            page = await session.new_page()
            await goto_profile(page, profile_url, direct=direct)
            
            # Check if we're logged in
            if 'login' in page.url:
                return {
                    "status": "error",
                    "message": "Not logged in. Please run login_linkedin tool first"
                }
            
            ctx.info(f"Sending connection request to {profile_url}")
            
            # Wait for profile to load
            await wait_for_selector_fallback(page, 'profile_top_card', timeout=10000)
            
            # Look for the Connect button — it may be in the main actions or the More dropdown
            connect_button = None
            try:
                connect_button = await wait_for_selector_fallback(page, 'connect_button', timeout=5000)
            except Exception:
                # Connect might be hidden under "More" dropdown
                try:
                    more_button = await wait_for_selector_fallback(page, 'more_actions_button', timeout=3000)
                    await dwell_and_click(more_button)
                    connect_button = await wait_for_selector_fallback(page, 'connect_button_more_menu', timeout=3000)
                except Exception:
                    pass
            
            if not connect_button:
                return {
                    "status": "error",
                    "message": "Connect button not found. Profile may already be connected or pending."
                }
            
            await dwell_and_click(connect_button)
            await pace(1.5)
            
            if note:
                # Click "Add a note" button in the connection dialog
                try:
                    add_note_button = await wait_for_selector_fallback(page, 'connect_add_note_button', timeout=3000)
                    await dwell_and_click(add_note_button)
                    
                    # Fill in the note
                    note_field = await wait_for_selector_fallback(page, 'connect_note_field', timeout=3000)
                    await type_text(note_field, note, clear=True)
                except Exception as note_error:
                    ctx.info(f"Could not add note: {str(note_error)}. Sending without note.")
            
            # Click Send
            try:
                send_button = await wait_for_selector_fallback(page, 'connect_send_button', timeout=5000)
                await dwell_and_click(send_button)
                await pace(2.0)
            except Exception as send_error:
                return {
                    "status": "error",
                    "message": f"Failed to click Send: {str(send_error)}"
                }
            
            # Extract the profile name for logging
            profile_name = await page.evaluate('''(selectors) => {
                const el = document.querySelector(selectors.profile_name.join(', '));
                return el ? el.innerText.trim() : 'Unknown';
            }''', selector_payload('profile_name'))
            
            await session.save_session(page)
            
            return {
                "status": "success",
                "message": f"Connection request sent to {profile_name}",
                "profile_url": profile_url,
                "profile_name": profile_name,
                "note_included": note is not None
            }
            
        except Exception as e:
            return {
                "status": "error",
                "message": f"Failed to send connection request: {str(e)}"
            }


@mcp.tool()
async def search_linkedin_posts(query: str, ctx: Context, count: int = 10, sort_by: str = "relevance") -> dict:
    """Search for LinkedIn posts matching a keyword query and return them with metadata.

    Args:
        query: Search keyword, e.g. "GitHub Copilot"
        ctx: MCP context
        count: Number of posts to retrieve (default: 10)
        sort_by: Sort order — "relevance" (default, surfaces high-engagement posts) or "date_posted"

    Returns:
        dict: status, query, count, and posts array.
              Each post has: post_number, post_url, author, content, timestamp, likes, comments
    """
    async with BrowserSession(platform='linkedin', headless=False) as session:
        try:
            if sort_by == "date_posted":
                search_url = f'https://www.linkedin.com/search/results/content/?keywords={quote(query)}&sortBy=date_posted'
            else:
                # Relevance sort (LinkedIn default) surfaces popular/high-engagement posts
                search_url = f'https://www.linkedin.com/search/results/content/?keywords={quote(query)}'
            page = await session.new_page(search_url)

            if 'login' in page.url or 'authwall' in page.url:
                return {
                    "status": "error",
                    "message": "Not logged in. Please run login_linkedin_secure tool first"
                }

            if ctx:
                ctx.info(f"Searching LinkedIn posts for: {query}")
            report_progress(ctx, 10, 100, "Loading search results...")

            # LinkedIn's React app takes ~10s to fully hydrate search results
            await pace(10.0)
            report_progress(ctx, 30, 100, "Extracting posts...")

            collected = []
            scroll_attempts = 0
            max_scrolls = 10

            EXTRACT_JS = """({ alreadySeen, selectors }) => {
                const results = [];
                const seenKeys = new Set(alreadySeen);

                const authorLinks = Array.from(document.querySelectorAll(selectors.search_posts_author_links.join(', ')));

                for (const aLink of authorLinks) {
                    let container = aLink.parentElement;
                    let depth = 0;
                    while (container && depth < 25) {
                        const text = (container.innerText || '').trim();
                        if (text.length > 300 && text.length < 12000) break;
                        depth++;
                        container = container.parentElement;
                    }
                    if (!container) continue;

                    const containerText = (container.innerText || '').trim();
                    if (containerText.length < 100) continue;

                    const dedupeKey = containerText.slice(0, 80);
                    if (seenKeys.has(dedupeKey)) continue;
                    seenKeys.add(dedupeKey);

                    let author = '';
                    const inLinks = Array.from(container.querySelectorAll(selectors.search_posts_author_links.join(', ')));
                    for (const il of inLinks) {
                        const hiddenSpan = il.querySelector(selectors.search_posts_author_name_hidden.join(', '));
                        let raw = hiddenSpan ? hiddenSpan.textContent.trim() : (il.textContent || '').trim().replace(/\\s+/g, ' ');
                        let candidate = raw.split('•')[0].trim().split('\\n')[0].trim();
                        if (candidate.length >= 2 && candidate.length <= 60) {
                            author = candidate;
                            break;
                        }
                    }
                    if (!author) {
                        for (const line of containerText.split('\\n').map(l => l.trim()).filter(Boolean).slice(0, 8)) {
                            if (line.length >= 3 && line.length <= 60 && !line.match(/^(Feed post|Follow|\\d|•|http)/i)) {
                                author = line;
                                break;
                            }
                        }
                    }

                    let postUrl = '';
                    // 1. Direct feed/update link
                    const feedLink = container.querySelector(selectors.search_posts_feed_link.join(', '));
                    if (feedLink) {
                        postUrl = feedLink.href.split('?')[0];
                    }
                    // 2. data-urn on the container or a parent/child
                    if (!postUrl) {
                        const urnSelector = selectors.search_posts_urn_container.join(', ');
                        let urnEl = container.closest(urnSelector)
                                 || container.querySelector(urnSelector);
                        if (!urnEl) {
                            // walk up a few levels
                            let p = container;
                            for (let i = 0; i < 5 && p; i++) {
                                const urn = p.getAttribute && p.getAttribute('data-urn');
                                if (urn && urn.includes('urn:li:activity')) { urnEl = p; break; }
                                p = p.parentElement;
                            }
                        }
                        if (urnEl) {
                            const urn = urnEl.getAttribute('data-urn');
                            postUrl = 'https://www.linkedin.com/feed/update/' + urn;
                        }
                    }
                    // 3. /posts/ style permalink
                    if (!postUrl) {
                        const postsLink = container.querySelector(selectors.search_posts_permalink.join(', '));
                        if (postsLink) postUrl = postsLink.href.split('?')[0];
                    }
                    // 4. Embedded activity URN anywhere in container HTML
                    if (!postUrl) {
                        const html = container.innerHTML || '';
                        const urnMatch = html.match(/urn:li:activity:(\\d+)/);
                        if (urnMatch) {
                            postUrl = 'https://www.linkedin.com/feed/update/urn:li:activity:' + urnMatch[1];
                        }
                    }
                    // Do NOT fall back to profile URL — skip posts without a real post link

                    const timeMatch = containerText.match(/\\b(\\d+[smh]\\b|\\d+[dw]\\b|\\d+ (min|hour|day|week|month)s? ago|just now)/i);
                    const timestamp = timeMatch ? timeMatch[0] : '';

                    const allLines = containerText.split('\\n').map(l => l.trim()).filter(Boolean);
                    const followIdx = allLines.findIndex(l => l.match(/^Follow$/i));
                    const bodyStart = followIdx >= 0 ? followIdx + 1 : 4;
                    const contentLines = allLines.slice(bodyStart)
                        .filter(l => l.length > 15)
                        .filter(l => !l.match(/^(Feed post|Like|Comment|Repost|Send|View my services|\\d+$)/i));
                    const content = contentLines.join(' ').slice(0, 600);

                    // Engagement counts: LinkedIn renders them as aria-labels in various formats:
                    //   "23 reactions", "View 23 reactions", "1,234 reactions", "45 comments", etc.
                    // Strategy 1: aria-label attributes anywhere inside container
                    // Strategy 2: text content of known LinkedIn social-count elements
                    // Strategy 3: innerText scan for numeric + keyword patterns
                    let likes = '';
                    let comments = '';

                    // Strategy 1: aria-label scan (handles "23 reactions" AND "View 23 reactions")
                    const ariaEls = Array.from(container.querySelectorAll(selectors.search_posts_aria_elements.join(', ')));
                    for (const el of ariaEls) {
                        const label = el.getAttribute('aria-label') || '';
                        const lower = label.toLowerCase();
                        // Match "23 reactions", "View 23 reactions", "1,234 reactions", etc.
                        if (/\\d/.test(label) && lower.includes('reaction') && !likes) {
                            const m = label.match(/[\\d,]+/);
                            if (m) likes = m[0].replace(/,/g, '') + ' reactions';
                        } else if (/\\d/.test(label) && lower.includes('comment') && !comments) {
                            const m = label.match(/[\\d,]+/);
                            if (m) comments = m[0].replace(/,/g, '') + ' comments';
                        }
                    }

                    // Strategy 2: known LinkedIn social-count CSS classes
                    if (!likes) {
                        const rxEl = container.querySelector(selectors.search_posts_reactions_count.join(', '));
                        if (rxEl) {
                            const txt = (rxEl.innerText || '').trim();
                            if (/^[\\d,]+$/.test(txt)) likes = txt.replace(/,/g, '') + ' reactions';
                        }
                    }
                    if (!comments) {
                        const cmEl = container.querySelector(selectors.search_posts_comments_count.join(', '));
                        if (cmEl) {
                            const txt = (cmEl.innerText || '').trim();
                            if (/^[\\d,]+$/.test(txt)) comments = txt.replace(/,/g, '') + ' comments';
                        }
                    }

                    // Strategy 3: scan containerText for patterns like "1,234 reactions" or "45 comments"
                    if (!likes) {
                        const rxMatch = containerText.match(/([\\d,]+)\\s+reaction/i);
                        if (rxMatch) likes = rxMatch[1].replace(/,/g, '') + ' reactions';
                    }
                    if (!comments) {
                        const cmMatch = containerText.match(/([\\d,]+)\\s+comment/i);
                        if (cmMatch) comments = cmMatch[1].replace(/,/g, '') + ' comments';
                    }

                    if (content.length > 20 && postUrl && (postUrl.includes('/feed/update/') || postUrl.includes('/posts/'))) {
                        results.push({ postUrl, author, content, timestamp, likes, comments, dedupeKey });
                    }
                }
                return results;
            }"""

            seen_keys = []

            while len(collected) < count and scroll_attempts < max_scrolls:
                new_posts = await page.evaluate(EXTRACT_JS, {
                    "alreadySeen": seen_keys,
                    "selectors": selector_payload(
                        'search_posts_aria_elements',
                        'search_posts_author_links',
                        'search_posts_comments_count',
                        'search_posts_feed_link',
                        'search_posts_permalink',
                        'search_posts_reactions_count',
                        'search_posts_urn_container',
                    ),
                })

                for post in new_posts:
                    key = post['dedupeKey']
                    if key not in seen_keys:
                        seen_keys.append(key)
                        collected.append(post)

                if len(collected) >= count:
                    break

                await page.evaluate("""(selectors) => {
                    const main = document.querySelector(selectors.search_posts_scroll_main.join(', '));
                    if (main) {
                        main.scrollTop = main.scrollHeight;
                    } else {
                        window.scrollTo(0, document.body.scrollHeight);
                    }
                }""", selector_payload('search_posts_scroll_main'))
                await pace(3.0)
                scroll_attempts += 1
                report_progress(ctx, 30 + scroll_attempts * 5, 100, f"Found {len(collected)} posts so far...")

            posts = []
            for idx, p in enumerate(collected[:count], start=1):
                posts.append({
                    "post_number": idx,
                    "post_url": p['postUrl'],
                    "author": p['author'] or 'Unknown',
                    "content": p['content'][:400] + ('...' if len(p['content']) > 400 else ''),
                    "timestamp": p['timestamp'],
                    "likes": p['likes'],
                    "comments": p['comments']
                })

            await session.save_session(page)
            report_progress(ctx, 100, 100, f"Collected {len(posts)} posts")

            return {
                "status": "success",
                "query": query,
                "count": len(posts),
                "posts": posts
            }

        except Exception as e:
            logger.error(f"Post search failed: {str(e)}")
            return {
                "status": "error",
                "message": f"Failed to search posts: {str(e)}"
            }


@mcp.tool()
async def comment_on_approved_posts(approved_posts: list, ctx: Context) -> dict:
    """Post comments on a list of user-approved LinkedIn posts.

    Call this ONLY after the user has reviewed and approved the posts and their comments.

    Args:
        approved_posts: List of dicts, each with 'post_url' and 'comment' keys.
        ctx: MCP context

    Returns:
        dict: status, summary (total/succeeded/failed), and per-post results
    """
    if not approved_posts:
        return {"status": "error", "message": "No posts provided"}

    results = []

    async with BrowserSession(platform='linkedin', headless=False) as session:
        try:
            page = await session.new_page('https://www.linkedin.com/feed/')
            if 'login' in page.url or 'authwall' in page.url:
                return {
                    "status": "error",
                    "message": "Not logged in. Please run login_linkedin_secure tool first"
                }

            total = len(approved_posts)

            for i, item in enumerate(approved_posts):
                post_url = item.get('post_url', '').strip()
                comment_text = item.get('comment', '').strip()

                if not post_url or not comment_text:
                    results.append({
                        "post_url": post_url,
                        "status": "skipped",
                        "message": "Missing post_url or comment"
                    })
                    continue

                report_progress(ctx, i, total, f"Commenting on post {i + 1}/{total}")
                if ctx:
                    ctx.info(f"Navigating to: {post_url}")

                try:
                    nav_url = post_url
                    is_profile_fallback = '/in/' in post_url and '/feed/update/' not in post_url and '/posts/' not in post_url
                    if is_profile_fallback:
                        nav_url = post_url.rstrip('/') + '/recent-activity/all/'

                    await page.goto(nav_url, wait_until='domcontentloaded', timeout=60000)

                    if 'login' in page.url or 'authwall' in page.url:
                        results.append({"post_url": post_url, "status": "error", "message": "Session expired"})
                        continue

                    await wait_for_selector_fallback(page, 'post_detail_container', timeout=20000)
                    await pace(1.5)

                    comment_trigger = await query_selector_fallback(page, 'post_comment_trigger')
                    if comment_trigger:
                        try:
                            await dwell_and_click(comment_trigger)
                        except Exception:
                            pass
                    else:
                        await page.evaluate('''(selectors) => {
                            const btn = Array.from(document.querySelectorAll(selectors.generic_button.join(', ')))
                                .find(b => (b.innerText || '').trim().toLowerCase() === 'comment');
                            if (btn) btn.click();
                        }''', selector_payload('generic_button'))
                    await pace(1.2)

                    editor = await wait_for_selector_fallback(page, 'post_comment_editor', timeout=8000)
                    await dwell_and_click(editor)
                    await type_text(editor, comment_text)
                    await pace(0.6)

                    submit_btn = await query_selector_fallback(page, 'post_comment_submit')
                    if not submit_btn:
                        submitted = await page.evaluate('''(selectors) => {
                            const editor = document.querySelector(selectors.post_comment_editor.join(', '));
                            if (!editor) return false;
                            let el = editor;
                            for (let i = 0; i < 8; i++) {
                                el = el.parentElement;
                                if (!el) break;
                                const btn = el.querySelector(selectors.post_comment_submit.join(', '));
                                if (btn) { btn.click(); return true; }
                            }
                            return false;
                        }''', selector_payload('post_comment_editor', 'post_comment_submit'))
                        if submitted:
                            await pace(2.5)
                            results.append({"post_url": post_url, "status": "success", "message": "Comment posted successfully", "comment": comment_text})
                        else:
                            results.append({"post_url": post_url, "status": "error", "message": "Submit button not found"})
                        continue
                    else:
                        await dwell_and_click(submit_btn)
                        await pace(2.5)
                        results.append({"post_url": post_url, "status": "success", "message": "Comment posted successfully", "comment": comment_text})

                except Exception as e:
                    logger.error(f"Error commenting on {post_url}: {str(e)}")
                    results.append({"post_url": post_url, "status": "error", "message": str(e)})

                await cooldown()

            await session.save_session(page)
            success_count = sum(1 for r in results if r['status'] == 'success')
            report_progress(ctx, total, total, f"Done: {success_count}/{total} comments posted")

            return {
                "status": "success",
                "summary": {"total": total, "succeeded": success_count, "failed": total - success_count},
                "results": results
            }

        except Exception as e:
            return {
                "status": "error",
                "message": f"Failed to post comments: {str(e)}",
                "results": results
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
        
