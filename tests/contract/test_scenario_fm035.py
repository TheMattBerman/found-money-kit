"""FM-035 service/membership scenario on the shared FM-033/034 harness."""

from __future__ import annotations

import io
import json
import re
import socket
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from found_money.build import (
    BuildPathError,
    _fixture_snapshots,
    _run_id_for,
    _safe_source_config,
    build,
)
from found_money.contracts.campaign import (
    CompleteRecoveryPlaySetV1,
    StrategyReviewerPacketV2,
)
from found_money.contracts.scenarios import ScenarioDefinitionV1, parse_scenario_definition
from found_money.events import detect_event_families
from found_money.identity import build_identity_graph, normalize_source_records
from found_money.imports.appointments import parse_appointments_file_bytes
from found_money.imports.proposals import parse_proposals_file_bytes
from found_money.receipts import sha256_bytes
from found_money.rendering import render_print_report_html
from found_money.rendering.proof import (
    CANONICAL_PRINT_REPORT_PAGE_COUNT,
    NAMED_VIEWPORT_DIMENSIONS,
    PDF_SIGNATURE,
    PNG_SIGNATURE,
    PRINT_PAGE_VIEWPORT,
    assert_us_letter_pdf,
    canonical_print_report_page_artifacts,
    count_print_report_html_pages,
    png_dimensions,
)
from found_money.safety import scan_output_tree
from found_money.safety.output_scan import TEXT_SUFFIXES, scan_text_artifact
from found_money.scenarios import (
    RAW_SERVICE_SOURCE_IDS,
    SCENARIO_PUBLIC_PATHS,
    SYNTHETIC_ECOMMERCE_V1,
    SYNTHETIC_SAAS_V1,
    SYNTHETIC_SERVICE_V1,
    SYNTHETIC_SERVICE_V1_APPOINTMENTS_OMITTED,
    SYNTHETIC_SERVICE_V1_CRM_OMITTED,
    SYNTHETIC_SERVICE_V1_PAYMENT_OMITTED,
    SYNTHETIC_SERVICE_V1_PROPOSALS_OMITTED,
    apply_complete_plays_to_money_map,
    load_scenario_definition,
    load_scenario_snapshot_bytes,
    load_scenario_snapshots,
    load_scenario_source_config_bytes,
    run_scenario_engine,
    validate_scenario_output_tree,
    validate_scenario_reviewer_packet,
)
from found_money.strategy import apply_complete_plays_to_money_map as strategy_helper

from _reviewer_packets import synthetic_reviewer_packet

ROOT = Path(__file__).resolve().parents[2]
FM035_FIXTURE = ROOT / "tests" / "fixtures" / "service" / "strategy" / "fm035"
REVIEWER_PACKET = FM035_FIXTURE / "blind-reviewer-packet.json"
OMITTED_APPOINTMENTS_CONFIG = ROOT / "configs" / "synthetic-service-v1-appointments-omitted.json"
OMITTED_PROPOSALS_CONFIG = ROOT / "configs" / "synthetic-service-v1-proposals-omitted.json"
OMITTED_CRM_CONFIG = ROOT / "configs" / "synthetic-service-v1-crm-omitted.json"
OMITTED_PAYMENT_CONFIG = ROOT / "configs" / "synthetic-service-v1-payment-omitted.json"
REQUIRED_FAMILIES = {
    "no_show_rebook",
    "trial_no_convert",
    "canceled_customer",
    "silent_proposal",
    "disappeared_high_value_customer",
}
PLAY_IDS = [
    "membership-silence-reopen",
    "membership-visit-proof",
    "membership-capacity-lane",
]
FORBIDDEN = (
    "payment_rescue",
    "payment rescue",
    "4900",
    "card-retry",
    "card retry",
    "billing portal",
    "billing-portal",
    "annual subscription",
    "catalog",
    "vip-silence-reopen",
    "vip-catalog-proof",
    "vip-capacity-lane",
    "payment-rescue-friction-fix",
    "ecommerce",
    "18500",
)
CUSTOMER_LEVEL_PUBLIC_KEYS = {
    "account_token",
    "cart_id",
    "charge_id",
    "customer_external_id",
    "customer_id",
    "customer_token",
    "member_node_ids",
    "member_rows",
    "members",
    "order_id",
    "refund_id",
    "source_id",
    "appointment_id",
    "proposal_id",
}
_PROVIDER_SHAPED_ID_RE = re.compile(
    r"\b(?:cus_|ch_|re_|apt_|prp_|sub_|in_|hs_(?:contact|ct|dl|deal)_)[A-Za-z0-9_]+"
)
_RECORD_PSEUDONYM_RE = re.compile(r"\b(?:cust_|rec_)[0-9a-f]{8,}\b")
LOCKED_PLAYS_DIGEST = "d66b01428015c3bb2a9f9eefd30ee64d5624290cddccf030c72ce418db541165"


def _write_packaged_config(directory: Path, fixture_id: str = SYNTHETIC_SERVICE_V1) -> Path:
    path = directory / f"{fixture_id}.json"
    path.write_bytes(load_scenario_source_config_bytes(fixture_id))
    return path


def _public_safe_config(fixture_id: str = SYNTHETIC_SERVICE_V1) -> dict:
    return _safe_source_config(source_mode="fixture", run_mode="public", fixture=fixture_id)


def _locked_run_id(fixture_id: str = SYNTHETIC_SERVICE_V1) -> str:
    return _run_id_for(_fixture_snapshots(fixture_id), _public_safe_config(fixture_id))


def _engine(run_id: str | None = None, *, fixture_id: str = SYNTHETIC_SERVICE_V1):
    return run_scenario_engine(
        run_id=run_id or _locked_run_id(fixture_id),
        safe_config=_public_safe_config(fixture_id),
        fixture_id=fixture_id,
    )


