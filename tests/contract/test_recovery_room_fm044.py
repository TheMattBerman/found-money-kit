"""FM-044 Recovery Room Launch-as-next-action contract tests."""

from __future__ import annotations

import re
from pathlib import Path

from found_money.build import _fixture_snapshots, _run_id_for, _safe_source_config, build
from found_money.redaction import assert_public_safe
from found_money.rendering import pile_display_name, render_recovery_room_html
from found_money.scenarios import SYNTHETIC_SERVICE_V1, run_scenario_engine
from found_money.strategy import build_thin_slice_strategized_money_map

ROOT = Path(__file__).resolve().parents[2]
SERVICE_CONFIG = ROOT / "configs" / "synthetic-service-v1.json"
NO_SEND = "No messages are sent and no upstream system is changed."
CHECKLIST_KEYS = ("audience", "offer", "copy", "destination", "tracking", "sender")


def _service_engine():
    safe = _safe_source_config(
        source_mode="fixture", run_mode="public", fixture=SYNTHETIC_SERVICE_V1
    )
    run_id = _run_id_for(_fixture_snapshots(SYNTHETIC_SERVICE_V1), safe)
    return run_scenario_engine(run_id=run_id, safe_config=safe, fixture_id=SYNTHETIC_SERVICE_V1)


def _launch_section(html: str) -> str:
    match = re.search(r'<section id="room-launch".*?</section>', html, re.DOTALL)
    assert match is not None
    return match.group(0)


def test_service_launch_shows_calendar_checklist_and_no_send():
    engine = _service_engine()
    html = render_recovery_room_html(
        engine.enriched_money_map,
        engine.strategy_run.recovery_plays,
        withheld_assets=list(engine.withheld_asset_ids),
        contribution_ledger=engine.ledger,
    )
    launch = _launch_section(html)
    assert "Launch status:" in launch
    assert NO_SEND in launch
    assert launch.count("data-recommended-action") == 1
    rows = re.findall(r'<li data-play-id="([^"]+)"', launch)
    play_ids = [play.play_id for play in engine.strategy_run.recovery_plays.plays]
    assert len(play_ids) == 3
    for play in engine.strategy_run.recovery_plays.plays:
        assert play.play_id in rows
        label = pile_display_name(play.pile_id)
        assert label in launch or play.title in launch
        for item in play.calendar:
            assert f"Day {item.day}" in launch
            assert item.action.text in launch
            assert item.stop_condition.text in launch
    for key in CHECKLIST_KEYS:
        assert f'data-checklist-item="{key}"' in launch
    withheld = set(engine.withheld_asset_ids)
    if any(asset == "complete_copy" or asset.startswith("complete_copy:") for asset in withheld):
        assert "Copy: withheld" in launch
        assert "Sender: withheld" in launch
    else:
        assert re.search(r">Copy</li>", launch) is not None
        assert "Copy: withheld" not in launch
    assert re.search(r"\bsent\b", launch.replace(NO_SEND, ""), re.IGNORECASE) is None
    find = re.search(r'<section id="room-find".*?</section>', html, re.DOTALL)
    assert find is not None
    assert "We found $387.00" in find.group(0)
    assert_public_safe(html)


def test_service_build_launch_calendar_covers_all_three_plays(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = build(output_root="service-v1", source_config=SERVICE_CONFIG)
    html = (result.output_root / "index.html").read_text(encoding="utf-8")
    launch = _launch_section(html)
    assert NO_SEND in launch
    engine = _service_engine()
    for play in engine.strategy_run.recovery_plays.plays:
        assert f'data-play-id="{play.play_id}"' in launch
        assert pile_display_name(play.pile_id) in launch or play.title in launch
    assert_public_safe(html)


def test_thin_slice_launch_names_withheld_checklist_items():
    money_map, _packet, play_set = build_thin_slice_strategized_money_map()
    html = render_recovery_room_html(
        money_map,
        play_set,
        launch_status="not_started",
        withheld_assets=["complete_copy", "creative_handoff", "private_segments"],
    )
    launch = _launch_section(html)
    assert NO_SEND in launch
    assert "Audience: withheld" in launch
    assert "Copy: withheld" in launch
    assert "Sender: withheld" in launch
    assert "complete_copy" in launch
    assert "creative_handoff" in launch
    assert "No calendar rows are available for this run." in launch
    assert 'data-status="ready"' in launch
    assert "Offer: withheld" not in launch
    assert re.search(r"\bsent\b", launch.replace(NO_SEND, ""), re.IGNORECASE) is None
    assert_public_safe(html)
