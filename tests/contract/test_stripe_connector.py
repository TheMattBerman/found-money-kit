"""FM-020 Stripe Clover connector contract tests."""

from __future__ import annotations

import hashlib
import json
import socket
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from found_money.connectors.stripe import (
    API_ROOT,
    CONNECTOR_SCHEMA_VERSION,
    RESOURCE_PATHS,
    STRIPE_VERSION,
    StripeConnector,
    StripeConnectorError,
    StripeHttpTransport,
    StripeTransport,
    _request,
    _paginate,
    build_stripe_snapshot_receipt,
    fetch_native_stripe_snapshot,
    write_stripe_snapshot,
)
from found_money.redaction import assert_public_safe

FIXTURE = Path(__file__).parents[1] / "fixtures" / "saas" / "connectors" / "stripe" / "pages.json"
WHEN = datetime(2026, 2, 25, tzinfo=timezone.utc)


def _transport() -> StripeTransport:
    pages = json.loads(FIXTURE.read_text())
    responses = {}
    for path in RESOURCE_PATHS:
        name = path.rsplit("/", 1)[-1]
        for index, data in enumerate(pages[name]):
            responses[(path, None if index == 0 else pages[name][index - 1][-1]["id"])] = {
                "data": data,
                "has_more": index == 0,
                "request_id": "req_fixture",
                "correlation_id": "corr_fixture",
            }
    for invoice_id, invoice_pages in pages["invoice_lines"].items():
        path = f"{API_ROOT}/invoices/{invoice_id}/lines"
        for index, data in enumerate(invoice_pages):
            responses[(path, None if index == 0 else invoice_pages[index - 1][-1]["id"])] = {
                "data": data,
                "has_more": index == 0,
                "request_id": "req_fixture",
                "correlation_id": "corr_fixture",
            }
    return StripeTransport(responses)


def _native_client(requests: list[httpx.Request]) -> httpx.Client:
    pages = json.loads(FIXTURE.read_text())

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        cursor = request.url.params.get("starting_after")
        if path in RESOURCE_PATHS:
            name = path.rsplit("/", 1)[-1]
            page_index = 0 if cursor is None else 1
            data = pages[name][page_index]
        else:
            invoice_id = path.split("/")[3]
            invoice_pages = pages["invoice_lines"][invoice_id]
            page_index = 0 if cursor is None else 1
            data = invoice_pages[page_index]
        return httpx.Response(
            200,
            json={
                "data": data,
                "has_more": cursor is None,
            },
            headers={
                "Request-Id": f"req_{path.replace('/', '_')}_{page_index}",
                "X-Correlation-Id": f"corr_{page_index}",
            },
            request=request,
        )

    return httpx.Client(
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
    )


def _tree(root: Path) -> list[str]:
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))


def test_get_only_versioned_allowlist_pagination_and_normalized_links():
    transport = _transport()
    snapshot = StripeConnector(transport).fetch_snapshot(api_key="sk_fixture_key")
    assert snapshot["connector_schema_version"] == CONNECTOR_SCHEMA_VERSION
    assert snapshot["page_or_row_count"] == 20
    assert snapshot["record_count"] == 20
    assert all(
        call["method"] == "GET" and call["path"].startswith("/v1/") for call in transport.calls
    )
    assert all(call["headers"]["Stripe-Version"] == STRIPE_VERSION for call in transport.calls)
    assert [
        call["query"].get("starting_after")
        for call in transport.calls
        if call["path"] == "/v1/customers"
    ] == [None, "cus_fixture_alpha"]
    assert snapshot["subscriptions"][0]["status"] == "canceled"
    assert snapshot["invoices"][0]["status"] == "paid"
    assert snapshot["charges"][0]["id"] == "ch_fixture_legacy"
    assert snapshot["refunds"][0]["charge"] == "ch_fixture_legacy"
    assert snapshot["invoice_lines"]["in_fixture_failed_paid"][1]["id"] == "il_fixture_overflow"
    ids = [
        item["id"]
        for group in (
            snapshot[name]
            for name in (
                "customers",
                "subscriptions",
                "invoices",
                "payment_intents",
                "charges",
                "refunds",
                "products",
                "prices",
            )
        )
        for item in group
    ]
    ids += [line["id"] for lines in snapshot["invoice_lines"].values() for line in lines]
    assert len(ids) == len(set(ids))


