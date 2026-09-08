from __future__ import annotations

import hashlib
import json
import socket
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from found_money.contracts.appointments import AppointmentV1, AppointmentsV1
from found_money.imports.appointments import (
    AppointmentsImportError,
    load_appointments_file,
    parse_appointments_canonical_json,
)
from found_money.receipts import parse_canonical_json

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/saas/imports/appointments"
WHEN = datetime(2026, 7, 29, 18, tzinfo=timezone.utc)


def _copy(tmp_path: Path, name: str) -> None:
    (tmp_path / name).write_bytes((FIXTURES / name).read_bytes())


def test_csv_json_canonical_bytes_and_receipt(tmp_path):
    _copy(tmp_path, "valid.csv")
    result, receipt, output, receipt_path = load_appointments_file(
        tmp_path, "valid.csv", tmp_path / "out", retrieved_at=WHEN
    )
    assert result.schema_version == "appointments.v1"
    assert result.appointments[0].appointment_id == "appointment_001"
    assert result.appointments[0].scheduled_at.isoformat() == "2026-07-29T18:45:30.123000+00:00"
    assert result.appointments[0].status == "scheduled"
    assert parse_appointments_canonical_json(output.read_bytes()) == result
    assert parse_canonical_json(receipt_path.read_bytes()) == receipt
    assert receipt.locator == "valid.csv" and receipt.page_or_row_count == receipt.record_count == 2
    assert receipt.content_hash == hashlib.sha256((tmp_path / "valid.csv").read_bytes()).hexdigest()
    _copy(tmp_path, "valid.json")
    assert (
        load_appointments_file(tmp_path, "valid.json", tmp_path / "json", retrieved_at=WHEN)[0]
        .appointments[0]
        .status
        == "no_show"
    )
    _copy(tmp_path, "valid-array.json")
    assert (
        load_appointments_file(
            tmp_path, "valid-array.json", tmp_path / "json-array", retrieved_at=WHEN
        )[0]
        .appointments[0]
        .status
        == "completed"
    )


@pytest.mark.parametrize(
    "payload, match",
    [
        (
            {
                "appointment_id": " ",
                "customer_id": "c",
                "scheduled_at": "2026-01-01T00:00:00Z",
                "status": "scheduled",
            },
            "appointment_id",
        ),
        (
            {
                "appointment_id": "a",
                "customer_id": " ",
                "scheduled_at": "2026-01-01T00:00:00Z",
                "status": "scheduled",
            },
            "customer_id",
        ),
        (
            {
                "appointment_id": "a",
                "customer_id": "c",
                "scheduled_at": "2026-01-01T00:00:00",
                "status": "scheduled",
            },
            "timezone-aware",
        ),
        (
            {
                "appointment_id": "a",
                "customer_id": "c",
                "scheduled_at": "bad",
                "status": "scheduled",
            },
            "ISO-8601",
        ),
        (
            {
                "appointment_id": "a",
                "customer_id": "c",
                "scheduled_at": "2026-01-01T00:00:00Z",
                "status": " ",
            },
            "non-empty",
        ),
        (
            {
                "appointment_id": "a",
                "customer_id": "c",
                "scheduled_at": "2026-01-01T00:00:00Z",
                "status": "other",
            },
            "unsupported",
        ),
        (
            {
                "appointment_id": "a",
                "customer_id": "c",
                "scheduled_at": "2026-01-01T00:00:00Z",
                "status": "scheduled",
                "extra": 1,
            },
            "extra",
        ),
    ],
)
def test_contract_rejections(payload, match):
    with pytest.raises(ValidationError, match=match):
        AppointmentV1.model_validate(payload)


