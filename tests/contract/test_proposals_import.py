from __future__ import annotations

import hashlib
import json
import socket
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from found_money.contracts.proposals import ProposalV1, ProposalsV1
from found_money.imports.proposals import (
    ProposalsImportError,
    load_proposals_file,
    parse_proposals_canonical_json,
)
from found_money.receipts import parse_canonical_json

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/saas/imports/proposals"
WHEN = datetime(2026, 7, 29, 18, tzinfo=timezone.utc)


def _copy(tmp_path: Path, name: str) -> None:
    (tmp_path / name).write_bytes((FIXTURES / name).read_bytes())


def test_csv_json_canonical_bytes_and_receipt(tmp_path):
    _copy(tmp_path, "valid.csv")
    result, receipt, output, receipt_path = load_proposals_file(
        tmp_path, "valid.csv", tmp_path / "out", retrieved_at=WHEN
    )
    assert result.schema_version == "proposals.v1"
    assert (
        result.proposals[0].proposal_id == "proposal_001" and result.proposals[0].status == "sent"
    )
    assert result.proposals[0].amount_minor == 12345 and result.proposals[0].currency == "usd"
    assert parse_proposals_canonical_json(output.read_bytes()) == result
    assert parse_canonical_json(receipt_path.read_bytes()) == receipt
    assert receipt.locator == "valid.csv" and receipt.page_or_row_count == receipt.record_count == 2
    assert receipt.content_hash == hashlib.sha256((tmp_path / "valid.csv").read_bytes()).hexdigest()
    _copy(tmp_path, "valid.json")
    assert (
        load_proposals_file(tmp_path, "valid.json", tmp_path / "json", retrieved_at=WHEN)[0]
        .proposals[0]
        .currency
        == "eur"
    )
    _copy(tmp_path, "valid-array.json")
    assert (
        load_proposals_file(
            tmp_path, "valid-array.json", tmp_path / "json-array", retrieved_at=WHEN
        )[0]
        .proposals[0]
        .currency
        == "usd"
    )


@pytest.mark.parametrize(
    "payload, match",
    [
        (
            {
                "proposal_id": " ",
                "customer_id": "c",
                "proposed_at": "2026-01-01T00:00:00Z",
                "status": "sent",
                "amount_minor": 1,
                "currency": "usd",
            },
            "proposal_id",
        ),
        (
            {
                "proposal_id": "p",
                "customer_id": " ",
                "proposed_at": "2026-01-01T00:00:00Z",
                "status": "sent",
                "amount_minor": 1,
                "currency": "usd",
            },
            "customer_id",
        ),
        (
            {
                "proposal_id": "p",
                "customer_id": "c",
                "proposed_at": "2026-01-01T00:00:00",
                "status": "sent",
                "amount_minor": 1,
                "currency": "usd",
            },
            "timezone-aware",
        ),
        (
            {
                "proposal_id": "p",
                "customer_id": "c",
                "proposed_at": "bad",
                "status": "sent",
                "amount_minor": 1,
                "currency": "usd",
            },
            "ISO-8601",
        ),
        (
            {
                "proposal_id": "p",
                "customer_id": "c",
                "proposed_at": "2026-01-01T00:00:00Z",
                "status": "other",
                "amount_minor": 1,
                "currency": "usd",
            },
            "unsupported",
        ),
        (
            {
                "proposal_id": "p",
                "customer_id": "c",
                "proposed_at": "2026-01-01T00:00:00Z",
                "status": "sent",
                "amount_minor": "1.5",
                "currency": "usd",
            },
            "non-negative integer",
        ),
        (
            {
                "proposal_id": "p",
                "customer_id": "c",
                "proposed_at": "2026-01-01T00:00:00Z",
                "status": "sent",
                "amount_minor": -1,
                "currency": "usd",
            },
            "non-negative integer",
        ),
        (
            {
                "proposal_id": "p",
                "customer_id": "c",
                "proposed_at": "2026-01-01T00:00:00Z",
                "status": "sent",
                "amount_minor": 1,
                "currency": "zzz",
            },
            "unsupported currency",
        ),
        (
            {
                "proposal_id": "p",
                "customer_id": "c",
                "proposed_at": "2026-01-01T00:00:00Z",
                "status": "sent",
                "amount_minor": 1,
                "currency": "usd",
                "extra": 1,
            },
            "extra",
        ),
    ],
)
def test_contract_rejections(payload, match):
    with pytest.raises(ValidationError, match=match):
        ProposalV1.model_validate(payload)


