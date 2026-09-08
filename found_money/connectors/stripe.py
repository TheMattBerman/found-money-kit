"""Read-only Stripe ``2026-02-25.clover`` connector.

``StripeTransport`` remains the deterministic fixture boundary.  The separate
``StripeHttpTransport`` is intentionally narrower than a general HTTP client:
it can issue only GETs to Stripe's API origin, only to the resource paths used by
the connector, and only with an environment-provided restricted key.
"""

from __future__ import annotations

import json
import os
import re
import time
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
from found_money.safety.allowlist import STRIPE_HOST, assert_network_allowed
from found_money.safety.audit import SafeRequestAuditor
from found_money.safety.writers import write_artifact_set_atomic

CONNECTOR_SCHEMA_VERSION = "stripe.2026-02-25.clover.v1"
STRIPE_VERSION = "2026-02-25.clover"
STRIPE_API_BASE = "https://api.stripe.com"
API_ROOT = "/v1"
RESOURCE_PATHS = (
    "/v1/customers",
    "/v1/subscriptions",
    "/v1/invoices",
    "/v1/payment_intents",
    "/v1/charges",
    "/v1/refunds",
    "/v1/products",
    "/v1/prices",
)
RESOURCE_NAMES = tuple(path.rsplit("/", 1)[-1] for path in RESOURCE_PATHS)
_ALLOWED_QUERY_KEYS = {"limit", "starting_after"}
_RESTRICTED_KEY_RE = re.compile(r"^rk_(?:test|live)_[A-Za-z0-9_-]+$")
_INVOICE_LINES_PATH_RE = re.compile(r"^/v1/invoices/[A-Za-z0-9_-]+/lines$")
_REQUEST_ID_HEADERS = ("Request-Id", "X-Request-Id", "Stripe-Request-Id")
_CORRELATION_ID_HEADERS = ("X-Correlation-Id", "Stripe-Correlation-Id")


class StripeConnectorError(RuntimeError):
    """Raised when the read-only Stripe connector contract is not satisfied."""


class StripeRequestTransport(Protocol):
    def request(
        self, *, method: str, path: str, query: Mapping[str, str], headers: Mapping[str, str]
    ) -> Mapping[str, Any]: ...


