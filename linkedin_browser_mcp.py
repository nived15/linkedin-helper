from fastmcp import FastMCP, Context
from playwright.async_api import async_playwright
import asyncio
import os
import json
from dotenv import load_dotenv
from cryptography.fernet import Fernet
import time
import logging
import sys
from pathlib import Path
from urllib.parse import quote

# Set up logging to stderr only
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stderr)]
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

def setup_sessions_directory():
    """Set up the sessions directory with proper permissions"""
    try:
        sessions_dir = Path(__file__).parent / 'sessions'
        sessions_dir.mkdir(mode=0o777, parents=True, exist_ok=True)
        # Ensure the directory has the correct permissions even if it already existed
        os.chmod(sessions_dir, 0o777)
        logger.debug(f"Sessions directory set up at {sessions_dir} with full permissions")
        return True
    except Exception as e:
        logger.error(f"Failed to set up sessions directory: {str(e)}")
        return False

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

# Helper to save cookies between sessions
async def save_cookies(page, platform):
    """Save cookies with proper directory permissions"""
    try:
        cookies = await page.context.cookies()
        
        # Validate cookies
        if not cookies or not isinstance(cookies, list):
            raise ValueError("Invalid cookie format")
            
        # Add timestamp for expiration check
        cookie_data = {
            "timestamp": int(time.time()),
            "cookies": cookies
        }
        
        # Ensure sessions directory exists with proper permissions
        if not setup_sessions_directory():
            raise Exception("Failed to set up sessions directory")
        
        # Encrypt cookies before saving
        key = os.getenv('COOKIE_ENCRYPTION_KEY', Fernet.generate_key())
        f = Fernet(key)
        encrypted_data = f.encrypt(json.dumps(cookie_data).encode())
        
        cookie_file = Path(__file__).parent / 'sessions' / f'{platform}_cookies.json'
        with open(cookie_file, 'wb') as f:
            f.write(encrypted_data)
        # Set file permissions to 666 (rw-rw-rw-)
        os.chmod(cookie_file, 0o666)
            
    except Exception as e:
        raise Exception(f"Failed to save cookies: {str(e)}")

# Helper to load cookies
async def load_cookies(context, platform):
    try:
        with open(f'sessions/{platform}_cookies.json', 'rb') as f:
            encrypted_data = f.read()
            
        # Decrypt cookies
        key = os.getenv('COOKIE_ENCRYPTION_KEY')
        if not key:
            return False
            
        f = Fernet(key)
        cookie_data = json.loads(f.decrypt(encrypted_data))
        
        # Check cookie expiration (24 hours)
        if int(time.time()) - cookie_data["timestamp"] > 86400:
            os.remove(f'sessions/{platform}_cookies.json')
            return False
            
        await context.add_cookies(cookie_data["cookies"])
        return True
        
    except FileNotFoundError:
        return False
    except Exception as e:
        # If there's any error loading cookies, delete the file and start fresh
        try:
            os.remove(f'sessions/{platform}_cookies.json')
        except:
            pass
        return False
    
