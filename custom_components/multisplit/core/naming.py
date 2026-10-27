"""Guessing what someone meant to type.

A configured entity id that does not exist is the failure that looks least
like a configuration mistake from the outside: the room simply never gets a
temperature, so it stands by for ever and reports no data. Naming the missing
id helps. Naming the id it was almost certainly meant to be helps more --
``climate.…_ischa_climat`` against ``climate.…_ischa_climate`` is one
character, and one character is exactly the kind of difference a person reads
straight past.

Pure: standard library only, so the matching is testable without Home
Assistant or a populated state machine.
"""

from __future__ import annotations

from collections.abc import Iterable
from difflib import SequenceMatcher

#: How many characters a candidate may differ by before it stops being a
#: plausible correction.
#:
#: Deliberately an absolute count rather than a similarity ratio. These entity
#: ids are long and almost entirely boilerplate --
#: `climate.mitsubishi_ac_erin_mitsubishi_ac_erin_climate` against
#: `climate.mitsubishi_ac_office_mitsubishi_ac_office_climate` scores over 0.85
#: similar while being a completely different room. A ratio measures how much
#: two strings have in common; what matters here is how much they do not.
_MAX_DIFFERENCE = 3


def closest(entity_id: str, candidates: Iterable[str]) -> str | None:
    """The candidate that ``entity_id`` was most likely meant to be.

    Restricted to the same domain: a missing ``climate.x`` is never a mistyped
    ``sensor.x``, and offering one would be worse than offering nothing.
    """
    domain = entity_id.split(".", 1)[0]
    best: tuple[int, str] | None = None

    for candidate in candidates:
        if candidate == entity_id or not candidate.startswith(f"{domain}."):
            continue
        difference = _difference(entity_id, candidate)
        if difference <= _MAX_DIFFERENCE and (best is None or difference < best[0]):
            best = (difference, candidate)

    return best[1] if best else None


def _difference(a: str, b: str) -> int:
    """Characters that do not line up between the two strings."""
    matched = sum(block.size for block in SequenceMatcher(None, a, b).get_matching_blocks())
    return len(a) + len(b) - 2 * matched


def describe_missing(missing: list[str], candidates: Iterable[str]) -> list[str]:
    """One line per missing entity, with a suggestion where there is one."""
    pool = list(candidates)
    lines = []
    for entity_id in missing:
        guess = closest(entity_id, pool)
        lines.append(
            f"{entity_id} — did you mean {guess}?" if guess else entity_id
        )
    return lines
