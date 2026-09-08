"""Reusable fixture transports that invoke production HubSpot/Stripe connectors."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from collections.abc import Callable
from typing import Any, Mapping

from found_money.connectors import hubspot as hubspot_mod
from found_money.connectors.hubspot import (
    OBJECTS_ROOT,
    PROPERTIES_ROOT,
    canonical_snapshot_bytes as hubspot_canonical_bytes,
)
from found_money.connectors.stripe import (
    API_ROOT,
    RESOURCE_PATHS,
    StripeConnector,
    StripeTransport,
    build_stripe_snapshot_receipt,
    canonical_snapshot_bytes as stripe_canonical_bytes,
)
from found_money.contracts.source import SourceReceiptV1
from found_money.contracts.source_stage import SourceDeclarationV1, parse_source_manifest
from found_money.imports.appointments import parse_appointments_file_bytes
from found_money.imports.orders import parse_orders_file_bytes
from found_money.imports.proposals import parse_proposals_file_bytes
from found_money.receipts import sha256_bytes
from found_money.scenarios.assets import (
    SYNTHETIC_ECOMMERCE_V1,
    SYNTHETIC_SAAS_V1,
    load_optional_cart_binding,
)
from found_money.scenarios.registry import ECOMMERCE_V1_ROOT, SAAS_V1_ROOT, scenario_arm
from found_money.source_stage import SourceArtifact

TRANSPORTS_ROOT = SAAS_V1_ROOT / "transports"
HUBSPOT_TRANSPORT_ROOT = TRANSPORTS_ROOT / "hubspot"
STRIPE_PAGES_PATH = TRANSPORTS_ROOT / "stripe" / "pages.json"
ECOMMERCE_STRIPE_PAGES_PATH = ECOMMERCE_V1_ROOT / "transports" / "stripe" / "pages.json"
ECOMMERCE_ORDERS_PATH = ECOMMERCE_V1_ROOT / "inputs" / "orders.csv"
ECOMMERCE_CART_PATH = ECOMMERCE_V1_ROOT / "optional" / "high-value-cart.json"
SOURCE_MANIFEST_PATH = SAAS_V1_ROOT / "source-manifest.json"
HUBSPOT_FIXTURE_TOKEN = "fixture-token"
STRIPE_FIXTURE_KEY = "fixture-stripe-key"


class HubSpotFixtureTransport:
    """Injected HubSpot transport backed by packaged fixture pages."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self.calls: list[dict[str, Any]] = []
        self._responses: dict[tuple[str, str | None], Path] = {
            (f"{PROPERTIES_ROOT}/contacts", None): root / "contacts-properties.json",
            (f"{PROPERTIES_ROOT}/deals", None): root / "deals-properties.json",
            (f"{OBJECTS_ROOT}/contacts", None): root / "contacts.json",
            (f"{OBJECTS_ROOT}/deals", None): root / "deals.json",
        }

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
        fixture = self._responses.get((path, query.get("after")))
        if fixture is None:
            raise ValueError("no injected HubSpot fixture response for request")
        return json.loads(fixture.read_text(encoding="utf-8"))


def stripe_fixture_transport(
    pages: Mapping[str, Any] | None = None,
    *,
    pages_path: Path | None = None,
    request_id: str = "req_saas_v1",
    correlation_id: str = "corr_saas_v1",
) -> StripeTransport:
    payload = pages or json.loads((pages_path or STRIPE_PAGES_PATH).read_text(encoding="utf-8"))
    responses: dict[tuple[str, str | None], dict[str, Any]] = {}
    for path in RESOURCE_PATHS:
        name = path.rsplit("/", 1)[-1]
        page_list = payload[name]
        for index, data in enumerate(page_list):
            cursor = None if index == 0 else page_list[index - 1][-1]["id"]
            responses[(path, cursor)] = {
                "data": data,
                "has_more": index < len(page_list) - 1,
                "request_id": request_id,
                "correlation_id": correlation_id,
            }
    for invoice_id, invoice_pages in payload.get("invoice_lines", {}).items():
        line_path = f"{API_ROOT}/invoices/{invoice_id}/lines"
        for index, data in enumerate(invoice_pages):
            cursor = None if index == 0 else invoice_pages[index - 1][-1]["id"]
            responses[(line_path, cursor)] = {
                "data": data,
                "has_more": index < len(invoice_pages) - 1,
                "request_id": request_id,
                "correlation_id": correlation_id,
            }
    return StripeTransport(responses)


def fetch_packaged_hubspot_snapshot(
    *, retrieved_at: datetime, transport_root: Path | None = None
) -> tuple[dict[str, Any], bytes]:
    snapshot = hubspot_mod.fetch_crm_snapshot(
        HubSpotFixtureTransport(transport_root or HUBSPOT_TRANSPORT_ROOT),
        token=HUBSPOT_FIXTURE_TOKEN,
        retrieved_at=retrieved_at,
    )
    return snapshot, hubspot_canonical_bytes(snapshot)