def test_service_fixture_uses_appointments_proposals_crm_payments_not_prior_bytes():
    service = load_scenario_snapshot_bytes(SYNTHETIC_SERVICE_V1)
    saas = load_scenario_snapshot_bytes(SYNTHETIC_SAAS_V1)
    ecommerce = load_scenario_snapshot_bytes(SYNTHETIC_ECOMMERCE_V1)
    assert set(service) == {"hubspot", "appointments", "proposals", "stripe"}
    assert service["stripe"] != saas["stripe"]
    assert service["stripe"] != ecommerce["stripe"]
    assert service["hubspot"] != saas["hubspot"]
    blob = b"".join(service.values()).decode("utf-8")
    assert "in_saas_fp_001" not in blob
    assert "cus_ecom_vip_001" not in blob
    assert "cus_svc_hv_001" in blob
    assert "apt_svc_ns_001" in blob
    assert "prp_svc_sp_001" in blob
    csv_apt = (
        ROOT
        / "found_money"
        / "scenarios"
        / "fixtures"
        / "service-v1"
        / "inputs"
        / "appointments.csv"
    ).read_bytes()
    json_apt = (
        ROOT
        / "found_money"
        / "scenarios"
        / "fixtures"
        / "service-v1"
        / "inputs"
        / "appointments.json"
    ).read_bytes()
    csv_prp = (
        ROOT / "found_money" / "scenarios" / "fixtures" / "service-v1" / "inputs" / "proposals.csv"
    ).read_bytes()
    json_prp = (
        ROOT / "found_money" / "scenarios" / "fixtures" / "service-v1" / "inputs" / "proposals.json"
    ).read_bytes()
    assert (
        parse_appointments_file_bytes(csv_apt, filename="appointments.csv").to_canonical_json()
        == service["appointments"]
    )
    assert (
        parse_appointments_file_bytes(json_apt, filename="appointments.json").to_canonical_json()
        == service["appointments"]
    )
    assert (
        parse_proposals_file_bytes(csv_prp, filename="proposals.csv").to_canonical_json()
        == service["proposals"]
    )
    assert (
        parse_proposals_file_bytes(json_prp, filename="proposals.json").to_canonical_json()
        == service["proposals"]
    )


def test_definition_is_extra_forbid_and_hash_locked():
    definition = load_scenario_definition(SYNTHETIC_SERVICE_V1)
    raw = (
        ROOT / "found_money" / "scenarios" / "fixtures" / "service-v1" / "definition.json"
    ).read_bytes()
    assert parse_scenario_definition(raw).to_canonical_json() == raw
    payload = json.loads(raw.decode("utf-8"))
    payload["unexpected"] = True
    with pytest.raises(ValidationError):
        ScenarioDefinitionV1.model_validate(payload)
    from found_money.scenarios.transports import packaged_fixture_hashes

    assert packaged_fixture_hashes(SYNTHETIC_SERVICE_V1) == definition.fixture_hashes
    assert sha256_bytes(load_scenario_source_config_bytes(SYNTHETIC_SERVICE_V1)) == (
        definition.source_config_hash
    )
    assert set(definition.expected_event_families) == REQUIRED_FAMILIES
    assert definition.expected_play_ids == PLAY_IDS
    assert len(definition.expected_card_ids) == 9
    assert definition.inherited_proof_contract.claims_new_visual_baseline is False
    assert definition.inherited_proof_contract.claims_print_ratification is False
    assert definition.high_value_cart_state is None
    omitted_a = load_scenario_definition(SYNTHETIC_SERVICE_V1_APPOINTMENTS_OMITTED)
    omitted_p = load_scenario_definition(SYNTHETIC_SERVICE_V1_PROPOSALS_OMITTED)
    omitted_c = load_scenario_definition(SYNTHETIC_SERVICE_V1_CRM_OMITTED)
    omitted_pay = load_scenario_definition(SYNTHETIC_SERVICE_V1_PAYMENT_OMITTED)
    assert omitted_a.omitted_service_sources == ["appointments"]
    assert omitted_p.omitted_service_sources == ["proposals"]
    assert omitted_c.omitted_service_sources == ["hubspot"]
    assert omitted_pay.omitted_service_sources == ["stripe"]
    assert "no_show_rebook" not in omitted_a.expected_event_families
    assert "silent_proposal" not in omitted_p.expected_event_families
    assert set(omitted_c.expected_event_families) == REQUIRED_FAMILIES
    assert set(omitted_pay.expected_event_families) == {"no_show_rebook", "silent_proposal"}
    assert "hubspot" not in omitted_c.sources
    assert "stripe" not in omitted_pay.sources


def test_appointment_proposal_identity_uses_declared_external_id_not_raw_equal_ids():
    snapshots = load_scenario_snapshots(SYNTHETIC_SERVICE_V1)
    stripe_ids = {item["id"] for item in snapshots["stripe"]["customers"]}
    apt_ids = {item["customer_id"] for item in snapshots["appointments"]["appointments"]}
    prp_ids = {item["customer_id"] for item in snapshots["proposals"]["proposals"]}
    assert stripe_ids.isdisjoint(apt_ids)
    assert stripe_ids.isdisjoint(prp_ids)
    clock = load_scenario_definition(SYNTHETIC_SERVICE_V1).clock
    graph = build_identity_graph(
        normalize_source_records(snapshots, default_observed_at=clock),
        run_id="run_identity_proof",
        built_at=clock,
    )
    assert len(graph.customers) == 5
    assert graph.ambiguous_identities == []
    namespaces = {
        edge.match_namespace for edge in graph.edges if edge.match_rule == "declared_external_id"
    }
    assert "appointments.customer_id" in namespaces
    assert "proposals.customer_id" in namespaces
    assert all(edge.match_rule != "fuzzy" for edge in graph.edges)


def test_detector_emits_five_service_families_without_payment_rescue_or_orders():
    snapshots = load_scenario_snapshots(SYNTHETIC_SERVICE_V1)
    clock = load_scenario_definition(SYNTHETIC_SERVICE_V1).clock
    graph = build_identity_graph(
        normalize_source_records(snapshots, default_observed_at=clock),
        run_id="run_detector_proof",
        built_at=clock,
    )
    detection = detect_event_families(snapshots, graph, run_id="run_detector_proof", built_at=clock)
    families = {item.event_family for item in detection.candidates.candidates}
    assert families == REQUIRED_FAMILIES
    blob = detection.candidates.to_canonical_json().decode("utf-8")
    assert "payment_rescue" not in blob
    assert "lapsed_repeat_buyer" not in blob
    assert "overdue_reorder" not in blob
    engine = _engine()
    by_customer: dict[str, set[str]] = {}
    for candidate in engine.candidates.candidates:
        by_customer.setdefault(candidate.customer_token, set()).add(candidate.event_family)
    assert len(by_customer) == 5
    assert all(len(items) == 1 for items in by_customer.values())
    assert set().union(*by_customer.values()) == REQUIRED_FAMILIES


