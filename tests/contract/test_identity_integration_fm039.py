"""FM-039 cross-source identity/lineage integration contract tests."""

from __future__ import annotations

import json
import socket
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from found_money.build import build, validate_public_artifact_payloads
from found_money.contracts.identity import IdentityEdgeV1
from found_money.contracts.identity_stage import (
    ALLOWED_MATCH_RULES,
    IdentityStageEvidenceV1,
    parse_identity_stage_evidence,
)
from found_money.contracts.run import compute_source_set_hash
from found_money.contracts.source_stage import SourceSetEntryV1, SourceSetManifestV1
from found_money.identity import (
    apply_identity_override,
    build_identity_graph,
    canonical_identity_payload_bytes,
    customer_token_for,
    load_fm039_matrix_snapshots,
    make_identity_override,
    normalize_source_records,
    normalize_source_set,
    public_identity_projection,
    sha256_bytes,
    verify_source_set_payloads,
)
from found_money.identity.stage import (
    IDENTITY_GRAPH_PATH,
    IDENTITY_PUBLIC_PATH,
    IDENTITY_STAGE_PATH,
    IdentityStageError,
    assert_identity_count_consistency,
    run_identity_stage,
    validate_identity_stage_bundle,
)
from found_money.safety.output_scan import scan_text_artifact

ROOT = Path(__file__).resolve().parents[2]
FM039 = ROOT / "tests" / "fixtures" / "saas" / "identity" / "fm039"
MATRIX = FM039 / "matrix"
ADAPTER = FM039 / "adapter"
EVIDENCE = FM039 / "evidence"
CONFIG = ROOT / "configs" / "synthetic-saas-thin-slice.json"
WHEN = datetime(2026, 7, 29, 18, 0, 5, tzinfo=timezone.utc)
RUN_ID = "run_fm039_matrix"
ALLOWED_RULES = set(ALLOWED_MATCH_RULES)


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _adapter_payloads() -> dict[str, dict]:
    return {
        "hubspot": _json(ADAPTER / "hubspot.json"),
        "stripe": _json(ADAPTER / "stripe.json"),
        "orders": _json(ADAPTER / "orders.json"),
        "appointments": _json(ADAPTER / "appointments.json"),
        "proposals": _json(ADAPTER / "proposals.json"),
        "universal-csv": _json(ADAPTER / "universal-orders.json"),
        "universal-json": _json(ADAPTER / "universal.json"),
    }


def _adapter_types() -> dict[str, str]:
    return {
        "hubspot": "hubspot",
        "stripe": "stripe",
        "orders": "orders",
        "appointments": "appointments",
        "proposals": "proposals",
        "universal-csv": "orders",
        "universal-json": "appointments",
    }


def _adapter_schemas() -> dict[str, str]:
    return {
        "hubspot": "hubspot-crm.2026-03.v1",
        "stripe": "stripe.2026-02-25.clover.v1",
        "orders": "orders.v1",
        "appointments": "appointments.v1",
        "proposals": "proposals.v1",
        "universal-csv": "orders.v1",
        "universal-json": "appointments.v1",
    }


