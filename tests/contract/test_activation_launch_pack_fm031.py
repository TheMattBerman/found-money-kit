"""FM-031 activation launch-pack contracts, adversarial guards, and AC proofs."""

from __future__ import annotations

import ast
import json
import socket
from datetime import datetime, timezone
from pathlib import Path

import pytest

from found_money.activation import (
    LaunchPackInputs,
    LaunchPackValidationError,
    build_launch_pack,
    public_safe_launch_pack_projection,
    validate_launch_pack_payloads,
    validate_launch_pack_tree,
    validate_private_launch_pack_payloads,
    write_launch_pack,
)
from found_money.contracts.activation import (
    PAYMENT_RESCUE_SEGMENT_ID,
    ActivationPolicyV1,
    ActivationSourceObjectRuleV1,
    MemberValueBasisV1,
    canonical_email_task_activation_policy,
)
from found_money.contracts.events import ExclusionLedgerV1, ExclusionRecordV1
from found_money.contracts.strategy import (
    RecoveryPlaySetV1,
    RecoveryPlayV1,
    StrategyBusinessProfileV1,
)
import found_money.contracts as contracts_pkg
from found_money.events import detect_failed_payments
from found_money.identity import (
    apply_identity_override,
    build_identity_graph,
    load_thin_slice_snapshots,
    normalize_source_records,
    parse_identity_override,
)
from found_money.map import build_thin_slice_money_map
from found_money.redaction import assert_public_safe
from found_money.safety import (
    prove_writer_rejects_escapes,
    scan_output_tree,
    scan_package_capabilities,
)
from found_money.strategy import (
    apply_recovery_plays_to_money_map,
    build_canonical_saas_recovery_strategy,
    canonical_saas_business_profile,
)
from found_money.value import build_contribution_ledger
import found_money.build as build_module

ROOT = Path(__file__).resolve().parents[2]
IDENTITY_FIXTURES = ROOT / "tests" / "fixtures" / "saas" / "identity"
OVERRIDE_FIXTURES = IDENTITY_FIXTURES / "overrides"
WHEN = datetime(2026, 7, 29, 18, 0, tzinfo=timezone.utc)


def _enriched_inputs(
    *,
    profile=None,
    graph=None,
    snapshots=None,
    exclusions=None,
    mode="private",
    activation_policy=None,
    identity_override=None,
    candidates_override=None,
    ledger_override=None,
):
    money_map = build_thin_slice_money_map(run_id="run_fm031_launch")
    strategy = build_canonical_saas_recovery_strategy(money_map)
    primary = sorted(strategy.recovery_plays.plays, key=lambda item: item.rank)[0]
    render_play_set = RecoveryPlaySetV1(
        run_id=money_map.run_id,
        built_at=strategy.recovery_plays.built_at,
        provider="fixture",
        plays=[
            RecoveryPlayV1(
                play_id=primary.play_id,
                pile_id=primary.pile_id,
                rank=1,
                title=primary.title,
                rationale=primary.rationale,
                recommended_actions=primary.recommended_actions,
            )
        ],
    )
    enriched = apply_recovery_plays_to_money_map(money_map, render_play_set)
    snaps = snapshots if snapshots is not None else load_thin_slice_snapshots()
    identity = (
        graph
        if graph is not None
        else build_identity_graph(normalize_source_records(snaps), run_id=money_map.run_id)
    )
    candidates = detect_failed_payments(snaps["stripe"], identity, run_id=money_map.run_id)
    ledger = build_contribution_ledger(candidates, snaps["stripe"])
    return LaunchPackInputs(
        money_map=enriched,
        recovery_plays=strategy.recovery_plays,
        evidence_packet=strategy.packet,
        contribution_ledger=ledger_override or ledger,
        identity_graph=identity,
        candidates=candidates_override or candidates,
        exclusions=exclusions
        or ExclusionLedgerV1(run_id=money_map.run_id, built_at=WHEN, exclusions=[]),
        business_profile=profile if profile is not None else canonical_saas_business_profile(),
        source_snapshots=snaps,
        mode=mode,
        activation_policy=activation_policy,
        identity_override=identity_override,
    )


