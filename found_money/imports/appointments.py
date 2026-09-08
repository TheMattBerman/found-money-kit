"""Read-only CSV/JSON importer for canonical ``appointments.v1`` evidence."""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from found_money.contracts.appointments import AppointmentV1, AppointmentsV1
from found_money.contracts.source import SourceReceiptV1
from found_money.receipts import (
    _atomic_write_bytes,
    _validate_relative_under_root,
    build_source_receipt,
)

_CSV_HEADERS = ("appointment_id", "customer_id", "scheduled_at", "status")


class AppointmentsImportError(ValueError):
    """A source-shape or row-specific appointment validation failure."""


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AppointmentsImportError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _csv_rows(source: bytes) -> list[tuple[int, dict[str, Any]]]:
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AppointmentsImportError("CSV must be UTF-8") from exc
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if reader.fieldnames != list(_CSV_HEADERS):
        raise AppointmentsImportError(f"CSV headers must be exactly {', '.join(_CSV_HEADERS)}")
    rows = []
    for number, row in enumerate(reader, start=2):
        if None in row:
            raise AppointmentsImportError(f"CSV row {number}: extra fields are not allowed")
        rows.append((number, row))
    return rows


def _json_rows(source: bytes) -> list[tuple[int, dict[str, Any]]]:
    try:
        payload = json.loads(source.decode("utf-8"), object_pairs_hook=_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AppointmentsImportError(f"malformed JSON: {exc}") from exc
    if isinstance(payload, dict):
        if set(payload) != {"appointments"} or not isinstance(payload["appointments"], list):
            raise AppointmentsImportError('JSON object must contain only an "appointments" array')
        items = payload["appointments"]
    elif isinstance(payload, list):
        items = payload
    else:
        raise AppointmentsImportError(
            'JSON must be an array or an object containing "appointments"'
        )
    rows = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise AppointmentsImportError(f"JSON item {index}: must be an object")
        rows.append((index, item))
    return rows


def _validated(rows: list[tuple[int, dict[str, Any]]], source_kind: str) -> AppointmentsV1:
    appointments: list[AppointmentV1] = []
    seen: set[str] = set()
    for position, row in rows:
        label = f"CSV row {position}" if source_kind == "CSV" else f"JSON item {position}"
        try:
            appointment = AppointmentV1.model_validate(row)
        except ValidationError as exc:
            details = "; ".join(
                f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                for error in exc.errors()
            )
            raise AppointmentsImportError(f"{label}: {details}") from exc
        if appointment.appointment_id in seen:
            raise AppointmentsImportError(
                f"{label}: duplicate appointment_id {appointment.appointment_id!r}"
            )
        seen.add(appointment.appointment_id)
        appointments.append(appointment)
    return AppointmentsV1(appointments=appointments)


def parse_appointments_file_bytes(source: bytes, *, filename: str) -> AppointmentsV1:
    """Validate CSV or JSON appointment bytes without writing caller-owned output."""
    suffix = Path(filename).suffix.lower()
    if suffix not in {".csv", ".json"}:
        raise AppointmentsImportError("input file must have a .csv or .json extension")
    rows = _csv_rows(source) if suffix == ".csv" else _json_rows(source)
    return _validated(rows, "CSV" if suffix == ".csv" else "JSON")


def parse_appointments_canonical_json(data: bytes) -> AppointmentsV1:
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("canonical JSON must be bytes")
    try:
        payload = json.loads(bytes(data).decode("utf-8"), object_pairs_hook=_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AppointmentsImportError(f"malformed JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise AppointmentsImportError("canonical JSON root must be an object")
    try:
        result = AppointmentsV1.model_validate(payload)
    except ValidationError as exc:
        raise AppointmentsImportError(str(exc)) from exc
    if result.to_canonical_json() != bytes(data):
        raise AppointmentsImportError("serialized bytes are not exactly canonical")
    return result


def load_appointments_file(
    input_root: Path | str,
    input_path: str,
    output_root: Path | str,
    *,
    normalized_path: str = "appointments/appointments.v1.json",
    receipt_path: str = "appointments/source-receipt.json",
    retrieved_at: datetime | None = None,
) -> tuple[AppointmentsV1, SourceReceiptV1, Path, Path]:
    """Validate a complete caller-root-relative appointment file before writing it."""
    input_file = _validate_relative_under_root(Path(input_root), input_path)
    suffix = input_file.suffix.lower()
    if suffix not in {".csv", ".json"}:
        raise AppointmentsImportError("input file must have a .csv or .json extension")
    try:
        source = input_file.read_bytes()
    except OSError as exc:
        raise AppointmentsImportError(f"unable to read input file: {input_path}") from exc
    rows = _csv_rows(source) if suffix == ".csv" else _json_rows(source)
    appointments = _validated(rows, "CSV" if suffix == ".csv" else "JSON")
    root = Path(output_root)
    normalized = _validate_relative_under_root(root, normalized_path)
    receipt_destination = _validate_relative_under_root(root, receipt_path)
    if normalized == receipt_destination:
        raise AppointmentsImportError("normalized output and receipt paths must differ")
    receipt = build_source_receipt(
        source_type="appointments",
        connector_schema_version="appointments.v1",
        retrieved_at=retrieved_at or datetime.now(timezone.utc),
        locator_kind="input_path",
        locator=input_path,
        content=source,
        page_or_row_count=len(rows),
        record_count=len(appointments.appointments),
    )
    _atomic_write_bytes(normalized, appointments.to_canonical_json())
    _atomic_write_bytes(receipt_destination, receipt.to_canonical_json())
    return appointments, receipt, normalized, receipt_destination
