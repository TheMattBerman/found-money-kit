"""FM-016 multi-currency and zero/three-decimal value contract tests."""

from __future__ import annotations

import ast
import json
import socket
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from found_money.contracts.events import RecoveryCandidateSetV1, RecoveryCandidateV1
from found_money.contracts.value import (
    CURRENCY_EXPONENTS,
    SUPPORTED_CURRENCIES,
    ContributionLedgerV1,
    ContributionV1,
    currency_exponent,
    format_major_units,
    normalize_currency,
    normalize_minor_units,
)
from found_money.events import detect_failed_payments
from found_money.identity import build_identity_graph, normalize_source_records
from found_money.map import (
    build_money_map,
    build_thin_slice_money_map,
    public_money_map_projection,
    write_money_map,
    write_public_money_map_projection,
)
from found_money.value import (
    build_contribution_ledger,
    public_contribution_projection,
    totals_by_currency,
    write_contribution_ledger,
    write_public_contribution_projection,
)

ROOT = Path(__file__).resolve().parents[2]
MAP_FIXTURES = ROOT / "tests" / "fixtures" / "saas" / "map"
EVENT_FIXTURES = ROOT / "tests" / "fixtures" / "saas" / "events"
VALUE_MODULE = ROOT / "found_money" / "value" / "__init__.py"
MAP_MODULE = ROOT / "found_money" / "map" / "__init__.py"
VALUE_CONTRACT = ROOT / "found_money" / "contracts" / "value.py"


def _list_tree(root: Path) -> list[str]:
    if not root.exists():
        return []
    return sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() or path.is_dir()
    )


def _load_map_fixture(name: str) -> tuple[ContributionLedgerV1, RecoveryCandidateSetV1]:
    payload = json.loads((MAP_FIXTURES / name).read_text(encoding="utf-8"))
    run_id = payload["run_id"]
    contributions = [ContributionV1.model_validate(item) for item in payload["contributions"]]
    candidates = [RecoveryCandidateV1.model_validate(item) for item in payload["candidates"]]
    ledger = ContributionLedgerV1.model_validate(
        {
            "schema_version": "contribution-ledger.v1",
            "run_id": run_id,
            "built_at": "2026-07-29T18:00:15.000Z",
            "contributions": [item.model_dump(mode="json") for item in contributions],
        }
    )
    candidate_set = RecoveryCandidateSetV1.model_validate(
        {
            "schema_version": "recovery-candidate.v1",
            "run_id": run_id,
            "built_at": "2026-07-29T18:00:10.000Z",
            "candidates": [item.model_dump(mode="json") for item in candidates],
        }
    )
    return ledger, candidate_set


def _graph_for_stripe(stripe: dict):
    nodes = normalize_source_records({"stripe": stripe})
    return build_identity_graph(nodes, run_id="run_currency_missing")


def _module_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_t1_currency_metadata_exponents():
    assert SUPPORTED_CURRENCIES == frozenset({"usd", "eur", "gbp", "cad", "aud", "jpy", "kwd"})
    assert CURRENCY_EXPONENTS == {
        "aud": 2,
        "cad": 2,
        "eur": 2,
        "gbp": 2,
        "jpy": 0,
        "kwd": 3,
        "usd": 2,
    }
    assert list(CURRENCY_EXPONENTS) == sorted(CURRENCY_EXPONENTS)
    assert currency_exponent("JPY") == 0
    assert currency_exponent(" usd ") == 2
    assert currency_exponent("kwd") == 3
    for code, exponent in CURRENCY_EXPONENTS.items():
        assert currency_exponent(code) == exponent


