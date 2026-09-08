"""Read-only HubSpot CRM 2026-03 connector with an injectable transport.

The fixture transport remains the normal test boundary.  ``HubSpotHttpTransport``
is deliberately narrow: it can issue only GETs to HubSpot's API host and only
the dated object and deal-to-contact association paths used below.
"""

from __future__ import annotations

import json
import os
import re
from hashlib import sha256
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

from found_money.contracts.source import SourceReceiptV1
from found_money.receipts import (
    _validate_relative_under_root,
    build_source_receipt,
)
from found_money.safety.allowlist import HUBSPOT_HOST, assert_network_allowed
from found_money.safety.audit import SafeRequestAuditor
from found_money.safety.writers import write_artifact_set_atomic

CONNECTOR_SCHEMA_VERSION = "hubspot-crm.2026-03.v1"
HUBSPOT_API_VERSION = "2026-03"
HUBSPOT_API_BASE = "https://api.hubapi.com"
OBJECTS_ROOT = "/crm/v3/objects"
PROPERTIES_ROOT = "/crm/v3/properties"
ASSOCIATIONS_ROOT = "/crm/v4/objects"
CONTACT_PROPERTIES = ("email", "phone", "lifecyclestage")
DEAL_PROPERTIES = ("dealstage", "pipeline", "amount", "closedate")
DEAL_STAGE_PROPERTY = "dealstage"


class HubSpotConnectorError(RuntimeError):
    """Raised when the read-only connector contract cannot be satisfied."""


class HubSpotTransport(Protocol):
    """Injected transport contract; responses are decoded JSON mappings."""

    def request(
        self,
        *,
        method: str,
        path: str,
        query: Mapping[str, str],
        headers: Mapping[str, str],
    ) -> Mapping[str, Any]: ...


_OBJECT_PATH_RE = re.compile(rf"^{re.escape(OBJECTS_ROOT)}/(?:contacts|deals)$")
_PROPERTIES_PATH_RE = re.compile(rf"^{re.escape(PROPERTIES_ROOT)}/(?:contacts|deals)$")
_ASSOCIATION_PATH_RE = re.compile(
    rf"^{re.escape(ASSOCIATIONS_ROOT)}/deals/[A-Za-z0-9_-]+/associations/contacts$"
)
_ALLOWED_QUERY_KEYS = {
    "after",
    "archived",
    "associations",
    "limit",
    "properties",
    "propertiesWithHistory",
}
_PROPERTY_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_REQUEST_ID_HEADERS = ("X-HubSpot-Request-Id", "X-Request-Id")
_CORRELATION_ID_HEADERS = ("X-HubSpot-Correlation-Id", "X-Correlation-Id")


def _allowlisted_path(path: str) -> bool:
    local = bool(
        _OBJECT_PATH_RE.fullmatch(path)
        or _PROPERTIES_PATH_RE.fullmatch(path)
        or _ASSOCIATION_PATH_RE.fullmatch(path)
    )
    if not local:
        return False
    try:
        assert_network_allowed("GET", HUBSPOT_HOST, path)
    except Exception:
        return False
    return True


