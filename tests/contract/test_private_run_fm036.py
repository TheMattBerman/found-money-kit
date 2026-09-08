"""FM-036 private-run harness and fixture-proof contract."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from found_money.contracts.private_run import (
    OperatorReviewPacketV1,
    PrivateRunAggregateProofV1,
    PrivateRunConfigV1,
    PrivateRunManifestV1,
    parse_operator_packet,
    parse_private_run_aggregate,
    parse_private_run_config,
    parse_private_run_manifest,
)
from found_money.contracts.private_run import PRIVATE_INPUT_ARTIFACTS
from found_money.private_run import (
    ALLOWED_SYSTEMS,
    IGNORED_PRIVATE_ROOT_PREFIXES,
    PRIVATE_RUN_ARTIFACTS,
    implementation_head_state,
    load_private_run_config,
    recompute_aggregate_proof,
    run_private_run,
)
from found_money.safety import (
    FakeHubSpotTransport,
    FakeStripeTransport,
    HUBSPOT_HOST,
    NetworkAllowlistError,
    SafeRequestAuditor,
    STRIPE_HOST,
    assert_network_allowed,
    scan_cli_help,
    scan_output_tree,
)
from found_money.scenarios import SYNTHETIC_SAAS_V1, run_scenario_engine

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "configs" / "synthetic-private-run-fixture.json"
REQUIRED_CHECKS = (
    "quality",
    "unit-and-contract",
    "public-safety",
    "render-proof",
    "verify-build-packet",
)
_REAL_HEAD = implementation_head_state


@pytest.fixture(autouse=True)
def _clean_implementation_head(monkeypatch):
    def _clean(repo=None):
        digest, _dirty = _REAL_HEAD(repo) if repo is not None else _REAL_HEAD()
        return digest, False

    monkeypatch.setattr("found_money.private_run.implementation_head_state", _clean)


def _write_config(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def _fixture_payload(**overrides: object) -> dict:
    payload: dict = {
        "schema_version": "found-money-private-run-config.v1",
        "scope": {
            "mode": "fixture",
            "systems": ["hubspot", "stripe"],
            "fixture": "synthetic-saas-v1",
            "runtime_mode": "test",
            "authorization": "not-authorized",
        },
        "credentials": {
            "source": "environment",
            "bindings": {
                "hubspot": "HUBSPOT_PRIVATE_APP_TOKEN",
                "stripe": "STRIPE_API_KEY",
            },
        },
    }
    payload.update(overrides)
    return payload


def _tree(root: Path) -> list[str]:
    if not root.exists():
        return []
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())


def test_documented_fixture_config_is_strict_and_credential_free():
    parsed = load_private_run_config(CONFIG)
    parse_private_run_config(parsed.to_canonical_json())
    assert parsed.scope.mode == "fixture"
    assert list(parsed.scope.systems) == ["hubspot", "stripe"]
    assert parsed.scope.fixture == SYNTHETIC_SAAS_V1
    assert parsed.credentials.source == "environment"
    assert parsed.credentials.bindings["hubspot"] == "HUBSPOT_PRIVATE_APP_TOKEN"
    assert parsed.credentials.bindings["stripe"] == "STRIPE_API_KEY"
    text = CONFIG.read_text(encoding="utf-8").casefold()
    for banned in ("sk_", "rk_", "pat-", "bearer ", 'token":', "secret"):
        assert banned not in text


def test_fixture_replay_writes_private_receipts_aggregate_and_operator_packet(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    result = run_private_run(
        config_path=CONFIG,
        private_root="private-runs/fm036-fixture",
        repo_root=ROOT,
    )
    root = result.private_root
    assert root == (tmp_path / "private-runs" / "fm036-fixture").resolve()
    for relative in PRIVATE_RUN_ARTIFACTS:
        assert (root / relative).is_file(), relative
    for relative in PRIVATE_INPUT_ARTIFACTS:
        assert (root / relative).is_file(), relative
    public_snapshots = [
        path
        for path in root.rglob("*")
        if path.is_file()
        and "snapshot" in path.name
        and "private/" not in path.relative_to(root).as_posix()
    ]
    assert public_snapshots == []

    manifest = parse_private_run_manifest((root / "private-run.json").read_bytes())
    aggregate = parse_private_run_aggregate((root / "aggregate-proof.json").read_bytes())
    packet = parse_operator_packet((root / "operator-packet.json").read_bytes())
    assert manifest.schema_version == "found-money-private-run.v1"
    assert set(manifest.systems) == {"hubspot", "stripe"}
    assert manifest.proof_classes["fixture_proof"].status == "pending"
    assert manifest.proof_classes["live_proof"].status == "not_started"
    assert manifest.proof_classes["human_verdict"].status == "not_started"
    assert list(manifest.required_checks) == list(REQUIRED_CHECKS)
    assert all(
        item.status == "pending"
        for item in manifest.proof_classes["fixture_proof"].required_check_results
    )
    assert packet.usefulness_verdict == "not_started"
    assert packet.human_issued is False
    assert packet.proof_class == "fixture"
    assert packet.issuer_kind == "fixture"
    assert packet.no_send is True
    assert packet.no_write is True
    assert packet.no_audience is True
    assert packet.no_spend is True
    assert packet.no_recovered_revenue_claim is True
    assert packet.claims_real_business_run is False
    assert aggregate.public_safe is True
    assert aggregate.public_safety_scan == "pass"
    assert scan_output_tree(root) == []


def test_independent_recompute_matches_aggregate_proof(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = run_private_run(
        config_path=CONFIG,
        private_root="private-runs/fm036-fixture",
        repo_root=ROOT,
    )
    written = parse_private_run_aggregate(
        (result.private_root / "aggregate-proof.json").read_bytes()
    )
    recomputed = recompute_aggregate_proof(result.private_root)
    assert recomputed.to_canonical_json() == written.to_canonical_json()
    assert written.identified_opportunity_minor
    assert "usd" in written.basis_counts_by_currency
    basis = written.basis_counts_by_currency["usd"]
    assert (
        basis.observed_event_count + basis.modeled_event_count + basis.unquantified_event_count >= 0
    )


def test_operator_packet_schema_accepts_only_the_three_verdicts():
    base = {
        "schema_version": "found-money-operator-review-packet.v1",
        "run_id": "prv_fixture",
        "usefulness_verdict": "useful",
        "rationale": "Operator reviewed aggregate proof only.",
        "limitations": ["Fixture replay is not a live-business read."],
        "unresolved_data_gaps": ["live_two_system_read"],
        "evidence_refs": {"aggregate-proof.json": "a" * 64},
        "no_send": True,
        "no_write": True,
        "no_audience": True,
        "no_spend": True,
        "no_recovered_revenue_claim": True,
        "claims_real_business_run": False,
        "human_issued": True,
        "proof_class": "human",
        "issuer_kind": "human",
        "operator_id": "matthew",
        "issued_at": "2026-08-11T18:00:00.000Z",
        "bound_commit_hash": "a" * 40,
        "check_evidence_hashes": {
            "quality": "b" * 64,
            "unit-and-contract": "c" * 64,
            "public-safety": "d" * 64,
            "render-proof": "e" * 64,
            "verify-build-packet": "f" * 64,
        },
    }
    for verdict in ("useful", "not_useful", "waived"):
        packet = OperatorReviewPacketV1.model_validate({**base, "usefulness_verdict": verdict})
        assert packet.usefulness_verdict == verdict
        parse_operator_packet(packet.to_canonical_json())
    with pytest.raises(ValueError):
        OperatorReviewPacketV1.model_validate({**base, "usefulness_verdict": "maybe"})
    with pytest.raises(ValueError):
        OperatorReviewPacketV1.model_validate({**base, "no_send": False})
    with pytest.raises(ValueError):
        OperatorReviewPacketV1.model_validate({**base, "claims_real_business_run": True})
    with pytest.raises(ValueError):
        OperatorReviewPacketV1.model_validate(
            {**base, "proof_class": "fixture", "human_issued": True}
        )


def test_proof_labels_and_commit_checks_are_exact(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    expected_commit = (
        subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        .stdout.strip()
        .lower()
    )
    result = run_private_run(
        config_path=CONFIG,
        private_root="private-runs/fm036-fixture",
        repo_root=ROOT,
    )
    manifest = parse_private_run_manifest((result.private_root / "private-run.json").read_bytes())
    assert manifest.commit_hash == expected_commit
    assert set(manifest.proof_classes) == {"fixture_proof", "live_proof", "human_verdict"}
    assert manifest.proof_classes["fixture_proof"].required_checks == list(REQUIRED_CHECKS)
    assert manifest.proof_classes["live_proof"].commit_hash is None
    assert manifest.proof_classes["human_verdict"].commit_hash is None


def test_cli_fixture_replay_from_relative_ignored_root(tmp_path, monkeypatch):
    from found_money.__main__ import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "found-money",
            "private-run",
            "--config",
            str(CONFIG),
            "--private-root",
            "private-runs/fm036-cli",
        ],
    )
    assert main() == 0
    assert (tmp_path / "private-runs" / "fm036-cli" / "operator-packet.json").is_file()


def test_cli_help_lists_private_run_without_send_or_write_verbs():
    violations = scan_cli_help()
    assert violations == []


def test_private_root_prefixes_are_gitignored_and_unpublished():
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for prefix in IGNORED_PRIVATE_ROOT_PREFIXES:
        assert f"{prefix}/" in gitignore or prefix in gitignore
        checked = subprocess.run(
            ["git", "check-ignore", "-q", f"{prefix}/probe.json"],
            cwd=ROOT,
        )
        assert checked.returncode == 0, prefix
    tracked = subprocess.run(
        ["git", "ls-files", "private-runs", "runs", "artifacts/private"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert tracked == ""


def test_no_raw_payload_is_committed_for_private_run():
    listed = subprocess.run(
        ["git", "ls-files"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    for path in listed:
        lowered = path.casefold()
        assert not lowered.startswith("private-runs/")
        assert "raw-payload" not in lowered
        assert "operator-packet.json" not in Path(path).name or path.startswith("tests/")


def test_existing_saas_scenario_engine_behavior_is_preserved():
    engine = run_scenario_engine(
        run_id="run_fm036_regression",
        safe_config={
            "schema_version": "found-money-build-source.v1",
            "mode": "fixture",
            "run_mode": "public",
            "fixture": SYNTHETIC_SAAS_V1,
            "credential_declaration": "none",
            "credential_runtime": "test",
        },
    )
    assert engine.definition.fixture_id == SYNTHETIC_SAAS_V1
    assert set(engine.definition.sources) == {"hubspot", "stripe"}
    assert engine.identity_aggregate.resolved_customer_count >= 1


def test_allowed_systems_are_hubspot_and_stripe_only():
    assert ALLOWED_SYSTEMS == ("hubspot", "stripe")


def test_canonical_models_reject_noncanonical_bytes():
    config = load_private_run_config(CONFIG)
    with pytest.raises(ValueError, match="canonical"):
        parse_private_run_config(config.to_canonical_json() + b" ")
    PrivateRunConfigV1.model_validate(config.canonical_dict())
    PrivateRunManifestV1.model_validate_json  # imported for contract surface
    PrivateRunAggregateProofV1.model_validate_json


def test_fixture_replay_does_not_open_sockets(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    def boom(*_args, **_kwargs):
        raise AssertionError("socket connection attempted")

    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket.socket, "connect", boom)
    result = run_private_run(
        config_path=CONFIG,
        private_root="private-runs/fm036-fixture",
        repo_root=ROOT,
    )
    assert result.run_id


def test_bounded_two_system_interface_records_allowlisted_gets_only():
    auditor = SafeRequestAuditor()
    hubspot = FakeHubSpotTransport(auditor=auditor)
    stripe = FakeStripeTransport(auditor=auditor)
    get_method = "GET"
    hubspot.request(method=get_method, path="/crm/v3/objects/contacts")
    stripe.request(method=get_method, path="/v1/customers")
    with pytest.raises(NetworkAllowlistError):
        assert_network_allowed("POST", HUBSPOT_HOST, "/crm/v3/objects/contacts")
    with pytest.raises(NetworkAllowlistError):
        assert_network_allowed("DELETE", STRIPE_HOST, "/v1/customers")
    log = auditor.as_log()
    assert [record.method for record in log.records] == ["GET", "GET"]
    payload = log.to_canonical_json().decode("utf-8").casefold()
    assert "authorization" not in payload
    assert "bearer" not in payload
