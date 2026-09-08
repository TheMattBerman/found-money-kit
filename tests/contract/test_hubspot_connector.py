"""FM-009 fixture-transport contract tests for HubSpot CRM 2026-03."""

from __future__ import annotations

import hashlib
import json
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import pytest
import httpx

from found_money.connectors.hubspot import (
    ASSOCIATIONS_ROOT,
    CONNECTOR_SCHEMA_VERSION,
    HUBSPOT_API_VERSION,
    HUBSPOT_API_BASE,
    OBJECTS_ROOT,
    PROPERTIES_ROOT,
    HubSpotHttpTransport,
    HubSpotConnectorError,
    _request,
    build_crm_snapshot_receipt,
    canonical_receipt_boundary_bytes,
    fetch_crm_snapshot,
    write_crm_snapshot,
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "saas" / "connectors" / "hubspot"
WHEN = datetime(2026, 3, 2, tzinfo=timezone.utc)


class RecordingTransport:
    def __init__(
        self,
        responses: Mapping[tuple[str, str | None], str],
        *,
        metadata: Mapping[str, str] | None = None,
    ) -> None:
        self.responses = responses
        self.metadata = dict(metadata or {})
        self.calls: list[dict[str, Any]] = []

    def request(
        self,
        *,
        method: str,
        path: str,
        query: Mapping[str, str],
        headers: Mapping[str, str],
    ) -> Mapping[str, Any]:
        self.calls.append(
            {"method": method, "path": path, "query": dict(query), "headers": dict(headers)}
        )
        key = (path, query.get("after"))
        fixture = self.responses[key]
        payload = json.loads((FIXTURES / fixture).read_text())
        payload.update(self.metadata)
        return payload


def _transport(*, metadata: Mapping[str, str] | None = None) -> RecordingTransport:
    return RecordingTransport(
        {
            (f"{PROPERTIES_ROOT}/contacts", None): "contacts-properties.json",
            (f"{PROPERTIES_ROOT}/deals", None): "deals-properties.json",
            (f"{OBJECTS_ROOT}/contacts", None): "contacts-page-1.json",
            (f"{OBJECTS_ROOT}/contacts", "contacts-2"): "contacts-page-2.json",
            (f"{OBJECTS_ROOT}/deals", None): "deals-page-1.json",
            (f"{OBJECTS_ROOT}/deals", "deals-2"): "deals-page-2.json",
            (
                f"{ASSOCIATIONS_ROOT}/deals/deal-fallback/associations/contacts",
                None,
            ): "deal-fallback-associations.json",
            (
                f"{ASSOCIATIONS_ROOT}/deals/deal-second-page/associations/contacts",
                None,
            ): "deal-fallback-associations.json",
        },
        metadata=metadata,
    )


def _tree(root: Path) -> list[str]:
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))


def test_two_pages_history_associations_and_unavailable_properties():
    transport = _transport()
    snapshot = fetch_crm_snapshot(transport, token="fixture-token")

    assert snapshot["connector_schema_version"] == CONNECTOR_SCHEMA_VERSION
    assert [contact["id"] for contact in snapshot["contacts"]] == [
        "contact-inline",
        "contact-fallback",
        "contact-second-page",
    ]
    assert [deal["id"] for deal in snapshot["deals"]] == [
        "deal-inline",
        "deal-fallback",
        "deal-second-page",
    ]
    assert (
        snapshot["deals"][0]["propertiesWithHistory"]["dealstage"][0]["value"] == "qualifiedtobuy"
    )
    assert snapshot["deal_to_contact_links"] == [
        {"deal_id": "deal-fallback", "contact_id": "contact-fallback"},
        {"deal_id": "deal-inline", "contact_id": "contact-inline"},
        {"deal_id": "deal-second-page", "contact_id": "contact-fallback"},
    ]
    assert snapshot["unavailable_properties"] == [
        {"object_type": "contacts", "property": "phone", "reason": "undefined_for_portal"},
        {"object_type": "deals", "property": "amount", "reason": "undefined_for_portal"},
    ]
    assert snapshot["contacts"][2]["properties"] == {"email": "second@example.invalid"}
    assert snapshot["page_or_row_count"] == 8
    assert all(call["method"] == "GET" for call in transport.calls)
    assert all(
        call["path"].startswith((OBJECTS_ROOT, PROPERTIES_ROOT, ASSOCIATIONS_ROOT))
        for call in transport.calls
    )
    object_calls = [call for call in transport.calls if call["path"].startswith(OBJECTS_ROOT)]
    assert object_calls[0]["query"]["properties"] == "email,lifecyclestage"
    assert object_calls[1]["query"]["properties"] == "email,lifecyclestage"
    assert object_calls[2]["query"]["propertiesWithHistory"] == "dealstage"
    assert object_calls[2]["query"]["associations"] == "contacts"
    assert snapshot["property_definitions"]["contacts"][0]["name"] == "email"
    assert snapshot["property_definitions"]["deals"][0]["name"] == "closedate"