def test_ac1_launch_pack_contains_required_classes_hashes_and_links(tmp_path):
    pack = build_launch_pack(_enriched_inputs(mode="private"))
    validate_launch_pack_payloads(pack.payloads)
    written = write_launch_pack(tmp_path, pack)
    assert (tmp_path / "launch-pack" / "manifest.json") in written.values()
    manifest = validate_launch_pack_tree(tmp_path)
    assert manifest.status == "approval_ready"
    assert manifest.approval_only is True
    assert manifest.export_only is True
    assert manifest.not_activated is True
    assert manifest.send_performed is False
    assert manifest.schedule_performed is False
    assert manifest.audience_created is False
    assert manifest.provider_write_performed is False
    classes = {entry.asset_class for entry in manifest.files}
    for required in (
        "money_map",
        "play_pages",
        "private_segments",
        "public_segments",
        "complete_copy",
        "calendar",
        "offer_landing_briefs",
        "tracking_plan",
        "launch_checklist",
        "creative_handoff",
        "production_brief",
        "withheld_ledger",
    ):
        assert required in classes
    assert manifest.withheld_assets == []
    assert "manifest.json" not in {entry.path for entry in manifest.files}
    combined = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (tmp_path / "launch-pack").rglob("*")
        if path.is_file() and path.suffix in {".md", ".html", ".json"}
    )
    assert "generic play" not in combined.casefold()
    assert "placeholder" not in combined.casefold()


def test_item1_mode_public_omits_private_private_keeps_segments(tmp_path):
    public_pack = build_launch_pack(_enriched_inputs(mode="public"))
    assert public_pack.mode == "public"
    assert public_pack.manifest.mode == "public"
    assert not any("/private/" in path for path in public_pack.payloads)
    write_launch_pack(tmp_path / "public", public_pack)
    assert not (tmp_path / "public" / "launch-pack" / "private").exists()
    validate_launch_pack_payloads(public_pack.payloads)

    private_pack = build_launch_pack(_enriched_inputs(mode="private"))
    assert any(path.startswith("launch-pack/private/") for path in private_pack.payloads)
    write_launch_pack(tmp_path / "private", private_pack)
    assert (tmp_path / "private" / "launch-pack" / "private" / "segments").is_dir()


def test_item1_public_scan_include_private_true_catches_named_private_leak(tmp_path):
    pack = build_launch_pack(_enriched_inputs(mode="public"))
    write_launch_pack(tmp_path, pack)
    leak_dir = tmp_path / "launch-pack" / "private"
    leak_dir.mkdir(parents=True)
    (leak_dir / "leak.json").write_text(
        json.dumps({"customer_token": "cust_leak", "email": "a@b.com"}),
        encoding="utf-8",
    )
    # Public trees must not skip paths named private.
    violations = scan_output_tree(tmp_path, include_private=True)
    assert violations
    with pytest.raises(LaunchPackValidationError, match="private"):
        validate_launch_pack_tree(tmp_path)


def test_item2_one_stable_segment_no_customer_duplication():
    pack = build_launch_pack(_enriched_inputs(mode="private"))
    assert pack.manifest.segment_ids == [PAYMENT_RESCUE_SEGMENT_ID]
    assert len(pack.private_segments) == 1
    private = pack.private_segments[PAYMENT_RESCUE_SEGMENT_ID]
    assert set(private.play_ids) == set(pack.manifest.play_ids)
    assert len(private.play_ids) == 3
    tokens = [member.customer_token for member in private.members]
    assert tokens
    assert len(tokens) == len(set(tokens))
    for member in private.members:
        assert set(member.play_ids) == set(private.play_ids)


