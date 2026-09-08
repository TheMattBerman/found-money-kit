"""FM-003 failed-payment detector and contribution ledger contract tests."""

from __future__ import annotations

import json
import socket
from datetime import timezone
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from found_money.contracts.events import RecoveryCandidateSetV1, RecoveryCandidateV1
from found_money.contracts.value import ContributionLedgerV1, ContributionV1
from found_money.events import (
    build_thin_slice_failed_payments,
    detect_failed_payments,
    parse_canonical_json as parse_events_json,
    write_recovery_candidates,
)
from found_money.identity import (
    build_identity_graph,
    load_thin_slice_snapshots,
    normalize_source_records,
)
from found_money.value import (
    build_contribution_ledger,
    parse_canonical_json as parse_value_json,
    public_contribution_projection,
    write_contribution_ledger,
    write_public_contribution_projection,
)

ROOT = Path(__file__).resolve().parents[2]
EVENT_FIXTURES = ROOT / "tests" / "fixtures" / "saas" / "events"
UTC = timezone.utc


def _candidate(**overrides):
    base = {
        "run_id": "run_events",
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
        "run_id": "run_events",
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
        "run_id": "run_events",
        "built_at": "2026-07-29T18:00:15.000Z",
        "contributions": [contribution.model_dump(mode="json")],
    }
    base.update(overrides)
    return ContributionLedgerV1.model_validate(base)


def _list_tree(root: Path) -> list[str]:
    if not root.exists():
        return []
    return sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() or path.is_dir()
    )


def _graph_for_stripe(stripe: dict):
    nodes = normalize_source_records({"stripe": stripe})
    return build_identity_graph(nodes, run_id="run_neg")


def test_recovery_candidate_contract_validation_and_canonical_bytes():
    candidate_set = _candidate_set()
    payload = candidate_set.to_canonical_json()
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
        RecoveryCandidateSetV1.model_validate(
            {**candidate_set.model_dump(mode="json"), "extra": True}
        )
    with pytest.raises(ValidationError):
        RecoveryCandidateV1.model_validate(
            {**_candidate().model_dump(mode="json"), "event_family": "closed_lost"}
        )

    # R-1: transformed constructs at start/end/entire extracted fields.
    for value in [
        "  recovery-candidate.v1",
        "recovery-candidate.v1  ",
        "  recovery-candidate.v1  ",
    ]:
        assert (
            RecoveryCandidateSetV1.model_validate(
                {**candidate_set.model_dump(mode="json"), "schema_version": value}
            ).schema_version
            == "recovery-candidate.v1"
        )
    for value in ["  stripe_invoice:inv_x", "stripe_invoice:inv_x  ", "  stripe_invoice:inv_x  "]:
        assert _candidate(economic_unit_key=value).economic_unit_key == "stripe_invoice:inv_x"
    for value in ["  cust_token_x", "cust_token_x  ", "  cust_token_x  "]:
        assert _candidate(customer_token=value).customer_token == "cust_token_x"
    encoded = candidate_set.to_canonical_json()
    assert parse_events_json(encoded).to_canonical_json() == encoded


def test_contribution_ledger_contract_validation_and_uniqueness():
    ledger = _ledger()
    payload = ledger.to_canonical_json()
    assert b'"amount_minor":"4900"' in payload
    assert payload.endswith(b"\n")

    with pytest.raises(ValidationError):
        ContributionLedgerV1.model_validate(
            {
                **ledger.model_dump(mode="json"),
                "contributions": [
                    _contribution().model_dump(mode="json"),
                    _contribution().model_dump(mode="json"),
                ],
            }
        )

    # R-1 currency / amount normalization at field edges.
    for value in ["  usd", "USD", " usd "]:
        assert _contribution(currency=value).currency == "usd"
    for value in ["4900", 4900, Decimal(4900)]:
        assert _contribution(amount_minor=value).amount_minor == Decimal(4900)
    with pytest.raises(ValidationError):
        _contribution(currency="zzz")
    with pytest.raises(ValidationError):
        _contribution(amount_minor=12.5)


