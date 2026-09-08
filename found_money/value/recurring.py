"""Pure recurring valuation over the already-selected, deduplicated ledger."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Mapping, Literal

from found_money.contracts.value import ContributionLedgerV1, ContributionV1, RecurringValuationV1
from found_money.strategy.intelligence import load_table
from found_money.value import (
    _candidate_records,
    _flatten_record,
    _gap_for_candidate,
    _record_amount,
    _record_currency,
)


def _utc(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else None
    except ValueError:
        return None


def apply_recurring_valuation(
    ledger: ContributionLedgerV1,
    candidates: Any,
    snapshots: Mapping[str, Any],
    graph: Any,
    *,
    business_model: str | None,
    clock: datetime,
    tenure_months: Decimal | None = None,
    ltv_override_minor: Decimal | None = None,
) -> ContributionLedgerV1:
    policy = load_table("recurring-valuation")
    if business_model not in policy["business_models"]:
        return ledger
    if tenure_months is not None and (not tenure_months.is_finite() or tenure_months <= 0):
        raise ValueError("tenure multiple must be finite and positive")
    if ltv_override_minor is not None and (
        not ltv_override_minor.is_finite() or ltv_override_minor <= 0
    ):
        raise ValueError("LTV override must be finite and positive")
    subscriptions = snapshots.get("stripe", {}).get("subscriptions", [])
    nodes = {node.node_id: node for node in graph.nodes}
    customers = {
        cluster.customer_token: {
            nodes[n].source_id
            for n in cluster.member_node_ids
            if n in nodes
            and nodes[n].source_system == "stripe"
            and nodes[n].object_type == "customer"
        }
        for cluster in graph.customers
    }
    resolved_ids = set().union(*customers.values()) if customers else set()
    history: list[Decimal] = []
    history_records = []
    for raw in subscriptions:
        record = _flatten_record(raw)
        if record.get("status") != "canceled" or record.get("customer_id") not in resolved_ids:
            continue
        start, end = _utc(record.get("created_at")), _utc(record.get("canceled_at"))
        if start is None or end is None or not start < end <= clock:
            continue
        seconds = Decimal(str((end - start).total_seconds()))
        history.append(seconds / (Decimal(policy["days_per_month"]) * 86400))
        history_records.append(raw)
    multiple = tenure_months
    if multiple is None and history:
        multiple = (sum(history, Decimal(0)) / Decimal(len(history))).quantize(
            Decimal(policy["tenure_precision"]), rounding=ROUND_HALF_UP
        )
    if multiple is None and ltv_override_minor is not None:
        multiple = Decimal(1)
    by_key = {c.candidate_key: c for c in candidates.candidates}
    retained = []
    gaps = list(ledger.data_gaps)
    for row in ledger.contributions:
        candidate = by_key.get(row.candidate_key)
        if (
            candidate is not None
            and candidate.event_family in policy["prospect_subscription_families"]
        ):
            gaps.append(
                _gap_for_candidate(
                    candidate,
                    "missing_amount",
                    "A trial subscription rate is not a recorded prospect deal value.",
                    row.currency,
                    [],
                )
            )
            continue
        if candidate is None or candidate.event_family not in policy["customer_lapse_families"]:
            retained.append(row)
            continue
        matched = _candidate_records(candidate, subscriptions)
        if not matched:
            ids = customers.get(candidate.customer_token, set())
            matched = [r for r in subscriptions if _flatten_record(r).get("customer_id") in ids]
        source = _flatten_record(matched[0]) if len(matched) == 1 else {}
        amount = _record_amount(source) if source else None
        currency = _record_currency(source) if source else None
        interval = source.get("billing_interval")
        if (
            amount is None
            or amount <= 0
            or currency != row.currency
            or interval != policy["monthly_interval"]
            or multiple is None
        ):
            gaps.append(
                _gap_for_candidate(
                    candidate,
                    "insufficient_history",
                    "Monthly amount or tenure is not supported by the supplied subscription evidence.",
                    row.currency,
                    [],
                )
            )
            continue
        expected = (
            ltv_override_minor
            if ltv_override_minor is not None
            else (amount * multiple).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        )
        basis: Literal["source_tenure", "owner_tenure", "owner_ltv_override"] = (
            "owner_ltv_override"
            if ltv_override_minor is not None
            else ("owner_tenure" if tenure_months is not None else "source_tenure")
        )
        digest = hashlib.sha256(
            json.dumps(
                {
                    "subscription": matched[0],
                    "tenure_history": history_records if tenure_months is None else [],
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        model = RecurringValuationV1(
            currency=currency,
            monthly_amount_minor=amount,
            tenure_multiple_months=multiple,
            expected_value_minor=expected,
            basis=basis,
            history_count=len(history) if tenure_months is None else 0,
            source_digest=digest,
            ltv_override_minor=ltv_override_minor,
        )
        data = row.model_dump()
        data.update(
            value_basis="modeled_opportunity",
            amount_minor=expected,
            modeled_opportunity=None,
            recurring_valuation=model,
        )
        retained.append(ContributionV1.model_validate(data))
    return ContributionLedgerV1(
        run_id=ledger.run_id,
        built_at=ledger.built_at,
        contributions=retained,
        data_gaps=gaps,
        overlap_ledger=ledger.overlap_ledger,
    )


def public_valuation_receipt(ledger: ContributionLedgerV1) -> dict[str, Any]:
    """Aggregate only: one recomputable tier per currency, no customer rows."""
    currencies: dict[str, dict[str, Any]] = {}
    for row in ledger.contributions:
        bucket = currencies.setdefault(
            row.currency,
            {
                "total_minor": 0,
                "observed_minor": 0,
                "modeled_minor": 0,
                "recorded_minor": 0,
                "selected_units": 0,
            },
        )
        tier = (
            "modeled_minor"
            if row.value_basis == "modeled_opportunity"
            else (
                "recorded_minor"
                if row.pile_id in load_table("recurring-valuation")["recorded_families"]
                else "observed_minor"
            )
        )
        bucket[tier] += int(row.amount_minor)
        bucket["total_minor"] += int(row.amount_minor)
        bucket["selected_units"] += 1
    models = [
        r.recurring_valuation for r in ledger.contributions if r.recurring_valuation is not None
    ]
    model_groups: dict[tuple[str, ...], dict[str, Any]] = {}
    for m in models:
        key = (
            m.currency,
            str(m.monthly_amount_minor),
            str(m.tenure_multiple_months),
            m.basis,
            str(m.expected_value_minor),
        )
        group = model_groups.setdefault(
            key,
            {
                "currency": m.currency,
                "monthly_amount_minor": str(m.monthly_amount_minor),
                "tenure_months": str(m.tenure_multiple_months),
                "basis": m.basis,
                "unit_value_minor": str(m.expected_value_minor),
                "unit_count": 0,
                "total_minor": 0,
            },
        )
        group["unit_count"] += 1
        group["total_minor"] += int(m.expected_value_minor)
    return {
        "schema_version": "valuation-summary.v1",
        "model_groups": [model_groups[key] for key in sorted(model_groups)],
        "run_id": ledger.run_id,
        "currencies": currencies,
        "unquantified_units": len(
            {g.candidate_key for g in ledger.data_gaps if g.reason_code != "overlap_excluded"}
        ),
        "tenure": [
            {
                "basis": m.basis,
                "months": str(m.tenure_multiple_months),
                "history_count": m.history_count,
            }
            for m in models[:1]
        ],
        "ledger_sha256": hashlib.sha256(ledger.to_canonical_json()).hexdigest(),
    }