@pytest.mark.parametrize(
    ("name", "payload", "match"),
    [
        (
            "duplicate.csv",
            "appointment_id,customer_id,scheduled_at,status\n"
            "appointment_1,customer_1,2026-01-01T00:00:00Z,scheduled\n"
            "appointment_1,customer_2,2026-01-02T00:00:00Z,completed\n",
            r"CSV row 3: duplicate appointment_id 'appointment_1'",
        ),
        (
            "duplicate.json",
            json.dumps(
                [
                    {
                        "appointment_id": "appointment_1",
                        "customer_id": "customer_1",
                        "scheduled_at": "2026-01-01T00:00:00Z",
                        "status": "scheduled",
                    },
                    {
                        "appointment_id": "appointment_1",
                        "customer_id": "customer_2",
                        "scheduled_at": "2026-01-02T00:00:00Z",
                        "status": "completed",
                    },
                ]
            ),
            r"JSON item 1: duplicate appointment_id 'appointment_1'",
        ),
    ],
)
def test_importer_duplicate_id_diagnostics_leave_no_output(tmp_path, name, payload, match):
    (tmp_path / name).write_text(payload, encoding="utf-8")
    output = tmp_path / "output"
    with pytest.raises(AppointmentsImportError, match=match):
        load_appointments_file(tmp_path, name, output, retrieved_at=WHEN)
    assert not output.exists()


@pytest.mark.parametrize("bad_path", ["../valid.csv", "/tmp/valid.csv", r"..\valid.csv"])
def test_input_path_escapes_leave_output_and_outside_unchanged(tmp_path, bad_path):
    _copy(tmp_path, "valid.csv")
    output, outside = tmp_path / "output", tmp_path / "outside"
    outside.mkdir()
    before_outside = list(outside.iterdir())
    with pytest.raises(ValueError):
        load_appointments_file(tmp_path, bad_path, output, retrieved_at=WHEN)
    assert not output.exists()
    assert list(outside.iterdir()) == before_outside


def test_traversal_output_path_leaves_output_and_outside_unchanged(tmp_path):
    _copy(tmp_path, "valid.csv")
    output, outside = tmp_path / "output", tmp_path / "outside"
    outside.mkdir()
    before_outside = list(outside.iterdir())
    with pytest.raises(ValueError):
        load_appointments_file(
            tmp_path, "valid.csv", output, normalized_path="../appointments.json", retrieved_at=WHEN
        )
    assert not output.exists()
    assert list(outside.iterdir()) == before_outside


def test_symlink_escape_leaves_output_and_outside_unchanged(tmp_path):
    _copy(tmp_path, "valid.csv")
    outside, output = tmp_path / "outside", tmp_path / "output"
    outside.mkdir()
    output.mkdir()
    escape = output / "escape"
    escape.symlink_to(outside, target_is_directory=True)
    before_output = list(output.iterdir())
    before_outside = list(outside.iterdir())
    with pytest.raises(ValueError, match="escapes"):
        load_appointments_file(
            tmp_path, "valid.csv", output, normalized_path="escape/x.json", retrieved_at=WHEN
        )
    assert list(output.iterdir()) == before_output
    assert escape.is_symlink() and escape.resolve() == outside
    assert list(outside.iterdir()) == before_outside


def test_late_invalid_and_network_safety(tmp_path, monkeypatch):
    _copy(tmp_path, "invalid_late.csv")
    with pytest.raises(AppointmentsImportError, match=r"CSV row 3:.*timezone-aware"):
        load_appointments_file(tmp_path, "invalid_late.csv", tmp_path / "out", retrieved_at=WHEN)
    assert not (tmp_path / "out").exists()
    _copy(tmp_path, "valid.csv")
    monkeypatch.setattr(
        socket, "socket", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network"))
    )
    assert load_appointments_file(tmp_path, "valid.csv", tmp_path / "safe", retrieved_at=WHEN)[
        2
    ].is_relative_to(tmp_path / "safe")


def test_document_duplicate_rejected():
    row = {
        "appointment_id": "a",
        "customer_id": "c",
        "scheduled_at": "2026-01-01T00:00:00Z",
        "status": "scheduled",
    }
    with pytest.raises(ValidationError, match="unique"):
        AppointmentsV1(appointments=[row, row])
