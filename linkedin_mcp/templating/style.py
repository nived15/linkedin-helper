"""Writing style enforcement for rendered LinkedIn messages.

The rules come from `.github/copilot-instructions.md` and they are treated as a
hard constraint, not a nicety. Three of them are mechanically checkable:

* **No em dashes.** Enforced totally. Authored text (a template body, a
  whole-message variation, an AI fragment from SEQ-05) is *rejected* if it
  contains one. Lead data is *normalised* instead, because a company whose name
  contains an em dash is not the template author's fault and must not cost the
  lead a message. The final rendered string is asserted dash-free either way, so
  nobody can smuggle one through a variable value or a spintax alternative.
* **No filler openers.** Checked against a documented list. Enforced on authored
  text at validation time and on the final rendered string at render time, since
  a lead-supplied value could in principle open a message.
* **Short sentences.** Enforced on authored text at validation time. At render
  time it is only a warning, because a long company name can push a sentence
  over the limit and refusing to message someone for having a long employer is
  not a defensible failure mode. This asymmetry is deliberate and is the one
  soft rule in the module.

"No bullet-point walls" and "numbers over generalities" are judgement calls that
no regex settles honestly, so they are left to the human reviewing the template.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass


__all__ = [
    "DEFAULT_STYLE",
    "FILLER_OPENERS",
    "FORBIDDEN_DASHES",
    "StylePolicy",
    "StyleViolation",
    "broken_punctuation",
    "contains_forbidden_dash",
    "first_violation",
    "normalise_dashes",
    "sentences",
    "style_violations",
    "tidy_whitespace",
]

EM_DASH = "\u2014"
EN_DASH = "\u2013"

FORBIDDEN_DASHES: tuple[str, ...] = (
    EM_DASH,  # em dash
    EN_DASH,  # en dash
    "\u2012",  # figure dash
    "\u2015",  # horizontal bar
    "\u2212",  # minus sign
    "\u2e3a",  # two-em dash
    "\u2e3b",  # three-em dash
)
"""Every dash-like character that must never survive into a rendered message.

