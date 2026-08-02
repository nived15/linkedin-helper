"""Even, deterministic assignment of whole-message variations across a queue.

The definition of done says variations are "split evenly across the queue, not
randomly", and the distinction is the whole point. `random.choice` over three
variations and 100 leads gives roughly 33 each, and sometimes gives 45. Roughly
is not evenly, and a run that cannot be reproduced cannot be reviewed.

So assignment is a plain round robin over the lead's position in the queue:
variation `sequence % count`. It is even by construction, not on average, and
the same lead in the same queue position always gets the same variation, which
means a message can be previewed before it is sent and re-derived afterwards
from the log.

Spintax alternatives are picked the same way, offsetting by the spintax node's
position in the template so two spins in one message do not move in lockstep.
Descending into a chosen alternative divides the sequence by that node's
branching factor, which is plain mixed-radix counting. Without that division a
spintax nested inside another can have an alternative that is unreachable: the
inner node is only visited on sequences the outer node selected it for, and on
that subset `sequence + ordinal` can be constant. The same division is applied
when the variation is chosen, so a template with three variations and a spin
with three alternatives does not pair each variation with one fixed spin.

Whole-message variations stay exactly even, always. Spintax alternatives are
even over the sequences on which they are reached, to within a message or two
once the divisions stack up, and every alternative is always reachable.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence


__all__ = [
    "assign_variations",
    "spintax_index",
    "variation_distribution",
    "variation_index",
    "variation_plan",
]


def _require_positive(count: int, label: str) -> int:
    if count <= 0:
        raise ValueError(f"{label} must be at least 1, got {count}")
    return count


def _require_sequence(sequence: int) -> int:
    if sequence < 0:
        raise ValueError(f"sequence must not be negative, got {sequence}")
    return sequence


def variation_index(sequence: int, count: int) -> int:
    """Return which variation the message at `sequence` uses.

    `sequence` is the lead's zero-based position in the campaign queue. SEQ-01
    (#19) owns that ordering; this function only needs it to be stable and
    contiguous for the split to come out even.
    """
    return _require_sequence(sequence) % _require_positive(count, "variation count")


def spintax_index(sequence: int, ordinal: int, count: int) -> int:
    """Return which spintax alternative node `ordinal` uses at `sequence`."""
    _require_sequence(sequence)
    if ordinal < 0:
        raise ValueError(f"ordinal must not be negative, got {ordinal}")
    return (sequence + ordinal) % _require_positive(count, "alternative count")


def assign_variations(total: int, count: int) -> list[int]:
    """Return the variation index for each of `total` messages, in queue order."""
    if total < 0:
        raise ValueError(f"total must not be negative, got {total}")
    _require_positive(count, "variation count")
    return [sequence % count for sequence in range(total)]


def variation_plan(sequences: Iterable[int], count: int) -> dict[int, int]:
    """Map each queue position to its variation index.

    Useful when the campaign engine wants to show a whole queue's assignment
    before it starts sending.
    """
    _require_positive(count, "variation count")
    return {sequence: variation_index(sequence, count) for sequence in sequences}


def variation_distribution(assignments: Sequence[int]) -> dict[int, int]:
    """Count how many messages each variation index received."""
    return dict(sorted(Counter(assignments).items()))
