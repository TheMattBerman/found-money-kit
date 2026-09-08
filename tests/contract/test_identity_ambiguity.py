"""FM-017 ambiguous-identity quarantine and override envelope proofs (T1–T8)."""

from __future__ import annotations

import ast
import hashlib
import json
import socket
from pathlib import Path

import pytest
from pydantic import ValidationError

from found_money.contracts.identity import (
    AmbiguousIdentityClusterV1,
    IdentityGraphV1,
    IdentityNodeV1,
    IdentityOverrideEnvelopeV1,
    ambiguous_cluster_hash,
    override_envelope_integrity,
)
from found_money.identity import (
    apply_identity_override,
    build_identity_graph,
    build_thin_slice_identity,
    customer_token_for,
    make_identity_override,
    parse_identity_override,
    public_identity_projection,
    write_identity_graph,
    write_public_identity_projection,
)

ROOT = Path(__file__).resolve().parents[2]
IDENTITY_FIXTURES = ROOT / "tests" / "fixtures" / "saas" / "identity"
OVERRIDE_FIXTURES = IDENTITY_FIXTURES / "overrides"
IDENTITY_MODULE = ROOT / "found_money" / "identity" / "__init__.py"
IDENTITY_CONTRACT = ROOT / "found_money" / "contracts" / "identity.py"
HASH_A = "a" * 64


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


def _load_case(name: str) -> dict:
    return json.loads((IDENTITY_FIXTURES / name).read_text(encoding="utf-8"))


def _graph_for_fixture(name: str, run_id: str | None = None) -> IdentityGraphV1:
    payload = _load_case(name)
    nodes = [IdentityNodeV1.model_validate(item) for item in payload["nodes"]]
    return build_identity_graph(nodes, run_id=run_id or f"run_{payload['case']}")


def _list_tree(root: Path) -> list[str]:
    if not root.exists():
        return []
    return sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() or path.is_dir()
    )


def _module_imports(path: Path) -> set[str]:
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


