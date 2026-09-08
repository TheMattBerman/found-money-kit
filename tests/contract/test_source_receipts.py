"""FM-001 source-receipt and run-manifest contract tests."""

from __future__ import annotations

import json
import re
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from found_money.contracts.run import RunManifestV1, compute_source_set_hash
from found_money.contracts.source import SourceReceiptV1
from found_money.receipts import (
    build_run_manifest,
    build_source_receipt,
    build_thin_slice_artifacts,
    parse_canonical_json,
    thin_slice_fixture_root,
    write_run_manifest,
    write_source_receipt,
    write_thin_slice_artifacts,
)

ROOT = Path(__file__).resolve().parents[2]
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
UTC = timezone.utc


def _receipt(**overrides):
    base = {
        "schema_version": "source-receipt.v1",
        "source_type": "hubspot",
        "connector_schema_version": "hubspot-snapshot.v1",
        "retrieved_at": "2026-07-29T18:00:00.000Z",
        "locator_kind": "input_path",
        "locator": "tests/fixtures/saas/thin-slice/hubspot/snapshot.json",
        "page_or_row_count": 1,
        "record_count": 1,
        "content_hash": HASH_A,
    }
    base.update(overrides)
    return SourceReceiptV1.model_validate(base)


def _manifest(**overrides):
    base = {
        "schema_version": "run-manifest.v1",
        "run_id": "run_001",
        "referenced_schema_versions": {
            "source-receipt": "source-receipt.v1",
            "run-manifest": "run-manifest.v1",
        },
        "started_at": "2026-07-29T18:00:00.000Z",
        "completed_at": "2026-07-29T18:00:05.000Z",
        "mode": "public",
        "source_receipt_paths": ["receipts/a.json"],
        "stages": {"ingest": "completed"},
        "artifact_hashes": {"receipts/a.json": HASH_A},
        "source_set_hash": HASH_B,
    }
    base.update(overrides)
    return RunManifestV1.model_validate(base)