def test_receipt_and_atomic_output_are_hashed_and_query_free(tmp_path):
    snapshot = fetch_crm_snapshot(_transport(), token="fixture-token")
    receipt = build_crm_snapshot_receipt(snapshot, retrieved_at=WHEN)
    snapshot_path, receipt_path = write_crm_snapshot(tmp_path, snapshot, receipt)

    assert snapshot_path.read_bytes()
    assert receipt_path.read_bytes()
    assert (
        receipt.content_hash
        == hashlib.sha256(canonical_receipt_boundary_bytes(snapshot)).hexdigest()
    )
    assert receipt.locator == f"{OBJECTS_ROOT}/contacts"
    assert "?" not in receipt.locator
    receipt_payload = receipt_path.read_text()
    assert json.loads(receipt_payload)["schema_version"] == "source-receipt.v1"
    assert "inline@example.invalid" not in receipt_payload
    assert "contact-inline" not in receipt_payload
    assert "fixture-token" not in receipt_payload
    assert not list(tmp_path.rglob("*.tmp"))


def test_receipt_boundary_is_canonical_redacted_and_changes_with_private_snapshot():
    snapshot = fetch_crm_snapshot(_transport(), token="fixture-token")
    boundary = canonical_receipt_boundary_bytes(snapshot)
    changed = json.loads(json.dumps(snapshot))
    changed["contacts"][0]["properties"]["email"] = "changed@example.invalid"

    assert boundary.endswith(b"\n")
    assert b"inline@example.invalid" not in boundary
    assert b"contact-inline" not in boundary
    assert (
        hashlib.sha256(boundary).hexdigest()
        == build_crm_snapshot_receipt(snapshot, retrieved_at=WHEN).content_hash
    )
    assert canonical_receipt_boundary_bytes(changed) != boundary


def test_fixture_replay_produces_byte_identical_redacted_receipts():
    first = fetch_crm_snapshot(_transport(), token="fixture-token")
    second = fetch_crm_snapshot(_transport(), token="fixture-token")

    first_receipt = build_crm_snapshot_receipt(first, retrieved_at=WHEN)
    second_receipt = build_crm_snapshot_receipt(second, retrieved_at=WHEN)

    assert canonical_receipt_boundary_bytes(first) == canonical_receipt_boundary_bytes(second)
    assert first_receipt.to_canonical_json() == second_receipt.to_canonical_json()


def test_exposed_response_ids_are_carried_into_the_redacted_receipt(tmp_path):
    snapshot = fetch_crm_snapshot(
        _transport(metadata={"request_id": "req_fixture", "correlation_id": "corr_fixture"}),
        token="fixture-token",
    )
    receipt = build_crm_snapshot_receipt(snapshot, retrieved_at=WHEN)
    _, receipt_path = write_crm_snapshot(tmp_path, snapshot, receipt)

    assert snapshot["request_ids"] == ["req_fixture"]
    assert snapshot["correlation_ids"] == ["corr_fixture"]
    assert receipt.request_id == "req_fixture"
    assert receipt.correlation_id == "corr_fixture"
    encoded = receipt_path.read_text()
    assert "req_fixture" in encoded
    assert "corr_fixture" in encoded
    assert "inline@example.invalid" not in encoded


