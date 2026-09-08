"""FM-005 recovery-play and strategy evidence contract tests."""

from __future__ import annotations

import importlib.util
import json
import socket
from datetime import timezone
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from found_money.contracts.strategy import (
    RecoveryPlaySetV1,
    RecoveryPlayV1,
    StrategyEvidencePacketV1,
    StrategyEvidencePileFactV1,
)
from found_money.map import build_money_map, build_thin_slice_money_map
from found_money.contracts.events import RecoveryCandidateSetV1
from found_money.contracts.value import ContributionLedgerV1
from found_money.strategy import (
    FixtureStrategyProvider,
    apply_recovery_plays_to_money_map,
    build_strategy_evidence_packet,
    build_thin_slice_strategized_money_map,
    parse_canonical_json,
    public_recovery_play_projection,
    write_public_recovery_play_projection,
    write_recovery_plays,
    write_strategy_evidence_packet,
)

ROOT = Path(__file__).resolve().parents[2]
UTC = timezone.utc


def _list_tree(root: Path) -> list[str]:
    if not root.exists():
        return []
    return sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() or path.is_dir()
    )


def _play(**overrides):
    base = {
        "play_id": "payment_rescue_dunning",
        "pile_id": "payment_rescue",
        "rank": 1,
        "title": "Payment rescue dunning for payment_rescue",
        "rationale": "1 observed payment totaling 4900 usd under observed face value",
        "recommended_actions": [
            "Human review of payment_rescue pile before any outreach",
            "Confirm observed face value of 4900 usd",
            "Queue recovery review without sending messages",
        ],
    }
    base.update(overrides)
    return RecoveryPlayV1.model_validate(base)


def _play_set(**overrides):
    play = _play()
    base = {
        "schema_version": "recovery-plays.v1",
        "run_id": "run_strategy",
        "built_at": "2026-07-29T18:00:30.000Z",
        "provider": "fixture",
        "plays": [play.model_dump(mode="json")],
    }
    base.update(overrides)
    return RecoveryPlaySetV1.model_validate(base)


def _pile_fact(**overrides):
    base = {
        "pile_id": "payment_rescue",
        "currency": "usd",
        "selected_value_minor": "4900",
        "customer_count": 1,
        "economic_unit_count": 1,
        "value_basis": "observed_face_value",
        "confidence_class": "observed",
    }
    base.update(overrides)
    return StrategyEvidencePileFactV1.model_validate(base)


def _packet(**overrides):
    fact = _pile_fact()
    base = {
        "schema_version": "strategy-evidence-packet.v1",
        "run_id": "run_strategy",
        "built_at": "2026-07-29T18:00:25.000Z",
        "pile_facts": [fact.model_dump(mode="json")],
    }
    base.update(overrides)
    return StrategyEvidencePacketV1.model_validate(base)


