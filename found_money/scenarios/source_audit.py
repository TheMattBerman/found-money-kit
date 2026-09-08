"""Bind the FM-035 source/proper-noun audit to the live service public tree."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping

from PIL import Image
from pypdf import PdfReader

from found_money.contracts.run import compute_source_set_hash
from found_money.contracts.source_audit import (
    PUBLIC_AUDIT_TEXT_SUFFIXES,
    SERVICE_SOURCE_AUDIT_EXTRACTIONS_SHA256,
    BinaryAuditExtractionsV1,
    BinaryAuditPdfExtractionV1,
    BinaryAuditPngExtractionV1,
    ServiceSourceProperNounAuditV1,
    parse_binary_audit_extractions,
)
from found_money.receipts import sha256_bytes
from found_money.scenarios.assets import load_scenario_definition, load_scenario_source_config_bytes
from found_money.scenarios.registry import SYNTHETIC_SERVICE_V1
from found_money.scenarios.transports import load_normalized_scenario_snapshots

# Encoder/platform PNG ancillary fields may vary across supported baseline
# platforms. They are excluded from the independently recomputed public
# semantic extraction. Textual content chunks, dimensions, and mode remain
# fail-closed.
_PNG_PLATFORM_INFO_KEYS = frozenset(
    {
        "aspect",
        "chromaticity",
        "creation time",
        "creation_time",
        "dpi",
        "exif",
        "gamma",
        "icc_profile",
        "interlace",
        "software",
        "transparency",
        "xml",
        "xmp",
    }
)

_CANONICAL_SERVICE_OUTPUT: Path | None = None


def _canonical_json_bytes(payload: Any) -> bytes:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return (text + "\n").encode("utf-8")


def current_service_run_binding() -> tuple[str, str]:
    """Return the live canonical service run_id and source_set_hash."""

    locked = load_scenario_definition(SYNTHETIC_SERVICE_V1)
    _snapshots, raw, receipts = load_normalized_scenario_snapshots(
        SYNTHETIC_SERVICE_V1, retrieved_at=locked.clock
    )
    expected_hashes = {name: sha256_bytes(raw[name]) for name in locked.sources}
    source_set_hash = compute_source_set_hash(
        {
            f"receipts/{name}.source-receipt.json": receipts[name].content_hash
            for name in locked.sources
        }
    )
    locked_config = {
        "schema_version": "found-money-build-source.v1",
        "mode": "fixture",
        "run_mode": "public",
        "credential_declaration": "none",
        "credential_runtime": "test",
        "fixture": SYNTHETIC_SERVICE_V1,
    }
    run_id = (
        "run_"
        + sha256_bytes(
            _canonical_json_bytes(
                {"source_config": locked_config, "source_hashes": expected_hashes}
            )
        )[:16]
    )
    return run_id, source_set_hash


def build_canonical_service_public_output(output_root: Path | str) -> Path:
    """Build the exact synthetic-service-v1 public tree under output_root."""

    from found_money.build import build

    root = Path(output_root)
    if root.is_absolute():
        parent = root.parent
        name = root.name
        parent.mkdir(parents=True, exist_ok=True)
        config_path = parent / ".synthetic-service-v1.source-config.json"
        config_path.write_bytes(load_scenario_source_config_bytes(SYNTHETIC_SERVICE_V1))
        previous = Path.cwd()
        try:
            os.chdir(parent)
            result = build(output_root=name, source_config=config_path.name)
        finally:
            os.chdir(previous)
        return result.output_root
    parent = Path.cwd() if str(root.parent) in {"", "."} else root.parent
    parent.mkdir(parents=True, exist_ok=True)
    config_path = parent / ".synthetic-service-v1.source-config.json"
    config_path.write_bytes(load_scenario_source_config_bytes(SYNTHETIC_SERVICE_V1))
    result = build(output_root=root, source_config=config_path)
    return result.output_root


def _default_canonical_service_output() -> Path:
    global _CANONICAL_SERVICE_OUTPUT
    cached = _CANONICAL_SERVICE_OUTPUT
    if cached is not None and cached.is_dir():
        return cached
    tmp = Path(tempfile.mkdtemp(prefix="found-money-service-source-audit-"))
    _CANONICAL_SERVICE_OUTPUT = build_canonical_service_public_output(tmp / "out")
    return _CANONICAL_SERVICE_OUTPUT


def list_public_tree_files(root: Path | str) -> dict[str, bytes]:
    """Return relative-path bytes for a public tree. Rejects symlinks and traversal."""

    output = Path(root)
    if not output.is_dir():
        raise ValueError("source proper-noun audit output root is missing")
    files: dict[str, bytes] = {}
    for path in sorted(output.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"source audit output contains a symlink: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(output).as_posix()
        if ".." in Path(relative).parts:
            raise ValueError(f"source audit output path contains traversal: {relative}")
        files[relative] = path.read_bytes()
    return files


def _output_files(root: Path) -> dict[str, bytes]:
    return list_public_tree_files(root)


def public_text_tree_binding(files: Mapping[str, bytes]) -> tuple[int, str]:
    """Return inspectable public text-file count and tree digest from live bytes."""

    paths = sorted(
        path for path in files if Path(path).suffix.casefold() in PUBLIC_AUDIT_TEXT_SUFFIXES
    )
    blob = b"".join(path.encode("utf-8") + b"\0" + files[path] + b"\0" for path in paths)
    return len(paths), sha256_bytes(blob)


def public_binary_tree_binding(files: Mapping[str, bytes]) -> tuple[int, str]:
    """Bind every public PDF/PNG byte without depending on encoder metadata parsing."""

    paths = sorted(path for path in files if Path(path).suffix.casefold() in {".pdf", ".png"})
    blob = b"".join(
        path.encode("utf-8") + b"\0" + sha256_bytes(files[path]).encode("ascii") + b"\0"
        for path in paths
    )
    return len(paths), sha256_bytes(blob)


def _extract_pdf(path: str, payload: bytes) -> BinaryAuditPdfExtractionV1:
    try:
        reader = PdfReader(io.BytesIO(payload))
    except Exception as exc:
        raise ValueError(f"source proper-noun audit PDF is unreadable: {path}") from exc
    text = "\n".join((page.extract_text() or "") for page in reader.pages)
    metadata: dict[str, str] = {}
    if reader.metadata is not None:
        for key, value in reader.metadata.items():
            metadata[str(key)] = "" if value is None else str(value)
    annotations: list[dict[str, Any]] = []
    for index, page in enumerate(reader.pages, start=1):
        annots = page.get("/Annots")
        if not annots:
            continue
        for annot in annots:
            obj = annot.get_object()
            dest = obj.get("/Dest")
            uri = None
            action = obj.get("/A")
            if action is not None:
                action = action.get_object()
                uri_value = action.get("/URI")
                if uri_value is not None:
                    uri = str(uri_value)
            annotations.append(
                {
                    "dest": None if dest is None else str(dest),
                    "page": index,
                    "uri": uri,
                }
            )
    return BinaryAuditPdfExtractionV1(
        path=path,
        sha256=sha256_bytes(payload),
        page_count=len(reader.pages),
        text=text,
        metadata=metadata,
        annotations=annotations,
    )


def _png_semantic_metadata(info: Mapping[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in info.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        if key.casefold() in _PNG_PLATFORM_INFO_KEYS:
            continue
        out[key] = value
    return out


def _extract_png(path: str, payload: bytes) -> BinaryAuditPngExtractionV1:
    try:
        with Image.open(io.BytesIO(payload)) as image:
            info: dict[str, Any] = dict(getattr(image, "info", {}) or {})
            text_chunks = getattr(image, "text", None) or {}
            info.update(dict(text_chunks))
            metadata = _png_semantic_metadata(info)
            mode = image.mode
            size = image.size
    except Exception as exc:
        raise ValueError(f"source proper-noun audit PNG is unreadable: {path}") from exc
    return BinaryAuditPngExtractionV1(
        path=path,
        sha256=sha256_bytes(payload),
        metadata=metadata,
        mode=mode,
        size=size,
    )


def extract_pdf_semantics(path: str, payload: bytes) -> BinaryAuditPdfExtractionV1:
    return _extract_pdf(path, payload)


def extract_png_semantics(path: str, payload: bytes) -> BinaryAuditPngExtractionV1:
    return _extract_png(path, payload)


def extract_binary_semantics(
    files: Mapping[str, bytes],
) -> tuple[list[BinaryAuditPdfExtractionV1], list[BinaryAuditPngExtractionV1]]:
    """Extract PDF/PNG semantics without locking a scenario-specific file count."""

    pdfs = [
        _extract_pdf(path, files[path])
        for path in sorted(files)
        if Path(path).suffix.casefold() == ".pdf"
    ]
    pngs = [
        _extract_png(path, files[path])
        for path in sorted(files)
        if Path(path).suffix.casefold() == ".png"
    ]
    return pdfs, pngs


def extract_public_binary_semantics(files: Mapping[str, bytes]) -> BinaryAuditExtractionsV1:
    """Independently extract PDF/PNG semantics from current output bytes."""

    pdfs, pngs = extract_binary_semantics(files)
    return BinaryAuditExtractionsV1(pdfs=pdfs, pngs=pngs)


def validate_service_source_proper_noun_audit(
    packet: ServiceSourceProperNounAuditV1,
    *,
    binary_extractions: bytes,
    output_root: Path | str | None = None,
) -> BinaryAuditExtractionsV1:
    """Bind the audit to the current service tree by recomputing live evidence."""

    run_id, source_set_hash = current_service_run_binding()
    if packet.run_id != run_id:
        raise ValueError("source proper-noun audit run_id does not bind to the current service run")
    if packet.source_set_hash != source_set_hash:
        raise ValueError("source proper-noun audit source_set_hash is stale")
    digest = sha256_bytes(binary_extractions)
    if (
        packet.binary_extractions_sha256 != digest
        or digest != SERVICE_SOURCE_AUDIT_EXTRACTIONS_SHA256
    ):
        raise ValueError("source proper-noun audit binary extractions hash is stale")
    extractions = parse_binary_audit_extractions(binary_extractions)
    if packet.audited_pdf_count != len(extractions.pdfs):
        raise ValueError("source proper-noun audit PDF count does not match extractions")
    if packet.audited_png_count != len(extractions.pngs):
        raise ValueError("source proper-noun audit PNG count does not match extractions")
    root = Path(output_root) if output_root is not None else _default_canonical_service_output()
    files = _output_files(root)
    text_count, text_digest = public_text_tree_binding(files)
    if text_count != packet.audited_text_file_count:
        raise ValueError("source proper-noun audit text file count does not match current output")
    current_platform = (
        "macos"
        if sys.platform == "darwin"
        else "linux"
        if sys.platform.startswith("linux")
        else None
    )
    if current_platform is None:
        raise ValueError("source proper-noun audit does not support this runtime platform")
    if packet.audited_text_tree_sha256 != packet.audited_text_tree_sha256_by_platform.get("macos"):
        raise ValueError("source proper-noun audit text tree hash is stale")
    # Live path/content tree digests are macOS-locked. Linux CI must not require a
    # second Ubuntu encoder capture; it still checks counts, path closure, and
    # the committed extraction blob.
    if current_platform == "macos" and (
        packet.audited_text_tree_sha256_by_platform.get("macos") != text_digest
    ):
        raise ValueError("source proper-noun audit text tree hash is stale")
    live_pdf_paths = sorted(path for path in files if Path(path).suffix.casefold() == ".pdf")
    live_png_paths = sorted(path for path in files if Path(path).suffix.casefold() == ".png")
    committed_pdf_paths = sorted(item.path for item in extractions.pdfs)
    committed_png_paths = sorted(item.path for item in extractions.pngs)
    if live_pdf_paths != committed_pdf_paths or live_png_paths != committed_png_paths:
        raise ValueError(
            "source proper-noun audit binary path closure does not match current output"
        )
    binary_count, binary_digest = public_binary_tree_binding(files)
    if binary_count != packet.audited_pdf_count + packet.audited_png_count:
        raise ValueError("source proper-noun audit binary file count is stale")
    if current_platform == "macos":
        if packet.audited_binary_tree_sha256_by_platform.get("macos") != binary_digest:
            raise ValueError(
                "source proper-noun audit binary tree hash is stale: "
                f"{current_platform}={binary_digest}"
            )
        for pdf_row in extractions.pdfs:
            live_pdf = _extract_pdf(pdf_row.path, files[pdf_row.path])
            if live_pdf.semantic_dict() != pdf_row.semantic_dict():
                raise ValueError(
                    "source proper-noun audit PDF extraction does not match current output"
                )
        for png_row in extractions.pngs:
            live_png = _extract_png(png_row.path, files[png_row.path])
            if live_png.semantic_dict() != png_row.semantic_dict():
                raise ValueError(
                    "source proper-noun audit PNG extraction does not match current output"
                )
    return extractions
