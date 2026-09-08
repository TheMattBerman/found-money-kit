"""Adversarial FM-039 identity integration attacks: NG-1..NG-4 plus path/hash closure."""

from __future__ import annotations

import ast
import json
import socket
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from found_money.contracts.identity import IdentityOverrideEnvelopeV1
from found_money.contracts.identity_stage import parse_identity_stage_evidence
from found_money.identity import (
    apply_identity_override,
    build_identity_graph,
    load_fm039_matrix_snapshots,
    make_identity_override,
    normalize_source_records,
    normalize_source_set,
    parse_identity_override,
)
from found_money.identity.stage import IdentityStageError, run_identity_stage
from found_money.safety.capability import FORBIDDEN_ATTR_CALLS, FORBIDDEN_IMPORT_ROOTS

ROOT = Path(__file__).resolve().parents[2]
IDENTITY_INIT = ROOT / "found_money" / "identity" / "__init__.py"
IDENTITY_STAGE = ROOT / "found_money" / "identity" / "stage.py"
IDENTITY_STAGE_CONTRACT = ROOT / "found_money" / "contracts" / "identity_stage.py"
FM039_TESTS = ROOT / "tests" / "contract" / "test_identity_integration_fm039.py"
WHEN = datetime(2026, 7, 29, 18, 0, 5, tzinfo=timezone.utc)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
            names.add(node.module)
    return names


def _call_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id.casefold())
            elif isinstance(func, ast.Attribute):
                names.add(func.attr.casefold())
    return names


def test_ng1_no_new_join_rule_fuzzy_or_override_format():
    source = IDENTITY_INIT.read_text(encoding="utf-8") + IDENTITY_STAGE.read_text(encoding="utf-8")
    assert "split_members" not in source
    assert "create_audience" not in source
    assert 'match_rule": "fuzzy_name"' not in source
    graph = build_identity_graph(
        normalize_source_records(load_fm039_matrix_snapshots()),
        run_id="run_ng1",
        built_at=WHEN,
    )
    household = next(
        item for item in graph.ambiguous_identities if item.reason == "household_email"
    )
    with pytest.raises(ValidationError):
        IdentityOverrideEnvelopeV1.model_validate(
            {
                "schema_version": "identity-override.v2",
                "target_cluster_hash": household.cluster_hash,
                "member_node_ids": household.member_node_ids,
                "resolution": "merge_members",
                "decided_by": "fixture.operator",
                "decided_at": "2026-07-29T19:00:00.000Z",
                "integrity": "a" * 64,
            }
        )
    with pytest.raises((ValidationError, ValueError)):
        make_identity_override(
            target_cluster_hash=household.cluster_hash,
            member_node_ids=household.member_node_ids,
            decided_by="fixture.operator",
            decided_at="2026-07-29T19:00:00.000Z",
            resolution="split_members",
        )
    same_name = [
        node
        for node in graph.nodes
        if node.display_name == "Identical Synth Name"
        and node.object_type in {"contact", "customer"}
    ]
    assert len(same_name) == 2
    assert not any(
        set(node.node_id for node in same_name).issubset(set(cluster.member_node_ids))
        for cluster in graph.customers
    )


def test_ng1_wrong_override_leaves_unrelated_clusters_untouched():
    graph = build_identity_graph(
        normalize_source_records(load_fm039_matrix_snapshots()),
        run_id="run_ng1_override",
        built_at=WHEN,
    )
    household = next(
        item for item in graph.ambiguous_identities if item.reason == "household_email"
    )
    recycled = next(item for item in graph.ambiguous_identities if item.reason == "recycled_phone")
    valid = make_identity_override(
        target_cluster_hash=household.cluster_hash,
        member_node_ids=household.member_node_ids,
        decided_by="fixture.operator",
        decided_at="2026-07-29T19:00:00.000Z",
    )
    tampered = bytearray(valid.to_canonical_json())
    tampered[20] ^= 0x01
    with pytest.raises(ValueError):
        parse_identity_override(bytes(tampered))
    wrong_target = make_identity_override(
        target_cluster_hash=recycled.cluster_hash,
        member_node_ids=household.member_node_ids,
        decided_by="fixture.operator",
        decided_at="2026-07-29T19:00:00.000Z",
    )
    with pytest.raises(ValueError, match="member_node_ids do not match target cluster"):
        apply_identity_override(graph, wrong_target)
    assert len(graph.ambiguous_identities) == 3


