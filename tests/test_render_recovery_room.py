"""FM-006 Recovery Room HTML render contract tests."""

from __future__ import annotations

import importlib.util
import socket
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from found_money.contracts.events import RecoveryCandidateSetV1
from found_money.contracts.map import MoneyMapV1
from found_money.contracts.strategy import RecoveryPlaySetV1
from found_money.contracts.value import ContributionLedgerV1
from found_money.map import build_money_map, build_thin_slice_money_map
from found_money.rendering import (
    build_thin_slice_recovery_room,
    render_money_map_html,
    render_print_report_html,
    render_top_play_html,
    write_recovery_room,
)
from found_money.rendering.proof import capture_print_report_artifacts, png_dimensions
from found_money.strategy import (
    FixtureStrategyProvider,
    apply_recovery_plays_to_money_map,
    build_strategy_evidence_packet,
    build_thin_slice_strategized_money_map,
)

RENDER_TEST_CONTRACT = "found-money-render-v1"

ROOT = Path(__file__).resolve().parents[1]


def _list_tree(root: Path) -> list[str]:
    if not root.exists():
        return []
    return sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() or path.is_dir()
    )


def _empty_money_map(run_id: str = "run_empty") -> MoneyMapV1:
    ledger = ContributionLedgerV1.model_validate(
        {
            "schema_version": "contribution-ledger.v1",
            "run_id": run_id,
            "built_at": "2026-07-29T18:00:15.000Z",
            "contributions": [],
        }
    )
    candidates = RecoveryCandidateSetV1.model_validate(
        {
            "schema_version": "recovery-candidate.v1",
            "run_id": run_id,
            "built_at": "2026-07-29T18:00:10.000Z",
            "candidates": [],
        }
    )
    return build_money_map(ledger, candidates)


def test_render_money_map_html_thin_slice_public_safe():
    money_map, _packet, play_set = build_thin_slice_strategized_money_map()
    html = render_money_map_html(money_map)
    assert "Money Map" in html
    assert money_map.run_id in html
    assert "4900" in html
    assert "usd" in html
    assert "payment_rescue" in html
    assert "payment_rescue_dunning" in html
    assert money_map.strategy_stage in html
    assert "observed" in html
    for forbidden in (
        "customer_token",
        "economic_unit_key",
        "external_ids",
        "inv_failed_001",
        "cus_synth_001",
        "http://",
        "https://",
        "@",
    ):
        assert forbidden not in html
    assert play_set.plays[0].play_id == "payment_rescue_dunning"


def test_render_top_play_html_thin_slice_public_safe():
    money_map, _packet, play_set = build_thin_slice_strategized_money_map()
    html = render_top_play_html(money_map, play_set)
    assert "Top Play" in html
    assert "payment_rescue_dunning" in html
    assert "payment_rescue" in html
    assert play_set.plays[0].title in html
    for action in play_set.plays[0].recommended_actions:
        assert action in html
    for forbidden in (
        "customer_token",
        "economic_unit_key",
        "external_ids",
        "inv_failed_001",
        "cus_synth_001",
        "http://",
        "https://",
        "rationale",
    ):
        assert forbidden not in html


def test_build_thin_slice_recovery_room_fixture_only():
    money_map, play_set, index_html, top_play_html = build_thin_slice_recovery_room(
        run_id="run_render_fixture"
    )
    assert money_map.run_id == "run_render_fixture"
    assert money_map.identified_opportunity_minor == {"usd": Decimal(4900)}
    assert money_map.piles[0].pile_id == "payment_rescue"
    assert money_map.piles[0].rank == 1
    assert play_set.plays[0].play_id == "payment_rescue_dunning"
    assert "4900" in index_html
    assert "payment_rescue_dunning" in top_play_html
    assert play_set.plays[0].title
    assert play_set.plays[0].recommended_actions