@pytest.mark.parametrize(
    ("name", "payload", "match"),
    [
        (
            "duplicate.csv",
            "proposal_id,customer_id,proposed_at,status,amount_minor,currency\n"
            "proposal_1,customer_1,2026-01-01T00:00:00Z,sent,1,usd\n"
            "proposal_1,customer_2,2026-01-02T00:00:00Z,accepted,2,usd\n",
            r"CSV row 3: duplicate proposal_id 'proposal_1'",
        ),
        (
            "duplicate.json",
            json.dumps(
                [
                    {
                        "proposal_id": "proposal_1",
                        "customer_id": "customer_1",
                        "proposed_at": "2026-01-01T00:00:00Z",
                        "status": "sent",
                        "amount_minor": 1,
                        "currency": "usd",
                    },
                    {
                        "proposal_id": "proposal_1",
                        "customer_id": "customer_2",
                        "proposed_at": "2026-01-02T00:00:00Z",
                        "status": "accepted",
                        "amount_minor": 2,
                        "currency": "usd",
                    },
                ]
            ),
            r"JSON item 1: duplicate proposal_id 'proposal_1'",
        ),
    ],
)
def test_importer_duplicate_id_diagnostics_leave_no_output(tmp_path, name, payload, match):
    (tmp_path / name).write_text(payload, encoding="utf-8")
    output = tmp_path / "output"
    with pytest.raises(ProposalsImportError, match=match):
        load_proposals_file(tmp_path, name, output, retrieved_at=WHEN)
    assert not output.exists()


@pytest.mark.parametrize("bad_path", ["../valid.csv", "/tmp/valid.csv", r"..\valid.csv"])
def test_input_path_escapes_leave_output_and_outside_unchanged(tmp_path, bad_path):
    _copy(tmp_path, "valid.csv")
    output, outside = tmp_path / "output", tmp_path / "outside"
    outside.mkdir()
    before_outside = list(outside.iterdir())
    with pytest.raises(ValueError):
        load_proposals_file(tmp_path, bad_path, output, retrieved_at=WHEN)
    assert not output.exists()
    assert list(outside.iterdir()) == before_outside


def test_traversal_output_path_leaves_output_and_outside_unchanged(tmp_path):
    _copy(tmp_path, "valid.csv")
    output, outside = tmp_path / "output", tmp_path / "outside"
    outside.mkdir()
    before_outside = list(outside.iterdir())
    with pytest.raises(ValueError):
        load_proposals_file(
            tmp_path, "valid.csv", output, normalized_path="../proposals.json", retrieved_at=WHEN
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
        load_proposals_file(
            tmp_path, "valid.csv", output, normalized_path="escape/x.json", retrieved_at=WHEN
        )
    assert list(output.iterdir()) == before_output
    assert escape.is_symlink() and escape.resolve() == outside
    assert list(outside.iterdir()) == before_outside


def test_late_invalid_and_network_safety(tmp_path, monkeypatch):
    _copy(tmp_path, "invalid_late.csv")
    with pytest.raises(ProposalsImportError, match=r"CSV row 3:.*timezone-aware"):
        load_proposals_file(tmp_path, "invalid_late.csv", tmp_path / "out", retrieved_at=WHEN)
    assert not (tmp_path / "out").exists()
    _copy(tmp_path, "valid.csv")
    monkeypatch.setattr(
        socket, "socket", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network"))
    )
    assert load_proposals_file(tmp_path, "valid.csv", tmp_path / "safe", retrieved_at=WHEN)[
        2
    ].is_relative_to(tmp_path / "safe")


def test_document_duplicate_rejected():
    row = {
        "proposal_id": "p",
        "customer_id": "c",
        "proposed_at": "2026-01-01T00:00:00Z",
        "status": "sent",
        "amount_minor": 1,
        "currency": "usd",
    }
    with pytest.raises(ValidationError, match="unique"):
        ProposalsV1(proposals=[row, row])
