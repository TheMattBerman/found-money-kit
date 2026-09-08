"""First-touch copy rules, loaded from the recovery-intelligence skill.

The rules themselves live in ``skills/recovery-intelligence/tables/``. This module
only applies them, so adding or changing a rule means editing a table rather than
editing Python.

Scope is deliberately narrow: the first step of a customer-facing sequence, plus
prohibitions that depend on which pile the play is for. Operator-facing copy keeps
its existing treatment.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

from found_money.strategy.intelligence import IntelligenceTableError, load_table


@lru_cache(maxsize=None)
def _first_touch() -> dict[str, Any]:
    return load_table("first-touch-rules")


@lru_cache(maxsize=None)
def _prohibitions() -> dict[str, dict[str, Any]]:
    by_pile = load_table("pile-prohibitions").get("by_pile")
    if not isinstance(by_pile, dict):
        raise IntelligenceTableError("pile-prohibitions.json has no 'by_pile' object")
    resolved: dict[str, dict[str, Any]] = {}
    for pile, entry in by_pile.items():
        if not isinstance(entry, dict):
            raise IntelligenceTableError(f"pile-prohibitions.json entry is not an object: {pile}")
        alias = entry.get("same_as")
        if alias is not None:
            target = by_pile.get(alias)
            if not isinstance(target, dict):
                raise IntelligenceTableError(
                    f"pile-prohibitions.json {pile} aliases unknown pile {alias!r}"
                )
            resolved[pile] = target
        else:
            resolved[pile] = entry
    return resolved


@lru_cache(maxsize=None)
def _forbidden_openers() -> tuple[str, ...]:
    section = _first_touch().get("forbidden_substrings_case_insensitive", {})
    openers = section.get("openers")
    if not isinstance(openers, list) or not openers:
        raise IntelligenceTableError("first-touch-rules.json has no opener list")
    return tuple(str(item).casefold() for item in openers)


@lru_cache(maxsize=None)
def _forbidden_link_patterns() -> tuple[re.Pattern[str], ...]:
    section = _first_touch().get("forbidden_patterns", {})
    patterns = [value for key, value in section.items() if key != "reason"]
    if not patterns:
        raise IntelligenceTableError("first-touch-rules.json has no link patterns")
    return tuple(re.compile(str(p), re.IGNORECASE) for p in patterns)


@lru_cache(maxsize=None)
def _forbidden_cta_shapes() -> tuple[str, ...]:
    section = _first_touch().get("forbidden_cta_shapes", {})
    shapes = section.get("yes_oriented")
    if not isinstance(shapes, list) or not shapes:
        raise IntelligenceTableError("first-touch-rules.json has no yes-oriented CTA list")
    return tuple(str(item).casefold() for item in shapes)


def first_touch_violations(*, subject: str, body: str, cta: str) -> list[str]:
    """Return rule names the first step breaks. Empty means it passes."""

    broken: list[str] = []
    folded_body = body.casefold()
    folded_subject = subject.casefold()

    if any(opener in folded_body or opener in folded_subject for opener in _forbidden_openers()):
        broken.append("throat_clearing_opener")

    for pattern in _forbidden_link_patterns():
        if pattern.search(body) or pattern.search(cta) or pattern.search(subject):
            broken.append("link_in_first_touch")
            break

    if any(shape in cta.casefold() for shape in _forbidden_cta_shapes()):
        broken.append("yes_oriented_cta")

    required = _first_touch().get("required", {})
    if required.get("subject_case") == "lower" and subject != subject.lower():
        broken.append("subject_is_not_a_utilitarian_label")
    if required.get("body_must_be_a_question") and "?" not in body:
        broken.append("body_is_not_a_question")

    max_words = required.get("body_words_max")
    if isinstance(max_words, int) and len(body.split()) > max_words:
        broken.append("body_is_longer_than_one_question")

    return broken


def pile_prohibition_violations(pile_id: str, text: str) -> list[str]:
    """Return prohibitions this pile's copy breaks. Empty means it passes."""

    entry = _prohibitions().get(pile_id)
    if entry is None:
        return []
    broken: list[str] = []
    folded = text.casefold()
    error_name = entry.get("forbidden_substrings_error_name", "cancellation_framing")
    for phrase in entry.get("forbidden_substrings_case_insensitive", []):
        if str(phrase).casefold() in folded:
            broken.append(error_name)
            break
    for name, pattern in (entry.get("forbidden_patterns") or {}).items():
        if re.search(str(pattern), text, re.IGNORECASE):
            broken.append(str(name))
    return broken


def pile_prohibits_email(pile_id: str) -> bool:
    """Whether the prohibitions table marks this pile as no-email-play."""

    entry = _prohibitions().get(pile_id)
    if entry is None:
        return False
    return bool(entry.get("no_email_play", False))