def test_thin_slice_detects_only_failed_invoice():
    graph, candidates = build_thin_slice_failed_payments()
    assert len(candidates.candidates) == 1
    item = candidates.candidates[0]
    assert item.event_family == "failed_payment"
    assert item.economic_unit_key == "stripe_invoice:inv_failed_001"
    assert item.lineage["invoice_node_id"] == "stripe:invoice:inv_failed_001"
    assert "cus_synth_001" in {
        node.source_id
        for node in graph.nodes
        if node.node_id
        in next(
            cluster.member_node_ids
            for cluster in graph.customers
            if cluster.customer_token == item.customer_token
        )
    }
    assert all(c.economic_unit_key != "stripe_invoice:inv_paid_001" for c in candidates.candidates)


def test_thin_slice_observed_face_value_contribution():
    _graph, candidates = build_thin_slice_failed_payments()
    snapshots = load_thin_slice_snapshots()
    ledger = build_contribution_ledger(candidates, snapshots["stripe"])
    assert len(ledger.contributions) == 1
    row = ledger.contributions[0]
    assert row.pile_id == "payment_rescue"
    assert row.value_basis == "observed_face_value"
    assert row.currency == "usd"
    assert row.amount_minor == Decimal(4900)
    assert row.economic_unit_key == "stripe_invoice:inv_failed_001"
    assert isinstance(row.amount_minor, Decimal)
    assert row.amount_minor == Decimal("4900")


def test_paid_invoice_never_candidates():
    stripe = json.loads((EVENT_FIXTURES / "paid_invoice_only.json").read_text(encoding="utf-8"))
    graph = _graph_for_stripe(stripe)
    candidates = detect_failed_payments(stripe, graph, run_id="run_paid_neg")
    assert candidates.candidates == []


def test_duplicate_economic_unit_key_rejected():
    _graph, candidates = build_thin_slice_failed_payments()
    snapshots = load_thin_slice_snapshots()
    first = build_contribution_ledger(candidates, snapshots["stripe"])
    second = build_contribution_ledger(candidates, snapshots["stripe"])
    assert len(first.contributions) == 1
    assert len(second.contributions) == 1
    assert first.contributions[0].economic_unit_key == "stripe_invoice:inv_failed_001"
    assert second.to_canonical_json() == first.to_canonical_json()

    with pytest.raises(ValidationError):
        ContributionLedgerV1.model_validate(
            {
                **first.model_dump(mode="json"),
                "contributions": [
                    first.contributions[0].model_dump(mode="json"),
                    first.contributions[0].model_dump(mode="json"),
                ],
            }
        )


def test_missing_amount_or_currency_fails_closed():
    missing = json.loads((EVENT_FIXTURES / "missing_amount.json").read_text(encoding="utf-8"))
    graph = _graph_for_stripe(missing)
    candidates = detect_failed_payments(missing, graph, run_id="run_missing")
    assert len(candidates.candidates) == 1
    with pytest.raises(ValueError, match="amount_due_cents"):
        build_contribution_ledger(candidates, missing)

    bad_currency = json.loads(
        (EVENT_FIXTURES / "unsupported_currency.json").read_text(encoding="utf-8")
    )
    graph2 = _graph_for_stripe(bad_currency)
    candidates2 = detect_failed_payments(bad_currency, graph2, run_id="run_currency")
    assert len(candidates2.candidates) == 1
    with pytest.raises(ValueError, match="unsupported currency"):
        build_contribution_ledger(candidates2, bad_currency)


