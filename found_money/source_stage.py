"""Atomic, read-only orchestration for the FM-021 source stage.

Every declaration is validated before the first source is read. File loaders
run inside a private staging directory, and that directory is renamed into the
caller-owned output root only after every source and receipt succeeds. Native
connectors are injected as adapters so this layer cannot silently create a
network or credential capability.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from found_money.contracts.source import SourceReceiptV1
from found_money.contracts.source_stage import (
    SourceDeclarationV1,
    SourceManifestV1,
    SourceSetEntryV1,
    SourceSetManifestV1,
    parse_source_manifest,
)
from found_money.contracts.run import compute_source_set_hash
from found_money.imports.appointments import load_appointments_file
from found_money.imports.orders import load_orders_file
from found_money.imports.proposals import load_proposals_file
from found_money.receipts import _atomic_write_bytes, _validate_relative_under_root

SOURCE_SET_OUTPUT_PATH = "source-set.json"
DEFAULT_RETRIEVED_AT = datetime(2026, 7, 29, 18, 0, 0, tzinfo=timezone.utc)


class SourceStageError(ValueError):
    """A sanitized source-manifest, validation, or atomic-output failure."""


@dataclass(frozen=True)
class SourceArtifact:
    """Normalized private bytes and their already-redacted source receipt."""

    normalized_bytes: bytes
    receipt: SourceReceiptV1


class NativeSourceAdapter(Protocol):
    """Injected native adapter boundary; implementations own provider details."""

    def fetch(
        self,
        declaration: SourceDeclarationV1,
        *,
        retrieved_at: datetime,
    ) -> SourceArtifact: ...


NativeAdapter = NativeSourceAdapter | Callable[..., SourceArtifact]


@dataclass(frozen=True)
class SourceStageResult:
    """Committed source-stage outputs and their deterministic source-set index."""

    output_root: Path
    source_set: SourceSetManifestV1
    receipts: dict[str, SourceReceiptV1]
    normalized_paths: dict[str, Path]

    @property
    def source_set_hash(self) -> str:
        return self.source_set.source_set_hash


def load_source_manifest(path: Path | str) -> SourceManifestV1:
    """Read a manifest file without exposing its local path in output metadata."""
    manifest_path = Path(path)
    try:
        raw = manifest_path.read_bytes()
    except OSError as exc:
        raise SourceStageError("source manifest is unavailable") from exc
    try:
        return parse_source_manifest(raw)
    except (TypeError, ValueError) as exc:
        raise SourceStageError(str(exc)) from exc


def _resolve_input_root(root: Path | str, manifest: SourceManifestV1) -> Path:
    base = Path(root)
    if not base.is_dir() or base.is_symlink():
        raise SourceStageError("input root must be an existing non-symlink directory")
    if manifest.source_root is not None:
        try:
            candidate = _validate_relative_under_root(base.resolve(), manifest.source_root)
        except ValueError as exc:
            raise SourceStageError("source_root failed input-root validation") from exc
        if not candidate.is_dir() or candidate.is_symlink():
            raise SourceStageError("source_root must be an existing directory")
        return candidate
    return base.resolve()


def _resolve_output_root(root: Path | str) -> Path:
    candidate = Path(root)
    if candidate.exists() or candidate.is_symlink():
        raise SourceStageError("source-stage output root must not already exist")
    parent = candidate.parent if candidate.parent != Path("") else Path.cwd()
    if not parent.exists() or not parent.is_dir() or parent.is_symlink():
        raise SourceStageError("source-stage output parent must be an existing directory")
    return candidate.resolve(strict=False)


def _source_paths(declaration: SourceDeclarationV1) -> tuple[str, str]:
    normalized = f"normalized/{declaration.source_id}/{declaration.schema_version}.json"
    receipt = f"receipts/{declaration.source_id}.source-receipt.json"
    return normalized, receipt


def _file_import(
    declaration: SourceDeclarationV1,
    *,
    input_root: Path,
    output_root: Path,
    retrieved_at: datetime,
) -> SourceReceiptV1:
    assert declaration.path is not None
    normalized_path, receipt_path = _source_paths(declaration)
    try:
        if declaration.effective_source_type == "orders":
            receipt = load_orders_file(
                input_root,
                declaration.path,
                output_root,
                normalized_path=normalized_path,
                receipt_path=receipt_path,
                retrieved_at=retrieved_at,
            )[1]
        elif declaration.effective_source_type == "appointments":
            receipt = load_appointments_file(
                input_root,
                declaration.path,
                output_root,
                normalized_path=normalized_path,
                receipt_path=receipt_path,
                retrieved_at=retrieved_at,
            )[1]
        elif declaration.effective_source_type == "proposals":
            receipt = load_proposals_file(
                input_root,
                declaration.path,
                output_root,
                normalized_path=normalized_path,
                receipt_path=receipt_path,
                retrieved_at=retrieved_at,
            )[1]
        else:  # The manifest contract should make this unreachable.
            raise SourceStageError("unsupported file source type")
    except SourceStageError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise SourceStageError(f"source {declaration.source_id}: {exc}") from exc
    return receipt


def _coerce_native_artifact(value: Any, *, source_id: str) -> SourceArtifact:
    if isinstance(value, SourceArtifact):
        artifact = value
    elif isinstance(value, tuple) and len(value) == 2:
        artifact = SourceArtifact(normalized_bytes=value[0], receipt=value[1])
    elif isinstance(value, Mapping):
        normalized_bytes = value.get("normalized_bytes")
        receipt = value.get("receipt")
        if not isinstance(normalized_bytes, bytes) or not isinstance(receipt, SourceReceiptV1):
            raise SourceStageError(
                f"source {source_id}: native adapter returned an invalid artifact"
            )
        artifact = SourceArtifact(normalized_bytes=normalized_bytes, receipt=receipt)
    else:
        raise SourceStageError(f"source {source_id}: native adapter returned an invalid artifact")
    if not isinstance(artifact.normalized_bytes, bytes):
        raise SourceStageError(f"source {source_id}: native normalized output must be bytes")
    if not isinstance(artifact.receipt, SourceReceiptV1):
        raise SourceStageError(f"source {source_id}: native adapter must return SourceReceiptV1")
    return artifact


def _native_import(
    declaration: SourceDeclarationV1,
    *,
    output_root: Path,
    retrieved_at: datetime,
    adapters: Mapping[str, NativeAdapter],
) -> SourceReceiptV1:
    adapter = adapters.get(declaration.source_id) or adapters.get(declaration.source_type)
    if adapter is None:
        raise SourceStageError(
            f"source {declaration.source_id}: native adapter must be injected; no network was attempted"
        )
    try:
        if hasattr(adapter, "fetch"):
            raw_artifact = adapter.fetch(declaration, retrieved_at=retrieved_at)
        else:
            raw_artifact = adapter(declaration, retrieved_at=retrieved_at)
        artifact = _coerce_native_artifact(raw_artifact, source_id=declaration.source_id)
    except SourceStageError:
        raise
    except Exception as exc:
        # Native/provider exceptions must not cross the public boundary.
        raise SourceStageError(f"source {declaration.source_id}: native adapter failed") from exc

    expected_type = declaration.effective_source_type
    if artifact.receipt.source_type != expected_type:
        raise SourceStageError(f"source {declaration.source_id}: receipt source type mismatch")
    if artifact.receipt.connector_schema_version != declaration.schema_version:
        raise SourceStageError(f"source {declaration.source_id}: receipt schema version mismatch")
    if artifact.receipt.locator_kind != "endpoint":
        raise SourceStageError(
            f"source {declaration.source_id}: native receipt must use endpoint locator"
        )
    normalized_path, receipt_path = _source_paths(declaration)
    _atomic_write_bytes(
        _validate_relative_under_root(output_root, normalized_path), artifact.normalized_bytes
    )
    _atomic_write_bytes(
        _validate_relative_under_root(output_root, receipt_path),
        artifact.receipt.to_canonical_json(),
    )
    return artifact.receipt


def _validate_receipt_paths(
    output_root: Path, declaration: SourceDeclarationV1, receipt: SourceReceiptV1
) -> tuple[str, str]:
    normalized_path, receipt_path = _source_paths(declaration)
    try:
        normalized = _validate_relative_under_root(output_root, normalized_path)
        receipt_file = _validate_relative_under_root(output_root, receipt_path)
    except ValueError as exc:
        raise SourceStageError(
            f"source {declaration.source_id}: output path validation failed"
        ) from exc
    if not normalized.is_file() or not receipt_file.is_file():
        raise SourceStageError(f"source {declaration.source_id}: staged output is incomplete")
    if receipt.source_type != declaration.effective_source_type:
        raise SourceStageError(f"source {declaration.source_id}: receipt source type mismatch")
    if receipt.connector_schema_version != declaration.schema_version:
        raise SourceStageError(f"source {declaration.source_id}: receipt schema version mismatch")
    return normalized_path, receipt_path


def run_source_stage(
    manifest: SourceManifestV1 | Mapping[str, Any] | bytes | bytearray | Path | str,
    *,
    input_root: Path | str | None = None,
    output_root: Path | str,
    adapters: Mapping[str, NativeAdapter] | None = None,
    retrieved_at: datetime | None = None,
) -> SourceStageResult:
    """Validate, normalize, receipt, and atomically commit one source set.

    ``adapters`` is keyed by source ID or source type. Native adapters are
    required explicitly; this function never reads provider credentials or
    creates an HTTP client on its own.
    """
    if isinstance(manifest, (Path, str)):
        manifest_path = Path(manifest)
        parsed_manifest = load_source_manifest(manifest_path)
        resolved_input_root = Path(input_root) if input_root is not None else manifest_path.parent
    elif isinstance(manifest, SourceManifestV1):
        parsed_manifest = manifest
        if input_root is None:
            raise SourceStageError("input_root is required for an in-memory source manifest")
        resolved_input_root = Path(input_root)
    else:
        try:
            parsed_manifest = parse_source_manifest(manifest)
        except (TypeError, ValueError) as exc:
            raise SourceStageError(str(exc)) from exc
        if input_root is None:
            raise SourceStageError("input_root is required for an in-memory source manifest")
        resolved_input_root = Path(input_root)

    input_base = _resolve_input_root(resolved_input_root, parsed_manifest)
    final_root = _resolve_output_root(output_root)
    stage_parent = final_root.parent
    stage_root = Path(tempfile.mkdtemp(prefix=".found-money-source-stage-", dir=stage_parent))
    when = retrieved_at or DEFAULT_RETRIEVED_AT
    adapter_map = adapters or {}
    receipts: dict[str, SourceReceiptV1] = {}
    normalized_paths: dict[str, Path] = {}

    try:
        for declaration in parsed_manifest.sources:
            if declaration.source_type in {"hubspot", "stripe"}:
                receipt = _native_import(
                    declaration,
                    output_root=stage_root,
                    retrieved_at=when,
                    adapters=adapter_map,
                )
            else:
                receipt = _file_import(
                    declaration,
                    input_root=input_base,
                    output_root=stage_root,
                    retrieved_at=when,
                )
            normalized_path, receipt_path = _validate_receipt_paths(
                stage_root, declaration, receipt
            )
            receipts[declaration.source_id] = receipt
            normalized_paths[declaration.source_id] = stage_root / normalized_path

        source_set_hash = compute_source_set_hash(
            {
                _source_paths(declaration)[1]: receipts[declaration.source_id].content_hash
                for declaration in parsed_manifest.sources
            }
        )
        entries = [
            SourceSetEntryV1(
                source_id=declaration.source_id,
                source_type=declaration.effective_source_type,
                connector_schema_version=declaration.schema_version,
                normalized_path=_source_paths(declaration)[0],
                receipt_path=_source_paths(declaration)[1],
                content_hash=receipts[declaration.source_id].content_hash,
                page_or_row_count=receipts[declaration.source_id].page_or_row_count,
                record_count=receipts[declaration.source_id].record_count,
            )
            for declaration in parsed_manifest.sources
        ]
        source_set = SourceSetManifestV1(source_set_hash=source_set_hash, sources=entries)
        _atomic_write_bytes(
            _validate_relative_under_root(stage_root, SOURCE_SET_OUTPUT_PATH),
            source_set.to_canonical_json(),
        )
        if final_root.exists():
            raise SourceStageError("source-stage output root appeared before commit")
        os.replace(stage_root, final_root)
        normalized_paths = {
            source_id: final_root / path.relative_to(stage_root)
            for source_id, path in normalized_paths.items()
        }
        return SourceStageResult(
            output_root=final_root,
            source_set=source_set,
            receipts=dict(receipts),
            normalized_paths=normalized_paths,
        )
    except SourceStageError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise SourceStageError("source stage failed before commit") from exc
    finally:
        if stage_root.exists():
            shutil.rmtree(stage_root, ignore_errors=True)


__all__ = [
    "DEFAULT_RETRIEVED_AT",
    "NativeSourceAdapter",
    "SOURCE_SET_OUTPUT_PATH",
    "SourceArtifact",
    "SourceStageError",
    "SourceStageResult",
    "load_source_manifest",
    "run_source_stage",
]