def _validate_query(query: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(query, Mapping):
        raise StripeConnectorError("Stripe request query is malformed")
    normalized: dict[str, str] = {}
    for key, value in query.items():
        if (
            not isinstance(key, str)
            or key not in _ALLOWED_QUERY_KEYS
            or not isinstance(value, str)
            or not value
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or "?" in value
            or "#" in value
        ):
            raise StripeConnectorError("Stripe request query is malformed")
        normalized[key] = value
    return normalized


def _normalize_response_id(value: Any, *, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise StripeConnectorError(f"Stripe response {field} must be a non-blank string")
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
    """Collect provider IDs without carrying raw response metadata to receipts."""

    def __init__(
        self, transport: StripeRequestTransport, *, auditor: SafeRequestAuditor | None = None
    ) -> None:
        self.transport = transport
        self.request_ids: list[str] = []
        self.correlation_ids: list[str] = []
        native = isinstance(transport, StripeHttpTransport) and transport.uses_live_network
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
        assert_network_allowed(method, STRIPE_HOST, path)
        self.auditor.record(method=method, host=STRIPE_HOST, path=path, provenance=self.transport)
        response = self.transport.request(method=method, path=path, query=query, headers=headers)
        if isinstance(response, Mapping):
            _record_response_ids(
                response,
                request_ids=self.request_ids,
                correlation_ids=self.correlation_ids,
            )
        return response


class StripeTransport:
    """A recording transport backed exclusively by injected fixture responses.

    Keys are ``(path, starting_after)`` pairs.  This deliberately small object is
    useful both as the production fixture transport and as a contract-test spy.
    """

    def __init__(self, responses: Mapping[tuple[str, str | None], Mapping[str, Any]]) -> None:
        self._responses = {key: dict(value) for key, value in responses.items()}
        self.calls: list[dict[str, Any]] = []

    def request(
        self, *, method: str, path: str, query: Mapping[str, str], headers: Mapping[str, str]
    ) -> Mapping[str, Any]:
        self.calls.append(
            {"method": method, "path": path, "query": dict(query), "headers": dict(headers)}
        )
        try:
            response = self._responses[(path, query.get("starting_after"))]
        except KeyError as exc:
            raise StripeConnectorError("no injected Stripe fixture response for request") from exc
        # Keep connector normalization unable to mutate the injected fixture.
        return json.loads(json.dumps(response))


def _credential(api_key: str | None) -> str:
    candidate = os.environ.get("STRIPE_API_KEY") if api_key is None else api_key
    if (
        not isinstance(candidate, str)
        or not candidate.strip()
        or any(char.isspace() for char in candidate)
    ):
        raise StripeConnectorError("Stripe credential is required and must be non-blank")
    return candidate


def _native_credential(*, mode: str) -> str:
    candidate = os.environ.get("STRIPE_API_KEY")
    if (
        not isinstance(candidate, str)
        or not _RESTRICTED_KEY_RE.fullmatch(candidate)
        or candidate.split("_", 2)[1] != mode
    ):
        raise StripeConnectorError("Stripe restricted credential is missing or mode-mismatched")
    return candidate


def _allowed_path(path: str) -> bool:
    local = bool(path in RESOURCE_PATHS or bool(_INVOICE_LINES_PATH_RE.fullmatch(path)))
    if not local:
        return False
    try:
        assert_network_allowed("GET", STRIPE_HOST, path)
    except Exception:
        return False
    return True


def _validate_base_url(base_url: str) -> str:
    if not isinstance(base_url, str) or any(
        ord(char) < 32 or ord(char) == 127 for char in base_url
    ):
        raise StripeConnectorError("Stripe API origin is invalid")
    try:
        parsed = urlsplit(base_url)
        port = parsed.port
    except ValueError:
        raise StripeConnectorError("Stripe API origin is invalid") from None
    if (
        parsed.scheme != "https"
        or parsed.hostname != "api.stripe.com"
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise StripeConnectorError("Stripe API origin is not allowlisted")
    return STRIPE_API_BASE


class StripeHttpTransport:
    """Concrete native transport constrained to safe, versioned Stripe GETs.

    The production constructor reads ``STRIPE_API_KEY`` itself so a caller
    cannot accidentally pass a key through a config or artifact path.  Tests
    may inject an ``httpx.Client`` with ``MockTransport`` and a monkeypatched
    environment value.
    """

    _RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        base_url: str = STRIPE_API_BASE,
        mode: str = "test",
        max_retries: int = 2,
        retry_delay_seconds: float = 0.25,
    ) -> None:
        if mode not in {"test", "live"}:
            raise StripeConnectorError("Stripe mode must be test or live")
        if not isinstance(max_retries, int) or not 0 <= max_retries <= 3:
            raise StripeConnectorError("Stripe retry count is out of bounds")
        if not isinstance(retry_delay_seconds, (int, float)) or not 0 <= retry_delay_seconds <= 5:
            raise StripeConnectorError("Stripe retry delay is out of bounds")
        self._base_url = _validate_base_url(base_url)
        self._token = _native_credential(mode=mode)
        self._max_retries = max_retries
        self._retry_delay_seconds = float(retry_delay_seconds)
        self._client = (
            client
            if client is not None
            else httpx.Client(
                timeout=httpx.Timeout(connect=5.0, read=15.0, write=15.0, pool=5.0),
                follow_redirects=False,
            )
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
        if method != "GET" or not isinstance(path, str) or not _allowed_path(path):
            raise StripeConnectorError("Stripe native request is not allowlisted")
        try:
            assert_network_allowed("GET", STRIPE_HOST, path)
        except Exception as exc:
            raise StripeConnectorError("Stripe native request is not allowlisted") from exc
        normalized_query = _validate_query(query)
        url = f"{self._base_url}{path}"
        try:
            parsed = urlsplit(url)
        except ValueError:
            raise StripeConnectorError("Stripe native request host is not allowlisted") from None
        if parsed.scheme != "https" or parsed.hostname != STRIPE_HOST:
            raise StripeConnectorError("Stripe native request host is not allowlisted")

        request_headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._token}",
            "Stripe-Version": STRIPE_VERSION,
        }
        for attempt in range(self._max_retries + 1):
            try:
                response = self._client.request(
                    "GET",
                    url,
                    params=normalized_query,
                    headers=request_headers,
                )
            except (httpx.HTTPError, OSError):
                if attempt >= self._max_retries:
                    raise StripeConnectorError("Stripe native request failed") from None
                if self._retry_delay_seconds:
                    time.sleep(self._retry_delay_seconds * (attempt + 1))
                continue
            if response.status_code in self._RETRYABLE_STATUSES and attempt < self._max_retries:
                if self._retry_delay_seconds:
                    time.sleep(self._retry_delay_seconds * (attempt + 1))
                continue
            if response.status_code < 200 or response.status_code >= 300:
                raise StripeConnectorError("Stripe native response status was not successful")
            try:
                payload = response.json()
            except (ValueError, UnicodeDecodeError):
                raise StripeConnectorError("Stripe native response is not valid JSON") from None
            if not isinstance(payload, Mapping):
                raise StripeConnectorError("Stripe native response must be an object")
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
        raise StripeConnectorError("Stripe native request failed")


def _request(
    transport: StripeRequestTransport,
    *,
    method: str,
    path: str,
    query: Mapping[str, str],
    api_key: str,
) -> Mapping[str, Any]:
    if method != "GET":
        raise StripeConnectorError("Stripe connector permits GET requests only")
    if not _allowed_path(path) or "?" in path or "#" in path:
        raise StripeConnectorError("Stripe request path is not an allowlisted /v1/ endpoint")
    try:
        assert_network_allowed("GET", STRIPE_HOST, path)
    except Exception as exc:
        raise StripeConnectorError(
            "Stripe request path is not an allowlisted /v1/ endpoint"
        ) from exc
    normalized_query = _validate_query(query)
    response = transport.request(
        method=method,
        path=path,
        query=normalized_query,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "Stripe-Version": STRIPE_VERSION,
        },
    )
    if not isinstance(response, Mapping):
        raise StripeConnectorError("Stripe transport returned a non-object response")
    return response