The em dash is the one the instructions name. The rest are included because they
render almost identically in a LinkedIn message box, so allowing them would make
the em dash ban trivial to sidestep by accident.
"""

FILLER_OPENERS: tuple[str, ...] = (
    "in today's world",
    "in today's fast-paced world",
    "in this day and age",
    "it's no secret",
    "let's be honest",
    "let me be honest",
    "let's face it",
    "at the end of the day",
    "needless to say",
    "i hope this message finds you well",
    "i hope this finds you well",
)
"""Openers the writing style rules ban outright, lowercased and apostrophes straight."""

_HORIZONTAL_WHITESPACE = re.compile(r"[ \t\u00a0]+")
_SPACE_BEFORE_PUNCTUATION = re.compile(r" +([,.;:!?])")
_BLANK_LINE_RUN = re.compile(r"\n{3,}")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_LINE_STARTS_WITH_PUNCTUATION = re.compile(r"(?:\A|\n)[ \t]*[,.;:!?]")
_DOUBLED_PUNCTUATION = re.compile(r"[,;:][ \t]*[,.;:!?]")
_TRAILING_PUNCTUATION = re.compile(r"[,;:]\s*\Z")
_EMPTY_DELIMITER_PAIR = re.compile(r"\(\s*\)|\[\s*\]|\{\s*\}|\"\s*\"")
_CURLY_APOSTROPHES = str.maketrans({"\u2018": "'", "\u2019": "'"})


@dataclass(frozen=True, slots=True)
class StylePolicy:
    """The style rules applied to one template or render.

    Defaults match `.github/copilot-instructions.md`. The sentence limit and the
    opener list are tunable, because a long-form post is not a connection note.
    The dash ban is not tunable: `extra_forbidden_dashes` only ever adds to
    :data:`FORBIDDEN_DASHES`, so no policy a caller constructs can let an em dash
    through. Making it a removable field once meant a permissive policy silently
    disabled the one rule that was supposed to be absolute.
    """

    max_sentence_words: int = 30
    filler_openers: tuple[str, ...] = FILLER_OPENERS
    extra_forbidden_dashes: tuple[str, ...] = ()
    check_filler_openers: bool = True
    check_sentence_length: bool = True

    @property
    def forbidden_dashes(self) -> tuple[str, ...]:
        """Every banned dash: the mandatory floor plus any the caller added."""
        if not self.extra_forbidden_dashes:
            return FORBIDDEN_DASHES
        return tuple(dict.fromkeys(FORBIDDEN_DASHES + self.extra_forbidden_dashes))


DEFAULT_STYLE = StylePolicy()


@dataclass(frozen=True, slots=True)
class StyleViolation:
    """One broken style rule, with enough context to fix it."""

    kind: str
    message: str
    excerpt: str = ""

    def __str__(self) -> str:
        return self.message


def normalise_dashes(text: str, policy: StylePolicy = DEFAULT_STYLE) -> str:
    """Replace every forbidden dash with a plain hyphen.

    Applied to lead-supplied values so a scraped company name cannot put an em
    dash into a message. A plain hyphen is a legal compound-modifier character
    under the writing style rules, so the result stays compliant.
    """
    for dash in policy.forbidden_dashes:
        text = text.replace(dash, "-")
    return text


def contains_forbidden_dash(text: str, policy: StylePolicy = DEFAULT_STYLE) -> str | None:
    """Return the first forbidden dash found, or None."""
    for dash in policy.forbidden_dashes:
        if dash in text:
            return dash
    return None


def sentences(text: str) -> list[str]:
    """Split text into sentences for the length rule.

    Newlines end a sentence too. A LinkedIn message is written in short lines and
    a line break is a full stop in practice, so treating a paragraph as one
    50-word sentence would be wrong.
    """
    found: list[str] = []
    for line in text.split("\n"):
        for candidate in _SENTENCE_SPLIT.split(line):
            cleaned = candidate.strip()
            if cleaned:
                found.append(cleaned)
    return found


def _fold(text: str) -> str:
    return text.translate(_CURLY_APOSTROPHES).lower().lstrip()


def style_violations(
    text: str,
    policy: StylePolicy = DEFAULT_STYLE,
    *,
    check_dashes: bool = True,
) -> list[StyleViolation]:
    """Return every style rule this text breaks.

    `check_dashes` is off for text that has already been dash-normalised, which
    keeps the caller from reporting a violation it has itself repaired.
    """
    violations: list[StyleViolation] = []

    if check_dashes:
        dash = contains_forbidden_dash(text, policy)
        if dash is not None:
            violations.append(
                StyleViolation(
                    kind="forbidden_dash",
                    message=(
                        f"text contains the forbidden dash {dash!r}; "
                        "use a plain hyphen or split the sentence"
                    ),
                    excerpt=_excerpt_around(text, dash),
                )
            )

    if policy.check_filler_openers:
        folded = _fold(text)
        for opener in policy.filler_openers:
            if folded.startswith(opener):
                violations.append(
                    StyleViolation(
                        kind="filler_opener",
                        message=f"text opens with the banned filler {opener!r}",
                        excerpt=text[:80],
                    )
                )
                break

    if policy.check_sentence_length:
        for sentence in sentences(text):
            words = sentence.split()
            if len(words) > policy.max_sentence_words:
                violations.append(
                    StyleViolation(
                        kind="long_sentence",
                        message=(
                            f"sentence runs to {len(words)} words, over the "
                            f"limit of {policy.max_sentence_words}; break it in two"
                        ),
                        excerpt=sentence[:80],
                    )
                )

    return violations


def _excerpt_around(text: str, needle: str, width: int = 30) -> str:
    index = text.find(needle)
    if index < 0:
        return text[:width]
    start = max(0, index - width)
    return text[start : index + width]


def tidy_whitespace(text: str) -> str:
    """Collapse the whitespace an IF/ELSE or an empty spintax branch leaves behind.

    Removing a branch mid-sentence leaves double spaces and a space in front of
    the comma that followed it. Neither is a broken message on its own, but both
    read as machine output, which is exactly what the writing style rules are
    there to prevent.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _HORIZONTAL_WHITESPACE.sub(" ", text)
    text = _SPACE_BEFORE_PUNCTUATION.sub(r"\1", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    text = _BLANK_LINE_RUN.sub("\n\n", text)
    return text.strip()


def broken_punctuation(text: str) -> str | None:
    """Return the punctuation artefact that proves a slot rendered empty.

    Runs after `tidy_whitespace`, so "Hi {firstName}," collapsing to "Hi ," has
    already become "Hi,". Tidying is what makes the residue detectable rather
    than what hides it. Four artefacts count, and each one means a variable slot
    vanished:

    * a line that starts with punctuation, as in ", welcome"
    * two punctuation marks with nothing between them, as in "Hi,, there"
    * a message that ends on a comma, semicolon or colon, as in "Hi," from
      ``Hi {IF firstName}{firstName}{END},``
    * an empty bracket or quote pair, as in "Hi ()" from
      ``Hi ({IF company}{company}{END})``

    The last two are the reason this function exists at all. Without them a
    template whose only personalisation was a bare conditional would render a
    greeting addressed to nobody and report itself as fine.
    """
    for pattern in (
        _LINE_STARTS_WITH_PUNCTUATION,
        _DOUBLED_PUNCTUATION,
        _TRAILING_PUNCTUATION,
        _EMPTY_DELIMITER_PAIR,
    ):
        match = pattern.search(text)
        if match is not None:
            return match.group().strip()
    return None


def first_violation(
    texts: Iterable[str],
    policy: StylePolicy = DEFAULT_STYLE,
    *,
    check_dashes: bool = True,
) -> StyleViolation | None:
    """Return the first violation across several texts, or None."""
    for text in texts:
        found = style_violations(text, policy, check_dashes=check_dashes)
        if found:
            return found[0]
    return None