def test_item3_excluded_and_ambiguous_absent_counts_in_withheld_ledger():
    from found_money.contracts.identity import IdentityNodeV1

    snaps = load_thin_slice_snapshots()
    thin = build_identity_graph(normalize_source_records(snaps), run_id="run_fm031_ex")
    household = [
        IdentityNodeV1.model_validate(item)
        for item in json.loads((IDENTITY_FIXTURES / "household_email.json").read_text())["nodes"]
    ]
    graph = build_identity_graph([*thin.nodes, *household], run_id="run_fm031_ex")
    eligible = thin.customers[0].customer_token
    exclusions = ExclusionLedgerV1(
        run_id="run_fm031_ex",
        built_at=WHEN,
        exclusions=[
            ExclusionRecordV1(
                run_id="run_fm031_ex",
                candidate_key="failed_payment:stripe_invoice:inv_failed_001",
                event_family="failed_payment",
                economic_unit_key="stripe_invoice:inv_failed_001",
                customer_token=eligible,
                reason_code="later_payment",
                priority=30,
                lineage={"source": "fixture"},
                disqualifying_evidence={"note": "test"},
            )
        ],
    )
    pack = build_launch_pack(
        _enriched_inputs(graph=graph, snapshots=snaps, exclusions=exclusions, mode="private")
    )
    private = pack.private_segments.get(PAYMENT_RESCUE_SEGMENT_ID)
    if private is not None:
        assert all(member.customer_token != eligible for member in private.members)
        for member in private.members:
            assert member.exclusion_status == "included"
    public = pack.public_segments.segments[0]
    assert public.withheld_exclusion_count >= 1 or public.withheld_ambiguous_count >= 1
    assert any(asset.asset_class == "exclusion_aggregate" for asset in pack.withheld.assets)


def test_item4_activation_policy_channel_minimization_and_field_allowlist():
    policy = canonical_email_task_activation_policy()
    assert policy.personalization_field_allowlist == []
    pack = build_launch_pack(_enriched_inputs(mode="private", activation_policy=policy))
    private = pack.private_segments[PAYMENT_RESCUE_SEGMENT_ID]
    for member in private.members:
        assert member.personalization_allowlist == []
        systems = {(row.source_system, row.object_type) for row in member.source_ids}
        assert systems == {("hubspot", "contact")}
        assert all(row.source_system != "stripe" for row in member.source_ids)

    missing_platform = ActivationPolicyV1(
        channels=["email", "task"],
        required_source_objects=[
            ActivationSourceObjectRuleV1(source_system="hubspot", object_type="company")
        ],
        allowed_source_objects=[
            ActivationSourceObjectRuleV1(source_system="hubspot", object_type="company")
        ],
        personalization_field_allowlist=[],
    )
    withheld = build_launch_pack(
        _enriched_inputs(mode="private", activation_policy=missing_platform)
    )
    assert PAYMENT_RESCUE_SEGMENT_ID not in withheld.private_segments
    assert any("missing_required_platform_id" in asset.reason for asset in withheld.withheld.assets)

    with pytest.raises(ValueError, match="channel"):
        ActivationPolicyV1(
            channels=["email"],
            required_source_objects=[
                ActivationSourceObjectRuleV1(source_system="hubspot", object_type="contact")
            ],
            allowed_source_objects=[
                ActivationSourceObjectRuleV1(source_system="hubspot", object_type="contact")
            ],
            personalization_field_allowlist=["email"],
        )


def test_item5_typed_value_basis_never_synthesizes_zero():
    pack = build_launch_pack(_enriched_inputs(mode="private"))
    private = pack.private_segments[PAYMENT_RESCUE_SEGMENT_ID]
    for member in private.members:
        assert isinstance(member.value_basis, MemberValueBasisV1)
        if member.value_basis.basis == "unquantified":
            assert member.value_basis.amount_minor is None
            assert member.value_basis.currency is None
        else:
            assert member.value_basis.currency
            assert member.value_basis.amount_minor is not None
            assert member.value_basis.amount_minor != "0"
    with pytest.raises(ValueError, match="zero"):
        MemberValueBasisV1(basis="observed_face_value", currency="usd", amount_minor="0")