class BrowserSession:
    """Context manager for browser sessions with cookie persistence"""
    
    def __init__(self, platform='linkedin', headless=False, launch_timeout=30000, max_retries=3):
        logger.info(f"Initializing {platform} browser session (headless: {headless})")
        self.platform = platform
        self.headless = headless
        self.launch_timeout = launch_timeout
        self.max_retries = max_retries
        self.playwright = None
        self.browser = None
        self.context = None
        self._closed = False
        
    async def __aenter__(self):
        retry_count = 0
        last_error = None
        
        # Ensure sessions directory exists with proper permissions
        if not setup_sessions_directory():
            raise Exception("Failed to set up sessions directory with proper permissions")
        
        while retry_count < self.max_retries and not self._closed:
            try:
                logger.info(f"Starting Playwright (attempt {retry_count + 1}/{self.max_retries})")
                
                # Ensure clean state
                await self._cleanup()
                
                # Initialize Playwright with timeout
                self.playwright = await asyncio.wait_for(
                    async_playwright().start(),
                    timeout=self.launch_timeout/1000
                )
                
                # Launch browser with more generous timeout and retry logic
                launch_success = False
                for attempt in range(3):
                    try:
                        logger.info(f"Launching browser (sub-attempt {attempt + 1}/3)")
                        self.browser = await self.playwright.chromium.launch(
                            headless=self.headless,
                            timeout=self.launch_timeout,
                            args=[
                                '--disable-dev-shm-usage',
                                '--no-sandbox',
                                '--disable-blink-features=AutomationControlled',  # Try to avoid detection
                                '--start-maximized'  # Start with maximized window
                            ]
                        )
                        launch_success = True
                        break
                    except Exception as e:
                        logger.error(f"Browser launch sub-attempt {attempt + 1} failed: {str(e)}")
                        await asyncio.sleep(2)  # Increased delay between attempts
                
                if not launch_success:
                    raise Exception("Failed to launch browser after 3 attempts")
                
                logger.info("Creating browser context")
                self.context = await self.browser.new_context(
                    viewport={'width': 1280, 'height': 800},
                    user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/96.0.4664.110 Safari/537.36'
                )
                
                # Try to load existing session
                logger.info("Attempting to load existing session")
                try:
                    session_loaded = await load_cookies(self.context, self.platform)
                    if session_loaded:
                        logger.info("Existing session loaded successfully")
                    else:
                        logger.info("No existing session found or session expired")
                except Exception as cookie_error:
                    logger.warning(f"Error loading cookies: {str(cookie_error)}")
                    # Continue even if cookie loading fails
                
                return self
                
            except Exception as e:
                last_error = e
                retry_count += 1
                logger.error(f"Browser session initialization attempt {retry_count} failed: {str(e)}")
                
                # Cleanup on failure
                await self._cleanup()
                
                if retry_count < self.max_retries and not self._closed:
                    await asyncio.sleep(2 * retry_count)  # Exponential backoff
                else:
                    logger.error("All browser session initialization attempts failed")
                    raise Exception(f"Failed to initialize browser after {self.max_retries} attempts. Last error: {str(last_error)}")

    async def _cleanup(self):
        """Clean up browser resources"""
        if self.browser:
            try:
                await self.browser.close()
            except Exception as e:
                logger.error(f"Error closing browser: {str(e)}")
        if self.playwright:
            try:
                await self.playwright.stop()
            except Exception as e:
                logger.error(f"Error stopping playwright: {str(e)}")
        self.browser = None
        self.playwright = None
        self.context = None

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        logger.info("Closing browser session")
        self._closed = True
        await self._cleanup()
        
    async def new_page(self, url=None):
        if self._closed:
            raise Exception("Browser session has been closed")
        
        page = await self.context.new_page()
        if url:
            try:
                await page.goto(url, wait_until='domcontentloaded', timeout=30000)
            except Exception as e:
                logger.error(f"Error navigating to {url}: {str(e)}")
                raise
        return page
        
    async def save_session(self, page):
        if self._closed:
            raise Exception("Browser session has been closed")
            
        try:
            await save_cookies(page, self.platform)
        except Exception as e:
            logger.error(f"Error saving session: {str(e)}")
            raise

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
                    await page.fill('#username', username)
                if password:
                    await page.fill('#password', password)
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
                await asyncio.sleep(3)
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
    
    # We'll pass the credentials to pre-fill them, but user can still modify them
    return await login_linkedin(username if username else None, password if password else None, ctx)

