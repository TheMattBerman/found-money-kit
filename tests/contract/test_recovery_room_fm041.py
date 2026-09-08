"""FM-041 Recovery Room Find reveal contract tests."""

from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path

from found_money.build import _fixture_snapshots, _run_id_for, _safe_source_config
from found_money.contracts.value import format_major_units
from found_money.redaction import assert_public_safe
from found_money.rendering import (
    FIND_PROMISE,
    format_find_money,
    pile_display_name,
    render_recovery_room_html,
)
from found_money.scenarios import SYNTHETIC_SERVICE_V1, run_scenario_engine
from found_money.strategy import build_thin_slice_strategized_money_map

ROOT = Path(__file__).resolve().parents[2]
DEMO_GIF = ROOT / "assets" / "demo.gif"
README = ROOT / "README.md"


def _service_engine():
    safe = _safe_source_config(
        source_mode="fixture", run_mode="public", fixture=SYNTHETIC_SERVICE_V1
    )
    run_id = _run_id_for(_fixture_snapshots(SYNTHETIC_SERVICE_V1), safe)
    return run_scenario_engine(run_id=run_id, safe_config=safe, fixture_id=SYNTHETIC_SERVICE_V1)


def _find_section(html: str) -> str:
    match = re.search(r'<section id="room-find".*?</section>', html, re.DOTALL)
    assert match is not None
    return match.group(0)


def test_service_find_reveal_is_visible_without_javascript():
    engine = _service_engine()
    html = render_recovery_room_html(
        engine.enriched_money_map,
        engine.strategy_run.recovery_plays,
        contribution_ledger=engine.ledger,
    )
    find = _find_section(html)
    assert FIND_PROMISE in find
    assert "We found $387.00" in find
    assert "money piles" in find
    assert "recoverable customers" in find
    assert "campaigns I would launch first" in find
    assert str(engine.enriched_money_map.customer_count) in find
    assert str(len(engine.strategy_run.recovery_plays.plays)) in find
    for name in (
        "Easy Rebook",
        "Second Start",
        "Comeback Offer",
        "Proposal Wake-Up",
        "VIP Return",
    ):
        assert name in find
    assert "Unquantified" in find
    # Tier pills display modeled/recorded totals even when zero; that is honest data,
    # not a promise. The old no-$0.00 rule predates the FM-047 tier strip.
    assert "$0.00" not in find.split("tier-pill")[0]
    assert "38700 usd" not in find.casefold()
    assert 'data-pile-id="no_show_rebook"' in find
    assert 'data-pile-id="trial_no_convert"' in find
    assert 'data-pile-id="canceled_customer"' in find
    assert 'data-pile-id="silent_proposal"' in find
    assert 'data-pile-id="disappeared_high_value_customer"' in find
    assert_public_safe(html)
    payload = json.loads(engine.enriched_money_map.to_canonical_json())
    assert payload["identified_opportunity_minor"]["usd"] == "38700"


def test_thin_slice_find_uses_major_units_and_display_name():
    money_map, _packet, play_set = build_thin_slice_strategized_money_map()
    html = render_recovery_room_html(money_map, play_set)
    find = _find_section(html)
    assert "We found $49.00" in find
    assert "Payment Rescue" in find
    assert FIND_PROMISE in find
    assert 'data-pile-id="payment_rescue"' in find
    assert "4900 usd" not in find.casefold()


def test_find_formats_jpy_and_kwd_without_fx_blend():
    money_map, _packet, play_set = build_thin_slice_strategized_money_map()
    mixed = money_map.model_copy(
        update={
            "identified_opportunity_minor": {
                "jpy": Decimal(1234),
                "kwd": Decimal(1234),
            }
        }
    )
    html = render_recovery_room_html(mixed, play_set)
    find = _find_section(html)
    assert "JPY 1,234" in find
    assert f"KWD {format_major_units(1234, 'kwd')}" in find
    assert format_find_money(1234, "jpy") == "JPY 1,234"
    assert format_find_money(1234, "kwd") == "KWD 1.234"
    assert "We found" in find


def test_locked_pile_display_names_and_demo_gif_caption():
    assert pile_display_name("payment_rescue") == "Payment Rescue"
    assert pile_display_name("failed_payment") == "Payment Rescue"
    assert pile_display_name("no_show_rebook") == "Easy Rebook"
    gif = DEMO_GIF.read_bytes()
    assert b"We found $387.00" in gif
    readme = README.read_text(encoding="utf-8")
    assert "Recovery Room" in readme
    assert "assets/launch-room.png" in readme
    assert "4900 usd" not in readme
