"""Read-only CSV/JSON importer for canonical ``proposals.v1`` evidence."""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from found_money.contracts.proposals import ProposalV1, ProposalsV1
from found_money.contracts.source import SourceReceiptV1
from found_money.receipts import (
    _atomic_write_bytes,
    _validate_relative_under_root,
    build_source_receipt,
)

_CSV_HEADERS = ("proposal_id", "customer_id", "proposed_at", "status", "amount_minor", "currency")


class ProposalsImportError(ValueError):
    """A source-shape or row-specific proposal validation failure."""


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProposalsImportError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _csv_rows(source: bytes) -> list[tuple[int, dict[str, Any]]]:
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProposalsImportError("CSV must be UTF-8") from exc
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if reader.fieldnames != list(_CSV_HEADERS):
        raise ProposalsImportError(f"CSV headers must be exactly {', '.join(_CSV_HEADERS)}")
    rows = []
    for number, row in enumerate(reader, start=2):
        if None in row:
            raise ProposalsImportError(f"CSV row {number}: extra fields are not allowed")
        rows.append((number, row))
    return rows


def _json_rows(source: bytes) -> list[tuple[int, dict[str, Any]]]:
    try:
        payload = json.loads(source.decode("utf-8"), object_pairs_hook=_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProposalsImportError(f"malformed JSON: {exc}") from exc
    if isinstance(payload, dict):
        if set(payload) != {"proposals"} or not isinstance(payload["proposals"], list):
            raise ProposalsImportError('JSON object must contain only a "proposals" array')
        items = payload["proposals"]
    elif isinstance(payload, list):
        items = payload
    else:
        raise ProposalsImportError('JSON must be an array or an object containing "proposals"')
    rows = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ProposalsImportError(f"JSON item {index}: must be an object")
        rows.append((index, item))
    return rows


def _validated(rows: list[tuple[int, dict[str, Any]]], source_kind: str) -> ProposalsV1:
    proposals: list[ProposalV1] = []
    seen: set[str] = set()
    for position, row in rows:
        label = f"CSV row {position}" if source_kind == "CSV" else f"JSON item {position}"
        try:
            proposal = ProposalV1.model_validate(row)
        except ValidationError as exc:
            details = "; ".join(
                f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                for error in exc.errors()
            )
            raise ProposalsImportError(f"{label}: {details}") from exc
        if proposal.proposal_id in seen:
            raise ProposalsImportError(f"{label}: duplicate proposal_id {proposal.proposal_id!r}")
        seen.add(proposal.proposal_id)
        proposals.append(proposal)
    return ProposalsV1(proposals=proposals)


def parse_proposals_file_bytes(source: bytes, *, filename: str) -> ProposalsV1:
    """Validate CSV or JSON proposal bytes without writing caller-owned output."""
    suffix = Path(filename).suffix.lower()
    if suffix not in {".csv", ".json"}:
        raise ProposalsImportError("input file must have a .csv or .json extension")
    rows = _csv_rows(source) if suffix == ".csv" else _json_rows(source)
    return _validated(rows, "CSV" if suffix == ".csv" else "JSON")


def parse_proposals_canonical_json(data: bytes) -> ProposalsV1:
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("canonical JSON must be bytes")
    try:
        payload = json.loads(bytes(data).decode("utf-8"), object_pairs_hook=_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProposalsImportError(f"malformed JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ProposalsImportError("canonical JSON root must be an object")
    try:
        result = ProposalsV1.model_validate(payload)
    except ValidationError as exc:
        raise ProposalsImportError(str(exc)) from exc
    if result.to_canonical_json() != bytes(data):
        raise ProposalsImportError("serialized bytes are not exactly canonical")
    return result


def load_proposals_file(
    input_root: Path | str,
    input_path: str,
    output_root: Path | str,
    *,
    normalized_path: str = "proposals/proposals.v1.json",
    receipt_path: str = "proposals/source-receipt.json",
    retrieved_at: datetime | None = None,
) -> tuple[ProposalsV1, SourceReceiptV1, Path, Path]:
    """Validate a complete caller-root-relative proposal file before writing it."""
    input_file = _validate_relative_under_root(Path(input_root), input_path)
    suffix = input_file.suffix.lower()
    if suffix not in {".csv", ".json"}:
        raise ProposalsImportError("input file must have a .csv or .json extension")
    try:
        source = input_file.read_bytes()
    except OSError as exc:
        raise ProposalsImportError(f"unable to read input file: {input_path}") from exc
    rows = _csv_rows(source) if suffix == ".csv" else _json_rows(source)
    proposals = _validated(rows, "CSV" if suffix == ".csv" else "JSON")
    root = Path(output_root)
    normalized = _validate_relative_under_root(root, normalized_path)
    receipt_destination = _validate_relative_under_root(root, receipt_path)
    if normalized == receipt_destination:
        raise ProposalsImportError("normalized output and receipt paths must differ")
    receipt = build_source_receipt(
        source_type="proposals",
        connector_schema_version="proposals.v1",
        retrieved_at=retrieved_at or datetime.now(timezone.utc),
        locator_kind="input_path",
        locator=input_path,
        content=source,
        page_or_row_count=len(rows),
        record_count=len(proposals.proposals),
    )
    _atomic_write_bytes(normalized, proposals.to_canonical_json())
    _atomic_write_bytes(receipt_destination, receipt.to_canonical_json())
    return proposals, receipt, normalized, receipt_destination