@mcp.tool()
async def get_linkedin_profile(username: str, ctx: Context) -> dict:
    """Get LinkedIn profile information including follower count and profile views"""
    async with BrowserSession(platform='linkedin', headless=False) as session:
        try:
            page = await session.new_page(f'https://www.linkedin.com/in/{username}')
            
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
            await page.wait_for_selector('.pv-top-card', timeout=10000)
            
            # Extract profile information
            profile_data = await page.evaluate('''() => {
                const getData = (selector) => {
                    const el = document.querySelector(selector);
                    return el ? el.innerText.trim() : null;
                };
                
                // Try to get follower count from multiple possible locations
                let followerCount = null;
                const followerElements = document.querySelectorAll('.pv-top-card--list-bullet .t-bold, .pvs-header__optional-link span.t-bold');
                for (const el of followerElements) {
                    const text = el.innerText.trim().toLowerCase();
                    if (text.includes('follower')) {
                        const match = text.match(/([0-9,]+)/);
                        if (match) followerCount = parseInt(match[1].replace(/,/g, ''));
                    }
                }
                // Also check the connections/followers section
                if (!followerCount) {
                    const allSpans = document.querySelectorAll('span');
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
                    name: getData('.pv-top-card--list .text-heading-xlarge') || getData('h1'),
                    headline: getData('.pv-top-card--list .text-body-medium'),
                    location: getData('.pv-top-card--list .text-body-small:not(.inline)'),
                    follower_count: followerCount,
                    connection_count: (() => {
                        const el = document.querySelector('.pv-top-card--list-bullet .t-bold');
                        if (el) {
                            const text = el.innerText.trim();
                            const match = text.match(/([0-9,]+)/);
                            if (match && !text.toLowerCase().includes('follower')) return parseInt(match[1].replace(/,/g, ''));
                        }
                        return null;
                    })(),
                    about: getData('.pv-shared-text-with-see-more .inline-show-more-text'),
                    profile_url: window.location.href
                };
            }''')
            
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
            await page.wait_for_timeout(3000)

            # Scroll to load content
            for i in range(min(count, 20)):  # Limit to reasonable number
                report_progress(ctx, i, count, f"Loading post {i+1}/{count}")
                
                try:
                    # Try multiple selectors — LinkedIn updates class names frequently
                    post_selector = None
                    for selector in [
                        '[data-urn*="urn:li:activity"]',
                        '[data-id*="urn:li:activity"]',
                        '.feed-shared-update-v2',
                        '.occludable-update',
                        'div[data-urn]',
                    ]:
                        try:
                            await page.wait_for_selector(selector, timeout=4000)
                            post_selector = selector
                            break
                        except Exception:
                            continue

                    if not post_selector:
                        errors.append(f"Error during scroll {i}: No feed post elements found on page")
                        await page.evaluate('window.scrollBy(0, 800)')
                        await page.wait_for_timeout(1500)
                        continue
                    
                    # Extract visible posts
                    new_posts = await page.evaluate('''() => {
                        // Try multiple selectors in order
                        const selectors = [
                            '[data-urn*="urn:li:activity"]',
                            '[data-id*="urn:li:activity"]',
                            ".feed-shared-update-v2",
                            ".occludable-update",
                            "div[data-urn]"
                        ];
                        let postElements = [];
                        for (const sel of selectors) {
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
                                    const shareLink = post.querySelector('a[href*="/feed/update/"]');
                                    if (shareLink) postUrl = shareLink.href.split('?')[0];
                                }

                                // Author profile URL
                                const authorLink = post.querySelector(
                                    '.update-components-actor__container-link, .feed-shared-actor__container-link, a[href*="/in/"]'
                                );
                                const authorProfileUrl = authorLink ? authorLink.href.split('?')[0] : null;

                                // Likes
                                const likesText = getText(post,
                                    '.social-details-social-counts__reactions-count',
                                    'button[aria-label*="reaction"] span',
                                    '[aria-label*="like"] span'
                                );
                                const likesCount = parseInt((likesText || '0').replace(/[^0-9]/g, '')) || 0;

                                // Comments
                                let commentsCount = 0;
                                for (const btn of post.querySelectorAll(
                                    '.social-details-social-counts__comments, button[aria-label*="comment"]'
                                )) {
                                    const t = btn.innerText?.trim() || btn.getAttribute('aria-label') || '';
                                    const m = t.match(/([0-9]+)/);
                                    if (m) { commentsCount = parseInt(m[1]); break; }
                                }

                                return {
                                    author: getText(post,
                                        '.update-components-actor__name span[aria-hidden="true"]',
                                        '.update-components-actor__name',
                                        '.feed-shared-actor__name'
                                    ) || 'Unknown',
                                    author_headline: getText(post,
                                        '.update-components-actor__description span[aria-hidden="true"]',
                                        '.update-components-actor__description',
                                        '.feed-shared-actor__description'
                                    ),
                                    author_profile_url: authorProfileUrl,
                                    content: getText(post,
                                        '.update-components-text span[dir]',
                                        '.update-components-text',
                                        '.feed-shared-text__text-view span[dir]',
                                        '.feed-shared-text'
                                    ),
                                    timestamp: getText(post,
                                        '.update-components-actor__sub-description span[aria-hidden="true"]',
                                        '.update-components-actor__sub-description',
                                        '.feed-shared-actor__sub-description'
                                    ),
                                    post_url: postUrl,
                                    likes_count: likesCount,
                                    comments_count: commentsCount
                                };
                            } catch (e) {
                                return null;
                            }
                        }).filter(p => p !== null && (p.content || p.author !== 'Unknown'));
                    }''')
                    
                    # Add new posts to our collection, avoiding duplicates
                    for post in new_posts:
                        if post not in posts:
                            posts.append(post)
                            
                    if len(posts) >= count:
                        break
                        
                    # Scroll down to load more content
                    await page.evaluate('window.scrollBy(0, 800)')
                    await page.wait_for_timeout(1000)  # Wait for content to load
                    
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
            search_url = f'https://www.linkedin.com/search/results/people/?keywords={query}'
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
            await page.wait_for_selector('.reusable-search__result-container', timeout=10000)
            ctx.info("Search results loaded")
            report_progress(ctx, 50, 100, "Extracting profile data...")
            
            # Extract profile data
            profiles = await page.evaluate('''(count) => {
                const results = [];
                const profileCards = document.querySelectorAll('.reusable-search__result-container');
                
                for (let i = 0; i < Math.min(profileCards.length, count); i++) {
                    const card = profileCards[i];
                    try {
                        const profile = {
                            name: card.querySelector('.entity-result__title-text a')?.innerText?.trim() || 'Unknown',
                            headline: card.querySelector('.entity-result__primary-subtitle')?.innerText?.trim() || '',
                            location: card.querySelector('.entity-result__secondary-subtitle')?.innerText?.trim() || '',
                            profileUrl: card.querySelector('.app-aware-link')?.href || '',
                            connectionDegree: card.querySelector('.dist-value')?.innerText?.trim() || '',
                            snippet: card.querySelector('.entity-result__summary')?.innerText?.trim() || ''
                        };
                        results.push(profile);
                    } catch (e) {
                        console.error("Error extracting profile", e);
                    }
                }
                return results;
            }''', count)
            
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
async def view_linkedin_profile(profile_url: str, ctx: Context) -> dict:
    """Visit and extract data from a specific LinkedIn profile"""
    if not ('linkedin.com/in/' in profile_url):
        return {
            "status": "error",
            "message": "Invalid LinkedIn profile URL. Should contain 'linkedin.com/in/'"
        }
        
    async with BrowserSession(platform='linkedin') as session:
        try:
            page = await session.new_page(profile_url)
            
            # Check if we're logged in
            if 'login' in page.url:
                return {
                    "status": "error", 
                    "message": "Not logged in. Please run login_linkedin tool first"
                }
                
            ctx.info(f"Viewing profile: {profile_url}")
            
            # Wait for profile to load
            await page.wait_for_selector('.pv-top-card', timeout=10000)
            await ctx.report_progress(0.5, 1.0)
            
            # Extract profile information
            profile_data = await page.evaluate('''() => {
                const getData = (selector, property = 'innerText') => {
                    const element = document.querySelector(selector);
                    return element ? element[property].trim() : null;
                };
                
                return {
                    name: getData('.pv-top-card--list .text-heading-xlarge'),
                    headline: getData('.pv-top-card--list .text-body-medium'),
                    location: getData('.pv-top-card--list .text-body-small:not(.inline)'),
                    connectionDegree: getData('.pv-top-card__connections-count .t-black--light'),
                    about: getData('.pv-shared-text-with-see-more .inline-show-more-text'),
                    experience: Array.from(document.querySelectorAll('#experience-section .pv-entity__summary-info'))
                        .map(exp => ({
                            title: exp.querySelector('h3')?.innerText?.trim() || '',
                            company: exp.querySelector('.pv-entity__secondary-title')?.innerText?.trim() || '',
                            duration: exp.querySelector('.pv-entity__date-range span:not(.visually-hidden)')?.innerText?.trim() || ''
                        })),
                    education: Array.from(document.querySelectorAll('#education-section .pv-education-entity'))
                        .map(edu => ({
                            school: edu.querySelector('.pv-entity__school-name')?.innerText?.trim() || '',
                            degree: edu.querySelector('.pv-entity__degree-name .pv-entity__comma-item')?.innerText?.trim() || '',
                            field: edu.querySelector('.pv-entity__fos .pv-entity__comma-item')?.innerText?.trim() || '',
                            dates: edu.querySelector('.pv-entity__dates span:not(.visually-hidden)')?.innerText?.trim() || ''
                        }))
                };
            }''')
            
            await ctx.report_progress(1.0, 1.0)
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
            await page.wait_for_selector('.feed-shared-update-v2', timeout=10000)
            ctx.info(f"Post loaded, performing action: {action}")
            
            # Read post content
            post_content = await page.evaluate('''() => {
                const post = document.querySelector('.feed-shared-update-v2');
                return {
                    author: post.querySelector('.feed-shared-actor__name')?.innerText?.trim() || 'Unknown',
                    content: post.querySelector('.feed-shared-text')?.innerText?.trim() || '',
                    engagementCount: post.querySelector('.social-details-social-counts__reactions-count')?.innerText?.trim() || '0'
                };
            }''')
            
            # Perform the requested action
            if action == "like":
                # Find and click like button if not already liked
                liked = await page.evaluate('''() => {
                    const likeButton = document.querySelector('button.react-button__trigger');
                    const isLiked = likeButton.getAttribute('aria-pressed') === 'true';
                    if (!isLiked) {
                        likeButton.click();
                        return true;
                    }
                    return false;
                }''')
                
                result = {
                    "status": "success",
                    "action": "like",
                    "performed": liked,
                    "message": "Successfully liked the post" if liked else "Post was already liked"
                }
                
            elif action == "comment" and comment:
                # Add comment to the post
                await page.click('button.comments-comment-box__trigger')  # Open comment box
                await page.fill('.ql-editor', comment)
                await page.click('button.comments-comment-box__submit-button')  # Submit comment
                
                # Wait for comment to appear
                await page.wait_for_timeout(2000)
                
                result = {
                    "status": "success",
                    "action": "comment",
                    "message": "Comment posted successfully"
                }
                
            elif action == "share":
                # Repost/share the post
                try:
                    # Click the repost button
                    repost_button = await page.wait_for_selector(
                        'button[aria-label*="Repost"], button[aria-label*="repost"]',
                        timeout=5000
                    )
                    await repost_button.click()
                    await page.wait_for_timeout(1000)
                    
                    # Click "Repost" option (instant repost without comment)
                    repost_option = await page.wait_for_selector(
                        'button:has-text("Repost"), div[data-artdeco-is-focused] button',
                        timeout=5000
                    )
                    await repost_option.click()
                    await page.wait_for_timeout(2000)
                    
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
async def send_connection_request(profile_url: str, ctx: Context, note: str | None = None) -> dict:
    """Send a connection request to a LinkedIn profile with an optional personalised note.
    
    Args:
        profile_url: The LinkedIn profile URL (must contain 'linkedin.com/in/')
        ctx: MCP context for logging and progress reporting
        note: Optional personalised connection note (max 300 characters)
        
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
            page = await session.new_page(profile_url)
            
            # Check if we're logged in
            if 'login' in page.url:
                return {
                    "status": "error",
                    "message": "Not logged in. Please run login_linkedin tool first"
                }
            
            ctx.info(f"Sending connection request to {profile_url}")
            
            # Wait for profile to load
            await page.wait_for_selector('.pv-top-card', timeout=10000)
            
            # Look for the Connect button — it may be in the main actions or the More dropdown
            connect_button = None
            try:
                connect_button = await page.wait_for_selector(
                    'button[aria-label*="Invite"][aria-label*="connect"], button:has-text("Connect")',
                    timeout=5000
                )
            except Exception:
                # Connect might be hidden under "More" dropdown
                try:
                    more_button = await page.wait_for_selector(
                        'button[aria-label="More actions"], button:has-text("More")',
                        timeout=3000
                    )
                    await more_button.click()
                    await page.wait_for_timeout(1000)
                    connect_button = await page.wait_for_selector(
                        'div[role="listbox"] button:has-text("Connect"), li button:has-text("Connect")',
                        timeout=3000
                    )
                except Exception:
                    pass
            
            if not connect_button:
                return {
                    "status": "error",
                    "message": "Connect button not found. Profile may already be connected or pending."
                }
            
            await connect_button.click()
            await page.wait_for_timeout(1500)
            
            if note:
                # Click "Add a note" button in the connection dialog
                try:
                    add_note_button = await page.wait_for_selector(
                        'button[aria-label="Add a note"], button:has-text("Add a note")',
                        timeout=3000
                    )
                    await add_note_button.click()
                    await page.wait_for_timeout(500)
                    
                    # Fill in the note
                    note_field = await page.wait_for_selector(
                        'textarea[name="message"], textarea#custom-message',
                        timeout=3000
                    )
                    await note_field.fill(note)
                    await page.wait_for_timeout(500)
                except Exception as note_error:
                    ctx.info(f"Could not add note: {str(note_error)}. Sending without note.")
            
            # Click Send
            try:
                send_button = await page.wait_for_selector(
                    'button[aria-label="Send invitation"], button[aria-label="Send now"], button:has-text("Send")',
                    timeout=5000
                )
                await send_button.click()
                await page.wait_for_timeout(2000)
            except Exception as send_error:
                return {
                    "status": "error",
                    "message": f"Failed to click Send: {str(send_error)}"
                }
            
            # Extract the profile name for logging
            profile_name = await page.evaluate('''() => {
                const el = document.querySelector('.pv-top-card--list .text-heading-xlarge, h1');
                return el ? el.innerText.trim() : 'Unknown';
            }''')
            
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
async def search_linkedin_posts(query: str, ctx: Context, count: int = 10) -> dict:
    """Search for LinkedIn posts matching a keyword query and return them with metadata.

    Args:
        query: Search keyword, e.g. "GitHub Copilot"
        ctx: MCP context
        count: Number of posts to retrieve (default: 10)

    Returns:
        dict: status, query, count, and posts array.
              Each post has: post_number, post_url, author, content, timestamp, likes, comments
    """
    async with BrowserSession(platform='linkedin', headless=False) as session:
        try:
            search_url = f'https://www.linkedin.com/search/results/content/?keywords={quote(query)}&sortBy=date_posted'
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
            await page.wait_for_timeout(10000)
            report_progress(ctx, 30, 100, "Extracting posts...")

            collected = []
            scroll_attempts = 0
            max_scrolls = 10

            EXTRACT_JS = """(alreadySeen) => {
                const results = [];
                const seenKeys = new Set(alreadySeen);

                const authorLinks = Array.from(document.querySelectorAll('a[href*="linkedin.com/in/"]'));

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
                    const inLinks = Array.from(container.querySelectorAll('a[href*="linkedin.com/in/"]'));
                    for (const il of inLinks) {
                        const hiddenSpan = il.querySelector('span[aria-hidden="true"]');
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
                    const feedLink = container.querySelector('a[href*="feed/update/urn:li:"]');
                    if (feedLink) {
                        postUrl = feedLink.href.split('?')[0];
                    } else {
                        const profileLink = container.querySelector('a[href*="linkedin.com/in/"]');
                        if (profileLink) postUrl = profileLink.href.split('?')[0];
                    }

                    const timeMatch = containerText.match(/\\b(\\d+[smh]\\b|\\d+[dw]\\b|\\d+ (min|hour|day|week|month)s? ago|just now)/i);
                    const timestamp = timeMatch ? timeMatch[0] : '';

                    const allLines = containerText.split('\\n').map(l => l.trim()).filter(Boolean);
                    const followIdx = allLines.findIndex(l => l.match(/^Follow$/i));
                    const bodyStart = followIdx >= 0 ? followIdx + 1 : 4;
                    const contentLines = allLines.slice(bodyStart)
                        .filter(l => l.length > 15)
                        .filter(l => !l.match(/^(Feed post|Like|Comment|Repost|Send|View my services|\\d+$)/i));
                    const content = contentLines.join(' ').slice(0, 600);

                    // Reaction/comment COUNT elements have aria-labels starting with a digit (e.g. "23 reactions")
                    // The Like action BUTTON has aria-label "Reaction button state: no reaction" — starts with capital R, not a digit
                    // Walk all [aria-label] elements inside container and pick the count ones only
                    let likes = '';
                    let comments = '';
                    const ariaEls = Array.from(container.querySelectorAll('[aria-label]'));
                    for (const el of ariaEls) {
                        const label = el.getAttribute('aria-label') || '';
                        if (/^\\d/.test(label)) {
                            const lower = label.toLowerCase();
                            if (lower.includes('reaction') && !likes) likes = label;
                            else if (lower.includes('comment') && !comments) comments = label;
                        }
                    }

                    if (content.length > 20) {
                        results.push({ postUrl, author, content, timestamp, likes, comments, dedupeKey });
                    }
                }
                return results;
            }"""

            seen_keys = []

            while len(collected) < count and scroll_attempts < max_scrolls:
                new_posts = await page.evaluate(EXTRACT_JS, seen_keys)

                for post in new_posts:
                    key = post['dedupeKey']
                    if key not in seen_keys:
                        seen_keys.append(key)
                        collected.append(post)

                if len(collected) >= count:
                    break

                await page.evaluate("""() => {
                    const main = document.querySelector('main#workspace') || document.querySelector('main');
                    if (main) {
                        main.scrollTop = main.scrollHeight;
                    } else {
                        window.scrollTo(0, document.body.scrollHeight);
                    }
                }""")
                await page.wait_for_timeout(3000)
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

                    await page.wait_for_selector(
                        '.feed-shared-update-v2, .update-components-update-v2, '
                        '.occludable-update, [data-urn]',
                        timeout=20000
                    )
                    await page.wait_for_timeout(1500)

                    comment_trigger = await page.query_selector(
                        'button.comment-button, '
                        'button[aria-label*="omment"], '
                        'button[aria-label*="Comment"], '
                        '.comments-comment-box__trigger, '
                        'button.comments-comment-box__trigger'
                    )
                    if comment_trigger:
                        try:
                            await comment_trigger.click()
                        except Exception:
                            pass
                    else:
                        await page.evaluate('''() => {
                            const btn = Array.from(document.querySelectorAll('button'))
                                .find(b => (b.innerText || '').trim().toLowerCase() === 'comment');
                            if (btn) btn.click();
                        }''')
                    await page.wait_for_timeout(1200)

                    editor = await page.wait_for_selector(
                        '.ql-editor[contenteditable="true"], '
                        '.comments-comment-box__text-editor [contenteditable="true"], '
                        'div[contenteditable="true"][role="textbox"]',
                        timeout=8000
                    )
                    await editor.click()
                    await editor.type(comment_text, delay=25)
                    await page.wait_for_timeout(600)

                    submit_btn = await page.query_selector(
                        'button.comments-comment-box__submit-button--cr, '
                        'button.comments-comment-box__submit-button, '
                        'button[type="submit"].comments-comment-box__submit-button--cr, '
                        'button[type="submit"].comments-comment-box__submit-button'
                    )
                    if not submit_btn:
                        submitted = await page.evaluate('''() => {
                            const editor = document.querySelector('.ql-editor[contenteditable="true"], div[contenteditable="true"][role="textbox"]');
                            if (!editor) return false;
                            let el = editor;
                            for (let i = 0; i < 8; i++) {
                                el = el.parentElement;
                                if (!el) break;
                                const btn = el.querySelector('button[type="submit"], button.comments-comment-box__submit-button--cr, button.comments-comment-box__submit-button');
                                if (btn) { btn.click(); return true; }
                            }
                            return false;
                        }''')
                        if submitted:
                            await page.wait_for_timeout(2500)
                            results.append({"post_url": post_url, "status": "success", "message": "Comment posted successfully", "comment": comment_text})
                        else:
                            results.append({"post_url": post_url, "status": "error", "message": "Submit button not found"})
                        continue
                    else:
                        await submit_btn.click()
                        await page.wait_for_timeout(2500)
                        results.append({"post_url": post_url, "status": "success", "message": "Comment posted successfully", "comment": comment_text})

                except Exception as e:
                    logger.error(f"Error commenting on {post_url}: {str(e)}")
                    results.append({"post_url": post_url, "status": "error", "message": str(e)})

                await asyncio.sleep(4)

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
        
