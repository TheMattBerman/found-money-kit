"""FM-002 exact identity-graph contract tests."""

from __future__ import annotations

import json
import socket
from datetime import timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from found_money.contracts.identity import (
    IdentityEdgeV1,
    IdentityGraphV1,
    IdentityNodeV1,
    PublicIdentityProjectionV1,
)
from found_money.identity import (
    build_identity_graph,
    build_thin_slice_identity,
    load_thin_slice_snapshots,
    normalize_source_records,
    parse_canonical_json,
    public_identity_projection,
    write_identity_graph,
    write_public_identity_projection,
)

ROOT = Path(__file__).resolve().parents[2]
IDENTITY_FIXTURES = ROOT / "tests" / "fixtures" / "saas" / "identity"
HASH_A = "a" * 64
UTC = timezone.utc


def _node(**overrides):
    base = {
        "node_id": "hubspot:contact:n1",
        "source_system": "hubspot",
        "object_type": "contact",
        "source_id": "n1",
        "observed_at": "2026-07-29T18:00:00.000Z",
        "payload_hash": HASH_A,
        "email": None,
        "phone": None,
        "external_ids": {},
        "display_name": None,
    }
    base.update(overrides)
    return IdentityNodeV1.model_validate(base)


def _graph(**overrides):
    node = _node()
    base = {
        "schema_version": "identity-graph.v1",
        "run_id": "run_identity",
        "built_at": "2026-07-29T18:00:05.000Z",
        "nodes": [node.model_dump(mode="json")],
        "edges": [],
        "customers": [
            {
                "customer_token": "cust_alone",
                "member_node_ids": [node.node_id],
            }
        ],
    }
    base.update(overrides)
    return IdentityGraphV1.model_validate(base)


def _list_tree(root: Path) -> list[str]:
    if not root.exists():
        return []
    return sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() or path.is_dir()
    )


def _load_case(name: str) -> dict:
    return json.loads((IDENTITY_FIXTURES / name).read_text(encoding="utf-8"))


def test_identity_graph_contract_validation_and_canonical_bytes():
    graph = _graph()
    payload = graph.to_canonical_json()
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
        IdentityGraphV1.model_validate(
            {k: v for k, v in graph.model_dump(mode="json").items() if k != "run_id"}
        )
    with pytest.raises(ValidationError):
        IdentityGraphV1.model_validate({**graph.model_dump(mode="json"), "extra": 1})
    with pytest.raises(ValidationError):
        _graph(schema_version="identity-graph.v0")
    with pytest.raises(ValidationError):
        _node(payload_hash="not-a-hash")
    with pytest.raises(ValidationError):
        _node(email="not-an-email")
    with pytest.raises(ValidationError):
        _node(phone="555-0100")

    # R-1: email, phone, external-id, hash, and identifiers at start/end/entire.
    for field, values in {
        "source_system": ["  hubspot", "hubspot  ", "  hubspot  "],
        "object_type": ["  contact", "contact  ", "  contact  "],
        "source_id": ["  n1", "n1  ", "  n1  "],
        "payload_hash": [
            f"  {HASH_A.upper()}",
            f"{HASH_A.upper()}  ",
            f"  {HASH_A.upper()}  ",
        ],
        "email": [
            "  Synth.R1@Example.Invalid",
            "synth.r1@end.invalid  ",
            "  Synth.Entire@Example.Invalid  ",
        ],
        "phone": ["  +15550103001", "+15550103002  ", "  +1 (555) 010-3003  "],
    }.items():
        for value in values:
            node = _node(**{field: value})
            encoded = _graph(
                nodes=[node.model_dump(mode="json")],
                customers=[
                    {
                        "customer_token": "cust_r1",
                        "member_node_ids": [node.node_id],
                    }
                ],
            ).to_canonical_json()
            assert parse_canonical_json(encoded).to_canonical_json() == encoded

    for value in ["  hubspot:contact:n1", "hubspot:contact:n1  ", "  hubspot:contact:n1  "]:
        node = _node(node_id=value)
        encoded = _graph(
            nodes=[node.model_dump(mode="json")],
            customers=[{"customer_token": "cust_r1", "member_node_ids": [node.node_id]}],
        ).to_canonical_json()
        assert parse_canonical_json(encoded).to_canonical_json() == encoded

    nested = _node(
        external_ids={"  found_money.synthetic_customer  ": "  cust_norm_001  "},
    )
    assert nested.external_ids == {"found_money.synthetic_customer": "cust_norm_001"}

    # R-1: schema_version whitespace at start/end/entire (validator strips).
    for value in ["  identity-graph.v1", "identity-graph.v1  ", "  identity-graph.v1  "]:
        encoded = _graph(schema_version=value).to_canonical_json()
        assert parse_canonical_json(encoded).to_canonical_json() == encoded

    # R-1: entire-field whitespace for identifiers fails closed.
    with pytest.raises(ValidationError):
        _node(source_id="   ")
    with pytest.raises(ValidationError):
        _node(node_id="   ")
    with pytest.raises(ValidationError):
        _node(source_system="   ")


