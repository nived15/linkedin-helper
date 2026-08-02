"""Centralised LinkedIn selector registry with ordered fallbacks.

Every selector LinkedIn automation touches lives here so a DOM change is one
edit rather than a search across the codebase. A name maps to a tuple of CSS
selectors tried in order, newest markup first, so a page that has already moved
on still resolves through a later entry.

Rebuilt for SCRAPE-01
---------------------
The search surface was written against LinkedIn's 2021 DOM and had rotted. The
old `search_result_*` group pointed at `.entity-result` classes LinkedIn has
since replaced, and the post search path had given up on selectors entirely and
walked the DOM by hand looking for anything that resembled a post. Both are
rebuilt here rather than patched.

The modern targets lead with attribute hooks that survive LinkedIn's class name
churn: `data-chameleon-result-urn` on a search result card, `data-view-name` on
a rendered template, `data-urn` on an activity. Class based selectors follow as
fallbacks, ending with the 2021 ones, because an ordered list costs nothing and
a stale entry that never matches is harmless.

Verification status
-------------------
The attribute hooks and the newer class names in this file were written from
LinkedIn's published markup patterns, not from a live session at the time of
writing. Treat the first entry of each rebuilt group as a hypothesis until it
has been checked against a logged-in browser. The fallback chains exist exactly
so that an unverified leading selector degrades instead of breaking the run.
"""

from __future__ import annotations

from typing import Iterable

