"""FM-023 value, modeled-opportunity, overlap, and recomputation contracts."""

from __future__ import annotations

from datetime import timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from found_money.contracts.events import RecoveryCandidateSetV1, RecoveryCandidateV1
from found_money.map import build_money_map, parse_canonical_json, public_money_map_projection
from found_money.value import (
    build_modeled_opportunity,
    build_value_ledger,
    public_contribution_projection,
    wilson_interval_80,
)

UTC = timezone.utc


def _candidate(
    family: str,
    unit: str,
    customer: str,
    *,
    source_reference: str | None = None,
    qualifying: dict[str, str] | None = None,
) -> RecoveryCandidateV1:
    return RecoveryCandidateV1.model_validate(
        {
            "run_id": "run_fm023",
            "event_family": family,
            "confidence_class": "observed" if family == "failed_payment" else "derived",
            "economic_unit_key": unit,
            "customer_token": customer,
            "lineage": {"source_reference": source_reference or unit},
            "qualifying_evidence": qualifying or {"event_family": family},
        }
    )


def _candidate_set(*candidates: RecoveryCandidateV1) -> RecoveryCandidateSetV1:
    return RecoveryCandidateSetV1(
        run_id="run_fm023",
        built_at="2026-07-29T18:00:10.000Z",
        candidates=list(candidates),
    )


def test_every_event_family_resolves_observed_minor_units_without_fx():
    candidates = _candidate_set(
        _candidate("failed_payment", "stripe_invoice:inv_1", "c1"),
        _candidate("expired_trial", "stripe_subscription:sub_1", "c2"),
        _candidate("trial_no_convert", "stripe_subscription:sub_2", "c3"),
        _candidate("closed_lost_stale_deal", "hubspot_deal:deal_1", "c4"),
        _candidate("canceled_customer", "stripe_subscription:sub_3", "c5"),
        _candidate("lapsed_repeat_buyer", "customer:c6:lapsed_repeat_buyer", "c6"),
        _candidate("silent_proposal", "proposal:proposal_1", "c7"),
        _candidate("no_show_rebook", "appointment:appointment_1", "c8"),
        _candidate("disappeared_high_value_customer", "order:order_1", "c9"),
        _candidate("overdue_reorder", "customer:c10:overdue_reorder", "c10"),
        _candidate("renewal_upsell", "stripe_subscription:sub_4", "c11"),
        _candidate("engaged_unbooked", "customer:c12:engaged_unbooked", "c12"),
    )
    records = [
        {"id": "inv_1", "amount_due_cents": "101", "currency": "USD"},
        {"id": "sub_1", "amount_minor": "102", "currency": "JPY"},
        {"id": "sub_2", "amount_minor": "103", "currency": "KWD"},
        {"id": "deal_1", "amount_minor": "104", "currency": "EUR"},
        {"id": "sub_3", "amount_minor": "105", "currency": "GBP"},
        {"customer_token": "c6", "amount_minor": "106", "currency": "USD"},
        {"id": "proposal_1", "amount_minor": "107", "currency": "CAD"},
        {"id": "appointment_1", "amount_minor": "108", "currency": "AUD"},
        {"id": "order_1", "amount_minor": "109", "currency": "USD"},
        {"customer_token": "c10", "amount_minor": "110", "currency": "JPY"},
        {"id": "sub_4", "amount_minor": "111", "currency": "KWD"},
        {"customer_token": "c12", "amount_minor": "112", "currency": "EUR"},
    ]

    ledger = build_value_ledger(candidates, {"normalized": {"records": records}})
    assert len(ledger.contributions) == 12
    assert ledger.data_gaps == []
    assert {item.pile_id for item in ledger.contributions} == {
        candidate.event_family for candidate in candidates.candidates
    }
    assert {item.currency for item in ledger.contributions} == {
        "aud",
        "cad",
        "eur",
        "gbp",
        "jpy",
        "kwd",
        "usd",
    }

    money_map = build_money_map(ledger, candidates)
    recomputed: dict[str, Decimal] = {}
    for item in ledger.contributions:
        recomputed[item.currency] = recomputed.get(item.currency, Decimal(0)) + item.amount_minor
    assert money_map.identified_opportunity_minor == dict(sorted(recomputed.items()))
    assert sum(item.economic_unit_count for item in money_map.piles) == 12
    assert all(item.value_basis == "observed_face_value" for item in money_map.piles)


