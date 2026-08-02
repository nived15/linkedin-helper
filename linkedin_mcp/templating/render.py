"""Rendering a template into a message, or refusing to.

The contract is deliberately narrow. `render_template` either returns a
`RenderedMessage` that is safe to send, or raises `RenderRefusal`. There is no
third outcome and no partially rendered string. A caller that would rather
branch than catch uses `safe_render_template`, which returns a `RenderResult`
carrying one or the other.

Why refusal rather than a best effort: a template that renders "Hi ," because
`first_name` was never scraped is worse than sending nothing. So an unguarded
token with no value stops the render. The template author's escape hatch is
`{IF firstName}Hi {firstName},{ELSE}Hi there,{END}`, which is exactly what the
IF/THEN/ELSE construct is for.

A variable counts as absent when its key is missing, its value is None, or it
strips to nothing. Whitespace-only is the case that bites, because a scrape that
returns " " looks present to every truthiness check and renders a broken
message.

**The SEQ-05 seam.** `{ai_*}` tokens are filled from `fragments`, an injected
mapping or callable this module never populates. SEQ-05 (#23) owns the
`ai_drafts` table and the generation flow; nothing here calls an LLM or touches
that table. When a fragment is absent the render refuses with
`MISSING_AI_FRAGMENT`, which `RenderRefusal.is_awaiting_ai` marks as "the draft
has not arrived", distinct from "this lead is unusable".
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from linkedin_mcp.leads.store import Lead
from linkedin_mcp.templating.errors import (
    RenderRefusal,
    RenderRefusalReason,
    TemplateSyntaxError,
)
from linkedin_mcp.templating.parser import (
    ConditionalNode,
    Node,
    Program,
    SpintaxNode,
    TextNode,
    VariableNode,
    parse_template,
)
from linkedin_mcp.templating.store import Template, inline_template
from linkedin_mcp.templating.style import (
    DEFAULT_STYLE,
    StylePolicy,
    broken_punctuation,
    contains_forbidden_dash,
    normalise_dashes,
    style_violations,
    tidy_whitespace,
)
from linkedin_mcp.templating.tokens import (
    coerce_value,
    fragment_name,
    is_ai_token,
    lead_context,
    normalise_token,
)
from linkedin_mcp.templating.variations import spintax_index, variation_index


__all__ = [
    "FragmentSource",
    "RenderedMessage",
    "RenderResult",
    "normalise_values",
    "preview_template",
    "render_for_lead",
    "render_template",
    "safe_render_for_lead",
    "safe_render_template",
]

FragmentSource = Callable[[str], str | None]
"""What SEQ-05 must supply: a lookup from bare fragment name to generated text.

