"""FM-047 Recovery Room Find spotlight and Play navigation contract tests."""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pytest

from found_money.build import _fixture_snapshots, _run_id_for, _safe_source_config
from found_money.redaction import assert_public_safe
from found_money.rendering import FIND_WALKTHROUGH_NEXT, render_recovery_room_html
from found_money.scenarios import (
    SYNTHETIC_ECOMMERCE_V1,
    SYNTHETIC_SAAS_V1,
    SYNTHETIC_SERVICE_V1,
    run_scenario_engine,
)
from found_money.strategy import build_thin_slice_strategized_money_map

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "found_money" / "rendering" / "templates" / "recovery_room.html"
PROTOTYPE_CLAIMS = (
    "recovers 80%",
    "50% off",
    "60-second",
    "60 second",
)
PROTOTYPE_EMOJI = ("👑", "🛡️", "💰", "📋", "🎓", "⚡", "🔒", "👈")


def _engine(fixture_id: str):
    safe = _safe_source_config(source_mode="fixture", run_mode="public", fixture=fixture_id)
    run_id = _run_id_for(_fixture_snapshots(fixture_id), safe)
    return run_scenario_engine(run_id=run_id, safe_config=safe, fixture_id=fixture_id)


def _service_engine():
    return _engine(SYNTHETIC_SERVICE_V1)


def _find_section(html: str) -> str:
    match = re.search(r'<section id="room-find".*?</section>', html, re.DOTALL)
    assert match is not None
    return match.group(0)


def _play_section(html: str) -> str:
    match = re.search(r'<section id="room-play".*?</section>', html, re.DOTALL)
    assert match is not None
    return match.group(0)


def _share_percent(amount_minor: Decimal, total_minor: Decimal) -> int:
    return int(
        (amount_minor * Decimal(100) / total_minor).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )


def test_service_find_spotlights_rank_one_and_queues_the_rest():
    engine = _service_engine()
    html = render_recovery_room_html(
        engine.enriched_money_map,
        engine.strategy_run.recovery_plays,
        contribution_ledger=engine.ledger,
    )
    find = _find_section(html)
    spotlight = re.search(r'<ol class="find-piles find-spotlight">.*?</ol>', find, re.DOTALL)
    queue = re.search(
        r'<div class="find-queue"[^>]*>.*?</div>\s*<h3>Owner walkthrough</h3>',
        find,
        re.DOTALL,
    )
    assert spotlight is not None
    assert queue is not None
    assert 'data-rank="1"' in spotlight.group(0)
    assert 'data-pile-id="disappeared_high_value_customer"' in spotlight.group(0)
    assert "VIP Return" in spotlight.group(0)
    assert "$220.00" in spotlight.group(0)
    assert "find-pile-bar" in spotlight.group(0)
    assert 'href="#' in spotlight.group(0)
    assert 'tabindex="0"' in queue.group(0)
    assert "aria-label=" in queue.group(0)
    queued_ranks = re.findall(r'data-rank="(\d+)"', queue.group(0))
    assert queued_ranks == ["2", "3", "4", "5"]
    for pile in engine.enriched_money_map.piles:
        assert f'id="pile-{pile.pile_id}-{pile.currency}"' in find
        assert f'data-pile-id="{pile.pile_id}"' in find
        assert f'data-rank="{pile.rank}"' in find
        if pile.selected_value_minor is not None:
            assert f'data-selected-value-minor="{pile.selected_value_minor}"' in find
        assert f'id="pile-{pile.rank}"' in find
    assert 'data-pile-id="no_show_rebook"' in find
    assert 'id="pile-5"' in find
    walkthrough = re.search(r'<ol class="find-walkthrough">(.*?)</ol>', find, re.DOTALL)
    assert walkthrough is not None
    steps = re.findall(r"<li>.*?</li>", walkthrough.group(1), re.DOTALL)
    assert len(steps) == 3
    top = min(engine.enriched_money_map.piles, key=lambda item: item.rank)
    assert top.why_recoverable in walkthrough.group(1)
    selected = next(
        play for play in engine.strategy_run.recovery_plays.plays if play.pile_id == top.pile_id
    )
    core = next(rung for rung in selected.offer_ladder.rungs if rung.role == "core")
    for item in core.constraints:
        assert item.text in walkthrough.group(1)
    assert FIND_WALKTHROUGH_NEXT in walkthrough.group(1)
    lowered = html.casefold()
    for claim in PROTOTYPE_CLAIMS:
        assert claim not in lowered
    for glyph in PROTOTYPE_EMOJI:
        assert glyph not in html
    assert_public_safe(html)