def test_native_mock_transport_uses_documented_paths_and_matches_fixture_boundary():
    fixture_snapshot = fetch_crm_snapshot(_transport(), token="fixture-token")
    calls: list[httpx.Request] = []
    fixture_names = {
        (f"{PROPERTIES_ROOT}/contacts", None): "contacts-properties.json",
        (f"{PROPERTIES_ROOT}/deals", None): "deals-properties.json",
        (f"{OBJECTS_ROOT}/contacts", None): "contacts-page-1.json",
        (f"{OBJECTS_ROOT}/contacts", "contacts-2"): "contacts-page-2.json",
        (f"{OBJECTS_ROOT}/deals", None): "deals-page-1.json",
        (f"{OBJECTS_ROOT}/deals", "deals-2"): "deals-page-2.json",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.method == "GET"
        assert request.url.host == "api.hubapi.com"
        assert request.headers["X-HubSpot-Api-Version"] == HUBSPOT_API_VERSION
        assert request.headers["Authorization"] == "Bearer fixture-token"
        if request.url.path.startswith(f"{ASSOCIATIONS_ROOT}/deals/"):
            fixture_name = "deal-fallback-associations.json"
        else:
            fixture_name = fixture_names[(request.url.path, request.url.params.get("after"))]
        if request.url.path == f"{OBJECTS_ROOT}/deals":
            assert request.url.params["associations"] == "contacts"
            assert request.url.params["propertiesWithHistory"] == "dealstage"
        payload = json.loads((FIXTURES / fixture_name).read_text())
        return httpx.Response(200, json=payload)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    transport = HubSpotHttpTransport(client=client, token="fixture-token")
    try:
        native_snapshot = fetch_crm_snapshot(transport, token="fixture-token")
    finally:
        transport.close()

    assert native_snapshot == fixture_snapshot
    assert len(calls) == 8
    assert hashlib.sha256(canonical_receipt_boundary_bytes(native_snapshot)).hexdigest() == (
        build_crm_snapshot_receipt(native_snapshot, retrieved_at=WHEN).content_hash
    )


def test_native_transport_captures_exposed_request_headers_without_raw_response_data():
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={
                    "X-HubSpot-Request-Id": "req_native",
                    "X-Correlation-Id": "corr_native",
                },
                json={"results": []},
            )
        )
    )
    transport = HubSpotHttpTransport(client=client, token="fixture-token")
    try:
        response = transport.request(
            method="GET", path=f"{OBJECTS_ROOT}/contacts", query={}, headers={}
        )
    finally:
        transport.close()

    assert response == {
        "results": [],
        "request_id": "req_native",
        "correlation_id": "corr_native",
    }


def test_fixture_transport_never_opens_a_socket(monkeypatch):
    def fail_socket(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("fixture mode must not open a socket")

    monkeypatch.setattr(socket, "socket", fail_socket)
    monkeypatch.setattr(socket, "create_connection", fail_socket)

    snapshot = fetch_crm_snapshot(_transport(), token="fixture-token")

    assert snapshot["contacts"]


@pytest.mark.parametrize(
    "response,match",
    [
        ({"results": "not-a-list"}, "results must be a list"),
        ({"results": [], "paging": {"next": {"after": []}}}, "paging cursor is invalid"),
    ],
)
def test_malformed_fixture_response_fails_closed(response, match):
    class MalformedTransport:
        def __init__(self) -> None:
            self.calls = 0

        def request(self, **kwargs: Any) -> Mapping[str, Any]:
            self.calls += 1
            return response

    transport = MalformedTransport()
    with pytest.raises(HubSpotConnectorError, match=match):
        fetch_crm_snapshot(transport, token="fixture-token")
    assert transport.calls == 1


def test_missing_or_blank_environment_credential_leaves_root_unchanged(tmp_path, monkeypatch):
    root = tmp_path / "empty"
    root.mkdir()
    for value in (None, "", "   "):
        if value is None:
            monkeypatch.delenv("HUBSPOT_PRIVATE_APP_TOKEN", raising=False)
        else:
            monkeypatch.setenv("HUBSPOT_PRIVATE_APP_TOKEN", value)
        before = _tree(root)
        with pytest.raises(HubSpotConnectorError, match="credential"):
            fetch_crm_snapshot(_transport(), output_root=root)
        assert _tree(root) == before


def test_non_get_request_stops_before_transport_call():
    transport = _transport()
    with pytest.raises(HubSpotConnectorError, match="GET"):
        _request(
            transport,
            method="POST",
            path=f"{OBJECTS_ROOT}/deals",
            query={},
            token="fixture-token",
        )
    assert transport.calls == []


@pytest.mark.parametrize(
    "base_url",
    [
        "http://api.hubapi.com",
        "https://evil.example",
        "https://api.hubapi.com.evil.example",
        "https://token@api.hubapi.com",
        "https://api.hubapi.com/other",
    ],
)
def test_native_transport_rejects_non_allowlisted_origin_before_io(base_url: str):
    with pytest.raises(HubSpotConnectorError, match="origin"):
        HubSpotHttpTransport(base_url=base_url)


def test_native_transport_rejects_malformed_origin_before_io():
    with pytest.raises(HubSpotConnectorError, match="origin"):
        HubSpotHttpTransport(base_url="https://api.hubapi.com:not-a-port")


def test_native_transport_uses_only_get_and_never_leaks_token_or_raw_response():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"results": []})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    transport = HubSpotHttpTransport(client=client)
    token = "fixture-secret-token"
    assert transport.request(
        method="GET",
        path=f"{OBJECTS_ROOT}/contacts",
        query={"limit": "100"},
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    ) == {"results": []}
    assert len(calls) == 1
    request = calls[0]
    assert request.headers["X-HubSpot-Api-Version"] == HUBSPOT_API_VERSION
    assert request.headers["Authorization"] == f"Bearer {token}"
    assert calls[0].method == "GET"
    assert str(calls[0].url).startswith(HUBSPOT_API_BASE)
    with pytest.raises(HubSpotConnectorError) as exc:
        transport.request(
            method="P" + "OST",
            path=f"{OBJECTS_ROOT}/contacts",
            query={},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert token not in str(exc.value)
    assert len(calls) == 1
    with pytest.raises(HubSpotConnectorError):
        transport.request(
            method="GET",
            path=f"{OBJECTS_ROOT}/contacts/unexpected",
            query={},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert len(calls) == 1
    transport.close()


def test_native_transport_requires_credential_before_io():
    calls: list[httpx.Request] = []
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: calls.append(request) or httpx.Response(200, json={"results": []})
        )
    )
    transport = HubSpotHttpTransport(client=client)
    with pytest.raises(HubSpotConnectorError, match="credential"):
        transport.request(
            method="GET",
            path=f"{OBJECTS_ROOT}/contacts",
            query={},
            headers={},
        )
    assert calls == []
    transport.close()


