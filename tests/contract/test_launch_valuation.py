"""Recalculate launch values from source evidence, independently of the renderer."""

from copy import deepcopy
from decimal import Decimal, ROUND_HALF_UP
import json

import pytest
from pydantic import ValidationError

from found_money.build import load_source_config
from found_money.contracts.value import RecurringValuationV1
from found_money.events.library import detect_event_families
from found_money.identity import build_identity_graph, normalize_source_records
from found_money.rendering import recovery_room_static_assets
from found_money.safety.output_scan import verified_texture_asset
from found_money.value import build_value_ledger
from found_money.value.recurring import apply_recurring_valuation, public_valuation_receipt
import importlib.util
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "launch_demo_generator",
    Path(__file__).resolve().parents[2] / "scripts/generate_demo_dataset.py",
)
_demo = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _demo
_spec.loader.exec_module(_demo)
NOW, build_dataset, hubspot_snapshot, stripe_snapshot = (
    _demo.NOW,
    _demo.build_dataset,
    _demo.hubspot_snapshot,
    _demo.stripe_snapshot,
)


@pytest.fixture(scope="module")
def source():
    data = build_dataset()
    return {"hubspot": hubspot_snapshot(data), "stripe": stripe_snapshot(data)}


def value(source, **kwargs):
    graph = build_identity_graph(
        normalize_source_records(source, default_observed_at=NOW),
        run_id="run_launch_values",
        built_at=NOW,
    )
    candidates = detect_event_families(
        source, graph, run_id="run_launch_values", built_at=NOW
    ).candidates
    base = build_value_ledger(candidates, source, built_at=NOW)
    ledger = apply_recurring_valuation(
        base, candidates, source, graph, business_model="saas", clock=NOW, **kwargs
    )
    return base, ledger


def test_recurring_values_use_each_customers_amount_and_leave_invoice_deals_unchanged(source):
    base, ledger = value(source, tenure_months=Decimal(16))
    originals = {r.candidate_key: r for r in base.contributions}
    models = [r for r in ledger.contributions if r.recurring_valuation]
    assert len(models) == 28
    assert len({r.recurring_valuation.monthly_amount_minor for r in models}) > 5
    for row in models:
        assert row.amount_minor == row.recurring_valuation.monthly_amount_minor * 16
        assert row.recurring_valuation.basis == "owner_tenure"
    for row in ledger.contributions:
        if not row.recurring_valuation:
            assert row.amount_minor == originals[row.candidate_key].amount_minor
    assert not any(r.pile_id in {"expired_trial", "trial_no_convert"} for r in ledger.contributions)
    receipt = public_valuation_receipt(ledger)
    for group in receipt["model_groups"]:
        assert int(group["unit_value_minor"]) == Decimal(group["monthly_amount_minor"]) * Decimal(
            group["tenure_months"]
        )
        assert group["total_minor"] == int(group["unit_value_minor"]) * group["unit_count"]
    assert sum(g["total_minor"] for g in receipt["model_groups"]) == sum(
        int(r.amount_minor) for r in models
    )
    assert set(receipt["currencies"]) == {"usd", "eur", "gbp"}
    for cur, bucket in receipt["currencies"].items():
        assert bucket["total_minor"] == sum(
            int(r.amount_minor) for r in ledger.contributions if r.currency == cur
        )
        assert bucket["total_minor"] == sum(
            bucket[k] for k in ("observed_minor", "modeled_minor", "recorded_minor")
        )


def test_source_tenure_is_recalculable_and_future_cancellation_is_excluded(source):
    _, ledger = value(source)
    from datetime import datetime

    months = []
    for row in source["stripe"]["subscriptions"]:
        if row.get("status") == "canceled":
            start = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(row["canceled_at"].replace("Z", "+00:00"))
            if start < end <= NOW:
                months.append(Decimal(str((end - start).total_seconds())) / Decimal("2629800"))
    expected = (sum(months) / len(months)).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
    models = [r.recurring_valuation for r in ledger.contributions if r.recurring_valuation]
    assert models and all(
        m.tenure_multiple_months == expected and m.history_count == len(months) for m in models
    )
    altered = deepcopy(source)
    for row in altered["stripe"]["subscriptions"]:
        if row.get("status") == "canceled":
            row["canceled_at"] = "2099-01-01T00:00:00Z"
    _, future = value(altered)
    assert not any(r.recurring_valuation for r in future.contributions)


def test_missing_monthly_interval_is_unquantified_and_model_tampering_rejected(source):
    altered = deepcopy(source)
    for row in altered["stripe"]["subscriptions"]:
        row.pop("billing_interval", None)
    _, ledger = value(altered, tenure_months=Decimal(16))
    assert not any(
        r.pile_id in {"canceled_customer", "disappeared_high_value_customer"}
        for r in ledger.contributions
    )
    assert any(g.reason_code == "insufficient_history" for g in ledger.data_gaps)
    _, good = value(source, tenure_months=Decimal(16))
    model = next(r.recurring_valuation for r in good.contributions if r.recurring_valuation)
    raw = model.model_dump()
    raw["expected_value_minor"] += 1
    with pytest.raises(ValidationError):
        RecurringValuationV1.model_validate(raw)