def test_exact_values_currencies_and_primary_rank():
    engine = _engine()
    ranked = sorted(engine.enriched_money_map.piles, key=lambda item: item.rank)
    assert ranked[0].pile_id == "disappeared_high_value_customer"
    assert str(ranked[0].selected_value_minor) == "22000"
    assert ranked[0].currency == "usd"
    silent = next(item for item in ranked if item.pile_id == "silent_proposal")
    assert str(silent.selected_value_minor) == "12500"
    canceled = next(item for item in ranked if item.pile_id == "canceled_customer")
    assert str(canceled.selected_value_minor) == "2400"
    trial = next(item for item in ranked if item.pile_id == "trial_no_convert")
    assert str(trial.selected_value_minor) == "1800"
    assert all(
        item.pile_id != "no_show_rebook" or item.value_basis == "unquantified" for item in ranked
    )
    assert not any(row.pile_id == "no_show_rebook" for row in engine.ledger.contributions)


def test_canonical_run_binds_durable_exclusion_reason():
    engine = _engine()
    assert engine.event_public.exclusion_count == 1
    assert engine.event_public.exclusions_by_reason == {"rebooking": 1}
    assert engine.aggregate_receipt.exclusion_count == 1
    assert engine.aggregate_receipt.exclusions_by_reason == {"rebooking": 1}
    assert engine.enriched_money_map.event_exclusions_by_reason == {"rebooking": 1}
    assert {item.event_family for item in engine.candidates.candidates} == REQUIRED_FAMILIES
    assert any(
        item.event_family == "no_show_rebook" and item.reason_code == "rebooking"
        for item in engine.exclusions.exclusions
    )


def test_omitted_required_sources_named_gaps_and_withholding():
    appointments = _engine(fixture_id=SYNTHETIC_SERVICE_V1_APPOINTMENTS_OMITTED)
    assert appointments.event_public.named_data_gaps == ["missing_appointments"]
    assert "missing_appointments" in appointments.data_gap_details
    assert "no_show_rebook_dependent_output" in appointments.withheld_asset_ids
    assert "no_show_rebook" not in appointments.event_public.candidates_by_family
    assert appointments.enriched_money_map.named_data_gaps == ["missing_appointments"]
    proposals = _engine(fixture_id=SYNTHETIC_SERVICE_V1_PROPOSALS_OMITTED)
    assert proposals.event_public.named_data_gaps == ["missing_proposals"]
    assert "missing_proposals" in proposals.data_gap_details
    assert "silent_proposal_dependent_output" in proposals.withheld_asset_ids
    assert "silent_proposal" not in proposals.event_public.candidates_by_family
    crm = _engine(fixture_id=SYNTHETIC_SERVICE_V1_CRM_OMITTED)
    assert crm.event_public.named_data_gaps == ["missing_crm"]
    assert "crm_dependent_output" in crm.withheld_asset_ids
    assert set(crm.event_public.candidates_by_family) == REQUIRED_FAMILIES
    payment = _engine(fixture_id=SYNTHETIC_SERVICE_V1_PAYMENT_OMITTED)
    assert payment.event_public.named_data_gaps == ["missing_payment"]
    assert "payment_dependent_output" in payment.withheld_asset_ids
    assert "trial_no_convert" not in payment.event_public.candidates_by_family
    assert "canceled_customer" not in payment.event_public.candidates_by_family
    assert "disappeared_high_value_customer" not in payment.event_public.candidates_by_family
    assert "silent_proposal" in payment.event_public.candidates_by_family
    assert "no_show_rebook" in payment.event_public.candidates_by_family
    assert "22000" not in json.dumps(payment.enriched_money_map.canonical_dict())
    assert payment.enriched_money_map.strategy_stage == "needs_strategy_review"
    assert list(payment.enriched_money_map.recommended_play_ids) == []
    assert list(payment.strategy_run.recovery_plays.plays) == []
    assert payment.strategy_run.receipt.status == "needs_strategy_review"
    assert payment.strategy_run.receipt.failure_code == "missing_payment"
    assert payment.strategy_run.differentiation is None
    plays_blob = payment.strategy_run.recovery_plays.to_canonical_json().decode("utf-8")
    assert "membership-silence-reopen" not in plays_blob
    assert "22000" not in plays_blob
    assert list(payment.aggregate_receipt.play_ids) == []
    assert list(payment.aggregate_receipt.card_ids) == []
    assert payment.aggregate_receipt.primary_play_id is None
    assert _locked_run_id() != _locked_run_id(SYNTHETIC_SERVICE_V1_APPOINTMENTS_OMITTED)
    assert _locked_run_id() != _locked_run_id(SYNTHETIC_SERVICE_V1_PROPOSALS_OMITTED)
    assert _locked_run_id() != _locked_run_id(SYNTHETIC_SERVICE_V1_CRM_OMITTED)
    assert _locked_run_id() != _locked_run_id(SYNTHETIC_SERVICE_V1_PAYMENT_OMITTED)


def test_engine_ranks_membership_first_and_plays_are_not_copied():
    engine = _engine()
    ranked = sorted(engine.enriched_money_map.piles, key=lambda item: item.rank)
    assert ranked[0].navigation is not None
    assert ranked[0].navigation.play_id == engine.enriched_money_map.recommended_play_ids[0]
    blob = engine.strategy_run.recovery_plays.to_canonical_json().decode("utf-8").casefold()
    for token in FORBIDDEN:
        assert token not in blob
    for family in REQUIRED_FAMILIES - {"disappeared_high_value_customer"}:
        assert family not in blob
        assert family.replace("_", " ") not in blob
    cards = [
        card.card_id
        for play in engine.strategy_run.recovery_plays.plays
        for card in play.concept_cards
    ]
    assert cards == load_scenario_definition(SYNTHETIC_SERVICE_V1).expected_card_ids
    packet_text = engine.strategy_run.packet.to_canonical_json().decode("utf-8")
    assert "Membership studio" in packet_text
    assert "Three return-visit studies" in packet_text
    assert "22000" in packet_text