def test_native_transport_rejects_malformed_response_without_exposing_body():
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text="customer@example.invalid")
        )
    )
    transport = HubSpotHttpTransport(client=client)
    with pytest.raises(HubSpotConnectorError) as exc:
        transport.request(method="GET", path=f"{OBJECTS_ROOT}/contacts", query={}, headers={})
    assert "customer@example.invalid" not in str(exc.value)
    transport.close()


@pytest.mark.parametrize("credential", ["", " ", "bad token", "bad\x00token", "bad\n token"])
def test_invalid_credential_stops_before_transport_call(credential: str):
    transport = _transport()
    with pytest.raises(HubSpotConnectorError, match="credential") as exc:
        fetch_crm_snapshot(transport, token=credential)
    if credential.strip():
        assert credential not in str(exc.value)
    assert transport.calls == []


@pytest.mark.parametrize(
    "snapshot_path,receipt_path",
    [
        ("../snapshot.json", "hubspot/receipt.json"),
        ("hubspot/snapshot.json", "../receipt.json"),
        ("/tmp/snapshot.json", "hubspot/receipt.json"),
        ("hubspot/snapshot.json", "/tmp/receipt.json"),
    ],
)
def test_write_rejects_escape_without_partial_output(
    tmp_path, snapshot_path: str, receipt_path: str
):
    root = tmp_path / "out"
    root.mkdir()
    snapshot = fetch_crm_snapshot(_transport(), token="fixture-token")
    receipt = build_crm_snapshot_receipt(snapshot, retrieved_at=WHEN)
    before = _tree(root)
    with pytest.raises(ValueError):
        write_crm_snapshot(
            root,
            snapshot,
            receipt,
            snapshot_path=snapshot_path,
            receipt_path=receipt_path,
        )
    assert _tree(root) == before


def test_write_rejects_symlink_escape_without_partial_output(tmp_path):
    root = tmp_path / "out"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)
    snapshot = fetch_crm_snapshot(_transport(), token="fixture-token")
    receipt = build_crm_snapshot_receipt(snapshot, retrieved_at=WHEN)
    before = _tree(root)
    with pytest.raises(ValueError):
        write_crm_snapshot(root, snapshot, receipt, snapshot_path="escape/snapshot.json")
    assert _tree(root) == before
    assert _tree(outside) == []