_RELATIONSHIP_FIELDS = {
    "customer": "customer_id",
    "subscription": "subscription_id",
    "invoice": "invoice_id",
    "payment_intent": "payment_intent_id",
    "charge": "charge_id",
    "price": "price_id",
    "product": "product_id",
}


def _normalize_record(item: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(item)
    normalized["id"] = str(item["id"])
    for source_field, normalized_field in _RELATIONSHIP_FIELDS.items():
        value = item.get(source_field)
        if value is not None and (
            not isinstance(value, (str, int)) or isinstance(value, bool) or not str(value)
        ):
            raise StripeConnectorError(f"Stripe {source_field} relationship is malformed")
        if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value):
            normalized[normalized_field] = str(value)
    return normalized


def _paginate(
    transport: StripeRequestTransport, *, path: str, api_key: str
) -> tuple[list[dict[str, Any]], int, list[str], list[str]]:
    cursor: str | None = None
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    request_ids: list[str] = []
    correlation_ids: list[str] = []
    pages = 0
    while True:
        query = {"limit": "100"}
        if cursor is not None:
            query["starting_after"] = cursor
        response = _request(transport, method="GET", path=path, query=query, api_key=api_key)
        pages += 1
        data = response.get("data")
        if not isinstance(data, list) or not isinstance(response.get("has_more"), bool):
            raise StripeConnectorError("Stripe list response requires data and has_more")
        for item in data:
            if (
                not isinstance(item, Mapping)
                or not isinstance(item.get("id"), (str, int))
                or isinstance(item.get("id"), bool)
                or not str(item.get("id"))
            ):
                raise StripeConnectorError("Stripe object requires an id")
            normalized = _normalize_record(item)
            if normalized["id"] in seen:
                raise StripeConnectorError("Stripe list response contains a duplicate id")
            seen.add(normalized["id"])
            records.append(normalized)
        for key, destination in (("request_id", request_ids), ("correlation_id", correlation_ids)):
            value = response.get(key)
            if value is not None:
                if not isinstance(value, str) or not value.strip():
                    raise StripeConnectorError(f"Stripe {key} must be a non-blank string")
                if value not in destination:
                    destination.append(value)
        if not response["has_more"]:
            break
        if not data:
            raise StripeConnectorError("Stripe has_more response requires a final record cursor")
        cursor = records[-1]["id"]
    return records, pages, request_ids, correlation_ids


