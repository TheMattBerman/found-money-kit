"""FM-004 Money Map contract tests."""

from __future__ import annotations

import importlib.util
import json
import socket
from datetime import timezone
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from found_money.contracts.events import RecoveryCandidateSetV1, RecoveryCandidateV1
from found_money.contracts.map import MoneyMapPileV1, MoneyMapV1
from found_money.contracts.value import ContributionLedgerV1, ContributionV1
from found_money.map import (
    build_money_map,
    build_thin_slice_money_map,
    parse_canonical_json,
    public_money_map_projection,
    write_money_map,
    write_public_money_map_projection,
)

ROOT = Path(__file__).resolve().parents[2]
MAP_FIXTURES = ROOT / "tests" / "fixtures" / "saas" / "map"
UTC = timezone.utc


def _candidate(**overrides):
    base = {
        "run_id": "run_map",
        "event_family": "failed_payment",
        "confidence_class": "observed",
        "economic_unit_key": "stripe_invoice:inv_x",
        "customer_token": "cust_token_x",
        "lineage": {"invoice_node_id": "stripe:invoice:inv_x"},
        "qualifying_evidence": {"status": "open", "collection_outcome": "payment_failed"},
    }
    base.update(overrides)
    return RecoveryCandidateV1.model_validate(base)


def _candidate_set(**overrides):
    candidate = _candidate()
    base = {
        "schema_version": "recovery-candidate.v1",
        "run_id": "run_map",
        "built_at": "2026-07-29T18:00:10.000Z",
        "candidates": [candidate.model_dump(mode="json")],
    }
    base.update(overrides)
    return RecoveryCandidateSetV1.model_validate(base)


def _contribution(**overrides):
    base = {
        "economic_unit_key": "stripe_invoice:inv_x",
        "pile_id": "payment_rescue",
        "value_basis": "observed_face_value",
        "currency": "usd",
        "amount_minor": "4900",
        "customer_token": "cust_token_x",
        "candidate_economic_unit_key": "stripe_invoice:inv_x",
    }
    base.update(overrides)
    return ContributionV1.model_validate(base)


def _ledger(**overrides):
    contribution = _contribution()
    base = {
        "schema_version": "contribution-ledger.v1",
        "run_id": "run_map",
        "built_at": "2026-07-29T18:00:15.000Z",
        "contributions": [contribution.model_dump(mode="json")],
    }
    base.update(overrides)
    return ContributionLedgerV1.model_validate(base)


def _money_map(**overrides):
    pile = MoneyMapPileV1.model_validate(
        {
            "pile_id": "payment_rescue",
            "rank": 1,
            "value_basis": "observed_face_value",
            "currency": "usd",
            "selected_value_minor": "4900",
            "customer_count": 1,
            "economic_unit_count": 1,
            "confidence_class": "observed",
            "why_recoverable": "1 observed failed payment with observed face value",
        }
    )
    base = {
        "schema_version": "money-map.v1",
        "run_id": "run_map",
        "built_at": "2026-07-29T18:00:20.000Z",
        "identified_opportunity_minor": {"usd": "4900"},
        "basis_counts_by_currency": {
            "usd": {
                "observed_event_count": 1,
                "modeled_event_count": 0,
                "unquantified_event_count": 0,
            }
        },
        "piles": [pile.model_dump(mode="json")],
        "recommended_play_ids": [],
        "strategy_stage": "not_started",
    }
    base.update(overrides)
    return MoneyMapV1.model_validate(base)


def _list_tree(root: Path) -> list[str]:
    if not root.exists():
        return []
    return sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() or path.is_dir()
    )


def _load_map_fixture(name: str) -> tuple[ContributionLedgerV1, RecoveryCandidateSetV1]:
    payload = json.loads((MAP_FIXTURES / name).read_text(encoding="utf-8"))
    run_id = payload["run_id"]
    contributions = [ContributionV1.model_validate(item) for item in payload["contributions"]]
    candidates = [RecoveryCandidateV1.model_validate(item) for item in payload["candidates"]]
    ledger = ContributionLedgerV1.model_validate(
        {
            "schema_version": "contribution-ledger.v1",
            "run_id": run_id,
            "built_at": "2026-07-29T18:00:15.000Z",
            "contributions": [item.model_dump(mode="json") for item in contributions],
        }
    )
    candidate_set = RecoveryCandidateSetV1.model_validate(
        {
            "schema_version": "recovery-candidate.v1",
            "run_id": run_id,
            "built_at": "2026-07-29T18:00:10.000Z",
            "candidates": [item.model_dump(mode="json") for item in candidates],
        }
    )
    return ledger, candidate_set


def test_money_map_contract_validation_and_canonical_bytes():
    money_map = _money_map()
    payload = money_map.to_canonical_json()
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
        MoneyMapV1.model_validate({**money_map.model_dump(mode="json"), "extra": True})

    for value in ["  money-map.v1", "money-map.v1  ", "  money-map.v1  "]:
        assert (
            MoneyMapV1.model_validate(
                {**money_map.model_dump(mode="json"), "schema_version": value}
            ).schema_version
            == "money-map.v1"
        )
    for value in ["  payment_rescue", "payment_rescue  ", "  payment_rescue  "]:
        pile = MoneyMapPileV1.model_validate(
            {**money_map.piles[0].model_dump(mode="json"), "pile_id": value}
        )
        assert pile.pile_id == "payment_rescue"
    encoded = money_map.to_canonical_json()
    assert parse_canonical_json(encoded).to_canonical_json() == encoded