def _validate_query(query: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(query, Mapping):
        raise HubSpotConnectorError("HubSpot request query is malformed")
    normalized: dict[str, str] = {}
    for key, value in query.items():
        if (
            not isinstance(key, str)
            or key not in _ALLOWED_QUERY_KEYS
            or not isinstance(value, str)
            or (key in {"after", "limit"} and not value)
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or "?" in value
            or "#" in value
        ):
            raise HubSpotConnectorError("HubSpot request query is malformed")
        normalized[key] = value
    return normalized


def _normalize_response_id(value: Any, *, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise HubSpotConnectorError(f"HubSpot response {field} must be a non-blank string")
    return value.strip()


def _record_response_ids(
    response: Mapping[str, Any], *, request_ids: list[str], correlation_ids: list[str]
) -> None:
    for field, destination in (
        ("request_id", request_ids),
        ("correlation_id", correlation_ids),
    ):
        value = _normalize_response_id(response.get(field), field=field)
        if value is not None and value not in destination:
            destination.append(value)


class _TrackingTransport:
    """Collect provider-exposed IDs without retaining raw response metadata in receipts."""

    def __init__(
        self, transport: HubSpotTransport, *, auditor: SafeRequestAuditor | None = None
    ) -> None:
        self.transport = transport
        self.request_ids: list[str] = []
        self.correlation_ids: list[str] = []
        native = isinstance(transport, HubSpotHttpTransport) and transport.uses_live_network
        self.auditor = auditor or (
            SafeRequestAuditor._for_native_transport(transport) if native else SafeRequestAuditor()
        )

    def request(
        self,
        *,
        method: str,
        path: str,
        query: Mapping[str, str],
        headers: Mapping[str, str],
    ) -> Mapping[str, Any]:
        assert_network_allowed(method, HUBSPOT_HOST, path)
        self.auditor.record(method=method, host=HUBSPOT_HOST, path=path, provenance=self.transport)
        response = self.transport.request(method=method, path=path, query=query, headers=headers)
        if isinstance(response, Mapping):
            _record_response_ids(
                response,
                request_ids=self.request_ids,
                correlation_ids=self.correlation_ids,
            )
        return response


def _validate_base_url(base_url: str) -> str:
    """Accept only HubSpot's HTTPS API origin, before any request is made."""
    if not isinstance(base_url, str) or any(
        ord(char) < 32 or ord(char) == 127 for char in base_url
    ):
        raise HubSpotConnectorError("HubSpot API origin is invalid")
    try:
        parsed = urlsplit(base_url)
        port = parsed.port
    except ValueError:
        raise HubSpotConnectorError("HubSpot API origin is invalid") from None
    if (
        parsed.scheme != "https"
        or parsed.hostname != "api.hubapi.com"
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise HubSpotConnectorError("HubSpot API origin is not allowlisted")
    return HUBSPOT_API_BASE


class HubSpotHttpTransport:
    """Concrete native transport; it has no capability beyond allowlisted GETs.

    ``client`` exists solely to permit fixture-only ``httpx.MockTransport``
    tests.  Production callers should use the default client and must arrange
    their own lifecycle/close it after use.
    """

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        base_url: str = HUBSPOT_API_BASE,
        token: str | None = None,
    ) -> None:
        self._base_url = _validate_base_url(base_url)
        self._token = _credential(token) if token is not None else None
        self._client = (
            client if client is not None else httpx.Client(timeout=30.0, follow_redirects=False)
        )
        self._network_client = self._client if client is None else None

    @property
    def uses_live_network(self) -> bool:
        return self._network_client is not None and self._client is self._network_client

    def close(self) -> None:
        self._client.close()

    def request(
        self,
        *,
        method: str,
        path: str,
        query: Mapping[str, str],
        headers: Mapping[str, str],
    ) -> Mapping[str, Any]:
        # Repeat the checks here: a caller cannot bypass the connector helper.
        if method != "GET" or not isinstance(path, str) or not _allowlisted_path(path):
            raise HubSpotConnectorError("HubSpot native request is not allowlisted")
        try:
            assert_network_allowed("GET", HUBSPOT_HOST, path)
        except Exception as exc:
            raise HubSpotConnectorError("HubSpot native request is not allowlisted") from exc
        normalized_query = _validate_query(query)
        token = self._token
        if token is None:
            authorization = headers.get("Authorization") if isinstance(headers, Mapping) else None
            if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
                raise HubSpotConnectorError("HubSpot credential is required and must be non-blank")
            token = _credential(authorization.removeprefix("Bearer "))
        url = f"{self._base_url}{path}"
        try:
            parsed = urlsplit(url)
        except ValueError:
            raise HubSpotConnectorError("HubSpot native request host is not allowlisted") from None
        if parsed.scheme != "https" or parsed.hostname != HUBSPOT_HOST:
            raise HubSpotConnectorError("HubSpot native request host is not allowlisted")
        request_headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "X-HubSpot-Api-Version": HUBSPOT_API_VERSION,
        }
        try:
            response = self._client.request(
                "GET", url, params=normalized_query, headers=request_headers
            )
        except httpx.HTTPError:
            # Do not include exception text: it can contain provider-controlled data.
            raise HubSpotConnectorError("HubSpot native request failed") from None
        if response.status_code < 200 or response.status_code >= 300:
            raise HubSpotConnectorError("HubSpot native response status was not successful")
        try:
            payload = response.json()
        except (ValueError, UnicodeDecodeError):
            raise HubSpotConnectorError("HubSpot native response is not valid JSON") from None
        if not isinstance(payload, Mapping):
            raise HubSpotConnectorError("HubSpot native response must be an object")
        payload_with_metadata = dict(payload)
        for field, header_names in (
            ("request_id", _REQUEST_ID_HEADERS),
            ("correlation_id", _CORRELATION_ID_HEADERS),
        ):
            if field not in payload_with_metadata:
                for header_name in header_names:
                    header_value = response.headers.get(header_name)
                    if header_value:
                        payload_with_metadata[field] = header_value
                        break
        return payload_with_metadata


def _credential(token: str | None) -> str:
    candidate = os.environ.get("HUBSPOT_PRIVATE_APP_TOKEN") if token is None else token
    if (
        not isinstance(candidate, str)
        or not candidate.strip()
        or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in candidate)
    ):
        raise HubSpotConnectorError("HubSpot credential is required and must be non-blank")
    return candidate


