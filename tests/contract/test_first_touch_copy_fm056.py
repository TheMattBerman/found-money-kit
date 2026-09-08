"""FM-056: the first touch reads like a person wrote it.

Rules come from ``skills/recovery-intelligence/tables/``. This suite proves the
enforcement applies them and holds no rule of its own, which is the point of the
split in ``docs/product/V2_RECUT.md`` section 3b.
"""

from __future__ import annotations

import pytest

from found_money.strategy.copy_rules import (
    first_touch_violations,
    pile_prohibition_violations,
)

GOOD = {
    "subject": "billing glitch?",
    "body": "Did the card on file stop working?",
    "cta": "Reply to this email",
}


def test_a_human_first_touch_passes():
    assert first_touch_violations(**GOOD) == []


def test_the_pre_fm056_canonical_body_fails():
    """The old body was an instruction to the operator, not customer copy."""

    broken = first_touch_violations(
        subject="A quick Annual subscription payment check",
        body=(
            "Email one: name the Annual subscription interruption and send the "
            "billing portal path with no extra terms"
        ),
        cta="Review the Annual subscription details in the billing portal",
    )
    assert "subject_is_not_a_utilitarian_label" in broken
    assert "body_is_not_a_question" in broken


@pytest.mark.parametrize(
    "opener",
    [
        "Hope you are doing well, did the card stop working?",
        "It's been a while, did the card stop working?",
        "Just checking in, did the card stop working?",
        "As you may remember, did the card stop working?",
    ],
)
def test_throat_clearing_openers_are_rejected(opener: str):
    assert "throat_clearing_opener" in first_touch_violations(**{**GOOD, "body": opener})


@pytest.mark.parametrize(
    "field,value",
    [
        ("body", "Did the card stop working? https://example.test/billing"),
        ("cta", "Update it at https://example.test/billing"),
        ("body", "Did the card stop working? [update it](https://example.test)"),
        ("cta", "Book a slot: www.example.test/calendar"),
    ],
)
def test_links_in_the_first_touch_are_rejected(field: str, value: str):
    """A link in touch one tells the reader it is a broadcast."""

    assert "link_in_first_touch" in first_touch_violations(**{**GOOD, field: value})


@pytest.mark.parametrize(
    "cta",
    ["Would you like to hop on a call?", "Can we schedule 15 minutes?", "Book a time with me"],
)
def test_yes_oriented_ctas_are_rejected(cta: str):
    assert "yes_oriented_cta" in first_touch_violations(**{**GOOD, "cta": cta})


def test_a_second_sentence_that_pitches_is_rejected_by_length():
    """Answering your own question is the named failure mode."""

    broken = first_touch_violations(
        **{
            **GOOD,
            "body": (
                "Did the card on file stop working? If so we have a new annual plan "
                "at a better rate and I can walk you through it whenever suits you."
            ),
        }
    )
    assert "body_is_longer_than_one_question" in broken


def test_headline_subject_is_rejected():
    assert "subject_is_not_a_utilitarian_label" in first_touch_violations(
        **{**GOOD, "subject": "Your Annual Subscription Needs Attention"}
    )


@pytest.mark.parametrize("pile", ["failed_payment", "payment_rescue"])
@pytest.mark.parametrize(
    "text",
    [
        "Sorry to see you go",
        "Before you go, tell us why you cancelled",
        "We'll miss you",
        "Update within 24 hours or your account will be paused",
        "You will lose access unless you act",
    ],
)
def test_involuntary_piles_reject_farewell_and_ultimatum(pile: str, text: str):
    """An involuntary lapse is not a decision to leave."""

    assert pile_prohibition_violations(pile, text) != []


@pytest.mark.parametrize("text", ["Sorry to see you go", "Before you go"])
def test_the_same_framing_is_allowed_where_the_customer_did_choose_to_leave(text: str):
    assert pile_prohibition_violations("canceled_customer", text) == []


def test_unlisted_piles_have_no_prohibitions():
    assert pile_prohibition_violations("renewal_upsell", "Sorry to see you go") == []
