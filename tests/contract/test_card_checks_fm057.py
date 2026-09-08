"""FM-057: schema facts are checked by code, not by a blind reviewer.

Every check here exists because a reviewer actually raised it as a finding, and
because the same finding appeared and disappeared across rounds on unchanged
text. Anything a machine can decide should not be left to a sampled judgment.
"""

from __future__ import annotations

import pytest

from pydantic import ValidationError

from found_money.contracts.campaign import CompleteRecoveryPlaySetV1
from found_money.strategy.campaign import _canonical_plays
from found_money.strategy.card_checks import card_violations, permitted_formats
from datetime import datetime, timezone

BUILT_AT = datetime(2026, 8, 10, 13, 0, tzinfo=timezone.utc)


def _plays() -> CompleteRecoveryPlaySetV1:
    return _canonical_plays("run_fm057", BUILT_AT)


def _mutate(field: str, value, *, play_index: int = 0, card_index: int = 0):
    """Return the canonical play set with one card field replaced."""

    payload = _plays().model_dump(mode="json")
    plays = sorted(payload["plays"], key=lambda item: item["rank"])
    card = plays[play_index]["concept_cards"][card_index]
    if isinstance(card[field], dict):
        card[field]["text"] = value
    else:
        card[field] = value
    payload["plays"] = plays
    return CompleteRecoveryPlaySetV1.model_validate(payload)


def test_canonical_cards_have_no_deterministic_defects():
    assert card_violations(_plays()) == []


def test_format_style_must_name_a_permitted_family():
    """Reviewer finding: 'describes a visual style but does not name a format'."""

    broken = _mutate("format_style", "Bold directional concept with aggregate labels")
    assert any("format_style:not_a_named_format" in v for v in card_violations(broken))


@pytest.mark.parametrize(
    "style",
    [
        "Founder customer call, split screen",
        "Use-case expansion, three short scenes",
        "Headline static, one line",
        "Wall of text static, high contrast",
    ],
)
def test_real_format_families_are_accepted(style: str):
    assert card_violations(_mutate("format_style", style)) == []


def test_permitted_formats_come_from_the_skill_table():
    assert "founder_customer_call" in permitted_formats()
    assert "apology_restock" in permitted_formats()


@pytest.mark.parametrize(
    "cta",
    [
        "Stop or complete the review in the billing portal",
        "Open the billing portal and then book a call",
        "Reply, or alternatively open the billing portal",
    ],
)
def test_cta_must_name_one_action(cta: str):
    """Reviewer finding: 'offers mutually exclusive instructions'."""

    assert any("cta:more_than_one_action" in v for v in card_violations(_mutate("cta", cta)))


def test_duplicate_card_names_are_rejected():
    dup = _plays().model_dump(mode="json")
    plays = sorted(dup["plays"], key=lambda item: item["rank"])
    plays[0]["concept_cards"][1]["card_name"] = plays[0]["concept_cards"][0]["card_name"]
    dup["plays"] = plays
    broken = CompleteRecoveryPlaySetV1.model_validate(dup)
    assert any("card_name:duplicate_of_" in v for v in card_violations(broken))


@pytest.mark.parametrize("hook", ["A $49.00 opportunity can stall", "The 4900 usd opportunity"])
def test_ledger_money_is_rejected_in_creative_hooks(hook: str):
    """V2_RECUT section 1: ledger money never crosses into customer copy."""

    assert any("hook:ledger_money_in_creative" in v for v in card_violations(_mutate("hook", hook)))


def test_big_idea_may_not_restate_its_own_play():
    """Reviewer finding: 'restates the play's core mechanism'."""

    play_idea = sorted(_plays().plays, key=lambda p: p.rank)[0].creative_big_idea.text
    broken = _mutate("big_idea", play_idea)
    assert any("big_idea:restates_play" in v for v in card_violations(broken))


def test_contract_already_rejects_finished_scripts_upstream():
    """The rubric asked a model to judge something the schema already refuses."""

    payload = _plays().model_dump(mode="json")
    plays = sorted(payload["plays"], key=lambda item: item["rank"])
    plays[0]["concept_cards"][0]["production_requirements"] = ["Scene 1 opens on the counter"]
    payload["plays"] = plays
    with pytest.raises(ValidationError, match="finished script"):
        CompleteRecoveryPlaySetV1.model_validate(payload)


def test_violations_are_stable_and_sorted():
    """The whole point: same input, same answer, every time."""

    broken = _mutate("format_style", "Bold directional concept")
    first = card_violations(broken)
    assert first == sorted(set(first))
    assert all(card_violations(broken) == first for _ in range(5))
