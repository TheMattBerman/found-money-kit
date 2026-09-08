"""Independently recomputable ecommerce situation evidence."""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, Mapping, Sequence

from found_money.contracts.cart import (
    EcommerceOptionalCartV1,
    cart_qualification_reason,
    omitted_cart_binding,
)
from found_money.contracts.events import EventDataGapV1
from found_money.contracts.identity import IdentityGraphV1
from found_money.contracts.scenarios import ScenarioSituationEvidenceV1, ScenarioSituationSetV1
from found_money.contracts.value import normalize_currency, normalize_minor_units
from found_money.identity import node_id_for
from found_money.receipts import sha256_bytes

_CART_JOIN_RULE = "declared_external_id"
_REFUND_JOIN_RULE = "same_source_object_id"
FORBIDDEN_PUBLIC_SITUATION_KEYS = frozenset(
    {
        "account_token",
        "amount_minor",
        "cart_id",
        "charge_id",
        "customer_external_id",
        "customer_id",
        "customer_token",
        "join_rule",
        "member_node_ids",
        "member_rows",
        "members",
        "order_id",
        "refund_id",
        "source_id",
    }
)


def _canonical_json_bytes(payload: Any) -> bytes:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return (text + "\n").encode("utf-8")


def _evidence_digest(facts: Sequence[Mapping[str, Any]]) -> str:
    ordered = sorted(
        (dict(item) for item in facts),
        key=lambda item: json.dumps(
            item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ),
    )
    return sha256_bytes(_canonical_json_bytes(ordered))


def _cluster_for_node_id(graph: IdentityGraphV1, node_id: str):
    for cluster in graph.customers:
        if node_id in cluster.member_node_ids:
            return cluster
    return None


def _cluster_for_orders_customer(graph: IdentityGraphV1, external_id: str):
    matches = []
    for cluster in graph.customers:
        nodes = [node for node in graph.nodes if node.node_id in cluster.member_node_ids]
        if any(
            node.source_id == external_id
            or node.external_ids.get("orders.customer_id") == external_id
            for node in nodes
        ):
            matches.append(cluster)
    if len(matches) != 1:
        return None
    return matches[0]


