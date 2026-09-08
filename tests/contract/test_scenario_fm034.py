"""FM-034 ecommerce scenario on the shared FM-033 harness."""

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
    SYNTHETIC_ECOMMERCE_V1_FIXTURE,
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
from found_money.contracts.cart import parse_optional_cart
from found_money.scenarios import (
    RAW_ECOMMERCE_SOURCE_IDS,
    SCENARIO_PUBLIC_PATHS,
    SYNTHETIC_ECOMMERCE_V1,
    SYNTHETIC_ECOMMERCE_V1_CART_OMITTED,
    SYNTHETIC_SAAS_V1,
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
REVIEWER_PACKET = (
    ROOT / "tests" / "fixtures" / "ecommerce" / "strategy" / "fm034" / "blind-reviewer-packet.json"
)
OMITTED_CONFIG = ROOT / "configs" / "synthetic-ecommerce-v1-cart-omitted.json"
REQUIRED_FAMILIES = {
    "lapsed_repeat_buyer",
    "overdue_reorder",
    "disappeared_high_value_customer",
}
FORBIDDEN_SAAS = (
    "payment_rescue",
    "payment rescue",
    "4900",
    "card-retry",
    "card retry",
    "billing portal",
    "billing-portal",
    "annual subscription",
)
CUSTOMER_LEVEL_SITUATION_KEYS = {
    "account_token",
    "amount_minor",
    "cart_id",
    "charge_id",
    "customer_external_id",
    "customer_id",
    "customer_token",
    "join_rule",
    "member_node_ids",
    "member_rows",
    "members",
    "order_id",
    "refund_id",
    "source_id",
}
CUSTOMER_LEVEL_PUBLIC_KEYS = CUSTOMER_LEVEL_SITUATION_KEYS - {"amount_minor", "join_rule"}
_PROVIDER_SHAPED_ID_RE = re.compile(
    r"\b(?:cus_|ch_|re_|cart_|ord_|hs_(?:contact|ct|dl|deal)_)[A-Za-z0-9_]+"
)
_RECORD_PSEUDONYM_RE = re.compile(r"\b(?:cust_|rec_)[0-9a-f]{8,}\b")


def _write_packaged_config(directory: Path) -> Path:
    path = directory / "synthetic-ecommerce-v1.json"
    path.write_bytes(load_scenario_source_config_bytes(SYNTHETIC_ECOMMERCE_V1))
    return path


def _public_safe_config() -> dict:
    return _safe_source_config(
        source_mode="fixture",
        run_mode="public",
        fixture=SYNTHETIC_ECOMMERCE_V1_FIXTURE,
    )


def _locked_run_id() -> str:
    return _run_id_for(_fixture_snapshots(SYNTHETIC_ECOMMERCE_V1_FIXTURE), _public_safe_config())


def _omitted_safe_config() -> dict:
    return _safe_source_config(
        source_mode="fixture",
        run_mode="public",
        fixture=SYNTHETIC_ECOMMERCE_V1_CART_OMITTED,
    )


def _omitted_run_id() -> str:
    return _run_id_for(
        _fixture_snapshots(SYNTHETIC_ECOMMERCE_V1_CART_OMITTED), _omitted_safe_config()
    )


def _engine(
    run_id: str | None = None, *, definition=None, fixture_id: str = SYNTHETIC_ECOMMERCE_V1
):
    safe = _public_safe_config() if fixture_id == SYNTHETIC_ECOMMERCE_V1 else _omitted_safe_config()
    return run_scenario_engine(
        run_id=run_id
        or (_locked_run_id() if fixture_id == SYNTHETIC_ECOMMERCE_V1 else _omitted_run_id()),
        safe_config=safe,
        fixture_id=fixture_id,
        definition=definition,
    )


def test_ecommerce_fixture_uses_orders_and_billing_transport_not_saas_bytes():
    ecommerce = load_scenario_snapshot_bytes(SYNTHETIC_ECOMMERCE_V1)
    saas = load_scenario_snapshot_bytes(SYNTHETIC_SAAS_V1)
    assert set(ecommerce) == {"stripe", "orders"}
    assert ecommerce["stripe"] != saas["stripe"]
    assert "in_saas_fp_001" not in ecommerce["stripe"].decode("utf-8")
    assert "cus_ecom_vip_001" in ecommerce["stripe"].decode("utf-8")
    assert "ord_ecom_vip_1" in ecommerce["orders"].decode("utf-8")
    csv = (
        ROOT / "found_money" / "scenarios" / "fixtures" / "ecommerce-v1" / "inputs" / "orders.csv"
    ).read_bytes()
    json_bytes = (
        ROOT / "found_money" / "scenarios" / "fixtures" / "ecommerce-v1" / "inputs" / "orders.json"
    ).read_bytes()
    assert csv
    assert json_bytes
    from found_money.imports.orders import parse_orders_file_bytes

    assert (
        parse_orders_file_bytes(csv, filename="orders.csv").to_canonical_json()
        == ecommerce["orders"]
    )
    assert (
        parse_orders_file_bytes(json_bytes, filename="orders.json").to_canonical_json()
        == ecommerce["orders"]
    )


def test_definition_is_extra_forbid_and_hash_locked():
    definition = load_scenario_definition(SYNTHETIC_ECOMMERCE_V1)
    raw = (
        ROOT / "found_money" / "scenarios" / "fixtures" / "ecommerce-v1" / "definition.json"
    ).read_bytes()
    assert parse_scenario_definition(raw).to_canonical_json() == raw
    payload = json.loads(raw.decode("utf-8"))
    payload["unexpected"] = True
    with pytest.raises(ValidationError):
        ScenarioDefinitionV1.model_validate(payload)
    from found_money.scenarios.transports import packaged_fixture_hashes

    assert packaged_fixture_hashes(SYNTHETIC_ECOMMERCE_V1) == definition.fixture_hashes
    assert sha256_bytes(load_scenario_source_config_bytes(SYNTHETIC_ECOMMERCE_V1)) == (
        definition.source_config_hash
    )
    assert set(definition.expected_event_families) == REQUIRED_FAMILIES
    assert definition.expected_play_ids == [
        "vip-silence-reopen",
        "vip-catalog-proof",
        "vip-capacity-lane",
    ]
    assert len(definition.expected_card_ids) == 9
    assert definition.high_value_cart_state == "supplied"
    cart = parse_optional_cart(
        (
            ROOT
            / "found_money"
            / "scenarios"
            / "fixtures"
            / "ecommerce-v1"
            / "optional"
            / "high-value-cart.json"
        ).read_bytes()
    )
    assert definition.high_value_cart_hash == cart.sha256()
    omitted = load_scenario_definition(SYNTHETIC_ECOMMERCE_V1_CART_OMITTED)
    assert omitted.high_value_cart_state == "omitted"
    assert omitted.high_value_cart_hash != definition.high_value_cart_hash
    assert definition.inherited_proof_contract.claims_new_visual_baseline is False
    assert definition.inherited_proof_contract.claims_print_ratification is False


def test_orders_billing_identity_uses_declared_external_id_not_raw_equal_ids():
    snapshots = load_scenario_snapshots(SYNTHETIC_ECOMMERCE_V1)
    stripe_ids = {item["id"] for item in snapshots["stripe"]["customers"]}
    order_ids = {item["customer_id"] for item in snapshots["orders"]["orders"]}
    assert stripe_ids.isdisjoint(order_ids)
    graph = build_identity_graph(
        normalize_source_records(
            snapshots, default_observed_at=load_scenario_definition(SYNTHETIC_ECOMMERCE_V1).clock
        ),
        run_id="run_identity_proof",
        built_at=load_scenario_definition(SYNTHETIC_ECOMMERCE_V1).clock,
    )
    assert len(graph.customers) == 5
    assert graph.ambiguous_identities == []
    for cluster in graph.customers:
        systems = {node_id.split(":", 1)[0] for node_id in cluster.member_node_ids}
        assert "stripe" in systems and "orders" in systems
    assert any(
        edge.match_rule == "declared_external_id" and edge.match_namespace == "orders.customer_id"
        for edge in graph.edges
    )


def test_detector_emits_lapsed_overdue_dormant_without_aliasing_refund_or_cart():
    snapshots = load_scenario_snapshots(SYNTHETIC_ECOMMERCE_V1)
    clock = load_scenario_definition(SYNTHETIC_ECOMMERCE_V1).clock
    graph = build_identity_graph(
        normalize_source_records(snapshots, default_observed_at=clock),
        run_id="run_detector_proof",
        built_at=clock,
    )
    detection = detect_event_families(snapshots, graph, run_id="run_detector_proof", built_at=clock)
    families = {item.event_family for item in detection.candidates.candidates}
    assert REQUIRED_FAMILIES <= families
    blob = detection.candidates.to_canonical_json().decode("utf-8")
    assert "refunded_buyer" not in blob
    assert "high_value_cart" not in blob
    engine = _engine()
    assert "refunded_buyer_next_move" in engine.typed_situations
    assert "high_value_cart" in engine.typed_situations
    assert engine.situation_set is not None
    present = {row.situation_id: row for row in engine.situation_set.situations}
    assert present["refunded_buyer_next_move"].status == "present"
    assert present["high_value_cart"].status == "present"
    assert present["refunded_buyer_next_move"].qualifying_count == 1
    assert present["high_value_cart"].qualifying_count == 1
    assert present["high_value_cart"].totals_minor_by_currency["usd"] is not None
    blob_public = engine.situation_set.to_canonical_json().decode("utf-8")
    for source_id in RAW_ECOMMERCE_SOURCE_IDS:
        assert source_id not in blob_public
    _assert_aggregate_only_situation_payload(json.loads(blob_public))
    selected = {(row.pile_id, row.customer_token) for row in engine.ledger.contributions}
    piles_by_customer: dict[str, set[str]] = {}
    for pile_id, token in selected:
        if pile_id in REQUIRED_FAMILIES:
            piles_by_customer.setdefault(token, set()).add(pile_id)
    assert all(len(items) == 1 for items in piles_by_customer.values())


def test_mixed_currency_stays_unquantified_and_overlap_selects_one_unit():
    engine = _engine()
    ranked = sorted(engine.enriched_money_map.piles, key=lambda item: item.rank)
    assert ranked[0].pile_id == "disappeared_high_value_customer"
    assert str(ranked[0].selected_value_minor) == "18500"
    overdue = next(item for item in ranked if item.pile_id == "overdue_reorder")
    assert str(overdue.selected_value_minor) == "10600"
    assert all(item.currency != "cad" for item in ranked)
    assert any(gap.reason_code == "ambiguous_value" for gap in engine.ledger.data_gaps)
    assert not any(row.pile_id == "lapsed_repeat_buyer" for row in engine.ledger.contributions)


def test_cart_omitted_emits_named_gap_and_withholds_dependent_output():
    engine = _engine(fixture_id=SYNTHETIC_ECOMMERCE_V1_CART_OMITTED)
    assert "high_value_cart" not in engine.typed_situations
    assert "missing_high_value_cart" in engine.data_gap_details
    assert "high_value_cart_dependent_output" in engine.withheld_asset_ids
    assert engine.event_public.data_gap_count >= 1
    cart_row = next(
        item for item in engine.situation_set.situations if item.situation_id == "high_value_cart"
    )
    refund_row = next(
        item
        for item in engine.situation_set.situations
        if item.situation_id == "refunded_buyer_next_move"
    )
    assert cart_row.status == "omitted"
    assert cart_row.withhold_reason == "missing_high_value_cart"
    assert cart_row.qualifying_count == 0
    assert cart_row.totals_minor_by_currency == {}
    assert refund_row.status == "present"
    assert refund_row.qualifying_count == 1
    _assert_aggregate_only_situation_payload(
        json.loads(engine.situation_set.to_canonical_json().decode("utf-8"))
    )


def test_supplied_and_omitted_run_ids_differ_and_follow_cart_hash():
    supplied = _locked_run_id()
    omitted = _omitted_run_id()
    assert supplied != omitted
    supplied_safe = _public_safe_config()
    mutated = dict(supplied_safe)
    mutated["high_value_cart_hash"] = "0" * 64
    mutated_id = _run_id_for(_fixture_snapshots(SYNTHETIC_ECOMMERCE_V1_FIXTURE), mutated)
    assert mutated_id != supplied
    assert mutated_id != omitted


def test_malformed_cart_fails_closed_and_cannot_share_run_id():
    with pytest.raises(ValueError):
        parse_optional_cart(
            b'{"carts":[{}],"schema_version":"ecommerce-optional-cart.v1","state":"supplied"}\n'
        )
    with pytest.raises(ValueError):
        parse_optional_cart(
            b'{"carts":[true],"schema_version":"ecommerce-optional-cart.v1","state":"supplied"}\n'
        )


def test_wrong_customer_completed_recent_and_below_threshold_withhold():
    from found_money.contracts.cart import HighValueCartRecordV1, cart_qualification_reason
    from found_money.identity import build_identity_graph, normalize_source_records
    from found_money.scenarios.situations import build_high_value_cart_evidence

    snapshots = load_scenario_snapshots(SYNTHETIC_ECOMMERCE_V1)
    clock = load_scenario_definition(SYNTHETIC_ECOMMERCE_V1).clock
    graph = build_identity_graph(
        normalize_source_records(snapshots, default_observed_at=clock),
        run_id="run_cart_qualify",
        built_at=clock,
    )
    good = parse_optional_cart(
        (
            ROOT
            / "found_money"
            / "scenarios"
            / "fixtures"
            / "ecommerce-v1"
            / "optional"
            / "high-value-cart.json"
        ).read_bytes()
    )
    present = build_high_value_cart_evidence(good, graph, clock=clock)
    assert present.status == "present"
    wrong = good.model_copy(
        update={
            "carts": [
                good.carts[0].model_copy(update={"customer_external_id": "ord_cust_missing_999"})
            ]
        }
    )
    assert build_high_value_cart_evidence(wrong, graph, clock=clock).status == "withheld"
    completed = good.model_copy(
        update={"carts": [good.carts[0].model_copy(update={"state": "completed"})]}
    )
    assert build_high_value_cart_evidence(completed, graph, clock=clock).withhold_reason == (
        "completed_high_value_cart"
    )
    recent = good.model_copy(
        update={"carts": [good.carts[0].model_copy(update={"observed_at": clock})]}
    )
    assert build_high_value_cart_evidence(recent, graph, clock=clock).withhold_reason == (
        "recent_high_value_cart"
    )
    cheap = good.model_copy(
        update={"carts": [good.carts[0].model_copy(update={"total_minor": "9999"})]}
    )
    assert build_high_value_cart_evidence(cheap, graph, clock=clock).withhold_reason == (
        "below_threshold_high_value_cart"
    )
    assert (
        cart_qualification_reason(
            HighValueCartRecordV1.model_validate(good.carts[0].canonical_dict()),
            clock=clock,
            joined=True,
        )
        is None
    )


def test_engine_ranks_vip_first_and_plays_are_not_copied_saas():
    engine = _engine()
    ranked = sorted(engine.enriched_money_map.piles, key=lambda item: item.rank)
    assert ranked[0].navigation is not None
    assert ranked[0].navigation.play_id == engine.enriched_money_map.recommended_play_ids[0]
    for pile in ranked[1:]:
        assert pile.navigation is None or pile.navigation.play_id is None
    blob = engine.strategy_run.recovery_plays.to_canonical_json().decode("utf-8").casefold()
    for token in FORBIDDEN_SAAS:
        assert token not in blob
    for family in REQUIRED_FAMILIES - {"disappeared_high_value_customer"}:
        assert family not in blob
        assert family.replace("_", " ") not in blob


def test_complete_play_helper_records_all_ids_and_rejects_unknown_duplicate_order():
    engine = _engine("run_complete_helper")
    enriched = strategy_helper(engine.money_map, engine.strategy_run.recovery_plays)
    assert (
        enriched.recommended_play_ids
        == load_scenario_definition(SYNTHETIC_ECOMMERCE_V1).expected_play_ids
    )
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


def test_saas_fixture_remains_byte_stable_beside_ecommerce():
    saas = load_scenario_snapshot_bytes(SYNTHETIC_SAAS_V1)
    ecommerce = load_scenario_snapshot_bytes(SYNTHETIC_ECOMMERCE_V1)
    assert saas["stripe"] != ecommerce["stripe"]
    saas_def = load_scenario_definition(SYNTHETIC_SAAS_V1)
    assert saas_def.expected_play_ids[0].startswith("payment-rescue")


def test_scenario_build_tree_manifest_public_scan_and_tamper(fake_proof, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = _write_packaged_config(tmp_path)
    result = build(output_root="ecom-out", source_config=config)
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
    with pytest.raises(ValueError, match="18500"):
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
    for source_id in RAW_ECOMMERCE_SOURCE_IDS:
        assert source_id not in audit
    for token in FORBIDDEN_SAAS:
        assert token not in audit.casefold()
    assert "api.stripe.com" not in audit
    situations = json.loads((result.output_root / "scenario" / "situations.json").read_text())
    assert situations["high_value_cart_state"] == "supplied"
    assert "cart_ecom_hv_001" not in json.dumps(situations)
    by_id = {item["situation_id"]: item for item in situations["situations"]}
    assert set(by_id) == {"high_value_cart", "refunded_buyer_next_move"}
    assert by_id["high_value_cart"]["status"] == "present"
    assert by_id["refunded_buyer_next_move"]["status"] == "present"
    assert by_id["high_value_cart"]["qualifying_count"] == 1
    assert by_id["refunded_buyer_next_move"]["qualifying_count"] == 1
    _assert_public_tree_has_no_customer_level_rows(result.output_root)


def test_omitted_documented_config_build_withholds_cart(fake_proof, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = tmp_path / "synthetic-ecommerce-v1-cart-omitted.json"
    config.write_bytes(load_scenario_source_config_bytes(SYNTHETIC_ECOMMERCE_V1_CART_OMITTED))
    result = build(output_root="ecom-omitted", source_config=config)
    validate_scenario_output_tree(result.output_root)
    situations = json.loads((result.output_root / "scenario" / "situations.json").read_text())
    cart_row = next(
        item for item in situations["situations"] if item["situation_id"] == "high_value_cart"
    )
    refund_row = next(
        item
        for item in situations["situations"]
        if item["situation_id"] == "refunded_buyer_next_move"
    )
    assert cart_row["status"] == "omitted"
    assert cart_row["qualifying_count"] == 0
    assert cart_row["totals_minor_by_currency"] == {}
    assert refund_row["status"] == "present"
    assert refund_row["qualifying_count"] == 1
    _assert_aggregate_only_situation_payload(situations)
    _assert_public_tree_has_no_customer_level_rows(result.output_root)
    withheld = json.loads((result.output_root / "launch-pack" / "withheld-assets.json").read_text())
    blob = json.dumps(withheld)
    assert "high_value_cart_dependent_output" in blob
    assert result.run_id != _locked_run_id()
    assert OMITTED_CONFIG.is_file()


@pytest.mark.parametrize(
    "field",
    ("qualifying_count", "totals_minor_by_currency", "status", "evidence_digest"),
)
def test_situation_aggregate_tamper_survives_outer_rehash(fake_proof, tmp_path, monkeypatch, field):
    monkeypatch.chdir(tmp_path)
    config = _write_packaged_config(tmp_path)
    result = build(output_root="ecom-out", source_config=config)
    target = result.output_root / "scenario" / "situations.json"
    original = target.read_bytes()
    mutated = json.loads(original.decode("utf-8"))
    row = next(item for item in mutated["situations"] if item["status"] == "present")
    if field == "qualifying_count":
        row["qualifying_count"] = row["qualifying_count"] + 1
    elif field == "totals_minor_by_currency":
        currency = next(iter(row["totals_minor_by_currency"]))
        row["totals_minor_by_currency"][currency] = "1"
    elif field == "status":
        row["status"] = "withheld"
        row["withhold_reason"] = "missing_refunded_buyer_next_move"
    else:
        row["evidence_digest"] = "0" * 64
    target.write_bytes((json.dumps(mutated, sort_keys=True, separators=(",", ":")) + "\n").encode())
    with pytest.raises(ValueError, match="hash mismatch"):
        validate_scenario_output_tree(result.output_root)
    _rehash_manifests(result.output_root)
    with pytest.raises(ValueError, match="situation evidence"):
        validate_scenario_output_tree(result.output_root)
    target.write_bytes(original)
    _rehash_manifests(result.output_root)
    validate_scenario_output_tree(result.output_root)


def test_public_situations_are_aggregate_only_without_customer_rows(
    fake_proof, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    config = _write_packaged_config(tmp_path)
    result = build(output_root="ecom-out", source_config=config)
    situations_path = result.output_root / "scenario" / "situations.json"
    payload = json.loads(situations_path.read_text(encoding="utf-8"))
    _assert_aggregate_only_situation_payload(payload)
    _assert_public_tree_has_no_customer_level_rows(result.output_root)
    blob = situations_path.read_text(encoding="utf-8")
    assert "account_token" not in blob
    assert "customer_token" not in blob
    assert _PROVIDER_SHAPED_ID_RE.search(blob) is None
    assert _RECORD_PSEUDONYM_RE.search(blob) is None


def test_two_mktemp_cli_runs_are_byte_identical_and_stay_in_caller_root(fake_proof, monkeypatch):
    from found_money.__main__ import main

    trees: list[dict[str, bytes]] = []
    roots: list[Path] = []
    for _ in range(2):
        cwd = Path(tempfile.mkdtemp(prefix="fm034-cli-"))
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


def test_reviewer_packet_validator_rejects_unbound_and_wrong_producer_packets():
    engine = _engine()
    unbound = synthetic_reviewer_packet("0" * 64, producer="codex")
    with pytest.raises(ValueError, match="does not bind"):
        validate_scenario_reviewer_packet(
            unbound,
            engine.strategy_run.recovery_plays,
            producer_model_family="fixture-strategy-v1",
        )


@pytest.mark.release_evidence
def test_ac4_committed_blind_reviewer_packet_binds_locked_ecommerce_plays():
    engine = _engine()
    live_plays = engine.strategy_run.recovery_plays
    live_bytes = live_plays.to_canonical_json()
    CompleteRecoveryPlaySetV1.model_validate_json(live_bytes)
    digest = sha256_bytes(live_bytes)
    # The reviewer is handed a request generated from this build by
    # `scripts/blind_review.py prepare`, so there is no second copy of the plays
    # or of the rubric to drift out of step with it.
    assert digest == "0616beb1cec00f1ff66a1347ae70620f27502250e250defd918cb70799e5c036"
    packet = StrategyReviewerPacketV2.model_validate_json(REVIEWER_PACKET.read_bytes())
    validate_scenario_reviewer_packet(
        packet,
        live_plays,
        producer_model_family="fixture-strategy-v1",
    )
    assert packet.reviewed_artifact_sha256 == digest
    assert packet.blind is True
    assert packet.producer_context_supplied is False
    assert packet.rubric_version == 2
    assert len(packet.runs) == 2
    assert len({review.context_id for review in packet.runs}) == 2
    for review in packet.runs:
        assert len({(s.target_id, s.rubric) for s in review.scores}) == 81
        assert review.reviewer_model_family.casefold() != packet.producer_model_family.casefold()
    assert packet.passed is True
    blob = live_bytes.decode("utf-8").casefold()
    for token in FORBIDDEN_SAAS:
        assert token not in blob
    provenance = (REVIEWER_PACKET.parent / "PROVENANCE.md").read_text(encoding="utf-8")
    assert "blind-reviewer-packet.json" in provenance
    assert "candidate-reviewer-packet.json" not in provenance
    assert "0616beb1cec00f1ff66a1347ae70620f27502250e250defd918cb70799e5c036" in provenance
    assert "81/81" in provenance
    for review in packet.runs:
        assert review.context_id in provenance
    assert "isolated" in provenance.casefold()
    assert "substitut" not in provenance.casefold()
    assert "normaliz" not in provenance.casefold()
    assert not (REVIEWER_PACKET.parent / "candidate-reviewer-packet.json").exists()


def test_real_chromium_mechanical_proof_no_console_clip_responsive_a11y_us_letter(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    result = build(output_root="proof-out", source_config=_write_packaged_config(tmp_path))
    validate_scenario_output_tree(result.output_root)
    pdf = (result.output_root / "print-report.pdf").read_bytes()
    text = assert_us_letter_pdf(pdf)
    # Copy states major units; the ledger keeps minor units. See FM-054.
    assert "185.00" in text
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


def _assert_aggregate_only_situation_payload(payload: dict) -> None:
    keys = _json_keys(payload)
    leaked = keys.intersection(CUSTOMER_LEVEL_SITUATION_KEYS)
    assert leaked == set()
    blob = json.dumps(payload)
    for source_id in RAW_ECOMMERCE_SOURCE_IDS:
        assert source_id not in blob
    assert _PROVIDER_SHAPED_ID_RE.search(blob) is None
    assert _RECORD_PSEUDONYM_RE.search(blob) is None
    for row in payload["situations"]:
        assert "qualifying_count" in row
        assert "totals_minor_by_currency" in row
        assert "evidence_digest" in row
        assert "status" in row
        assert "situation_id" in row


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
        for source_id in RAW_ECOMMERCE_SOURCE_IDS:
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
            "vip-silence-reopen vip-catalog-proof vip-capacity-lane $185.00"
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