def test_thin_slice_money_map_headline_and_pile():
    money_map = build_thin_slice_money_map()
    assert len(money_map.piles) == 1
    pile = money_map.piles[0]
    assert pile.pile_id == "payment_rescue"
    assert pile.rank == 1
    assert pile.value_basis == "observed_face_value"
    assert pile.currency == "usd"
    assert pile.selected_value_minor == Decimal(4900)
    assert pile.customer_count == 1
    assert pile.economic_unit_count == 1
    assert pile.confidence_class == "observed"
    assert pile.why_recoverable
    assert money_map.identified_opportunity_minor == {"usd": Decimal(4900)}
    assert money_map.recommended_play_ids == []
    assert money_map.strategy_stage == "not_started"
    counts = money_map.basis_counts_by_currency["usd"]
    assert counts.observed_event_count == 1
    assert counts.modeled_event_count == 0
    assert counts.unquantified_event_count == 0


def test_build_thin_slice_money_map_fixture_only():
    money_map = build_thin_slice_money_map(run_id="run_fixture_only")
    assert money_map.run_id == "run_fixture_only"
    assert money_map.piles[0].selected_value_minor == Decimal(4900)


def test_empty_ledger_zero_totals():
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
    assert money_map.piles == []
    assert money_map.identified_opportunity_minor == {}
    assert money_map.basis_counts_by_currency == {}


def test_mixed_currency_keeps_separated_totals():
    ledger, candidates = _load_map_fixture("mixed_currency.json")
    money_map = build_money_map(ledger, candidates)
    assert money_map.identified_opportunity_minor == {
        "eur": Decimal(2000),
        "jpy": Decimal(1234),
        "kwd": Decimal(1234),
        "usd": Decimal(1234),
    }
    assert [pile.currency for pile in money_map.piles] == ["eur", "jpy", "kwd", "usd"]
    assert [pile.rank for pile in money_map.piles] == [1, 2, 3, 4]
    assert all(pile.pile_id == "payment_rescue" for pile in money_map.piles)
    text = money_map.to_canonical_json().decode("utf-8").lower()
    assert "fx" not in text
    assert "exchange" not in text


def test_duplicate_economic_unit_key_still_rejected_at_ledger():
    with pytest.raises(ValidationError):
        ContributionLedgerV1.model_validate(
            {
                "schema_version": "contribution-ledger.v1",
                "run_id": "run_dup",
                "built_at": "2026-07-29T18:00:15.000Z",
                "contributions": [
                    _contribution().model_dump(mode="json"),
                    _contribution().model_dump(mode="json"),
                ],
            }
        )


def test_money_map_writes_reject_escape_without_partial_state(tmp_path):
    money_map = build_thin_slice_money_map()
    projection = public_money_map_projection(money_map)
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "out"
    root.mkdir()
    before_outside = _list_tree(outside)
    before_root = _list_tree(root)

    for writer, payload in (
        (write_money_map, money_map),
        (write_public_money_map_projection, projection),
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


def test_public_money_map_projection_omits_raw_identity():
    money_map = build_thin_slice_money_map()
    projection = public_money_map_projection(money_map)
    text = projection.to_canonical_json().decode("utf-8")
    assert "payment_rescue" in text
    assert "usd" in text
    assert "4900" in text
    for forbidden in (
        "email",
        "phone",
        "external_ids",
        "inv_failed_001",
        "cus_synth_001",
        "source_id",
        "why_recoverable",
    ):
        assert forbidden not in text


def test_money_map_round_trip_and_public_safety_scan(tmp_path):
    money_map = build_thin_slice_money_map()
    projection = public_money_map_projection(money_map)

    for parser, payload in (
        (parse_canonical_json, money_map.to_canonical_json()),
        (parse_canonical_json, projection.to_canonical_json()),
    ):
        assert parser(payload).to_canonical_json() == payload

    with pytest.raises(ValueError, match="unknown major"):
        parse_canonical_json(
            b'{"schema_version":"money-map.v2","run_id":"r","built_at":"2026-07-29T18:00:20.000Z",'
            b'"identified_opportunity_minor":{},"basis_counts_by_currency":{},"piles":[],'
            b'"recommended_play_ids":[],"strategy_stage":"not_started"}\n'
        )
    with pytest.raises(ValueError, match="malformed"):
        parse_canonical_json(b"{")
    with pytest.raises(ValueError, match="duplicate JSON object key"):
        parse_canonical_json(b'{"schema_version":"money-map-public.v1","schema_version":"x"}\n')

    write_money_map(tmp_path, "map/money-map.json", money_map)
    write_public_money_map_projection(tmp_path, "map/money-map-public.json", projection)

    spec = importlib.util.spec_from_file_location(
        "public_safety", ROOT / "scripts" / "public_safety.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    public_text = (tmp_path / "map" / "money-map-public.json").read_text(encoding="utf-8")
    assert module.scan_contract_artifact_text("public", public_text) == []
    assert module.main(ROOT) == 0
    assert module.scan_generated_contract_artifacts(ROOT) == []


def test_ng1_money_map_builders_do_not_open_sockets(monkeypatch):
    def boom(*_args, **_kwargs):
        raise AssertionError("socket connection attempted")

    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket.socket, "connect", boom)
    build_thin_slice_money_map()


def test_r1_path_normalization_edges(tmp_path):
    money_map = build_thin_slice_money_map()
    for relative in ["  map/out.json", "map/out.json  ", "./map/out.json"]:
        path = write_money_map(tmp_path, relative, money_map)
        assert path.exists()
        path.unlink()
