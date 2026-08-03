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
    # --- Follow button ----------------------------------------------------
    "follow_button": (
        'button[aria-label*="Follow"][aria-label*="Follow"]',
        'button:has-text("Follow")',
        '[data-control-name="follow"]',
    ),
    "follow_button_more_menu": (
        'div[role="listbox"] button:has-text("Follow")',
        'li button:has-text("Follow")',
        'div.artdeco-dropdown__content button:has-text("Follow")',
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
    # --- SCRAPE-03 profile detail -----------------------------------------
    # Everything the deep-scraper reads off one rendered /in/ page. The older
    # `profile_*` groups above stay exactly as they are because
    # `linkedin_browser_mcp.py` still reads them; these are the names
    # `linkedin_mcp.scrape.profile_extract` uses.
    #
    # Verification status: written from LinkedIn's published markup patterns
    # rather than from a live logged-in session. Treat the leading entry of
    # every group as a hypothesis. The fallback chains are the point: an
    # outdated first selector degrades to None rather than breaking the run.
    "profile_detail_top_card": (
        'section[data-view-name="profile-card"]',
        "section[data-member-id]",
        "div.ph5.pb5",
        ".pv-top-card",
        ".top-card-layout",
    ),
    "profile_detail_member_urn": (
        "section[data-member-id]",
        "[data-member-id]",
        "main [data-entity-urn]",
        "[data-urn]",
    ),
    "profile_detail_name": (
        'h1.text-heading-xlarge',
        "main h1",
        ".pv-top-card--list .text-heading-xlarge",
        "h1",
    ),
    "profile_detail_headline": (
        'div.text-body-medium.break-words',
        ".pv-top-card--list .text-body-medium",
        ".text-body-medium.break-words",
    ),
    "profile_detail_location": (
        'span.text-body-small.inline.t-black--light.break-words',
        ".pv-top-card--list-bullet .text-body-small",
        ".text-body-small.inline.t-black--light.break-words",
    ),
    "profile_detail_about": (
        'section:has(div#about) div.inline-show-more-text span[aria-hidden="true"]',
        "section:has(div#about) .display-flex.ph5.pv3",
        ".pv-shared-text-with-see-more .inline-show-more-text",
        ".display-flex.ph5.pv3 .visually-hidden",
    ),
    "profile_detail_avatar": (
        "img.pv-top-card-profile-picture__image--show",
        'img[class*="profile-photo-edit__preview"]',
        "main img.presence-entity__image",
        ".pv-top-card__photo img",
    ),
    "profile_detail_distance": (
        "span.dist-value",
        ".pv-top-card__distance-badge .dist-value",
        'span[class*="distance-badge"]',
        ".distance-badge",
    ),
    # One group for both counts. LinkedIn renders "500+ connections" and
    # "12,345 followers" as sibling list items with the same classes, so the
    # reader classifies them by their own text rather than by two selectors
    # that would each match the other's node.
    "profile_detail_network_stats": (
        "ul.pv-top-card--list-bullet li",
        ".pv-top-card--list-bullet .t-bold",
        ".pvs-header__optional-link span.t-bold",
        "main span.t-bold",
    ),
    "profile_detail_mutual_connections": (
        'a[href*="facetConnectionOf"]',
        'a[href*="/search/results/people/"][href*="connectionOf"]',
        ".pv-top-card--list-bullet a span",
        'span[class*="mutual"]',
    ),
    "profile_detail_premium_badge": (
        'li-icon[type="linkedin-bug"][aria-label*="Premium"]',
        'svg[data-test-icon="premium-chip-xsmall"]',
        '[data-test-icon="premium-app-xsmall"]',
        ".pv-member-badge--for-top-card",
    ),
    "profile_detail_influencer_badge": (
        'li-icon[type="linkedin-influencer-color-icon"]',
        'svg[data-test-icon="linkedin-influencer-color-small"]',
        ".pv-member-badge__influencer-icon",
    ),
    "profile_detail_openlink_badge": (
        'li-icon[type="linkedin-openlink-icon"]',
        'svg[data-test-icon="open-link-small"]',
        ".pv-member-badge--open-link",
    ),
    "profile_detail_jobseeker_badge": (
        'section:has(div#open_to) a[href*="opportunities/job-opportunities"]',
        'div[class*="open-to-work"]',
        'img[class*="profile-photo-edit__preview--open-to-work"]',
        ".pv-open-to-carousel-card--job-seeker",
    ),
    "profile_detail_hiring_badge": (
        'section:has(div#open_to) a[href*="opportunities/hiring"]',
        'div[class*="hiring-frame"]',
        'img[class*="profile-photo-edit__preview--hiring"]',
        ".pv-open-to-carousel-card--hiring",
    ),
    "profile_experience_section": (
        "section:has(div#experience)",
        'section[data-view-name="profile-card"]:has(#experience)',
        "#experience-section",
        "main section:has(#experience)",
    ),
    "profile_experience_entry": (
        "li.artdeco-list__item",
        "div.pvs-entity",
        ".pv-entity__position-group-pager",
        ".pv-entity__summary-info",
    ),
    "profile_experience_entry_title": (
        'div.t-bold span[aria-hidden="true"]',
        'span.mr1.t-bold span[aria-hidden="true"]',
        ".pv-entity__summary-info h3",
        "h3",
    ),
    "profile_experience_entry_company": (
        'span.t-14.t-normal span[aria-hidden="true"]',
        ".pv-entity__secondary-title",
        "span.t-14.t-normal",
    ),
    "profile_experience_entry_company_link": (
        'a[href*="/company/"]',
        'a[data-field="experience_company_logo"]',
    ),
    "profile_experience_entry_dates": (
        'span.t-14.t-normal.t-black--light span[aria-hidden="true"]',
        ".pv-entity__date-range span:not(.visually-hidden)",
        "span.pvs-entity__caption-wrapper",
    ),
    "profile_experience_entry_location": (
        'span.t-14.t-normal.t-black--light:nth-of-type(2) span[aria-hidden="true"]',
        ".pv-entity__location span:not(.visually-hidden)",
        'span[class*="entity__location"]',
    ),
    "profile_education_section": (
        "section:has(div#education)",
        'section[data-view-name="profile-card"]:has(#education)',
        "#education-section",
        "main section:has(#education)",
    ),
    "profile_education_entry": (
        "li.artdeco-list__item",
        "div.pvs-entity",
        ".pv-education-entity",
        ".pv-profile-section__list-item",
    ),
    "profile_education_entry_school": (
        'div.t-bold span[aria-hidden="true"]',
        ".pv-entity__school-name",
        "h3",
    ),
    "profile_education_entry_degree": (
        'span.t-14.t-normal span[aria-hidden="true"]',
        ".pv-entity__degree-name .pv-entity__comma-item",
        "span.t-14.t-normal",
    ),
    "profile_education_entry_dates": (
        'span.t-14.t-normal.t-black--light span[aria-hidden="true"]',
        ".pv-entity__dates span:not(.visually-hidden)",
        "span.pvs-entity__caption-wrapper",
    ),
    "profile_skills_section": (
        "section:has(div#skills)",
        'section[data-view-name="profile-card"]:has(#skills)',
        "#skills-section",
        "main section:has(#skills)",
    ),
    "profile_skills_entry": (
        "li.artdeco-list__item",
        "div.pvs-entity",
        ".pv-skill-category-entity",
    ),
    "profile_skills_entry_name": (
        'div.t-bold span[aria-hidden="true"]',
        ".pv-skill-category-entity__name-text",
        "span.t-bold",
    ),
    "profile_skills_entry_endorsements": (
        'span.t-14.t-normal.t-black--light span[aria-hidden="true"]',
        ".pv-skill-category-entity__endorsement-count",
        'span[class*="endorsement-count"]',
    ),
    # Contact info opens as an in-page overlay from the top card. It is reached
    # by clicking, never by loading /overlay/contact-info/ directly, because a
    # direct load spends the 40-a-day profile budget CORE-04 exists to protect.
    "profile_contact_info_trigger": (
        "#top-card-text-details-contact-info",
        'a[href*="overlay/contact-info"]',
        'a[data-control-name="contact_see_more"]',
        'button:has-text("Contact info")',
    ),
    "profile_contact_info_modal": (
        "div.pv-profile-section__section-info",
        'div[role="dialog"] .artdeco-modal__content',
        ".pv-contact-info",
        'div[role="dialog"]',
    ),
    "profile_contact_info_section": (
        "section.pv-contact-info__contact-type",
        ".pv-contact-info__contact-type",
        'div[class*="contact-info"] section',
    ),
    "profile_contact_info_section_header": (
        "h3.pv-contact-info__header",
        ".pv-contact-info__header",
        "h3",
    ),
    "profile_contact_info_section_value": (
        ".pv-contact-info__contact-link",
        ".pv-contact-info__ci-container span",
        "a",
        "span",
    ),
    "profile_contact_info_close": (
        'button[aria-label="Dismiss"]',
        "button.artdeco-modal__dismiss",
        'button[data-test-modal-close-btn]',
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
    # --- SCRAPE-04 engagers and attendees ---------------------------------
    # Post reactions live behind a modal, comments live inline under the post,
    # and every other people list here is a lazily loaded column of profile
    # links. Verification status matches the rest of this file: the leading
    # entries are hypotheses written from LinkedIn's published markup, and the
    # structural `:has(a[href*="/in/"])` fallbacks are what actually keeps a
    # run alive when a class name churns.
    "post_reactions_trigger": (
        'button[data-reaction-details]',
        "button.social-details-social-counts__count-value",
        'button[aria-label*="reactions"]',
        'button[aria-label*="reaction"]',
    ),
    "post_reactions_modal": (
        "div.social-details-reactors-modal",
        'div[role="dialog"]:has(a[href*="/in/"])',
        "div.artdeco-modal",
    ),
    "post_reactor_item": (
        "li.social-details-reactors-tab-body-list-item",
        'div[role="dialog"] li:has(a[href*="/in/"])',
        "li.artdeco-list__item",
    ),
    "post_reactor_profile_link": (
        'a.social-details-reactors-tab-body-list-item__link[href*="/in/"]',
        'a[href*="/in/"]',
        ".app-aware-link",
    ),
    "post_reactor_name": (
        'span.artdeco-entity-lockup__title span[aria-hidden="true"]',
        ".artdeco-entity-lockup__title",
        'a[href*="/in/"] span[aria-hidden="true"]',
    ),
    "post_reactor_headline": (
        "div.artdeco-entity-lockup__subtitle",
        ".artdeco-entity-lockup__subtitle",
        ".social-details-reactors-tab-body-list-item__subtitle",
    ),
    "post_reactor_distance": (
        "span.artdeco-entity-lockup__degree",
        ".artdeco-entity-lockup__degree",
        ".dist-value",
    ),
    "post_reactor_avatar": (
        'div[role="dialog"] img.EntityPhoto-circle-3',
        'div[role="dialog"] img[class*="evi-image"]',
        "img.presence-entity__image",
    ),
    "post_reactions_load_more": (
        'div[role="dialog"] button.scaffold-finite-scroll__load-button',
        'div[role="dialog"] button:has-text("Load more")',
        "button.scaffold-finite-scroll__load-button",
    ),
    "post_comment_item": (
        "article.comments-comment-entity",
        "article.comments-comment-item",
        'article:has(a[href*="/in/"])',
    ),
    "post_comment_author_link": (
        'a.comments-comment-meta__image-link[href*="/in/"]',
        'a.comments-post-meta__image-link[href*="/in/"]',
        'a[href*="/in/"]',
    ),
    "post_comment_author_name": (
        'span.comments-comment-meta__description-title',
        ".comments-post-meta__name-text",
        '.comments-comment-meta__description-title span[aria-hidden="true"]',
    ),
    "post_comment_author_headline": (
        "div.comments-comment-meta__description-subtitle",
        ".comments-post-meta__headline",
        ".comments-comment-item__postor-headline",
    ),
    "post_comment_author_avatar": (
        "article.comments-comment-entity img.EntityPhoto-circle-3",
        'article img[class*="evi-image"]',
        "img.comments-comment-meta__image",
    ),
    "post_comments_load_more": (
        "button.comments-comments-list__load-more-comments-button",
        'button:has-text("Load more comments")',
        'button:has-text("Show more comments")',
    ),
    "event_attendee_item": (
        "li.event-attendee-list__item",
        'ul[role="list"] > li:has(a[href*="/in/"])',
        "li.artdeco-list__item",
    ),
    "event_attendee_profile_link": (
        'a.event-attendee-list__attendee-link[href*="/in/"]',
        'a[href*="/in/"]',
        ".app-aware-link",
    ),
    "event_attendee_name": (
        'span.artdeco-entity-lockup__title span[aria-hidden="true"]',
        ".artdeco-entity-lockup__title",
        'a[href*="/in/"] span[aria-hidden="true"]',
    ),
    "event_attendee_headline": (
        "div.artdeco-entity-lockup__subtitle",
        ".artdeco-entity-lockup__subtitle",
        ".event-attendee-list__attendee-headline",
    ),
    "event_attendee_distance": (
        "span.artdeco-entity-lockup__degree",
        ".artdeco-entity-lockup__degree",
        ".dist-value",
    ),
    "event_attendee_load_more": (
        "button.scaffold-finite-scroll__load-button",
        'button:has-text("Show more results")',
        'button:has-text("Load more")',
    ),
    "company_employee_item": (
        "li.org-people-profile-card__profile-card-spacing",
        "div.org-people-profile-card",
        'li:has(a[href*="/in/"])',
    ),
    "company_employee_profile_link": (
        'a.org-people-profile-card__profile-title-link[href*="/in/"]',
        'a[href*="/in/"]',
        ".app-aware-link",
    ),
    "company_employee_name": (
        "div.org-people-profile-card__profile-title",
        ".artdeco-entity-lockup__title",
        'a[href*="/in/"] span[aria-hidden="true"]',
    ),
    "company_employee_headline": (
        "div.org-people-profile-card__profile-info",
        ".artdeco-entity-lockup__subtitle",
        ".lt-line-clamp--multi-line",
    ),
    "company_employee_load_more": (
        "button.scaffold-finite-scroll__load-button",
        'button:has-text("Show more results")',
        'button:has-text("Load more")',
    ),
    "connection_item": (
        "li.mn-connection-card",
        'ul[role="list"] > li:has(a[href*="/in/"])',
        "li.artdeco-list__item",
    ),
    "connection_profile_link": (
        'a.mn-connection-card__link[href*="/in/"]',
        'a[href*="/in/"]',
        ".app-aware-link",
    ),
    "connection_name": (
        "span.mn-connection-card__name",
        ".artdeco-entity-lockup__title",
        'a[href*="/in/"] span[aria-hidden="true"]',
    ),
    "connection_headline": (
        "span.mn-connection-card__occupation",
        ".artdeco-entity-lockup__subtitle",
        ".mn-connection-card__details .t-14",
    ),
    "connection_load_more": (
        "button.scaffold-finite-scroll__load-button",
        'button:has-text("Show more results")',
        'button:has-text("Load more")',
    ),
    "follower_item": (
        "li.follows-recommendation-card",
        'ul[role="list"] > li:has(a[href*="/in/"])',
        "li.artdeco-list__item",
    ),
    "follower_profile_link": (
        'a.follows-recommendation-card__link[href*="/in/"]',
        'a[href*="/in/"]',
        ".app-aware-link",
    ),
    "follower_name": (
        'span.artdeco-entity-lockup__title span[aria-hidden="true"]',
        ".artdeco-entity-lockup__title",
        'a[href*="/in/"] span[aria-hidden="true"]',
    ),
    "follower_headline": (
        "div.artdeco-entity-lockup__subtitle",
        ".artdeco-entity-lockup__subtitle",
        ".follows-recommendation-card__headline",
    ),
    "follower_load_more": (
        "button.scaffold-finite-scroll__load-button",
        'button:has-text("Show more results")',
        'button:has-text("Load more")',
    ),
    # --- SEQ-03 inbox and threads -----------------------------------------
    # LinkedIn's messaging surface is two panes on one route: a lazily loaded
    # conversation list on the left and the open conversation on the right.
    # Clicking a list row swaps the right pane without a navigation, which is
    # why the scanner reads both from the same page object.
    #
    # Verification status matches the rest of this file. None of these has been
    # checked against a live logged-in session, so every group leads with an
    # attribute or role hook and falls back to structure. A missing optional
    # field reads as None rather than raising, and a thread whose messages
    # cannot be read is reported as unreadable rather than guessed at.
    "inbox_thread_list": (
        "ul.msg-conversations-container__conversations-list",
        'div[data-view-name="messaging-conversation-list"]',
        "aside.msg-conversations-container",
    ),
    "inbox_thread_item": (
        "li.msg-conversation-listitem",
        'li[data-view-name="messaging-conversation-list-item"]',
        "li.msg-conversations-container__convo-item",
        'li:has(a[href*="/messaging/thread/"])',
    ),
    "inbox_thread_link": (
        'a.msg-conversation-listitem__link[href*="/messaging/thread/"]',
        'a[href*="/messaging/thread/"]',
        "a.msg-conversation-listitem__link",
    ),
    "inbox_thread_participant_link": (
        'a[href*="/in/"]',
        "a.msg-conversation-listitem__link",
        ".app-aware-link",
    ),
    "inbox_thread_participant_name": (
        "h3.msg-conversation-listitem__participant-names",
        ".msg-conversation-listitem__participant-names",
        'span.truncate[aria-hidden="true"]',
    ),
    "inbox_thread_preview": (
        "p.msg-conversation-card__message-snippet",
        ".msg-conversation-card__message-snippet-body",
        ".msg-conversation-listitem__message-snippet",
    ),
    "inbox_thread_timestamp": (
        "time.msg-conversation-listitem__time-stamp",
        ".msg-conversation-listitem__time-stamp",
        "time",
    ),
    "inbox_thread_unread_badge": (
        "span.msg-conversation-card__unread-count",
        ".notification-badge--show",
        'li[aria-label*="unread"]',
    ),
    "inbox_thread_load_more": (
        "button.msg-conversations-container__see-more-btn",
        "button.scaffold-finite-scroll__load-button",
        'button:has-text("Load more conversations")',
    ),
    "inbox_message_list": (
        "ul.msg-s-message-list-content",
        'div[data-view-name="messaging-thread"]',
        ".msg-s-message-list",
    ),
    "inbox_message_item": (
        "li.msg-s-message-list__event",
        'li[data-view-name="messaging-message"]',
        "div.msg-s-event-listitem",
        "li.msg-s-event-listitem",
    ),
    "inbox_message_sender_link": (
        'a.msg-s-event-listitem__link[href*="/in/"]',
        'a[href*="/in/"]',
        ".msg-s-event-listitem__profile-picture-link",
    ),
    "inbox_message_sender_name": (
        "span.msg-s-message-group__name",
        ".msg-s-message-group__profile-link span",
        ".msg-s-event-listitem__name",
    ),
    "inbox_message_body": (
        "div.msg-s-event-listitem__body",
        "p.msg-s-event-listitem__body",
        ".msg-s-event__content .break-words",
    ),
    "inbox_message_timestamp": (
        "time.msg-s-message-group__timestamp",
        "time.msg-s-message-list__time-heading",
        "time",
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