@pytest.mark.parametrize(
    ("comparables", "positives"),
    [(29, 5), (30, 4)],
)
def test_modeled_opportunity_is_unquantified_below_both_thresholds(comparables, positives):
    candidate = _candidate("silent_proposal", "proposal:p1", "c1")
    assert (
        build_modeled_opportunity(
            candidate,
            average_value_minor="10000",
            currency="usd",
            comparables=comparables,
            positive_outcomes=positives,
            cohort="silent proposals",
            window="180 days",
        )
        is None
    )
    ledger = build_value_ledger(
        _candidate_set(candidate),
        {},
        modeled_inputs={
            candidate.candidate_key: {
                "average_value_minor": "10000",
                "currency": "usd",
                "comparables": comparables,
                "positive_outcomes": positives,
            }
        },
    )
    assert ledger.contributions == []
    assert ledger.data_gaps[0].reason_code == "insufficient_history"
    assert ledger.data_gaps[0].currency == "usd"


def test_modeled_opportunity_30_and_5_exposes_hand_calculated_wilson_fields():
    candidate = _candidate("silent_proposal", "proposal:p1", "c1")
    modeled = build_modeled_opportunity(
        candidate,
        average_value_minor="10000",
        currency="usd",
        comparables=30,
        positive_outcomes=5,
        cohort="silent proposals",
        window="180 days",
        basis="same currency and event family",
    )
    assert modeled is not None
    assert modeled.empirical_rate == Decimal("0.166666666666666667")
    assert modeled.numerator == 5
    assert modeled.denominator == 30
    assert modeled.cohort == "silent proposals"
    assert modeled.window == "180 days"
    assert modeled.basis == "same currency and event family"
    assert (modeled.wilson_interval_low, modeled.wilson_interval_high) == wilson_interval_80(5, 30)
    assert modeled.wilson_interval_low == Decimal("0.097317839894103999")
    assert modeled.wilson_interval_high == Decimal("0.270618341582076584")
    assert modeled.expected_value_minor == Decimal(1667)

    ledger = build_value_ledger(
        _candidate_set(candidate),
        {},
        modeled_inputs={
            candidate.candidate_key: {
                "average_value_minor": "10000",
                "currency": "usd",
                "comparables": 30,
                "positive_outcomes": 5,
                "cohort": "silent proposals",
                "window": "180 days",
                "basis": "same currency and event family",
            }
        },
    )
    assert len(ledger.contributions) == 1
    contribution = ledger.contributions[0]
    assert contribution.value_basis == "modeled_opportunity"
    assert contribution.amount_minor == Decimal(1667)
    assert contribution.modeled_opportunity == modeled


def test_overlap_matrix_selects_one_value_and_keeps_traceable_exclusion():
    failed = _candidate("failed_payment", "stripe_invoice:inv_1", "shared")
    canceled = _candidate("canceled_customer", "stripe_subscription:sub_1", "shared")
    proposal = _candidate("silent_proposal", "proposal:p1", "shared-proposal-deal")
    deal = _candidate("closed_lost_stale_deal", "hubspot_deal:d1", "shared-proposal-deal")
    trial = _candidate("expired_trial", "stripe_subscription:sub_2", "shared-trial")
    renewal = _candidate("renewal_upsell", "stripe_subscription:sub_3", "shared-trial")
    reorder = _candidate(
        "overdue_reorder", "customer:shared-reorder:overdue_reorder", "shared-reorder"
    )
    lapsed = _candidate(
        "lapsed_repeat_buyer", "customer:shared-reorder:lapsed_repeat_buyer", "shared-reorder"
    )
    candidates = _candidate_set(failed, canceled, proposal, deal, trial, renewal, reorder, lapsed)
    ledger = build_value_ledger(
        candidates,
        {
            "records": [
                {"id": "inv_1", "amount_minor": "100", "currency": "usd"},
                {"id": "sub_1", "amount_minor": "200", "currency": "usd"},
                {"id": "p1", "amount_minor": "300", "currency": "usd"},
                {"id": "d1", "amount_minor": "400", "currency": "usd"},
                {"id": "sub_2", "amount_minor": "500", "currency": "usd"},
                {"id": "sub_3", "amount_minor": "600", "currency": "usd"},
                {"customer_token": "shared-reorder", "amount_minor": "700", "currency": "usd"},
            ]
        },
    )
    assert sorted(item.amount_minor for item in ledger.contributions) == [
        Decimal(100),
        Decimal(400),
        Decimal(600),
        Decimal(700),
    ]
    assert ledger.overlap_ledger is not None
    assert len(ledger.overlap_ledger.records) == 4
    excluded = {record.excluded_candidate_key for record in ledger.overlap_ledger.records}
    assert canceled.candidate_key in excluded
    assert proposal.candidate_key in excluded
    assert trial.candidate_key in excluded
    assert lapsed.candidate_key in excluded
    assert {record.selected_candidate_key for record in ledger.overlap_ledger.records} == {
        failed.candidate_key,
        deal.candidate_key,
        renewal.candidate_key,
        reorder.candidate_key,
    }

    money_map = build_money_map(ledger, candidates)
    assert money_map.identified_opportunity_minor == {"usd": Decimal(1800)}
    assert money_map.overlap_exclusion_count == 4
    assert sum(item.economic_unit_count for item in money_map.piles) == 4