def test_write_recovery_room_rejects_escape_without_partial_state(tmp_path):
    _map, _plays, index_html, top_play_html = build_thin_slice_recovery_room()
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "out"
    root.mkdir()
    before_outside = _list_tree(outside)
    before_root = _list_tree(root)

    for relative in ("../outside", "/tmp/room", "sibling/../../escape"):
        with pytest.raises(ValueError):
            write_recovery_room(root, index_html, top_play_html, relative_dir=relative)
        assert _list_tree(root) == before_root
        assert _list_tree(outside) == before_outside

    link = root / "escape"
    link.symlink_to(outside)
    with pytest.raises(ValueError):
        write_recovery_room(root, index_html, top_play_html, relative_dir="escape/room")
    assert _list_tree(outside) == before_outside
    link.unlink()

    index_path, top_path = write_recovery_room(root, index_html, top_play_html)
    assert index_path.read_text(encoding="utf-8") == index_html
    assert top_path.read_text(encoding="utf-8") == top_play_html


def test_empty_map_index_zero_totals():
    money_map = _empty_money_map()
    html = render_money_map_html(money_map)
    assert "Money Map" in html
    assert "No identified opportunity." in html
    assert "No piles." in html
    assert "100" not in html


def test_empty_plays_top_play_raises():
    money_map = build_thin_slice_money_map()
    assert money_map.recommended_play_ids == []
    play_set = RecoveryPlaySetV1.model_validate(
        {
            "schema_version": "recovery-plays.v1",
            "run_id": money_map.run_id,
            "built_at": "2026-07-29T18:00:30.000Z",
            "provider": "fixture",
            "plays": [],
        }
    )
    with pytest.raises(ValueError, match="recommended_play_ids is empty"):
        render_top_play_html(money_map, play_set)


def test_missing_selected_play_raises():
    money_map, _packet, play_set = build_thin_slice_strategized_money_map()
    empty_set = play_set.model_copy(update={"plays": []})
    with pytest.raises(ValueError, match="missing from play set"):
        render_top_play_html(money_map, empty_set)


def test_render_contract_marker_and_public_safety_scan(tmp_path):
    assert RENDER_TEST_CONTRACT == "found-money-render-v1"
    money_map, play_set, index_html, top_play_html = build_thin_slice_recovery_room()
    write_recovery_room(tmp_path, index_html, top_play_html)
    assert "Money Map" in (tmp_path / "recovery-room" / "index.html").read_text(encoding="utf-8")
    assert "Top Play" in (tmp_path / "recovery-room" / "top-play.html").read_text(encoding="utf-8")
    assert money_map.recommended_play_ids == ["payment_rescue_dunning"]
    assert play_set.plays[0].play_id == "payment_rescue_dunning"

    spec = importlib.util.spec_from_file_location(
        "public_safety", ROOT / "scripts" / "public_safety.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.scan_html_artifact_text("index", index_html) == []
    assert module.scan_html_artifact_text("top", top_play_html) == []
    assert module.main(ROOT) == 0
    assert module.scan_generated_contract_artifacts(ROOT) == []

    render_guard_spec = importlib.util.spec_from_file_location(
        "render_guard", ROOT / "scripts" / "render_guard.py"
    )
    assert render_guard_spec and render_guard_spec.loader
    render_guard = importlib.util.module_from_spec(render_guard_spec)
    render_guard_spec.loader.exec_module(render_guard)
    assert render_guard.main(ROOT) == 0


def test_ng1_render_builders_do_not_open_sockets(monkeypatch):
    def boom(*_args, **_kwargs):
        raise AssertionError("socket connection attempted")

    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket.socket, "connect", boom)
    build_thin_slice_recovery_room()


def test_r1_path_normalization_edges(tmp_path):
    _map, _plays, index_html, top_play_html = build_thin_slice_recovery_room()
    for relative in ["  recovery-room", "recovery-room  ", "./recovery-room", "recovery-room/"]:
        index_path, top_path = write_recovery_room(
            tmp_path, index_html, top_play_html, relative_dir=relative
        )
        assert index_path.exists()
        assert top_path.exists()
        index_path.unlink()
        top_path.unlink()
        index_path.parent.rmdir()


def test_render_contract_asserts_money_map_content():
    """Non-constant assert required by render_guard found-money-render-v1."""
    money_map = build_thin_slice_money_map()
    packet = build_strategy_evidence_packet(money_map)
    play_set = FixtureStrategyProvider().propose(packet)
    enriched = apply_recovery_plays_to_money_map(money_map, play_set)
    html = render_money_map_html(enriched)
    assert "Money Map" in html and enriched.piles[0].pile_id == "payment_rescue"


