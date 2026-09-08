"""FM-024 explainable ranking and Money Map navigation contracts."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from playwright.sync_api import sync_playwright

from found_money.contracts.events import RecoveryCandidateSetV1, RecoveryCandidateV1
from found_money.contracts.ranking import RANK_FACTOR_NAMES
from found_money.contracts.strategy import RecoveryPlaySetV1
from found_money.contracts.value import ContributionLedgerV1, ContributionV1
from found_money.map import build_money_map, public_money_map_projection
from found_money.rendering import render_money_map_html
from found_money.strategy import apply_recovery_plays_to_money_map

UTC = timezone.utc
RUN_ID = "run_fm024_ranking"


def _candidate(
    family: str,
    unit: str,
    customer: str,
    *,
    source: str,
    event_at: str | None = None,
    confidence: str = "observed",
    qualifying: dict[str, str] | None = None,
    evidence_references: list[str] | None = None,
) -> RecoveryCandidateV1:
    payload: dict[str, object] = {
        "run_id": RUN_ID,
        "event_family": family,
        "confidence_class": confidence,
        "economic_unit_key": unit,
        "customer_token": customer,
        "lineage": {"source_reference": source},
        "qualifying_evidence": qualifying or {"event_family": family},
    }
    if event_at is not None:
        payload["event_at"] = event_at
    if evidence_references is not None:
        payload["evidence_references"] = evidence_references
    return RecoveryCandidateV1.model_validate(payload)


def _candidate_set(*candidates: RecoveryCandidateV1) -> RecoveryCandidateSetV1:
    return RecoveryCandidateSetV1(
        run_id=RUN_ID,
        built_at="2026-07-29T18:00:10.000Z",
        candidates=list(candidates),
    )


def _contribution(
    candidate: RecoveryCandidateV1,
    *,
    pile_id: str,
    amount: str,
    currency: str = "usd",
) -> ContributionV1:
    return ContributionV1.model_validate(
        {
            "economic_unit_key": candidate.economic_unit_key,
            "pile_id": pile_id,
            "value_basis": "observed_face_value",
            "currency": currency,
            "amount_minor": amount,
            "customer_token": candidate.customer_token,
            "candidate_economic_unit_key": candidate.economic_unit_key,
            "candidate_key": candidate.candidate_key,
        }
    )


def _ledger(*contributions: ContributionV1) -> ContributionLedgerV1:
    return ContributionLedgerV1(
        run_id=RUN_ID,
        built_at="2026-07-29T18:00:15.000Z",
        contributions=list(contributions),
    )


def _fixture() -> tuple[ContributionLedgerV1, RecoveryCandidateSetV1]:
    failed = _candidate(
        "failed_payment",
        "stripe_invoice:inv_1",
        "customer_1",
        source="stripe:invoice:inv_1",
        event_at="2026-07-25T00:00:00Z",
        qualifying={
            "event_family": "failed_payment",
            "status": "open",
            "offer_fit": "0.80",
            "friction": "0.20",
        },
        evidence_references=["stripe/invoices/failed"],
    )
    proposal = _candidate(
        "silent_proposal",
        "proposal:p_1",
        "customer_2",
        source="hubspot:deal:p_1",
        event_at="2026-07-20T00:00:00Z",
        confidence="derived",
        qualifying={
            "event_family": "silent_proposal",
            "status": "silent",
            "offer_fit": "0.40",
            "friction": "0.40",
        },
        evidence_references=["hubspot/deals/silent"],
    )
    return (
        _ledger(
            _contribution(failed, pile_id="payment_rescue", amount="9000"),
            _contribution(proposal, pile_id="silent_proposal", amount="9000"),
        ),
        _candidate_set(failed, proposal),
    )


def _play_set(run_id: str = RUN_ID) -> RecoveryPlaySetV1:
    return RecoveryPlaySetV1.model_validate(
        {
            "schema_version": "recovery-plays.v1",
            "run_id": run_id,
            "built_at": "2026-07-29T18:00:30.000Z",
            "provider": "fixture",
            "plays": [
                {
                    "play_id": "payment_rescue_dunning",
                    "pile_id": "payment_rescue",
                    "rank": 1,
                    "title": "Payment rescue dunning",
                    "rationale": "Human review of an observed payment pile",
                    "recommended_actions": ["Review the linked play before outreach"],
                }
            ],
        }
    )


def test_rank_explanation_has_exact_factors_weights_and_contributions():
    ledger, candidates = _fixture()
    money_map = build_money_map(ledger, candidates)

    assert [pile.pile_id for pile in money_map.piles] == [
        "payment_rescue",
        "silent_proposal",
    ]
    explanation = money_map.piles[0].rank_explanation
    assert explanation is not None
    assert [factor.name for factor in explanation.factors] == list(RANK_FACTOR_NAMES)
    assert sum(explanation.weights.values(), Decimal(0)) == Decimal(100)
    assert explanation.tie_break_key == "payment_rescue|usd"

    factors = {factor.name: factor for factor in explanation.factors}
    assert factors["value"].score == Decimal("1.000000")
    assert factors["event_strength"].score == Decimal("1.000000")
    assert factors["recency"].score == Decimal("0.977778")
    assert factors["reachable_count"].score == Decimal("1.000000")
    assert factors["offer_fit"].score == Decimal("0.800000")
    assert factors["friction"].score == Decimal("0.800000")
    assert factors["proof_context"].score == Decimal("1.000000")
    assert factors["readiness"].score == Decimal("1.000000")
    assert explanation.total_score == Decimal("96.666670")
    for factor in explanation.factors:
        assert factor.contribution == factor.score * factor.weight
        if factor.score > 0:
            assert factor.source_count > 0
            assert factor.evidence_references


def test_ranking_is_permutation_stable_and_uses_explicit_tie_break_key():
    ledger, candidates = _fixture()
    reversed_ledger = _ledger(*reversed(ledger.contributions))
    reversed_candidates = _candidate_set(*reversed(candidates.candidates))

    first = build_money_map(ledger, candidates)
    second = build_money_map(reversed_ledger, reversed_candidates)
    assert first.to_canonical_json() == second.to_canonical_json()

    first_candidate = _candidate(
        "failed_payment",
        "stripe_invoice:inv_a",
        "customer_a",
        source="stripe:invoice:inv_a",
    )
    second_candidate = _candidate(
        "failed_payment",
        "stripe_invoice:inv_b",
        "customer_b",
        source="stripe:invoice:inv_b",
    )
    tied = build_money_map(
        _ledger(
            _contribution(first_candidate, pile_id="payment_rescue", amount="1000"),
            _contribution(second_candidate, pile_id="failed_payment", amount="1000"),
        ),
        _candidate_set(first_candidate, second_candidate),
    )
    assert [pile.pile_id for pile in tied.piles] == ["failed_payment", "payment_rescue"]
    assert [pile.rank for pile in tied.piles] == [1, 2]


def test_missing_optional_factor_evidence_fails_closed_without_opaque_rank():
    candidate = _candidate(
        "silent_proposal",
        "proposal:p_missing",
        "customer_missing",
        source="hubspot:proposal:p_missing",
        confidence="derived",
        qualifying={"event_family": "silent_proposal"},
    )
    money_map = build_money_map(
        _ledger(_contribution(candidate, pile_id="silent_proposal", amount="1200")),
        _candidate_set(candidate),
    )
    explanation = money_map.piles[0].rank_explanation
    assert explanation is not None
    factors = {factor.name: factor for factor in explanation.factors}
    for name in ("recency", "offer_fit", "friction"):
        assert factors[name].score == Decimal(0)
        assert factors[name].source_count == 0
        assert factors[name].evidence_references == []
    assert explanation.total_score == sum(
        (factor.contribution for factor in explanation.factors), Decimal(0)
    )
    assert money_map.piles[0].readiness == "ready_for_strategy"
    assert money_map.piles[0].navigation is not None
    assert money_map.piles[0].navigation.state == "deferred"


def test_public_projection_and_html_keep_explanations_aggregate_safe():
    ledger, candidates = _fixture()
    money_map = build_money_map(ledger, candidates)
    public_text = public_money_map_projection(money_map).to_canonical_json().decode()
    assert "rank_explanation" not in public_text
    assert "evidence_references" not in public_text
    assert "stripe_invoice:inv_1" not in public_text
    assert '"navigation"' in public_text
    assert '"state":"deferred"' in public_text

    enriched = apply_recovery_plays_to_money_map(money_map, _play_set())
    assert enriched.piles[0].navigation is not None
    assert enriched.piles[0].navigation.state == "available"
    assert enriched.piles[0].navigation.play_id == "payment_rescue_dunning"
    html = render_money_map_html(enriched)
    assert "Totals" in html
    assert "Data gaps and exclusions" in html
    assert "Rank explanation" in html
    assert 'href="#pile/payment_rescue/usd"' in html
    assert "stripe_invoice:inv_1" not in html


def test_compatibility_html_playwright_labels_and_pile_link():
    ledger, candidates = _fixture()
    money_map = build_money_map(ledger, candidates)
    enriched = apply_recovery_plays_to_money_map(money_map, _play_set())
    html = render_money_map_html(enriched)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.set_content(html)
        assert page.locator("main").count() == 1
        for label in ("Totals", "Data gaps and exclusions", "Rank explanation"):
            assert page.get_by_text(label, exact=True).count() >= 1
        link = page.locator('a[href="#pile/payment_rescue/usd"]')
        assert link.count() == 1
        link.click()
        assert page.url.endswith("#pile/payment_rescue/usd")
        assert page.locator('[id="pile/payment_rescue/usd"]').count() == 1
        browser.close()


def test_navigation_is_deferred_without_plays_and_ambiguous_links_fail_closed():
    ledger, candidates = _fixture()
    money_map = build_money_map(ledger, candidates)
    empty_play_set = RecoveryPlaySetV1.model_validate(
        {
            "schema_version": "recovery-plays.v1",
            "run_id": RUN_ID,
            "built_at": "2026-07-29T18:00:30.000Z",
            "provider": "fixture",
            "plays": [],
        }
    )
    deferred = apply_recovery_plays_to_money_map(money_map, empty_play_set)
    assert all(pile.navigation is not None for pile in deferred.piles)
    assert all(pile.navigation.state == "deferred" for pile in deferred.piles if pile.navigation)
    assert deferred.next_action == "review_top_ranked_pile"

    first = _candidate(
        "failed_payment",
        "stripe_invoice:inv_usd",
        "customer_usd",
        source="stripe:invoice:inv_usd",
    )
    second = _candidate(
        "failed_payment",
        "stripe_invoice:inv_eur",
        "customer_eur",
        source="stripe:invoice:inv_eur",
    )
    ambiguous = build_money_map(
        _ledger(
            _contribution(first, pile_id="payment_rescue", amount="1000", currency="usd"),
            _contribution(second, pile_id="payment_rescue", amount="1000", currency="eur"),
        ),
        _candidate_set(first, second),
    )
    with pytest.raises(ValueError, match="ambiguous pile_id"):
        apply_recovery_plays_to_money_map(ambiguous, _play_set())


def test_rank_inputs_are_utc_and_never_need_network(monkeypatch):
    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr("socket.create_connection", boom)
    monkeypatch.setattr("socket.socket.connect", boom)
    ledger, candidates = _fixture()
    money_map = build_money_map(ledger, candidates, built_at=datetime(2026, 7, 29, 18, tzinfo=UTC))
    assert money_map.built_at.tzinfo == UTC
    assert money_map.piles
