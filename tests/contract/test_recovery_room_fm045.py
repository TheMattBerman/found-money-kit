"""FM-045 print packet matches the Find reveal language."""

from __future__ import annotations

from pathlib import Path

from pypdf import PdfReader

from found_money.build import _fixture_snapshots, _run_id_for, _safe_source_config, build
from found_money.contracts.value import format_major_units
from found_money.rendering import (
    FIND_PROMISE,
    format_find_money,
    pile_display_name,
    render_print_report_html,
)
from found_money.scenarios import SYNTHETIC_SERVICE_V1, run_scenario_engine

ROOT = Path(__file__).resolve().parents[2]
SERVICE_CONFIG = ROOT / "configs" / "synthetic-service-v1.json"
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


def _pdf_text(path: Path) -> str:
    return "\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)


def _collapsed(text: str) -> str:
    return " ".join(text.split())


def test_service_print_html_matches_find_reveal_language():
    engine = _service_engine()
    html = render_print_report_html(
        engine.enriched_money_map,
        engine.strategy_run.recovery_plays,
        contribution_ledger=engine.ledger,
    )
    assert FIND_PROMISE in html
    assert "$387.00" in html
    assert format_find_money(38700, "usd") == "$387.00"
    assert "38700 usd" not in html.casefold()
    cover = html.split('data-print-page="cover"', 1)[1].split("</section>", 1)[0]
    assert "$387.00" in cover
    assert FIND_PROMISE in cover
    assert "ranked piles" in cover
    assert "customers in scope" in cover
    assert "Recovery Plays" in cover
    assert "38700 usd" not in cover.casefold()
    assert "recovered revenue" not in cover.casefold()
    assert "recovered" not in cover.casefold()
    assert len(engine.strategy_run.recovery_plays.plays) == 3
    for play in engine.strategy_run.recovery_plays.plays:
        assert play.title in html
        assert play.diagnosis.text in html
        core = next(rung for rung in play.offer_ladder.rungs if rung.role == "core")
        assert core.mechanism.text in html
        assert play.primary_cta.text in html
        for item in play.email_sequence:
            assert item.subject.text in html
    for name in SERVICE_PILE_NAMES:
        assert name in html
    assert "Easy Rebook" in html and "Unquantified" in html
    assert "https://" not in html


def test_service_build_print_pdf_matches_reveal(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = build(output_root="service-v1", source_config=SERVICE_CONFIG)
    pdf_path = result.output_root / "print-report.pdf"
    text = _pdf_text(pdf_path)
    collapsed = _collapsed(text)
    lowered = collapsed.casefold()
    assert "$387.00" in text
    assert FIND_PROMISE in text
    assert "38700 usd" not in lowered
    assert "recovered revenue" not in text[:900].casefold()
    for name in SERVICE_PILE_NAMES:
        assert name in text
    engine = _service_engine()
    for play in engine.strategy_run.recovery_plays.plays:
        assert play.title in collapsed
        assert play.diagnosis.text in collapsed
        core = next(rung for rung in play.offer_ladder.rungs if rung.role == "core")
        assert core.mechanism.text in collapsed
        assert play.primary_cta.text in collapsed
        for item in play.email_sequence:
            assert item.subject.text in collapsed


def test_print_cover_uses_major_units_not_minor_headline():
    engine = _service_engine()
    money_map = engine.enriched_money_map
    html = render_print_report_html(
        money_map,
        engine.strategy_run.recovery_plays,
        contribution_ledger=engine.ledger,
    )
    cover = html.split('data-print-page="cover"', 1)[1].split("</section>", 1)[0]
    for currency, amount in money_map.identified_opportunity_minor.items():
        major = f"${format_major_units(amount, currency)}"
        assert major in cover
        assert f"{amount} {currency}" not in cover.casefold()
    assert money_map.customer_count is not None
    assert str(money_map.customer_count) in cover
    assert str(len(engine.strategy_run.recovery_plays.plays)) in cover
    for pile in money_map.piles:
        assert pile_display_name(pile.pile_id) in html
    assert "Easy Rebook" in html
