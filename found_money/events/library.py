"""Deterministic V1 event-family detection and exclusion selection.

The event library consumes already-normalized source mappings and an optional
identity graph. It never performs I/O against a provider, guesses an identity,
or assigns a monetary value. Raw source identifiers remain private lineage
evidence; the public projection contains aggregate counts only.
"""

from __future__ import annotations

import os
import json
import shutil
import tempfile
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from found_money.contracts.events import (
    ConfidenceClass,
    EventFamily,
    SUPPRESSION_PRIORITY,
    EventDataGapLedgerV1,
    EventDataGapV1,
    ExclusionLedgerV1,
    ExclusionRecordV1,
    PublicEventProjectionV1,
    RecoveryCandidateSetV1,
    RecoveryCandidateV1,
    SuppressionReason,
)
from found_money.contracts.identity import IdentityGraphV1
from found_money.identity import node_id_for
from found_money.receipts import _atomic_write_bytes, _validate_relative_under_root


DEFAULT_EVENT_BUILT_AT = datetime(2026, 7, 29, 18, 0, 0, tzinfo=timezone.utc)
EVENT_CANDIDATES_PATH = "events/recovery-candidates.json"
EVENT_EXCLUSIONS_PATH = "events/exclusion-ledger.json"
EVENT_DATA_GAPS_PATH = "events/data-gaps.json"
EVENT_PUBLIC_PATH = "events/public.json"

_SOURCE_COLLECTIONS: dict[str, tuple[str, ...]] = {
    "hubspot": ("contacts", "deals"),
    "stripe": (
        "customers",
        "subscriptions",
        "invoices",
        "payment_intents",
        "charges",
        "refunds",
        "products",
        "prices",
    ),
    "orders": ("orders",),
    "appointments": ("appointments",),
    "proposals": ("proposals",),
}
_IGNORED_COLLECTIONS = frozenset(
    {
        "relationships",
        "economic_units",
        "resource_counts",
        "request_ids",
        "correlation_ids",
        "property_definitions",
        "requested_properties",
        "unavailable_properties",
    }
)
_FAMILY_ALIASES = {
    "closed-lost/stale-deal": "closed_lost_stale_deal",
    "closed_lost_stale_deal": "closed_lost_stale_deal",
    "closed-lost-stale-deal": "closed_lost_stale_deal",
    "no-show/rebook": "no_show_rebook",
    "no_show_rebook": "no_show_rebook",
    "trial-no-convert": "trial_no_convert",
    "trial_no_convert": "trial_no_convert",
}