def test_crm_only_file_configuration_and_explicit_clock(tmp_path, source):
    (tmp_path / "crm.json").write_text(json.dumps(source["hubspot"]))
    config = {
        "schema_version": "found-money-build-source.v1",
        "mode": "file",
        "run_mode": "public",
        "clock": NOW.isoformat(),
        "sources": {"hubspot_snapshot": "crm.json"},
        "credentials": {"credential_mode": "none", "runtime_mode": "test", "scopes": []},
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    snapshots, safe, _ = load_source_config(path)
    assert set(snapshots) == {"hubspot"}
    assert safe["clock"] == NOW.isoformat()


def test_texture_carrier_only_accepts_exact_reviewed_bytes():
    payload = recovery_room_static_assets()["assets/atlas-texture.js"]
    assert verified_texture_asset("assets/atlas-texture.js", payload)
    with pytest.raises(ValueError):
        verified_texture_asset("assets/atlas-texture.js", payload + b"; alert('changed')")
    assert not verified_texture_asset("assets/something-else.js", payload)


def test_realistic_build_keeps_campaign_audiences_and_profile_bound(tmp_path, monkeypatch, source):
    from found_money.build import build
    from found_money.contracts.map import MoneyMapV1
    from found_money.contracts.campaign import CompleteRecoveryPlaySetV1

    monkeypatch.chdir(tmp_path)
    for system in source:
        (tmp_path / f"{system}.json").write_text(json.dumps(source[system]))
    config = {
        "schema_version": "found-money-build-source.v1",
        "mode": "file",
        "run_mode": "public",
        "clock": NOW.isoformat(),
        "business_model": "saas",
        "strategy_provider": "skill",
        "data_origin": "synthetic",
        "sources": {f"{system}_snapshot": f"{system}.json" for system in source},
        "credentials": {"credential_mode": "none", "runtime_mode": "test", "scopes": []},
        "business_profile": {
            "product": "scheduling software for plumbers",
            "proof": "product walkthrough",
            "margin_percent": "75",
            "capacity": 20,
            "destination": "account billing portal",
            "business_model": "saas",
            "tenure_multiple_months": "16",
        },
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    result = build(output_root="out", source_config=path)
    root = result.output_root
    money_map = MoneyMapV1.model_validate_json((root / "money-map.json").read_bytes())
    plays = CompleteRecoveryPlaySetV1.model_validate_json(
        (root / "recovery-plays.json").read_bytes()
    )
    segments = json.loads((root / "launch-pack/public/segments.json").read_text())["segments"]
    assert len(segments) == 3
    for play in plays.plays:
        expected_segment = (
            "seg_payment_rescue" if play.pile_id == "payment_rescue" else f"seg_{play.pile_id}"
        )
        segment = next(s for s in segments if play.play_id in s["play_ids"])
        assert segment["segment_id"] == expected_segment
        assert segment["eligible_member_count"] > 0
        for email in play.email_sequence:
            assert "ev_business_" not in email.body.text + email.cta.text
            assert "account billing portal" in email.cta.text
    assert len(money_map.recommended_play_ids) == 3
    receipt = json.loads((root / "provenance/valuation-summary.json").read_text())
    assert all(
        int(amount) == receipt["currencies"][currency]["total_minor"]
        for currency, amount in money_map.identified_opportunity_minor.items()
    )
    room = (root / "index.html").read_text()
    assert "Example business · Illustrative data and estimates" in room
    assert "SYNTHETIC DEMO" not in room
    assert (root / "print-report.pdf").stat().st_size > 10_000


def test_guide_passes_validated_answers_into_build_before_opening(tmp_path, monkeypatch):
    import found_money.build as build_module
    import found_money.profile.cli as cli
    from found_money.profile import BusinessProfileV1

    answers = {
        "product": "member scheduling",
        "proof": "product walkthrough",
        "margin_percent": "75",
        "capacity": "20",
        "destination": "billing portal",
        "business_model": "saas",
        "tenure_multiple_months": "12",
    }
    received = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_ask", lambda question: answers.get(question["id"]))
    monkeypatch.setattr("builtins.input", lambda _: "confirm")
    monkeypatch.setattr(cli, "_open_html", lambda path: received.append(path))

    def capture(**kwargs):
        assert isinstance(kwargs["business_profile"], BusinessProfileV1)
        received.append(kwargs["business_profile"])
        (tmp_path / "out").mkdir()
        (tmp_path / "out/index.html").write_text("<html></html>")
        (tmp_path / "out/run.json").write_text("{}")

    monkeypatch.setattr(build_module, "build", capture)
    assert cli.guided_session("out") == 0
    assert received[0].product == "member scheduling"
    assert received[0].tenure_multiple_months == 12
    assert received[1] == tmp_path / "out/index.html"