def _request(
    transport: HubSpotTransport,
    *,
    method: str,
    path: str,
    query: Mapping[str, str],
    token: str,
    auditor: SafeRequestAuditor | None = None,
) -> Mapping[str, Any]:
    """Issue one allowlisted request without exposing credentials in errors."""
    if method != "GET":
        raise HubSpotConnectorError("HubSpot connector permits GET requests only")
    if not isinstance(path, str) or not _allowlisted_path(path):
        raise HubSpotConnectorError(
            "HubSpot request path is not an allowlisted HubSpot CRM endpoint"
        )
    try:
        assert_network_allowed("GET", HUBSPOT_HOST, path)
    except Exception as exc:
        raise HubSpotConnectorError(
            "HubSpot request path is not an allowlisted HubSpot CRM endpoint"
        ) from exc
    if auditor is not None:
        auditor.record(method="GET", host=HUBSPOT_HOST, path=path)
    credential = _credential(token)
    headers = {
        "Authorization": f"Bearer {credential}",
        "Accept": "application/json",
        "X-HubSpot-Api-Version": HUBSPOT_API_VERSION,
    }
    response = transport.request(
        method=method,
        path=path,
        query=_validate_query(query),
        headers=headers,
    )
    if not isinstance(response, Mapping):
        raise HubSpotConnectorError("HubSpot transport returned a non-object response")
    return response


def _unavailable(object_type: str, response: Mapping[str, Any]) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    explicit = response.get("unavailable_properties")
    if explicit is None:
        return items
    if isinstance(explicit, Mapping):
        explicit = [{"property": name, "reason": reason} for name, reason in explicit.items()]
    if not isinstance(explicit, list):
        raise HubSpotConnectorError("HubSpot unavailable properties must be a list or object")
    for entry in explicit:
        name: Any
        reason: Any
        if isinstance(entry, str):
            name = entry
            reason = "unavailable"
        elif isinstance(entry, Mapping):
            name = entry.get("property") or entry.get("name")
            reason = entry.get("reason", "unavailable")
        else:
            raise HubSpotConnectorError("HubSpot unavailable property entry is malformed")
        if (
            not isinstance(name, str)
            or not name.strip()
            or not isinstance(reason, str)
            or not reason.strip()
        ):
            raise HubSpotConnectorError("HubSpot unavailable property entry is malformed")
        items.append({"object_type": object_type, "property": name, "reason": reason})
    return items


def _merge_unavailable(
    existing: list[dict[str, str]], additions: list[dict[str, str]]
) -> list[dict[str, str]]:
    grouped: dict[tuple[str, str], dict[str, str]] = {}
    for item in existing + additions:
        key = (item["object_type"], item["property"])
        current = grouped.get(key)
        if current is None or current["reason"] == "not_declared_in_portal":
            grouped[key] = item
    return [grouped[key] for key in sorted(grouped)]


def _association_id(result: Mapping[str, Any]) -> str | None:
    for key in ("id", "toObjectId"):
        value = result.get(key)
        if isinstance(value, (str, int)) and str(value):
            return str(value)
    target = result.get("to")
    if isinstance(target, Mapping) and isinstance(target.get("id"), (str, int)):
        return str(target["id"])
    return None