def test_share_of_total_badge_tracks_rendered_pile_totals():
    engine = _service_engine()
    html = render_recovery_room_html(
        engine.enriched_money_map,
        engine.strategy_run.recovery_plays,
        contribution_ledger=engine.ledger,
    )
    find = _find_section(html)
    quantified = [pile.selected_value_minor for pile in engine.enriched_money_map.piles]
    total = sum(quantified, Decimal(0))
    top = min(engine.enriched_money_map.piles, key=lambda item: item.rank)
    expected = f"{_share_percent(top.selected_value_minor, total)}% of the find"
    spotlight = re.search(r'<ol class="find-piles find-spotlight">.*?</ol>', find, re.DOTALL)
    assert spotlight is not None
    share = re.search(r'<span class="find-pile-share">(.*?)</span>', spotlight.group(0))
    assert share is not None
    assert share.group(1) == expected
    assert expected == "57% of the find"

    dropped = next(pile for pile in engine.enriched_money_map.piles if pile.rank == 2)
    reduced_map = engine.enriched_money_map.model_copy(
        update={"piles": [pile for pile in engine.enriched_money_map.piles if pile.rank != 2]}
    )
    reduced_html = render_recovery_room_html(
        reduced_map,
        engine.strategy_run.recovery_plays,
        contribution_ledger=engine.ledger,
    )
    reduced_find = _find_section(reduced_html)
    reduced_total = total - dropped.selected_value_minor
    reduced_expected = f"{_share_percent(top.selected_value_minor, reduced_total)}% of the find"
    reduced_share = re.search(r'<span class="find-pile-share">(.*?)</span>', reduced_find)
    assert reduced_share is not None
    assert reduced_share.group(1) == reduced_expected
    assert reduced_share.group(1) != share.group(1)


def test_plays_stay_fully_visible_and_copy_controls_are_enhancement_only():
    engine = _service_engine()
    html = render_recovery_room_html(
        engine.enriched_money_map,
        engine.strategy_run.recovery_plays,
        contribution_ledger=engine.ledger,
    )
    play = _play_section(html)
    assert 'class="play-angles"' in play
    for item in engine.strategy_run.recovery_plays.plays:
        article = re.search(
            rf'<article id="{re.escape(item.play_id)}".*?</article>', play, re.DOTALL
        )
        assert article is not None
        body = article.group(0)
        assert f'data-play-id="{item.play_id}"' in body
        assert f'href="#{item.play_id}"' in play
        for email in item.email_sequence:
            assert email.subject.text in body
            assert email.body.text in body
            assert email.cta.text in body
        for card in item.concept_cards:
            assert f'id="{card.card_id}"' in body
        assert " hidden" not in body
        assert "display:none" not in body.replace(" ", "")
    buttons = re.findall(r"<button\b([^>]*)>(.*?)</button>", play, re.DOTALL)
    assert buttons
    for attrs, label in buttons:
        assert 'type="button"' in attrs
        assert "disabled" in attrs
        assert "copy-email" in attrs
        assert "aria-label=" in attrs
        lowered = f"{attrs} {label}".casefold()
        assert "send" not in lowered
        assert "schedule" not in lowered
        assert "dispatch" not in lowered
    assert html.count("<h1") == 1
    for room_id in ("room-find", "room-evidence", "room-play", "room-launch"):
        assert html.count(f'<section id="{room_id}"') == 1
    assert 'id="stealads-instructions"' in html
    assert 'id="matt-emerald-instructions"' in html
    assert "not automatic integrations" in html


def test_template_has_no_hardcoded_find_statistics():
    text = TEMPLATE.read_text(encoding="utf-8")
    assert re.search(r"\$\d", text) is None
    assert re.search(r"\d+%", text) is None
    assert re.search(r"\d+\s+customers", text, re.I) is None
    for glyph in PROTOTYPE_EMOJI:
        assert glyph not in text


def test_thin_slice_spotlights_the_only_pile_without_a_queue():
    money_map, _packet, play_set = build_thin_slice_strategized_money_map()
    html = render_recovery_room_html(money_map, play_set)
    find = _find_section(html)
    assert "find-spotlight" in find
    assert "find-queue" not in find
    assert "Payment Rescue" in find
    assert "We found $49.00" in find
    assert "strategy deferred" in find or "Open linked play" in find


@pytest.mark.parametrize(
    "fixture_id", [SYNTHETIC_SAAS_V1, SYNTHETIC_ECOMMERCE_V1, SYNTHETIC_SERVICE_V1]
)
def test_canonical_rooms_stay_public_safe(fixture_id: str):
    engine = _engine(fixture_id)
    html = render_recovery_room_html(
        engine.enriched_money_map,
        engine.strategy_run.recovery_plays,
        contribution_ledger=engine.ledger,
    )
    assert_public_safe(html)
    find = _find_section(html)
    assert "find-spotlight" in find
    play = _play_section(html)
    for item in engine.strategy_run.recovery_plays.plays:
        assert f'id="{item.play_id}"' in play
        assert f'data-play-id="{item.play_id}"' in play