def _refund_rows(stripe: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    rows = stripe.get("refunds") or []
    if not isinstance(rows, list):
        raise ValueError("stripe refunds must be an array")
    out: list[Mapping[str, Any]] = []
    for item in rows:
        if not isinstance(item, Mapping):
            raise ValueError("stripe refund records must be objects")
        out.append(item)
    return out


def _index_by_id(rows: Any, *, label: str) -> dict[str, Mapping[str, Any]]:
    if not isinstance(rows, list):
        raise ValueError(f"{label} must be an array")
    indexed: dict[str, Mapping[str, Any]] = {}
    for item in rows:
        if not isinstance(item, Mapping):
            raise ValueError(f"{label} records must be objects")
        identifier = item.get("id")
        if not isinstance(identifier, str) or not identifier.strip():
            raise ValueError(f"{label} records require string ids")
        indexed[identifier.strip()] = item
    return indexed


def _totals_from_facts(facts: Sequence[Mapping[str, Any]]) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = {}
    for item in facts:
        currency = normalize_currency(str(item["currency"]))
        amount = normalize_minor_units(item["amount"])
        totals[currency] = totals.get(currency, Decimal(0)) + amount
    return dict(sorted(totals.items()))


def _shared_state(facts: Sequence[Mapping[str, Any]]) -> str | None:
    states = {str(item["state"]) for item in facts if item.get("state") is not None}
    if len(states) == 1:
        return next(iter(states))
    return None


def _public_aggregate(
    *,
    situation_id: Literal["refunded_buyer_next_move", "high_value_cart"],
    status: Literal["present", "withheld", "omitted"],
    facts: Sequence[Mapping[str, Any]],
    withhold_reason: str | None = None,
) -> ScenarioSituationEvidenceV1:
    return ScenarioSituationEvidenceV1(
        situation_id=situation_id,
        status=status,
        qualifying_count=len(facts),
        totals_minor_by_currency=_totals_from_facts(facts),
        evidence_digest=_evidence_digest(facts),
        state=_shared_state(facts),
        withhold_reason=withhold_reason,
    )


def assert_public_situation_payload(payload: Mapping[str, Any]) -> None:
    """Reject customer-level keys from a public situation artifact."""

    def walk(value: Any) -> None:
        if isinstance(value, Mapping):
            leaked = FORBIDDEN_PUBLIC_SITUATION_KEYS.intersection(value)
            if leaked:
                raise ValueError(
                    "situation evidence leaked customer-level key " + ", ".join(sorted(leaked))
                )
            for nested in value.values():
                walk(nested)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(payload)


def build_refunded_buyer_evidence(
    snapshots: Mapping[str, Mapping[str, Any]],
    graph: IdentityGraphV1,
    *,
    run_id: str,
) -> ScenarioSituationEvidenceV1:
    stripe = snapshots.get("stripe") or {}
    refunds = _refund_rows(stripe)
    charges = _index_by_id(stripe.get("charges") or [], label="stripe.charges")
    qualifying: list[dict[str, Any]] = []
    for refund in refunds:
        refund_id = refund.get("id")
        charge_id = refund.get("charge")
        status = refund.get("status")
        if not isinstance(refund_id, str) or not isinstance(charge_id, str):
            continue
        if not isinstance(status, str) or status.strip().lower() != "succeeded":
            continue
        charge = charges.get(charge_id.strip())
        if charge is None:
            continue
        customer_id = charge.get("customer")
        if not isinstance(customer_id, str) or not customer_id.strip():
            continue
        cluster = _cluster_for_node_id(
            graph, node_id_for("stripe", "customer", customer_id.strip())
        )
        if cluster is None:
            continue
        amount = normalize_minor_units(refund.get("amount"))
        currency = normalize_currency(str(refund.get("currency")))
        # Customer linkage and source IDs stay in-memory for digest recomputation.
        qualifying.append(
            {
                "amount": str(amount),
                "charge_id": charge_id.strip(),
                "currency": currency,
                "customer_id": customer_id.strip(),
                "join_rule": _REFUND_JOIN_RULE,
                "refund_id": refund_id.strip(),
                "status": status.strip().lower(),
                "state": status.strip().lower(),
            }
        )
    if len(qualifying) != 1:
        return _public_aggregate(
            situation_id="refunded_buyer_next_move",
            status="withheld",
            facts=qualifying,
            withhold_reason="missing_refunded_buyer_next_move",
        )
    return _public_aggregate(
        situation_id="refunded_buyer_next_move",
        status="present",
        facts=qualifying,
    )


def build_high_value_cart_evidence(
    cart: EcommerceOptionalCartV1,
    graph: IdentityGraphV1,
    *,
    clock: datetime,
) -> ScenarioSituationEvidenceV1:
    if cart.state == "omitted":
        return _public_aggregate(
            situation_id="high_value_cart",
            status="omitted",
            facts=(),
            withhold_reason="missing_high_value_cart",
        )
    qualifying: list[dict[str, Any]] = []
    withheld_reasons: list[str] = []
    for row in cart.carts:
        cluster = _cluster_for_orders_customer(graph, row.customer_external_id)
        reason = cart_qualification_reason(row, clock=clock, joined=cluster is not None)
        facts = {
            "amount": str(row.total_minor),
            "cart_id": row.cart_id,
            "currency": row.currency,
            "customer_external_id": row.customer_external_id,
            "join_rule": _CART_JOIN_RULE if cluster is not None else None,
            "observed_at": row.canonical_dict()["observed_at"],
            "state": row.state,
        }
        if reason is None and cluster is not None:
            qualifying.append(facts)
            continue
        withheld_reasons.append(reason or "wrong_customer_high_value_cart")
    if len(qualifying) == 1:
        return _public_aggregate(
            situation_id="high_value_cart",
            status="present",
            facts=qualifying,
        )
    if withheld_reasons:
        return _public_aggregate(
            situation_id="high_value_cart",
            status="withheld",
            facts=qualifying,
            withhold_reason=withheld_reasons[0],
        )
    return _public_aggregate(
        situation_id="high_value_cart",
        status="withheld",
        facts=qualifying,
        withhold_reason="missing_high_value_cart",
    )


def build_ecommerce_situation_set(
    *,
    run_id: str,
    cart: EcommerceOptionalCartV1,
    snapshots: Mapping[str, Mapping[str, Any]],
    graph: IdentityGraphV1,
    clock: datetime,
) -> ScenarioSituationSetV1:
    refund = build_refunded_buyer_evidence(snapshots, graph, run_id=run_id)
    high_value = build_high_value_cart_evidence(cart, graph, clock=clock)
    payload = ScenarioSituationSetV1(
        run_id=run_id,
        high_value_cart_state=cart.state,
        high_value_cart_hash=cart.sha256(),
        situations=[refund, high_value],
    )
    assert_public_situation_payload(json.loads(payload.to_canonical_json().decode("utf-8")))
    return payload


def situation_data_gaps(
    situations: ScenarioSituationSetV1,
    *,
    run_id: str,
) -> list[EventDataGapV1]:
    gaps: list[EventDataGapV1] = []
    for item in situations.situations:
        if item.status == "present" or item.withhold_reason is None:
            continue
        source = "optional-cart" if item.situation_id == "high_value_cart" else "stripe.refunds"
        gaps.append(
            EventDataGapV1(
                run_id=run_id,
                reason_code="missing_evidence",
                source_reference=source,
                detail=item.withhold_reason,
            )
        )
    return gaps


def cart_or_omitted(cart: EcommerceOptionalCartV1 | None) -> EcommerceOptionalCartV1:
    return omitted_cart_binding() if cart is None else cart
