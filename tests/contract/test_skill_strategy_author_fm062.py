"""Contract and property tests for FM-062: Strategy from the corpus skill, not a frozen script."""

import json
from decimal import Decimal
from datetime import datetime, timezone
import pytest

from found_money.contracts.strategy import (
    StrategyBusinessProfileV1,
)
from found_money.contracts.map import MoneyMapV1, MoneyMapPileV1
from found_money.contracts.ranking import (
    DEFAULT_RANK_WEIGHTS,
    RANK_FACTOR_NAMES,
    RankExplanationV1,
    RankFactorV1,
    quantize_rank,
)
from found_money.profile import BusinessProfileV1, adapt_business_profile
from found_money.strategy.author import (
    generate_recovery_plays,
)
from found_money.strategy.campaign import (
    validate_complete_recovery_play_set,
)
from found_money.strategy.boundary import build_grounded_strategy_packet
from found_money.strategy.intelligence import (
    no_email_play_for,
    reengage_days_for,
    touch_ceiling_for,
    warmth_for,
)
from found_money.strategy.card_checks import card_violations
from found_money.strategy.offer_ladder_checks import offer_ladder_set_violations
from found_money.build import build


@pytest.fixture
def base_profile() -> StrategyBusinessProfileV1:
    return StrategyBusinessProfileV1(
        product="Enterprise Cloud Analytics",
        proof="Verified case study: 40% efficiency improvement",
        margin="65 percent gross margin",
        channel="Email and executive outreach",
        capacity="25 enterprise reviews per week",
        destination="account review portal",
    )


def _mock_explanation(pile_id: str, currency: str) -> RankExplanationV1:
    factors = []
    total_score = Decimal("0.000000")
    for name in RANK_FACTOR_NAMES:
        weight = DEFAULT_RANK_WEIGHTS[name]
        if name == "value":
            score = Decimal("1.000000")
            contribution = quantize_rank(score * weight)
            source_count = 1
            refs = ["ev_1"]
        else:
            score = Decimal("0.000000")
            contribution = Decimal("0.000000")
            source_count = 0
            refs = []
        total_score += contribution
        factors.append(
            RankFactorV1(
                name=name,
                score=score,
                weight=weight,
                contribution=contribution,
                source_count=source_count,
                explanation=f"mock {name} explanation",
                evidence_references=refs,
            )
        )
    return RankExplanationV1(
        total_score=quantize_rank(total_score),
        weights=DEFAULT_RANK_WEIGHTS,
        factors=factors,
        tie_break_key=f"{pile_id}|{currency}",
    )


def _make_mock_money_map(piles_spec: list[tuple[str, int, int]]) -> MoneyMapV1:
    """piles_spec: list of (pile_id, rank, amount_minor)"""
    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    piles = [
        MoneyMapPileV1(
            pile_id=p_id,
            rank=rank,
            currency="usd",
            selected_value_minor=amount,
            value_basis="observed_face_value",
            confidence_class="observed",
            customer_count=10,
            economic_unit_count=10,
            why_recoverable="Direct recovery opportunity based on documented records",
            readiness="ready_for_strategy",
            rank_explanation=_mock_explanation(p_id, "usd"),
        )
        for p_id, rank, amount in piles_spec
    ]
    total_minor = sum(amount for _, _, amount in piles_spec)
    return MoneyMapV1(
        run_id="run_test_fm062",
        built_at=now,
        strategy_stage="not_started",
        identified_opportunity_minor={"usd": total_minor},
        basis_counts_by_currency={},
        piles=piles,
        recommended_play_ids=[],
        customer_count=len(piles_spec) * 10,
        source_count=2,
        data_gap_count=0,
        overlap_exclusion_count=0,
    )


# AC-1: Table rule tests for canceled_customer and closed_lost_stale_deal
def test_ac1_canceled_customer_table_rules():
    assert touch_ceiling_for("canceled_customer") == 2
    assert reengage_days_for("canceled_customer") == 90
    assert warmth_for("canceled_customer") == "warm"
    assert not no_email_play_for("canceled_customer")


def test_ac1_closed_lost_stale_deal_table_rules():
    assert no_email_play_for("closed_lost_stale_deal") is True


# AC-2: Pile inclusion & conditioning tests
def test_ac2_pile_inclusion_generates_play(base_profile):
    m_map = _make_mock_money_map(
        [
            ("payment_rescue", 1, 150000),
            ("canceled_customer", 2, 250000),
            ("disappeared_high_value_customer", 3, 350000),
        ]
    )
    packet = build_grounded_strategy_packet(m_map, base_profile)
    play_set = generate_recovery_plays(packet, base_profile, money_map=m_map)
    pile_ids = [p.pile_id for p in play_set.plays]
    assert "canceled_customer" in pile_ids
    assert "payment_rescue" in pile_ids
    assert "disappeared_high_value_customer" in pile_ids


