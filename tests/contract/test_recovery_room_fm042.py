"""FM-042 Recovery Room Play-as-campaign contract tests."""

from __future__ import annotations

import re
from pathlib import Path

from found_money.build import _fixture_snapshots, _run_id_for, _safe_source_config
from found_money.redaction import assert_public_safe
from found_money.rendering import pile_display_name, render_recovery_room_html
from found_money.scenarios import SYNTHETIC_SERVICE_V1, run_scenario_engine
from found_money.strategy import build_thin_slice_strategized_money_map

ROOT = Path(__file__).resolve().parents[2]


def _service_engine():
    safe = _safe_source_config(
        source_mode="fixture", run_mode="public", fixture=SYNTHETIC_SERVICE_V1
    )
    run_id = _run_id_for(_fixture_snapshots(SYNTHETIC_SERVICE_V1), safe)
    return run_scenario_engine(run_id=run_id, safe_config=safe, fixture_id=SYNTHETIC_SERVICE_V1)


def _play_section(html: str) -> str:
    match = re.search(r'<section id="room-play".*?</section>', html, re.DOTALL)
    assert match is not None
    return match.group(0)


def test_service_plays_render_as_campaign_pitches():
    engine = _service_engine()
    html = render_recovery_room_html(
        engine.enriched_money_map,
        engine.strategy_run.recovery_plays,
        contribution_ledger=engine.ledger,
    )
    play = _play_section(html)
    for item in engine.strategy_run.recovery_plays.plays:
        assert f"Play {item.play_id} for pile {item.pile_id}" not in play
        article = re.search(
            rf'<article id="{re.escape(item.play_id)}".*?</article>', play, re.DOTALL
        )
        assert article is not None
        body = article.group(0)
        assert f'data-play-id="{item.play_id}"' in body
        pitch = re.search(r'<header class="play-pitch">(.*?)</header>', body, re.DOTALL)
        assert pitch is not None
        header = pitch.group(1)
        title_at = header.find(item.title)
        pile_at = header.find(pile_display_name(item.pile_id))
        diagnosis_at = header.find(item.diagnosis.text)
        offer_at = header.find(
            next(rung.mechanism.text for rung in item.offer_ladder.rungs if rung.role == "core")
        )
        cta_at = header.find(item.primary_cta.text)
        assert 0 <= title_at < pile_at < diagnosis_at < offer_at < cta_at
        assert "$" in header or "Unquantified" in header
        assert '<p class="play-email-subject">' in body
        assert '<p class="play-email-body">' in body
        first_email = item.email_sequence[0]
        concatenated = (
            f"Step {first_email.order} — {first_email.lifecycle_stage} — "
            f"{first_email.subject.text} — {first_email.body.text} — CTA: {first_email.cta.text}"
        )
        assert concatenated not in body
        assert first_email.subject.text in body
        assert first_email.body.text in body
        for card in item.concept_cards:
            assert f'id="{card.card_id}"' in body
    assert play.count('id="concept-') == 9
    find = re.search(r'<section id="room-find".*?</section>', html, re.DOTALL)
    assert find is not None
    assert "We found $387.00" in find.group(0)
    assert_public_safe(html)


def test_thin_slice_incomplete_play_keeps_fallback():
    money_map, _packet, play_set = build_thin_slice_strategized_money_map()
    html = render_recovery_room_html(money_map, play_set)
    play = _play_section(html)
    assert "Thin fixture play; complete campaign assets remain unavailable." in play
    assert f'data-play-id="{play_set.plays[0].play_id}"' in play
    assert f"Play {play_set.plays[0].play_id} for pile" not in play
