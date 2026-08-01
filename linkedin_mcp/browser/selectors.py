"""Centralised LinkedIn selector registry with ordered fallbacks."""

from __future__ import annotations

from typing import Iterable

SELECTORS: dict[str, tuple[str, ...]] = {
    "login_username": (
        "#username",
        'input[name="session_key"]',
    ),
    "login_password": (
        "#password",
        'input[name="session_password"]',
    ),
    "profile_top_card": (
        ".pv-top-card",
        ".top-card-layout",
    ),
    "profile_name": (
        ".pv-top-card--list .text-heading-xlarge",
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
    "search_result_container": (
        ".reusable-search__result-container",
        ".search-results-container .entity-result",
    ),
    "search_result_title_link": (
        ".entity-result__title-text a",
        ".app-aware-link span[aria-hidden=\"true\"]",
    ),
    "search_result_headline": (
        ".entity-result__primary-subtitle",
    ),
    "search_result_location": (
        ".entity-result__secondary-subtitle",
    ),
    "search_result_profile_link": (
        ".app-aware-link",
    ),
    "search_result_distance": (
        ".dist-value",
    ),
    "search_result_snippet": (
        ".entity-result__summary",
    ),
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
    "post_detail_container": (
        ".feed-shared-update-v2",
        ".update-components-update-v2",
        ".occludable-update",
        '[data-urn*="urn:li:activity"]',
    ),
    "post_detail_author": (
        ".feed-shared-actor__name",
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
        'button[aria-label*="omment"]',
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