def test_complete_play_helper_records_all_ids_and_rejects_unknown_duplicate_order():
    engine = _engine("run_complete_helper")
    enriched = strategy_helper(engine.money_map, engine.strategy_run.recovery_plays)
    assert enriched.recommended_play_ids == PLAY_IDS
    plays = sorted(engine.strategy_run.recovery_plays.plays, key=lambda item: item.rank)
    unknown = SimpleNamespace(
        run_id=engine.money_map.run_id,
        plays=[
            SimpleNamespace(play_id=play.play_id, pile_id="missing_pile", rank=play.rank)
            for play in plays
        ],
    )
    with pytest.raises(ValueError, match="unknown pile"):
        apply_complete_plays_to_money_map(engine.money_map, unknown)  # type: ignore[arg-type]
    duplicate = SimpleNamespace(
        run_id=engine.money_map.run_id,
        plays=[
            SimpleNamespace(
                play_id="same-play-identifier",
                pile_id="disappeared_high_value_customer",
                rank=1,
            ),
            SimpleNamespace(
                play_id="same-play-identifier",
                pile_id="disappeared_high_value_customer",
                rank=2,
            ),
            SimpleNamespace(
                play_id="other-play-identifier",
                pile_id="disappeared_high_value_customer",
                rank=3,
            ),
        ],
    )
    with pytest.raises(ValueError, match="duplicate play"):
        apply_complete_plays_to_money_map(engine.money_map, duplicate)  # type: ignore[arg-type]


def test_saas_and_ecommerce_fixtures_remain_byte_stable_beside_service():
    saas = load_scenario_snapshot_bytes(SYNTHETIC_SAAS_V1)
    ecommerce = load_scenario_snapshot_bytes(SYNTHETIC_ECOMMERCE_V1)
    service = load_scenario_snapshot_bytes(SYNTHETIC_SERVICE_V1)
    assert saas["stripe"] != service["stripe"]
    assert ecommerce["stripe"] != service["stripe"]
    saas_def = (
        ROOT / "found_money" / "scenarios" / "fixtures" / "saas-v1" / "definition.json"
    ).read_bytes()
    ecom_def = (
        ROOT / "found_money" / "scenarios" / "fixtures" / "ecommerce-v1" / "definition.json"
    ).read_bytes()
    assert parse_scenario_definition(saas_def).to_canonical_json() == saas_def
    assert parse_scenario_definition(ecom_def).to_canonical_json() == ecom_def


def test_scenario_build_tree_manifest_public_scan_and_tamper(fake_proof, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = _write_packaged_config(tmp_path)
    result = build(output_root="svc-out", source_config=config)
    validate_scenario_output_tree(result.output_root)
    text_violations: list[str] = []
    for path in result.output_root.rglob("*"):
        if path.is_file() and path.suffix.lower() in TEXT_SUFFIXES:
            text_violations.extend(
                scan_text_artifact(
                    path.relative_to(result.output_root).as_posix(),
                    path.read_text(encoding="utf-8"),
                    suffix=path.suffix.lower(),
                )
            )
    assert text_violations == []
    target = result.output_root / "money-map.json"
    original = target.read_bytes()
    mutated = json.loads(original.decode("utf-8"))
    mutated["piles"][0]["selected_value_minor"] = "1"
    target.write_bytes((json.dumps(mutated, sort_keys=True, separators=(",", ":")) + "\n").encode())
    with pytest.raises(ValueError, match="hash mismatch"):
        validate_scenario_output_tree(result.output_root)
    _rehash_manifests(result.output_root)
    with pytest.raises(ValueError, match="22000"):
        validate_scenario_output_tree(result.output_root)
    target.write_bytes(original)
    _rehash_manifests(result.output_root)
    validate_scenario_output_tree(result.output_root)
    audit = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in result.output_root.rglob("*")
        if path.is_file() and path.suffix in {".json", ".html", ".md", ".csv"}
    )
    for noun in ("Acme", "Globex", "Salesforce", "Shopify"):
        assert noun not in audit
    for source_id in RAW_SERVICE_SOURCE_IDS:
        assert source_id not in audit
    for token in FORBIDDEN:
        if token == "4900":
            assert re.search(r"(?<![0-9])4900(?![0-9])", audit) is None
        else:
            assert token not in audit.casefold()
    assert "api.stripe.com" not in audit
    assert not (result.output_root / "scenario" / "situations.json").exists()
    _assert_public_tree_has_no_customer_level_rows(result.output_root)