def test_ng2_identity_stage_does_not_grow_connectors_or_events():
    stage_source = IDENTITY_STAGE.read_text(encoding="utf-8")
    assert "found_money.connectors" not in stage_source
    assert "detect_event_families" not in stage_source
    assert "build_money_map" not in stage_source
    assert "build_launch_pack" not in stage_source
    assert "httpx" not in stage_source
    imports = _imports(IDENTITY_STAGE) | _imports(IDENTITY_STAGE_CONTRACT)
    assert "httpx" not in imports
    assert "requests" not in imports
    assert "found_money.events" not in imports


def test_ng3_zero_network_credential_send_or_audience_capability(monkeypatch):
    imports = _imports(IDENTITY_INIT) | _imports(IDENTITY_STAGE) | _imports(IDENTITY_STAGE_CONTRACT)
    assert not (imports & FORBIDDEN_IMPORT_ROOTS)
    calls = (
        _call_names(IDENTITY_INIT)
        | _call_names(IDENTITY_STAGE)
        | _call_names(IDENTITY_STAGE_CONTRACT)
    )
    assert not (calls & FORBIDDEN_ATTR_CALLS)
    assert "create_audience" not in calls
    assert "sendmail" not in calls

    def forbid(*_args, **_kwargs):
        raise AssertionError("FM-039 identity code attempted network access")

    monkeypatch.setattr(socket, "socket", forbid)
    monkeypatch.setattr(socket, "create_connection", forbid)
    normalize_source_records(load_fm039_matrix_snapshots())
    build_identity_graph(
        normalize_source_records(load_fm039_matrix_snapshots()),
        run_id="run_ng3",
        built_at=WHEN,
    )


def test_ng4_does_not_edit_prior_identity_issue_fixtures():
    prior = ROOT / "tests" / "fixtures" / "saas" / "identity"
    locked = {
        "same_source_object_id.json",
        "unique_exact_email.json",
        "unique_exact_e164_phone.json",
        "same_display_name_no_key.json",
        "conflicting_stronger_identifiers.json",
        "household_email.json",
        "recycled_phone.json",
        "overrides/valid_merge_household_email.json",
    }
    for name in locked:
        assert (prior / name).is_file()
    fm002 = ROOT / "tests" / "contract" / "test_identity_graph.py"
    fm017 = ROOT / "tests" / "contract" / "test_identity_ambiguity.py"
    assert "FM-002" in fm002.read_text(encoding="utf-8")
    assert "FM-017" in fm017.read_text(encoding="utf-8")
    assert FM039_TESTS.is_file()


def test_adversarial_noncanonical_stage_evidence_and_duplicate_keys():
    graph = build_identity_graph(
        normalize_source_records(load_fm039_matrix_snapshots()),
        run_id="run_adv_canonical",
        built_at=WHEN,
    )
    from found_money.identity import public_identity_projection
    from found_money.identity.stage import build_identity_stage_evidence

    projection = public_identity_projection(graph)
    evidence = build_identity_stage_evidence(
        graph,
        projection,
        source_set_hash="a" * 64,
        source_references=[
            {
                "declared_source": "hubspot",
                "source_type": "hubspot",
                "receipt_path": "receipts/hubspot.source-receipt.json",
                "content_hash": "b" * 64,
                "identity_node_count": 1,
            }
        ],
    )
    canonical = evidence.to_canonical_json()
    pretty = json.dumps(json.loads(canonical), indent=2).encode() + b"\n"
    with pytest.raises(ValueError, match="not exactly canonical"):
        parse_identity_stage_evidence(pretty)
    duplicate = canonical.replace(b'"run_id":', b'"run_id":"x","run_id":', 1)
    with pytest.raises(ValueError):
        parse_identity_stage_evidence(duplicate)
    with pytest.raises(ValueError):
        parse_identity_stage_evidence(canonical[:-2])