def test_recovery_play_contract_validation_and_canonical_bytes():
    play_set = _play_set()
    packet = _packet()
    for model in (play_set, packet):
        payload = model.to_canonical_json()
        assert payload.endswith(b"\n")
        assert payload.count(b"\n") == 1
        assert (
            json.dumps(
                json.loads(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
            + b"\n"
            == payload
        )

    with pytest.raises(ValidationError):
        RecoveryPlaySetV1.model_validate({**play_set.model_dump(mode="json"), "extra": True})
    with pytest.raises(ValidationError):
        StrategyEvidencePacketV1.model_validate({**packet.model_dump(mode="json"), "extra": True})

    for value in ["  recovery-plays.v1", "recovery-plays.v1  ", "  recovery-plays.v1  "]:
        assert (
            RecoveryPlaySetV1.model_validate(
                {**play_set.model_dump(mode="json"), "schema_version": value}
            ).schema_version
            == "recovery-plays.v1"
        )
    for value in [
        "  strategy-evidence-packet.v1",
        "strategy-evidence-packet.v1  ",
        "  strategy-evidence-packet.v1  ",
    ]:
        assert (
            StrategyEvidencePacketV1.model_validate(
                {**packet.model_dump(mode="json"), "schema_version": value}
            ).schema_version
            == "strategy-evidence-packet.v1"
        )
    for value in [
        "  payment_rescue_dunning",
        "payment_rescue_dunning  ",
        "  payment_rescue_dunning  ",
    ]:
        play = RecoveryPlayV1.model_validate({**_play().model_dump(mode="json"), "play_id": value})
        assert play.play_id == "payment_rescue_dunning"
    for value in ["  payment_rescue", "payment_rescue  ", "  payment_rescue  "]:
        play = RecoveryPlayV1.model_validate({**_play().model_dump(mode="json"), "pile_id": value})
        assert play.pile_id == "payment_rescue"
    for value in ["  fixture", "fixture  ", "  fixture  "]:
        assert (
            RecoveryPlaySetV1.model_validate(
                {**play_set.model_dump(mode="json"), "provider": value}
            ).provider
            == "fixture"
        )


def test_strategy_evidence_packet_is_pii_free_for_thin_slice():
    money_map = build_thin_slice_money_map()
    packet = build_strategy_evidence_packet(money_map)
    assert packet.run_id == money_map.run_id
    assert len(packet.pile_facts) == 1
    fact = packet.pile_facts[0]
    assert fact.pile_id == "payment_rescue"
    assert fact.currency == "usd"
    assert fact.selected_value_minor == Decimal(4900)
    assert fact.customer_count == 1
    assert fact.economic_unit_count == 1
    text = packet.to_canonical_json().decode("utf-8")
    for forbidden in (
        "customer_token",
        "economic_unit_key",
        "lineage",
        "qualifying_evidence",
        "external_ids",
        "inv_failed_001",
        "cus_synth_001",
        "email",
        "phone",
    ):
        assert forbidden not in text


def test_fixture_strategy_provider_thin_slice_play():
    money_map = build_thin_slice_money_map()
    packet = build_strategy_evidence_packet(money_map)
    play_set = FixtureStrategyProvider().propose(packet)
    assert play_set.provider == "fixture"
    assert len(play_set.plays) == 1
    play = play_set.plays[0]
    assert play.play_id == "payment_rescue_dunning"
    assert play.pile_id == "payment_rescue"
    assert play.rank == 1
    assert play.title
    assert play.rationale
    assert play.recommended_actions
    text = play_set.to_canonical_json().decode("utf-8")
    for forbidden in ("@", "http://", "https://", "inv_failed_001", "cus_synth_001", "+1"):
        assert forbidden not in text


def test_apply_recovery_plays_enriches_money_map():
    money_map = build_thin_slice_money_map()
    packet = build_strategy_evidence_packet(money_map)
    play_set = FixtureStrategyProvider().propose(packet)
    enriched = apply_recovery_plays_to_money_map(money_map, play_set)
    assert enriched.recommended_play_ids == ["payment_rescue_dunning"]
    assert enriched.strategy_stage == "completed"
    assert enriched.identified_opportunity_minor == money_map.identified_opportunity_minor
    assert enriched.piles[0].selected_value_minor == Decimal(4900)
    assert money_map.recommended_play_ids == []
    assert money_map.strategy_stage == "not_started"


def test_build_thin_slice_strategized_money_map_fixture_only():
    enriched, packet, play_set = build_thin_slice_strategized_money_map(
        run_id="run_fixture_strategy"
    )
    assert enriched.run_id == "run_fixture_strategy"
    assert packet.run_id == "run_fixture_strategy"
    assert play_set.run_id == "run_fixture_strategy"
    assert enriched.recommended_play_ids == ["payment_rescue_dunning"]
    assert enriched.strategy_stage == "completed"
    assert enriched.identified_opportunity_minor == {"usd": Decimal(4900)}
    plain = build_thin_slice_money_map(run_id="run_plain")
    assert plain.recommended_play_ids == []
    assert plain.strategy_stage == "not_started"


def test_empty_map_no_plays():
    ledger = ContributionLedgerV1.model_validate(
        {
            "schema_version": "contribution-ledger.v1",
            "run_id": "run_empty",
            "built_at": "2026-07-29T18:00:15.000Z",
            "contributions": [],
        }
    )
    candidates = RecoveryCandidateSetV1.model_validate(
        {
            "schema_version": "recovery-candidate.v1",
            "run_id": "run_empty",
            "built_at": "2026-07-29T18:00:10.000Z",
            "candidates": [],
        }
    )
    money_map = build_money_map(ledger, candidates)
    packet = build_strategy_evidence_packet(money_map)
    assert packet.pile_facts == []
    play_set = FixtureStrategyProvider().propose(packet)
    assert play_set.plays == []
    enriched = apply_recovery_plays_to_money_map(money_map, play_set)
    assert enriched.recommended_play_ids == []
    assert enriched.strategy_stage == "not_started"


def test_unknown_pile_id_raises():
    money_map = build_money_map(
        ContributionLedgerV1.model_validate(
            {
                "schema_version": "contribution-ledger.v1",
                "run_id": "run_strategy",
                "built_at": "2026-07-29T18:00:15.000Z",
                "contributions": [],
            }
        ),
        RecoveryCandidateSetV1.model_validate(
            {
                "schema_version": "recovery-candidate.v1",
                "run_id": "run_strategy",
                "built_at": "2026-07-29T18:00:10.000Z",
                "candidates": [],
            }
        ),
    )
    play_set = _play_set()
    with pytest.raises(ValueError, match="unknown pile_id"):
        apply_recovery_plays_to_money_map(money_map, play_set)


def test_forbidden_identity_field_rejected_in_evidence_packet():
    payload = _packet().model_dump(mode="json")
    payload["customer_token"] = "cust_token_x"
    with pytest.raises((ValidationError, ValueError)):
        StrategyEvidencePacketV1.model_validate(payload)

    payload = _packet().model_dump(mode="json")
    payload["pile_facts"][0]["economic_unit_key"] = "stripe_invoice:inv_x"
    with pytest.raises((ValidationError, ValueError)):
        StrategyEvidencePacketV1.model_validate(payload)


def test_strategy_writes_reject_escape_without_partial_state(tmp_path):
    _enriched, packet, play_set = build_thin_slice_strategized_money_map()
    projection = public_recovery_play_projection(play_set)
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "out"
    root.mkdir()
    before_outside = _list_tree(outside)
    before_root = _list_tree(root)

    for writer, payload in (
        (write_strategy_evidence_packet, packet),
        (write_recovery_plays, play_set),
        (write_public_recovery_play_projection, projection),
    ):
        for relative in ("../outside/x.json", "/tmp/x.json", "sibling/../../x.json"):
            with pytest.raises(ValueError):
                writer(root, relative, payload)
            assert _list_tree(root) == before_root
            assert _list_tree(outside) == before_outside

        link = root / "escape"
        link.symlink_to(outside)
        with pytest.raises(ValueError):
            writer(root, "escape/x.json", payload)
        assert _list_tree(outside) == before_outside
        link.unlink()


def test_public_recovery_play_projection_omits_raw_identity():
    _enriched, _packet, play_set = build_thin_slice_strategized_money_map()
    projection = public_recovery_play_projection(play_set)
    text = projection.to_canonical_json().decode("utf-8")
    assert "payment_rescue_dunning" in text
    assert "payment_rescue" in text
    for forbidden in (
        "email",
        "phone",
        "external_ids",
        "inv_failed_001",
        "cus_synth_001",
        "customer_token",
        "economic_unit_key",
        "rationale",
        "source_id",
    ):
        assert forbidden not in text


def test_recovery_plays_round_trip_and_public_safety_scan(tmp_path):
    enriched, packet, play_set = build_thin_slice_strategized_money_map()
    projection = public_recovery_play_projection(play_set)

    for payload in (
        packet.to_canonical_json(),
        play_set.to_canonical_json(),
        projection.to_canonical_json(),
    ):
        assert parse_canonical_json(payload).to_canonical_json() == payload

    with pytest.raises(ValueError, match="unknown major"):
        parse_canonical_json(
            b'{"schema_version":"recovery-plays.v2","run_id":"r",'
            b'"built_at":"2026-07-29T18:00:30.000Z","provider":"fixture","plays":[]}\n'
        )
    with pytest.raises(ValueError, match="malformed"):
        parse_canonical_json(b"{")
    with pytest.raises(ValueError, match="duplicate JSON object key"):
        parse_canonical_json(
            b'{"schema_version":"recovery-plays-public.v1","schema_version":"x"}\n'
        )

    write_strategy_evidence_packet(tmp_path, "strategy/strategy-evidence-packet.json", packet)
    write_recovery_plays(tmp_path, "strategy/recovery-plays.json", play_set)
    write_public_recovery_play_projection(
        tmp_path, "strategy/recovery-plays-public.json", projection
    )
    assert enriched.strategy_stage == "completed"

    spec = importlib.util.spec_from_file_location(
        "public_safety", ROOT / "scripts" / "public_safety.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    public_text = (tmp_path / "strategy" / "recovery-plays-public.json").read_text(encoding="utf-8")
    assert module.scan_contract_artifact_text("public", public_text) == []
    assert module.main(ROOT) == 0
    assert module.scan_generated_contract_artifacts(ROOT) == []


def test_ng1_strategy_builders_do_not_open_sockets(monkeypatch):
    def boom(*_args, **_kwargs):
        raise AssertionError("socket connection attempted")

    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket.socket, "connect", boom)
    build_thin_slice_strategized_money_map()


def test_r1_path_and_identifier_normalization_edges(tmp_path):
    _enriched, packet, play_set = build_thin_slice_strategized_money_map()
    for relative in ["  strategy/out.json", "strategy/out.json  ", "./strategy/out.json"]:
        path = write_recovery_plays(tmp_path, relative, play_set)
        assert path.exists()
        path.unlink()
    for value in ["  run_strategy", "run_strategy  ", "  run_strategy  "]:
        assert (
            StrategyEvidencePacketV1.model_validate(
                {**packet.model_dump(mode="json"), "run_id": value}
            ).run_id
            == "run_strategy"
        )