def test_item6_manifest_claims_hash_closure_and_rejects_tamper(tmp_path):
    pack = build_launch_pack(_enriched_inputs(mode="private"))
    manifest = pack.manifest
    assert manifest.mode == "private"
    assert manifest.approval_only is True
    assert manifest.export_only is True
    assert manifest.not_activated is True
    assert manifest.send_performed is False
    assert manifest.schedule_performed is False
    assert manifest.audience_created is False
    assert manifest.provider_write_performed is False
    assert "manifest.json" not in {entry.path for entry in manifest.files}
    write_launch_pack(tmp_path, pack)
    validate_launch_pack_tree(tmp_path)

    orphan = tmp_path / "launch-pack" / "orphan.txt"
    orphan.write_text("orphan", encoding="utf-8")
    with pytest.raises(LaunchPackValidationError, match="unindexed"):
        validate_launch_pack_tree(tmp_path)
    orphan.unlink()

    target = tmp_path / "launch-pack" / "README.md"
    target.write_text(target.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(LaunchPackValidationError, match="hash mismatch"):
        validate_launch_pack_tree(tmp_path)


def test_item6_creative_handoff_binds_canonical_value_hash():
    pack = build_launch_pack(_enriched_inputs(mode="public"))
    for path, data in pack.payloads.items():
        if not path.startswith("launch-pack/creative-handoff/"):
            continue
        handoff = json.loads(data)
        assert handoff["value_ledger_sha256"] == pack.manifest.value_ledger_sha256

    tampered = dict(pack.payloads)
    handoff_path = next(
        path for path in tampered if path.startswith("launch-pack/creative-handoff/")
    )
    handoff = json.loads(tampered[handoff_path])
    handoff["value_ledger_sha256"] = "0" * 64
    tampered[handoff_path] = (
        json.dumps(handoff, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    manifest = json.loads(tampered["launch-pack/manifest.json"])
    for entry in manifest["files"]:
        if f"launch-pack/{entry['path']}" == handoff_path:
            entry["sha256"] = __import__("hashlib").sha256(tampered[handoff_path]).hexdigest()
    from found_money.contracts.activation import LaunchPackManifestV1

    tampered["launch-pack/manifest.json"] = LaunchPackManifestV1.model_validate(
        manifest
    ).to_canonical_json()
    with pytest.raises(LaunchPackValidationError, match="value hash"):
        validate_launch_pack_payloads(tampered)


def test_item6_rejects_duplicate_and_noncanonical_paths():
    pack = build_launch_pack(_enriched_inputs(mode="public"))
    payloads = dict(pack.payloads)
    payloads["launch-pack/./README.md"] = payloads["launch-pack/README.md"]
    with pytest.raises(LaunchPackValidationError, match="noncanonical|unindexed"):
        validate_launch_pack_payloads(payloads)


def test_item7_missing_inputs_omit_assets_restoring_clears_withholding():
    empty = StrategyBusinessProfileV1()
    withheld = build_launch_pack(_enriched_inputs(profile=empty, mode="public"))
    assert withheld.status in {"partial", "withheld"}
    assert withheld.withheld_asset_ids
    # Independent assets remain.
    assert "launch-pack/money-map.json" in withheld.payloads
    assert "launch-pack/public/segments.json" in withheld.payloads
    assert any(path.startswith("launch-pack/plays/") for path in withheld.payloads)
    assert not any(path.startswith("launch-pack/copy/") for path in withheld.payloads)
    assert not any(path.startswith("launch-pack/calendar/") for path in withheld.payloads)
    assert not any(path.startswith("launch-pack/creative-handoff/") for path in withheld.payloads)
    assert not any(path.startswith("launch-pack/production-brief/") for path in withheld.payloads)
    # No placeholder stub bodies.
    for path, data in withheld.payloads.items():
        if path.endswith(".md"):
            assert "# Withheld:" not in data.decode("utf-8")

    restored = build_launch_pack(_enriched_inputs(mode="public"))
    assert restored.status == "approval_ready"
    assert restored.withheld_asset_ids == ()
    assert any(path.endswith("-copy.md") for path in restored.payloads)


def test_item8_fail_closed_links(tmp_path):
    from found_money.activation.validate import _validate_relative_links

    indexed = {"manifest.json": object(), "copy/ok.md": object()}
    payloads = {
        "launch-pack/manifest.json": b"{}",
        "launch-pack/copy/ok.md": b"# ok\n",
    }

    def _expect(relative: str, text: str, match: str) -> None:
        with pytest.raises(LaunchPackValidationError, match=match):
            _validate_relative_links(relative, text, indexed, payloads)

    _expect("plays/x.html", '<a href="https://example.com/x">x</a>', "disallowed|absolute")
    _expect("plays/x.html", '<a href="http://example.com/x">x</a>', "disallowed|absolute")
    _expect("plays/x.html", '<a href="mailto:a@b.com">x</a>', "disallowed|absolute")
    _expect("plays/x.html", '<a href="file:///etc/passwd">x</a>', "disallowed|absolute")
    _expect("plays/x.html", '<a href="/etc/passwd">x</a>', "absolute")
    _expect("plays/x.html", '<a href="../manifest.json?x=1">x</a>', "query")
    _expect("plays/x.html", '<a href="%2e%2e/manifest.json">x</a>', "percent-encoded")
    _expect("plays/x.html", '<a href="../../outside.md">x</a>', "escape")
    _expect("plays/x.html", '<a href="missing-file.md">x</a>', "broken|missing")
    _expect("plays/x.html", '<a href="#missing-anchor">x</a>', "bad fragment")
    _expect("plays/x.md", "[x](copy/)", "broken|missing|absolute")

    pack = build_launch_pack(_enriched_inputs(mode="public"))
    write_launch_pack(tmp_path, pack)
    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    link = tmp_path / "launch-pack" / "escape-link"
    link.symlink_to(outside)
    with pytest.raises(LaunchPackValidationError, match="symlink|unindexed"):
        validate_launch_pack_tree(tmp_path)


def test_item9_public_aggregates_have_no_identity_and_no_token_example():
    pack = build_launch_pack(_enriched_inputs(mode="public"))
    public = pack.public_segments.canonical_dict()
    text = json.dumps(public)
    for forbidden in (
        "customer_token",
        "source_id",
        "members",
        "hs_contact_synth_001",
        "cus_synth_001",
        "@",
        "rows",
    ):
        assert forbidden not in text
    projection = public_safe_launch_pack_projection(pack)
    assert "public_token_example" not in projection
    assert_public_safe(
        {
            "run_id": projection["run_id"],
            "status": projection["status"],
            "play_ids": projection["play_ids"],
            "segment_ids": projection["segment_ids"],
        }
    )


def test_item10_override_requires_explicit_binding_one_cluster():
    from found_money.contracts.identity import IdentityNodeV1
    from found_money.identity import make_identity_override

    snaps = load_thin_slice_snapshots()
    thin = build_identity_graph(normalize_source_records(snaps), run_id="run_fm031_ovr")
    household_nodes = [
        IdentityNodeV1.model_validate(item)
        for item in json.loads((IDENTITY_FIXTURES / "household_email.json").read_text())["nodes"]
    ]
    phone_nodes = [
        IdentityNodeV1.model_validate(item)
        for item in json.loads((IDENTITY_FIXTURES / "recycled_phone.json").read_text())["nodes"]
    ]
    multi = build_identity_graph(
        [*thin.nodes, *household_nodes, *phone_nodes],
        run_id="run_fm031_ovr",
    )
    household = next(c for c in multi.ambiguous_identities if c.reason == "household_email")
    override = make_identity_override(
        target_cluster_hash=household.cluster_hash,
        member_node_ids=list(household.member_node_ids),
        decided_by="fixture.operator",
        decided_at=WHEN,
    )
    resolved = apply_identity_override(multi, override)
    assert len(resolved.ambiguous_identities) == 1
    assert resolved.ambiguous_identities[0].reason == "recycled_phone"

    with pytest.raises(ValueError, match="resolved target"):
        build_launch_pack(
            _enriched_inputs(
                graph=multi,
                snapshots=snaps,
                mode="private",
                identity_override=override,
            )
        )

    # Resolved graph alone must not fabricate override proof.
    bare = build_launch_pack(_enriched_inputs(graph=resolved, snapshots=snaps, mode="private"))
    assert bare.private_segments[PAYMENT_RESCUE_SEGMENT_ID].identity_override is None

    bound = build_launch_pack(
        _enriched_inputs(
            graph=resolved,
            snapshots=snaps,
            mode="private",
            identity_override=override,
        )
    )
    binding = bound.private_segments[PAYMENT_RESCUE_SEGMENT_ID].identity_override
    assert binding is not None
    assert binding.target_cluster_hash == override.target_cluster_hash
    assert binding.integrity == override.integrity
    assert set(binding.member_node_ids) == set(override.member_node_ids)

    # A candidate for the resolved target becomes eligible, while the unrelated
    # recycled-phone cluster remains quarantined and contributes no source IDs.
    target_token = next(
        customer.customer_token
        for customer in resolved.customers
        if set(customer.member_node_ids) == set(override.member_node_ids)
    )
    base = _enriched_inputs(mode="private")
    target_candidate = base.candidates.candidates[0].model_copy(
        update={"customer_token": target_token}
    )
    target_candidates = base.candidates.model_copy(update={"candidates": [target_candidate]})
    target_contribution = base.contribution_ledger.contributions[0].model_copy(
        update={"customer_token": target_token}
    )
    target_ledger = base.contribution_ledger.model_copy(
        update={"contributions": [target_contribution]}
    )
    admitted = build_launch_pack(
        _enriched_inputs(
            graph=resolved,
            snapshots=snaps,
            mode="private",
            identity_override=override,
            candidates_override=target_candidates,
            ledger_override=target_ledger,
        )
    )
    admitted_members = admitted.private_segments[PAYMENT_RESCUE_SEGMENT_ID].members
    assert [member.customer_token for member in admitted_members] == [target_token]
    exported_ids = {source.source_id for member in admitted_members for source in member.source_ids}
    unrelated_ids = {
        node.source_id
        for node in resolved.nodes
        if node.node_id in resolved.ambiguous_identities[0].member_node_ids
    }
    assert exported_ids.isdisjoint(unrelated_ids)

    parsed = parse_identity_override(
        (OVERRIDE_FIXTURES / "valid_merge_household_email.json").read_bytes()
    )
    assert parsed.target_cluster_hash


def test_item11_private_validation_separate_public_validator_unweakened():
    private_pack = build_launch_pack(_enriched_inputs(mode="private"))
    validate_private_launch_pack_payloads(private_pack.payloads)
    private_bytes = next(
        data
        for path, data in private_pack.payloads.items()
        if path.startswith("launch-pack/private/") and path.endswith(".json")
    )
    with pytest.raises(build_module.BuildConfigError):
        build_module.validate_public_artifact_payloads(
            {"launch-pack/private/segments/x.json": private_bytes}
        )
    public_pack = build_launch_pack(_enriched_inputs(mode="public"))
    build_module.validate_public_artifact_payloads(public_pack.payloads)


def test_item12_contracts_export_activation_types():
    assert hasattr(contracts_pkg, "ActivationPolicyV1")
    assert hasattr(contracts_pkg, "LaunchPackManifestV1")
    assert hasattr(contracts_pkg, "PrivateSegmentMemberV1")
    assert hasattr(contracts_pkg, "canonical_email_task_activation_policy")
    assert contracts_pkg.PAYMENT_RESCUE_SEGMENT_ID == PAYMENT_RESCUE_SEGMENT_ID


def test_item13_generated_html_is_escaped():
    pack = build_launch_pack(_enriched_inputs(mode="public"))
    html_payloads = [
        data.decode("utf-8") for path, data in pack.payloads.items() if path.endswith(".html")
    ]
    assert html_payloads
    # Fixture copy is plain, but generator must use escaped attribute writes.
    import found_money.activation.pack as pack_mod

    source = Path(pack_mod.__file__).read_text(encoding="utf-8")
    assert "html.escape" in source


def test_item14_determinism_malformed_schema_cross_mutation_no_network():
    first = build_launch_pack(_enriched_inputs(mode="private"))
    second = build_launch_pack(_enriched_inputs(mode="private"))
    assert first.payloads == second.payloads

    bad = dict(first.payloads)
    bad["launch-pack/manifest.json"] = b'{"schema_version":"nope"}\n'
    with pytest.raises(Exception):
        validate_launch_pack_payloads(bad)

    mutated = dict(first.payloads)
    manifest = json.loads(mutated["launch-pack/manifest.json"])
    manifest["money_map_sha256"] = "0" * 64
    mutated["launch-pack/manifest.json"] = (
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    with pytest.raises(LaunchPackValidationError):
        validate_launch_pack_payloads(mutated)

    cross = dict(first.payloads)
    private_path = next(
        path for path in cross if path.endswith(f"{PAYMENT_RESCUE_SEGMENT_ID}.json")
    )
    private = json.loads(cross[private_path])
    private["play_ids"] = ["not-a-real-play"]
    cross[private_path] = (
        json.dumps(private, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    # Fix hash so path check reaches cross-ID validation.
    digest = __import__("hashlib").sha256(cross[private_path]).hexdigest()
    manifest = json.loads(cross["launch-pack/manifest.json"])
    for entry in manifest["files"]:
        if entry["path"].endswith(f"{PAYMENT_RESCUE_SEGMENT_ID}.json"):
            entry["sha256"] = digest
    from found_money.contracts.activation import LaunchPackManifestV1

    cross["launch-pack/manifest.json"] = LaunchPackManifestV1.model_validate(
        manifest
    ).to_canonical_json()
    with pytest.raises((LaunchPackValidationError, Exception)):
        validate_launch_pack_payloads(cross)

    activation_root = ROOT / "found_money" / "activation"
    for path in activation_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr.casefold() not in {
                    "send_message",
                    "sendmail",
                    "create_audience",
                    "schedule_message",
                }
    assert scan_package_capabilities(ROOT) == []


@pytest.mark.parametrize(
    ("path_predicate", "old_value", "new_value"),
    [
        (lambda path: path.endswith(".html"), None, "evil-play"),
        (lambda path: "/copy/" in path, PAYMENT_RESCUE_SEGMENT_ID, "seg_evil"),
        (
            lambda path: path.endswith("checklist/launch-checklist.md"),
            None,
            "evil-play",
        ),
        (
            lambda path: path.endswith("public/segments.csv"),
            PAYMENT_RESCUE_SEGMENT_ID,
            "seg_evil",
        ),
    ],
)
def test_cross_id_validation_rejects_rehashed_html_markdown_and_csv_tamper(
    path_predicate, old_value, new_value
):
    pack = build_launch_pack(_enriched_inputs(mode="public"))
    tampered = dict(pack.payloads)
    path = next(path for path in tampered if path_predicate(path))
    text = tampered[path].decode()
    if old_value is None:
        old_value = pack.manifest.play_ids[0]
    assert old_value in text
    tampered[path] = text.replace(old_value, new_value, 1).encode()
    manifest = json.loads(tampered["launch-pack/manifest.json"])
    for entry in manifest["files"]:
        if f"launch-pack/{entry['path']}" == path:
            entry["sha256"] = __import__("hashlib").sha256(tampered[path]).hexdigest()
    from found_money.contracts.activation import LaunchPackManifestV1

    tampered["launch-pack/manifest.json"] = LaunchPackManifestV1.model_validate(
        manifest
    ).to_canonical_json()
    with pytest.raises(LaunchPackValidationError, match="unknown|indexed"):
        validate_launch_pack_payloads(tampered)


@pytest.mark.parametrize("suffix", [".md", ".html"])
def test_play_page_rejects_rehashed_untrusted_alternative_id(suffix):
    pack = build_launch_pack(_enriched_inputs(mode="public"))
    tampered = dict(pack.payloads)
    primary = pack.manifest.play_ids[0]
    alternative = next(play_id for play_id in pack.manifest.play_ids if play_id != primary)
    path = next(path for path in tampered if path.endswith(f"plays/{primary}{suffix}"))
    text = tampered[path].decode()
    assert "Alternative Play ID: " in text
    assert alternative in text
    tampered[path] = text.replace(alternative, "evil-play", 1).encode()
    manifest = json.loads(tampered["launch-pack/manifest.json"])
    for entry in manifest["files"]:
        if f"launch-pack/{entry['path']}" == path:
            entry["sha256"] = __import__("hashlib").sha256(tampered[path]).hexdigest()
    from found_money.contracts.activation import LaunchPackManifestV1

    tampered["launch-pack/manifest.json"] = LaunchPackManifestV1.model_validate(
        manifest
    ).to_canonical_json()
    with pytest.raises(LaunchPackValidationError, match="unknown|complete"):
        validate_launch_pack_payloads(tampered)


def test_item14_unrelated_value_pile_never_populates_payment_rescue_value():
    base = _enriched_inputs(mode="private")
    unrelated = base.contribution_ledger.contributions[0].model_copy(
        update={"pile_id": "canceled_customer"}
    )
    ledger = base.contribution_ledger.model_copy(update={"contributions": [unrelated]})
    pack = build_launch_pack(_enriched_inputs(mode="private", ledger_override=ledger))
    member = pack.private_segments[PAYMENT_RESCUE_SEGMENT_ID].members[0]
    assert member.value_basis.basis == "unquantified"
    assert member.value_basis.amount_minor is None


def test_writer_rejects_stale_private_files_before_public_rewrite(tmp_path):
    private = build_launch_pack(_enriched_inputs(mode="private"))
    write_launch_pack(tmp_path, private)
    before = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    public = build_launch_pack(_enriched_inputs(mode="public"))
    with pytest.raises(ValueError, match="stale"):
        write_launch_pack(tmp_path, public)
    after = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_ac3_ambiguous_members_quarantined_until_valid_override():
    from found_money.contracts.identity import IdentityNodeV1
    from found_money.identity import customer_token_for, make_identity_override

    def _load_case(name: str) -> dict:
        return json.loads((IDENTITY_FIXTURES / name).read_text(encoding="utf-8"))

    unique_nodes = [
        IdentityNodeV1.model_validate(item)
        for item in _load_case("unique_exact_email.json")["nodes"]
    ]
    household_nodes = [
        IdentityNodeV1.model_validate(item) for item in _load_case("household_email.json")["nodes"]
    ]
    snaps = load_thin_slice_snapshots()
    thin_graph = build_identity_graph(normalize_source_records(snaps), run_id="run_fm031_thin")
    graph = build_identity_graph(
        [*thin_graph.nodes, *household_nodes],
        run_id="run_fm031_ambiguous",
    )
    quarantined = set(graph.ambiguous_identities[0].member_node_ids)
    pack = build_launch_pack(_enriched_inputs(graph=graph, snapshots=snaps, mode="private"))
    for private in pack.private_segments.values():
        for member in private.members:
            member_nodes = {
                f"{row.source_system}:{row.object_type}:{row.source_id}"
                for row in member.source_ids
            }
            assert not (member_nodes & quarantined)

    override = parse_identity_override(
        (OVERRIDE_FIXTURES / "valid_merge_household_email.json").read_bytes()
    )
    household_only = build_identity_graph(household_nodes, run_id="run_fm031_household")
    assert override.target_cluster_hash == household_only.ambiguous_identities[0].cluster_hash
    resolved = apply_identity_override(household_only, override)
    assert resolved.ambiguous_identities == []
    assert resolved.customers[0].customer_token == customer_token_for(sorted(quarantined))

    phone_nodes = [
        IdentityNodeV1.model_validate(item) for item in _load_case("recycled_phone.json")["nodes"]
    ]
    multi = build_identity_graph([*household_nodes, *phone_nodes], run_id="run_fm031_multi")
    by_reason = {cluster.reason: cluster for cluster in multi.ambiguous_identities}
    targeted = make_identity_override(
        target_cluster_hash=by_reason["household_email"].cluster_hash,
        member_node_ids=list(by_reason["household_email"].member_node_ids),
        decided_by="fixture.operator",
        decided_at=WHEN,
    )
    only_one = apply_identity_override(multi, targeted)
    assert len(only_one.ambiguous_identities) == 1
    assert only_one.ambiguous_identities[0].reason == "recycled_phone"
    assert unique_nodes


def test_ac5_manifest_schema_pii_path_and_no_write_guards(tmp_path):
    pack = build_launch_pack(_enriched_inputs(mode="public"))
    write_launch_pack(tmp_path, pack)
    validate_launch_pack_tree(tmp_path)
    violations = scan_output_tree(tmp_path, include_private=True)
    assert violations == []
    prove_writer_rejects_escapes(tmp_path / "escape-root")


def test_ac6_approval_only_export_cannot_activate(tmp_path, monkeypatch):
    pack = build_launch_pack(_enriched_inputs(mode="public"))
    write_launch_pack(tmp_path, pack)
    manifest = json.loads((tmp_path / "launch-pack" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["approval_only"] is True
    assert manifest["export_only"] is True
    assert manifest["not_activated"] is True
    assert manifest["send_performed"] is False
    assert manifest["audience_created"] is False
    assert manifest["provider_write_performed"] is False
    readme = (tmp_path / "launch-pack" / "README.md").read_text(encoding="utf-8").casefold()
    assert "approval-only" in readme
    assert "does not send" in readme or "do not send" in readme

    def boom(*_a, **_k):
        raise AssertionError("network attempted")

    monkeypatch.setattr(socket, "create_connection", boom)
    build_launch_pack(_enriched_inputs(mode="public"))