def _source_set_for(
    payloads: dict[str, dict],
    types: dict[str, str],
    schemas: dict[str, str],
) -> SourceSetManifestV1:
    entries: list[SourceSetEntryV1] = []
    receipt_hashes: dict[str, str] = {}
    for source_id, payload in payloads.items():
        encoded = (
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        receipt_path = f"receipts/{source_id}.source-receipt.json"
        content_hash = sha256_bytes(encoded)
        receipt_hashes[receipt_path] = content_hash
        entries.append(
            SourceSetEntryV1(
                source_id=source_id,
                source_type=types[source_id],
                connector_schema_version=schemas[source_id],
                normalized_path=f"normalized/{source_id}/{schemas[source_id]}.json",
                receipt_path=receipt_path,
                content_hash=content_hash,
                page_or_row_count=1,
                record_count=1,
            )
        )
    return SourceSetManifestV1(
        source_set_hash=compute_source_set_hash(receipt_hashes),
        sources=entries,
    )


def _matrix_graph():
    nodes = normalize_source_records(load_fm039_matrix_snapshots())
    graph = build_identity_graph(nodes, run_id=RUN_ID, built_at=WHEN)
    return graph, public_identity_projection(graph)


def _matrix_source_set_and_payloads():
    payloads = load_fm039_matrix_snapshots()
    types = {"hubspot": "hubspot", "stripe": "stripe"}
    schemas = {
        "hubspot": "hubspot-crm.2026-03.v1",
        "stripe": "stripe.2026-02-25.clover.v1",
    }
    return _source_set_for(payloads, types, schemas), payloads


def test_ac1_supported_sources_become_typed_nodes_without_invented_keys():
    payloads = _adapter_payloads()
    snapshot_nodes = normalize_source_records(
        {
            **{
                key: payloads[key]
                for key in ("hubspot", "stripe", "orders", "appointments", "proposals")
            },
            "csv": payloads["universal-csv"],
            "json": payloads["universal-json"],
        }
    )
    by_system = {}
    for node in snapshot_nodes:
        by_system.setdefault(node.source_system, []).append(node)
    assert set(by_system) == {
        "hubspot",
        "stripe",
        "orders",
        "appointments",
        "proposals",
        "csv",
        "json",
    }
    for node in snapshot_nodes:
        assert node.source_system
        assert node.object_type
        assert node.source_id
        assert node.payload_hash
        assert node.observed_at.tzinfo is not None
        if node.source_system in {"orders", "appointments", "proposals", "csv", "json"}:
            assert node.email is None
            assert node.phone is None
            assert node.external_ids
    hubspot_contact = next(node for node in snapshot_nodes if node.source_id == "hs_adapter_001")
    stripe_customer = next(node for node in snapshot_nodes if node.source_id == "st_adapter_001")
    assert hubspot_contact.email is None
    assert hubspot_contact.phone is None
    assert stripe_customer.email is None
    assert stripe_customer.phone is None

    source_set = _source_set_for(payloads, _adapter_types(), _adapter_schemas())
    set_nodes = normalize_source_set(source_set, payloads)
    assert {node.source_system for node in set_nodes} == set(payloads)
    csv_nodes = [node for node in set_nodes if node.source_system == "universal-csv"]
    json_nodes = [node for node in set_nodes if node.source_system == "universal-json"]
    assert csv_nodes and all(node.email is None and node.phone is None for node in csv_nodes)
    assert json_nodes and all(node.email is None and node.phone is None for node in json_nodes)
    assert all(node.payload_hash for node in set_nodes)


@pytest.mark.parametrize(
    ("source_key", "schema", "row_key", "base_row", "mutations"),
    [
        (
            "orders",
            "orders.v1",
            "orders",
            {
                "order_id": "o1",
                "customer_id": "c1",
                "ordered_at": "2026-07-29T18:00:00.000Z",
                "currency": "USD",
                "total_minor": 1000,
            },
            (
                ("total_minor", 9999),
                ("currency", "EUR"),
                ("order_id", "o2"),
            ),
        ),
        (
            "appointments",
            "appointments.v1",
            "appointments",
            {
                "appointment_id": "a1",
                "customer_id": "c1",
                "scheduled_at": "2026-07-29T18:00:00.000Z",
                "status": "scheduled",
            },
            (("status", "no_show"),),
        ),
        (
            "proposals",
            "proposals.v1",
            "proposals",
            {
                "proposal_id": "p1",
                "customer_id": "c1",
                "proposed_at": "2026-07-29T18:00:00.000Z",
                "status": "sent",
                "amount_minor": 5000,
                "currency": "USD",
            },
            (
                ("amount_minor", 1),
                ("status", "accepted"),
            ),
        ),
    ],
)
def test_ac1_file_source_payload_hash_binds_complete_normalized_row(
    source_key, schema, row_key, base_row, mutations
):
    baseline = {"schema_version": schema, row_key: [dict(base_row)]}
    base_nodes = normalize_source_records({source_key: baseline})
    assert len(base_nodes) == 1
    base_node = base_nodes[0]
    expected_observed = datetime(2026, 7, 29, 18, 0, 0, tzinfo=timezone.utc)
    assert base_node.source_id == "c1"
    assert base_node.observed_at == expected_observed
    full_row_hash = sha256_bytes(
        json.dumps(base_row, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    )
    assert base_node.payload_hash == full_row_hash

    for field, value in mutations:
        mutated_row = dict(base_row)
        mutated_row[field] = value
        mutated = {"schema_version": schema, row_key: [mutated_row]}
        mutated_node = normalize_source_records({source_key: mutated})[0]
        assert mutated_node.payload_hash != base_node.payload_hash
        assert mutated_node.observed_at == expected_observed
        assert mutated_node.source_id == "c1"
        assert mutated_node.payload_hash == sha256_bytes(
            json.dumps(
                mutated_row, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
        )


def test_ac1_file_source_duplicate_customer_collapse_keeps_first_complete_row():
    payload = {
        "schema_version": "orders.v1",
        "orders": [
            {
                "order_id": "o1",
                "customer_id": "c1",
                "ordered_at": "2026-07-29T18:00:00.000Z",
                "currency": "USD",
                "total_minor": 1000,
            },
            {
                "order_id": "o2",
                "customer_id": "c1",
                "ordered_at": "2026-07-29T19:00:00.000Z",
                "currency": "USD",
                "total_minor": 2000,
            },
        ],
    }
    nodes = normalize_source_records({"orders": payload})
    assert len(nodes) == 1
    assert nodes[0].source_id == "c1"
    assert nodes[0].payload_hash == sha256_bytes(
        json.dumps(
            payload["orders"][0], ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    )
    assert nodes[0].observed_at == datetime(2026, 7, 29, 18, 0, 0, tzinfo=timezone.utc)


def test_ac2_two_source_matrix_emits_exactly_four_rules_and_negatives():
    graph, projection = _matrix_graph()
    rules = {edge.match_rule for edge in graph.edges}
    assert rules == ALLOWED_RULES
    for edge in graph.edges:
        assert edge.match_rule in ALLOWED_RULES
        assert edge.lineage
        assert "left_source" in edge.lineage
        assert "right_source" in edge.lineage
        assert edge.left_node_id != edge.right_node_id

    same_source = [edge for edge in graph.edges if edge.match_rule == "same_source_object_id"]
    external = [edge for edge in graph.edges if edge.match_rule == "declared_external_id"]
    email = [edge for edge in graph.edges if edge.match_rule == "unique_exact_email"]
    phone = [edge for edge in graph.edges if edge.match_rule == "unique_exact_e164_phone"]
    assert len(same_source) == 1
    assert {same_source[0].left_node_id, same_source[0].right_node_id} == {
        "hubspot:contact:hs_shared_001",
        "hubspot:contact:hs_shared_001:2",
    }
    assert any(
        edge.match_namespace == "found_money.synthetic_customer"
        and edge.match_value == "cust_matrix_ext"
        for edge in external
    )
    assert any(edge.match_value == "synth.join@example.invalid" for edge in email)
    assert any(edge.match_value == "+15550102201" for edge in phone)

    name_left = next(node for node in graph.nodes if node.source_id == "hs_name_left")
    name_right = next(node for node in graph.nodes if node.source_id == "st_name_right")
    assert name_left.display_name == name_right.display_name == "Identical Synth Name"
    name_tokens = {
        cluster.customer_token
        for cluster in graph.customers
        if name_left.node_id in cluster.member_node_ids
        or name_right.node_id in cluster.member_node_ids
    }
    assert len(name_tokens) == 2
    assert not any(
        {name_left.node_id, name_right.node_id}.issubset(set(cluster.member_node_ids))
        for cluster in graph.customers
    )

    conflict_ids = {"hubspot:contact:hs_conflict_left", "stripe:customer:st_conflict_right"}
    assert any(
        cluster.reason == "conflicting_stronger_identifier"
        and set(cluster.member_node_ids) == conflict_ids
        for cluster in graph.ambiguous_identities
    )
    assert all(
        not conflict_ids.intersection(cluster.member_node_ids) for cluster in graph.customers
    )
    assert projection.customer_count == len(graph.customers)
    assert {row.customer_token for row in projection.customers} == {
        cluster.customer_token for cluster in graph.customers
    }


def test_ac3_quarantine_stays_out_until_hash_bound_override():
    graph, projection = _matrix_graph()
    household = next(
        item for item in graph.ambiguous_identities if item.reason == "household_email"
    )
    recycled = next(item for item in graph.ambiguous_identities if item.reason == "recycled_phone")
    conflict = next(
        item
        for item in graph.ambiguous_identities
        if item.reason == "conflicting_stronger_identifier"
    )
    quarantined = {
        node_id for cluster in graph.ambiguous_identities for node_id in cluster.member_node_ids
    }
    assert quarantined.isdisjoint(
        {node_id for cluster in graph.customers for node_id in cluster.member_node_ids}
    )
    assert household.member_node_ids == [
        "hubspot:contact:hs_household_a",
        "hubspot:contact:hs_household_b",
        "stripe:customer:st_household_c",
    ]
    assert recycled.member_node_ids == [
        "hubspot:contact:hs_phone_recycle_a",
        "hubspot:contact:hs_phone_recycle_b",
        "stripe:customer:st_phone_recycle_c",
    ]
    public_tokens = {row.customer_token for row in projection.customers}
    household_token = customer_token_for(household.member_node_ids)
    assert household_token not in public_tokens

    override = make_identity_override(
        target_cluster_hash=household.cluster_hash,
        member_node_ids=household.member_node_ids,
        decided_by="fixture.operator",
        decided_at="2026-07-29T19:00:00.000Z",
    )
    resolved = apply_identity_override(graph, override)
    assert len(resolved.ambiguous_identities) == len(graph.ambiguous_identities) - 1
    assert all(
        item.cluster_hash != household.cluster_hash for item in resolved.ambiguous_identities
    )
    assert any(item.cluster_hash == recycled.cluster_hash for item in resolved.ambiguous_identities)
    assert any(item.cluster_hash == conflict.cluster_hash for item in resolved.ambiguous_identities)
    assert any(
        set(cluster.member_node_ids) == set(household.member_node_ids)
        for cluster in resolved.customers
    )
    resolved_projection = public_identity_projection(resolved)
    assert resolved_projection.customer_count == len(graph.customers) + 1
    assert household_token in {row.customer_token for row in resolved_projection.customers}
    assert_identity_count_consistency(resolved, resolved_projection)


def test_ac4_unified_build_emits_canonical_identity_artifacts(monkeypatch, tmp_path):
    import found_money.build as build_module
    from found_money.rendering.proof import FOUR_ROOM_PNGS, PNG_SIGNATURE, REQUIRED_ARTIFACTS

    def fake_capture(*_args, **_kwargs):
        artifacts = {
            name: PNG_SIGNATURE + b"fake-png" if name.endswith(".png") else b"%PDF-1.4\nfake\n"
            for name in REQUIRED_ARTIFACTS
        }
        artifacts["print-report-contact-sheet.png"] = PNG_SIGNATURE + b"fake-contact"
        artifacts.update(
            {
                f"print-report-page-{page:02d}.png": PNG_SIGNATURE + f"page-{page}".encode()
                for page in range(1, 20)
            }
        )
        return artifacts

    monkeypatch.setattr(build_module, "capture_recovery_room_artifacts", fake_capture)
    monkeypatch.setattr(
        build_module,
        "capture_four_room_screenshots",
        lambda *_a, **_k: {name: PNG_SIGNATURE + b"fake-png" for name in FOUR_ROOM_PNGS},
    )
    monkeypatch.setattr(
        build_module,
        "validate_print_review_packet",
        lambda: None,
    )
    monkeypatch.chdir(tmp_path)
    public = build(output_root="public-out", source_config=CONFIG)
    public_projection = public.output_root / IDENTITY_PUBLIC_PATH
    public_evidence = public.output_root / IDENTITY_STAGE_PATH
    assert public_projection.is_file()
    assert public_evidence.is_file()
    assert not (public.output_root / IDENTITY_GRAPH_PATH).exists()
    projection_payload = json.loads(public_projection.read_text(encoding="utf-8"))
    evidence = parse_identity_stage_evidence(public_evidence.read_bytes())
    assert projection_payload["schema_version"] == "identity-public.v1"
    assert projection_payload["customer_count"] == evidence.resolved_customer_count
    assert (
        evidence.source_set_hash
        == json.loads((public.output_root / "run.json").read_text())["source_set_hash"]
    )
    assert evidence.ambiguous_cluster_count == 0
    assert set(evidence.match_rule_counts) == ALLOWED_RULES
    assert {ref.declared_source for ref in evidence.source_references} == {"hubspot", "stripe"}
    assert (
        scan_text_artifact(IDENTITY_PUBLIC_PATH, public_projection.read_text(), suffix=".json")
        == []
    )
    assert (
        scan_text_artifact(IDENTITY_STAGE_PATH, public_evidence.read_text(), suffix=".json") == []
    )
    validate_public_artifact_payloads(
        {
            IDENTITY_PUBLIC_PATH: public_projection.read_bytes(),
            IDENTITY_STAGE_PATH: public_evidence.read_bytes(),
        }
    )

    private_config = tmp_path / "private.json"
    private_config.write_text(
        json.dumps(
            {
                "schema_version": "found-money-build-source.v1",
                "mode": "private",
                "source_mode": "fixture",
                "credentials": {"credential_mode": "none", "runtime_mode": "test", "scopes": []},
                "fixture": "synthetic-saas-thin-slice",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    private = build(output_root="private-out", source_config=private_config)
    assert (private.output_root / IDENTITY_GRAPH_PATH).is_file()
    private_graph = json.loads((private.output_root / IDENTITY_GRAPH_PATH).read_text())
    private_projection = json.loads((private.output_root / IDENTITY_PUBLIC_PATH).read_text())
    assert private_graph["schema_version"] == "identity-graph.v1"
    assert private_projection["customer_count"] == len(private_graph["customers"])
    assert "email" not in json.dumps(private_projection)


def test_ac5_canonical_bytes_tokens_order_and_atomic_paths(tmp_path):
    source_set, payloads = _matrix_source_set_and_payloads()
    first = run_identity_stage(
        source_set,
        payloads,
        output_root=tmp_path / "identity-a",
        run_id=RUN_ID,
        built_at=WHEN,
    )
    second = run_identity_stage(
        source_set,
        payloads,
        output_root=tmp_path / "identity-b",
        run_id=RUN_ID,
        built_at=WHEN,
    )
    assert first.graph.to_canonical_json() == second.graph.to_canonical_json()
    assert first.projection.to_canonical_json() == second.projection.to_canonical_json()
    assert first.evidence.to_canonical_json() == second.evidence.to_canonical_json()
    assert first.graph.to_canonical_json().endswith(b"\n")
    assert first.graph.to_canonical_json().count(b"\n") == 1
    edge_order = [
        (edge.left_node_id, edge.right_node_id, edge.match_rule) for edge in first.graph.edges
    ]
    assert edge_order == sorted(edge_order)
    token_order = [cluster.customer_token for cluster in first.graph.customers]
    assert token_order == sorted(token_order)
    assert first.projection.customers[0].customer_token == token_order[0]

    before = sorted(tmp_path.iterdir())
    with pytest.raises(IdentityStageError):
        run_identity_stage(
            source_set,
            payloads,
            output_root=tmp_path / ".." / "escape",
            run_id=RUN_ID,
            built_at=WHEN,
        )
    with pytest.raises(IdentityStageError):
        run_identity_stage(
            source_set,
            payloads,
            output_root=tmp_path / "identity-a",
            run_id=RUN_ID,
            built_at=WHEN,
        )
    assert sorted(tmp_path.iterdir()) == before
    assert not any(
        path.name.startswith(".found-money-identity-stage-") for path in tmp_path.iterdir()
    )


def test_ac5_malformed_duplicate_and_unsupported_fields_fail_closed(tmp_path):
    with pytest.raises(ValueError, match="unsupported identity field"):
        normalize_source_records(
            {
                "hubspot": {
                    "contacts": [{"id": "hs_bad", "fuzzy_score": 0.9, "email": "a@example.invalid"}]
                }
            }
        )
    with pytest.raises(ValueError, match="missing a source id"):
        normalize_source_records({"stripe": {"customers": [{"email": "a@example.invalid"}]}})
    with pytest.raises(ValueError, match="email must be a normalized exact address"):
        normalize_source_records(
            {"hubspot": {"contacts": [{"id": "hs_bad_email", "email": "not-an-email"}]}}
        )
    with pytest.raises(ValueError, match="phone must be unique exact E.164"):
        normalize_source_records(
            {"stripe": {"customers": [{"id": "st_bad_phone", "phone": "555-0100"}]}}
        )
    with pytest.raises(ValueError, match="duplicate identity source records"):
        normalize_source_records(
            {
                "hubspot": {
                    "contacts": [
                        {"id": "hs_dup", "display_name": "Same"},
                        {"id": "hs_dup", "display_name": "Same"},
                    ]
                }
            }
        )
    with pytest.raises(ValueError, match="unsupported identity source type"):
        normalize_source_records({"salesforce": {"accounts": [{"id": "x"}]}})
    with pytest.raises(ValidationError):
        IdentityEdgeV1.model_validate(
            {
                "left_node_id": "a",
                "right_node_id": "b",
                "match_rule": "name_similarity",
                "match_namespace": None,
                "match_value": "x",
                "lineage": {"left_source": "hubspot:a", "right_source": "stripe:b"},
            }
        )
    with pytest.raises(ValidationError):
        IdentityStageEvidenceV1.model_validate(
            {
                "schema_version": "identity-stage.v1",
                "run_id": "run_x",
                "built_at": "2026-07-29T18:00:05.000Z",
                "source_set_hash": "a" * 64,
                "private_graph_path": "identity/identity-graph.json",
                "private_graph_sha256": "b" * 64,
                "public_projection_path": "identity/identity-public.json",
                "public_projection_sha256": "c" * 64,
                "resolved_customer_count": 0,
                "ambiguous_cluster_count": 0,
                "quarantined_member_count": 0,
                "match_rule_counts": {"fuzzy_name": 1},
                "source_references": [],
            }
        )
    source_set, payloads = _matrix_source_set_and_payloads()
    bad_payloads = dict(payloads)
    bad_payloads["hubspot"] = {**payloads["hubspot"], "fuzzy_score": 0.42}
    with pytest.raises(IdentityStageError):
        run_identity_stage(
            source_set,
            bad_payloads,
            output_root=tmp_path / "bad-identity",
            run_id=RUN_ID,
            built_at=WHEN,
        )
    assert not (tmp_path / "bad-identity").exists()
    assert not any(
        path.name.startswith(".found-money-identity-stage-") for path in tmp_path.iterdir()
    )


def test_ac5_stale_payload_content_hash_and_source_set_mismatch_fail_before_write(tmp_path):
    source_set, payloads = _matrix_source_set_and_payloads()
    verified_payloads, verified_hashes = verify_source_set_payloads(source_set, payloads)
    assert set(verified_payloads) == set(payloads)
    assert set(verified_hashes) == set(payloads)
    for source_id, payload in payloads.items():
        assert verified_hashes[source_id] == sha256_bytes(canonical_identity_payload_bytes(payload))
        assert verified_hashes[source_id] == sha256_bytes(
            canonical_identity_payload_bytes(
                (
                    json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                    + "\n"
                ).encode()
            )
        )

    tampered = {key: dict(value) for key, value in payloads.items()}
    stripe_customers = [dict(row) for row in tampered["stripe"]["customers"]]
    for row in stripe_customers:
        if row.get("email") == "synth.join@example.invalid":
            row["email"] = "tampered.join@example.invalid"
            break
    tampered["stripe"] = {**tampered["stripe"], "customers": stripe_customers}
    out = tmp_path / "stale-payload"
    with pytest.raises(ValueError, match="content_hash"):
        normalize_source_set(source_set, tampered)
    with pytest.raises(IdentityStageError):
        run_identity_stage(
            source_set,
            tampered,
            output_root=out,
            run_id=RUN_ID,
            built_at=WHEN,
        )
    assert not out.exists()
    assert not any(
        path.name.startswith(".found-money-identity-stage-") for path in tmp_path.iterdir()
    )

    mismatched = source_set.model_copy(update={"source_set_hash": "a" * 64})
    mismatch_out = tmp_path / "source-set-mismatch"
    with pytest.raises(ValueError, match="source_set_hash"):
        normalize_source_set(mismatched, payloads)
    with pytest.raises(IdentityStageError):
        run_identity_stage(
            mismatched,
            payloads,
            output_root=mismatch_out,
            run_id=RUN_ID,
            built_at=WHEN,
        )
    assert not mismatch_out.exists()

    raw_payloads = {key: canonical_identity_payload_bytes(value) for key, value in payloads.items()}
    raw_nodes = normalize_source_set(source_set, raw_payloads)
    mapping_nodes = normalize_source_set(source_set, payloads)
    assert [node.payload_hash for node in raw_nodes] == [
        node.payload_hash for node in mapping_nodes
    ]
    tampered_bytes = dict(raw_payloads)
    tampered_bytes["stripe"] = bytearray(tampered_bytes["stripe"])
    tampered_bytes["stripe"][0] ^= 0x01
    tampered_bytes["stripe"] = bytes(tampered_bytes["stripe"])
    with pytest.raises(ValueError, match="content_hash"):
        normalize_source_set(source_set, tampered_bytes)


@pytest.mark.parametrize("source_key", ["orders", "appointments", "proposals"])
@pytest.mark.parametrize("customer_id", [None, "", "   "])
def test_ac5_missing_blank_file_source_customer_id_fails_closed_without_write(
    tmp_path, source_key, customer_id
):
    schemas = {
        "orders": "orders.v1",
        "appointments": "appointments.v1",
        "proposals": "proposals.v1",
    }
    row_keys = {
        "orders": "orders",
        "appointments": "appointments",
        "proposals": "proposals",
    }
    rows = {
        "orders": {
            "order_id": "o1",
            "ordered_at": "2026-07-29T18:00:00.000Z",
            "currency": "USD",
            "total_minor": 1000,
        },
        "appointments": {
            "appointment_id": "a1",
            "scheduled_at": "2026-07-29T18:00:00.000Z",
            "status": "scheduled",
        },
        "proposals": {
            "proposal_id": "p1",
            "proposed_at": "2026-07-29T18:00:00.000Z",
            "status": "sent",
            "amount_minor": 5000,
            "currency": "USD",
        },
    }
    row = dict(rows[source_key])
    if customer_id is not None:
        row["customer_id"] = customer_id
    payload = {"schema_version": schemas[source_key], row_keys[source_key]: [row]}
    with pytest.raises(ValueError, match="customer_id"):
        normalize_source_records({source_key: payload})

    mixed = {
        "schema_version": schemas[source_key],
        row_keys[source_key]: [
            {**rows[source_key], "customer_id": "valid_customer"},
            row,
        ],
    }
    with pytest.raises(ValueError, match="customer_id"):
        normalize_source_records({source_key: mixed})

    source_set = _source_set_for(
        {source_key: payload},
        {source_key: source_key},
        {source_key: schemas[source_key]},
    )
    out = tmp_path / f"missing-{source_key}-{customer_id!r}"
    with pytest.raises(IdentityStageError):
        run_identity_stage(
            source_set,
            {source_key: payload},
            output_root=out,
            run_id="run_missing_key",
            built_at=WHEN,
        )
    assert not out.exists()
    assert not any(
        path.name.startswith(".found-money-identity-stage-") for path in tmp_path.iterdir()
    )


def test_ac4_identity_stage_bundle_rejects_recanonicalized_semantic_tamper():
    source_set, payloads = _matrix_source_set_and_payloads()
    graph, projection = _matrix_graph()
    from found_money.identity.stage import build_identity_stage_evidence

    _decoded, verified = verify_source_set_payloads(source_set, payloads)
    evidence = build_identity_stage_evidence(
        graph,
        projection,
        source_set_hash=source_set.source_set_hash,
        source_references=[
            {
                "declared_source": entry.source_id,
                "source_type": entry.source_type,
                "receipt_path": entry.receipt_path,
                "content_hash": verified[entry.source_id],
                "identity_node_count": sum(
                    1 for node in graph.nodes if node.source_system == entry.source_id
                ),
            }
            for entry in source_set.sources
        ],
    )
    validate_identity_stage_bundle(
        graph=graph,
        projection=projection,
        evidence=evidence,
        source_set=source_set,
        payloads=payloads,
    )

    tamper_cases = [
        ("resolved_customer_count", evidence.resolved_customer_count + 5),
        ("ambiguous_cluster_count", evidence.ambiguous_cluster_count + 1),
        ("private_graph_sha256", sha256_bytes(b"tampered-graph")),
        ("public_projection_sha256", sha256_bytes(b"tampered-projection")),
        ("source_set_hash", "a" * 64),
    ]
    for field, value in tamper_cases:
        payload = json.loads(evidence.to_canonical_json())
        payload[field] = value
        tampered = (
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        parse_identity_stage_evidence(tampered)
        with pytest.raises(IdentityStageError):
            validate_identity_stage_bundle(
                graph=graph,
                projection=projection,
                evidence=tampered,
                source_set=source_set,
                payloads=payloads,
            )

    rules = json.loads(evidence.to_canonical_json())
    rules["match_rule_counts"] = {
        **rules["match_rule_counts"],
        "unique_exact_email": rules["match_rule_counts"]["unique_exact_email"] + 1,
    }
    tampered_rules = (
        json.dumps(rules, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    parse_identity_stage_evidence(tampered_rules)
    with pytest.raises(IdentityStageError):
        validate_identity_stage_bundle(
            graph=graph,
            projection=projection,
            evidence=tampered_rules,
            source_set=source_set,
            payloads=payloads,
        )

    refs = json.loads(evidence.to_canonical_json())
    refs["source_references"][0]["content_hash"] = "b" * 64
    tampered_refs = (
        json.dumps(refs, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    parse_identity_stage_evidence(tampered_refs)
    with pytest.raises(IdentityStageError):
        validate_identity_stage_bundle(
            graph=graph,
            projection=projection,
            evidence=tampered_refs,
            source_set=source_set,
            payloads=payloads,
        )


def test_ac6_public_safety_and_no_network(monkeypatch, tmp_path):
    def forbid(*_args, **_kwargs):
        raise AssertionError("identity integration attempted network access")

    monkeypatch.setattr(socket, "socket", forbid)
    monkeypatch.setattr(socket, "create_connection", forbid)
    graph, projection = _matrix_graph()
    source_set, payloads = _matrix_source_set_and_payloads()
    result = run_identity_stage(
        source_set,
        payloads,
        output_root=tmp_path / "safe",
        run_id=RUN_ID,
        built_at=WHEN,
    )
    assert result.projection.to_canonical_json() == projection.to_canonical_json()
    assert (
        scan_text_artifact(
            IDENTITY_PUBLIC_PATH, result.projection.to_canonical_json().decode(), suffix=".json"
        )
        == []
    )
    assert (
        scan_text_artifact(
            IDENTITY_STAGE_PATH, result.evidence.to_canonical_json().decode(), suffix=".json"
        )
        == []
    )
    validate_public_artifact_payloads(
        {
            IDENTITY_PUBLIC_PATH: result.projection.to_canonical_json(),
            IDENTITY_STAGE_PATH: result.evidence.to_canonical_json(),
        }
    )
    with pytest.raises(Exception):
        validate_public_artifact_payloads(
            {IDENTITY_PUBLIC_PATH: b'{"schema_version":"identity-public.v1","email":"a@b.co"}\n'}
        )


def test_r_evidence_packet_matches_locked_canonical_bytes(tmp_path):
    source_set, payloads = _matrix_source_set_and_payloads()
    result = run_identity_stage(
        source_set,
        payloads,
        output_root=tmp_path / "evidence-run",
        run_id=RUN_ID,
        built_at=WHEN,
    )
    household = next(
        item for item in result.graph.ambiguous_identities if item.reason == "household_email"
    )
    override = make_identity_override(
        target_cluster_hash=household.cluster_hash,
        member_node_ids=household.member_node_ids,
        decided_by="fixture.operator",
        decided_at="2026-07-29T19:00:00.000Z",
    )
    resolved = apply_identity_override(result.graph, override)
    ledger = {
        "schema_version": "fm039-matrix-ledger.v1",
        "run_id": RUN_ID,
        "allowed_match_rules": list(ALLOWED_MATCH_RULES),
        "match_rule_counts": result.evidence.match_rule_counts,
        "resolved_customer_count": result.evidence.resolved_customer_count,
        "ambiguous_cluster_count": result.evidence.ambiguous_cluster_count,
        "quarantined_member_count": result.evidence.quarantined_member_count,
        "private_graph_sha256": result.evidence.private_graph_sha256,
        "public_projection_sha256": result.evidence.public_projection_sha256,
        "source_set_hash": result.evidence.source_set_hash,
        "override_integrity": override.integrity,
        "override_target_cluster_hash": override.target_cluster_hash,
        "resolved_after_override_count": len(resolved.customers),
        "ambiguous_after_override_count": len(resolved.ambiguous_identities),
    }
    ledger_bytes = (
        json.dumps(ledger, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    assert (EVIDENCE / "identity-graph.json").read_bytes() == result.graph.to_canonical_json()
    assert (EVIDENCE / "identity-public.json").read_bytes() == result.projection.to_canonical_json()
    assert (EVIDENCE / "identity-stage.json").read_bytes() == result.evidence.to_canonical_json()
    assert (EVIDENCE / "identity-override.json").read_bytes() == override.to_canonical_json()
    assert (EVIDENCE / "matrix-ledger.json").read_bytes() == ledger_bytes
    parse_identity_stage_evidence((EVIDENCE / "identity-stage.json").read_bytes())
    validate_identity_stage_bundle(
        graph=result.graph,
        projection=result.projection,
        evidence=result.evidence,
        source_set=source_set,
        payloads=payloads,
    )
    validate_identity_stage_bundle(
        graph=(EVIDENCE / "identity-graph.json").read_bytes(),
        projection=(EVIDENCE / "identity-public.json").read_bytes(),
        evidence=(EVIDENCE / "identity-stage.json").read_bytes(),
        source_set=source_set,
        payloads=payloads,
    )
    assert (
        scan_text_artifact(
            "identity-public.json",
            (EVIDENCE / "identity-public.json").read_text(),
            suffix=".json",
        )
        == []
    )
    assert (
        scan_text_artifact(
            "identity-stage.json",
            (EVIDENCE / "identity-stage.json").read_text(),
            suffix=".json",
        )
        == []
    )