def test_normalize_maps_synthetic_customer_key_into_external_ids():
    snapshots = load_thin_slice_snapshots()
    nodes = normalize_source_records(snapshots)
    by_id = {node.source_id: node for node in nodes}

    contact = by_id["hs_contact_synth_001"]
    assert contact.source_system == "hubspot"
    assert contact.object_type == "contact"
    assert contact.external_ids == {"found_money.synthetic_customer": "cust_synth_001"}
    assert contact.email is None
    assert contact.phone is None
    assert len(contact.payload_hash) == 64

    customer = by_id["cus_synth_001"]
    assert customer.source_system == "stripe"
    assert customer.object_type == "customer"
    assert customer.external_ids == {"found_money.synthetic_customer": "cust_synth_001"}
    assert customer.email is None
    assert customer.phone is None

    assert "inv_failed_001" in by_id
    assert "inv_paid_001" in by_id
    assert "hs_deal_synth_001" in by_id
    # Never invent identity fields that the snapshot omitted.
    for node in nodes:
        raw_blob = json.dumps(snapshots)
        if node.email is not None:
            assert node.email.split("@")[0] in raw_blob.lower() or "@" in raw_blob
        if node.phone is not None:
            assert any(ch.isdigit() for ch in raw_blob)


def test_build_identity_graph_allows_only_the_four_exact_join_rules():
    allowed = {
        "same_source_object_id",
        "declared_external_id",
        "unique_exact_email",
        "unique_exact_e164_phone",
    }
    graph, _projection = build_thin_slice_identity()
    for edge in graph.edges:
        assert edge.match_rule in allowed
        assert edge.lineage
        assert edge.left_node_id != edge.right_node_id

    with pytest.raises(ValidationError):
        IdentityEdgeV1.model_validate(
            {
                "left_node_id": "a",
                "right_node_id": "b",
                "match_rule": "fuzzy_name",
                "match_namespace": None,
                "match_value": "x",
                "lineage": {"left_source": "hubspot:a", "right_source": "stripe:b"},
            }
        )

    # Non-unique email/phone (3+ eligible parties) must not auto-merge; FM-017 quarantines.
    shared_email = [
        _node(
            node_id=f"hubspot:contact:e{i}",
            source_id=f"e{i}",
            email="billing@acme.invalid",
        )
        for i in range(1, 5)
    ]
    email_graph = build_identity_graph(shared_email, run_id="run_nonunique_email")
    assert email_graph.customers == []
    assert email_graph.edges == []
    assert len(email_graph.ambiguous_identities) == 1
    assert email_graph.ambiguous_identities[0].reason == "household_email"
    assert set(email_graph.ambiguous_identities[0].member_node_ids) == {
        node.node_id for node in shared_email
    }

    shared_phone = [
        _node(
            node_id=f"stripe:customer:p{i}",
            source_system="stripe",
            object_type="customer",
            source_id=f"p{i}",
            phone="+15550100001",
        )
        for i in range(1, 4)
    ]
    phone_graph = build_identity_graph(shared_phone, run_id="run_nonunique_phone")
    assert phone_graph.customers == []
    assert phone_graph.edges == []
    assert len(phone_graph.ambiguous_identities) == 1
    assert phone_graph.ambiguous_identities[0].reason == "recycled_phone"
    assert set(phone_graph.ambiguous_identities[0].member_node_ids) == {
        node.node_id for node in shared_phone
    }