def test_t1_ambiguous_and_override_contracts_canonical_and_hashes():
    members = ["hubspot:contact:a", "stripe:customer:b", "hubspot:contact:c"]
    reason = "household_email"
    expected_hash = ambiguous_cluster_hash(members, reason)
    digest = hashlib.sha256(
        (
            json.dumps(
                {"member_node_ids": sorted(members), "reason": reason},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    ).hexdigest()
    assert expected_hash == digest
    assert expected_hash == expected_hash.lower()

    cluster = AmbiguousIdentityClusterV1.model_validate(
        {
            "member_node_ids": members,
            "reason": reason,
            "cluster_hash": expected_hash,
        }
    )
    assert cluster.canonical_dict() == {
        "member_node_ids": sorted(members),
        "reason": reason,
        "cluster_hash": expected_hash,
    }
    payload = cluster.to_canonical_json()
    assert payload.endswith(b"\n")
    assert payload.count(b"\n") == 1
    assert (
        json.dumps(
            json.loads(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        + b"\n"
        == payload
    )

    with pytest.raises(ValidationError):
        AmbiguousIdentityClusterV1.model_validate(
            {
                "member_node_ids": members,
                "reason": reason,
                "cluster_hash": expected_hash,
                "signature": "not-allowed",
            }
        )
    with pytest.raises(ValidationError):
        AmbiguousIdentityClusterV1.model_validate(
            {
                "member_node_ids": members,
                "reason": reason,
                "reasons": [reason],
                "cluster_hash": expected_hash,
            }
        )
    with pytest.raises(ValidationError):
        AmbiguousIdentityClusterV1.model_validate(
            {
                "member_node_ids": members,
                "reason": "fuzzy_name",
                "cluster_hash": expected_hash,
            }
        )
    with pytest.raises(ValidationError):
        AmbiguousIdentityClusterV1.model_validate(
            {
                "member_node_ids": members,
                "reason": reason,
                "cluster_hash": "0" * 64,
            }
        )
    with pytest.raises(ValidationError):
        AmbiguousIdentityClusterV1.model_validate({"reason": reason, "cluster_hash": expected_hash})

    override = make_identity_override(
        target_cluster_hash=expected_hash,
        member_node_ids=members,
        decided_by="fixture.operator",
        decided_at="2026-07-29T19:00:00.000Z",
    )
    body = {k: v for k, v in override.canonical_dict().items() if k != "integrity"}
    assert override.integrity == override_envelope_integrity(body)
    assert (
        override.integrity
        == hashlib.sha256(
            (
                json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            ).encode("utf-8")
        ).hexdigest()
    )
    encoded = override.to_canonical_json()
    assert encoded.endswith(b"\n")
    assert parse_identity_override(encoded).to_canonical_json() == encoded

    for forbidden in ("public_key", "hmac_secret", "private_key", "certificate", "api_key"):
        with pytest.raises(ValidationError):
            IdentityOverrideEnvelopeV1.model_validate({**override.canonical_dict(), forbidden: "x"})
    with pytest.raises(ValidationError):
        IdentityOverrideEnvelopeV1.model_validate(
            {k: v for k, v in override.canonical_dict().items() if k != "integrity"}
        )
    with pytest.raises(ValidationError):
        IdentityOverrideEnvelopeV1.model_validate(
            {**override.canonical_dict(), "resolution": "split_members"}
        )

    graph = IdentityGraphV1.model_validate(
        {
            "schema_version": "identity-graph.v1",
            "run_id": "run_t1",
            "built_at": "2026-07-29T18:00:05.000Z",
            "nodes": [
                _node(node_id=mid, source_id=mid.split(":")[-1]).model_dump(mode="json")
                for mid in sorted(members)
            ],
            "edges": [],
            "customers": [],
            "ambiguous_identities": [cluster.canonical_dict()],
        }
    )
    assert "ambiguous_identities" in graph.canonical_dict()
    assert graph.to_canonical_json().endswith(b"\n")
    empty = IdentityGraphV1.model_validate(
        {
            "schema_version": "identity-graph.v1",
            "run_id": "run_empty_amb",
            "built_at": "2026-07-29T18:00:05.000Z",
            "nodes": [_node().model_dump(mode="json")],
            "edges": [],
            "customers": [
                {"customer_token": "cust_alone", "member_node_ids": ["hubspot:contact:n1"]}
            ],
        }
    )
    assert empty.canonical_dict()["ambiguous_identities"] == []


@pytest.mark.parametrize(
    ("fixture_name", "reason"),
    [
        ("household_email.json", "household_email"),
        ("recycled_phone.json", "recycled_phone"),
        ("conflicting_external_id.json", "conflicting_stronger_identifier"),
        ("non_unique_email_three_parties.json", "household_email"),
        ("non_unique_phone_three_parties.json", "recycled_phone"),
        ("conflicting_stronger_identifiers.json", "conflicting_stronger_identifier"),
        (
            "transitive_external_id_conflict.json",
            "conflicting_stronger_identifier",
        ),
    ],
)
def test_t2_quarantine_matrix(fixture_name: str, reason: str):
    payload = _load_case(fixture_name)
    nodes = [IdentityNodeV1.model_validate(item) for item in payload["nodes"]]
    graph = build_identity_graph(nodes, run_id=f"run_{payload['case']}")
    assert graph.customers == []
    if fixture_name == "transitive_external_id_conflict.json":
        assert {edge.match_rule for edge in graph.edges} == {"same_source_object_id"}
    else:
        assert graph.edges == []
    assert len(graph.ambiguous_identities) == 1
    cluster = graph.ambiguous_identities[0]
    assert cluster.reason == reason
    assert set(cluster.member_node_ids) == {node.node_id for node in nodes}
    assert cluster.cluster_hash == ambiguous_cluster_hash(cluster.member_node_ids, reason)
    assert {node.node_id for node in graph.nodes} == {node.node_id for node in nodes}


def test_t2_quarantine_expands_over_stronger_identity_components():
    payload = _load_case("component_quarantine.json")
    nodes = [IdentityNodeV1.model_validate(item) for item in payload["nodes"]]
    graph = build_identity_graph(nodes, run_id="run_component_quarantine")
    expected_members = {node.node_id for node in nodes}

    assert graph.customers == []
    assert len(graph.ambiguous_identities) == 1
    cluster = graph.ambiguous_identities[0]
    assert cluster.reason == "household_email"
    assert set(cluster.member_node_ids) == expected_members
    assert {edge.match_rule for edge in graph.edges} == {
        "declared_external_id",
        "same_source_object_id",
    }
    assert all(
        node_id in expected_members
        for edge in graph.edges
        for node_id in (edge.left_node_id, edge.right_node_id)
    )
    assert public_identity_projection(graph).customer_count == 0


@pytest.mark.parametrize(
    ("fixture_name", "match_rule", "channel"),
    [
        (
            "strongly_joined_external_id_email.json",
            "declared_external_id",
            "email",
        ),
        (
            "strongly_joined_same_source_phone.json",
            "same_source_object_id",
            "phone",
        ),
    ],
)
def test_t2_three_records_in_one_strong_component_do_not_quarantine(
    fixture_name: str,
    match_rule: str,
    channel: str,
):
    payload = _load_case(fixture_name)
    nodes = [IdentityNodeV1.model_validate(item) for item in payload["nodes"]]
    graph = build_identity_graph(nodes, run_id=f"run_{payload['case']}")

    assert len(graph.customers) == 1
    assert graph.ambiguous_identities == []
    assert set(graph.customers[0].member_node_ids) == {node.node_id for node in nodes}
    assert {edge.match_rule for edge in graph.edges} == {match_rule}
    assert len({getattr(node, channel) for node in nodes}) == 1


def test_t2_blocked_cross_component_weaker_join_quarantines_both_components():
    payload = _load_case("cross_component_stronger_identifier_conflict.json")
    nodes = [IdentityNodeV1.model_validate(item) for item in payload["nodes"]]
    graph = build_identity_graph(
        nodes,
        run_id="run_cross_component_stronger_identifier_conflict",
    )
    expected_members = {node.node_id for node in nodes}

    assert graph.customers == []
    assert len(graph.ambiguous_identities) == 1
    cluster = graph.ambiguous_identities[0]
    assert cluster.reason == "conflicting_stronger_identifier"
    assert set(cluster.member_node_ids) == expected_members
    assert {edge.match_rule for edge in graph.edges} == {"declared_external_id"}
    assert not any(edge.match_rule == "unique_exact_email" for edge in graph.edges)
    assert public_identity_projection(graph).customer_count == 0


def test_t2_three_records_across_two_conflicting_components_quarantines_shared_email():
    payload = _load_case("three_records_two_components_conflicting_email.json")
    nodes = [IdentityNodeV1.model_validate(item) for item in payload["nodes"]]
    graph = build_identity_graph(
        nodes,
        run_id="run_three_records_two_components_conflicting_email",
    )

    assert graph.customers == []
    assert {edge.match_rule for edge in graph.edges} == {"declared_external_id"}
    assert len(graph.ambiguous_identities) == 1
    cluster = graph.ambiguous_identities[0]
    assert cluster.reason == "conflicting_stronger_identifier"
    assert set(cluster.member_node_ids) == {node.node_id for node in nodes}
    assert cluster.cluster_hash == ambiguous_cluster_hash(
        cluster.member_node_ids,
        "conflicting_stronger_identifier",
    )
    assert public_identity_projection(graph).customer_count == 0


@pytest.mark.parametrize(
    ("fixture_name", "reason"),
    [
        ("three_records_two_components_compatible_email.json", "household_email"),
        ("three_records_two_components_compatible_phone.json", "recycled_phone"),
    ],
)
def test_t2_three_records_across_two_compatible_components_quarantines_shared_channel(
    fixture_name: str,
    reason: str,
):
    payload = _load_case(fixture_name)
    nodes = [IdentityNodeV1.model_validate(item) for item in payload["nodes"]]
    graph = build_identity_graph(nodes, run_id=f"run_{payload['case']}")

    assert graph.customers == []
    assert {edge.match_rule for edge in graph.edges} == {"declared_external_id"}
    assert len(graph.ambiguous_identities) == 1
    cluster = graph.ambiguous_identities[0]
    assert cluster.reason == reason
    assert set(cluster.member_node_ids) == {node.node_id for node in nodes}
    assert public_identity_projection(graph).customer_count == 0


def test_t2_overlapping_quarantine_keys_union_without_resolution_leak():
    payload = _load_case("overlapping_channels.json")
    nodes = [IdentityNodeV1.model_validate(item) for item in payload["nodes"]]
    graph = build_identity_graph(nodes, run_id="run_overlapping_channels")

    assert graph.customers == []
    assert graph.edges == []
    assert len(graph.ambiguous_identities) == 1
    cluster = graph.ambiguous_identities[0]
    assert cluster.reason == "household_email"
    assert set(cluster.canonical_dict()) == {
        "member_node_ids",
        "reason",
        "cluster_hash",
    }
    assert set(cluster.member_node_ids) == {node.node_id for node in nodes}

    projection = public_identity_projection(graph)
    assert projection.customer_count == 0
    assert projection.customers == []
    public_text = projection.to_canonical_json().decode("utf-8")
    for node in nodes:
        assert node.node_id not in public_text


def test_t2_unaffected_exact_joins_and_same_name_negative():
    for filename, match_rule in [
        ("same_source_object_id.json", "same_source_object_id"),
        ("unique_exact_email.json", "unique_exact_email"),
        ("unique_exact_e164_phone.json", "unique_exact_e164_phone"),
    ]:
        payload = _load_case(filename)
        nodes = [IdentityNodeV1.model_validate(item) for item in payload["nodes"]]
        graph = build_identity_graph(nodes, run_id=f"run_{payload['case']}")
        assert len(graph.customers) == 1
        assert graph.ambiguous_identities == []
        assert any(edge.match_rule == match_rule for edge in graph.edges)

    same_name = _load_case("same_display_name_no_key.json")
    nodes = [IdentityNodeV1.model_validate(item) for item in same_name["nodes"]]
    graph = build_identity_graph(nodes, run_id="run_same_name")
    assert len(graph.customers) == len(nodes)
    assert graph.ambiguous_identities == []
    assert graph.edges == []

    thin, _projection = build_thin_slice_identity()
    assert len(thin.customers) == 1
    assert thin.ambiguous_identities == []


def test_t3_public_projection_resolved_only():
    household = _graph_for_fixture("household_email.json")
    projection = public_identity_projection(household)
    assert projection.customer_count == 0
    assert projection.customers == []
    text = projection.to_canonical_json().decode("utf-8")
    for node in household.nodes:
        assert node.node_id not in text
        if node.email:
            assert node.email not in text
        if node.phone:
            assert node.phone not in text
    for cluster in household.ambiguous_identities:
        assert cluster.cluster_hash not in text

    unique_nodes = [
        IdentityNodeV1.model_validate(item)
        for item in _load_case("unique_exact_email.json")["nodes"]
    ]
    household_nodes = [
        IdentityNodeV1.model_validate(item) for item in _load_case("household_email.json")["nodes"]
    ]
    mixed = build_identity_graph(
        [*unique_nodes, *household_nodes],
        run_id="run_mixed_resolved_ambiguous",
    )
    assert len(mixed.customers) == 1
    assert len(mixed.ambiguous_identities) == 1
    assert mixed.ambiguous_identities[0].reason == "household_email"
    quarantined = set(mixed.ambiguous_identities[0].member_node_ids)
    assert quarantined.isdisjoint(set(mixed.customers[0].member_node_ids))
    mixed_projection = public_identity_projection(mixed)
    assert mixed_projection.customer_count == 1
    assert len(mixed_projection.customers) == 1
    assert mixed_projection.customers[0].customer_token == mixed.customers[0].customer_token
    assert mixed_projection.customers[0].member_count == 2
    public_text = mixed_projection.to_canonical_json().decode("utf-8")
    for node_id in quarantined:
        assert node_id not in public_text


def test_t4_valid_override_resolves_only_target_cluster():
    household_nodes = [
        IdentityNodeV1.model_validate(item) for item in _load_case("household_email.json")["nodes"]
    ]
    phone_nodes = [
        IdentityNodeV1.model_validate(item) for item in _load_case("recycled_phone.json")["nodes"]
    ]
    graph = build_identity_graph(
        [*household_nodes, *phone_nodes],
        run_id="run_multi_ambiguous",
    )
    assert graph.customers == []
    assert len(graph.ambiguous_identities) == 2
    by_reason = {cluster.reason: cluster for cluster in graph.ambiguous_identities}
    assert set(by_reason) == {"household_email", "recycled_phone"}
    before = graph.to_canonical_json()

    override = parse_identity_override(
        (OVERRIDE_FIXTURES / "valid_merge_household_email.json").read_bytes()
    )
    assert override.target_cluster_hash == by_reason["household_email"].cluster_hash
    resolved = apply_identity_override(graph, override)

    assert graph.to_canonical_json() == before
    assert len(resolved.customers) == 1
    assert len(resolved.ambiguous_identities) == 1
    assert resolved.ambiguous_identities[0].reason == "recycled_phone"
    assert resolved.ambiguous_identities[0].cluster_hash == by_reason["recycled_phone"].cluster_hash
    customer = resolved.customers[0]
    assert set(customer.member_node_ids) == set(by_reason["household_email"].member_node_ids)
    assert customer.customer_token == customer_token_for(customer.member_node_ids)
    projection = public_identity_projection(resolved)
    assert projection.customer_count == 1
    assert projection.customers[0].customer_token == customer.customer_token


@pytest.mark.parametrize(
    "case",
    [
        "bad_integrity",
        "non_canonical",
        "wrong_hash",
        "wrong_members",
        "bad_resolution",
        "missing_integrity",
        "missing_cluster",
        "bad_schema",
    ],
)
def test_t5_invalid_override_fail_closed_unchanged_graph(case: str):
    household_nodes = [
        IdentityNodeV1.model_validate(item) for item in _load_case("household_email.json")["nodes"]
    ]
    phone_nodes = [
        IdentityNodeV1.model_validate(item) for item in _load_case("recycled_phone.json")["nodes"]
    ]
    graph = build_identity_graph(
        [*household_nodes, *phone_nodes],
        run_id="run_fail_closed",
    )
    before = graph.to_canonical_json()
    household = next(c for c in graph.ambiguous_identities if c.reason == "household_email")

    if case == "bad_integrity":
        raw = (OVERRIDE_FIXTURES / "invalid_bad_integrity.json").read_bytes()
        with pytest.raises((ValidationError, ValueError)):
            parse_identity_override(raw)
    elif case == "non_canonical":
        raw = (OVERRIDE_FIXTURES / "invalid_non_canonical.json").read_bytes()
        with pytest.raises(ValueError, match="canonical"):
            parse_identity_override(raw)
    elif case == "wrong_hash":
        override = parse_identity_override(
            (OVERRIDE_FIXTURES / "invalid_wrong_target_hash.json").read_bytes()
        )
        with pytest.raises(ValueError, match="absent"):
            apply_identity_override(graph, override)
    elif case == "wrong_members":
        override = parse_identity_override(
            (OVERRIDE_FIXTURES / "invalid_wrong_members.json").read_bytes()
        )
        with pytest.raises(ValueError, match="member_node_ids"):
            apply_identity_override(graph, override)
    elif case == "bad_resolution":
        raw = (OVERRIDE_FIXTURES / "invalid_bad_resolution.json").read_bytes()
        with pytest.raises(ValidationError):
            parse_identity_override(raw)
    elif case == "missing_integrity":
        raw = (OVERRIDE_FIXTURES / "invalid_missing_integrity.json").read_bytes()
        with pytest.raises(ValidationError):
            parse_identity_override(raw)
    elif case == "missing_cluster":
        override = make_identity_override(
            target_cluster_hash=household.cluster_hash,
            member_node_ids=household.member_node_ids,
            decided_by="fixture.operator",
            decided_at="2026-07-29T19:00:00.000Z",
        )
        empty = build_identity_graph([], run_id="run_no_clusters")
        empty_before = empty.to_canonical_json()
        with pytest.raises(ValueError, match="absent"):
            apply_identity_override(empty, override)
        assert empty.to_canonical_json() == empty_before
    elif case == "bad_schema":
        body = json.loads(
            (OVERRIDE_FIXTURES / "valid_merge_household_email.json").read_text(encoding="utf-8")
        )
        body["schema_version"] = "identity-override.v0"
        body["integrity"] = override_envelope_integrity(body)
        raw = (
            json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        with pytest.raises(ValidationError):
            parse_identity_override(raw)

    assert graph.to_canonical_json() == before


def test_t6_path_safety_and_public_identity_scans(tmp_path):
    root = tmp_path / "out"
    root.mkdir()
    before = _list_tree(root)
    graph = _graph_for_fixture("household_email.json", run_id="run_quarantine_public")
    projection = public_identity_projection(graph)
    assert projection.customer_count == 0

    rejects = [
        "../outside.json",
        "sub/..",
        "..",
        "a/../../outside.json",
        r"..\outside.json",
        "./../outside.json",
        "~/escaped.json",
        str(tmp_path / "sibling.json"),
        "/tmp/abs.json",
        "",
    ]
    for relative in rejects:
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

    private = write_identity_graph(root, "identity/identity-graph.json", graph)
    public = write_public_identity_projection(root, "identity/identity-public.json", projection)
    assert private.read_bytes() == graph.to_canonical_json()
    assert public.read_bytes() == projection.to_canonical_json()
    public_text = public.read_text(encoding="utf-8")
    assert "email" not in public_text
    assert "phone" not in public_text
    assert "external_ids" not in public_text
    assert "source_id" not in public_text
    for node in graph.nodes:
        assert node.source_id not in public_text
        if node.email:
            assert node.email not in public_text


def test_t7_thin_slice_and_missing_value_regressions():
    from found_money.io import public_report
    from found_money.mapping import map_row

    graph, projection = build_thin_slice_identity()
    assert len(graph.customers) == 1
    assert graph.ambiguous_identities == []
    assert projection.customer_count == 1

    # Missing source value stays unquantified; obsolete 100-unit default remains absent.
    report = public_report(
        [
            map_row(
                {
                    "id": "x",
                    "email": "x@example.invalid",
                    "trial_end_days": 2,
                    "subscribed": True,
                    "consent_email": True,
                    "value": None,
                }
            )
        ]
    )
    value = report["queue"][0]["opportunity_value"]
    assert value["basis"] == "unquantified: no observed source value"
    assert (value["low"], value["high"]) == (0, 0)
    assert "default value of 100" not in json.dumps(report).lower()


def test_t8_socket_and_static_scope_safety(monkeypatch):
    def boom(*_args, **_kwargs):
        raise AssertionError("socket connection attempted")

    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "socket", boom)

    graph = _graph_for_fixture("household_email.json")
    override = parse_identity_override(
        (OVERRIDE_FIXTURES / "valid_merge_household_email.json").read_bytes()
    )
    resolved = apply_identity_override(graph, override)
    public_identity_projection(resolved)
    build_thin_slice_identity()

    identity_imports = _module_imports(IDENTITY_MODULE)
    contract_imports = _module_imports(IDENTITY_CONTRACT)
    networkish = {"requests", "urllib", "httpx", "aiohttp", "socket", "ssl"}
    assert not (identity_imports & networkish)
    assert not (contract_imports & networkish)

    forbidden_modules = {
        "found_money.connectors",
        "found_money.events",
        "found_money.value",
        "found_money.map",
        "found_money.strategy",
        "found_money.rendering",
    }
    assert not (identity_imports & forbidden_modules)
    assert not (contract_imports & forbidden_modules)

    identity_text = IDENTITY_MODULE.read_text(encoding="utf-8").lower()
    contract_text = IDENTITY_CONTRACT.read_text(encoding="utf-8").lower()
    for blob in (identity_text, contract_text):
        assert "hmac" not in blob
        assert "private_key" not in blob
        assert "public_key" not in blob
        assert "cryptography" not in blob
        assert "api_key" not in blob
        assert "agent-ready" not in blob