def test_source_receipt_validation_and_canonical_bytes():
    receipt = _receipt()
    payload = receipt.to_canonical_json()
    assert payload.endswith(b"\n")
    assert payload.count(b"\n") == 1
    assert (
        json.dumps(
            json.loads(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        + b"\n"
        == payload
    )

    # Missing / extra / invalid
    with pytest.raises(ValidationError):
        SourceReceiptV1.model_validate(
            {k: v for k, v in _receipt().model_dump(mode="json").items() if k != "content_hash"}
        )
    with pytest.raises(ValidationError):
        SourceReceiptV1.model_validate({**_receipt().model_dump(mode="json"), "extra": 1})
    with pytest.raises(ValidationError):
        _receipt(page_or_row_count=-1)
    with pytest.raises(ValidationError):
        _receipt(locator_kind="input_path", locator="/abs/path")
    with pytest.raises(ValidationError):
        _receipt(locator_kind="endpoint", locator="/crm/v3/objects?limit=1")
    with pytest.raises(ValidationError):
        _receipt(content_hash="not-a-hash")

    # R-1: normalization fixtures place transformed values at start, end, and entire field.
    cases = {
        "schema_version": [
            "  source-receipt.v1",
            "source-receipt.v1  ",
            "  source-receipt.v1  ",
        ],
        "source_type": ["  hubspot", "hubspot  ", "  hubspot  "],
        "connector_schema_version": [
            "  hubspot-snapshot.v1",
            "hubspot-snapshot.v1  ",
            "  hubspot-snapshot.v1  ",
        ],
        "retrieved_at": [
            "  2026-07-29T18:00:00.000Z",
            "2026-07-29T18:00:00.000Z  ",
            "  2026-07-29T18:00:00.000Z  ",
        ],
        "locator": [
            "  fixtures/input.json",
            "fixtures/input.json  ",
            "  fixtures/input.json  ",
        ],
        "content_hash": [
            f"  {HASH_A.upper()}",
            f"{HASH_A.upper()}  ",
            f"  {HASH_A.upper()}  ",
        ],
        "request_id": ["  req_start", "req_end  ", "  req_entire  "],
        "correlation_id": ["  corr_start", "corr_end  ", "  corr_entire  "],
    }
    for field, values in cases.items():
        for value in values:
            normalized = _receipt(**{field: value})
            encoded = normalized.to_canonical_json()
            assert parse_canonical_json(encoded).to_canonical_json() == encoded


def test_run_manifest_validation_and_forbidden_fields():
    manifest = _manifest()
    assert manifest.schema_version == "run-manifest.v1"
    with pytest.raises(ValidationError):
        _manifest(token="secret-value")
    with pytest.raises(ValidationError):
        _manifest(api_key="x")
    with pytest.raises(ValidationError):
        _manifest(source_receipt_paths=["/tmp/a.json"])
    with pytest.raises(ValidationError):
        _manifest(source_receipt_paths=["../outside.json"])
    with pytest.raises(ValidationError):
        _manifest(artifact_hashes={"a.json": "abc"})
    with pytest.raises(ValidationError):
        _manifest(
            started_at="2026-07-29T19:00:00.000Z",
            completed_at="2026-07-29T18:00:00.000Z",
        )
    with pytest.raises(ValidationError):
        _manifest(stages={"ingest": "running"})
    with pytest.raises(ValidationError):
        RunManifestV1.model_validate({**manifest.model_dump(mode="json"), "extra": True})

    # R-1 normalization coverage for manifest fields.
    for field, values in {
        "schema_version": ["  run-manifest.v1", "run-manifest.v1  ", "  run-manifest.v1  "],
        "run_id": ["  run_start", "run_end  ", "  run_entire  "],
        "started_at": [
            "  2026-07-29T18:00:00.000Z",
            "2026-07-29T18:00:00.000Z  ",
            "  2026-07-29T18:00:00.000Z  ",
        ],
        "completed_at": [
            "  2026-07-29T18:00:05.000Z",
            "2026-07-29T18:00:05.000Z  ",
            "  2026-07-29T18:00:05.000Z  ",
        ],
        "source_set_hash": [
            f"  {HASH_B.upper()}",
            f"{HASH_B.upper()}  ",
            f"  {HASH_B.upper()}  ",
        ],
    }.items():
        for value in values:
            encoded = _manifest(**{field: value}).to_canonical_json()
            assert parse_canonical_json(encoded).to_canonical_json() == encoded

    # Nested map keys/values at start/end/entire.
    nested = _manifest(
        referenced_schema_versions={
            "  source-receipt  ": "  source-receipt.v1  ",
        },
        source_receipt_paths=["  receipts/a.json  "],
        artifact_hashes={"  receipts/a.json  ": f"  {HASH_A.upper()}  "},
        stages={"  ingest  ": "completed"},
    )
    assert "source-receipt" in nested.referenced_schema_versions
    assert nested.source_receipt_paths == ["receipts/a.json"]
    assert nested.artifact_hashes["receipts/a.json"] == HASH_A


def test_thin_saas_fixture_is_synthetic_and_public_safe():
    root = thin_slice_fixture_root()
    hubspot = (root / "hubspot" / "snapshot.json").read_text(encoding="utf-8")
    stripe = (root / "stripe" / "snapshot.json").read_text(encoding="utf-8")
    combined = hubspot + "\n" + stripe
    assert "cust_synth_001" in hubspot and "cust_synth_001" in stripe
    assert "inv_failed_001" in stripe
    assert "later_success_control" in stripe or "inv_paid_001" in stripe
    forbidden = [
        r"(?i)@gmail\.com",
        r"(?i)@yahoo\.com",
        r"(?i)https?://",
        r"(?i)vault",
        r"(?i)private-runs",
        r"(?i)password\s*[:=]",
        r"(?i)api[_-]?key",
        r"\+1\d{10}",
    ]
    for pattern in forbidden:
        assert re.search(pattern, combined) is None, pattern
    # No live email domains / phones / URLs in the fixture itself.
    assert re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", combined) is None
    assert "Matthew" not in combined
    assert "Berman" not in combined


def test_receipt_and_source_set_hashes_are_deterministic_and_isolated(tmp_path):
    fixture = thin_slice_fixture_root()
    hubspot = (fixture / "hubspot" / "snapshot.json").read_bytes()
    stripe = (fixture / "stripe" / "snapshot.json").read_bytes()
    when = datetime(2026, 7, 29, 18, 0, 0, tzinfo=UTC)
    start = when
    end = when + timedelta(seconds=5)

    def build_pair(hubspot_bytes: bytes, stripe_bytes: bytes):
        receipts = {
            "receipts/hubspot.json": build_source_receipt(
                source_type="hubspot",
                connector_schema_version="hubspot-snapshot.v1",
                retrieved_at=when,
                locator_kind="input_path",
                locator="tests/fixtures/saas/thin-slice/hubspot/snapshot.json",
                content=hubspot_bytes,
                page_or_row_count=1,
                record_count=1,
            ),
            "receipts/stripe.json": build_source_receipt(
                source_type="stripe",
                connector_schema_version="stripe-snapshot.v1",
                retrieved_at=when,
                locator_kind="input_path",
                locator="tests/fixtures/saas/thin-slice/stripe/snapshot.json",
                content=stripe_bytes,
                page_or_row_count=1,
                record_count=2,
            ),
        }
        manifest = build_run_manifest(
            run_id="run_det",
            referenced_schema_versions={
                "source-receipt": "source-receipt.v1",
                "run-manifest": "run-manifest.v1",
            },
            started_at=start,
            completed_at=end,
            mode="public",
            source_receipts=receipts,
            stages={"ingest": "completed"},
        )
        return receipts, manifest

    first_receipts, first_manifest = build_pair(hubspot, stripe)
    second_receipts, second_manifest = build_pair(hubspot, stripe)
    assert (
        first_receipts["receipts/hubspot.json"].to_canonical_json()
        == second_receipts["receipts/hubspot.json"].to_canonical_json()
    )
    assert (
        first_receipts["receipts/stripe.json"].to_canonical_json()
        == second_receipts["receipts/stripe.json"].to_canonical_json()
    )
    assert first_manifest.to_canonical_json() == second_manifest.to_canonical_json()

    mutated = hubspot + b"\n"
    changed_receipts, changed_manifest = build_pair(mutated, stripe)
    assert (
        changed_receipts["receipts/hubspot.json"].content_hash
        != first_receipts["receipts/hubspot.json"].content_hash
    )
    assert (
        changed_receipts["receipts/stripe.json"].to_canonical_json()
        == first_receipts["receipts/stripe.json"].to_canonical_json()
    )
    assert changed_manifest.source_set_hash != first_manifest.source_set_hash
    # Untouched unrelated fields remain byte-identical in the stripe receipt and
    # in manifest fields that do not depend on the hubspot hash.
    left = json.loads(first_manifest.to_canonical_json())
    right = json.loads(changed_manifest.to_canonical_json())
    for key in (
        "run_id",
        "referenced_schema_versions",
        "started_at",
        "completed_at",
        "mode",
        "stages",
        "schema_version",
    ):
        assert left[key] == right[key]


def _list_tree(root: Path) -> list[str]:
    if not root.exists():
        return []
    return sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() or path.is_dir()
    )


def test_receipt_writes_reject_escape_without_partial_state(tmp_path):
    root = tmp_path / "out"
    root.mkdir()
    before = _list_tree(root)
    receipt = _receipt()
    manifest = _manifest()

    rejects = [
        ("../outside.json", "traversal"),
        (str(tmp_path / "sibling.json"), "absolute"),
        ("/tmp/abs.json", "absolute"),
    ]
    for relative, _label in rejects:
        with pytest.raises(ValueError):
            write_source_receipt(root, relative, receipt)
        # R-3: assert complete output-root state after failure, not only the exception.
        assert _list_tree(root) == before
        assert list(root.glob("**/*")) == []
        with pytest.raises(ValueError):
            write_run_manifest(root, relative, manifest)
        assert _list_tree(root) == before
        assert list(root.glob("**/*")) == []

    # Symlink escape
    outside = tmp_path / "outside-target"
    outside.mkdir()
    link = root / "link-out"
    link.symlink_to(outside, target_is_directory=True)
    before_link = _list_tree(root)
    with pytest.raises(ValueError):
        write_source_receipt(root, "link-out/escaped.json", receipt)
    assert _list_tree(root) == before_link
    assert list(outside.iterdir()) == []

    # Successful atomic write stays under root.
    written = write_source_receipt(root, "receipts/ok.json", receipt)
    assert written.is_file()
    assert written.resolve().is_relative_to(root.resolve())
    assert written.read_bytes() == receipt.to_canonical_json()
    assert not list(root.glob("**/*.tmp"))


def test_public_safety_scans_generated_contract_artifacts():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "public_safety", ROOT / "scripts" / "public_safety.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.main(ROOT) == 0
    violations = module.scan_generated_contract_artifacts(ROOT)
    assert violations == []
    poisoned = '{"schema_version":"source-receipt.v1","email":"a@b.co"}\n'
    found = module.scan_contract_artifact_text("poison", poisoned)
    assert found


def test_contract_round_trip_and_version_failures():
    receipts, manifest, encoded = build_thin_slice_artifacts()
    for relative, payload in encoded.items():
        parsed = parse_canonical_json(payload)
        assert parsed.to_canonical_json() == payload

    with pytest.raises(ValueError, match="malformed"):
        parse_canonical_json(b"{not-json")
    with pytest.raises(ValueError, match="duplicate"):
        parse_canonical_json(b'{"a":1,"a":2}\n')
    with pytest.raises(ValueError, match="unknown major"):
        bad = json.loads(encoded["receipts/hubspot.source-receipt.json"])
        bad["schema_version"] = "source-receipt.v2"
        parse_canonical_json(
            (
                json.dumps(bad, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            ).encode()
        )
    # Non-canonical spacing must fail even when semantically valid.
    pretty = json.dumps(json.loads(encoded["manifests/run-manifest.json"]), indent=2) + "\n"
    with pytest.raises(ValueError, match="canonical"):
        parse_canonical_json(pretty.encode())


def test_ng1_fixture_builders_do_not_open_sockets(monkeypatch):
    def forbid(*_args, **_kwargs):
        raise AssertionError("socket usage is forbidden in FM-001")

    monkeypatch.setattr(socket, "socket", forbid)
    monkeypatch.setattr(socket, "create_connection", forbid)
    write_thin_slice_artifacts.__module__  # touch import
    build_thin_slice_artifacts()
    compute_source_set_hash({"a.json": HASH_A})


def test_source_set_hash_changes_with_path_or_hash():
    first = compute_source_set_hash({"a.json": HASH_A, "b.json": HASH_B})
    second = compute_source_set_hash({"a.json": HASH_A, "b.json": HASH_C})
    third = compute_source_set_hash({"a2.json": HASH_A, "b.json": HASH_B})
    assert first != second
    assert first != third
    assert len(first) == 64