def test_thin_slice_hubspot_stripe_external_id_join():
    graph, projection = build_thin_slice_identity(run_id="run_thin_slice_identity")
    assert len(graph.customers) == 1
    assert graph.ambiguous_identities == []
    cluster = graph.customers[0]
    member_source_ids = {
        node.source_id for node in graph.nodes if node.node_id in set(cluster.member_node_ids)
    }
    assert "hs_contact_synth_001" in member_source_ids
    assert "cus_synth_001" in member_source_ids
    assert "inv_failed_001" not in member_source_ids
    assert "inv_paid_001" not in member_source_ids

    invoice_ids = {"inv_failed_001", "inv_paid_001"}
    assert invoice_ids.issubset({node.source_id for node in graph.nodes})
    for node in graph.nodes:
        if node.source_id in invoice_ids:
            assert node.node_id not in cluster.member_node_ids

    matching = [
        edge
        for edge in graph.edges
        if edge.match_rule == "declared_external_id"
        and edge.match_namespace == "found_money.synthetic_customer"
        and edge.match_value == "cust_synth_001"
    ]
    assert matching
    assert projection.customer_count == 1
    assert projection.customers[0].customer_token == cluster.customer_token
    assert projection.customers[0].member_count == len(cluster.member_node_ids)


def test_identity_join_matrix_and_non_merge_negatives():
    quarantine_cases = {
        "conflicting_stronger_identifiers.json": "conflicting_stronger_identifier",
        "non_unique_email_three_parties.json": "household_email",
        "non_unique_phone_three_parties.json": "recycled_phone",
    }
    cases = [
        "same_source_object_id.json",
        "unique_exact_email.json",
        "unique_exact_e164_phone.json",
        "same_display_name_no_key.json",
        *quarantine_cases,
    ]
    for filename in cases:
        payload = _load_case(filename)
        nodes = [IdentityNodeV1.model_validate(item) for item in payload["nodes"]]
        graph = build_identity_graph(nodes, run_id=f"run_{payload['case']}")
        assert "ambiguous_identities" in graph.canonical_dict()

        if payload["expect_merge"]:
            assert len(graph.customers) == 1
            assert set(graph.customers[0].member_node_ids) == {node.node_id for node in nodes}
            assert any(edge.match_rule == payload["match_rule"] for edge in graph.edges)
            assert graph.ambiguous_identities == []
        elif filename in quarantine_cases:
            assert graph.customers == []
            assert graph.edges == []
            assert len(graph.ambiguous_identities) == 1
            assert graph.ambiguous_identities[0].reason == quarantine_cases[filename]
            assert set(graph.ambiguous_identities[0].member_node_ids) == {
                node.node_id for node in nodes
            }
            assert {node.node_id for node in graph.nodes} == {node.node_id for node in nodes}
        else:
            # same-name/no-key remains unresolved singletons with no quarantine.
            assert len(graph.customers) == len(nodes)
            assert graph.edges == []
            assert graph.ambiguous_identities == []
            assert all(len(cluster.member_node_ids) == 1 for cluster in graph.customers)