def test_omitted_documented_config_builds_withhold_dependent_outputs(
    fake_proof, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    cases = (
        (
            SYNTHETIC_SERVICE_V1_APPOINTMENTS_OMITTED,
            "svc-apt-omitted",
            "missing_appointments",
            "no_show_rebook_dependent_output",
            OMITTED_APPOINTMENTS_CONFIG,
        ),
        (
            SYNTHETIC_SERVICE_V1_PROPOSALS_OMITTED,
            "svc-prp-omitted",
            "missing_proposals",
            "silent_proposal_dependent_output",
            OMITTED_PROPOSALS_CONFIG,
        ),
        (
            SYNTHETIC_SERVICE_V1_CRM_OMITTED,
            "svc-crm-omitted",
            "missing_crm",
            "crm_dependent_output",
            OMITTED_CRM_CONFIG,
        ),
    )
    for fixture_id, output_root, gap, asset, documented in cases:
        config = tmp_path / f"{fixture_id}.json"
        config.write_bytes(load_scenario_source_config_bytes(fixture_id))
        result = build(output_root=output_root, source_config=config)
        validate_scenario_output_tree(result.output_root)
        events = json.loads((result.output_root / "events" / "public.json").read_text())
        money = json.loads((result.output_root / "money-map.json").read_text())
        aggregate = json.loads(
            (result.output_root / "scenario" / "aggregate-receipt.json").read_text()
        )
        withheld = json.loads(
            (result.output_root / "launch-pack" / "withheld-assets.json").read_text()
        )
        html = (result.output_root / "index.html").read_text()
        assert events["named_data_gaps"] == [gap]
        assert money["named_data_gaps"] == [gap]
        assert aggregate["named_data_gaps"] == [gap]
        assert asset in json.dumps(withheld)
        assert gap in html
        assert gap in json.dumps(withheld)
        plays = json.loads((result.output_root / "recovery-plays.json").read_text())
        play_ids = [play["play_id"] for play in plays["plays"]]
        assert play_ids == PLAY_IDS
        published = "\n".join(
            [
                json.dumps(plays),
                json.dumps(money),
                html,
                (result.output_root / "top-play.html").read_text(),
            ]
        )
        assert "22000" in published
        assert "membership-silence-reopen" in published
        launch = json.loads((result.output_root / "launch-pack" / "manifest.json").read_text())
        assert launch["status"] in {"approval_ready", "partial"}
        assert result.run_id != _locked_run_id()
        assert documented.is_file()
        _assert_public_tree_has_no_customer_level_rows(result.output_root)


def _payment_omitted_published_blob(root: Path) -> str:
    parts = [
        (root / "recovery-plays.json").read_text(encoding="utf-8"),
        (root / "index.html").read_text(encoding="utf-8"),
        (root / "top-play.html").read_text(encoding="utf-8"),
        (root / "launch-pack" / "manifest.json").read_text(encoding="utf-8"),
        (root / "launch-pack" / "recovery-plays.json").read_text(encoding="utf-8"),
    ]
    for path in root.joinpath("launch-pack").rglob("*"):
        if path.is_file() and path.suffix.lower() in {".md", ".html", ".json", ".csv"}:
            parts.append(path.read_text(encoding="utf-8", errors="ignore"))
    return "\n".join(parts)


def _assert_payment_omitted_operator_tree(root: Path, *, include_pdf: bool = False) -> None:
    money = json.loads((root / "money-map.json").read_text(encoding="utf-8"))
    plays = json.loads((root / "recovery-plays.json").read_text(encoding="utf-8"))
    launch = json.loads((root / "launch-pack" / "manifest.json").read_text(encoding="utf-8"))
    withheld = json.loads(
        (root / "launch-pack" / "withheld-assets.json").read_text(encoding="utf-8")
    )
    html = (root / "index.html").read_text(encoding="utf-8")
    top = (root / "top-play.html").read_text(encoding="utf-8")
    assert money["named_data_gaps"] == ["missing_payment"]
    assert money["strategy_stage"] == "needs_strategy_review"
    assert money.get("recommended_play_ids") in (None, [])
    assert "22000" not in json.dumps(money)
    assert plays.get("plays") == []
    published = _payment_omitted_published_blob(root)
    for play_id in PLAY_IDS:
        assert play_id not in published
        assert play_id not in html
        assert play_id not in top
    for card_id in (
        "concept-1-1",
        "concept-1-2",
        "concept-1-3",
        "concept-2-1",
        "concept-2-2",
        "concept-2-3",
        "concept-3-1",
        "concept-3-2",
        "concept-3-3",
    ):
        assert card_id not in published
    assert "22000" not in published
    assert "Strategy status: deferred. No generic fallback play was created." in html
    assert "missing_payment" in html
    assert "silent_proposal" in json.dumps(money)
    assert "12500" in json.dumps(money)
    assert launch["status"] != "approval_ready"
    assert launch["available_play_count"] == 0
    assert launch.get("play_ids") in (None, [])
    withheld_blob = json.dumps(withheld)
    assert "payment_dependent_output" in withheld_blob
    assert "missing_payment" in withheld_blob
    copy_dir = root / "launch-pack" / "copy"
    calendar_dir = root / "launch-pack" / "calendar"
    assert not copy_dir.exists() or list(copy_dir.glob("*.md")) == []
    assert not calendar_dir.exists() or list(calendar_dir.glob("*.md")) == []
    if include_pdf:
        from pypdf import PdfReader

        pdf_text = "".join(
            page.extract_text() or "" for page in PdfReader(root / "print-report.pdf").pages
        )
        assert "22000" not in pdf_text
        for play_id in PLAY_IDS:
            assert play_id not in pdf_text
        assert "missing_payment" in pdf_text
        assert "$125.00" in pdf_text


def test_payment_omitted_build_does_not_publish_ready_plays_or_22000(
    fake_proof, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    config = tmp_path / "payment-omitted.json"
    config.write_bytes(load_scenario_source_config_bytes(SYNTHETIC_SERVICE_V1_PAYMENT_OMITTED))
    result = build(output_root="svc-pay-omitted", source_config=config)
    _assert_payment_omitted_operator_tree(result.output_root)
    assert OMITTED_PAYMENT_CONFIG.is_file()
    _assert_public_tree_has_no_customer_level_rows(result.output_root)


def test_payment_omitted_real_build_validator_and_rehash_reject_play_22000_ready(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    config = tmp_path / "payment-omitted.json"
    config.write_bytes(load_scenario_source_config_bytes(SYNTHETIC_SERVICE_V1_PAYMENT_OMITTED))
    result = build(output_root="svc-pay-omitted-live", source_config=config)
    validate_scenario_output_tree(result.output_root)
    _assert_payment_omitted_operator_tree(result.output_root, include_pdf=True)
    plays_path = result.output_root / "recovery-plays.json"
    original_plays = plays_path.read_bytes()
    mutated = json.loads(original_plays.decode("utf-8"))
    mutated["plays"] = [
        {
            "play_id": play_id,
            "rank": index,
            "pile_id": "disappeared_high_value_customer",
            "campaign_name": "fabricated",
            "concept_cards": [{"card_id": f"concept-{index}-{card}"} for card in range(1, 4)],
        }
        for index, play_id in enumerate(PLAY_IDS, start=1)
    ]
    plays_path.write_bytes(
        (json.dumps(mutated, sort_keys=True, separators=(",", ":")) + "\n").encode()
    )
    html_path = result.output_root / "index.html"
    original_html = html_path.read_bytes()
    html_path.write_text(
        original_html.decode("utf-8")
        + " membership-silence-reopen membership-visit-proof membership-capacity-lane 22000 usd",
        encoding="utf-8",
    )
    launch_path = result.output_root / "launch-pack" / "manifest.json"
    original_launch = launch_path.read_bytes()
    launch = json.loads(original_launch.decode("utf-8"))
    launch["status"] = "approval_ready"
    launch["play_ids"] = list(PLAY_IDS)
    launch["available_play_count"] = 3
    launch_path.write_bytes(
        (json.dumps(launch, sort_keys=True, separators=(",", ":")) + "\n").encode()
    )
    with pytest.raises(ValueError, match="hash mismatch"):
        validate_scenario_output_tree(result.output_root)
    _rehash_manifests(result.output_root)
    with pytest.raises(ValueError, match="play|22000|approval_ready|recovery plays"):
        validate_scenario_output_tree(result.output_root)
    plays_path.write_bytes(original_plays)
    html_path.write_bytes(original_html)
    launch_path.write_bytes(original_launch)
    _rehash_manifests(result.output_root)
    validate_scenario_output_tree(result.output_root)


def test_named_gap_rename_survives_local_rehash(fake_proof, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = tmp_path / "appointments-omitted.json"
    config.write_bytes(load_scenario_source_config_bytes(SYNTHETIC_SERVICE_V1_APPOINTMENTS_OMITTED))
    result = build(output_root="gap-tamper", source_config=config)
    validate_scenario_output_tree(result.output_root)
    events_path = result.output_root / "events" / "public.json"
    original = events_path.read_bytes()
    mutated = json.loads(original.decode("utf-8"))
    mutated["named_data_gaps"] = ["missing_payment"]
    events_path.write_bytes(
        (json.dumps(mutated, sort_keys=True, separators=(",", ":")) + "\n").encode()
    )
    with pytest.raises(ValueError, match="hash mismatch"):
        validate_scenario_output_tree(result.output_root)
    _rehash_manifests(result.output_root)
    with pytest.raises(ValueError, match="named data gaps|public events"):
        validate_scenario_output_tree(result.output_root)
    events_path.write_bytes(original)
    _rehash_manifests(result.output_root)
    validate_scenario_output_tree(result.output_root)


def test_exclusion_reason_tamper_survives_local_rehash(fake_proof, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = _write_packaged_config(tmp_path)
    result = build(output_root="excl-tamper", source_config=config)
    validate_scenario_output_tree(result.output_root)
    events_path = result.output_root / "events" / "public.json"
    original = events_path.read_bytes()
    mutated = json.loads(original.decode("utf-8"))
    mutated["exclusions_by_reason"] = {"later_payment": 1}
    mutated["exclusion_count"] = 1
    events_path.write_bytes(
        (json.dumps(mutated, sort_keys=True, separators=(",", ":")) + "\n").encode()
    )
    with pytest.raises(ValueError, match="hash mismatch"):
        validate_scenario_output_tree(result.output_root)
    _rehash_manifests(result.output_root)
    with pytest.raises(ValueError, match="exclusion|public events"):
        validate_scenario_output_tree(result.output_root)
    events_path.write_bytes(original)
    _rehash_manifests(result.output_root)
    validate_scenario_output_tree(result.output_root)


def test_two_mktemp_cli_runs_are_byte_identical_and_stay_in_caller_root(fake_proof, monkeypatch):
    from found_money.__main__ import main

    trees: list[dict[str, bytes]] = []
    roots: list[Path] = []
    for _ in range(2):
        cwd = Path(tempfile.mkdtemp(prefix="fm035-cli-"))
        assert ROOT not in cwd.parents and cwd != ROOT
        config = _write_packaged_config(cwd)
        monkeypatch.chdir(cwd)
        monkeypatch.setattr(
            sys,
            "argv",
            ["found-money", "build", "--config", str(config), "--output-root", "out"],
        )
        assert main() == 0
        out = cwd / "out"
        roots.append(out)
        trees.append(
            {
                path.relative_to(out).as_posix(): path.read_bytes()
                for path in sorted(out.rglob("*"))
                if path.is_file()
            }
        )
        assert all(ROOT not in path.parents for path in out.rglob("*") if path.is_file())
    assert trees[0] == trees[1]
    with pytest.raises(BuildPathError):
        build(output_root="../escaped", source_config=_write_packaged_config(roots[0].parent))


def test_scenario_build_has_no_network_or_provider_mutations(fake_proof, tmp_path, monkeypatch):
    def blocked(*_args, **_kwargs):
        raise AssertionError("network access is forbidden")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(socket, "socket", blocked)
    build(output_root="out", source_config=_write_packaged_config(tmp_path))


@pytest.mark.release_evidence
def test_ac4_committed_blind_reviewer_packet_binds_locked_service_plays():
    engine = _engine()
    live_plays = engine.strategy_run.recovery_plays
    live_bytes = live_plays.to_canonical_json()
    CompleteRecoveryPlaySetV1.model_validate_json(live_bytes)
    digest = sha256_bytes(live_bytes)
    assert digest == LOCKED_PLAYS_DIGEST
    assert REVIEWER_PACKET.is_file()
    packet_bytes = REVIEWER_PACKET.read_bytes()
    packet = StrategyReviewerPacketV2.model_validate_json(packet_bytes)
    assert packet.to_canonical_json() == packet_bytes
    validate_scenario_reviewer_packet(
        packet,
        live_plays,
        producer_model_family="fixture-strategy-v1",
    )
    assert packet.reviewed_artifact_sha256 == digest
    assert packet.blind is True
    assert packet.producer_context_supplied is False
    assert packet.producer_model_family == "fixture-strategy-v1"
    assert packet.passed is True
    assert packet.actionable_findings == []
    assert packet.rubric_version == 2
    assert packet.disputes == []
    assert len(packet.runs) == 2
    assert len({review.context_id for review in packet.runs}) == 2
    for review in packet.runs:
        assert review.reviewer_runtime == "codex-cli-0.149.1"
        assert review.reviewer_model_family == "gpt-5.6-luna"
        assert len({(s.target_id, s.rubric) for s in review.scores}) == 81
        assert review.reviewer_model_family.casefold() != packet.producer_model_family.casefold()
    blob = live_bytes.decode("utf-8").casefold()
    for token in FORBIDDEN:
        assert token not in blob
    provenance = (FM035_FIXTURE / "PROVENANCE.md").read_text(encoding="utf-8")
    assert "blind-reviewer-packet.json" in provenance
    assert "candidate-reviewer-packet.json" not in provenance
    assert LOCKED_PLAYS_DIGEST in provenance
    assert "isolated" in provenance.casefold()
    assert "fixture-strategy-v1" in provenance
    assert "gpt-5.6-luna" in provenance
    assert "81/81" in provenance
    for review in packet.runs:
        assert review.context_id in provenance
    assert "blind=true" in provenance
    assert "producer_context_supplied=false" in provenance
    assert "run_cb7b13d1e7cb3f1f" in provenance
    assert "not done" not in provenance.casefold()
    assert "substitut" not in provenance.casefold()
    assert "normaliz" not in provenance.casefold()
    assert not (FM035_FIXTURE / "candidate-reviewer-packet.json").exists()


def test_reviewer_packet_validator_rejects_unbound_and_wrong_producer_packets():
    engine = _engine()
    unbound = synthetic_reviewer_packet("0" * 64, producer="codex")
    with pytest.raises(ValueError, match="does not bind"):
        validate_scenario_reviewer_packet(
            unbound,
            engine.strategy_run.recovery_plays,
            producer_model_family="fixture-strategy-v1",
        )
    rebound = unbound.model_copy(
        update={
            "reviewed_artifact_sha256": sha256_bytes(
                engine.strategy_run.recovery_plays.to_canonical_json()
            )
        }
    )
    with pytest.raises(ValueError, match="producer family"):
        validate_scenario_reviewer_packet(
            rebound,
            engine.strategy_run.recovery_plays,
            producer_model_family="fixture-strategy-v1",
        )


def test_reviewer_packet_fail_closed_on_stale_hash_coverage_family_blind_and_findings():
    engine = _engine()
    packet = StrategyReviewerPacketV2.model_validate_json(REVIEWER_PACKET.read_bytes())
    payload = packet.model_dump(mode="json")
    first, second = payload["runs"]
    scores = list(first["scores"])

    with pytest.raises(ValueError, match="does not bind"):
        validate_scenario_reviewer_packet(
            packet.model_copy(update={"reviewed_artifact_sha256": "0" * 64}),
            engine.strategy_run.recovery_plays,
            producer_model_family="fixture-strategy-v1",
        )

    def with_first(**changes):
        return {**payload, "runs": [{**first, **changes}, second]}

    with pytest.raises(ValidationError, match="exactly cover"):
        StrategyReviewerPacketV2.model_validate(with_first(scores=scores[:-1]))
    duplicated = list(scores)
    duplicated[1] = scores[0]
    with pytest.raises(ValidationError, match="scores an item twice"):
        StrategyReviewerPacketV2.model_validate(with_first(scores=duplicated))
    extra = [
        *scores,
        {
            "target_id": "card:9-9",
            "rubric": "card_name",
            "passed": True,
            "finding": None,
            "evidence": None,
        },
    ]
    with pytest.raises(ValidationError, match="exactly cover"):
        StrategyReviewerPacketV2.model_validate(with_first(scores=extra))
    with pytest.raises(ValidationError, match="differ from producer"):
        StrategyReviewerPacketV2.model_validate(
            with_first(reviewer_model_family=payload["producer_model_family"])
        )

    with pytest.raises(ValidationError, match="two consecutive runs"):
        StrategyReviewerPacketV2.model_validate({**payload, "runs": [first]})
    with pytest.raises(ValidationError, match="independent contexts"):
        StrategyReviewerPacketV2.model_validate({**payload, "runs": [first, first]})
    with pytest.raises(ValidationError):
        StrategyReviewerPacketV2.model_validate({**payload, "blind": False})
    with pytest.raises(ValidationError):
        StrategyReviewerPacketV2.model_validate({**payload, "producer_context_supplied": True})
    with pytest.raises(ValidationError):
        StrategyReviewerPacketV2.model_validate({**payload, "rubric_version": 1})

    failed = {
        **scores[0],
        "passed": False,
        "finding": "collapse",
        "evidence": "quoted artifact text",
    }
    with pytest.raises(ValidationError, match="derived from scores"):
        StrategyReviewerPacketV2.model_validate(
            {**with_first(scores=[failed, *scores[1:]]), "passed": True}
        )
    with pytest.raises(ValidationError, match="actionable findings"):
        StrategyReviewerPacketV2.model_validate(
            {
                **with_first(scores=[failed, *scores[1:]]),
                "passed": False,
                "actionable_findings": [],
            }
        )
    # A failure that cites nothing is not a finding, so it cannot be recorded.
    with pytest.raises(ValidationError, match="cited artifact text"):
        StrategyReviewerPacketV2.model_validate(
            with_first(scores=[{**failed, "evidence": None}, *scores[1:]])
        )


def test_real_chromium_mechanical_proof_no_console_clip_responsive_a11y_us_letter(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    result = build(output_root="proof-out", source_config=_write_packaged_config(tmp_path))
    validate_scenario_output_tree(result.output_root)
    pdf = (result.output_root / "print-report.pdf").read_bytes()
    text = assert_us_letter_pdf(pdf)
    # Copy states major units; the ledger keeps minor units. See FM-054.
    assert "220.00" in text
    assert "rebooking" in text
    assert "4900" not in text
    proof = result.output_root / "render-proof"
    for name, expected in NAMED_VIEWPORT_DIMENSIONS.items():
        payload = (proof / name).read_bytes()
        assert payload.startswith(PNG_SIGNATURE)
        assert png_dimensions(payload) == expected
    page_pngs = sorted(proof.glob("print-report-page-*.png"))
    assert [path.name for path in page_pngs] == list(canonical_print_report_page_artifacts())
    for path in page_pngs:
        assert png_dimensions(path.read_bytes()) == PRINT_PAGE_VIEWPORT
    assert pdf.startswith(PDF_SIGNATURE)
    assert (proof / "print-report.pdf").read_bytes() == pdf
    print_review = json.loads((proof / "print-report-manifest.json").read_text(encoding="utf-8"))
    assert print_review["page_count"] == CANONICAL_PRINT_REPORT_PAGE_COUNT
    assert print_review["human_review"]["status"] == "pending"
    assert scan_output_tree(result.output_root, include_private=True) == []


def test_locked_scenario_print_html_matches_canonical_page_count_contract():
    engine = _engine()
    html = render_print_report_html(engine.enriched_money_map, engine.strategy_run.recovery_plays)
    assert count_print_report_html_pages(html) == CANONICAL_PRINT_REPORT_PAGE_COUNT


def _json_keys(payload: object) -> set[str]:
    keys: set[str] = set()
    if isinstance(payload, dict):
        keys.update(payload)
        for value in payload.values():
            keys.update(_json_keys(value))
    elif isinstance(payload, list):
        for item in payload:
            keys.update(_json_keys(item))
    return keys


def _assert_public_tree_has_no_customer_level_rows(root: Path) -> None:
    for relative in SCENARIO_PUBLIC_PATHS:
        path = root / relative
        if not path.is_file() or path.suffix != ".json":
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        keys = _json_keys(payload)
        leaked = keys.intersection(CUSTOMER_LEVEL_PUBLIC_KEYS)
        assert leaked == set(), f"{relative} leaked {sorted(leaked)}"
        blob = json.dumps(payload)
        for source_id in RAW_SERVICE_SOURCE_IDS:
            assert source_id not in blob
        assert _PROVIDER_SHAPED_ID_RE.search(blob) is None
        assert _RECORD_PSEUDONYM_RE.search(blob) is None


def _rehash_manifests(root: Path) -> None:
    files = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
        and path.relative_to(root).as_posix() not in {"run.json", "scenario/manifest.json"}
    }
    scenario = json.loads((root / "scenario" / "manifest.json").read_text(encoding="utf-8"))
    scenario["artifacts"] = [
        {"path": path, "sha256": sha256_bytes(data)} for path, data in sorted(files.items())
    ]
    scenario["definition_sha256"] = sha256_bytes(files["scenario/definition.json"])
    scenario["aggregate_receipt_sha256"] = sha256_bytes(files["scenario/aggregate-receipt.json"])
    manifest_bytes = (json.dumps(scenario, sort_keys=True, separators=(",", ":")) + "\n").encode()
    (root / "scenario" / "manifest.json").write_bytes(manifest_bytes)
    run = json.loads((root / "run.json").read_text(encoding="utf-8"))
    hashed = dict(files)
    hashed["scenario/manifest.json"] = manifest_bytes
    run["artifacts"] = [
        {"path": path, "sha256": sha256_bytes(data)} for path, data in sorted(hashed.items())
    ]
    (root / "run.json").write_bytes(
        (json.dumps(run, sort_keys=True, separators=(",", ":")) + "\n").encode()
    )


@pytest.fixture
def fake_proof(monkeypatch):
    import found_money.build as build_module
    from PIL import Image
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    from found_money.rendering.proof import (
        FOUR_ROOM_PNGS,
        PRINT_REPORT_CONTACT_SHEET,
        build_print_contact_sheet,
        rasterize_print_report_page_pngs,
        resolve_render_baselines_dir,
    )

    def _png(width: int, height: int, color: tuple[int, int, int]) -> bytes:
        image = Image.new("RGB", (width, height), color)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()

    def _minimal_pdf() -> bytes:
        writer = PdfWriter()
        body = (
            "Money Map Recovery Plays Human control "
            "membership-silence-reopen membership-visit-proof membership-capacity-lane "
            "$220.00 $125.00 rebooking missing_appointments missing_proposals missing_crm missing_payment"
        )
        for index in range(1, CANONICAL_PRINT_REPORT_PAGE_COUNT + 1):
            page = writer.add_blank_page(width=612, height=792)
            font = DictionaryObject(
                {
                    NameObject("/Type"): NameObject("/Font"),
                    NameObject("/Subtype"): NameObject("/Type1"),
                    NameObject("/BaseFont"): NameObject("/Helvetica"),
                }
            )
            resources = DictionaryObject()
            resources[NameObject("/Font")] = DictionaryObject(
                {NameObject("/F1"): writer._add_object(font)}
            )
            page[NameObject("/Resources")] = resources
            stream = DecodedStreamObject()
            stream.set_data(
                f"BT /F1 18 Tf 72 720 Td (page {index:02d}) Tj 0 -24 Td ({body}) Tj ET".encode()
            )
            page[NameObject("/Contents")] = writer._add_object(stream)
        buffer = io.BytesIO()
        writer.write(buffer)
        return buffer.getvalue()

    def _fake_capture(*_args, **_kwargs):
        money_desktop = _png(1440, 900, (12, 34, 56))
        top_desktop = _png(1440, 900, (70, 80, 90))
        print_pdf = _minimal_pdf()
        page_pngs = rasterize_print_report_page_pngs(print_pdf)
        artifacts = {
            "index.png": money_desktop,
            "index.pdf": print_pdf,
            "top-play.png": top_desktop,
            "top-play.pdf": print_pdf,
            "money-map-desktop-1440x900.png": money_desktop,
            "money-map-mobile-390x844.png": _png(390, 844, (22, 44, 66)),
            "top-play-desktop-1440x900.png": top_desktop,
            "top-play-mobile-390x844.png": _png(390, 844, (32, 54, 76)),
            "print-report.pdf": print_pdf,
            PRINT_REPORT_CONTACT_SHEET: build_print_contact_sheet(page_pngs),
        }
        artifacts.update(
            {
                f"print-report-page-{index:02d}.png": payload
                for index, payload in enumerate(page_pngs, start=1)
            }
        )
        return artifacts

    def _fake_print_review():
        artifacts = _fake_capture()
        pages = sorted(name for name in artifacts if name.startswith("print-report-page-"))
        return {
            "platform": resolve_render_baselines_dir().name,
            "run_id": "run_f7e27bfd5aeb154d",
            "reviewer": "test reviewer",
            "reviewed_implementation_head": "0" * 40,
            "pdf_sha256": build_module.sha256_bytes(artifacts["print-report.pdf"]),
            "contact_sheet_sha256": build_module.sha256_bytes(
                artifacts[PRINT_REPORT_CONTACT_SHEET]
            ),
            "page_reviews": [
                {
                    "page": index,
                    "artifact": name,
                    "sha256": build_module.sha256_bytes(artifacts[name]),
                    "status": "pass",
                }
                for index, name in enumerate(pages, start=1)
            ],
        }

    monkeypatch.setattr(build_module, "capture_recovery_room_artifacts", _fake_capture)
    monkeypatch.setattr(build_module, "validate_print_review_packet", _fake_print_review)
    monkeypatch.setattr(
        build_module,
        "capture_four_room_screenshots",
        lambda *_args, **_kwargs: {
            name: _png(1440, 900, (100 + index * 10, 20, 30))
            for index, name in enumerate(FOUR_ROOM_PNGS)
        },
    )