def test_print_report_renders_three_play_contract_when_upstream_fields_exist(tmp_path):
    money_map, _packet, _play_set = build_thin_slice_strategized_money_map()

    def play(rank: int) -> dict:
        return {
            "play_id": f"recovery_play_{rank}",
            "pile_id": "payment_rescue",
            "rank": rank,
            "title": f"Recovery play {rank}",
            "campaign_name": f"Campaign {rank}",
            "strategy": f"Strategy {rank}",
            "rationale": f"Grounded rationale {rank}",
            "value_basis": "observed_face_value",
            "audience": f"Audience {rank}",
            "recoverability": "High from observed evidence",
            "diagnosis": f"Diagnosis {rank}",
            "offer": f"Offer mechanism {rank}",
            "offer_rationale": f"Offer rationale {rank}",
            "constraints": "Stop if consent or capacity is unavailable.",
            "lifecycle_sequence": f"Lifecycle sequence {rank}",
            "channel_emphasis": f"Channel emphasis {rank}",
            "creative_big_idea": f"Creative big idea {rank}",
            "email_sequence": [{"subject": "Opening", "body": "Review the observed opportunity."}],
            "sms": [{"stage": "Reminder", "text": "Review only."}],
            "task_talk_track": [{"label": "Task", "text": "Human review before outreach."}],
            "cta": "Book a review",
            "objections": [{"label": "Timing", "text": "Offer a later review."}],
            "urgency": "none",
            "calendar": [{"label": "Day 1", "text": "Review."}],
            "stop_conditions": [{"label": "Stop", "text": "Stop on opt-out."}],
            "tracking": "Record review status.",
            "success_event": "review_completed",
            "recommended_actions": [f"Action {rank}"],
            "concept_cards": [
                {
                    "card_id": f"card_{rank}_{card}",
                    "name": f"Card {rank}.{card}",
                    "audience_tension": "A real tension",
                    "big_idea": "A grounded idea",
                    "hook": "A clear hook",
                    "first_three_seconds": "Opening visual",
                    "proof_device": "Observed evidence",
                    "format_style": "Direct response",
                    "cta": "Book a review",
                    "pile_specific_fit": "Fits the payment pile",
                    "production_requirements": "Simple text and proof treatment",
                }
                for card in range(1, 4)
            ],
        }

    play_set = SimpleNamespace(run_id=money_map.run_id, plays=[play(1), play(2), play(3)])
    html = render_print_report_html(money_map, play_set)

    assert 'content="ready-for-human-review"' in html
    assert "Recovery Plays" in html
    assert "3" in html
    assert all(f"recovery_play_{rank}" in html for rank in range(1, 4))
    assert html.count("Concept Card") >= 9
    assert "not present in this run" not in html
    assert "https://" not in html

    print_path = tmp_path / "print-report.html"
    print_path.write_text(html, encoding="utf-8")
    artifacts = capture_print_report_artifacts(print_path.resolve())
    page_names = sorted(name for name in artifacts if name.startswith("print-report-page-"))
    assert len(page_names) == 19
    assert all(png_dimensions(artifacts[name]) == (816, 1056) for name in page_names)
    assert artifacts["print-report-contact-sheet.png"].startswith(b"\x89PNG")


def test_incomplete_print_remains_dependency_hold_after_artifact_capture(tmp_path):
    money_map, play_set, _index_html, _top_html = build_thin_slice_recovery_room()
    html = render_print_report_html(money_map, play_set)
    assert 'content="dependency-hold"' in html
    print_path = tmp_path / "print-report.html"
    print_path.write_text(html, encoding="utf-8")
    before = print_path.read_bytes()

    artifacts = capture_print_report_artifacts(print_path.resolve())

    page_names = sorted(name for name in artifacts if name.startswith("print-report-page-"))
    assert artifacts["print-report.pdf"].startswith(b"%PDF-")
    assert len(page_names) == 7
    assert print_path.read_bytes() == before
    assert 'content="dependency-hold"' in print_path.read_text(encoding="utf-8")