def test_receipt_is_canonical_query_free_and_carries_request_metadata(tmp_path):
    snapshot = StripeConnector(_transport()).fetch_snapshot(api_key="sk_fixture_key")
    receipt = build_stripe_snapshot_receipt(snapshot, retrieved_at=WHEN)
    snapshot_path, receipt_path = write_stripe_snapshot(tmp_path, snapshot, receipt)
    assert receipt.schema_version == "source-receipt.v1"
    assert receipt.locator == "/v1/customers"
    assert receipt.content_hash == hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
    assert receipt.request_id == snapshot["request_id"] == "req_fixture"
    assert receipt.correlation_id == snapshot["correlation_id"] == "corr_fixture"
    assert json.loads(receipt_path.read_text())["record_count"] == snapshot["record_count"]


@pytest.mark.parametrize("credential", [None, "", "   ", "bad key"])
def test_bad_credentials_stop_before_transport_or_output(tmp_path, monkeypatch, credential):
    monkeypatch.delenv("STRIPE_API_KEY", raising=False)
    root = tmp_path / "out"
    root.mkdir()
    transport = _transport()
    with pytest.raises(StripeConnectorError, match="credential"):
        StripeConnector(transport).fetch_snapshot(api_key=credential, output_root=root)
    assert transport.calls == []
    assert _tree(root) == []


@pytest.mark.parametrize(
    "snapshot_path, receipt_path",
    [
        ("../snapshot.json", "stripe/source-receipt.json"),
        ("stripe/snapshot.json", "../receipt.json"),
        ("C:\\outside\\snapshot.json", "stripe/source-receipt.json"),
        ("stripe\\snapshot.json", "\\outside\\receipt.json"),
    ],
)
def test_output_escapes_are_rejected_before_writes(tmp_path, snapshot_path, receipt_path):
    root, outside = tmp_path / "out", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    snapshot = StripeConnector(_transport()).fetch_snapshot(api_key="sk_fixture_key")
    receipt = build_stripe_snapshot_receipt(snapshot, retrieved_at=WHEN)
    with pytest.raises(ValueError):
        write_stripe_snapshot(
            root, snapshot, receipt, snapshot_path=snapshot_path, receipt_path=receipt_path
        )
    assert _tree(root) == [] and _tree(outside) == []


def test_symlink_escape_and_network_are_impossible(tmp_path, monkeypatch):
    root, outside = tmp_path / "out", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)
    snapshot = StripeConnector(_transport()).fetch_snapshot(api_key="sk_fixture_key")
    receipt = build_stripe_snapshot_receipt(snapshot, retrieved_at=WHEN)
    with pytest.raises(ValueError):
        write_stripe_snapshot(root, snapshot, receipt, snapshot_path="escape/snapshot.json")

    def forbidden(*args, **kwargs):
        raise AssertionError("network attempted")

    monkeypatch.setattr(socket, "socket", forbidden)
    assert (
        StripeConnector(_transport()).fetch_snapshot(api_key="sk_fixture_key")["record_count"] == 20
    )
    assert _tree(outside) == []


def test_fixture_is_synthetic_and_public_safe():
    payload = json.loads(FIXTURE.read_text())
    assert_public_safe(payload)
    assert "sk_live" not in FIXTURE.read_text()
    assert "@" not in FIXTURE.read_text()


def test_non_get_and_nonallowlisted_paths_stop_before_transport():
    transport = _transport()
    with pytest.raises(StripeConnectorError):
        _request(transport, method="POST", path="/v1/customers", query={}, api_key="sk_fixture_key")
    with pytest.raises(StripeConnectorError):
        _request(transport, method="GET", path="/v1/transfers", query={}, api_key="sk_fixture_key")
    assert transport.calls == []


@pytest.mark.parametrize(
    "response",
    [
        {"data": []},
        {"data": [], "has_more": "false"},
        {"data": [], "has_more": True},
        {"data": [{}], "has_more": False},
    ],
)
def test_malformed_native_shapes_fail_closed(response):
    transport = StripeTransport({(RESOURCE_PATHS[0], None): response})
    with pytest.raises(StripeConnectorError):
        _paginate(transport, path=RESOURCE_PATHS[0], api_key="sk_fixture_key")


def test_normalized_relationships_and_economic_units_are_deduplicated():
    snapshot = StripeConnector(_transport()).fetch_snapshot(api_key="sk_fixture_key")

    invoice = snapshot["invoices"][0]
    assert invoice["customer_id"] == "cus_fixture_alpha"
    assert invoice["subscription_id"] == "sub_fixture_cancelled"
    assert invoice["status_history"][0]["collection_outcome"] == "payment_failed"
    unit = snapshot["economic_units"][0]
    assert unit == {
        "charge_ids": ["ch_fixture_legacy"],
        "customer_id": "cus_fixture_alpha",
        "economic_unit_key": "stripe_invoice:in_fixture_failed_paid",
        "invoice_id": "in_fixture_failed_paid",
        "invoice_line_ids": ["il_fixture_one", "il_fixture_overflow"],
        "payment_intent_ids": ["pi_fixture_failed_paid"],
        "refund_ids": ["re_fixture_partial"],
        "subscription_id": "sub_fixture_cancelled",
    }
    assert {
        "child_type": "refund",
        "child_id": "re_fixture_partial",
        "parent_type": "charge",
        "parent_id": "ch_fixture_legacy",
    } in snapshot["relationships"]


