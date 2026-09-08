"""FM-043 Recovery Room Evidence-as-why contract tests."""

from __future__ import annotations

import re

from found_money.build import _fixture_snapshots, _run_id_for, _safe_source_config
from found_money.redaction import assert_public_safe
from found_money.rendering import (
    evidence_basis_label,
    pile_display_name,
    render_recovery_room_html,
)
from found_money.scenarios import SYNTHETIC_SERVICE_V1, run_scenario_engine
from found_money.strategy import build_thin_slice_strategized_money_map

SERVICE_PILE_NAMES = (
    "Easy Rebook",
    "Second Start",
    "Comeback Offer",
    "Proposal Wake-Up",
    "VIP Return",
)


def _service_engine():
    safe = _safe_source_config(
        source_mode="fixture", run_mode="public", fixture=SYNTHETIC_SERVICE_V1
    )
    run_id = _run_id_for(_fixture_snapshots(SYNTHETIC_SERVICE_V1), safe)
    return run_scenario_engine(run_id=run_id, safe_config=safe, fixture_id=SYNTHETIC_SERVICE_V1)


def _evidence_section(html: str) -> str:
    match = re.search(r'<section id="room-evidence".*?</section>', html, re.DOTALL)
    assert match is not None
    return match.group(0)


def test_service_evidence_names_why_each_pile_exists():
    engine = _service_engine()
    html = render_recovery_room_html(
        engine.enriched_money_map,
        engine.strategy_run.recovery_plays,
        contribution_ledger=engine.ledger,
    )
    evidence = _evidence_section(html)
    blocks = re.findall(r'<li class="evidence-pile".*?</li>', evidence, re.DOTALL)
    assert len(blocks) == 5
    for name in SERVICE_PILE_NAMES:
        assert name in re.sub(r"<[^>]+>", "", evidence)
    easy = next(block for block in blocks if "Easy Rebook" in block)
    assert "Unquantified" in easy
    assert "38700 usd" not in re.sub(r"<[^>]+>", "", evidence).casefold()
    money_map = engine.enriched_money_map
    play_ids = {play.play_id for play in engine.strategy_run.recovery_plays.plays}
    named = {pile_display_name(pile.pile_id): pile for pile in money_map.piles}
    for block in blocks:
        display = re.search(r"<h3>(.*?)</h3>", block)
        assert display is not None
        name = display.group(1)
        why = re.search(r'<p class="evidence-why">(.*?)</p>', block, re.DOTALL)
        basis = re.search(r'<p class="evidence-basis">(.*?)</p>', block)
        amount = re.search(r'<p class="evidence-amount">(.*?)</p>', block)
        confidence = re.search(r'<p class="evidence-confidence">(.*?)</p>', block)
        readiness = re.search(r'<p class="evidence-readiness">(.*?)</p>', block)
        assert why is not None and basis is not None and amount is not None
        assert confidence is not None and readiness is not None
        if name == "Easy Rebook":
            assert why.group(1) == "Source value is missing, so this pile stays unquantified."
            assert basis.group(1) == "Unquantified"
            assert amount.group(1) == "Unquantified"
            continue
        pile = named[name]
        assert why.group(1) == pile.why_recoverable
        assert basis.group(1) == evidence_basis_label(pile.value_basis)
        assert confidence.group(1) == pile.confidence_class
        expected_readiness = pile.readiness or "needs_strategy_review"
        assert readiness.group(1) == expected_readiness
        play_id = (
            pile.navigation.play_id
            if pile.navigation is not None and pile.navigation.play_id in play_ids
            else None
        )
        if play_id is not None:
            assert f'href="#{play_id}"' in block
    assert "Customer count:" in re.sub(r"<[^>]+>", "", evidence)
    assert "Source count:" in re.sub(r"<[^>]+>", "", evidence)
    assert "Overlap exclusion count:" in re.sub(r"<[^>]+>", "", evidence)
    assert evidence.count("data-recommended-action") == 1
    assert "economic_unit_key" not in re.sub(r"<[^>]+>", "", evidence)
    assert "customer_token" not in re.sub(r"<[^>]+>", "", evidence)
    find = re.search(r'<section id="room-find".*?</section>', html, re.DOTALL)
    assert find is not None
    assert "We found $387.00" in find.group(0)
    assert_public_safe(html)


def test_thin_slice_evidence_keeps_run_level_counts():
    money_map, _packet, play_set = build_thin_slice_strategized_money_map()
    html = render_recovery_room_html(money_map, play_set)
    evidence = _evidence_section(html)
    assert "Payment Rescue" in re.sub(r"<[^>]+>", "", evidence)
    assert money_map.piles[0].why_recoverable in re.sub(r"<[^>]+>", "", evidence)
    assert (
        "Observed" in re.sub(r"<[^>]+>", "", evidence)
        or "Modeled" in re.sub(r"<[^>]+>", "", evidence)
        or "Unquantified" in re.sub(r"<[^>]+>", "", evidence)
    )
    assert f"Customer count: {money_map.customer_count}" in re.sub(r"<[^>]+>", "", evidence)
    assert f"Source count: {money_map.source_count}" in re.sub(r"<[^>]+>", "", evidence)
    assert f"Overlap exclusion count: {money_map.overlap_exclusion_count}" in re.sub(
        r"<[^>]+>", "", evidence
    )
    assert "economic_unit_key" not in html
    assert "customer_token" not in html