def test_ac2_pile_omission_generates_no_play(base_profile):
    m_map = _make_mock_money_map(
        [
            ("payment_rescue", 1, 150000),
            ("disappeared_high_value_customer", 2, 350000),
        ]
    )
    packet = build_grounded_strategy_packet(m_map, base_profile)
    play_set = generate_recovery_plays(packet, base_profile, money_map=m_map)
    pile_ids = [p.pile_id for p in play_set.plays]
    assert "canceled_customer" not in pile_ids
    assert "payment_rescue" in pile_ids
    assert "disappeared_high_value_customer" in pile_ids
    assert len(play_set.plays) == 3


# AC-3: Full validation of skill-authored recovery play set
def test_ac3_full_validation_of_authored_plays(base_profile):
    m_map = _make_mock_money_map(
        [
            ("payment_rescue", 1, 150000),
            ("canceled_customer", 2, 250000),
            ("disappeared_high_value_customer", 3, 350000),
        ]
    )
    packet = build_grounded_strategy_packet(m_map, base_profile)
    play_set = generate_recovery_plays(packet, base_profile, money_map=m_map)

    # Assert deterministic card violations is empty
    violations = card_violations(play_set)
    assert not violations, f"Card violations found: {violations}"

    # Assert offer ladder violations is empty
    ladder_violations = offer_ladder_set_violations(play_set)
    assert not ladder_violations, f"Offer ladder violations found: {ladder_violations}"

    # Validate against complete recovery play set schema and evidence grounding
    validate_complete_recovery_play_set(play_set, packet)


# AC-5: Canceled customer prohibition enforcement
def test_ac5_canceled_customer_prohibits_payment_glitch_framing(base_profile):
    m_map = _make_mock_money_map(
        [
            ("canceled_customer", 1, 250000),
        ]
    )
    packet = build_grounded_strategy_packet(m_map, base_profile)
    play_set = generate_recovery_plays(packet, base_profile, money_map=m_map)
    canceled_play = next(p for p in play_set.plays if p.pile_id == "canceled_customer")

    # Assert sequence ceiling is exactly 2
    assert len(canceled_play.email_sequence) == 2
    # Assert reengage days is 90
    assert canceled_play.sequence_plan.reengage_days == 90

    # Verify no payment glitch framing in copy
    canonical_json = canceled_play.to_canonical_json().decode("utf-8").lower()
    assert "payment glitch" not in canonical_json
    assert "card on file" not in canonical_json


# AC-7: BusinessProfileV1 adapter to StrategyBusinessProfileV1
def test_ac7_business_profile_adapter():
    intake = BusinessProfileV1(
        product="Cloud SaaS Platform",
        proof="Case study with 500+ seats",
        margin_percent=Decimal("80"),
        capacity=50,
        destination="customer billing center",
        business_model="saas",
        tenure_multiple_months=Decimal("12"),
    )
    adapted = adapt_business_profile(intake)
    assert isinstance(adapted, StrategyBusinessProfileV1)
    assert adapted.product == "Cloud SaaS Platform"
    assert adapted.proof == "Case study with 500+ seats"
    assert adapted.margin == "80 percent contribution margin"
    assert adapted.channel == "email and sms with a human review task"
    assert adapted.capacity == "50 recovery reviews per week"
    assert adapted.destination == "customer billing center"
    assert adapted.business_model == "saas"
    assert adapted.tenure_multiple_months == Decimal("12")