def test_observed_value_wins_against_modeled_representation_of_same_headline_group():
    failed = _candidate("failed_payment", "stripe_invoice:inv_1", "shared")
    canceled = _candidate("canceled_customer", "stripe_subscription:sub_1", "shared")
    ledger = build_value_ledger(
        _candidate_set(failed, canceled),
        {"records": [{"id": "sub_1", "amount_minor": "900", "currency": "usd"}]},
        modeled_inputs={
            failed.candidate_key: {
                "average_value_minor": "10000",
                "currency": "usd",
                "comparables": 30,
                "positive_outcomes": 5,
            }
        },
    )
    assert len(ledger.contributions) == 1
    assert ledger.contributions[0].candidate_key == canceled.candidate_key
    assert ledger.overlap_ledger is not None
    assert ledger.overlap_ledger.records[0].reason_code == "observed_preferred"


def test_missing_currency_and_value_never_receive_a_default():
    candidate = _candidate("closed_lost_stale_deal", "hubspot_deal:d1", "c1")
    ledger = build_value_ledger(
        _candidate_set(candidate),
        {"records": [{"id": "d1", "amount_minor": "1000"}]},
    )
    assert ledger.contributions == []
    assert ledger.data_gaps[0].reason_code == "missing_currency"
    assert ledger.data_gaps[0].currency is None
    money_map = build_money_map(ledger, _candidate_set(candidate))
    assert money_map.identified_opportunity_minor == {}
    assert money_map.piles == []
    assert money_map.data_gap_count == 1


def test_independent_recompute_and_canonical_round_trip_preserve_mixed_currencies():
    usd = _candidate("failed_payment", "stripe_invoice:inv_usd", "c1")
    jpy = _candidate("silent_proposal", "proposal:p_jpy", "c2")
    unquantified = _candidate("engaged_unbooked", "customer:c3:engaged_unbooked", "c3")
    candidates = _candidate_set(usd, jpy, unquantified)
    ledger = build_value_ledger(
        candidates,
        {
            "records": [
                {"id": "inv_usd", "amount_minor": "1234", "currency": "usd"},
                {"id": "p_jpy", "amount_minor": "5678", "currency": "jpy"},
            ]
        },
    )
    money_map = build_money_map(ledger, candidates)
    recomputed: dict[str, Decimal] = {}
    for item in ledger.contributions:
        recomputed[item.currency] = recomputed.get(item.currency, Decimal(0)) + item.amount_minor
    assert dict(sorted(recomputed.items())) == money_map.identified_opportunity_minor
    assert money_map.basis_counts_by_currency["usd"].observed_event_count == 1
    assert money_map.basis_counts_by_currency["jpy"].observed_event_count == 1
    assert money_map.basis_counts_by_currency == {
        "jpy": money_map.basis_counts_by_currency["jpy"],
        "usd": money_map.basis_counts_by_currency["usd"],
    }
    assert money_map.data_gap_count == 1
    assert (
        parse_canonical_json(money_map.to_canonical_json()).to_canonical_json()
        == money_map.to_canonical_json()
    )
    # Public projections contain aggregate rows only and do not expose the
    # private value data-gap or overlap details.
    public = public_contribution_projection(ledger).to_canonical_json()
    public_map = public_money_map_projection(money_map).to_canonical_json()
    public_text = public.decode("utf-8")
    public_map_text = public_map.decode("utf-8")
    assert "customer_token" in public_text
    assert "economic_unit_key" not in public_text
    assert "economic_unit_key" not in public_map_text
    assert all(token not in public_map_text for token in ("c1", "c2", "c3"))


def test_modeled_contract_rejects_ineligible_history_and_invalid_currency():
    candidate = _candidate("silent_proposal", "proposal:p1", "c1")
    with pytest.raises(ValidationError):
        build_modeled_opportunity(
            candidate,
            average_value_minor="10000",
            currency="usd",
            comparables=30,
            positive_outcomes=5,
            cohort=" ",
            window="180 days",
        )
    with pytest.raises(ValueError):
        build_modeled_opportunity(
            candidate,
            average_value_minor="10000",
            currency="usd",
            comparables=31,
            positive_outcomes=32,
            cohort="cohort",
            window="180 days",
        )