@dataclass(frozen=True)
class EventDetectionConfig:
    """Explicit deterministic thresholds used by aggregate event families."""

    stale_deal_days: int = 30
    proposal_silence_days: int = 14
    lapse_days: int = 90
    high_value_minor: int = 10_000
    reorder_days: int = 60
    renewal_window_days: int = 30

    def __post_init__(self) -> None:
        for name in (
            "stale_deal_days",
            "proposal_silence_days",
            "lapse_days",
            "high_value_minor",
            "reorder_days",
            "renewal_window_days",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")


@dataclass(frozen=True)
class EventDetectionResult:
    """Headline candidates plus inspectable exclusions and data gaps."""

    candidates: RecoveryCandidateSetV1
    exclusions: ExclusionLedgerV1
    data_gaps: EventDataGapLedgerV1
    public_projection: PublicEventProjectionV1

    @property
    def candidate_set(self) -> RecoveryCandidateSetV1:
        """Compatibility name used by the existing Money Map consumer."""
        return self.candidates

    @property
    def exclusion_ledger(self) -> ExclusionLedgerV1:
        return self.exclusions


@dataclass(frozen=True)
class _Record:
    source: str
    collection: str
    index: int
    values: dict[str, Any]
    reference: str
    customer_token: str | None = None
    identity_blocked: bool = False


@dataclass
class _Context:
    records: list[_Record]
    by_customer: dict[str, list[_Record]]
    source_payloads: dict[str, Mapping[str, Any]]
    graph: IdentityGraphV1 | None
    built_at: datetime
    run_id: str
    data_gaps: list[EventDataGapV1]


def _normalize_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().lower()


def _as_mapping(value: Any, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return value


def _record_id(values: Mapping[str, Any], index: int) -> str:
    for key in ("id", "order_id", "appointment_id", "proposal_id"):
        value = values.get(key)
        if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value).strip():
            return str(value).strip()
    return f"row-{index + 1}"


def _source_records(source: str, payload: Mapping[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Flatten normalized connector/file collections without inventing fields."""
    selected = _SOURCE_COLLECTIONS.get(source)
    collections = selected or tuple(
        key
        for key, value in payload.items()
        if key not in _IGNORED_COLLECTIONS and isinstance(value, list)
    )
    output: list[tuple[str, dict[str, Any]]] = []
    invoice_by_id: dict[str, Mapping[str, Any]] = {
        str(item.get("id")): item
        for item in payload.get("invoices", [])
        if isinstance(item, Mapping) and item.get("id") is not None
    }
    for collection in sorted(collections):
        raw_items = payload.get(collection)
        if not isinstance(raw_items, list):
            continue
        for item in raw_items:
            if not isinstance(item, Mapping):
                raise ValueError(f"{source}.{collection} records must be objects")
            output.append((collection, dict(item)))
    invoice_lines = payload.get("invoice_lines")
    if source == "stripe" and invoice_lines is not None:
        if not isinstance(invoice_lines, Mapping):
            raise ValueError("stripe.invoice_lines must be an object")
        for invoice_id in sorted(invoice_lines):
            rows = invoice_lines[invoice_id]
            if not isinstance(rows, list):
                raise ValueError("stripe invoice-line collections must be arrays")
            parent = invoice_by_id.get(str(invoice_id), {})
            for item in rows:
                if not isinstance(item, Mapping):
                    raise ValueError("stripe.invoice_lines records must be objects")
                values = dict(item)
                values["invoice_id"] = str(invoice_id)
                for key in ("customer_id", "subscription_id"):
                    if (
                        key not in values
                        and isinstance(parent, Mapping)
                        and parent.get(key) is not None
                    ):
                        values[key] = parent[key]
                output.append(("invoice_lines", values))
    return output


def _get(record: _Record, key: str) -> Any:
    value = record.values.get(key)
    if value is not None:
        return value
    properties = record.values.get("properties")
    if isinstance(properties, Mapping):
        return properties.get(key)
    return None


def _get_any(record: _Record, *keys: str) -> Any:
    for key in keys:
        value = _get(record, key)
        if value is not None:
            return value
    return None


def _parse_time(value: Any, *, label: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"{label} must be an ISO-8601 timestamp") from exc
    else:
        raise ValueError(f"{label} must be an ISO-8601 timestamp")
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must be timezone-aware")
    return parsed.astimezone(timezone.utc).replace(microsecond=(parsed.microsecond // 1000) * 1000)


def _time(record: _Record, *keys: str) -> datetime | None:
    for key in keys:
        value = _get(record, key)
        if value is not None:
            return _parse_time(value, label=f"{record.reference}.{key}")
    return None


def _record_time(record: _Record) -> datetime | None:
    return _time(
        record,
        "event_at",
        "occurred_at",
        "attempted_at",
        "paid_at",
        "canceled_at",
        "cancelled_at",
        "closed_at",
        "closedate",
        "ordered_at",
        "purchased_at",
        "scheduled_at",
        "proposed_at",
        "renewal_at",
        "renewal_date",
        "trial_end",
        "trial_ends_at",
        "current_period_end",
        "last_activity_at",
        "created_at",
        "created",
    )


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return False


def _integer(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else None
    if isinstance(value, str):
        try:
            parsed = Decimal(value.strip())
        except InvalidOperation:
            return None
        return int(parsed) if parsed == parsed.to_integral_value() else None
    return None


def _status(record: _Record) -> str:
    value = _get_any(record, "status", "state", "lifecycle_stage", "deal_stage", "dealstage")
    return _normalize_text(value)


def _history(record: _Record) -> list[Mapping[str, Any]]:
    raw = _get_any(record, "status_history", "event_history")
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, Mapping)]
    raw_properties = record.values.get("propertiesWithHistory")
    if isinstance(raw_properties, Mapping):
        output: list[Mapping[str, Any]] = []
        for key in sorted(raw_properties):
            entries = raw_properties[key]
            if isinstance(entries, list):
                output.extend(item for item in entries if isinstance(item, Mapping))
        return output
    return []


def _history_time(entry: Mapping[str, Any], *, reference: str) -> datetime | None:
    for key in ("event_at", "occurred_at", "timestamp", "created_at", "created"):
        if entry.get(key) is not None:
            return _parse_time(entry[key], label=f"{reference}.{key}")
    return None


def _history_has(
    record: _Record, states: set[str]
) -> tuple[datetime | None, Mapping[str, Any] | None]:
    matches: list[tuple[datetime | None, Mapping[str, Any]]] = []
    for entry in _history(record):
        state = _normalize_text(
            entry.get("status")
            or entry.get("state")
            or entry.get("stage")
            or entry.get("value")
            or entry.get("outcome")
        )
        if state in states:
            matches.append((_history_time(entry, reference=record.reference), entry))
    matches.sort(key=lambda item: item[0] or datetime.min.replace(tzinfo=timezone.utc))
    return matches[-1] if matches else (None, None)


def _family_hint(record: _Record, family: str) -> bool:
    raw = _normalize_text(_get_any(record, "event_family", "family"))
    normalized = _FAMILY_ALIASES.get(raw, raw.replace(" ", "_"))
    return normalized == family


def _record_identifier(record: _Record) -> str:
    return _record_id(record.values, record.index)


def _identity_signal(record: _Record) -> bool:
    return any(
        _get(record, key) is not None
        for key in (
            "customer_id",
            "customer_token",
            "synthetic_customer_key",
            "identity_node_id",
            "identity_node_ids",
            "email",
            "phone",
        )
    )


def _hubspot_contact_ids(record: _Record, payload: Mapping[str, Any]) -> list[str]:
    if record.collection == "contacts":
        return [_record_identifier(record)]
    if record.collection != "deals":
        return []
    values: set[str] = set()
    links = payload.get("deal_to_contact_links")
    if isinstance(links, list):
        for link in links:
            if isinstance(link, Mapping) and str(link.get("deal_id")) == _record_identifier(record):
                if link.get("contact_id") is not None:
                    values.add(str(link["contact_id"]))
    association_map = payload.get("associations")
    if isinstance(association_map, Mapping):
        linked = association_map.get(_record_identifier(record))
        if isinstance(linked, list):
            for item in linked:
                if isinstance(item, Mapping):
                    contact_id = item.get("contact_id") or item.get("toObjectId") or item.get("id")
                    if contact_id is not None:
                        values.add(str(contact_id))
                elif item is not None:
                    values.add(str(item))
    associations = record.values.get("associations")
    if isinstance(associations, Mapping):
        contacts = associations.get("contacts")
        if isinstance(contacts, Mapping) and isinstance(contacts.get("results"), list):
            for item in contacts["results"]:
                if isinstance(item, Mapping) and item.get("id") is not None:
                    values.add(str(item["id"]))
    return sorted(values)


def _node_ids_for_record(record: _Record, payload: Mapping[str, Any]) -> list[str]:
    explicit = _get_any(record, "identity_node_id")
    if isinstance(explicit, str) and explicit.strip():
        return [explicit.strip()]
    explicit_many = _get_any(record, "identity_node_ids")
    if isinstance(explicit_many, list):
        return sorted({str(item).strip() for item in explicit_many if str(item).strip()})
    source = record.source
    if source == "stripe":
        customer_id = _get_any(record, "customer_id")
        if customer_id is not None:
            return [node_id_for("stripe", "customer", str(customer_id))]
        if record.collection == "customers":
            return [node_id_for("stripe", "customer", _record_identifier(record))]
    if source == "hubspot":
        contacts = _hubspot_contact_ids(record, payload)
        return [node_id_for("hubspot", "contact", item) for item in contacts]
    customer_id = _get_any(record, "customer_id")
    if customer_id is not None:
        return [
            node_id_for(source, "customer", str(customer_id)),
            node_id_for(source, "contact", str(customer_id)),
        ]
    return []


def _build_context(
    sources: Mapping[str, Any],
    graph: IdentityGraphV1 | None,
    built_at: datetime,
    run_id: str,
) -> _Context:
    source_payloads: dict[str, Mapping[str, Any]] = {}
    raw_records: list[_Record] = []
    for source_name in sorted(sources):
        source_value = sources[source_name]
        if isinstance(source_value, Mapping):
            payload = source_value
        elif isinstance(source_value, list):
            payload = {source_name: source_value}
        else:
            raise ValueError(f"source {source_name} must be an object or list")
        source = str(source_name).strip().lower()
        source_payloads[source] = payload
        for index, (collection, values) in enumerate(_source_records(source, payload)):
            identifier = _record_id(values, index)
            raw_records.append(
                _Record(
                    source=source,
                    collection=collection,
                    index=index,
                    values=values,
                    reference=f"{source}/{collection}/{identifier}",
                )
            )

    node_to_customer: dict[str, str] = {}
    ambiguous_nodes: set[str] = set()
    graph_tokens: set[str] = set()
    if graph is not None:
        for cluster in graph.customers:
            graph_tokens.add(cluster.customer_token)
            for node_id in cluster.member_node_ids:
                node_to_customer[node_id] = cluster.customer_token
        for ambiguous_cluster in graph.ambiguous_identities:
            ambiguous_nodes.update(ambiguous_cluster.member_node_ids)

    records: list[_Record] = []
    for record in raw_records:
        node_ids = _node_ids_for_record(record, source_payloads.get(record.source, {}))
        blocked = bool(set(node_ids).intersection(ambiguous_nodes))
        tokens = {node_to_customer[node_id] for node_id in node_ids if node_id in node_to_customer}
        explicit_token = _get_any(record, "customer_token")
        token: str | None = None
        if not blocked and len(tokens) == 1:
            token = next(iter(tokens))
        elif not blocked and len(tokens) > 1:
            blocked = True
        elif not blocked and isinstance(explicit_token, str) and explicit_token.strip():
            candidate_token = explicit_token.strip()
            if (
                graph is None
                or candidate_token in graph_tokens
                or candidate_token.startswith("cust_")
            ):
                token = candidate_token
        records.append(
            _Record(
                source=record.source,
                collection=record.collection,
                index=record.index,
                values=record.values,
                reference=record.reference,
                customer_token=token,
                identity_blocked=blocked,
            )
        )

    by_customer: dict[str, list[_Record]] = defaultdict(list)
    data_gaps: list[EventDataGapV1] = []
    for record in records:
        if record.customer_token is not None:
            by_customer[record.customer_token].append(record)
        elif _identity_signal(record):
            data_gaps.append(
                EventDataGapV1(
                    run_id=run_id,
                    reason_code="ambiguous_identity"
                    if record.identity_blocked
                    else "missing_identity",
                    source_reference=record.reference,
                    detail=(
                        "identity cluster is quarantined"
                        if record.identity_blocked
                        else "no resolved customer token is available"
                    ),
                )
            )
    for token in by_customer:
        by_customer[token].sort(key=lambda item: item.reference)
    return _Context(records, dict(by_customer), source_payloads, graph, built_at, run_id, data_gaps)


def _records(ctx: _Context, source: str, collection: str) -> list[_Record]:
    return [
        record
        for record in ctx.records
        if record.source == source and record.collection == collection
    ]


def _customer_records(ctx: _Context, token: str) -> list[_Record]:
    return ctx.by_customer.get(token, [])


def _candidate(
    ctx: _Context,
    family: EventFamily,
    records: Sequence[_Record],
    *,
    token: str,
    economic_unit_key: str,
    event_at: datetime | None,
    confidence: ConfidenceClass,
    evidence: Mapping[str, Any],
) -> RecoveryCandidateV1:
    ordered = sorted(records, key=lambda item: item.reference)
    when = event_at or ctx.built_at
    recency = max((ctx.built_at - when).days, 0)
    lineage = {
        "source_reference": ordered[0].reference,
        "source_system": ordered[0].source,
    }
    for index, record in enumerate(ordered[1:], start=2):
        lineage[f"source_reference_{index}"] = record.reference
    evidence_values = {key: str(value) for key, value in evidence.items() if value is not None}
    evidence_values.setdefault("event_family", family)
    return RecoveryCandidateV1(
        run_id=ctx.run_id,
        event_family=family,
        confidence_class=confidence,
        economic_unit_key=economic_unit_key,
        customer_token=token,
        lineage=lineage,
        qualifying_evidence=evidence_values,
        event_at=when,
        recency_days=recency,
        evidence_references=[record.reference for record in ordered],
    )


def _unit(record: _Record, prefix: str | None = None) -> str:
    explicit = _get(record, "economic_unit_key")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    name = _record_identifier(record)
    return f"{prefix or record.source + '_' + record.collection}:{name}"


def _add_candidate(
    candidates: dict[str, RecoveryCandidateV1], candidate: RecoveryCandidateV1
) -> None:
    current = candidates.get(candidate.candidate_key)
    if current is None or (
        (candidate.event_at or datetime.max.replace(tzinfo=timezone.utc)),
        tuple(candidate.evidence_references),
    ) > (
        (current.event_at or datetime.max.replace(tzinfo=timezone.utc)),
        tuple(current.evidence_references),
    ):
        candidates[candidate.candidate_key] = candidate


def _failed_payment_candidates(ctx: _Context, out: dict[str, RecoveryCandidateV1]) -> None:
    failed_states = {"payment_failed", "failed", "past_due", "uncollectible", "unpaid"}
    for record in _records(ctx, "stripe", "invoices"):
        status = _status(record)
        outcome = _normalize_text(_get(record, "collection_outcome"))
        history_at, history_entry = _history_has(record, failed_states)
        failed = status in {"open", "past_due", "unpaid", "uncollectible"} and (
            outcome in failed_states or _bool(_get(record, "payment_failed"))
        )
        if not failed and history_entry is None:
            continue
        token = record.customer_token
        if token is None:
            continue
        event_at = history_at or _time(record, "attempted_at", "event_at") or _record_time(record)
        candidate = _candidate(
            ctx,
            "failed_payment",
            [record],
            token=token,
            economic_unit_key=_unit(record, "stripe_invoice"),
            event_at=event_at,
            confidence="observed",
            evidence={
                "status": status,
                "collection_outcome": outcome,
                "source": "stripe.invoice",
            },
        )
        _add_candidate(out, candidate)


def _trial_end(record: _Record) -> datetime | None:
    return _time(record, "trial_end", "trial_ends_at", "trial_end_at")


def _trial_candidates(ctx: _Context, out: dict[str, RecoveryCandidateV1]) -> None:
    for record in _records(ctx, "stripe", "subscriptions"):
        token = record.customer_token
        if token is None:
            continue
        trial_end = _trial_end(record)
        status = _status(record)
        converted = _bool(_get_any(record, "converted", "converted_to_paid", "paid_conversion"))
        if trial_end is not None and trial_end <= ctx.built_at and not converted:
            if status in {"trialing", "trial", "pending_trial"} or _family_hint(
                record, "expired_trial"
            ):
                _add_candidate(
                    out,
                    _candidate(
                        ctx,
                        "expired_trial",
                        [record],
                        token=token,
                        economic_unit_key=_unit(record, "stripe_subscription"),
                        event_at=trial_end,
                        confidence="observed",
                        evidence={"status": status, "trial_end": trial_end.isoformat()},
                    ),
                )
            if status in {"canceled", "cancelled", "expired", "incomplete_expired"} or _family_hint(
                record, "trial_no_convert"
            ):
                _add_candidate(
                    out,
                    _candidate(
                        ctx,
                        "trial_no_convert",
                        [record],
                        token=token,
                        economic_unit_key=_unit(record, "stripe_subscription"),
                        event_at=trial_end,
                        confidence="derived",
                        evidence={"status": status, "trial_end": trial_end.isoformat()},
                    ),
                )


def _deal_candidates(
    ctx: _Context, config: EventDetectionConfig, out: dict[str, RecoveryCandidateV1]
) -> None:
    closed_states = {"closed_lost", "closedlost", "closed-lost", "lost"}
    for record in _records(ctx, "hubspot", "deals"):
        token = record.customer_token
        if token is None:
            continue
        status = _status(record)
        history_at, history_entry = _history_has(record, closed_states)
        closed_at = (
            history_at
            or _time(record, "closed_at", "closedate", "event_at")
            or _record_time(record)
        )
        if (status not in closed_states and history_entry is None) or closed_at is None:
            continue
        if ctx.built_at - closed_at < timedelta(days=config.stale_deal_days):
            continue
        _add_candidate(
            out,
            _candidate(
                ctx,
                "closed_lost_stale_deal",
                [record],
                token=token,
                economic_unit_key=_unit(record, "hubspot_deal"),
                event_at=closed_at,
                confidence="observed",
                evidence={"deal_stage": status, "closed_at": closed_at.isoformat()},
            ),
        )


def _canceled_customer_candidates(ctx: _Context, out: dict[str, RecoveryCandidateV1]) -> None:
    for record in ctx.records:
        if record.collection not in {"subscriptions", "customers", "contacts"}:
            continue
        token = record.customer_token
        if token is None:
            continue
        status = _status(record)
        if status not in {"canceled", "cancelled", "churned", "inactive", "closed"} and not _bool(
            _get_any(record, "canceled", "cancelled", "churned")
        ):
            continue
        event_at = _time(record, "canceled_at", "cancelled_at", "event_at") or _record_time(record)
        _add_candidate(
            out,
            _candidate(
                ctx,
                "canceled_customer",
                [record],
                token=token,
                economic_unit_key=_unit(record, f"{record.source}_{record.collection}"),
                event_at=event_at,
                confidence="observed",
                evidence={"status": status, "source": record.reference},
            ),
        )


def _order_groups(ctx: _Context) -> dict[str, list[_Record]]:
    groups: dict[str, list[_Record]] = defaultdict(list)
    for record in _records(ctx, "orders", "orders"):
        if record.customer_token is not None and _record_time(record) is not None:
            groups[record.customer_token].append(record)
    for records in groups.values():
        records.sort(key=lambda item: (_record_time(item) or ctx.built_at, item.reference))
    return groups


def _order_candidates(
    ctx: _Context, config: EventDetectionConfig, out: dict[str, RecoveryCandidateV1]
) -> None:
    for token, orders in _order_groups(ctx).items():
        if len(orders) < 2:
            continue
        latest_old = [
            order
            for order in orders
            if ctx.built_at - (_record_time(order) or ctx.built_at)
            >= timedelta(days=config.lapse_days)
        ]
        if latest_old:
            last_old = latest_old[-1]
            event_at = _record_time(last_old)
            assert event_at is not None
            last_old_index = orders.index(last_old)
            refs = orders[max(0, last_old_index - 1) : last_old_index + 1]
            _add_candidate(
                out,
                _candidate(
                    ctx,
                    "lapsed_repeat_buyer",
                    refs,
                    token=token,
                    economic_unit_key=f"customer:{token}:lapsed_repeat_buyer",
                    event_at=event_at,
                    confidence="derived",
                    evidence={"order_count": len(orders), "last_order_at": event_at.isoformat()},
                ),
            )
        high_value = [
            order
            for order in orders
            if (_integer(_get_any(order, "total_minor", "amount_minor", "amount_due_cents")) or -1)
            >= config.high_value_minor
        ]
        stale_high_value = [
            order
            for order in high_value
            if ctx.built_at - (_record_time(order) or ctx.built_at)
            >= timedelta(days=config.lapse_days)
        ]
        if stale_high_value:
            last_high = stale_high_value[-1]
            event_at = _record_time(last_high)
            if event_at is not None:
                _add_candidate(
                    out,
                    _candidate(
                        ctx,
                        "disappeared_high_value_customer",
                        [last_high],
                        token=token,
                        economic_unit_key=f"customer:{token}:disappeared_high_value_customer",
                        event_at=event_at,
                        confidence="derived",
                        evidence={
                            "amount_minor": _integer(
                                _get_any(
                                    last_high, "total_minor", "amount_minor", "amount_due_cents"
                                )
                            ),
                            "last_high_value_order_at": event_at.isoformat(),
                        },
                    ),
                )
        stale_reorders = [
            order
            for order in orders
            if ctx.built_at - (_record_time(order) or ctx.built_at)
            >= timedelta(days=config.reorder_days)
        ]
        if stale_reorders:
            latest = stale_reorders[-1]
            latest_at = _record_time(latest)
            assert latest_at is not None
            latest_index = orders.index(latest)
            _add_candidate(
                out,
                _candidate(
                    ctx,
                    "overdue_reorder",
                    orders[max(0, latest_index - 1) : latest_index + 1],
                    token=token,
                    economic_unit_key=f"customer:{token}:overdue_reorder",
                    event_at=latest_at,
                    confidence="derived",
                    evidence={"order_count": len(orders), "last_order_at": latest_at.isoformat()},
                ),
            )


def _payment_amount_minor(record: _Record) -> int | None:
    amount = _integer(
        _get_any(record, "amount_minor", "amount_due_cents", "amount_cents", "total_minor")
    )
    if amount is not None:
        return amount
    if record.collection in {"charges", "payment_intents", "invoices"}:
        return _integer(_get_any(record, "amount"))
    return None


def _payment_disappeared_candidates(
    ctx: _Context, config: EventDetectionConfig, out: dict[str, RecoveryCandidateV1]
) -> None:
    """Narrow service/payment extension of disappeared-high-value. Does not use orders."""
    paid_states = {"paid", "succeeded", "complete", "completed"}
    by_token: dict[str, list[_Record]] = defaultdict(list)
    for record in ctx.records:
        if record.source != "stripe" or record.collection not in {
            "invoices",
            "charges",
            "payment_intents",
        }:
            continue
        if record.customer_token is None:
            continue
        status = _status(record)
        paid = status in paid_states or _normalize_text(_get(record, "collection_outcome")) in {
            "payment_succeeded",
            "succeeded",
            "paid",
        }
        if not paid:
            continue
        amount = _payment_amount_minor(record)
        event_at = _time(record, "paid_at", "event_at") or _record_time(record)
        if amount is None or event_at is None:
            continue
        if amount < config.high_value_minor:
            continue
        if ctx.built_at - event_at < timedelta(days=config.lapse_days):
            continue
        by_token[record.customer_token].append(record)
    for token, records in by_token.items():
        records.sort(key=lambda item: (_record_time(item) or ctx.built_at, item.reference))
        last = records[-1]
        event_at = _time(last, "paid_at", "event_at") or _record_time(last)
        if event_at is None:
            continue
        _add_candidate(
            out,
            _candidate(
                ctx,
                "disappeared_high_value_customer",
                [last],
                token=token,
                economic_unit_key=f"customer:{token}:disappeared_high_value_customer",
                event_at=event_at,
                confidence="derived",
                evidence={
                    "amount_minor": _payment_amount_minor(last),
                    "last_high_value_payment_at": event_at.isoformat(),
                    "source": last.reference,
                },
            ),
        )


def _proposal_candidates(
    ctx: _Context, config: EventDetectionConfig, out: dict[str, RecoveryCandidateV1]
) -> None:
    for record in _records(ctx, "proposals", "proposals"):
        token = record.customer_token
        if token is None:
            continue
        status = _status(record)
        history_at, history_entry = _history_has(record, {"sent", "draft", "open"})
        event_at = history_at or _time(record, "proposed_at", "event_at") or _record_time(record)
        if event_at is None or ctx.built_at - event_at < timedelta(
            days=config.proposal_silence_days
        ):
            continue
        if (
            status not in {"sent", "draft", "open", "accepted", "rejected", "expired"}
            and history_entry is None
        ):
            continue
        if (
            status in {"sent", "draft", "open"}
            or history_entry is not None
            or _family_hint(record, "silent_proposal")
        ):
            _add_candidate(
                out,
                _candidate(
                    ctx,
                    "silent_proposal",
                    [record],
                    token=token,
                    economic_unit_key=_unit(record, "proposal"),
                    event_at=event_at,
                    confidence="observed",
                    evidence={"status": status, "proposed_at": event_at.isoformat()},
                ),
            )


def _appointment_candidates(ctx: _Context, out: dict[str, RecoveryCandidateV1]) -> None:
    for record in _records(ctx, "appointments", "appointments"):
        token = record.customer_token
        if token is None or _status(record) != "no_show":
            continue
        event_at = _time(record, "scheduled_at", "event_at") or _record_time(record)
        _add_candidate(
            out,
            _candidate(
                ctx,
                "no_show_rebook",
                [record],
                token=token,
                economic_unit_key=_unit(record, "appointment"),
                event_at=event_at,
                confidence="observed",
                evidence={
                    "status": "no_show",
                    "scheduled_at": event_at.isoformat() if event_at else None,
                },
            ),
        )


def _renewal_candidates(
    ctx: _Context, config: EventDetectionConfig, out: dict[str, RecoveryCandidateV1]
) -> None:
    deadline = ctx.built_at + timedelta(days=config.renewal_window_days)
    for record in _records(ctx, "stripe", "subscriptions"):
        token = record.customer_token
        if token is None or _status(record) not in {"active", "trialing"}:
            continue
        renewal_at = _time(record, "renewal_at", "renewal_date", "current_period_end")
        if not (
            _bool(_get(record, "renewal_due"))
            or (renewal_at is not None and renewal_at <= deadline)
        ):
            continue
        _add_candidate(
            out,
            _candidate(
                ctx,
                "renewal_upsell",
                [record],
                token=token,
                economic_unit_key=_unit(record, "stripe_subscription"),
                event_at=renewal_at or ctx.built_at,
                confidence="observed",
                evidence={
                    "status": _status(record),
                    "renewal_at": renewal_at.isoformat() if renewal_at else "due",
                },
            ),
        )


def _engagement_candidates(ctx: _Context, out: dict[str, RecoveryCandidateV1]) -> None:
    for record in ctx.records:
        if record.collection not in {"contacts", "customers"} or record.customer_token is None:
            continue
        score = _integer(_get_any(record, "engagement_score", "engagement_count"))
        engaged = _bool(_get_any(record, "engaged", "recently_engaged", "engagement_event"))
        if not engaged and not (score is not None and score > 0):
            continue
        event_at = _time(record, "engagement_at", "last_activity_at", "event_at") or _record_time(
            record
        )
        _add_candidate(
            out,
            _candidate(
                ctx,
                "engaged_unbooked",
                [record],
                token=record.customer_token,
                economic_unit_key=f"customer:{record.customer_token}:engaged_unbooked",
                event_at=event_at,
                confidence="derived",
                evidence={
                    "engaged": "true",
                    "engagement_score": score if score is not None else "present",
                },
            ),
        )


def _after(record: _Record, event_at: datetime) -> bool:
    when = _record_time(record)
    return when is not None and when > event_at


def _record_flag(record: _Record, *keys: str) -> bool:
    return any(_bool(_get(record, key)) for key in keys)


def _suppression_options(
    candidate: RecoveryCandidateV1, ctx: _Context
) -> dict[SuppressionReason, tuple[dict[str, str], list[str]]]:
    options: dict[SuppressionReason, tuple[dict[str, str], list[str]]] = {}
    event_at = candidate.event_at or ctx.built_at
    records = _customer_records(ctx, candidate.customer_token)

    def add(reason: SuppressionReason, record: _Record, detail: str) -> None:
        current = options.get(reason)
        value = (
            {"source_reference": record.reference, "detail": detail},
            [record.reference],
        )
        if current is None or value[1] < current[1]:
            options[reason] = value

    for record in records:
        if _record_flag(
            record,
            "do_not_contact",
            "disqualified",
            "explicit_disqualification",
            "email_opt_out",
            "sms_opt_out",
            "invalid_address",
        ):
            add("explicit_disqualification", record, "explicit disqualification or opt-out")
        status = _status(record)
        if status in {"fraud", "fraudulent", "disputed", "chargeback", "dispute"} or _record_flag(
            record, "fraud", "dispute", "disputed", "chargeback"
        ):
            add("fraud_dispute", record, "fraud or dispute state")

        if record.source == "stripe" and record.collection in {
            "invoices",
            "payment_intents",
            "charges",
        }:
            paid = status in {"paid", "succeeded", "complete", "completed"} or _normalize_text(
                _get(record, "collection_outcome")
            ) in {"payment_succeeded", "succeeded", "paid"}
            same_record_history = (
                record.reference == candidate.lineage.get("source_reference")
                and _history_has(record, {"payment_failed", "failed", "past_due"})[1] is not None
            )
            if paid and (
                candidate.event_family in {"failed_payment", "expired_trial", "trial_no_convert"}
                and (
                    _after(record, event_at)
                    or same_record_history
                    or record.reference != candidate.lineage.get("source_reference")
                )
            ):
                add("later_payment", record, "a later payment succeeded")

        if candidate.event_family in {"expired_trial", "trial_no_convert", "canceled_customer"}:
            if status in {"active", "reactivated", "trialing", "paid"} and (
                _after(record, event_at)
                or record.reference != candidate.lineage.get("source_reference")
            ):
                add("reactivation", record, "customer or subscription is active again")
            if _record_flag(record, "reactivated", "reactivation"):
                add("reactivation", record, "explicit reactivation evidence")

        if candidate.event_family in {"no_show_rebook", "engaged_unbooked"}:
            if (
                record.collection == "appointments"
                and _status(record) in {"scheduled", "completed"}
                and _after(record, event_at)
            ):
                add("rebooking", record, "a later appointment was booked or completed")

        if candidate.event_family in {
            "lapsed_repeat_buyer",
            "disappeared_high_value_customer",
            "overdue_reorder",
        }:
            if record.collection == "orders" and _after(record, event_at):
                add("purchase", record, "a later order was recorded")
            if (
                candidate.event_family == "disappeared_high_value_customer"
                and record.source == "stripe"
                and record.collection in {"invoices", "payment_intents", "charges"}
            ):
                paid = status in {"paid", "succeeded", "complete", "completed"} or _normalize_text(
                    _get(record, "collection_outcome")
                ) in {"payment_succeeded", "succeeded", "paid"}
                if paid and _after(record, event_at):
                    add("later_payment", record, "a later payment succeeded")

        if candidate.event_family == "silent_proposal" and record.collection == "proposals":
            status = _status(record)
            history_sent = _history_has(record, {"sent", "draft", "open"})[1] is not None
            if status == "accepted" and (
                record.reference == candidate.lineage.get("source_reference")
                or record.reference not in candidate.evidence_references
                or _after(record, event_at)
            ):
                add("purchase", record, "proposal was accepted")
            elif status == "rejected" and (
                record.reference == candidate.lineage.get("source_reference")
                or record.reference not in candidate.evidence_references
                or _after(record, event_at)
            ):
                add("explicit_disqualification", record, "proposal was rejected")
            elif (
                history_sent
                and status in {"sent", "draft", "open"}
                and record.reference != candidate.lineage.get("source_reference")
            ):
                add("active_negotiation", record, "proposal remains active")

        if candidate.event_family in {"closed_lost_stale_deal", "silent_proposal"}:
            if (
                record.collection == "deals"
                and _status(record)
                not in {
                    "closed_lost",
                    "closedlost",
                    "closed-lost",
                    "lost",
                    "closed_won",
                    "closedwon",
                    "won",
                }
                and record.reference != candidate.lineage.get("source_reference")
            ):
                add("active_negotiation", record, "an active deal remains")

        if _record_flag(record, "active_service_issue", "service_issue", "open_case"):
            add("active_service_issue", record, "an active service issue is present")

        owner_activity = _time(record, "owner_activity_at", "recent_owner_activity_at")
        if owner_activity is not None and owner_activity > event_at:
            add("recent_owner_activity", record, "owner activity occurred after the event")

    return options


def _apply_suppressions(
    ctx: _Context,
    candidates: Sequence[RecoveryCandidateV1],
) -> tuple[list[RecoveryCandidateV1], ExclusionLedgerV1]:
    headline: list[RecoveryCandidateV1] = []
    exclusions: list[ExclusionRecordV1] = []
    for candidate in candidates:
        options = _suppression_options(candidate, ctx)
        if not options:
            headline.append(candidate)
            continue
        ordered = sorted(options, key=lambda item: (SUPPRESSION_PRIORITY[item], item))
        reason = ordered[0]
        evidence, refs = options[reason]
        competing = [item for item in ordered[1:]]
        exclusions.append(
            ExclusionRecordV1(
                run_id=candidate.run_id,
                candidate_key=candidate.candidate_key,
                event_family=candidate.event_family,
                economic_unit_key=candidate.economic_unit_key,
                customer_token=candidate.customer_token,
                reason_code=reason,
                priority=SUPPRESSION_PRIORITY[reason],
                lineage=dict(candidate.lineage),
                disqualifying_evidence=evidence,
                evidence_references=refs,
                competing_reason_codes=competing,
            )
        )
    ledger = ExclusionLedgerV1(
        run_id=ctx.run_id,
        built_at=ctx.built_at,
        exclusions=exclusions,
    )
    return headline, ledger


def _public_projection(
    candidates: RecoveryCandidateSetV1,
    exclusions: ExclusionLedgerV1,
    data_gaps: EventDataGapLedgerV1,
) -> PublicEventProjectionV1:
    by_family: dict[str, int] = defaultdict(int)
    by_reason: dict[str, int] = defaultdict(int)
    for candidate in candidates.candidates:
        by_family[candidate.event_family] += 1
    for exclusion in exclusions.exclusions:
        by_reason[exclusion.reason_code] += 1
    from found_money.contracts.events import public_named_data_gaps

    return PublicEventProjectionV1(
        run_id=candidates.run_id,
        built_at=candidates.built_at,
        candidate_count=len(candidates.candidates),
        exclusion_count=len(exclusions.exclusions),
        data_gap_count=len(data_gaps.gaps),
        candidates_by_family=dict(by_family),
        exclusions_by_reason=dict(by_reason),
        named_data_gaps=public_named_data_gaps([gap.detail for gap in data_gaps.gaps]),
    )


def detect_event_families(
    sources: Mapping[str, Any],
    graph: IdentityGraphV1 | None = None,
    *,
    run_id: str = "run_event_detection",
    built_at: datetime | None = None,
    config: EventDetectionConfig | None = None,
) -> EventDetectionResult:
    """Detect all named families and deterministically apply exclusions.

    ``sources`` must already be normalized by FM-019/020/021 or by the typed
    file contracts. The optional graph is authoritative for identity: records
    in an ambiguous cluster are withheld and surfaced as data gaps.
    """
    when = built_at or DEFAULT_EVENT_BUILT_AT
    if when.tzinfo is None:
        raise ValueError("built_at must be timezone-aware")
    when = when.astimezone(timezone.utc).replace(microsecond=(when.microsecond // 1000) * 1000)
    if graph is not None and graph.run_id != run_id:
        raise ValueError("identity graph run_id must match event run_id")
    ctx = _build_context(sources, graph, when, run_id)
    thresholds = config or EventDetectionConfig()
    generated: dict[str, RecoveryCandidateV1] = {}
    _failed_payment_candidates(ctx, generated)
    _trial_candidates(ctx, generated)
    _deal_candidates(ctx, thresholds, generated)
    _canceled_customer_candidates(ctx, generated)
    _order_candidates(ctx, thresholds, generated)
    _payment_disappeared_candidates(ctx, thresholds, generated)
    _proposal_candidates(ctx, thresholds, generated)
    _appointment_candidates(ctx, generated)
    _renewal_candidates(ctx, thresholds, generated)
    _engagement_candidates(ctx, generated)
    all_candidates = sorted(generated.values(), key=lambda item: item.candidate_key)
    headline, exclusions = _apply_suppressions(ctx, all_candidates)
    candidate_set = RecoveryCandidateSetV1(run_id=run_id, built_at=when, candidates=headline)
    # Rebind empty-ledger and graph-free runs to the caller's explicit run ID.
    if exclusions.run_id != run_id:
        exclusions = ExclusionLedgerV1(
            run_id=run_id,
            built_at=when,
            exclusions=[
                item.model_copy(update={"run_id": run_id}) for item in exclusions.exclusions
            ],
        )
    data_gaps = EventDataGapLedgerV1(
        run_id=run_id,
        built_at=when,
        gaps=[item.model_copy(update={"run_id": run_id}) for item in ctx.data_gaps],
    )
    public = _public_projection(candidate_set, exclusions, data_gaps)
    return EventDetectionResult(candidate_set, exclusions, data_gaps, public)


def detect_events(*args: Any, **kwargs: Any) -> EventDetectionResult:
    """Compatibility alias for callers that use the shorter detector name."""
    return detect_event_families(*args, **kwargs)


def write_event_detection(
    output_root: Path | str,
    result: EventDetectionResult,
    *,
    candidates_path: str = EVENT_CANDIDATES_PATH,
    exclusions_path: str = EVENT_EXCLUSIONS_PATH,
    data_gaps_path: str = EVENT_DATA_GAPS_PATH,
    public_path: str = EVENT_PUBLIC_PATH,
) -> dict[str, Path]:
    """Atomically commit private candidate/exclusion files and an aggregate view."""
    final_root = Path(output_root)
    if final_root.exists() or final_root.is_symlink():
        raise ValueError("event output root must not already exist")
    parent = final_root.parent if final_root.parent != Path("") else Path.cwd()
    if not parent.exists() or not parent.is_dir() or parent.is_symlink():
        raise ValueError("event output parent must be an existing directory")
    stage = Path(tempfile.mkdtemp(prefix=".found-money-events-", dir=parent))
    payloads = {
        candidates_path: result.candidates.to_canonical_json(),
        exclusions_path: result.exclusions.to_canonical_json(),
        data_gaps_path: result.data_gaps.to_canonical_json(),
        public_path: result.public_projection.to_canonical_json(),
    }
    try:
        for relative, payload in payloads.items():
            _atomic_write_bytes(_validate_relative_under_root(stage, relative), payload)
        os.replace(stage, final_root)
        return {relative: final_root / relative for relative in payloads}
    finally:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)


def _duplicate_key_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _parse_json(data: bytes | bytearray) -> Mapping[str, Any]:
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("canonical event JSON must be bytes")
    try:
        payload = json.loads(bytes(data).decode("utf-8"), object_pairs_hook=_duplicate_key_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"malformed canonical event JSON: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("canonical event JSON root must be an object")
    return payload


def parse_exclusion_ledger(data: bytes | bytearray) -> ExclusionLedgerV1:
    payload = _parse_json(data)
    ledger = ExclusionLedgerV1.model_validate(payload)
    if ledger.to_canonical_json() != bytes(data):
        raise ValueError("serialized exclusion ledger is not canonical")
    return ledger


def parse_data_gap_ledger(data: bytes | bytearray) -> EventDataGapLedgerV1:
    payload = _parse_json(data)
    ledger = EventDataGapLedgerV1.model_validate(payload)
    if ledger.to_canonical_json() != bytes(data):
        raise ValueError("serialized data-gap ledger is not canonical")
    return ledger


def parse_public_event_projection(data: bytes | bytearray) -> PublicEventProjectionV1:
    payload = _parse_json(data)
    projection = PublicEventProjectionV1.model_validate(payload)
    if projection.to_canonical_json() != bytes(data):
        raise ValueError("serialized public event projection is not canonical")
    return projection


__all__ = [
    "DEFAULT_EVENT_BUILT_AT",
    "EVENT_CANDIDATES_PATH",
    "EVENT_DATA_GAPS_PATH",
    "EVENT_EXCLUSIONS_PATH",
    "EVENT_PUBLIC_PATH",
    "EventDetectionConfig",
    "EventDetectionResult",
    "detect_event_families",
    "detect_events",
    "parse_data_gap_ledger",
    "parse_exclusion_ledger",
    "parse_public_event_projection",
    "write_event_detection",
]