def test_adversarial_source_set_payload_mismatch_and_path_escape(tmp_path):
    from found_money.contracts.run import compute_source_set_hash
    from found_money.contracts.source_stage import SourceSetEntryV1, SourceSetManifestV1
    from found_money.identity import sha256_bytes

    payloads = load_fm039_matrix_snapshots()
    encoded = {
        key: (
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        for key, value in payloads.items()
    }
    entries = []
    hashes = {}
    for source_id, data in encoded.items():
        receipt_path = f"receipts/{source_id}.source-receipt.json"
        hashes[receipt_path] = sha256_bytes(data)
        entries.append(
            SourceSetEntryV1(
                source_id=source_id,
                source_type=source_id,
                connector_schema_version=(
                    "hubspot-crm.2026-03.v1"
                    if source_id == "hubspot"
                    else "stripe.2026-02-25.clover.v1"
                ),
                normalized_path=f"normalized/{source_id}/snapshot.json",
                receipt_path=receipt_path,
                content_hash=hashes[receipt_path],
                page_or_row_count=1,
                record_count=1,
            )
        )
    source_set = SourceSetManifestV1(
        source_set_hash=compute_source_set_hash(hashes),
        sources=entries,
    )
    with pytest.raises(ValueError, match="must match the source-set exactly"):
        normalize_source_set(source_set, {"hubspot": payloads["hubspot"]})
    with pytest.raises(IdentityStageError):
        run_identity_stage(
            source_set,
            payloads,
            output_root=tmp_path / "missing-parent" / "out",
            run_id="run_escape",
            built_at=WHEN,
        )
    assert not (tmp_path / "missing-parent").exists()
    existing = tmp_path / "already"
    existing.mkdir()
    with pytest.raises(IdentityStageError, match="must not already exist"):
        run_identity_stage(
            source_set,
            payloads,
            output_root=existing,
            run_id="run_exists",
            built_at=WHEN,
        )
    sentinel = existing / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")
    assert sentinel.read_text(encoding="utf-8") == "keep"
    tampered_payloads = {key: dict(value) for key, value in payloads.items()}
    tampered_stripe = dict(payloads["stripe"])
    tampered_customers = [dict(row) for row in tampered_stripe["customers"]]
    tampered_customers[0]["email"] = "stale@example.invalid"
    tampered_stripe["customers"] = tampered_customers
    tampered_payloads["stripe"] = tampered_stripe
    stale_out = tmp_path / "stale-hash"
    with pytest.raises(ValueError, match="content_hash"):
        normalize_source_set(source_set, tampered_payloads)
    with pytest.raises(IdentityStageError):
        run_identity_stage(
            source_set,
            tampered_payloads,
            output_root=stale_out,
            run_id="run_stale",
            built_at=WHEN,
        )
    assert not stale_out.exists()


@pytest.mark.parametrize(
    "tamper",
    [
        "resolved_customer_count",
        "private_graph_sha256",
        "match_rule_counts",
        "source_ref_content_hash",
    ],
)
def test_adversarial_recanonicalized_identity_stage_tamper_fails_closed(tamper):
    from found_money.identity import (
        public_identity_projection,
        sha256_bytes,
        verify_source_set_payloads,
    )
    from found_money.identity.stage import (
        build_identity_stage_evidence,
        validate_identity_stage_bundle,
    )
    from found_money.contracts.run import compute_source_set_hash
    from found_money.contracts.source_stage import SourceSetEntryV1, SourceSetManifestV1

    payloads = load_fm039_matrix_snapshots()
    hashes = {}
    entries = []
    for source_id, payload in payloads.items():
        encoded = (
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        receipt_path = f"receipts/{source_id}.source-receipt.json"
        hashes[receipt_path] = sha256_bytes(encoded)
        entries.append(
            SourceSetEntryV1(
                source_id=source_id,
                source_type=source_id,
                connector_schema_version=(
                    "hubspot-crm.2026-03.v1"
                    if source_id == "hubspot"
                    else "stripe.2026-02-25.clover.v1"
                ),
                normalized_path=f"normalized/{source_id}/snapshot.json",
                receipt_path=receipt_path,
                content_hash=hashes[receipt_path],
                page_or_row_count=1,
                record_count=1,
            )
        )
    source_set = SourceSetManifestV1(
        source_set_hash=compute_source_set_hash(hashes),
        sources=entries,
    )
    graph = build_identity_graph(
        normalize_source_records(payloads),
        run_id="run_adv_tamper",
        built_at=WHEN,
    )
    projection = public_identity_projection(graph)
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
    payload = json.loads(evidence.to_canonical_json())
    if tamper == "resolved_customer_count":
        payload["resolved_customer_count"] = payload["resolved_customer_count"] + 5
    elif tamper == "private_graph_sha256":
        payload["private_graph_sha256"] = sha256_bytes(b"tampered-graph")
    elif tamper == "match_rule_counts":
        payload["match_rule_counts"]["unique_exact_email"] += 1
    else:
        payload["source_references"][0]["content_hash"] = "c" * 64
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
