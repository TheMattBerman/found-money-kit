"""Builders and atomic writers for source receipts and run manifests."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from found_money.contracts.run import (
    RunManifestV1,
    StageState,
    compute_source_set_hash,
)
from found_money.contracts.source import LocatorKind, SourceReceiptV1

_TRAVERSAL_RE = re.compile(r"(^|/)\.\.(/|$)")
_WINDOWS_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")
_SCHEMA_MAJOR_RE = re.compile(r"^(?P<name>.+)\.v(?P<major>\d+)$")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def build_source_receipt(
    *,
    source_type: str,
    connector_schema_version: str,
    retrieved_at: datetime,
    locator_kind: LocatorKind,
    locator: str,
    content: bytes,
    page_or_row_count: int,
    record_count: int,
    request_id: str | None = None,
    correlation_id: str | None = None,
) -> SourceReceiptV1:
    """Build a typed receipt whose content_hash covers the provided source bytes."""
    return SourceReceiptV1(
        source_type=source_type,
        connector_schema_version=connector_schema_version,
        retrieved_at=retrieved_at,
        locator_kind=locator_kind,
        locator=locator,
        page_or_row_count=page_or_row_count,
        record_count=record_count,
        content_hash=sha256_bytes(content),
        request_id=request_id,
        correlation_id=correlation_id,
    )


def build_run_manifest(
    *,
    run_id: str,
    referenced_schema_versions: Mapping[str, str],
    started_at: datetime,
    completed_at: datetime,
    mode: str,
    source_receipts: Mapping[str, SourceReceiptV1],
    stages: Mapping[str, StageState],
    artifact_hashes: Mapping[str, str] | None = None,
) -> RunManifestV1:
    """Build a typed run manifest with a deterministic source-set hash."""
    path_to_hash = {path: receipt.content_hash for path, receipt in source_receipts.items()}
    return RunManifestV1(
        run_id=run_id,
        referenced_schema_versions=dict(referenced_schema_versions),
        started_at=started_at,
        completed_at=completed_at,
        mode=mode,  # type: ignore[arg-type]
        source_receipt_paths=sorted(path_to_hash),
        stages=dict(stages),
        artifact_hashes=dict(artifact_hashes or {}),
        source_set_hash=compute_source_set_hash(path_to_hash),
    )


def _validate_relative_under_root(output_root: Path, relative_path: str) -> Path:
    if not isinstance(relative_path, str):
        raise ValueError("relative path must be a string")
    raw = relative_path.strip().replace("\\", "/")
    if not raw:
        raise ValueError("relative path must be non-empty")
    if raw.startswith("/") or _WINDOWS_ABS_RE.match(raw) or raw.startswith("~/"):
        raise ValueError("absolute paths are rejected")
    if raw.startswith("./"):
        raw = raw[2:]
    if not raw or _TRAVERSAL_RE.search(raw) or raw == ".." or Path(raw).is_absolute():
        raise ValueError("traversal or absolute paths are rejected")

    root = output_root.expanduser().resolve(strict=False)
    # Validate before creating any directories.
    candidate = root.joinpath(*Path(raw).parts)
    try:
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise ValueError("path escapes the caller-owned output root") from exc

    # Symlink-escape: any existing symlink in the parent chain that leaves root.
    probe = root
    for part in Path(raw).parts[:-1]:
        probe = probe / part
        if probe.is_symlink():
            try:
                probe.resolve(strict=True).relative_to(root)
            except (OSError, ValueError) as exc:
                raise ValueError("symlink escapes the caller-owned output root") from exc
        if probe.exists() and not probe.is_dir():
            raise ValueError("parent path component is not a directory")
    if candidate.exists() and candidate.is_symlink():
        try:
            candidate.resolve(strict=True).relative_to(root)
        except (OSError, ValueError) as exc:
            raise ValueError("symlink escapes the caller-owned output root") from exc
    return candidate


def _atomic_write_bytes(destination: Path, payload: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=str(destination.parent),
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, destination)
    except Exception:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)
        raise


def write_source_receipt(
    output_root: Path | str,
    relative_path: str,
    receipt: SourceReceiptV1,
) -> Path:
    """Atomically write a canonical source receipt beneath a caller-owned root."""
    root = Path(output_root)
    destination = _validate_relative_under_root(root, relative_path)
    _atomic_write_bytes(destination, receipt.to_canonical_json())
    return destination


def write_run_manifest(
    output_root: Path | str,
    relative_path: str,
    manifest: RunManifestV1,
) -> Path:
    """Atomically write a canonical run manifest beneath a caller-owned root."""
    root = Path(output_root)
    destination = _validate_relative_under_root(root, relative_path)
    _atomic_write_bytes(destination, manifest.to_canonical_json())
    return destination


def _object_pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate JSON object key: {key}")
        out[key] = value
    return out


def _major_schema_version(schema_version: str) -> tuple[str, int]:
    match = _SCHEMA_MAJOR_RE.fullmatch(schema_version.strip())
    if match is None:
        raise ValueError(f"unrecognized schema_version: {schema_version!r}")
    return match.group("name"), int(match.group("major"))


def parse_canonical_json(data: bytes) -> SourceReceiptV1 | RunManifestV1:
    """Parse canonical receipt/manifest bytes into the matching typed model."""
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("canonical JSON must be bytes")
    try:
        payload = json.loads(data.decode("utf-8"), object_pairs_hook=_object_pairs_hook)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"malformed JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("canonical JSON root must be an object")
    schema_version = payload.get("schema_version")
    if not isinstance(schema_version, str):
        raise ValueError("schema_version is required")
    name, major = _major_schema_version(schema_version)
    if name == "source-receipt":
        if major != 1:
            raise ValueError(f"unknown major schema version: {schema_version}")
        model: SourceReceiptV1 | RunManifestV1 = SourceReceiptV1.model_validate(payload)
    elif name == "run-manifest":
        if major != 1:
            raise ValueError(f"unknown major schema version: {schema_version}")
        model = RunManifestV1.model_validate(payload)
    else:
        raise ValueError(f"unknown major schema version: {schema_version}")
    canonical = model.to_canonical_json()
    if canonical != bytes(data):
        raise ValueError("serialized bytes are not exactly canonical")
    return model


def thin_slice_fixture_root() -> Path:
    return Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "saas" / "thin-slice"


def build_thin_slice_artifacts(
    *,
    retrieved_at: datetime | None = None,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
    run_id: str = "run_thin_slice_001",
) -> tuple[dict[str, SourceReceiptV1], RunManifestV1, dict[str, bytes]]:
    """Build deterministic receipts and a manifest for the synthetic SaaS fixture."""
    root = thin_slice_fixture_root()
    hubspot_path = root / "hubspot" / "snapshot.json"
    stripe_path = root / "stripe" / "snapshot.json"
    hubspot_bytes = hubspot_path.read_bytes()
    stripe_bytes = stripe_path.read_bytes()
    when = retrieved_at or datetime(2026, 7, 29, 18, 0, 0, tzinfo=timezone.utc)
    start = started_at or datetime(2026, 7, 29, 18, 0, 0, tzinfo=timezone.utc)
    end = completed_at or datetime(2026, 7, 29, 18, 0, 5, tzinfo=timezone.utc)

    receipts = {
        "receipts/hubspot.source-receipt.json": build_source_receipt(
            source_type="hubspot",
            connector_schema_version="hubspot-snapshot.v1",
            retrieved_at=when,
            locator_kind="input_path",
            locator="tests/fixtures/saas/thin-slice/hubspot/snapshot.json",
            content=hubspot_bytes,
            page_or_row_count=1,
            record_count=1,
            request_id="req_hubspot_thin_slice",
            correlation_id="corr_thin_slice",
        ),
        "receipts/stripe.source-receipt.json": build_source_receipt(
            source_type="stripe",
            connector_schema_version="stripe-snapshot.v1",
            retrieved_at=when,
            locator_kind="input_path",
            locator="tests/fixtures/saas/thin-slice/stripe/snapshot.json",
            content=stripe_bytes,
            page_or_row_count=1,
            record_count=2,
            request_id="req_stripe_thin_slice",
            correlation_id="corr_thin_slice",
        ),
    }
    manifest = build_run_manifest(
        run_id=run_id,
        referenced_schema_versions={
            "source-receipt": "source-receipt.v1",
            "run-manifest": "run-manifest.v1",
            "hubspot-snapshot": "hubspot-snapshot.v1",
            "stripe-snapshot": "stripe-snapshot.v1",
        },
        started_at=start,
        completed_at=end,
        mode="public",
        source_receipts=receipts,
        stages={
            "ingest": "completed",
            "identity": "not_started",
            "value": "not_started",
            "strategy": "not_started",
        },
        artifact_hashes={
            "receipts/hubspot.source-receipt.json": sha256_bytes(
                receipts["receipts/hubspot.source-receipt.json"].to_canonical_json()
            ),
            "receipts/stripe.source-receipt.json": sha256_bytes(
                receipts["receipts/stripe.source-receipt.json"].to_canonical_json()
            ),
        },
    )
    encoded = {path: receipt.to_canonical_json() for path, receipt in receipts.items()}
    encoded["manifests/run-manifest.json"] = manifest.to_canonical_json()
    return receipts, manifest, encoded


def write_thin_slice_artifacts(output_root: Path | str) -> Path:
    """Write the synthetic thin-slice receipts and manifest under output_root."""
    root = Path(output_root)
    receipts, manifest, _encoded = build_thin_slice_artifacts()
    for relative, receipt in receipts.items():
        write_source_receipt(root, relative, receipt)
    write_run_manifest(root, "manifests/run-manifest.json", manifest)
    return root
