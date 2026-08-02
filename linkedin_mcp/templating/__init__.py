"""SEQ-02 dynamic messaging template engine.

Turns a stored template plus one lead's data into a message that is safe to
send, or refuses and says why. Five features, in the order they matter:

1. **Variable insertion.** `{firstName}`, `{company}`, `{position}`,
   `{mutualTotal}` and `{cs_*}` custom fields from DB-02. The public token to
   database column mapping lives in one reviewable table in
   :mod:`linkedin_mcp.templating.tokens`.
2. **IF/THEN/ELSE on variable presence.** `{IF firstName}Hi {firstName},{ELSE}Hi
   there,{END}`. Presence only, never values, and it changes text, never
   routing. An unguarded token with no value refuses the render instead of
   producing "Hi ,".
3. **Spintax.** `{a|b|c}`, nested, with alternatives chosen deterministically
   from the message's queue position rather than at random.
4. **Whole-message variations**, split evenly across the queue by round robin.
   100 messages over 3 variations is 34/33/33 every time, not on average.
5. **Hybrid templates.** A static skeleton plus `{ai_*}` fragments injected by
   the caller. This package defines that seam and never calls an LLM.

What it deliberately does not do:

* It does not import :mod:`linkedin_mcp.sequences`. SEQ-01 (#19) owns the
  campaign state machine. A refusal names its destination sub-list as the string
  ``"skipped"`` and lets #19 perform the move.
* It does not generate AI text or touch the `ai_drafts` table. SEQ-05 (#23) owns
  that. It must supply a value for every name in `Program.fragments()`, keyed by
  the bare fragment name or the full `ai_` token.
* It does not register MCP tools. MCP-03 (#26) owns that.

Typical use::

    from linkedin_mcp.templating import create_template, safe_render_for_lead

    template = create_template(
        conn, account_id, "invite",
        "{IF firstName}Hi {firstName},{ELSE}Hi there,{END} "
        "{saw your work at|noticed what you are building at} {company}.",
        variations=[
            "{IF firstName}Hi {firstName},{ELSE}Hi there,{END} "
            "your team at {company} keeps coming up in my conversations."
        ],
    )
    result = safe_render_for_lead(conn, template, lead_id, sequence=queue_position)
    if result.ok:
        send(result.rendered.text)
    else:
        move_lead_to(result.sublist)  # always "skipped"
"""

from linkedin_mcp.templating.errors import (
    AWAITING_AI_REASONS,
    RenderRefusal,
    RenderRefusalReason,
    SKIPPED_SUBLIST,
    TemplateError,
    TemplateNotFoundError,
    TemplateStyleError,
    TemplateSyntaxError,
)
from linkedin_mcp.templating.parser import (
    AI_KINDS,
    MAX_NESTING_DEPTH,
    MAX_STYLE_SAMPLES,
    TEMPLATE_KINDS,
    ConditionalNode,
    Node,
    Program,
    SpintaxNode,
    TextNode,
    VariableNode,
    compile_bodies,
    parse_template,
    style_samples,
    validate_kind,
    validate_style,
    validate_template,
)
from linkedin_mcp.templating.render import (
    FragmentSource,
    RenderedMessage,
    RenderResult,
    normalise_values,
    preview_template,
    render_for_lead,
    render_template,
    safe_render_for_lead,
    safe_render_template,
)
from linkedin_mcp.templating.store import (
    TEMPLATE_COLUMNS,
    Template,
    compile_template,
    count_templates,
    create_template,
    delete_template,
    get_template,
    get_template_by_name,
    inline_template,
    list_templates,
    require_template,
    template_from_row,
    update_template,
)
from linkedin_mcp.templating.style import (
    DEFAULT_STYLE,
    FILLER_OPENERS,
    FORBIDDEN_DASHES,
    StylePolicy,
    StyleViolation,
    broken_punctuation,
    contains_forbidden_dash,
    normalise_dashes,
    sentences,
    style_violations,
    tidy_whitespace,
)
from linkedin_mcp.templating.tokens import (
    AI_FRAGMENT_PREFIX,
    CUSTOM_FIELD_PREFIX,
    LEAD_TOKEN_COLUMNS,
    MUTUAL_TOTAL_CUSTOM_FIELD,
    MUTUAL_TOTAL_TOKEN,
    RESERVED_KEYWORDS,
    fragment_name,
    is_ai_token,
    is_custom_field_token,
    is_known_token,
    known_token_names,
    lead_context,
    lead_tokens,
    normalise_token,
)
from linkedin_mcp.templating.variations import (
    assign_variations,
    spintax_index,
    variation_distribution,
    variation_index,
    variation_plan,
)


__all__ = [
    "AI_FRAGMENT_PREFIX",
    "AI_KINDS",
    "AWAITING_AI_REASONS",
    "CUSTOM_FIELD_PREFIX",
    "DEFAULT_STYLE",
    "FILLER_OPENERS",
    "FORBIDDEN_DASHES",
    "LEAD_TOKEN_COLUMNS",
    "MAX_NESTING_DEPTH",
    "MAX_STYLE_SAMPLES",
    "MUTUAL_TOTAL_CUSTOM_FIELD",
    "MUTUAL_TOTAL_TOKEN",
    "RESERVED_KEYWORDS",
    "SKIPPED_SUBLIST",
    "TEMPLATE_COLUMNS",
    "TEMPLATE_KINDS",
    "ConditionalNode",
    "FragmentSource",
    "Node",
    "Program",
    "RenderRefusal",
    "RenderRefusalReason",
    "RenderResult",
    "RenderedMessage",
    "SpintaxNode",
    "StylePolicy",
    "StyleViolation",
    "Template",
    "TemplateError",
    "TemplateNotFoundError",
    "TemplateStyleError",
    "TemplateSyntaxError",
    "TextNode",
    "VariableNode",
    "assign_variations",
    "broken_punctuation",
    "compile_bodies",
    "compile_template",
    "contains_forbidden_dash",
    "count_templates",
    "create_template",
    "delete_template",
    "fragment_name",
    "get_template",
    "get_template_by_name",
    "inline_template",
    "is_ai_token",
    "is_custom_field_token",
    "is_known_token",
    "known_token_names",
    "lead_context",
    "lead_tokens",
    "list_templates",
    "normalise_dashes",
    "normalise_token",
    "normalise_values",
    "parse_template",
    "preview_template",
    "render_for_lead",
    "render_template",
    "require_template",
    "safe_render_for_lead",
    "safe_render_template",
    "sentences",
    "spintax_index",
    "style_samples",
    "style_violations",
    "template_from_row",
    "tidy_whitespace",
    "update_template",
    "validate_kind",
    "validate_style",
    "validate_template",
    "variation_distribution",
    "variation_index",
    "variation_plan",
]