SELECTORS: dict[str, tuple[str, ...]] = {
    # --- Authentication ---------------------------------------------------
    "login_username": (
        "#username",
        'input[name="session_key"]',
    ),
    "login_password": (
        "#password",
        'input[name="session_password"]',
    ),
    # --- Profile page -----------------------------------------------------
    "profile_top_card": (
        "section[data-member-id]",
        ".pv-top-card",
        ".top-card-layout",
    ),
    "profile_name": (
        ".pv-top-card--list .text-heading-xlarge",
        "h1.text-heading-xlarge",
        "h1",
    ),
    "profile_headline": (
        ".pv-top-card--list .text-body-medium",
        ".text-body-medium.break-words",
    ),
    "profile_location": (
        ".pv-top-card--list .text-body-small:not(.inline)",
        ".text-body-small.inline.t-black--light.break-words",
    ),
    "profile_follower_items": (
        ".pv-top-card--list-bullet .t-bold",
        ".pvs-header__optional-link span.t-bold",
    ),
    "profile_text_spans": (
        "span",
    ),
    "profile_connection_count": (
        ".pv-top-card--list-bullet .t-bold",
        ".pv-top-card__connections-count .t-black--light",
    ),
    "profile_about": (
        ".pv-shared-text-with-see-more .inline-show-more-text",
        ".display-flex.ph5.pv3 .visually-hidden",
    ),
    # --- Global search bar ------------------------------------------------
    "global_search_trigger": (
        "button.search-global-typeahead__collapsed-search-button",
        "#global-nav-search button",
        'button[aria-label="Search"]',
    ),
    "global_search_input": (
        "input.search-global-typeahead__input",
        "#global-nav-typeahead input",
        'input[role="combobox"][aria-label*="Search"]',
        'input[placeholder="Search"]',
    ),
    "global_search_typeahead_option": (
        'div[role="listbox"] li',
        ".search-typeahead-v2__hit",
        ".search-global-typeahead__hit",
    ),
    # --- People search results (rebuilt, SCRAPE-01) -----------------------
    # `search_result_*` is the legacy group `linkedin_browser_mcp.py` reads.
    # `people_result_*` is the rebuilt group the scrape package reads. Both
    # resolve, so neither caller has to move before the other is ready.
    "search_result_container": (
        "div[data-chameleon-result-urn]",
        'div[data-view-name="search-entity-result-universal-template"]',
        "li.reusable-search__result-container",
        ".reusable-search__result-container",
        ".search-results-container .entity-result",
    ),
    "search_result_title_link": (
        'span[data-anonymize="person-name"]',
        '.entity-result__title-text a span[aria-hidden="true"]',
        ".entity-result__title-text a",
        '.app-aware-link span[aria-hidden="true"]',
    ),
    "search_result_headline": (
        ".entity-result__primary-subtitle",
        "div.t-14.t-black.t-normal",
    ),
    "search_result_location": (
        ".entity-result__secondary-subtitle",
        "div.t-14.t-normal.t-black--light",
    ),
    "search_result_profile_link": (
        'a[href*="/in/"]',
        ".app-aware-link",
    ),
    "search_result_distance": (
        ".entity-result__badge-text",
        "span.entity-result__badge span.dist-value",
        ".dist-value",
    ),
    "search_result_snippet": (
        "p.entity-result__summary",
        ".entity-result__summary",
    ),
    "people_result_item": (
        "div[data-chameleon-result-urn]",
        'div[data-view-name="search-entity-result-universal-template"]',
        'ul[role="list"] > li:has(a[href*="/in/"])',
        "li.reusable-search__result-container",
        ".search-results-container .entity-result",
    ),
    "people_result_profile_link": (
        'a[data-test-app-aware-link][href*="/in/"]',
        'a[href*="/in/"]',
        ".app-aware-link",
    ),
    "people_result_name": (
        'span[data-anonymize="person-name"]',
        'a[href*="/in/"] span[aria-hidden="true"]',
        '.entity-result__title-text a span[aria-hidden="true"]',
        ".entity-result__title-text",
    ),
    "people_result_headline": (
        'div[data-anonymize="headline"]',
        ".entity-result__primary-subtitle",
        "div.t-14.t-black.t-normal",
    ),
    "people_result_location": (
        'div[data-anonymize="location"]',
        ".entity-result__secondary-subtitle",
        "div.t-14.t-normal.t-black--light",
    ),
    "people_result_summary": (
        "p.entity-result__summary",
        ".entity-result__summary",
    ),
    "people_result_current_position": (
        'p.entity-result__summary--2-lines',
        ".entity-result__summary",
    ),
    "people_result_distance": (
        ".entity-result__badge-text",
        "span.entity-result__badge span.dist-value",
        ".dist-value",
    ),
    "people_result_avatar": (
        "img.presence-entity__image",
        'img[class*="evi-image"]',
        "img.EntityPhoto-circle-3",
    ),
    "people_result_premium_badge": (
        'li-icon[type="linkedin-bug"][aria-label*="Premium"]',
        'svg[data-test-icon="premium-chip-xsmall"]',
        ".entity-result__badge--premium",
    ),
    "search_results_empty_state": (
        'div[data-view-name="search-no-results"]',
        ".search-reusable-search-no-results",
        ".search-results__no-results",
    ),
    "search_results_scroll_container": (
        "main#workspace",
        "div.search-results-container",
        "main",
    ),
    "search_pagination_next": (
        "button.artdeco-pagination__button--next",
        'button[aria-label="Next"]',
    ),
    "search_total_results": (
        ".search-results-container h2",
        "div.pb2.t-black--light.t-14",
    ),
    # --- Feed posts -------------------------------------------------------
    "feed_post_container": (
        '[data-urn*="urn:li:activity"]',
        '[data-id*="urn:li:activity"]',
        ".feed-shared-update-v2",
        ".occludable-update",
        "div[data-urn]",
    ),
    "feed_post_share_link": (
        'a[href*="/feed/update/"]',
    ),
    "feed_post_author_link": (
        ".update-components-actor__meta-link",
        ".update-components-actor__container-link",
        ".feed-shared-actor__container-link",
        'a[href*="/in/"]',
    ),
    "feed_post_likes": (
        ".social-details-social-counts__reactions-count",
        'button[aria-label*="reaction"] span',
        'button[aria-label*="like"] span',
    ),
    "feed_post_comments": (
        ".social-details-social-counts__comments",
        'button[aria-label*="comment"]',
    ),
    "feed_post_author_name": (
        '.update-components-actor__title span[aria-hidden="true"]',
        '.update-components-actor__name span[aria-hidden="true"]',
        ".update-components-actor__name",
        ".feed-shared-actor__name",
    ),
    "feed_post_author_headline": (
        '.update-components-actor__description span[aria-hidden="true"]',
        ".update-components-actor__description",
        ".feed-shared-actor__description",
    ),
    "feed_post_content": (
        '.update-components-text span[dir]',
        ".update-components-text",
        '.feed-shared-text__text-view span[dir]',
        ".feed-shared-text",
    ),
    "feed_post_timestamp": (
        '.update-components-actor__sub-description span[aria-hidden="true"]',
        ".update-components-actor__sub-description",
        ".feed-shared-actor__sub-description",
    ),
    # --- Single post detail ----------------------------------------------
    "post_detail_container": (
        ".feed-shared-update-v2",
        ".update-components-update-v2",
        ".occludable-update",
        '[data-urn*="urn:li:activity"]',
    ),
    "post_detail_author": (
        ".feed-shared-actor__name",
        '.update-components-actor__title span[aria-hidden="true"]',
        '.update-components-actor__name span[aria-hidden="true"]',
        ".update-components-actor__name",
    ),
    "post_detail_content": (
        ".feed-shared-text",
        '.feed-shared-text__text-view span[dir]',
        '.update-components-text span[dir]',
        ".update-components-text",
    ),
    "post_detail_engagement": (
        ".social-details-social-counts__reactions-count",
        '[aria-label*="reaction"]',
    ),
    "post_like_button": (
        "button.react-button__trigger",
        'button[aria-label*="Like"]',
    ),
    "post_comment_trigger": (
        "button.comments-comment-box__trigger",
        "button.comment-button",
        'button[aria-label*="Comment"]',
        'button[aria-label*="comment"]',
    ),
    "post_comment_editor": (
        '.ql-editor[contenteditable="true"]',
        '.comments-comment-box__text-editor [contenteditable="true"]',
        'div[contenteditable="true"][role="textbox"]',
    ),
    "post_comment_submit": (
        "button.comments-comment-box__submit-button--cr",
        "button.comments-comment-box__submit-button",
        'button[type="submit"].comments-comment-box__submit-button--cr',
        'button[type="submit"].comments-comment-box__submit-button',
    ),
    "post_repost_button": (
        'button[aria-label*="Repost"]',
        'button[aria-label*="repost"]',
    ),
    "post_repost_option": (
        'button:has-text("Repost")',
        "div[data-artdeco-is-focused] button",
    ),
    # --- Invitations ------------------------------------------------------
    "connect_button": (
        'button[aria-label*="Invite"][aria-label*="connect"]',
        'button:has-text("Connect")',
    ),
    "more_actions_button": (
        'button[aria-label="More actions"]',
        'button:has-text("More")',
    ),
    "connect_button_more_menu": (
        'div[role="listbox"] button:has-text("Connect")',
        'li button:has-text("Connect")',
    ),
    "connect_add_note_button": (
        'button[aria-label="Add a note"]',
        'button:has-text("Add a note")',
    ),
    "connect_note_field": (
        'textarea[name="message"]',
        "textarea#custom-message",
    ),
    "connect_send_button": (
        'button[aria-label="Send invitation"]',
        'button[aria-label="Send now"]',
        'button:has-text("Send")',
    ),
    # --- Profile detail sections -----------------------------------------
    "profile_experience_item": (
        "#experience-section .pv-entity__summary-info",
    ),
    "profile_experience_title": (
        "h3",
    ),
    "profile_experience_company": (
        ".pv-entity__secondary-title",
    ),
    "profile_experience_duration": (
        ".pv-entity__date-range span:not(.visually-hidden)",
    ),
    "profile_education_item": (
        "#education-section .pv-education-entity",
    ),
    "profile_education_school": (
        ".pv-entity__school-name",
    ),
    "profile_education_degree": (
        ".pv-entity__degree-name .pv-entity__comma-item",
    ),
    "profile_education_field": (
        ".pv-entity__fos .pv-entity__comma-item",
    ),
    "profile_education_dates": (
        ".pv-entity__dates span:not(.visually-hidden)",
    ),
    # --- Legacy post search DOM walk -------------------------------------
    # `linkedin_browser_mcp.py` still walks the DOM with these. The scrape
    # package uses the `post_result_*` group below instead.
    "search_posts_author_links": (
        'a[href*="linkedin.com/in/"]',
    ),
    "search_posts_author_name_hidden": (
        'span[aria-hidden="true"]',
    ),
    "search_posts_feed_link": (
        'a[href*="feed/update/urn:li:"]',
    ),
    "search_posts_urn_container": (
        '[data-urn*="urn:li:activity"]',
    ),
    "search_posts_permalink": (
        'a[href*="/posts/"]',
    ),
    "search_posts_aria_elements": (
        "[aria-label]",
    ),
    "search_posts_reactions_count": (
        ".social-details-social-counts__reactions-count",
        '[class*="reactions-count"]',
        '[class*="social-counts"] span',
    ),
    "search_posts_comments_count": (
        '[class*="comments-count"]',
        '[class*="comment-count"]',
    ),
    "search_posts_scroll_main": (
        "main#workspace",
        "main",
    ),
    # --- Post search results (rebuilt, SCRAPE-01) -------------------------
    "post_result_item": (
        "div[data-chameleon-result-urn]",
        '[data-urn*="urn:li:activity"]',
        '[data-id*="urn:li:activity"]',
        "div.feed-shared-update-v2",
        ".occludable-update",
    ),
    "post_result_permalink": (
        'a[href*="/feed/update/"]',
        'a[href*="/posts/"]',
    ),
    "post_result_author_link": (
        ".update-components-actor__meta-link",
        ".update-components-actor__container-link",
        'a[href*="/in/"]',
    ),
    "post_result_author_name": (
        '.update-components-actor__title span[aria-hidden="true"]',
        '.update-components-actor__name span[aria-hidden="true"]',
        ".update-components-actor__title",
        ".update-components-actor__name",
    ),
    "post_result_author_headline": (
        '.update-components-actor__description span[aria-hidden="true"]',
        ".update-components-actor__description",
    ),
    "post_result_content": (
        '.update-components-text span[dir="ltr"]',
        ".feed-shared-inline-show-more-text",
        ".update-components-text",
        ".feed-shared-text",
    ),
    "post_result_timestamp": (
        '.update-components-actor__sub-description span[aria-hidden="true"]',
        ".update-components-actor__sub-description",
        "time",
    ),
    "post_result_reactions": (
        'span[data-test-id="social-actions__reaction-count"]',
        ".social-details-social-counts__reactions-count",
        'button[aria-label*="reaction"]',
    ),
    "post_result_comments": (
        "li.social-details-social-counts__comments button",
        ".social-details-social-counts__comments",
        'button[aria-label*="comment"]',
    ),
    "post_result_reposts": (
        'button[aria-label*="repost"]',
        ".social-details-social-counts__item--right-aligned",
    ),
    # --- Group member lists (new, SCRAPE-01) ------------------------------
    "group_members_container": (
        "div.groups-members-list",
        "section.groups-members",
        'ul[role="list"]',
    ),
    "group_member_item": (
        "li.groups-members-list__member",
        'li:has(a[href*="/in/"])',
        "li.artdeco-list__item",
    ),
    "group_member_profile_link": (
        'a[href*="/in/"]',
        ".app-aware-link",
    ),
    "group_member_name": (
        'a[href*="/in/"] span[aria-hidden="true"]',
        ".artdeco-entity-lockup__title",
        ".groups-members-list__member-name",
    ),
    "group_member_headline": (
        ".artdeco-entity-lockup__subtitle",
        ".groups-members-list__member-headline",
    ),
    "group_member_load_more": (
        "button.scaffold-finite-scroll__load-button",
        'button:has-text("Show more results")',
    ),
    # --- Generic ----------------------------------------------------------
    "generic_button": (
        "button",
    ),
}


def selector_fallbacks(name: str) -> tuple[str, ...]:
    """Return the ordered selector fallbacks for a named LinkedIn target."""
    return SELECTORS[name]


def selector_union(name: str) -> str:
    """Return a comma-joined CSS selector union for a named target."""
    return ", ".join(selector_fallbacks(name))


def selector_payload(*names: str) -> dict[str, list[str]]:
    """Return serialisable selector lists for page.evaluate calls."""
    return {name: list(selector_fallbacks(name)) for name in names}


def flatten_selector_names(names: Iterable[str]) -> list[str]:
    """Expand multiple named selector groups into a single ordered list."""
    expanded: list[str] = []
    for name in names:
        expanded.extend(selector_fallbacks(name))
    return expanded