# AC-4: Unconfigured strategy hold and empty plays
def test_ac4_unconfigured_strategy_hold_and_empty_plays(monkeypatch, tmp_path):
    from pathlib import Path
    from found_money.rendering.proof import (
        FOUR_ROOM_PNGS,
        PDF_SIGNATURE,
        PNG_SIGNATURE,
        PRINT_REPORT_CONTACT_SHEET,
        REQUIRED_ARTIFACTS,
    )
    import found_money.build as build_module

    ROOT = Path(__file__).resolve().parents[2]
    FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "saas" / "thin-slice"

    def _fake_proof_capture(*_args, **_kwargs):
        artifacts = {
            name: (PNG_SIGNATURE + b"fake-png")
            if name.endswith(".png")
            else (PDF_SIGNATURE + b"1.4\n")
            for name in REQUIRED_ARTIFACTS
        }
        artifacts[PRINT_REPORT_CONTACT_SHEET] = PNG_SIGNATURE + b"fake-contact-sheet"
        artifacts.update(
            {
                f"print-report-page-{page:02d}.png": PNG_SIGNATURE + f"fake-page-{page}".encode()
                for page in range(1, 20)
            }
        )
        return artifacts

    monkeypatch.setattr(build_module, "capture_recovery_room_artifacts", _fake_proof_capture)
    monkeypatch.setattr(build_module, "validate_print_review_packet", _fake_proof_capture)
    monkeypatch.setattr(
        build_module,
        "capture_four_room_screenshots",
        lambda *_args, **_kwargs: {name: PNG_SIGNATURE + b"fake-png" for name in FOUR_ROOM_PNGS},
    )

    monkeypatch.chdir(tmp_path)
    cfg_file = tmp_path / "source_file_mode.json"
    cfg_file.write_text(
        json.dumps(
            {
                "schema_version": "found-money-build-source.v1",
                "mode": "file",
                "credentials": {"credential_mode": "none", "runtime_mode": "test", "scopes": []},
                "sources": {
                    "hubspot_snapshot": str(FIXTURE_ROOT / "hubspot" / "snapshot.json"),
                    "stripe_snapshot": str(FIXTURE_ROOT / "stripe" / "snapshot.json"),
                },
            }
        ),
        encoding="utf-8",
    )
    out_dir = tmp_path / "out"
    build(output_root="out", source_config=cfg_file)

    # Verify money map strategy stage
    m_map_data = json.loads((out_dir / "money-map.json").read_text())
    assert m_map_data["strategy_stage"] == "needs_strategy_review"

    # Verify recovery-plays.json has empty plays
    plays_data = json.loads((out_dir / "recovery-plays.json").read_text())
    assert plays_data["plays"] == []

    # Verify recovery room html displays strategy status deferred message
    index_html = (out_dir / "index.html").read_text()
    assert "Strategy status: deferred. No generic fallback play was created." in index_html


# AC-6: File mode with stub/skill provider verifies money map values match plays
def test_ac6_file_mode_money_map_values_match_plays(monkeypatch, tmp_path):
    from pathlib import Path
    from found_money.rendering.proof import (
        FOUR_ROOM_PNGS,
        PDF_SIGNATURE,
        PNG_SIGNATURE,
        PRINT_REPORT_CONTACT_SHEET,
        REQUIRED_ARTIFACTS,
    )
    import found_money.build as build_module

    ROOT = Path(__file__).resolve().parents[2]
    FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "saas" / "thin-slice"

    def _fake_proof_capture(*_args, **_kwargs):
        artifacts = {
            name: (PNG_SIGNATURE + b"fake-png")
            if name.endswith(".png")
            else (PDF_SIGNATURE + b"1.4\n")
            for name in REQUIRED_ARTIFACTS
        }
        artifacts[PRINT_REPORT_CONTACT_SHEET] = PNG_SIGNATURE + b"fake-contact-sheet"
        artifacts.update(
            {
                f"print-report-page-{page:02d}.png": PNG_SIGNATURE + f"fake-page-{page}".encode()
                for page in range(1, 20)
            }
        )
        return artifacts

    monkeypatch.setattr(build_module, "capture_recovery_room_artifacts", _fake_proof_capture)
    monkeypatch.setattr(build_module, "validate_print_review_packet", _fake_proof_capture)
    monkeypatch.setattr(
        build_module,
        "capture_four_room_screenshots",
        lambda *_args, **_kwargs: {name: PNG_SIGNATURE + b"fake-png" for name in FOUR_ROOM_PNGS},
    )

    monkeypatch.chdir(tmp_path)
    # A later successful payment is correctly suppressed by the full file detector.
    # Keep only the failed invoice for this strategy-value contract.
    stripe_data = json.loads((FIXTURE_ROOT / "stripe/snapshot.json").read_text())
    stripe_data["invoices"] = [
        r for r in stripe_data["invoices"] if r.get("collection_outcome") == "payment_failed"
    ]
    stripe_file = tmp_path / "stripe.json"
    stripe_file.write_text(json.dumps(stripe_data))
    cfg_file = tmp_path / "source_stub_mode.json"
    cfg_file.write_text(
        json.dumps(
            {
                "schema_version": "found-money-build-source.v1",
                "mode": "file",
                "strategy_provider": "stub",
                "credentials": {"credential_mode": "none", "runtime_mode": "test", "scopes": []},
                "sources": {
                    "hubspot_snapshot": str(FIXTURE_ROOT / "hubspot" / "snapshot.json"),
                    "stripe_snapshot": str(stripe_file),
                },
            }
        ),
        encoding="utf-8",
    )
    out_dir = tmp_path / "out_stub"
    build(output_root="out_stub", source_config=cfg_file)

    # Verify plays were generated
    plays_data = json.loads((out_dir / "recovery-plays.json").read_text())
    assert len(plays_data["plays"]) == 3
    assert plays_data["provider"] == "stub"