def _next_after(response: Mapping[str, Any], *, resource: str) -> str | None:
    paging = response.get("paging")
    if paging is None:
        return None
    if not isinstance(paging, Mapping):
        raise HubSpotConnectorError(f"HubSpot {resource} paging is malformed")
    next_page = paging.get("next")
    if next_page is None:
        return None
    if not isinstance(next_page, Mapping) or "after" not in next_page:
        raise HubSpotConnectorError(f"HubSpot {resource} paging cursor is malformed")
    next_after = next_page["after"]
    if (
        isinstance(next_after, bool)
        or not isinstance(next_after, (str, int))
        or not str(next_after)
    ):
        raise HubSpotConnectorError(f"HubSpot {resource} paging cursor is invalid")
    return str(next_after)


def _normalize_requested_properties(
    requested: tuple[str, ...], *, object_type: str
) -> tuple[str, ...]:
    if not isinstance(requested, tuple):
        raise HubSpotConnectorError(f"HubSpot {object_type} properties are malformed")
    normalized: list[str] = []
    for name in requested:
        if not isinstance(name, str) or not _PROPERTY_NAME_RE.fullmatch(name):
            raise HubSpotConnectorError(f"HubSpot {object_type} property name is malformed")
        if name not in normalized:
            normalized.append(name)
    return tuple(normalized)


def _property_definitions(
    transport: HubSpotTransport,
    *,
    object_type: str,
    requested: tuple[str, ...],
    token: str,
) -> tuple[list[dict[str, Any]], int, list[dict[str, str]], tuple[str, ...]]:
    """Read portal property metadata and never synthesize missing fields."""
    path = f"{PROPERTIES_ROOT}/{object_type}"
    after: str | None = None
    definitions: list[dict[str, Any]] = []
    unavailable: list[dict[str, str]] = []
    pages = 0
    seen_cursors: set[str] = set()
    while True:
        query = {"archived": "false"}
        if after is not None:
            query["after"] = after
        response = _request(transport, method="GET", path=path, query=query, token=token)
        pages += 1
        results = response.get("results")
        if not isinstance(results, list):
            raise HubSpotConnectorError("HubSpot property response results must be a list")
        for definition in results:
            if not isinstance(definition, Mapping) or not isinstance(definition.get("name"), str):
                raise HubSpotConnectorError("HubSpot property definition requires a name")
            definitions.append(dict(definition))
        unavailable = _merge_unavailable(unavailable, _unavailable(object_type, response))
        next_after = _next_after(response, resource="property")
        if next_after is None:
            break
        after = next_after
        if after in seen_cursors:
            raise HubSpotConnectorError("HubSpot property paging cursor repeated")
        seen_cursors.add(after)

    names = {str(definition["name"]) for definition in definitions}
    missing = [
        {"object_type": object_type, "property": name, "reason": "not_declared_in_portal"}
        for name in requested
        if name not in names
    ]
    unavailable = _merge_unavailable(unavailable, missing)
    available = tuple(name for name in requested if name in names)
    return (
        sorted(definitions, key=lambda definition: str(definition["name"])),
        pages,
        unavailable,
        available,
    )