def fetch_packaged_stripe_snapshot(
    *,
    retrieved_at: datetime,
    pages_path: Path | None = None,
    request_id: str = "req_saas_v1",
    correlation_id: str = "corr_saas_v1",
) -> tuple[dict[str, Any], bytes]:
    snapshot = StripeConnector(
        stripe_fixture_transport(
            pages_path=pages_path, request_id=request_id, correlation_id=correlation_id
        )
    ).fetch_snapshot(
        api_key=STRIPE_FIXTURE_KEY,
        retrieved_at=retrieved_at,
    )
    return snapshot, stripe_canonical_bytes(snapshot)


def packaged_source_manifest(fixture_id: str = SYNTHETIC_SAAS_V1):
    arm = scenario_arm(fixture_id)
    return parse_source_manifest((arm.root / arm.source_manifest_file).read_bytes())


def packaged_fixture_hashes(fixture_id: str = SYNTHETIC_SAAS_V1) -> dict[str, str]:
    arm = scenario_arm(fixture_id)
    files: dict[str, str] = {}
    hubspot_root = arm.hubspot_transport_root()
    if hubspot_root is not None:
        files["hubspot"] = _hash_tree(hubspot_root)
    stripe_pages = arm.stripe_pages_path()
    if stripe_pages is not None:
        files["stripe"] = sha256_bytes(stripe_pages.read_bytes())
    orders = arm.orders_input_path()
    if orders is not None:
        files["orders"] = sha256_bytes(orders.read_bytes())
    appointments = arm.appointments_input_path()
    if appointments is not None:
        files["appointments"] = sha256_bytes(appointments.read_bytes())
    proposals = arm.proposals_input_path()
    if proposals is not None:
        files["proposals"] = sha256_bytes(proposals.read_bytes())
    return dict(sorted(files.items()))


def connector_schema_hashes(fixture_id: str = SYNTHETIC_SAAS_V1) -> dict[str, str]:
    from found_money.connectors.hubspot import CONNECTOR_SCHEMA_VERSION as hubspot_version
    from found_money.connectors.hubspot import HUBSPOT_API_VERSION
    from found_money.connectors.stripe import CONNECTOR_SCHEMA_VERSION as stripe_version
    from found_money.connectors.stripe import STRIPE_VERSION

    arm = scenario_arm(fixture_id)
    hashes: dict[str, str] = {}
    if arm.stripe_pages_path() is not None:
        hashes[stripe_version] = sha256_bytes(f"{stripe_version}\n{STRIPE_VERSION}\n".encode())
    if arm.hubspot_transport_root() is not None:
        hashes[hubspot_version] = sha256_bytes(
            f"{hubspot_version}\n{HUBSPOT_API_VERSION}\n".encode()
        )
    if arm.orders_input_path() is not None:
        hashes["orders.v1"] = sha256_bytes(b"orders.v1\n")
    if arm.appointments_input_path() is not None:
        hashes["appointments.v1"] = sha256_bytes(b"appointments.v1\n")
    if arm.proposals_input_path() is not None:
        hashes["proposals.v1"] = sha256_bytes(b"proposals.v1\n")
    return hashes


def native_scenario_adapters(
    *, retrieved_at: datetime, fixture_id: str = SYNTHETIC_SAAS_V1
) -> dict[str, Any]:
    arm = scenario_arm(fixture_id)

    def hubspot_fetch(
        declaration: SourceDeclarationV1, *, retrieved_at: datetime
    ) -> SourceArtifact:
        snapshot, raw = fetch_packaged_hubspot_snapshot(
            retrieved_at=retrieved_at, transport_root=arm.hubspot_transport_root()
        )
        receipt = hubspot_mod.build_crm_snapshot_receipt(snapshot, retrieved_at=retrieved_at)
        return SourceArtifact(normalized_bytes=raw, receipt=receipt)

    def stripe_fetch(declaration: SourceDeclarationV1, *, retrieved_at: datetime) -> SourceArtifact:
        snapshot, raw = fetch_packaged_stripe_snapshot(
            retrieved_at=retrieved_at,
            pages_path=arm.stripe_pages_path(),
            request_id=arm.stripe_request_id,
            correlation_id=arm.stripe_correlation_id,
        )
        receipt = build_stripe_snapshot_receipt(snapshot, retrieved_at=retrieved_at)
        return SourceArtifact(normalized_bytes=raw, receipt=receipt)

    return {"hubspot": hubspot_fetch, "stripe": stripe_fetch}


def _file_snapshot(
    path: Path,
    *,
    source_type: str,
    schema_version: str,
    retrieved_at: datetime,
    parse: Callable[..., Any],
    count: Callable[[Any], int],
) -> tuple[dict[str, Any], bytes, SourceReceiptV1]:
    from found_money.receipts import build_source_receipt

    parsed = parse(path.read_bytes(), filename=path.name)
    raw = parsed.to_canonical_json()
    receipt = build_source_receipt(
        source_type=source_type,
        connector_schema_version=schema_version,
        retrieved_at=retrieved_at,
        locator_kind="input_path",
        locator=path.name if path.parent.name != "inputs" else f"inputs/{path.name}",
        content=path.read_bytes(),
        page_or_row_count=count(parsed),
        record_count=count(parsed),
    )
    return json.loads(raw.decode("utf-8")), raw, receipt