@pytest.mark.parametrize(
    ("currency", "amount_minor", "expected"),
    [
        ("usd", 1234, "12.34"),
        ("USD", "1234", "12.34"),
        ("jpy", 1234, "1234"),
        ("JPY", Decimal(1234), "1234"),
        ("kwd", 1234, "1.234"),
        ("kwd", "1234", "1.234"),
        ("eur", 100, "1.00"),
        ("usd", 0, "0.00"),
        ("jpy", 0, "0"),
        ("kwd", 0, "0.000"),
    ],
)
def test_t2_decimal_only_major_unit_formatting(currency, amount_minor, expected):
    formatted = format_major_units(amount_minor, currency)
    assert formatted == expected
    assert isinstance(formatted, str)
    # Prove Decimal path: reconstructing via Decimal must match exactly.
    assert format(Decimal(formatted), "f") == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("usd", "usd"),
        (" USD ", "usd"),
        ("Eur", "eur"),
        (" gbp", "gbp"),
        ("CAD", "cad"),
        ("aud ", "aud"),
        ("jpy", "jpy"),
        ("KWD", "kwd"),
    ],
)
def test_t3_currency_normalization_accepts_boundary_whitespace_and_case(raw, expected):
    assert normalize_currency(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        None,
        123,
        True,
        1.0,
        "",
        "us",
        "usdd",
        "zzz",
        "us$",
        "u s",
        "us-d",
    ],
)
def test_t3_currency_normalization_rejects_invalid(raw):
    with pytest.raises(ValueError):
        normalize_currency(raw)


@pytest.mark.parametrize(
    "raw",
    [
        None,
        True,
        False,
        12.5,
        -1,
        "-1",
        "1.5",
        Decimal("1.5"),
        Decimal("-3"),
        "abc",
        "",
        [],
        {},
    ],
)
def test_t3_minor_units_reject_invalid(raw):
    with pytest.raises((ValueError, TypeError, ValidationError)):
        normalize_minor_units(raw)


def test_t3_zero_minor_units_accepted():
    assert normalize_minor_units(0) == Decimal(0)
    assert normalize_minor_units("0") == Decimal(0)
    assert normalize_minor_units(Decimal(0)) == Decimal(0)
    row = ContributionV1.model_validate(
        {
            "economic_unit_key": "stripe_invoice:inv_zero",
            "pile_id": "payment_rescue",
            "value_basis": "observed_face_value",
            "currency": "usd",
            "amount_minor": 0,
            "customer_token": "cust_zero",
            "candidate_economic_unit_key": "stripe_invoice:inv_zero",
        }
    )
    assert row.amount_minor == Decimal(0)


def test_t4_mixed_currency_totals_remain_separated():
    ledger, candidates = _load_map_fixture("mixed_currency.json")
    projection = public_contribution_projection(ledger)
    money_map = build_money_map(ledger, candidates)

    assert projection.total_minor_by_currency == {
        "eur": Decimal(2000),
        "jpy": Decimal(1234),
        "kwd": Decimal(1234),
        "usd": Decimal(1234),
    }
    assert totals_by_currency(list(ledger.contributions)) == projection.total_minor_by_currency
    assert money_map.identified_opportunity_minor == {
        "eur": Decimal(2000),
        "jpy": Decimal(1234),
        "kwd": Decimal(1234),
        "usd": Decimal(1234),
    }
    pile_by_currency = {pile.currency: pile for pile in money_map.piles}
    assert set(pile_by_currency) == {"eur", "jpy", "kwd", "usd"}
    assert pile_by_currency["usd"].selected_value_minor == Decimal(1234)
    assert pile_by_currency["eur"].selected_value_minor == Decimal(2000)
    assert pile_by_currency["jpy"].selected_value_minor == Decimal(1234)
    assert pile_by_currency["kwd"].selected_value_minor == Decimal(1234)

    private = money_map.canonical_dict()
    public = public_money_map_projection(money_map).canonical_dict()
    for payload in (private, public, projection.canonical_dict()):
        text = json.dumps(payload)
        assert "fx" not in text.lower()
        assert "exchange" not in text.lower()
        assert "combined" not in text.lower()
        assert "total_minor" not in payload or "total_minor_by_currency" in payload
    # No blended scalar headline field.
    assert "identified_opportunity" not in private
    assert isinstance(private["identified_opportunity_minor"], dict)
    assert list(private["identified_opportunity_minor"]) == ["eur", "jpy", "kwd", "usd"]