def test_event_value_writes_reject_escape_without_partial_state(tmp_path):
    _graph, candidates = build_thin_slice_failed_payments()
    snapshots = load_thin_slice_snapshots()
    ledger = build_contribution_ledger(candidates, snapshots["stripe"])
    projection = public_contribution_projection(ledger)
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "out"
    root.mkdir()
    before_outside = _list_tree(outside)
    before_root = _list_tree(root)

    for writer, payload in (
        (write_recovery_candidates, candidates),
        (write_contribution_ledger, ledger),
        (write_public_contribution_projection, projection),
    ):
        for relative in ("../outside/x.json", "/tmp/x.json", "sibling/../../x.json"):
            with pytest.raises(ValueError):
                writer(root, relative, payload)
            assert _list_tree(root) == before_root
            assert _list_tree(outside) == before_outside

        # symlink escape
        link = root / "escape"
        link.symlink_to(outside)
        with pytest.raises(ValueError):
            writer(root, "escape/x.json", payload)
        assert _list_tree(outside) == before_outside
        link.unlink()


def test_public_contribution_projection_omits_raw_identity():
    _graph, candidates = build_thin_slice_failed_payments()
    snapshots = load_thin_slice_snapshots()
    ledger = build_contribution_ledger(candidates, snapshots["stripe"])
    projection = public_contribution_projection(ledger)
    text = projection.to_canonical_json().decode("utf-8")
    assert "customer_token" in text
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
    ):
        assert forbidden not in text


def test_event_value_round_trip_and_public_safety_scan(tmp_path):
    import importlib.util

    _graph, candidates = build_thin_slice_failed_payments()
    snapshots = load_thin_slice_snapshots()
    ledger = build_contribution_ledger(candidates, snapshots["stripe"])
    projection = public_contribution_projection(ledger)

    for parser, payload in (
        (parse_events_json, candidates.to_canonical_json()),
        (parse_value_json, ledger.to_canonical_json()),
        (parse_value_json, projection.to_canonical_json()),
    ):
        assert parser(payload).to_canonical_json() == payload

    with pytest.raises(ValueError, match="unknown major"):
        parse_events_json(
            b'{"schema_version":"recovery-candidate.v2","run_id":"r",'
            b'"built_at":"2026-07-29T18:00:10.000Z","candidates":[]}\n'
        )
    with pytest.raises(ValueError, match="malformed"):
        parse_value_json(b"{")
    with pytest.raises(ValueError, match="duplicate JSON object key"):
        parse_value_json(b'{"schema_version":"contribution-public.v1","schema_version":"x"}\n')

    write_recovery_candidates(tmp_path, "events/recovery-candidates.json", candidates)
    write_contribution_ledger(tmp_path, "value/contribution-ledger.json", ledger)
    write_public_contribution_projection(tmp_path, "value/contribution-public.json", projection)

    spec = importlib.util.spec_from_file_location(
        "public_safety", ROOT / "scripts" / "public_safety.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    public_text = (tmp_path / "value" / "contribution-public.json").read_text(encoding="utf-8")
    assert module.scan_contract_artifact_text("public", public_text) == []
    dirty = public_text.replace('"usd"', '"usd","email":"a@b.com"')
    assert module.scan_contract_artifact_text("dirty", dirty)

    assert module.main(ROOT) == 0
    assert module.scan_generated_contract_artifacts(ROOT) == []


def test_ng1_event_value_builders_do_not_open_sockets(monkeypatch):
    def boom(*_args, **_kwargs):
        raise AssertionError("socket connection attempted")

    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket.socket, "connect", boom)
    build_thin_slice_failed_payments()
    snapshots = load_thin_slice_snapshots()
    _graph, candidates = build_thin_slice_failed_payments()
    build_contribution_ledger(candidates, snapshots["stripe"])


def test_r1_path_normalization_edges(tmp_path):
    _graph, candidates = build_thin_slice_failed_payments()
    snapshots = load_thin_slice_snapshots()
    ledger = build_contribution_ledger(candidates, snapshots["stripe"])
    for relative in ["  value/out.json", "value/out.json  ", "./value/out.json"]:
        path = write_contribution_ledger(tmp_path, relative, ledger)
        assert path.exists()
        path.unlink()