def _paginate(
    transport: HubSpotTransport,
    *,
    object_type: str,
    properties: tuple[str, ...],
    token: str,
    with_history: bool = False,
    include_associations: bool = False,
) -> tuple[list[dict[str, Any]], int, list[dict[str, str]]]:
    path = f"{OBJECTS_ROOT}/{object_type}"
    after: str | None = None
    records: list[dict[str, Any]] = []
    unavailable: list[dict[str, str]] = []
    pages = 0
    seen_cursors: set[str] = set()
    seen_ids: set[str] = set()
    while True:
        query = {"limit": "100", "archived": "false"}
        if properties:
            query["properties"] = ",".join(properties)
        if with_history:
            query["propertiesWithHistory"] = DEAL_STAGE_PROPERTY
        if include_associations:
            query["associations"] = "contacts"
        if after is not None:
            query["after"] = after
        response = _request(transport, method="GET", path=path, query=query, token=token)
        pages += 1
        page_records = response.get("results")
        if not isinstance(page_records, list):
            raise HubSpotConnectorError("HubSpot list response results must be a list")
        for record in page_records:
            if not isinstance(record, Mapping) or not isinstance(record.get("id"), (str, int)):
                raise HubSpotConnectorError("HubSpot record requires an id")
            record_id = str(record["id"])
            if record_id in seen_ids:
                raise HubSpotConnectorError("HubSpot list response contains a duplicate id")
            seen_ids.add(record_id)
            properties_value = record.get("properties", {})
            history_value = record.get("propertiesWithHistory", {})
            if not isinstance(properties_value, Mapping):
                raise HubSpotConnectorError("HubSpot record properties must be an object")
            if with_history and not isinstance(history_value, Mapping):
                raise HubSpotConnectorError("HubSpot property history must be an object")
            normalized: dict[str, Any] = {
                "id": record_id,
                "properties": dict(properties_value),
            }
            if with_history:
                normalized["propertiesWithHistory"] = dict(history_value)
            if "associations" in record:
                if not isinstance(record["associations"], Mapping):
                    raise HubSpotConnectorError("HubSpot record associations must be an object")
                normalized["associations"] = dict(record["associations"])
            records.append(normalized)
        unavailable.extend(_unavailable(object_type, response))
        next_after = _next_after(response, resource="object")
        if next_after is None:
            break
        after = next_after
        if after in seen_cursors:
            raise HubSpotConnectorError("HubSpot paging cursor repeated")
        seen_cursors.add(after)
    return records, pages, unavailable


def _inline_contact_ids(deal: Mapping[str, Any]) -> list[str] | None:
    associations = deal.get("associations")
    if not isinstance(associations, Mapping) or "contacts" not in associations:
        return None
    contacts = associations["contacts"]
    if not isinstance(contacts, Mapping):
        raise HubSpotConnectorError("HubSpot inline associations must be an object")
    results = contacts.get("results")
    if not isinstance(results, list):
        raise HubSpotConnectorError("HubSpot inline association results must be a list")
    ids: set[str] = set()
    for item in results:
        if not isinstance(item, Mapping) or not isinstance(item.get("id"), (str, int)):
            raise HubSpotConnectorError("HubSpot inline association requires an id")
        ids.add(str(item["id"]))
    return sorted(ids)


def _association_contact_ids(
    transport: HubSpotTransport, *, deal_id: str, token: str
) -> tuple[list[str], int]:
    path = f"{ASSOCIATIONS_ROOT}/deals/{deal_id}/associations/contacts"
    after: str | None = None
    ids: set[str] = set()
    pages = 0
    seen_cursors: set[str] = set()
    while True:
        query = {"limit": "100"}
        if after is not None:
            query["after"] = after
        response = _request(transport, method="GET", path=path, query=query, token=token)
        pages += 1
        results = response.get("results")
        if not isinstance(results, list):
            raise HubSpotConnectorError("HubSpot association response results must be a list")
        for result in results:
            if not isinstance(result, Mapping):
                raise HubSpotConnectorError("HubSpot association requires an id")
            associated_id = _association_id(result)
            if associated_id is None:
                raise HubSpotConnectorError("HubSpot association requires an id")
            ids.add(associated_id)
        next_after = _next_after(response, resource="association")
        if next_after is None:
            return sorted(ids), pages
        after = next_after
        if after in seen_cursors:
            raise HubSpotConnectorError("HubSpot association paging cursor repeated")
        seen_cursors.add(after)


