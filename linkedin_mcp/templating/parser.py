"""Template syntax: lexing, parsing and authoring-time validation.

The grammar is small enough to state in full.

* ``text`` is anything outside braces.
* ``{firstName}`` is a **variable**. The content is a bare identifier.
* ``{a|b|c}`` is **spintax**. The content contains at least one top-level ``|``.
  Alternatives are parsed recursively, so ``{Hi {firstName}|Hey {firstName}}``
  works and so does nesting spintax inside spintax.
* ``{IF firstName}then{ELSE}otherwise{END}`` is a **conditional**. It tests
  presence only, never a value, and it changes text, never routing. ``{ELSE}``
  is optional. Conditionals nest.
* ``\\{``, ``\\}``, ``\\|`` and ``\\\\`` are escapes for a literal brace, pipe or
  backslash.

Two ambiguities are resolved here on purpose, because leaving either to chance
is how a broken message gets sent.

**``{a}`` is a variable, never a single-alternative spintax.** A one-alternative
spin has no effect, so reading it as a variable is the only reading that can
ever be useful. The consequence is that ``{a}`` is an unknown token and is
rejected at authoring time rather than silently rendering the letter "a".

**Escapes are backslashes, not doubled braces.** With ``{{`` as the escape,
``{{firstName}|there}`` would lex as a literal ``{`` and the author could not
start a spintax alternative with a variable. With backslashes that template
means exactly what it looks like.

Anything else inside braces is a syntax error. There is no "pass it through as
literal text" fallback, because that is precisely how ``{firstNam}`` ends up in
a sent message.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType

from linkedin_mcp.templating.errors import TemplateStyleError, TemplateSyntaxError
from linkedin_mcp.templating.style import (
    DEFAULT_STYLE,
    StylePolicy,
    StyleViolation,
    broken_punctuation,
    contains_forbidden_dash,
    style_violations,
    tidy_whitespace,
)
from linkedin_mcp.templating.tokens import (
    AI_FRAGMENT_PREFIX,
    RESERVED_KEYWORDS,
    fragment_name,
    is_ai_token,
    is_identifier,
    is_known_token,
    known_token_names,
    normalise_token,
)


__all__ = [
    "AI_KINDS",
    "ESCAPABLE",
    "MAX_NESTING_DEPTH",
    "MAX_STYLE_SAMPLES",
    "SAMPLE_VALUE",
    "TEMPLATE_KINDS",
    "ConditionalNode",
    "Node",
    "Program",
    "SpintaxNode",
    "TextNode",
    "VariableNode",
    "compile_bodies",
    "parse_template",
    "style_samples",
    "validate_kind",
    "validate_style",
    "validate_template",
]

TEMPLATE_KINDS: tuple[str, ...] = ("static", "ai", "hybrid")
"""Mirrors the CHECK constraint on `templates.kind` in `0001_init.sql`."""

AI_KINDS: frozenset[str] = frozenset({"ai", "hybrid"})

ESCAPABLE: frozenset[str] = frozenset({"{", "}", "\\", "|"})

MAX_NESTING_DEPTH = 50
"""How deeply conditionals and spintax may nest before the template is refused.

Parsing and rendering both walk the tree recursively, so without a limit a body
like ``"{a|" * 600 + "x" + "}" * 600`` raises `RecursionError` out of
`safe_render_template`, which is exactly the unhandled crash that function
exists to prevent. Fifty levels is far past anything a human writes.
"""

MAX_STYLE_SAMPLES = 64
"""Cap on the strings `style_samples` produces for authoring checks.