A plain mapping works too, keyed by either the bare name (`opener`) or the full
token (`ai_opener`).
"""


@dataclass(frozen=True, slots=True)
class RenderedMessage:
    """A message that passed every check and is safe to send."""

    text: str
    sequence: int
    variation_index: int
    template_name: str | None = None
    template_id: int | None = None
    lead_id: int | None = None
    tokens_used: tuple[str, ...] = ()
    fragments_used: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_result(self) -> dict[str, Any]:
        """Return an MCP-shaped result for this message."""
        return {
            "status": "success",
            "text": self.text,
            "sequence": self.sequence,
            "variation_index": self.variation_index,
            "template_name": self.template_name,
            "template_id": self.template_id,
            "lead_id": self.lead_id,
            "tokens_used": list(self.tokens_used),
            "fragments_used": list(self.fragments_used),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class RenderResult:
    """Either a rendered message or a typed refusal. Never both, never neither."""

    rendered: RenderedMessage | None = None
    refusal: RenderRefusal | None = None

    def __post_init__(self) -> None:
        if (self.rendered is None) == (self.refusal is None):
            raise ValueError("a RenderResult carries exactly one of rendered, refusal")

    @property
    def ok(self) -> bool:
        return self.rendered is not None

    @property
    def text(self) -> str | None:
        return None if self.rendered is None else self.rendered.text

    @property
    def sublist(self) -> str | None:
        """The `campaign_leads` sub-list to move the lead to, or None if it sent.

        Always `skipped` on refusal. SEQ-01 (#19) owns the state machine that
        performs the move; this module only names the destination.
        """
        return None if self.refusal is None else self.refusal.sublist

    def to_result(self) -> dict[str, Any]:
        if self.refusal is not None:
            return self.refusal.to_result()
        assert self.rendered is not None
        return self.rendered.to_result()


def normalise_values(values: Mapping[str, Any]) -> dict[str, str]:
    """Key a caller's context by token name and render every value as text.

    Values are stripped and dash-normalised here. Lead data is never refused for
    a style violation, because a company whose registered name contains an em
    dash should still be messageable.
    """
    return {
        normalise_token(key): normalise_dashes(coerce_value(value))
        for key, value in values.items()
    }


def _fragment_lookup(
    fragments: Mapping[str, Any] | FragmentSource | None,
) -> FragmentSource:
    if fragments is None:
        return lambda name: None
    if callable(fragments):
        return fragments

    keyed = {fragment_name(str(key)): value for key, value in fragments.items()}
    return lambda name: keyed.get(name)


class _Renderer:
    """One render pass. Holds the state a single message needs and nothing else."""

    def __init__(
        self,
        values: Mapping[str, str],
        *,
        fragments: FragmentSource,
        policy: StylePolicy,
        context: dict[str, Any],
    ) -> None:
        self.values = values
        self.fragments = fragments
        self.policy = policy
        self.context = context
        self.tokens_used: set[str] = set()
        self.fragments_used: set[str] = set()
        self._fragment_cache: dict[str, str] = {}
        self._fragment_checked: set[str] = set()

    def refuse(
        self,
        reason: RenderRefusalReason,
        message: str,
        **detail: Any,
    ) -> RenderRefusal:
        payload = dict(self.context)
        payload.update(detail)
        return RenderRefusal(reason, message, **payload)

    def resolve_fragment(self, token: str) -> str:
        """Call the injected fragment source, turning its failures into refusals.

        SEQ-05 (#23) will supply this callable, and a draft store that is down or
        raising is not a reason for a campaign tick to crash. The lead is fine,
        so the refusal is marked as awaiting AI and can be retried.
        """
        name = fragment_name(token)
        if name not in self._fragment_cache:
            try:
                value = self.fragments(name)
            except Exception as error:
                raise self.refuse(
                    RenderRefusalReason.FRAGMENT_SOURCE_FAILED,
                    (
                        f"the AI fragment source raised {type(error).__name__} "
                        f"while resolving {{{token}}}: {error}"
                    ),
                    token=token,
                    fragment=name,
                    error=f"{type(error).__name__}: {error}",
                ) from error
            self._fragment_cache[name] = coerce_value(value)
        return self._fragment_cache[name]

    def resolve(self, token: str) -> str:
        """Return a token's value, empty string meaning absent."""
        if is_ai_token(token):
            return self.resolve_fragment(token)
        return self.values.get(token, "")

    def present(self, token: str) -> bool:
        return bool(self.resolve(token))

    def render_variable(self, token: str) -> str:
        value = self.resolve(token)
        if not value:
            if is_ai_token(token):
                raise self.refuse(
                    RenderRefusalReason.MISSING_AI_FRAGMENT,
                    (
                        f"AI fragment {{{token}}} has not been generated yet, so the "
                        "message cannot be rendered"
                    ),
                    token=token,
                    fragment=fragment_name(token),
                )
            raise self.refuse(
                RenderRefusalReason.MISSING_VARIABLE,
                (
                    f"{{{token}}} has no value for this lead and the template does "
                    f"not guard it; wrap it in '{{IF {token}}}...{{ELSE}}...{{END}}' "
                    "or the message would render with a hole in it"
                ),
                token=token,
            )

        if is_ai_token(token):
            self.check_fragment_style(token, value)
            self.fragments_used.add(fragment_name(token))
        else:
            self.tokens_used.add(token)
        return value

    def check_fragment_style(self, token: str, value: str) -> None:
        """Hold AI fragments to the same writing style rules as a human author.

        Generated text is the likeliest source of an em dash in the whole system,
        so it is refused rather than repaired. SEQ-05 (#23) should treat this as
        a signal to regenerate the draft.
        """
        name = fragment_name(token)
        if name in self._fragment_checked:
            return
        self._fragment_checked.add(name)
        violations = style_violations(value, self.policy)
        if violations:
            raise self.refuse(
                RenderRefusalReason.STYLE_VIOLATION,
                f"AI fragment {{{token}}} breaks a writing style rule: {violations[0].message}",
                token=token,
                fragment=name,
                violation=violations[0].kind,
            )

    def render(self, nodes: Sequence[Node], sequence: int) -> str:
        parts: list[str] = []
        for node in nodes:
            if isinstance(node, TextNode):
                parts.append(node.value)
            elif isinstance(node, VariableNode):
                parts.append(self.render_variable(node.name))
            elif isinstance(node, ConditionalNode):
                self.tokens_used.add(node.name)
                branch = node.then_nodes if self.present(node.name) else node.else_nodes
                parts.append(self.render(branch, sequence))
            elif isinstance(node, SpintaxNode):
                width = len(node.alternatives)
                chosen = spintax_index(sequence, node.ordinal, width)
                parts.append(self.render(node.alternatives[chosen], sequence // width))
            else:  # pragma: no cover - the node union is closed
                raise TypeError(f"unknown template node {node!r}")
        return "".join(parts)


def _as_template(template: Template | str) -> Template:
    if isinstance(template, Template):
        return template
    return inline_template(template)


def render_template(
    template: Template | str,
    values: Mapping[str, Any] | None = None,
    *,
    sequence: int = 0,
    fragments: Mapping[str, Any] | FragmentSource | None = None,
    policy: StylePolicy = DEFAULT_STYLE,
    max_chars: int | None = None,
    lead_id: int | None = None,
) -> RenderedMessage:
    """Render one message, or raise `RenderRefusal`.

    `sequence` is the lead's zero-based position in the campaign queue. It
    chooses the whole-message variation and every spintax alternative, so the
    same lead at the same position always produces the same message.
    """
    resolved = _as_template(template)
    bodies = resolved.bodies()
    index = variation_index(sequence, len(bodies))
    context: dict[str, Any] = {
        "template_name": resolved.name,
        "template_id": resolved.id,
        "lead_id": lead_id,
        "sequence": sequence,
        "variation_index": index,
    }

    try:
        program: Program = parse_template(bodies[index])
    except TemplateSyntaxError as error:
        raise RenderRefusal(
            RenderRefusalReason.TEMPLATE_INVALID,
            f"template cannot be parsed: {error}",
            **context,
        ) from error

    renderer = _Renderer(
        normalise_values(values or {}),
        fragments=_fragment_lookup(fragments),
        policy=policy,
        context=context,
    )
    text = tidy_whitespace(renderer.render(program.nodes, sequence // len(bodies)))

    if not text:
        raise renderer.refuse(
            RenderRefusalReason.EMPTY_MESSAGE,
            "template rendered to an empty message",
        )

    dash = contains_forbidden_dash(text, policy)
    if dash is not None:
        raise renderer.refuse(
            RenderRefusalReason.STYLE_VIOLATION,
            (
                f"rendered message contains the forbidden dash {dash!r}; "
                "the writing style rules ban it outright"
            ),
            violation="forbidden_dash",
        )

    artefact = broken_punctuation(text)
    if artefact is not None:
        raise renderer.refuse(
            RenderRefusalReason.BROKEN_PUNCTUATION,
            (
                f"rendered message has dangling punctuation near {artefact!r}, "
                "which means a slot rendered empty"
            ),
            artefact=artefact,
        )

    warnings: list[str] = []
    for violation in style_violations(text, policy, check_dashes=False):
        if violation.kind == "long_sentence":
            warnings.append(violation.message)
            continue
        raise renderer.refuse(
            RenderRefusalReason.STYLE_VIOLATION,
            f"rendered message breaks a writing style rule: {violation.message}",
            violation=violation.kind,
        )

    if max_chars is not None and len(text) > max_chars:
        raise renderer.refuse(
            RenderRefusalReason.TOO_LONG,
            f"rendered message is {len(text)} characters, over the {max_chars} limit",
            length=len(text),
            max_chars=max_chars,
        )

    return RenderedMessage(
        text=text,
        sequence=sequence,
        variation_index=index,
        template_name=resolved.name,
        template_id=resolved.id,
        lead_id=lead_id,
        tokens_used=tuple(sorted(renderer.tokens_used)),
        fragments_used=tuple(sorted(renderer.fragments_used)),
        warnings=tuple(warnings),
    )


def safe_render_template(
    template: Template | str,
    values: Mapping[str, Any] | None = None,
    **kwargs: Any,
) -> RenderResult:
    """Render without raising. Check `RenderResult.ok` before sending anything."""
    try:
        return RenderResult(rendered=render_template(template, values, **kwargs))
    except RenderRefusal as refusal:
        return RenderResult(refusal=refusal)


def render_for_lead(
    conn: sqlite3.Connection,
    template: Template | str,
    lead: Lead | int,
    *,
    sequence: int = 0,
    fragments: Mapping[str, Any] | FragmentSource | None = None,
    extras: Mapping[str, Any] | None = None,
    policy: StylePolicy = DEFAULT_STYLE,
    max_chars: int | None = None,
) -> RenderedMessage:
    """Render for one stored lead, pulling its columns and `{cs_*}` fields."""
    values = lead_context(conn, lead, extras=extras)
    lead_id = lead.id if isinstance(lead, Lead) else lead
    return render_template(
        template,
        values,
        sequence=sequence,
        fragments=fragments,
        policy=policy,
        max_chars=max_chars,
        lead_id=lead_id,
    )


def safe_render_for_lead(
    conn: sqlite3.Connection,
    template: Template | str,
    lead: Lead | int,
    *,
    extras: Mapping[str, Any] | None = None,
    **kwargs: Any,
) -> RenderResult:
    """Render for one stored lead without raising.

    A lead id that does not resolve is a refusal like any other, so a deleted
    lead cannot crash a campaign tick. Only the lead lookup sits inside that
    catch: a `LookupError` raised later, by an injected fragment source for
    instance, must not be mislabelled as a missing lead.
    """
    lead_id = lead.id if isinstance(lead, Lead) else lead
    try:
        values = lead_context(conn, lead, extras=extras)
    except LookupError as error:
        return RenderResult(
            refusal=RenderRefusal(
                RenderRefusalReason.LEAD_NOT_FOUND,
                str(error),
                lead_id=lead_id,
            )
        )
    return safe_render_template(template, values, lead_id=lead_id, **kwargs)


def preview_template(
    template: Template | str,
    contexts: Iterable[Mapping[str, Any]],
    *,
    start: int = 0,
    fragments: Mapping[str, Any] | FragmentSource | None = None,
    policy: StylePolicy = DEFAULT_STYLE,
    max_chars: int | None = None,
) -> list[RenderResult]:
    """Render a run of messages in queue order, refusals included.

    This is what a campaign preview shows a human before the first invite goes
    out: the real variation split, the real spins, and the leads that would be
    skipped.
    """
    return [
        safe_render_template(
            template,
            values,
            sequence=start + offset,
            fragments=fragments,
            policy=policy,
            max_chars=max_chars,
        )
        for offset, values in enumerate(contexts)
    ]
