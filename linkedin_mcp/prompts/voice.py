"""MCP-05 (#28): Nived's writing rules, derived rather than retyped.

The rules already exist three times over: as Markdown in
`.github/copilot-instructions.md`, as Markdown again in
`.github/instructions/content-voice.instructions.md`, and as enforceable code in
:mod:`linkedin_mcp.templating.style`. Only the third is checkable, and only the
third is what actually rejects a template at validation time.

A prompt that hand-copied the prose would be a fourth copy that drifts from the
one the code enforces, and the drift would stay invisible until a client
followed the prompt and the template store refused what it produced. So
everything here is generated from
:data:`~linkedin_mcp.templating.style.FORBIDDEN_DASHES`,
:data:`~linkedin_mcp.templating.style.FILLER_OPENERS` and the live
:class:`~linkedin_mcp.templating.style.StylePolicy`. Adding a banned opener to
`style.py` changes what every prompt says on the next call.

The two rules no regex settles honestly, "no bullet-point walls" and "numbers
over generalities", are stated as judgement calls, because that is what
`style.py` says they are.

`tests/test_prompts.py` asserts that everything this module emits, and every
prompt rendered through the shipped server, passes
:func:`~linkedin_mcp.templating.style.style_violations` with nothing to report. A
prompt that preaches "no em dashes" while containing one is exactly the kind of
thing that survives review for months.
"""

from __future__ import annotations

import unicodedata

from linkedin_mcp.templating.style import (
    DEFAULT_STYLE,
    StylePolicy,
)

__all__ = [
    "PERSONA",
    "banned_dash_names",
    "voice_rules",
]

PERSONA = (
    "Nived Velayudhan is a Solution Engineer at Microsoft.\n"
    "He sells and implements GitHub Copilot for enterprise customers, so he "
    "writes as a practitioner who has watched large engineering teams adopt it "
    "and resist it.\n"
    "He is opinionated but honest. He names real limits and trade-offs.\n"
    "He is not a cheerleader, and nothing he writes should read as a vendor "
    "endorsement."
)
"""Who the generated text is meant to sound like.

Short on purpose. A persona long enough to be a character brief gets skimmed,
and the numbered rules below do the work that matters.
"""

_UNNAMED_DASH = "dash"


def banned_dash_names(policy: StylePolicy = DEFAULT_STYLE) -> tuple[str, ...]:
    """Every forbidden dash, named, straight from the policy.

    Reads `policy.forbidden_dashes` rather than the module constant, so a
    caller who added a dash through `extra_forbidden_dashes` sees it named. The
    constant is the floor of that property and no policy can shrink it, so the
    default result is still every dash in `style.py`.

    Named rather than shown. A prompt that listed the characters would contain
    the very characters it bans, so it would fail the check it is asking the
    client to pass, and the client would be told to avoid a symbol while being
    handed seven of them.
    """
    names: list[str] = []
    for dash in policy.forbidden_dashes:
        try:
            names.append(unicodedata.name(dash).lower())
        except ValueError:  # pragma: no cover - every dash in style.py is named
            names.append(_UNNAMED_DASH)
    return tuple(dict.fromkeys(names))


def voice_rules(policy: StylePolicy = DEFAULT_STYLE) -> str:
    """Return the writing rules as text, generated from the live policy.

    Every number and every list is read from `policy` or from `style.py` rather
    than written down here, so this cannot fall out of step with the checker
    that rejects a template.
    """
    dashes = ", ".join(banned_dash_names(policy))
    openers = "\n".join(f'   - "{opener}"' for opener in policy.filler_openers)
    return "\n".join(
        [
            "Voice rules. These are hard constraints, not preferences.",
            "linkedin_mcp.templating.style enforces the first three, so text "
            "that breaks them is rejected before it reaches LinkedIn.",
            "",
            f"1. No dashes of these kinds, anywhere: {dashes}.",
            "   A plain hyphen is allowed only inside a compound modifier, as "
            "in left-to-right.",
            "   If a sentence needs a pause in the middle, write two sentences.",
            "2. Never open with filler. These openers are rejected outright:",
            openers,
            f"3. Keep every sentence under {policy.max_sentence_words} words. "
            "Short sentences beat long ones.",
            "4. Prefer prose to bullet points. Use a list only for genuinely "
            "separate items.",
            '5. Use numbers, not generalities. "Saved 40 minutes" beats "saved '
            'a lot of time".',
            "   No regex checks this one, so it is on you.",
            "6. Write like someone who builds things. No corporate filler, no "
            "motivational lines, no launch-day superlatives.",
            "",
            PERSONA,
        ]
    )