Sample count grows with the number of branch points, and each sample costs a
full tree walk, so an adversarial body of a thousand conditionals would make
validation quadratic. Real templates never come close to the cap.
"""

SAMPLE_VALUE = "Sample"
"""Stand-in for a variable when style-checking a template before any lead exists."""


@dataclass(frozen=True, slots=True)
class TextNode:
    value: str


@dataclass(frozen=True, slots=True)
class VariableNode:
    name: str


@dataclass(frozen=True, slots=True)
class SpintaxNode:
    alternatives: tuple[tuple["Node", ...], ...]
    ordinal: int


@dataclass(frozen=True, slots=True)
class ConditionalNode:
    name: str
    then_nodes: tuple["Node", ...]
    else_nodes: tuple["Node", ...]
    ordinal: int


Node = TextNode | VariableNode | SpintaxNode | ConditionalNode


@dataclass(frozen=True, slots=True)
class _Text:
    value: str


@dataclass(frozen=True, slots=True)
class _Group:
    raw: str


@dataclass(slots=True)
class _Counters:
    spintax: int = 0
    conditional: int = 0
    spintax_sizes: dict[int, int] = field(default_factory=dict)

    def next_spintax(self) -> int:
        ordinal = self.spintax
        self.spintax += 1
        return ordinal

    def next_conditional(self) -> int:
        ordinal = self.conditional
        self.conditional += 1
        return ordinal


@dataclass(frozen=True, slots=True)
class Program:
    """A parsed template body, ready to render.

    `spintax_sizes` maps each spintax node's ordinal to its number of
    alternatives. The renderer uses the ordinal to pick an alternative
    deterministically from the message sequence number, so the same lead in the
    same queue position always gets the same message.
    """

    source: str
    nodes: tuple[Node, ...]
    spintax_count: int
    conditional_count: int
    spintax_sizes: Mapping[int, int]

    def tokens(self) -> tuple[str, ...]:
        """Every token this body references, including inside IF conditions."""
        found: list[str] = []
        _collect_tokens(self.nodes, found)
        return tuple(sorted(set(found)))

    def variables(self) -> tuple[str, ...]:
        """Tokens resolved from lead data. Excludes the `{ai_*}` namespace."""
        return tuple(name for name in self.tokens() if not is_ai_token(name))

    def ai_tokens(self) -> tuple[str, ...]:
        """The `{ai_*}` tokens, as written."""
        return tuple(name for name in self.tokens() if is_ai_token(name))

    def fragments(self) -> tuple[str, ...]:
        """The AI fragment names SEQ-05 must supply, without the `ai_` prefix."""
        return tuple(sorted({fragment_name(name) for name in self.ai_tokens()}))


def _collect_tokens(nodes: Sequence[Node], found: list[str]) -> None:
    for node in nodes:
        if isinstance(node, VariableNode):
            found.append(node.name)
        elif isinstance(node, ConditionalNode):
            found.append(node.name)
            _collect_tokens(node.then_nodes, found)
            _collect_tokens(node.else_nodes, found)
        elif isinstance(node, SpintaxNode):
            for alternative in node.alternatives:
                _collect_tokens(alternative, found)


def _scan_group(source: str, start: int) -> tuple[str, int]:
    """Return the raw content of the group opening at `start`, and the index after it."""
    depth = 1
    index = start + 1
    inner: list[str] = []
    length = len(source)

    while index < length:
        char = source[index]
        if char == "\\":
            if index + 1 >= length:
                raise TemplateSyntaxError(
                    "template ends with a dangling backslash; write '\\\\' for a literal one"
                )
            inner.append(char)
            inner.append(source[index + 1])
            index += 2
            continue
        if char == "{":
            depth += 1
            inner.append(char)
            index += 1
            continue
        if char == "}":
            depth -= 1
            if depth == 0:
                return "".join(inner), index + 1
            inner.append(char)
            index += 1
            continue
        inner.append(char)
        index += 1

    snippet = source[start : start + 40]
    raise TemplateSyntaxError(
        f"unclosed '{{' in template near {snippet!r}; write '\\{{' for a literal brace"
    )


def _lex(source: str) -> list[_Text | _Group]:
    tokens: list[_Text | _Group] = []
    buffer: list[str] = []
    index = 0
    length = len(source)

    while index < length:
        char = source[index]
        if char == "\\":
            if index + 1 >= length:
                raise TemplateSyntaxError(
                    "template ends with a dangling backslash; write '\\\\' for a literal one"
                )
            escaped = source[index + 1]
            if escaped not in ESCAPABLE:
                raise TemplateSyntaxError(
                    f"unknown escape '\\{escaped}'; only '\\{{', '\\}}', '\\|' and "
                    "'\\\\' are escapes"
                )
            buffer.append(escaped)
            index += 2
            continue
        if char == "{":
            raw, index = _scan_group(source, index)
            if buffer:
                tokens.append(_Text("".join(buffer)))
                buffer = []
            tokens.append(_Group(raw))
            continue
        if char == "}":
            snippet = source[max(0, index - 20) : index + 20]
            raise TemplateSyntaxError(
                f"unmatched '}}' in template near {snippet!r}; "
                "write '\\}' for a literal brace"
            )
        buffer.append(char)
        index += 1

    if buffer:
        tokens.append(_Text("".join(buffer)))
    return tokens


def _split_alternatives(raw: str) -> list[str]:
    """Split spintax content on top-level pipes, ignoring nested and escaped ones."""
    parts: list[str] = []
    buffer: list[str] = []
    depth = 0
    index = 0
    length = len(raw)

    while index < length:
        char = raw[index]
        if char == "\\" and index + 1 < length:
            buffer.append(char)
            buffer.append(raw[index + 1])
            index += 2
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        elif char == "|" and depth == 0:
            parts.append("".join(buffer))
            buffer = []
            index += 1
            continue
        buffer.append(char)
        index += 1

    parts.append("".join(buffer))
    return parts


def _keyword(raw: str) -> str:
    head = raw.strip()
    if not head:
        return ""
    return head.split(None, 1)[0].upper()


def _require_known_token(name: str, *, context: str) -> str:
    if not is_identifier(name):
        raise TemplateSyntaxError(
            f"{context} {name!r} is not a valid token name; "
            "use letters, digits and underscores, starting with a letter"
        )
    if name.upper() in RESERVED_KEYWORDS:
        raise TemplateSyntaxError(
            f"{name!r} is a reserved keyword and cannot be used as a token name"
        )
    if not is_known_token(name):
        raise TemplateSyntaxError(
            f"{context} {name!r} is not a known token; expected one of "
            f"{', '.join(known_token_names())}, a custom field like "
            f"{{cs_industry}}, or an AI fragment like {{{AI_FRAGMENT_PREFIX}opener}}"
        )
    return normalise_token(name)


def _parse_sequence(
    tokens: Sequence[_Text | _Group],
    index: int,
    stoppers: frozenset[str],
    counters: _Counters,
    depth: int = 0,
) -> tuple[tuple[Node, ...], int, str | None]:
    if depth > MAX_NESTING_DEPTH:
        raise TemplateSyntaxError(
            f"template nests more than {MAX_NESTING_DEPTH} levels deep; "
            "flatten the conditionals or spintax"
        )
    nodes: list[Node] = []

    while index < len(tokens):
        token = tokens[index]
        if isinstance(token, _Text):
            nodes.append(TextNode(token.value))
            index += 1
            continue

        raw = token.raw
        head = raw.strip()
        upper = head.upper()

        if upper in RESERVED_KEYWORDS and upper != "IF":
            index += 1
            if upper in stoppers:
                return tuple(nodes), index, upper
            raise TemplateSyntaxError(
                f"'{{{upper}}}' appears without a matching '{{IF ...}}'"
            )

        keyword = _keyword(raw)
        if keyword == "IF":
            node, index = _parse_conditional(tokens, index, counters, depth)
            nodes.append(node)
            continue

        alternatives = _split_alternatives(raw)
        if len(alternatives) > 1:
            nodes.append(_build_spintax(alternatives, counters, depth))
            index += 1
            continue

        if not head:
            raise TemplateSyntaxError("empty '{}' group; write '\\{\\}' for literal braces")

        nodes.append(VariableNode(_require_known_token(head, context="variable")))
        index += 1

    if stoppers:
        raise TemplateSyntaxError(
            "'{IF ...}' is never closed; every conditional needs a matching '{END}'"
        )
    return tuple(nodes), index, None


def _parse_conditional(
    tokens: Sequence[_Text | _Group],
    index: int,
    counters: _Counters,
    depth: int = 0,
) -> tuple[ConditionalNode, int]:
    token = tokens[index]
    assert isinstance(token, _Group)
    parts = token.raw.strip().split(None, 1)
    if len(parts) != 2 or not parts[1].strip():
        raise TemplateSyntaxError(
            "'{IF}' needs a token name, as in '{IF firstName}...{END}'"
        )
    name = _require_known_token(parts[1].strip(), context="IF condition")
    ordinal = counters.next_conditional()

    then_nodes, index, stopper = _parse_sequence(
        tokens, index + 1, frozenset({"ELSE", "END"}), counters, depth + 1
    )
    else_nodes: tuple[Node, ...] = ()
    if stopper == "ELSE":
        else_nodes, index, stopper = _parse_sequence(
            tokens, index, frozenset({"END"}), counters, depth + 1
        )
    if stopper != "END":
        raise TemplateSyntaxError(
            f"'{{IF {name}}}' is never closed; every conditional needs a matching '{{END}}'"
        )
    return ConditionalNode(name, then_nodes, else_nodes, ordinal), index


def _build_spintax(
    alternatives: Sequence[str],
    counters: _Counters,
    depth: int = 0,
) -> SpintaxNode:
    ordinal = counters.next_spintax()
    counters.spintax_sizes[ordinal] = len(alternatives)
    parsed = tuple(
        _parse_sequence(_lex(alternative), 0, frozenset(), counters, depth + 1)[0]
        for alternative in alternatives
    )
    return SpintaxNode(parsed, ordinal)


def parse_template(source: str) -> Program:
    """Parse a template body into a `Program`, validating every token name.

    Raises `TemplateSyntaxError` for anything that cannot be parsed or names a
    token the renderer could never resolve.
    """
    counters = _Counters()
    nodes, _, _ = _parse_sequence(_lex(source), 0, frozenset(), counters)
    return Program(
        source=source,
        nodes=nodes,
        spintax_count=counters.spintax,
        conditional_count=counters.conditional,
        spintax_sizes=MappingProxyType(dict(counters.spintax_sizes)),
    )


def _sample(
    nodes: Sequence[Node],
    *,
    else_ordinal: int | None,
    spin_choice: tuple[int, int] | None,
) -> str:
    parts: list[str] = []
    for node in nodes:
        if isinstance(node, TextNode):
            parts.append(node.value)
        elif isinstance(node, VariableNode):
            parts.append(SAMPLE_VALUE)
        elif isinstance(node, ConditionalNode):
            branch = (
                node.else_nodes if node.ordinal == else_ordinal else node.then_nodes
            )
            parts.append(
                _sample(branch, else_ordinal=else_ordinal, spin_choice=spin_choice)
            )
        elif isinstance(node, SpintaxNode):
            chosen = 0
            if spin_choice is not None and spin_choice[0] == node.ordinal:
                chosen = spin_choice[1]
            parts.append(
                _sample(
                    node.alternatives[chosen],
                    else_ordinal=else_ordinal,
                    spin_choice=spin_choice,
                )
            )
    return "".join(parts)


def style_samples(program: Program) -> list[str]:
    """Return concrete strings a template could produce, for authoring checks.

    Sampled, not exhaustive, and the distinction matters. Rendering every
    combination of branches and spins explodes combinatorially, so this varies
    one choice at a time: the base sample, then one sample per conditional taking
    its ELSE branch, then one per non-default spintax alternative. That catches
    the case that actually happens, which is a fallback branch nobody proofread.

    It does not catch a violation that only appears when two choices interact.
    ``{Hello|In today's}{ there| world| now}.`` passes here and is refused at
    render time on the sequence that selects "In today's world". The renderer,
    not this function, is what guarantees no bad message is ever returned.

    Capped at :data:`MAX_STYLE_SAMPLES` so validating an adversarial body with a
    thousand branch points stays cheap.
    """
    samples = [
        tidy_whitespace(_sample(program.nodes, else_ordinal=None, spin_choice=None))
    ]
    for ordinal in range(program.conditional_count):
        if len(samples) >= MAX_STYLE_SAMPLES:
            return samples
        samples.append(
            tidy_whitespace(
                _sample(program.nodes, else_ordinal=ordinal, spin_choice=None)
            )
        )
    for ordinal, size in program.spintax_sizes.items():
        for choice in range(1, size):
            if len(samples) >= MAX_STYLE_SAMPLES:
                return samples
            samples.append(
                tidy_whitespace(
                    _sample(
                        program.nodes, else_ordinal=None, spin_choice=(ordinal, choice)
                    )
                )
            )
    return samples


def _raise_style(violation: StyleViolation, *, where: str) -> None:
    raise TemplateStyleError(f"{where}: {violation.message}")


def validate_style(
    body: str,
    program: Program,
    policy: StylePolicy = DEFAULT_STYLE,
    *,
    where: str = "template body",
) -> None:
    """Apply the writing style rules to authored template text.

    The dash ban is checked against the raw body, which is total: no branch,
    spin or escape can hide one. The opener, sentence-length and stranded
    punctuation rules are checked against `style_samples`, because they only
    make sense on text that has had its braces resolved, and are therefore
    sampled rather than exhaustive. The renderer repeats all of them on the
    finished message, so anything sampling misses is caught before sending.
    """
    dash = contains_forbidden_dash(body, policy)
    if dash is not None:
        _raise_style(
            StyleViolation(
                kind="forbidden_dash",
                message=(
                    f"contains the forbidden dash {dash!r}; the writing style "
                    "rules ban em dashes outright, so use a plain hyphen or two "
                    "sentences"
                ),
            ),
            where=where,
        )

    for sample in style_samples(program):
        for violation in style_violations(sample, policy, check_dashes=False):
            _raise_style(violation, where=where)
        artefact = broken_punctuation(sample)
        if artefact is not None:
            _raise_style(
                StyleViolation(
                    kind="broken_punctuation",
                    message=(
                        f"leaves dangling punctuation near {artefact!r} when it "
                        f"renders as {sample[:60]!r}; move the punctuation inside "
                        "the branch that supplies the text"
                    ),
                ),
                where=where,
            )


def validate_kind(kind: str, fragments: Sequence[str]) -> None:
    """Check a template's `kind` against the AI fragments its bodies reference."""
    if kind not in TEMPLATE_KINDS:
        raise ValueError(
            f"unknown template kind {kind!r}; expected one of {', '.join(TEMPLATE_KINDS)}"
        )
    if kind == "static" and fragments:
        raise TemplateSyntaxError(
            "a static template must not reference AI fragments; "
            f"found {', '.join(sorted(fragments))}. Set kind='hybrid' instead"
        )
    if kind in AI_KINDS and not fragments:
        raise TemplateSyntaxError(
            f"a {kind!r} template must reference at least one '{{{AI_FRAGMENT_PREFIX}...}}' "
            "fragment for SEQ-05 to fill; set kind='static' instead"
        )


def validate_template(
    body: str,
    *,
    kind: str = "static",
    policy: StylePolicy = DEFAULT_STYLE,
    where: str = "template body",
) -> Program:
    """Parse and fully validate a single template body."""
    program = parse_template(body)
    validate_style(body, program, policy, where=where)
    validate_kind(kind, program.fragments())
    return program


def compile_bodies(
    bodies: Sequence[str],
    *,
    kind: str = "static",
    policy: StylePolicy = DEFAULT_STYLE,
) -> tuple[Program, ...]:
    """Parse and validate a body plus its whole-message variations together.

    The kind rule is applied to the union of the fragments every body references,
    so a hybrid template does not have to repeat its AI fragment in every
    variation to be considered hybrid.
    """
    if not bodies:
        raise TemplateSyntaxError("a template needs at least one body")

    programs: list[Program] = []
    fragments: set[str] = set()
    for index, body in enumerate(bodies):
        where = "template body" if index == 0 else f"variation {index}"
        if not body.strip():
            raise TemplateSyntaxError(f"{where} is empty")
        program = parse_template(body)
        validate_style(body, program, policy, where=where)
        fragments.update(program.fragments())
        programs.append(program)

    validate_kind(kind, sorted(fragments))
    return tuple(programs)
