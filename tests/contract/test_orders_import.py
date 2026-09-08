"""FM-011 orders.v1 validated file-import contract tests."""

from __future__ import annotations

import hashlib
import json
import socket
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from found_money.contracts.orders import OrderV1, OrdersV1
from found_money.imports.orders import (
    OrdersImportError,
    load_orders_file,
    parse_orders_canonical_json,
)
from found_money.receipts import parse_canonical_json

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "saas" / "imports" / "orders"
WHEN = datetime(2026, 7, 29, 18, 0, tzinfo=timezone.utc)


def _copy_fixture(tmp_path: Path, name: str) -> Path:
    source = FIXTURES / name
    destination = tmp_path / name
    destination.write_bytes(source.read_bytes())
    return destination


def test_csv_and_json_import_to_canonical_orders_and_receipt(tmp_path):
    _copy_fixture(tmp_path, "valid.csv")
    orders, receipt, output, receipt_path = load_orders_file(
        tmp_path, "valid.csv", tmp_path / "output", retrieved_at=WHEN
    )
    assert orders.schema_version == "orders.v1"
    assert [order.order_id for order in orders.orders] == ["order_001", "order_002"]
    assert orders.orders[0].ordered_at.isoformat() == "2026-07-29T18:45:30.123000+00:00"
    assert orders.orders[0].currency == "usd"
    assert orders.orders[0].total_minor == 12345
    assert parse_orders_canonical_json(output.read_bytes()) == orders
    assert receipt.locator == "valid.csv"
    assert receipt.page_or_row_count == receipt.record_count == 2
    assert receipt.content_hash == hashlib.sha256((tmp_path / "valid.csv").read_bytes()).hexdigest()
    assert parse_canonical_json(receipt_path.read_bytes()) == receipt

    _copy_fixture(tmp_path, "valid.json")
    json_orders, _, json_output, _ = load_orders_file(
        tmp_path,
        "valid.json",
        tmp_path / "json-output",
        retrieved_at=WHEN,
    )
    assert json_orders.orders[0].order_id == "order_003"
    assert json.loads(json_output.read_text(encoding="utf-8"))["schema_version"] == "orders.v1"


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        (
            {
                "order_id": " ",
                "customer_id": "c",
                "ordered_at": "2026-01-01T00:00:00Z",
                "currency": "usd",
                "total_minor": 1,
            },
            "order_id",
        ),
        (
            {
                "order_id": "o",
                "customer_id": "c",
                "ordered_at": "2026-01-01T00:00:00",
                "currency": "usd",
                "total_minor": 1,
            },
            "timezone-aware",
        ),
        (
            {
                "order_id": "o",
                "customer_id": "c",
                "ordered_at": "bad",
                "currency": "usd",
                "total_minor": 1,
            },
            "ISO-8601",
        ),
        (
            {
                "order_id": "o",
                "customer_id": "c",
                "ordered_at": "2026-01-01T00:00:00Z",
                "currency": "zzz",
                "total_minor": 1,
            },
            "unsupported currency",
        ),
        (
            {
                "order_id": "o",
                "customer_id": "c",
                "ordered_at": "2026-01-01T00:00:00Z",
                "currency": "usd",
                "total_minor": "1.5",
            },
            "non-negative integer",
        ),
        (
            {
                "order_id": "o",
                "customer_id": "c",
                "ordered_at": "2026-01-01T00:00:00Z",
                "currency": "usd",
                "total_minor": -1,
            },
            "non-negative integer",
        ),
        (
            {
                "order_id": "o",
                "customer_id": "c",
                "ordered_at": "2026-01-01T00:00:00Z",
                "currency": "usd",
                "total_minor": 1,
                "extra": 1,
            },
            "extra",
        ),
    ],
)
def test_order_contract_rejects_invalid_values(payload, match):
    with pytest.raises(ValidationError, match=match):
        OrderV1.model_validate(payload)


def test_duplicate_and_row_index_diagnostics_leave_no_output(tmp_path):
    _copy_fixture(tmp_path, "invalid_late.csv")
    with pytest.raises(OrdersImportError, match=r"CSV row 3:.*timezone-aware"):
        load_orders_file(tmp_path, "invalid_late.csv", tmp_path / "output", retrieved_at=WHEN)
    assert not (tmp_path / "output").exists()

    (tmp_path / "duplicate.json").write_text(
        json.dumps(
            [
                {
                    "order_id": "o",
                    "customer_id": "c",
                    "ordered_at": "2026-01-01T00:00:00Z",
                    "currency": "usd",
                    "total_minor": 1,
                },
                {
                    "order_id": "o",
                    "customer_id": "c2",
                    "ordered_at": "2026-01-01T00:00:00Z",
                    "currency": "usd",
                    "total_minor": 2,
                },
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(OrdersImportError, match=r"JSON item 1: duplicate order_id"):
        load_orders_file(tmp_path, "duplicate.json", tmp_path / "json-output", retrieved_at=WHEN)
    assert not (tmp_path / "json-output").exists()


@pytest.mark.parametrize("bad_path", ["../valid.csv", "/tmp/valid.csv", r"..\\valid.csv"])
def test_input_and_output_path_escapes_are_rejected_before_writes(tmp_path, bad_path):
    _copy_fixture(tmp_path, "valid.csv")
    output = tmp_path / "output"
    with pytest.raises(ValueError):
        load_orders_file(tmp_path, bad_path, output, retrieved_at=WHEN)
    assert not output.exists()

    with pytest.raises(ValueError):
        load_orders_file(
            tmp_path, "valid.csv", output, normalized_path="../orders.json", retrieved_at=WHEN
        )
    assert not output.exists()


def test_symlink_escape_and_no_socket_or_credential_access(tmp_path, monkeypatch):
    _copy_fixture(tmp_path, "valid.csv")
    outside = tmp_path / "outside"
    outside.mkdir()
    output = tmp_path / "output"
    output.mkdir()
    (output / "escape").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="escapes"):
        load_orders_file(tmp_path, "valid.csv", output, normalized_path="escape/orders.json")
    assert not list(outside.iterdir())

    def blocked_socket(*args, **kwargs):
        raise AssertionError("network access is forbidden")

    monkeypatch.setattr(socket, "socket", blocked_socket)
    orders, _, output_path, _ = load_orders_file(
        tmp_path, "valid.csv", tmp_path / "safe-output", retrieved_at=WHEN
    )
    assert orders.orders and output_path.is_relative_to(tmp_path / "safe-output")


def test_orders_document_rejects_duplicates_and_requires_canonical_bytes():
    with pytest.raises(ValidationError, match="unique"):
        OrdersV1.model_validate(
            {
                "orders": [
                    {
                        "order_id": "o",
                        "customer_id": "c",
                        "ordered_at": "2026-01-01T00:00:00Z",
                        "currency": "usd",
                        "total_minor": 1,
                    },
                    {
                        "order_id": "o",
                        "customer_id": "c2",
                        "ordered_at": "2026-01-01T00:00:00Z",
                        "currency": "usd",
                        "total_minor": 2,
                    },
                ]
            }
        )
