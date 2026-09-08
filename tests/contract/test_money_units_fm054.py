"""FM-054: money units are normalized before the claim check.

Ledger evidence is minor units; human copy is major units. Comparing the raw
magnitudes fails in both directions, and the second one is the dangerous one:
``$4,900`` matches the minor-unit digits of a pile worth $49.00, so a validator
whose whole job is refusing invented money licensed a 100x overstatement.
"""

from __future__ import annotations

import pytest

from found_money.strategy.campaign import (
    _evidence_text,
    _has_unsupported_numeric_claim,
    _numeric_claims,
)

EVIDENCE = [_evidence_text("map_value", "4900 usd")]


def test_map_value_evidence_is_rendered_in_major_units():
    assert _evidence_text("map_value", "4900 usd") == "49.00 usd"


@pytest.mark.parametrize("copy", ["$49.00", "$49", "49 dollars", "usd 49"])
def test_truthful_renderings_of_the_amount_are_allowed(copy: str):
    assert not _has_unsupported_numeric_claim(copy, EVIDENCE)


@pytest.mark.parametrize("copy", ["$4,900", "4900 usd", "$490.00", "$4.90"])
def test_overstatements_and_raw_minor_units_are_rejected(copy: str):
    """`$4,900` is the regression that mattered: it passed before FM-054."""

    assert _has_unsupported_numeric_claim(copy, EVIDENCE)


def test_zero_decimal_currencies_are_not_scaled():
    assert _evidence_text("map_value", "4900 jpy") == "4900 jpy"
    assert not _has_unsupported_numeric_claim("4900 jpy", [_evidence_text("map_value", "4900 jpy")])


def test_three_decimal_currencies_use_their_own_exponent():
    assert _evidence_text("map_value", "4900 kwd") == "4.900 kwd"


@pytest.mark.parametrize(
    "category,value",
    [
        ("business_input", "40 percent contribution margin"),
        ("business_input", "100 recovery reviews per week"),
        ("map_basis", "observed_face_value"),
        ("data_gap", "4900 usd"),
    ],
)
def test_non_money_categories_are_left_alone(category: str, value: str):
    """Percent and capacity are not currency. Only map_value carries minor units."""

    assert _evidence_text(category, value) == value


@pytest.mark.parametrize(
    "value",
    ["not money", "4900", "4900 zzz", "", "4900 usd extra"],
)
def test_unparseable_or_unsupported_values_pass_through_unchanged(value: str):
    assert _evidence_text("map_value", value) == value


def test_currency_mismatch_after_scaling_is_still_rejected():
    """49.00 in the right magnitude but the wrong currency must not pass."""

    assert _has_unsupported_numeric_claim("£49.00", EVIDENCE)


# --- magnitude suffix must be a whole token -------------------------------
#
# Found while landing FM-054, but a defect in the FM-049 parser rather than a
# consequence of unit normalization. `_CLAIM_AMOUNT` allowed an optional
# [kKmMbB] immediately after the digits, so the suffix swallowed the first
# letter of the following word. It stayed latent only because canonical copy
# said "4900 usd" rather than "$49.00".


@pytest.mark.parametrize(
    "copy,expected",
    [
        ("$220.00 membership", "220.00"),  # was 220000000.00
        ("$49 bonus", "49"),  # was 49000000000
        ("$30 kits", "30"),
        ("$12 boxes", "12"),
    ],
)
def test_a_following_word_is_not_read_as_a_magnitude_suffix(copy: str, expected: str):
    claims = _numeric_claims(copy)
    assert {str(claim.value) for claim in claims} == {expected}


@pytest.mark.parametrize(
    "copy,expected",
    [
        ("$5k plan", "5000"),
        ("$7 k", "7000"),
        ("$2.5M ARR", "2500000.0"),
        ("$3 million deal", "3000000"),
        ("$1.5 billion", "1500000000.0"),
        ("two thousand dollars", "2000"),
    ],
)
def test_real_magnitudes_still_parse(copy: str, expected: str):
    claims = _numeric_claims(copy)
    assert {str(claim.value) for claim in claims} == {expected}
