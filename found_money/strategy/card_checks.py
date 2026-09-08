"""Deterministic concept-card checks (FM-057).

A blind reviewer should not be asked to rediscover schema facts. Whether a field
is empty, whether a format is one of the permitted ones, whether a CTA names one
action rather than two, whether a card restates its own play: all of that is
mechanical, and asking a model to judge it is what produced whole-field reversals
between review rounds.

So these run in the build path as a hard gate, and the rubric handed to a reviewer
covers only what genuinely needs judgment. Every check here corresponds to a
finding a reviewer actually raised, which is the test of whether it belongs.

Deliberately absent: empty content, single-word content, and finished scripts in
``production_requirements``. ``GroundedCopyV1`` and the play contract already
reject all three upstream, so duplicating them here would be unreachable code
pretending to be a safeguard.

That absence is itself the finding. Three of the ten concept-card rubric items,
including ``production_requirements_and_no_finished_script``, were already
machine-enforced before a reviewer was ever asked to judge them.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from found_money.strategy.intelligence import load_table

if TYPE_CHECKING:  # pragma: no cover - typing only
    from found_money.contracts.campaign import CompleteRecoveryPlaySetV1

# "Take the slot or complete the review" is two instructions. A reader has to
# choose, which is not a call to action. Reviewer finding: "offers mutually
# exclusive instructions rather than a clear call to action".
_SPLIT_CTA_RE = re.compile(r"\b(?:or|and then|otherwise|alternatively)\b", re.IGNORECASE)

# Ledger money must not appear in customer-facing creative.
_MONEY_RE = re.compile(r"[$£€]\s*\d|\b\d+(?:[.,]\d+)?\s*(?:usd|gbp|eur)\b", re.IGNORECASE)


def permitted_formats() -> frozenset[str]:
    """Format families the concept cards may use, from the skill table."""

    formats = load_table("ad-concepts").get("formats")
    if not isinstance(formats, dict) or not formats:
        raise ValueError("ad-concepts.json has no formats object")
    return frozenset(formats)


def _format_token(style: str) -> str:
    """Reduce a written format style to a comparable token."""

    return "_".join(re.findall(r"[a-z]+", style.casefold()))


def _names_a_permitted_format(style: str) -> bool:
    token = _format_token(style)
    if not token:
        return False
    # A style names a permitted family if the family's words appear in order at
    # the front of it, so "Founder customer call, split screen" resolves to
    # founder_customer_call without pinning the whole descriptive tail.
    return any(token.startswith(family) for family in _PERMITTED_FORMAT_PREFIXES())


def _PERMITTED_FORMAT_PREFIXES() -> frozenset[str]:  # noqa: N802 - internal helper
    extra = {
        # Paid static families from the paid addendum. These are equally real
        # formats; the ad-concepts table lists the lapsed-customer subset.
        "first_frame_screenshot",
        "social_comment_screenshot",
        "founder_letter",
        "tweet_style",
        "wall_of_text",
        "us_versus_them",
        "headline_static",
        "social_native_grid",
    }
    return frozenset(permitted_formats() | extra)


def card_violations(play_set: CompleteRecoveryPlaySetV1) -> list[str]:
    """Return deterministic card defects as ``path:rule`` strings.

    Empty means the schema half of the rubric passes. This never judges quality.
    """

    from found_money.strategy.campaign import _materially_different

    violations: list[str] = []
    seen_names: dict[str, str] = {}

    for play in sorted(play_set.plays, key=lambda item: item.rank):
        for card in play.concept_cards:
            path = f"card:{play.rank}-{card.card_id.rsplit('-', 1)[-1]}"

            name = card.card_name.strip()
            folded = name.casefold()
            if folded in seen_names:
                violations.append(f"{path}.card_name:duplicate_of_{seen_names[folded]}")
            else:
                seen_names[folded] = path

            if not _names_a_permitted_format(card.format_style):
                violations.append(f"{path}.format_style:not_a_named_format")

            if _SPLIT_CTA_RE.search(card.cta.text):
                violations.append(f"{path}.cta:more_than_one_action")

            if _MONEY_RE.search(card.hook.text):
                violations.append(f"{path}.hook:ledger_money_in_creative")

            # A card that restates its play has not added a concept. This is the
            # "restates the play's core mechanism" finding, made mechanical.
            if not _materially_different(card.big_idea.text, play.creative_big_idea.text):
                violations.append(f"{path}.big_idea:restates_play")

    return sorted(set(violations))