def load_normalized_scenario_snapshots(
    fixture_id: str = SYNTHETIC_SAAS_V1,
    *,
    retrieved_at: datetime,
) -> tuple[dict[str, dict[str, Any]], dict[str, bytes], dict[str, SourceReceiptV1]]:
    arm = scenario_arm(fixture_id)
    snapshots: dict[str, dict[str, Any]] = {}
    raw: dict[str, bytes] = {}
    receipts: dict[str, SourceReceiptV1] = {}
    hubspot_root = arm.hubspot_transport_root()
    if hubspot_root is not None:
        hubspot, hubspot_bytes = fetch_packaged_hubspot_snapshot(
            retrieved_at=retrieved_at, transport_root=hubspot_root
        )
        snapshots["hubspot"] = hubspot
        raw["hubspot"] = hubspot_bytes
        receipts["hubspot"] = hubspot_mod.build_crm_snapshot_receipt(
            hubspot, retrieved_at=retrieved_at
        )
    stripe_pages = arm.stripe_pages_path()
    if stripe_pages is not None:
        stripe, stripe_bytes = fetch_packaged_stripe_snapshot(
            retrieved_at=retrieved_at,
            pages_path=stripe_pages,
            request_id=arm.stripe_request_id,
            correlation_id=arm.stripe_correlation_id,
        )
        snapshots["stripe"] = stripe
        raw["stripe"] = stripe_bytes
        receipts["stripe"] = build_stripe_snapshot_receipt(stripe, retrieved_at=retrieved_at)
    orders_path = arm.orders_input_path()
    if orders_path is not None:
        data, data_bytes, receipt = _file_snapshot(
            orders_path,
            source_type="orders",
            schema_version="orders.v1",
            retrieved_at=retrieved_at,
            parse=parse_orders_file_bytes,
            count=lambda item: len(item.orders),
        )
        snapshots["orders"] = data
        raw["orders"] = data_bytes
        receipts["orders"] = receipt
    appointments_path = arm.appointments_input_path()
    if appointments_path is not None:
        data, data_bytes, receipt = _file_snapshot(
            appointments_path,
            source_type="appointments",
            schema_version="appointments.v1",
            retrieved_at=retrieved_at,
            parse=parse_appointments_file_bytes,
            count=lambda item: len(item.appointments),
        )
        snapshots["appointments"] = data
        raw["appointments"] = data_bytes
        receipts["appointments"] = receipt
    proposals_path = arm.proposals_input_path()
    if proposals_path is not None:
        data, data_bytes, receipt = _file_snapshot(
            proposals_path,
            source_type="proposals",
            schema_version="proposals.v1",
            retrieved_at=retrieved_at,
            parse=parse_proposals_file_bytes,
            count=lambda item: len(item.proposals),
        )
        snapshots["proposals"] = data
        raw["proposals"] = data_bytes
        receipts["proposals"] = receipt
    if not snapshots:
        raise ValueError("unknown scenario fixture")
    return snapshots, raw, receipts


def derive_raw_source_ids(snapshots: Mapping[str, Mapping[str, Any]]) -> frozenset[str]:
    """Collect raw provider object IDs and synthetic keys from normalized snapshots."""
    found: set[str] = set()
    for payload in snapshots.values():
        _collect_ids(payload, found)
    return frozenset(found)


def _collect_ids(value: Any, found: set[str]) -> None:
    if isinstance(value, Mapping):
        for key in ("id", "appointment_id", "proposal_id", "customer_id", "order_id"):
            identifier = value.get(key)
            if isinstance(identifier, str) and _looks_like_source_id(identifier):
                found.add(identifier)
        synthetic_key = value.get("synthetic_customer_key")
        if isinstance(synthetic_key, str) and synthetic_key.strip():
            found.add(synthetic_key.strip())
        properties = value.get("properties")
        if isinstance(properties, Mapping):
            nested = properties.get("synthetic_customer_key")
            if isinstance(nested, str) and nested.strip():
                found.add(nested.strip())
        for nested_value in value.values():
            _collect_ids(nested_value, found)
    elif isinstance(value, list):
        for item in value:
            _collect_ids(item, found)


def load_optional_cart(*, supplied: bool = True, fixture_id: str = SYNTHETIC_ECOMMERCE_V1):
    if not supplied:
        from found_money.contracts.cart import omitted_cart_binding

        return omitted_cart_binding()
    return load_optional_cart_binding(fixture_id)


def _looks_like_source_id(value: str) -> bool:
    if len(value) < 6:
        return False
    prefixes = (
        "hs_",
        "cus_",
        "sub_",
        "in_",
        "pi_",
        "ch_",
        "re_",
        "prod_",
        "price_",
        "il_",
        "ord_",
        "cart_",
        "apt_",
        "prp_",
    )
    return value.startswith(prefixes)


def _hash_tree(root: Path) -> str:
    parts: list[bytes] = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            relative = path.relative_to(root).as_posix().encode("utf-8")
            parts.append(relative + b"\n" + path.read_bytes())
    return sha256_bytes(b"\0".join(parts))