def test_t5_exact_decimal_serialization_and_currency_key_order():
    for name, currency, amount in (
        ("usd.json", "usd", "1234"),
        ("jpy.json", "jpy", "1234"),
        ("kwd.json", "kwd", "1234"),
        ("mixed_currency.json", None, None),
    ):
        ledger, candidates = _load_map_fixture(name)
        money_map = build_money_map(ledger, candidates)
        projection = public_contribution_projection(ledger)
        ledger_bytes = ledger.to_canonical_json()
        map_bytes = money_map.to_canonical_json()
        public_bytes = projection.to_canonical_json()
        assert ledger_bytes.endswith(b"\n")
        assert map_bytes.endswith(b"\n")
        assert b'"amount_minor":"' in ledger_bytes
        if currency is not None:
            assert f'"currency":"{currency}"'.encode() in ledger_bytes
            assert f'"amount_minor":"{amount}"'.encode() in ledger_bytes
            assert f'"{currency}":"{amount}"'.encode() in map_bytes
        map_payload = json.loads(map_bytes)
        assert list(map_payload["identified_opportunity_minor"]) == sorted(
            map_payload["identified_opportunity_minor"]
        )
        assert list(map_payload["basis_counts_by_currency"]) == sorted(
            map_payload["basis_counts_by_currency"]
        )
        public_payload = json.loads(public_bytes)
        assert list(public_payload["total_minor_by_currency"]) == sorted(
            public_payload["total_minor_by_currency"]
        )
        # Canonical sort_keys ordering is byte-stable.
        assert (
            json.dumps(
                json.loads(map_bytes),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            + b"\n"
            == map_bytes
        )


def test_t6_zero_observed_and_missing_unquantified_without_default():
    ledger, candidates = _load_map_fixture("zero_value.json")
    money_map = build_money_map(ledger, candidates)
    projection = public_contribution_projection(ledger)
    assert ledger.contributions[0].amount_minor == Decimal(0)
    assert ledger.contributions[0].value_basis == "observed_face_value"
    assert money_map.identified_opportunity_minor == {"usd": Decimal(0)}
    assert money_map.piles[0].selected_value_minor == Decimal(0)
    assert money_map.basis_counts_by_currency["usd"].observed_event_count == 1
    assert money_map.basis_counts_by_currency["usd"].unquantified_event_count == 0
    assert projection.total_minor_by_currency == {"usd": Decimal(0)}
    assert b'"amount_minor":"0"' in ledger.to_canonical_json()

    missing = json.loads((EVENT_FIXTURES / "missing_amount.json").read_text(encoding="utf-8"))
    graph = _graph_for_stripe(missing)
    missing_candidates = detect_failed_payments(missing, graph, run_id="run_missing")
    assert len(missing_candidates.candidates) == 1
    with pytest.raises(ValueError, match="amount_due_cents"):
        build_contribution_ledger(missing_candidates, missing)
    # No default amount or invented currency is produced.
    assert "amount_due_cents" not in missing["invoices"][0]
    assert missing["invoices"][0]["currency"] == "usd"

    missing_currency = json.loads(
        (EVENT_FIXTURES / "missing_amount.json").read_text(encoding="utf-8")
    )
    missing_currency_invoice = missing_currency["invoices"][0]
    missing_currency_invoice["amount_due_cents"] = 4900
    missing_currency_invoice.pop("currency")
    currency_graph = _graph_for_stripe(missing_currency)
    currency_candidates = detect_failed_payments(
        missing_currency,
        currency_graph,
        run_id="run_missing_currency",
    )
    assert len(currency_candidates.candidates) == 1
    with pytest.raises(ValueError, match="missing currency; refusing default value"):
        build_contribution_ledger(currency_candidates, missing_currency)
    assert "currency" not in missing_currency_invoice
    assert missing_currency_invoice["amount_due_cents"] == 4900


def test_t7_r1_boundaries_and_r3_output_root_unchanged(tmp_path):
    # R-1 field/path normalization.
    assert normalize_currency(" USD ") == "usd"
    assert normalize_minor_units(" 4900 ") == Decimal(4900)
    ledger, candidates = _load_map_fixture("mixed_currency.json")
    money_map = build_money_map(ledger, candidates)
    projection = public_contribution_projection(ledger)
    public_map = public_money_map_projection(money_map)

    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "out"
    root.mkdir()
    before_outside = _list_tree(outside)
    before_root = _list_tree(root)

    for writer, payload in (
        (write_contribution_ledger, ledger),
        (write_public_contribution_projection, projection),
        (write_money_map, money_map),
        (write_public_money_map_projection, public_map),
    ):
        for relative in (
            "../outside/x.json",
            "/tmp/x.json",
            "sibling/../../x.json",
            r"..\outside.json",
            "~/escaped.json",
            "",
        ):
            with pytest.raises(ValueError):
                writer(root, relative, payload)
            assert _list_tree(root) == before_root
            assert _list_tree(outside) == before_outside

        link = root / "escape"
        link.symlink_to(outside)
        with pytest.raises(ValueError):
            writer(root, "escape/x.json", payload)
        assert _list_tree(root) == sorted([*before_root, "escape"])
        assert _list_tree(outside) == before_outside
        link.unlink()
        assert _list_tree(root) == before_root

    # Invalid values fail before any write; complete trees stay unchanged.
    with pytest.raises(ValidationError):
        ContributionV1.model_validate(
            {
                "economic_unit_key": "stripe_invoice:inv_bad",
                "pile_id": "payment_rescue",
                "value_basis": "observed_face_value",
                "currency": "zzz",
                "amount_minor": "100",
                "customer_token": "cust_bad",
                "candidate_economic_unit_key": "stripe_invoice:inv_bad",
            }
        )
    with pytest.raises(ValidationError):
        ContributionV1.model_validate(
            {
                "economic_unit_key": "stripe_invoice:inv_bad",
                "pile_id": "payment_rescue",
                "value_basis": "observed_face_value",
                "currency": "usd",
                "amount_minor": "-1",
                "customer_token": "cust_bad",
                "candidate_economic_unit_key": "stripe_invoice:inv_bad",
            }
        )
    assert _list_tree(root) == before_root
    assert _list_tree(outside) == before_outside

    for relative in ["  value/out.json", "value/out.json  ", "./value/out.json"]:
        path = write_contribution_ledger(tmp_path / "ok", relative, ledger)
        assert path.exists()


def test_t8_socket_and_static_safety(monkeypatch):
    def boom(*_args, **_kwargs):
        raise AssertionError("socket connection attempted")

    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "socket", boom)

    ledger, candidates = _load_map_fixture("mixed_currency.json")
    projection = public_contribution_projection(ledger)
    money_map = build_money_map(ledger, candidates)
    public_money_map_projection(money_map)
    build_thin_slice_money_map()
    assert projection.total_minor_by_currency
    assert money_map.identified_opportunity_minor

    value_imports = _module_imports(VALUE_MODULE)
    map_imports = _module_imports(MAP_MODULE)
    contract_imports = _module_imports(VALUE_CONTRACT)
    networkish = {"requests", "urllib", "httpx", "aiohttp", "socket", "ssl"}
    assert not (value_imports & networkish)
    assert not (contract_imports & networkish)
    assert not (map_imports & networkish)
    contract_text = VALUE_CONTRACT.read_text(encoding="utf-8")
    value_text = VALUE_MODULE.read_text(encoding="utf-8")
    map_text = MAP_MODULE.read_text(encoding="utf-8")
    for blob in (contract_text, value_text):
        lowered = blob.lower()
        assert "exchange_rate" not in lowered
        assert "forex" not in lowered
        assert "api_key" not in lowered
        assert "stripe.api" not in lowered
    assert "hubspot" not in value_text.lower()
    assert "fx" not in contract_text.lower()
    assert "format_major_units" in contract_text
    # Thin-slice composition may import identity/events; builders must not add FX.
    assert "exchange_rate" not in map_text.lower()
    assert "forex" not in map_text.lower()