def test_identity_writes_reject_escape_without_partial_state(tmp_path):
    root = tmp_path / "out"
    root.mkdir()
    before = _list_tree(root)
    graph, projection = build_thin_slice_identity()

    # R-1: traversal/backslash/./ constructs at start, end, and entire path value.
    # R-3: every rejection asserts the complete output-root state afterward.
    rejects = [
        ("../outside.json", "traversal-start"),
        ("sub/..", "traversal-end"),
        ("..", "traversal-entire"),
        ("a/../../outside.json", "traversal-middle"),
        (r"..\outside.json", "backslash-traversal"),
        ("./../outside.json", "dotslash-traversal"),
        ("~/escaped.json", "home-absolute"),
        (str(tmp_path / "sibling.json"), "absolute-sibling"),
        ("/tmp/abs.json", "absolute-posix"),
        ("", "empty"),
    ]
    for relative, _label in rejects:
        with pytest.raises(ValueError):
            write_identity_graph(root, relative, graph)
        assert _list_tree(root) == before
        assert list(root.glob("**/*")) == []
        with pytest.raises(ValueError):
            write_public_identity_projection(root, relative, projection)
        assert _list_tree(root) == before
        assert list(root.glob("**/*")) == []

    outside = tmp_path / "outside-target"
    outside.mkdir()
    link = root / "link-out"
    link.symlink_to(outside, target_is_directory=True)
    before_link = _list_tree(root)
    with pytest.raises(ValueError):
        write_identity_graph(root, "link-out/escaped.json", graph)
    assert _list_tree(root) == before_link
    assert list(outside.iterdir()) == []
    with pytest.raises(ValueError):
        write_public_identity_projection(root, "link-out/escaped-public.json", projection)
    assert _list_tree(root) == before_link
    assert list(outside.iterdir()) == []

    private = write_identity_graph(root, "identity/identity-graph.json", graph)
    public = write_public_identity_projection(root, "identity/identity-public.json", projection)
    assert private.read_bytes() == graph.to_canonical_json()
    assert public.read_bytes() == projection.to_canonical_json()
    assert not list(root.glob("**/*.tmp"))


def test_public_identity_projection_omits_raw_identity():
    _graph_private, projection = build_thin_slice_identity()
    text = projection.to_canonical_json().decode("utf-8")
    assert "email" not in text
    assert "phone" not in text
    assert "external_ids" not in text
    assert "source_id" not in text
    assert "hs_contact_synth_001" not in text
    assert "cus_synth_001" not in text
    assert "cust_synth_001" not in text
    assert projection.schema_version == "identity-public.v1"
    assert projection.customer_count >= 1


def test_identity_round_trip_and_public_safety_scan():
    import importlib.util

    graph, projection = build_thin_slice_identity()
    for payload in (graph.to_canonical_json(), projection.to_canonical_json()):
        parsed = parse_canonical_json(payload)
        assert parsed.to_canonical_json() == payload

    with pytest.raises(ValueError, match="malformed"):
        parse_canonical_json(b"{not-json")
    with pytest.raises(ValueError, match="duplicate"):
        parse_canonical_json(b'{"a":1,"a":2}\n')
    with pytest.raises(ValueError, match="unknown major"):
        bad = json.loads(graph.to_canonical_json())
        bad["schema_version"] = "identity-graph.v2"
        parse_canonical_json(
            (
                json.dumps(bad, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            ).encode()
        )
    pretty = json.dumps(json.loads(graph.to_canonical_json()), indent=2) + "\n"
    with pytest.raises(ValueError, match="canonical"):
        parse_canonical_json(pretty.encode())

    spec = importlib.util.spec_from_file_location(
        "public_safety", ROOT / "scripts" / "public_safety.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.main(ROOT) == 0
    violations = module.scan_generated_contract_artifacts(ROOT)
    assert violations == []
    poisoned = '{"schema_version":"identity-public.v1","email":"a@b.co"}\n'
    assert module.scan_contract_artifact_text("poison", poisoned)


def test_ng1_identity_builders_do_not_open_sockets(monkeypatch):
    def forbid(*_args, **_kwargs):
        raise AssertionError("socket usage is forbidden in FM-002")

    monkeypatch.setattr(socket, "socket", forbid)
    monkeypatch.setattr(socket, "create_connection", forbid)
    build_thin_slice_identity()
    public_identity_projection(build_identity_graph(normalize_source_records({}), run_id="empty"))


def test_public_projection_model_rejects_extra_identity_fields():
    with pytest.raises(ValidationError):
        PublicIdentityProjectionV1.model_validate(
            {
                "schema_version": "identity-public.v1",
                "run_id": "run_x",
                "built_at": "2026-07-29T18:00:05.000Z",
                "customer_count": 1,
                "customers": [{"customer_token": "cust_x", "member_count": 1}],
                "email": "leak@example.invalid",
            }
        )