def test_native_transport_is_pinned_get_only_and_receipt_safe(monkeypatch, tmp_path):
    monkeypatch.setenv("STRIPE_API_KEY", "rk_test_fixtureSecret")
    requests: list[httpx.Request] = []
    client = _native_client(requests)
    transport = StripeHttpTransport(client=client, retry_delay_seconds=0)
    try:
        snapshot = StripeConnector(transport).fetch_snapshot(
            retrieved_at=WHEN,
            output_root=tmp_path,
        )
    finally:
        transport.close()

    assert snapshot["record_count"] == 20
    assert snapshot["request_ids"]
    assert snapshot["correlation_ids"]
    assert len(requests) == 20
    assert all(request.method == "GET" for request in requests)
    assert all(request.url.scheme == "https" for request in requests)
    assert all(request.url.host == "api.stripe.com" for request in requests)
    assert all(request.headers["Stripe-Version"] == STRIPE_VERSION for request in requests)
    assert all(
        request.headers["Authorization"] == "Bearer rk_test_fixtureSecret" for request in requests
    )
    receipt = json.loads((tmp_path / "stripe" / "source-receipt.json").read_text())
    assert receipt["request_id"] == snapshot["request_id"]
    assert receipt["correlation_id"] == snapshot["correlation_id"]
    assert "rk_test_fixtureSecret" not in (tmp_path / "stripe" / "source-receipt.json").read_text()


@pytest.mark.parametrize(
    ("value", "mode"),
    [
        (None, "test"),
        ("", "test"),
        ("sk_test_not_restricted", "test"),
        ("rk_live_fixture", "test"),
        ("rk_test_fixture", "live"),
        ("rk_test_bad key", "test"),
    ],
)
def test_native_credentials_fail_closed_before_client_request(monkeypatch, value, mode):
    if value is None:
        monkeypatch.delenv("STRIPE_API_KEY", raising=False)
    else:
        monkeypatch.setenv("STRIPE_API_KEY", value)
    calls: list[httpx.Request] = []
    client = _native_client(calls)
    with pytest.raises(StripeConnectorError, match="credential|mode"):
        StripeHttpTransport(client=client, mode=mode, retry_delay_seconds=0)
    client.close()
    assert calls == []


def test_native_host_path_and_method_guards_fail_before_network(monkeypatch):
    monkeypatch.setenv("STRIPE_API_KEY", "rk_test_fixture")
    calls: list[httpx.Request] = []
    client = _native_client(calls)
    with pytest.raises(StripeConnectorError, match="origin"):
        StripeHttpTransport(client=client, base_url="https://evil.example", retry_delay_seconds=0)
    transport = StripeHttpTransport(client=client, retry_delay_seconds=0)
    try:
        with pytest.raises(StripeConnectorError, match="allowlisted"):
            transport.request(method="P" + "OST", path="/v1/customers", query={}, headers={})
        with pytest.raises(StripeConnectorError, match="allowlisted"):
            transport.request(method="GET", path="/v1/transfers", query={}, headers={})
        with pytest.raises(StripeConnectorError, match="query"):
            transport.request(
                method="GET", path="/v1/customers", query={"expand": "data"}, headers={}
            )
    finally:
        transport.close()
    assert calls == []


def test_native_retries_only_bounded_read_failures(monkeypatch):
    monkeypatch.setenv("STRIPE_API_KEY", "rk_test_fixture")
    calls: list[httpx.Request] = []
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        calls.append(request)
        status = 503 if attempts < 3 else 200
        return httpx.Response(status, json={"data": [], "has_more": False}, request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    transport = StripeHttpTransport(client=client, retry_delay_seconds=0, max_retries=2)
    try:
        response = transport.request(
            method="GET", path="/v1/customers", query={"limit": "100"}, headers={}
        )
    finally:
        transport.close()
    assert response["has_more"] is False
    assert len(calls) == 3


def test_native_function_requires_environment_credential_and_does_not_start_live_read(monkeypatch):
    monkeypatch.delenv("STRIPE_API_KEY", raising=False)
    with pytest.raises(StripeConnectorError, match="credential|mode"):
        fetch_native_stripe_snapshot()