def canonical_snapshot_bytes(snapshot: Mapping[str, Any]) -> bytes:
    """Encode a normalized snapshot deterministically for its receipt hash."""
    return (
        json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def canonical_receipt_boundary_bytes(snapshot: Mapping[str, Any]) -> bytes:
    """Encode a PII-free receipt boundary for a private normalized snapshot.

    Raw records can be returned or written only by the caller to a private
    destination.  Their canonical digest makes every source change observable
    without carrying record fields, IDs, or values into receipt content.
    """
    if not isinstance(snapshot, Mapping):
        raise HubSpotConnectorError("HubSpot snapshot must be an object")
    contacts = snapshot.get("contacts")
    deals = snapshot.get("deals")
    pages = snapshot.get("page_or_row_count")
    if not isinstance(contacts, list) or not isinstance(deals, list) or not isinstance(pages, int):
        raise HubSpotConnectorError("HubSpot snapshot is malformed for receipt creation")
    boundary = {
        "connector_schema_version": CONNECTOR_SCHEMA_VERSION,
        "hubspot_api_version": HUBSPOT_API_VERSION,
        "endpoint_paths": [
            f"{OBJECTS_ROOT}/contacts",
            f"{OBJECTS_ROOT}/deals",
            f"{PROPERTIES_ROOT}/contacts",
            f"{PROPERTIES_ROOT}/deals",
            f"{ASSOCIATIONS_ROOT}/deals/{{deal_id}}/associations/contacts",
        ],
        "page_or_row_count": pages,
        "record_count": len(contacts) + len(deals),
        "private_snapshot_sha256": sha256(canonical_snapshot_bytes(snapshot)).hexdigest(),
    }
    return (
        json.dumps(boundary, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def build_crm_snapshot_receipt(
    snapshot: Mapping[str, Any],
    *,
    retrieved_at: datetime,
    locator: str = f"{OBJECTS_ROOT}/contacts",
) -> SourceReceiptV1:
    """Build the receipt accompanying a normalized HubSpot CRM snapshot."""
    return build_source_receipt(
        source_type="hubspot",
        connector_schema_version=CONNECTOR_SCHEMA_VERSION,
        retrieved_at=retrieved_at,
        locator_kind="endpoint",
        locator=locator,
        content=canonical_receipt_boundary_bytes(snapshot),
        page_or_row_count=int(snapshot["page_or_row_count"]),
        record_count=len(snapshot["contacts"]) + len(snapshot["deals"]),
        request_id=snapshot.get("request_id"),
        correlation_id=snapshot.get("correlation_id"),
    )


def write_crm_snapshot(
    output_root: Path | str,
    snapshot: Mapping[str, Any],
    receipt: SourceReceiptV1,
    *,
    snapshot_path: str = "hubspot/snapshot.json",
    receipt_path: str = "hubspot/source-receipt.json",
    auditor: SafeRequestAuditor | None = None,
    assertion_path: str = "hubspot/no-mutation-assertion.json",
) -> tuple[Path, Path]:
    """Atomically write validated HubSpot snapshot and receipt below output_root."""
    from found_money.safety.audit import build_no_mutation_assertion, empty_fixture_assertion

    root = Path(output_root)
    snapshot_destination = _validate_relative_under_root(root, snapshot_path)
    receipt_destination = _validate_relative_under_root(root, receipt_path)
    assertion_destination = _validate_relative_under_root(root, assertion_path)
    destinations = {snapshot_destination, receipt_destination, assertion_destination}
    if len(destinations) != 3:
        raise HubSpotConnectorError("HubSpot snapshot, receipt, and assertion paths must differ")
    payload = canonical_snapshot_bytes(snapshot)
    expected = build_crm_snapshot_receipt(
        snapshot, retrieved_at=receipt.retrieved_at, locator=receipt.locator
    )
    if (
        receipt.content_hash != expected.content_hash
        or receipt.request_id != expected.request_id
        or receipt.correlation_id != expected.correlation_id
    ):
        raise HubSpotConnectorError("HubSpot receipt does not match the snapshot receipt boundary")
    if auditor is None:
        assertion = empty_fixture_assertion(
            attached_receipt_hashes={"source-receipt": receipt.content_hash},
            built_at=receipt.retrieved_at,
        )
    else:
        assertion = build_no_mutation_assertion(
            auditor,
            attached_receipt_hashes={"source-receipt": receipt.content_hash},
            built_at=receipt.retrieved_at,
        )
    write_artifact_set_atomic(
        root,
        {
            snapshot_path: payload,
            receipt_path: receipt.to_canonical_json(),
            assertion_path: assertion.to_canonical_json(),
        },
    )
    return snapshot_destination, receipt_destination


def fetch_crm_snapshot(
    transport: HubSpotTransport,
    *,
    token: str | None = None,
    retrieved_at: datetime | None = None,
    output_root: Path | str | None = None,
    snapshot_path: str = "hubspot/snapshot.json",
    receipt_path: str = "hubspot/source-receipt.json",
    contact_properties: tuple[str, ...] = CONTACT_PROPERTIES,
    deal_properties: tuple[str, ...] = DEAL_PROPERTIES,
) -> dict[str, Any]:
    """Fetch normalized contacts, deals, and deal-contact links through injected GET transport."""
    credential = _credential(token)
    tracked_transport = _TrackingTransport(transport)
    requested_contact_properties = _normalize_requested_properties(
        contact_properties, object_type="contacts"
    )
    requested_deal_properties = _normalize_requested_properties(
        deal_properties, object_type="deals"
    )
    (
        contact_definitions,
        contact_property_pages,
        contact_unavailable,
        available_contact_properties,
    ) = _property_definitions(
        tracked_transport,
        object_type="contacts",
        requested=requested_contact_properties,
        token=credential,
    )
    (
        deal_definitions,
        deal_property_pages,
        deal_unavailable,
        available_deal_properties,
    ) = _property_definitions(
        tracked_transport,
        object_type="deals",
        requested=requested_deal_properties,
        token=credential,
    )
    contacts, contact_pages, contact_page_unavailable = _paginate(
        tracked_transport,
        object_type="contacts",
        properties=available_contact_properties,
        token=credential,
    )
    deals, deal_pages, deal_page_unavailable = _paginate(
        tracked_transport,
        object_type="deals",
        properties=available_deal_properties,
        token=credential,
        with_history=True,
        include_associations=True,
    )
    contact_unavailable = _merge_unavailable(contact_unavailable, contact_page_unavailable)
    deal_unavailable = _merge_unavailable(deal_unavailable, deal_page_unavailable)
    links: list[dict[str, str]] = []
    association_pages = 0
    for deal in deals:
        contact_ids = _inline_contact_ids(deal)
        if contact_ids is None:
            contact_ids, pages = _association_contact_ids(
                tracked_transport, deal_id=deal["id"], token=credential
            )
            association_pages += pages
        for contact_id in contact_ids:
            links.append({"deal_id": deal["id"], "contact_id": contact_id})
    snapshot: dict[str, Any] = {
        "connector_schema_version": CONNECTOR_SCHEMA_VERSION,
        "contacts": contacts,
        "deals": deals,
        "deal_to_contact_links": sorted(
            links, key=lambda link: (link["deal_id"], link["contact_id"])
        ),
        "property_definitions": {
            "contacts": contact_definitions,
            "deals": deal_definitions,
        },
        "requested_properties": {
            "contacts": list(requested_contact_properties),
            "deals": list(requested_deal_properties),
        },
        "unavailable_properties": _merge_unavailable(contact_unavailable, deal_unavailable),
        "request_ids": tracked_transport.request_ids,
        "correlation_ids": tracked_transport.correlation_ids,
        "request_id": tracked_transport.request_ids[0] if tracked_transport.request_ids else None,
        "correlation_id": tracked_transport.correlation_ids[0]
        if tracked_transport.correlation_ids
        else None,
        "page_or_row_count": (
            contact_property_pages
            + deal_property_pages
            + contact_pages
            + deal_pages
            + association_pages
        ),
    }
    if output_root is not None:
        when = retrieved_at or datetime.now(timezone.utc)
        receipt = build_crm_snapshot_receipt(snapshot, retrieved_at=when)
        write_crm_snapshot(
            output_root,
            snapshot,
            receipt,
            snapshot_path=snapshot_path,
            receipt_path=receipt_path,
            auditor=tracked_transport.auditor,
        )
    return snapshot


def fetch_native_crm_snapshot(
    *,
    token: str | None = None,
    retrieved_at: datetime | None = None,
    output_root: Path | str | None = None,
    snapshot_path: str = "hubspot/snapshot.json",
    receipt_path: str = "hubspot/source-receipt.json",
    contact_properties: tuple[str, ...] = CONTACT_PROPERTIES,
    deal_properties: tuple[str, ...] = DEAL_PROPERTIES,
) -> dict[str, Any]:
    """Use the bounded native transport for a Matthew-authorized private run.

    This helper performs no smoke automatically; tests must use the injected
    fixture transport path instead.
    """
    credential = _credential(token)
    transport = HubSpotHttpTransport(token=credential)
    try:
        return fetch_crm_snapshot(
            transport,
            token=credential,
            retrieved_at=retrieved_at,
            output_root=output_root,
            snapshot_path=snapshot_path,
            receipt_path=receipt_path,
            contact_properties=contact_properties,
            deal_properties=deal_properties,
        )
    finally:
        transport.close()
