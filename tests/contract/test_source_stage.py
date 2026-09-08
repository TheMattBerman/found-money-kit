"""FM-021 source-manifest, whole-stage, and atomic-output contracts."""

from __future__ import annotations

import json
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from found_money.contracts.source_stage import (
    SOURCE_MANIFEST_SCHEMA,
    SourceManifestV1,
    SourceSetManifestV1,
    parse_source_manifest,
)
from found_money.receipts import build_source_receipt
from found_money.source_stage import SourceArtifact, SourceStageError, run_source_stage

ROOT = Path(__file__).resolve().parents[2]
INPUT_FIXTURES = ROOT / "tests" / "fixtures" / "saas" / "imports"
WHEN = datetime(2026, 7, 29, 18, 0, 0, tzinfo=timezone.utc)


def _copy_inputs(tmp_path: Path) -> Path:
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    (inputs / "orders.csv").write_bytes((INPUT_FIXTURES / "orders" / "valid.csv").read_bytes())
    (inputs / "appointments.json").write_bytes(
        (INPUT_FIXTURES / "appointments" / "valid.json").read_bytes()
    )
    (inputs / "proposals.csv").write_bytes(
        (INPUT_FIXTURES / "proposals" / "valid.csv").read_bytes()
    )
    (inputs / "universal.csv").write_bytes((inputs / "orders.csv").read_bytes())
    (inputs / "universal.json").write_bytes((inputs / "appointments.json").read_bytes())
    return inputs


def _declarations(include_native: bool = True) -> list[dict[str, object]]:
    declarations: list[dict[str, object]] = [
        {"id": "orders", "type": "orders", "schema_version": "orders.v1", "path": "orders.csv"},
        {
            "id": "appointments",
            "type": "appointments",
            "schema_version": "appointments.v1",
            "path": "appointments.json",
        },
        {
            "id": "proposals",
            "type": "proposals",
            "schema_version": "proposals.v1",
            "path": "proposals.csv",
        },
        {
            "id": "universal-csv",
            "type": "csv",
            "schema_version": "orders.v1",
            "path": "universal.csv",
        },
        {
            "id": "universal-json",
            "type": "json",
            "schema_version": "appointments.v1",
            "path": "universal.json",
        },
    ]
    if include_native:
        declarations.extend(
            [
                {
                    "id": "hubspot",
                    "type": "hubspot",
                    "schema_version": "hubspot-crm.2026-03.v1",
                },
                {
                    "id": "stripe",
                    "type": "stripe",
                    "schema_version": "stripe.2026-02-25.clover.v1",
                },
            ]
        )
    return declarations


def _manifest(declarations: list[dict[str, object]]) -> SourceManifestV1:
    return SourceManifestV1(schema_version=SOURCE_MANIFEST_SCHEMA, sources=declarations)


def _native_adapter(declaration, *, retrieved_at: datetime) -> SourceArtifact:
    source_type = declaration.effective_source_type
    schema = declaration.schema_version
    locator = "/crm/v3/objects/contacts" if source_type == "hubspot" else "/v1/customers"
    boundary = f"{source_type}-redacted-boundary".encode()
    receipt = build_source_receipt(
        source_type=source_type,
        connector_schema_version=schema,
        retrieved_at=retrieved_at,
        locator_kind="endpoint",
        locator=locator,
        content=boundary,
        page_or_row_count=2,
        record_count=1,
        request_id=f"req_{source_type}",
        correlation_id="corr_source_stage",
    )
    return SourceArtifact(
        normalized_bytes=json.dumps(
            {"private_snapshot": source_type}, sort_keys=True, separators=(",", ":")
        ).encode()
        + b"\n",
        receipt=receipt,
    )


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_manifest_accepts_all_v1_sources_and_commits_one_receipt_tree(tmp_path):
    inputs = _copy_inputs(tmp_path)
    manifest = _manifest(_declarations())
    result = run_source_stage(
        manifest,
        input_root=inputs,
        output_root=tmp_path / "source-stage",
        adapters={"hubspot": _native_adapter, "stripe": _native_adapter},
        retrieved_at=WHEN,
    )

    assert result.output_root.is_dir()
    assert len(result.receipts) == 7
    assert result.source_set_hash == result.source_set.source_set_hash
    assert (
        result.output_root / "source-set.json"
    ).read_bytes() == result.source_set.to_canonical_json()
    parsed = SourceSetManifestV1.model_validate(
        json.loads((result.output_root / "source-set.json").read_bytes())
    )
    assert [entry.source_id for entry in parsed.sources] == sorted(result.receipts)
    assert all(path.is_file() for path in result.normalized_paths.values())
    assert not any(
        path.name.startswith(".found-money-source-stage-") for path in tmp_path.iterdir()
    )


