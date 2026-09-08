"""Read-only CSV/JSON importer for canonical ``orders.v1`` evidence."""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from found_money.contracts.orders import OrderV1, OrdersV1
from found_money.contracts.source import SourceReceiptV1
from found_money.receipts import (
    _atomic_write_bytes,
    _validate_relative_under_root,
    build_source_receipt,
)

_CSV_HEADERS = ("order_id", "customer_id", "ordered_at", "currency", "total_minor")


class OrdersImportError(ValueError):
    """A source-shape or row-specific validation failure."""


def _object_pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise OrdersImportError(f"duplicate JSON object key: {key}")
        output[key] = value
    return output


def _csv_rows(source: bytes) -> list[tuple[int, dict[str, Any]]]:
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise OrdersImportError("CSV must be UTF-8") from exc
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if reader.fieldnames != list(_CSV_HEADERS):
        raise OrdersImportError(f"CSV headers must be exactly {', '.join(_CSV_HEADERS)}")
    rows: list[tuple[int, dict[str, Any]]] = []
    for row_number, row in enumerate(reader, start=2):
        if None in row:
            raise OrdersImportError(f"CSV row {row_number}: extra fields are not allowed")
        rows.append((row_number, row))
    return rows


def _json_rows(source: bytes) -> list[tuple[int, dict[str, Any]]]:
    try:
        payload = json.loads(source.decode("utf-8"), object_pairs_hook=_object_pairs_hook)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OrdersImportError(f"malformed JSON: {exc}") from exc
    if isinstance(payload, dict):
        if set(payload) != {"orders"} or not isinstance(payload["orders"], list):
            raise OrdersImportError('JSON object must contain only an "orders" array')
        items = payload["orders"]
    elif isinstance(payload, list):
        items = payload
    else:
        raise OrdersImportError('JSON must be an array or an object containing "orders"')
    rows: list[tuple[int, dict[str, Any]]] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise OrdersImportError(f"JSON item {index}: must be an object")
        rows.append((index, item))
    return rows


def _validated_orders(rows: list[tuple[int, dict[str, Any]]], *, source_kind: str) -> OrdersV1:
    orders: list[OrderV1] = []
    seen: set[str] = set()
    for position, row in rows:
        label = f"CSV row {position}" if source_kind == "CSV" else f"JSON item {position}"
        try:
            order = OrderV1.model_validate(row)
        except ValidationError as exc:
            details = "; ".join(
                f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                for error in exc.errors()
            )
            raise OrdersImportError(f"{label}: {details}") from exc
        if order.order_id in seen:
            raise OrdersImportError(f"{label}: duplicate order_id {order.order_id!r}")
        seen.add(order.order_id)
        orders.append(order)
    return OrdersV1(orders=orders)


def parse_orders_file_bytes(source: bytes, *, filename: str) -> OrdersV1:
    """Validate CSV or JSON order bytes without writing caller-owned output."""
    suffix = Path(filename).suffix.lower()
    if suffix not in {".csv", ".json"}:
        raise OrdersImportError("input file must have a .csv or .json extension")
    rows = _csv_rows(source) if suffix == ".csv" else _json_rows(source)
    return _validated_orders(rows, source_kind="CSV" if suffix == ".csv" else "JSON")


def parse_orders_canonical_json(data: bytes) -> OrdersV1:
    """Parse and verify exactly canonical ``orders.v1`` bytes."""
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("canonical JSON must be bytes")
    try:
        payload = json.loads(bytes(data).decode("utf-8"), object_pairs_hook=_object_pairs_hook)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OrdersImportError(f"malformed JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise OrdersImportError("canonical JSON root must be an object")
    try:
        orders = OrdersV1.model_validate(payload)
    except ValidationError as exc:
        raise OrdersImportError(str(exc)) from exc
    if orders.to_canonical_json() != bytes(data):
        raise OrdersImportError("serialized bytes are not exactly canonical")
    return orders


def load_orders_file(
    input_root: Path | str,
    input_path: str,
    output_root: Path | str,
    *,
    normalized_path: str = "orders/orders.v1.json",
    receipt_path: str = "orders/source-receipt.json",
    retrieved_at: datetime | None = None,
) -> tuple[OrdersV1, SourceReceiptV1, Path, Path]:
    """Validate a complete caller-root-relative orders file, then write it atomically.

    Input and output paths are intentionally relative to caller-owned roots so the
    receipt can preserve a portable source locator without exposing local paths.
    """
    input_file = _validate_relative_under_root(Path(input_root), input_path)
    suffix = input_file.suffix.lower()
    if suffix not in {".csv", ".json"}:
        raise OrdersImportError("input file must have a .csv or .json extension")
    try:
        source_bytes = input_file.read_bytes()
    except OSError as exc:
        raise OrdersImportError(f"unable to read input file: {input_path}") from exc

    rows = _csv_rows(source_bytes) if suffix == ".csv" else _json_rows(source_bytes)
    orders = _validated_orders(rows, source_kind="CSV" if suffix == ".csv" else "JSON")

    # Validate every output target after all source validation and before any write.
    root = Path(output_root)
    normalized_destination = _validate_relative_under_root(root, normalized_path)
    receipt_destination = _validate_relative_under_root(root, receipt_path)
    if normalized_destination == receipt_destination:
        raise OrdersImportError("normalized output and receipt paths must differ")

    receipt = build_source_receipt(
        source_type="orders",
        connector_schema_version="orders.v1",
        retrieved_at=retrieved_at or datetime.now(timezone.utc),
        locator_kind="input_path",
        locator=input_path,
        content=source_bytes,
        page_or_row_count=len(rows),
        record_count=len(orders.orders),
    )
    _atomic_write_bytes(normalized_destination, orders.to_canonical_json())
    _atomic_write_bytes(receipt_destination, receipt.to_canonical_json())
    return orders, receipt, normalized_destination, receipt_destination