def canonical_snapshot_bytes(snapshot: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()


def _relationship_rows(
    records: list[Mapping[str, Any]], *, child_type: str, parent_field: str, parent_type: str
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for record in records:
        child_id = str(record["id"])
        parent_id = record.get(parent_field)
        if isinstance(parent_id, (str, int)) and not isinstance(parent_id, bool) and str(parent_id):
            rows.append(
                {
                    "child_type": child_type,
                    "child_id": child_id,
                    "parent_type": parent_type,
                    "parent_id": str(parent_id),
                }
            )
    return sorted(rows, key=lambda row: (row["child_type"], row["child_id"], row["parent_id"]))


def _build_relationships(snapshot: Mapping[str, Any]) -> list[dict[str, str]]:
    specifications = (
        ("subscriptions", "subscription", "customer", "customer"),
        ("invoices", "invoice", "customer", "customer"),
        ("invoices", "invoice", "subscription", "subscription"),
        ("payment_intents", "payment_intent", "invoice", "invoice"),
        ("payment_intents", "payment_intent", "customer", "customer"),
        ("charges", "charge", "payment_intent", "payment_intent"),
        ("charges", "charge", "customer", "customer"),
        ("refunds", "refund", "charge", "charge"),
        ("products", "product", "id", "product"),
        ("prices", "price", "product", "product"),
    )
    relationships: list[dict[str, str]] = []
    for collection, child_type, raw_parent_field, parent_type in specifications:
        records = snapshot.get(collection)
        if not isinstance(records, list):
            raise StripeConnectorError(f"Stripe {collection} collection is malformed")
        if raw_parent_field == "id":
            continue
        relationships.extend(
            _relationship_rows(
                records,
                child_type=child_type,
                parent_field=raw_parent_field,
                parent_type=parent_type,
            )
        )
    for invoice_id, lines in (snapshot.get("invoice_lines") or {}).items():
        if not isinstance(invoice_id, str) or not isinstance(lines, list):
            raise StripeConnectorError("Stripe invoice lines are malformed")
        for line in lines:
            if not isinstance(line, Mapping) or not isinstance(line.get("id"), str):
                raise StripeConnectorError("Stripe invoice line is malformed")
            relationships.append(
                {
                    "child_type": "invoice_line",
                    "child_id": str(line["id"]),
                    "parent_type": "invoice",
                    "parent_id": invoice_id,
                }
            )
    return sorted(
        relationships,
        key=lambda row: (
            row["child_type"],
            row["child_id"],
            row["parent_type"],
            row["parent_id"],
        ),
    )


def _build_economic_units(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    invoices = snapshot.get("invoices")
    if not isinstance(invoices, list):
        raise StripeConnectorError("Stripe invoices collection is malformed")
    payment_intents = {
        str(item["id"]): item
        for item in snapshot.get("payment_intents", [])
        if isinstance(item, Mapping) and isinstance(item.get("id"), str)
    }
    charges = {
        str(item["id"]): item
        for item in snapshot.get("charges", [])
        if isinstance(item, Mapping) and isinstance(item.get("id"), str)
    }
    refunds = [item for item in snapshot.get("refunds", []) if isinstance(item, Mapping)]
    lines_by_invoice = snapshot.get("invoice_lines")
    if not isinstance(lines_by_invoice, Mapping):
        raise StripeConnectorError("Stripe invoice lines are malformed")

    units: list[dict[str, Any]] = []
    for invoice in invoices:
        if not isinstance(invoice, Mapping) or not isinstance(invoice.get("id"), str):
            raise StripeConnectorError("Stripe invoice is malformed")
        invoice_id = str(invoice["id"])
        unit: dict[str, Any] = {
            "economic_unit_key": f"stripe_invoice:{invoice_id}",
            "invoice_id": invoice_id,
            "payment_intent_ids": [],
            "charge_ids": [],
            "refund_ids": [],
            "invoice_line_ids": [],
        }
        for field in ("customer_id", "subscription_id"):
            value = invoice.get(field)
            if isinstance(value, str) and value:
                unit[field] = value
        for payment_intent_id, payment_intent in payment_intents.items():
            if payment_intent.get("invoice_id") == invoice_id:
                unit["payment_intent_ids"].append(payment_intent_id)
        for charge_id, charge in charges.items():
            if charge.get("payment_intent_id") in unit["payment_intent_ids"] or (
                charge.get("invoice_id") == invoice_id
            ):
                unit["charge_ids"].append(charge_id)
        for refund in refunds:
            if refund.get("charge_id") in unit["charge_ids"] and isinstance(refund.get("id"), str):
                unit["refund_ids"].append(str(refund["id"]))
        line_records = lines_by_invoice.get(invoice_id, [])
        if not isinstance(line_records, list):
            raise StripeConnectorError("Stripe invoice line collection is malformed")
        unit["invoice_line_ids"] = sorted(
            str(line["id"])
            for line in line_records
            if isinstance(line, Mapping) and isinstance(line.get("id"), str)
        )
        for key in ("payment_intent_ids", "charge_ids", "refund_ids"):
            unit[key] = sorted(set(unit[key]))
        units.append(unit)
    return sorted(units, key=lambda unit: str(unit["economic_unit_key"]))


def build_stripe_snapshot_receipt(
    snapshot: Mapping[str, Any], *, retrieved_at: datetime
) -> SourceReceiptV1:
    return build_source_receipt(
        source_type="stripe",
        connector_schema_version=CONNECTOR_SCHEMA_VERSION,
        retrieved_at=retrieved_at,
        locator_kind="endpoint",
        locator="/v1/customers",
        content=canonical_snapshot_bytes(snapshot),
        page_or_row_count=int(snapshot["page_or_row_count"]),
        record_count=int(snapshot["record_count"]),
        request_id=snapshot.get("request_id"),
        correlation_id=snapshot.get("correlation_id"),
    )


def write_stripe_snapshot(
    output_root: Path | str,
    snapshot: Mapping[str, Any],
    receipt: SourceReceiptV1,
    *,
    snapshot_path: str = "stripe/snapshot.json",
    receipt_path: str = "stripe/source-receipt.json",
    auditor: SafeRequestAuditor | None = None,
    assertion_path: str = "stripe/no-mutation-assertion.json",
) -> tuple[Path, Path]:
    from found_money.safety.audit import build_no_mutation_assertion, empty_fixture_assertion

    root = Path(output_root)
    snapshot_destination = _validate_relative_under_root(root, snapshot_path)
    receipt_destination = _validate_relative_under_root(root, receipt_path)
    assertion_destination = _validate_relative_under_root(root, assertion_path)
    destinations = {snapshot_destination, receipt_destination, assertion_destination}
    if len(destinations) != 3:
        raise StripeConnectorError("Stripe snapshot, receipt, and assertion paths must differ")
    expected = build_stripe_snapshot_receipt(snapshot, retrieved_at=receipt.retrieved_at)
    if (
        receipt.content_hash != expected.content_hash
        or receipt.request_id != expected.request_id
        or receipt.correlation_id != expected.correlation_id
    ):
        raise StripeConnectorError(
            "Stripe receipt does not match snapshot bytes or request metadata"
        )
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
            snapshot_path: canonical_snapshot_bytes(snapshot),
            receipt_path: receipt.to_canonical_json(),
            assertion_path: assertion.to_canonical_json(),
        },
    )
    return snapshot_destination, receipt_destination


class StripeConnector:
    """Normalize all allowlisted Stripe fixture resources through an injected transport."""

    def __init__(self, transport: StripeRequestTransport) -> None:
        self.transport = transport

    def fetch_snapshot(
        self,
        *,
        api_key: str | None = None,
        retrieved_at: datetime | None = None,
        output_root: Path | str | None = None,
        snapshot_path: str = "stripe/snapshot.json",
        receipt_path: str = "stripe/source-receipt.json",
    ) -> dict[str, Any]:
        credential = _credential(api_key)
        tracked_transport = _TrackingTransport(self.transport)
        snapshot: dict[str, Any] = {"connector_schema_version": CONNECTOR_SCHEMA_VERSION}
        page_count = 0
        record_count = 0
        resource_counts: dict[str, dict[str, int]] = {}
        request_ids: list[str] = []
        correlation_ids: list[str] = []
        for path, name in zip(RESOURCE_PATHS, RESOURCE_NAMES, strict=True):
            records, pages, requests, correlations = _paginate(
                tracked_transport, path=path, api_key=credential
            )
            snapshot[name] = records
            page_count += pages
            record_count += len(records)
            resource_counts[name] = {"pages": pages, "records": len(records)}
            request_ids.extend(value for value in requests if value not in request_ids)
            correlation_ids.extend(value for value in correlations if value not in correlation_ids)
        lines: dict[str, list[dict[str, Any]]] = {}
        line_pages = 0
        for invoice in snapshot["invoices"]:
            invoice_id = invoice["id"]
            records, pages, requests, correlations = _paginate(
                tracked_transport, path=f"/v1/invoices/{invoice_id}/lines", api_key=credential
            )
            lines[invoice_id] = records
            page_count += pages
            line_pages += pages
            record_count += len(records)
            request_ids.extend(value for value in requests if value not in request_ids)
            correlation_ids.extend(value for value in correlations if value not in correlation_ids)
        snapshot["invoice_lines"] = {key: lines[key] for key in sorted(lines)}
        snapshot["relationships"] = _build_relationships(snapshot)
        snapshot["economic_units"] = _build_economic_units(snapshot)
        resource_counts["invoice_lines"] = {
            "pages": line_pages,
            "records": sum(len(records) for records in lines.values()),
        }
        object_ids = [object_["id"] for name in RESOURCE_NAMES for object_ in snapshot[name]] + [
            line["id"] for records in lines.values() for line in records
        ]
        if len(object_ids) != len(set(object_ids)):
            raise StripeConnectorError("Stripe normalized snapshot contains a duplicate id")
        snapshot["page_or_row_count"] = page_count
        snapshot["record_count"] = record_count
        snapshot["resource_counts"] = resource_counts
        snapshot["request_ids"] = request_ids
        snapshot["correlation_ids"] = correlation_ids
        snapshot["request_id"] = request_ids[0] if request_ids else None
        snapshot["correlation_id"] = correlation_ids[0] if correlation_ids else None
        if output_root is not None:
            receipt = build_stripe_snapshot_receipt(
                snapshot, retrieved_at=retrieved_at or datetime.now(timezone.utc)
            )
            write_stripe_snapshot(
                output_root,
                snapshot,
                receipt,
                snapshot_path=snapshot_path,
                receipt_path=receipt_path,
                auditor=tracked_transport.auditor,
            )
        return snapshot


def fetch_stripe_snapshot(transport: StripeRequestTransport, **kwargs: Any) -> dict[str, Any]:
    """Convenience wrapper matching the existing connector's functional API."""
    return StripeConnector(transport).fetch_snapshot(**kwargs)


def fetch_native_stripe_snapshot(
    *,
    mode: str = "test",
    retrieved_at: datetime | None = None,
    output_root: Path | str | None = None,
    snapshot_path: str = "stripe/snapshot.json",
    receipt_path: str = "stripe/source-receipt.json",
) -> dict[str, Any]:
    """Fetch a private native snapshot using only ``STRIPE_API_KEY``.

    This function does not select credentials, persist them, or perform any
    write-capable Stripe operation.  A live call remains a separately
    authorized human gate; fixture transport is the normal automated path.
    """
    transport = StripeHttpTransport(mode=mode)
    try:
        return StripeConnector(transport).fetch_snapshot(
            retrieved_at=retrieved_at,
            output_root=output_root,
            snapshot_path=snapshot_path,
            receipt_path=receipt_path,
        )
    finally:
        transport.close()