def test_late_invalid_source_leaves_no_output_or_stage_residue(tmp_path):
    inputs = _copy_inputs(tmp_path)
    invalid = inputs / "invalid-proposals.csv"
    invalid.write_text(
        "proposal_id,customer_id,proposed_at,status,amount_minor,currency\n"
        "p1,c1,2026-01-01T00:00:00Z,sent,1,usd\n"
        "p2,c2,2026-01-02T00:00:00,sent,2,usd\n",
        encoding="utf-8",
    )
    manifest = _manifest(
        [
            {"id": "orders", "type": "orders", "schema_version": "orders.v1", "path": "orders.csv"},
            {
                "id": "late-proposals",
                "type": "proposals",
                "schema_version": "proposals.v1",
                "path": "invalid-proposals.csv",
            },
        ]
    )
    with pytest.raises(SourceStageError, match=r"late-proposals:.*CSV row 3.*timezone-aware"):
        run_source_stage(
            manifest, input_root=inputs, output_root=tmp_path / "output", retrieved_at=WHEN
        )
    assert not (tmp_path / "output").exists()
    assert not any(
        path.name.startswith(".found-money-source-stage-") for path in tmp_path.iterdir()
    )


def test_manifest_rejects_unknown_duplicate_and_unsafe_declarations_before_output(tmp_path):
    cases = [
        [{"id": "x", "type": "unknown", "schema_version": "x.v1", "path": "x.csv"}],
        [
            {"id": "same", "type": "orders", "schema_version": "orders.v1", "path": "a.csv"},
            {"id": "same", "type": "orders", "schema_version": "orders.v1", "path": "b.csv"},
        ],
        [{"id": "escape", "type": "orders", "schema_version": "orders.v1", "path": "../x.csv"}],
        [{"id": "absolute", "type": "orders", "schema_version": "orders.v1", "path": "/tmp/x.csv"}],
    ]
    for declarations in cases:
        with pytest.raises((ValidationError, ValueError)):
            _manifest(declarations)
    with pytest.raises(ValueError, match="duplicate JSON object key"):
        parse_source_manifest(
            b'{"schema_version":"found-money-source-manifest.v1","sources":[],"sources":[]}'
        )


def test_repeated_inputs_are_byte_stable_and_one_byte_changes_are_isolated(tmp_path):
    inputs = _copy_inputs(tmp_path)
    manifest = _manifest(
        [
            {"id": "orders", "type": "orders", "schema_version": "orders.v1", "path": "orders.csv"},
            {
                "id": "appointments",
                "type": "appointments",
                "schema_version": "appointments.v1",
                "path": "appointments.json",
            },
        ]
    )
    first = run_source_stage(
        manifest, input_root=inputs, output_root=tmp_path / "first", retrieved_at=WHEN
    )
    second = run_source_stage(
        manifest, input_root=inputs, output_root=tmp_path / "second", retrieved_at=WHEN
    )
    assert _tree(first.output_root) == _tree(second.output_root)

    (inputs / "orders.csv").write_bytes((inputs / "orders.csv").read_bytes() + b"\n")
    changed = run_source_stage(
        manifest, input_root=inputs, output_root=tmp_path / "changed", retrieved_at=WHEN
    )
    assert (
        changed.receipts["appointments"].to_canonical_json()
        == first.receipts["appointments"].to_canonical_json()
    )
    assert changed.receipts["orders"].content_hash != first.receipts["orders"].content_hash
    assert changed.source_set_hash != first.source_set_hash


def test_native_adapter_is_explicit_and_receipt_boundary_is_checked(tmp_path):
    inputs = _copy_inputs(tmp_path)
    manifest = _manifest(
        [{"id": "hubspot", "type": "hubspot", "schema_version": "hubspot-crm.2026-03.v1"}]
    )
    with pytest.raises(SourceStageError, match="native adapter must be injected"):
        run_source_stage(manifest, input_root=inputs, output_root=tmp_path / "no-adapter")
    assert not (tmp_path / "no-adapter").exists()

    def bad_adapter(declaration, *, retrieved_at):
        artifact = _native_adapter(declaration, retrieved_at=retrieved_at)
        return SourceArtifact(
            normalized_bytes=artifact.normalized_bytes,
            receipt=artifact.receipt.model_copy(update={"source_type": "stripe"}),
        )

    with pytest.raises(SourceStageError, match="receipt source type mismatch"):
        run_source_stage(
            manifest,
            input_root=inputs,
            output_root=tmp_path / "bad-adapter",
            adapters={"hubspot": bad_adapter},
            retrieved_at=WHEN,
        )
    assert not (tmp_path / "bad-adapter").exists()


def test_source_stage_cli_uses_manifest_parent_when_input_root_is_omitted(tmp_path, monkeypatch):
    inputs = _copy_inputs(tmp_path)
    manifest_path = inputs / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": SOURCE_MANIFEST_SCHEMA,
                "sources": [
                    {
                        "id": "orders",
                        "type": "orders",
                        "schema_version": "orders.v1",
                        "path": "orders.csv",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    from found_money.__main__ import main

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "found-money",
            "source-stage",
            "--manifest",
            str(manifest_path),
            "--output-root",
            str(tmp_path / "cli-output"),
        ],
    )
    assert main() == 0
    assert (tmp_path / "cli-output" / "source-set.json").is_file()


def test_source_stage_makes_no_network_calls(tmp_path, monkeypatch):
    inputs = _copy_inputs(tmp_path)
    manifest = _manifest(
        [{"id": "orders", "type": "orders", "schema_version": "orders.v1", "path": "orders.csv"}]
    )

    def fail_socket(*_args, **_kwargs):
        raise AssertionError("source stage attempted network access")

    monkeypatch.setattr(socket, "socket", fail_socket)
    result = run_source_stage(
        manifest, input_root=inputs, output_root=tmp_path / "safe", retrieved_at=WHEN
    )
    assert result.source_set_hash
