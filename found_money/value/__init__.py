"""Observed face-value contribution ledger for recovery candidates."""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Iterable
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from pathlib import Path
from typing import Any, Mapping, cast

from found_money.contracts.events import EVENT_FAMILIES, RecoveryCandidateSetV1, RecoveryCandidateV1
from found_money.contracts.value import (
    ContributionLedgerV1,
    ContributionV1,
    ModeledOpportunityV1,
    OverlapLedgerV1,
    OverlapRecordV1,
    PublicContributionProjectionV1,
    PublicContributionRowV1,
    OverlapReason,
    ValueGapReason,
    ValueDataGapV1,
    normalize_currency,
    normalize_minor_units,
)

_TRAVERSAL_RE = re.compile(r"(^|/)\.\.(/|$)")
_WINDOWS_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")
_SCHEMA_MAJOR_RE = re.compile(r"^(?P<name>.+)\.v(?P<major>\d+)$")

_WILSON_80_Z = Decimal("1.2815515655446004")
_PROBABILITY_QUANTUM = Decimal("0.000000000000000001")
_OVERLAP_FAMILY_PAIRS: tuple[tuple[str, str, str], ...] = (
    ("failed_payment", "canceled_customer", "invoice_subscription"),
    ("silent_proposal", "closed_lost_stale_deal", "proposal_deal"),
    ("expired_trial", "trial_no_convert", "trial_subscription"),
    ("expired_trial", "renewal_upsell", "trial_subscription"),
    ("trial_no_convert", "renewal_upsell", "trial_subscription"),
    ("overdue_reorder", "lapsed_repeat_buyer", "reorder_lapse"),
)
_ECOMMERCE_OVERLAP_FAMILIES = frozenset(
    {
        "overdue_reorder",
        "lapsed_repeat_buyer",
        "disappeared_high_value_customer",
    }
)
_EVENT_PRIORITY: dict[str, int] = {
    "failed_payment": 10,
    "closed_lost_stale_deal": 20,
    "silent_proposal": 30,
    "renewal_upsell": 40,
    "disappeared_high_value_customer": 45,
    "overdue_reorder": 50,
    "lapsed_repeat_buyer": 60,
    "canceled_customer": 70,
    "expired_trial": 80,
    "trial_no_convert": 90,
    "no_show_rebook": 110,
    "engaged_unbooked": 120,
}


def _invoice_id_from_unit_key(economic_unit_key: str) -> str:
    prefix = "stripe_invoice:"
    if not economic_unit_key.startswith(prefix):
        raise ValueError(f"unsupported economic_unit_key: {economic_unit_key}")
    invoice_id = economic_unit_key[len(prefix) :].strip()
    if not invoice_id:
        raise ValueError("economic_unit_key is missing invoice id")
    return invoice_id


def _invoice_by_id(stripe_snapshot: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    invoices = stripe_snapshot.get("invoices") or []
    if not isinstance(invoices, list):
        raise ValueError("stripe snapshot invoices must be a list")
    by_id: dict[str, Mapping[str, Any]] = {}
    for invoice in invoices:
        if not isinstance(invoice, Mapping):
            raise ValueError("invoice records must be objects")
        invoice_id = str(invoice.get("id") or "").strip()
        if not invoice_id:
            raise ValueError("invoice id is required")
        by_id[invoice_id] = invoice
    return by_id


def _contribution_for_candidate(
    candidate: RecoveryCandidateV1,
    invoice: Mapping[str, Any],
) -> ContributionV1:
    if "amount_due_cents" not in invoice or invoice.get("amount_due_cents") is None:
        raise ValueError("missing amount_due_cents; refusing default value")
    if "currency" not in invoice or invoice.get("currency") is None:
        raise ValueError("missing currency; refusing default value")
    amount = normalize_minor_units(invoice["amount_due_cents"])
    currency = normalize_currency(str(invoice["currency"]))
    return ContributionV1(
        economic_unit_key=candidate.economic_unit_key,
        pile_id="payment_rescue",
        value_basis="observed_face_value",
        currency=currency,
        amount_minor=amount,
        customer_token=candidate.customer_token,
        candidate_economic_unit_key=candidate.economic_unit_key,
    )


def build_contribution_ledger(
    candidate_set: RecoveryCandidateSetV1,
    stripe_snapshot: Mapping[str, Any],
    *,
    built_at: datetime | None = None,
) -> ContributionLedgerV1:
    """Map accepted failed-payment candidates to observed face-value contributions."""
    when = built_at or datetime(2026, 7, 29, 18, 0, 15, tzinfo=timezone.utc)
    invoices = _invoice_by_id(stripe_snapshot)
    contributions: list[ContributionV1] = []
    seen: set[str] = set()
    for candidate in candidate_set.candidates:
        if candidate.event_family != "failed_payment":
            raise ValueError(f"unsupported event family: {candidate.event_family}")
        if candidate.economic_unit_key in seen:
            raise ValueError(
                f"duplicate economic_unit_key in candidate set: {candidate.economic_unit_key}"
            )
        invoice_id = _invoice_id_from_unit_key(candidate.economic_unit_key)
        invoice = invoices.get(invoice_id)
        if invoice is None:
            raise ValueError(f"no stripe invoice for {candidate.economic_unit_key}")
        contributions.append(_contribution_for_candidate(candidate, invoice))
        seen.add(candidate.economic_unit_key)
    contributions.sort(key=lambda item: item.economic_unit_key)
    return ContributionLedgerV1(
        run_id=candidate_set.run_id,
        built_at=when,
        contributions=contributions,
    )


def totals_by_currency(contributions: list[ContributionV1]) -> dict[str, Decimal]:
    """Sum observed minor units per currency without FX or blended scalars."""
    totals: dict[str, Decimal] = {}
    for item in contributions:
        totals[item.currency] = totals.get(item.currency, Decimal(0)) + item.amount_minor
    return dict(sorted(totals.items()))


def public_contribution_projection(
    ledger: ContributionLedgerV1,
) -> PublicContributionProjectionV1:
    rows = [
        PublicContributionRowV1(
            customer_token=item.customer_token,
            pile_id=item.pile_id,
            currency=item.currency,
            amount_minor=item.amount_minor,
        )
        for item in ledger.contributions
    ]
    rows.sort(key=lambda item: (item.customer_token, item.pile_id, item.currency))
    return PublicContributionProjectionV1(
        run_id=ledger.run_id,
        built_at=ledger.built_at,
        rows=rows,
        total_minor_by_currency=totals_by_currency(list(ledger.contributions)),
    )


def _validate_relative_under_root(output_root: Path, relative_path: str) -> Path:
    if not isinstance(relative_path, str):
        raise ValueError("relative path must be a string")
    raw = relative_path.strip().replace("\\", "/")
    if not raw:
        raise ValueError("relative path must be non-empty")
    if raw.startswith("/") or _WINDOWS_ABS_RE.match(raw) or raw.startswith("~/"):
        raise ValueError("absolute paths are rejected")
    if raw.startswith("./"):
        raw = raw[2:]
    if not raw or _TRAVERSAL_RE.search(raw) or raw == ".." or Path(raw).is_absolute():
        raise ValueError("traversal or absolute paths are rejected")
    root = output_root.expanduser().resolve(strict=False)
    candidate = root.joinpath(*Path(raw).parts)
    try:
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise ValueError("path escapes the caller-owned output root") from exc
    probe = root
    for part in Path(raw).parts[:-1]:
        probe = probe / part
        if probe.is_symlink():
            try:
                probe.resolve(strict=True).relative_to(root)
            except (OSError, ValueError) as exc:
                raise ValueError("symlink escapes the caller-owned output root") from exc
        if probe.exists() and not probe.is_dir():
            raise ValueError("parent path component is not a directory")
    if candidate.exists() and candidate.is_symlink():
        try:
            candidate.resolve(strict=True).relative_to(root)
        except (OSError, ValueError) as exc:
            raise ValueError("symlink escapes the caller-owned output root") from exc
    return candidate


def _atomic_write_bytes(destination: Path, payload: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=str(destination.parent),
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, destination)
    except Exception:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)
        raise


def write_contribution_ledger(
    output_root: Path | str,
    relative_path: str,
    ledger: ContributionLedgerV1,
) -> Path:
    destination = _validate_relative_under_root(Path(output_root), relative_path)
    _atomic_write_bytes(destination, ledger.to_canonical_json())
    return destination


def write_public_contribution_projection(
    output_root: Path | str,
    relative_path: str,
    projection: PublicContributionProjectionV1,
) -> Path:
    destination = _validate_relative_under_root(Path(output_root), relative_path)
    _atomic_write_bytes(destination, projection.to_canonical_json())
    return destination


def _object_pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate JSON object key: {key}")
        out[key] = value
    return out


def parse_canonical_json(
    data: bytes,
) -> ContributionLedgerV1 | PublicContributionProjectionV1:
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("canonical JSON must be bytes")
    try:
        payload = json.loads(data.decode("utf-8"), object_pairs_hook=_object_pairs_hook)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"malformed JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("canonical JSON root must be an object")
    schema_version = payload.get("schema_version")
    if not isinstance(schema_version, str):
        raise ValueError("schema_version is required")
    match = _SCHEMA_MAJOR_RE.fullmatch(schema_version.strip())
    if match is None:
        raise ValueError(f"unrecognized schema_version: {schema_version!r}")
    name, major = match.group("name"), int(match.group("major"))
    if name == "contribution-ledger":
        if major != 1:
            raise ValueError(f"unknown major schema version: {schema_version}")
        model: ContributionLedgerV1 | PublicContributionProjectionV1 = (
            ContributionLedgerV1.model_validate(payload)
        )
    elif name == "contribution-public":
        if major != 1:
            raise ValueError(f"unknown major schema version: {schema_version}")
        model = PublicContributionProjectionV1.model_validate(payload)
    else:
        raise ValueError(f"unknown major schema version: {schema_version}")
    if model.to_canonical_json() != bytes(data):
        raise ValueError("serialized bytes are not exactly canonical")
    return model


# ---------------------------------------------------------------------------
# FM-023 generic value, modeled-opportunity, and overlap layer
# ---------------------------------------------------------------------------


def wilson_interval_80(numerator: int, denominator: int) -> tuple[Decimal, Decimal]:
    """Return the deterministic 80% Wilson interval using Decimal arithmetic."""
    if isinstance(numerator, bool) or not isinstance(numerator, int) or numerator < 0:
        raise ValueError("numerator must be a non-negative integer")
    if isinstance(denominator, bool) or not isinstance(denominator, int) or denominator <= 0:
        raise ValueError("denominator must be a positive integer")
    if numerator > denominator:
        raise ValueError("numerator cannot exceed denominator")
    with localcontext() as context:
        context.prec = 70
        n = Decimal(denominator)
        p = Decimal(numerator) / n
        z = _WILSON_80_Z
        z_squared = z * z
        denominator_term = Decimal(1) + z_squared / n
        center = (p + z_squared / (Decimal(2) * n)) / denominator_term
        spread = (
            z
            * ((p * (Decimal(1) - p) / n) + z_squared / (Decimal(4) * n * n)).sqrt()
            / denominator_term
        )
        lower = max(Decimal(0), center - spread)
        upper = min(Decimal(1), center + spread)
    return (
        lower.quantize(_PROBABILITY_QUANTUM, rounding=ROUND_HALF_UP),
        upper.quantize(_PROBABILITY_QUANTUM, rounding=ROUND_HALF_UP),
    )


def _strict_count(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return value


def _decimal_from_model(value: Any, field_name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError(f"{field_name} must be an integer minor-unit amount")
    try:
        return normalize_minor_units(value)
    except (TypeError, ValueError, InvalidOperation) as exc:
        raise ValueError(f"{field_name} must be an integer minor-unit amount") from exc


def build_modeled_opportunity(
    candidate: RecoveryCandidateV1,
    *,
    average_value_minor: Any,
    currency: Any,
    comparables: int,
    positive_outcomes: int,
    cohort: str,
    window: str,
    basis: str = "historical comparable outcomes",
) -> ModeledOpportunityV1 | None:
    """Build a modeled opportunity only when both V1 history thresholds pass.

    A return value of ``None`` is an intentional insufficient-history result;
    callers must preserve it as a value data gap rather than assigning zero.
    """
    denominator = _strict_count(comparables, "comparables")
    numerator = _strict_count(positive_outcomes, "positive_outcomes")
    if numerator > denominator:
        raise ValueError("positive_outcomes cannot exceed comparables")
    if denominator < 30 or numerator < 5:
        return None
    average = _decimal_from_model(average_value_minor, "average_value_minor")
    code = normalize_currency(currency)
    with localcontext() as context:
        context.prec = 70
        rate = (Decimal(numerator) / Decimal(denominator)).quantize(
            _PROBABILITY_QUANTUM, rounding=ROUND_HALF_UP
        )
        expected = (average * rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    lower, upper = wilson_interval_80(numerator, denominator)
    return ModeledOpportunityV1(
        candidate_key=candidate.candidate_key,
        pile_id=candidate.event_family,
        currency=code,
        cohort=cohort,
        window=window,
        empirical_rate=rate,
        numerator=numerator,
        denominator=denominator,
        basis=basis,
        wilson_interval_low=lower,
        wilson_interval_high=upper,
        average_value_minor=average,
        expected_value_minor=expected,
    )


def _walk_source_records(value: Any) -> Iterable[Mapping[str, Any]]:
    """Yield caller-provided mapping records without reading files or a network."""
    if isinstance(value, list):
        for item in value:
            yield from _walk_source_records(item)
        return
    if not isinstance(value, Mapping):
        return
    keys = {str(key).lower() for key in value}
    identifier_keys = {
        "id",
        "invoice_id",
        "subscription_id",
        "deal_id",
        "proposal_id",
        "order_id",
        "appointment_id",
        "reference",
        "source_reference",
        "economic_unit_key",
        "customer_token",
        "customer_id",
        "contact_id",
        "account_id",
    }
    if keys.intersection(identifier_keys):
        yield value
    for item in value.values():
        if isinstance(item, (Mapping, list)):
            yield from _walk_source_records(item)


def _flatten_record(record: Mapping[str, Any]) -> dict[str, Any]:
    flattened = dict(record)
    for key in ("properties", "fields", "attributes"):
        nested = record.get(key)
        if isinstance(nested, Mapping):
            for nested_key, nested_value in nested.items():
                flattened.setdefault(str(nested_key), nested_value)
    return flattened


def _record_reference(record: Mapping[str, Any]) -> str:
    flat = _flatten_record(record)
    for key in ("source_reference", "reference", "economic_unit_key", "id"):
        value = flat.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "source-record"


def _record_tokens(record: Mapping[str, Any]) -> set[str]:
    flat = _flatten_record(record)
    tokens: set[str] = set()
    for key, value in flat.items():
        key_text = str(key).lower()
        if key_text in {
            "id",
            "invoice_id",
            "subscription_id",
            "deal_id",
            "proposal_id",
            "order_id",
            "appointment_id",
            "reference",
            "source_reference",
            "economic_unit_key",
            "source_id",
        }:
            if isinstance(value, str) and value.strip():
                tokens.add(value.strip())
    return tokens


def _candidate_tokens(candidate: RecoveryCandidateV1) -> set[str]:
    tokens = {
        candidate.economic_unit_key,
        candidate.economic_unit_key.rsplit(":", 1)[-1],
        candidate.candidate_key,
    }
    tokens.update(candidate.lineage.values())
    tokens.update(candidate.evidence_references)
    for value in list(tokens):
        if "/" in value:
            tokens.add(value.rsplit("/", 1)[-1])
    return {item.strip() for item in tokens if isinstance(item, str) and item.strip()}


def _candidate_records(
    candidate: RecoveryCandidateV1,
    records: list[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    candidate_tokens = _candidate_tokens(candidate)
    direct = [record for record in records if candidate_tokens.intersection(_record_tokens(record))]
    if direct:
        return sorted(direct, key=_record_reference)
    # Customer-level families can be represented by normalized source rows
    # whose stable source ID is not copied into the candidate key.
    customer_values = {candidate.customer_token}
    customer_keys = {
        "customer_token",
        "customer_id",
        "customer",
        "contact_id",
        "contact_token",
        "account_id",
    }
    matches: list[Mapping[str, Any]] = []
    for record in records:
        flat = _flatten_record(record)
        for key in customer_keys:
            value = flat.get(key)
            if isinstance(value, str) and value.strip() in customer_values:
                matches.append(record)
                break
    return sorted(matches, key=_record_reference)


_MINOR_AMOUNT_FIELDS = (
    "amount_minor",
    "value_minor",
    "total_minor",
    "amount_due_cents",
    "amount_cents",
    "total_cents",
    "price_minor",
    "unit_amount_minor",
)


def _record_amount(record: Mapping[str, Any]) -> Decimal | None:
    flat = _flatten_record(record)
    found: list[Decimal] = []
    for field_name in _MINOR_AMOUNT_FIELDS:
        raw = flat.get(field_name)
        if raw is None:
            continue
        found.append(_decimal_from_model(raw, field_name))
    # A generic ``amount`` is accepted only when its source explicitly marks
    # it as a minor-unit amount. An ordinary major-unit string cannot be
    # safely inferred here.
    raw_amount = flat.get("amount")
    amount_unit = str(flat.get("amount_unit") or "").strip().lower()
    if raw_amount is not None and amount_unit in {"minor", "minor_unit", "cents"}:
        found.append(_decimal_from_model(raw_amount, "amount"))
    if not found:
        return None
    if len(set(found)) != 1:
        raise ValueError("source records disagree on amount")
    return found[0]


def _record_currency(record: Mapping[str, Any]) -> str | None:
    flat = _flatten_record(record)
    found: list[str] = []
    for field_name in ("currency", "currency_code", "currency_iso"):
        raw = flat.get(field_name)
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            continue
        found.append(normalize_currency(raw))
    if not found:
        return None
    if len(set(found)) != 1:
        raise ValueError("source records disagree on currency")
    return found[0]


def _source_references(records: Iterable[Mapping[str, Any]]) -> list[str]:
    return sorted({_record_reference(record) for record in records})


def _observed_value(
    candidate: RecoveryCandidateV1,
    records: list[Mapping[str, Any]],
) -> tuple[Decimal | None, str | None, str | None, list[str]]:
    matched = _candidate_records(candidate, records)
    references = _source_references(matched)
    if not matched:
        return None, None, "missing_source", []
    values: list[tuple[Decimal, str]] = []
    for record in matched:
        amount = _record_amount(record)
        currency = _record_currency(record)
        if amount is not None and currency is not None:
            values.append((amount, currency))
    if not values:
        has_amount = any(_record_amount(record) is not None for record in matched)
        has_currency = any(_record_currency(record) is not None for record in matched)
        return (
            None,
            next(
                (_record_currency(record) for record in matched if _record_currency(record)), None
            ),
            "missing_currency" if has_amount and not has_currency else "missing_amount",
            references,
        )
    if len(set(values)) != 1:
        return None, values[0][1], "ambiguous_value", references
    amount, currency = values[0]
    return amount, currency, None, references


def _model_input_for_candidate(
    candidate: RecoveryCandidateV1,
    modeled_inputs: Mapping[str, Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    for key in (candidate.candidate_key, candidate.economic_unit_key):
        raw = modeled_inputs.get(key)
        if raw is not None:
            if not isinstance(raw, Mapping):
                raise ValueError(f"modeled input for {key} must be an object")
            return raw
    return None


def _model_candidate(
    candidate: RecoveryCandidateV1,
    model_input: Mapping[str, Any],
) -> tuple[ModeledOpportunityV1 | None, str | None, str | None]:
    def _pick(*names: str) -> Any:
        for name in names:
            if name in model_input:
                return model_input[name]
        return None

    denominator = _pick("comparables", "denominator", "comparable_count")
    numerator = _pick("positive_outcomes", "numerator", "successes", "positive_count")
    average = _pick("average_value_minor", "base_value_minor", "value_minor")
    currency = _pick("currency", "currency_code")
    if denominator is None or numerator is None:
        return None, None, "missing_model_value"
    if average is None:
        return None, None, "missing_model_value"
    if currency is None:
        return None, None, "missing_currency"
    try:
        modeled = build_modeled_opportunity(
            candidate,
            average_value_minor=average,
            currency=currency,
            comparables=denominator,
            positive_outcomes=numerator,
            cohort=str(_pick("cohort") or "same event family and currency"),
            window=str(_pick("window") or "historical comparable window"),
            basis=str(_pick("basis") or "historical comparable outcomes"),
        )
    except (TypeError, ValueError) as exc:
        return None, None, str(exc)
    if modeled is None:
        return None, normalize_currency(currency), "insufficient_history"
    return modeled, modeled.currency, None


def _explicit_overlap_key(candidate: RecoveryCandidateV1) -> str | None:
    for source in (candidate.qualifying_evidence, candidate.lineage):
        for key in ("overlap_group_key", "economic_unit_group", "shared_economic_unit"):
            value = source.get(key)
            if value:
                return f"explicit:{value}"
    return None


def _overlap_group_key(
    candidate: RecoveryCandidateV1, candidates: list[RecoveryCandidateV1]
) -> str:
    explicit = _explicit_overlap_key(candidate)
    if explicit:
        return explicit
    if candidate.event_family in _ECOMMERCE_OVERLAP_FAMILIES and any(
        item.customer_token == candidate.customer_token
        and item.event_family in _ECOMMERCE_OVERLAP_FAMILIES
        for item in candidates
    ):
        return f"customer:{candidate.customer_token}:ecommerce_order"
    same_unit = [
        item for item in candidates if item.economic_unit_key == candidate.economic_unit_key
    ]
    if len(same_unit) > 1:
        return f"economic-unit:{candidate.economic_unit_key}"
    for left, right, name in _OVERLAP_FAMILY_PAIRS:
        if candidate.event_family not in {left, right}:
            continue
        if any(
            item.customer_token == candidate.customer_token and item.event_family in {left, right}
            for item in candidates
        ):
            return f"customer:{candidate.customer_token}:{name}"
    references = set(candidate.lineage.values())
    if references and candidate.event_family in {"silent_proposal", "closed_lost_stale_deal"}:
        if any(
            item is not candidate
            and item.event_family in {"silent_proposal", "closed_lost_stale_deal"}
            and references.intersection(item.lineage.values())
            for item in candidates
        ):
            return f"shared-source:{sorted(references)[0]}"
    return f"candidate:{candidate.candidate_key}"


def _selection_key(
    candidate: RecoveryCandidateV1,
    contribution: ContributionV1,
) -> tuple[int, int, str]:
    basis_rank = 0 if contribution.value_basis == "observed_face_value" else 1
    return basis_rank, _EVENT_PRIORITY.get(candidate.event_family, 999), candidate.candidate_key


def _gap_for_candidate(
    candidate: RecoveryCandidateV1,
    reason_code: str,
    detail: str,
    currency: str | None,
    source_references: list[str],
) -> ValueDataGapV1:
    allowed_reasons = {
        "missing_amount",
        "missing_currency",
        "missing_source",
        "ambiguous_value",
        "insufficient_history",
        "missing_model_value",
        "unsupported_event_family",
        "overlap_excluded",
    }
    if reason_code not in allowed_reasons:
        reason_code = "missing_model_value"
    return ValueDataGapV1(
        run_id=candidate.run_id,
        candidate_key=candidate.candidate_key,
        event_family=candidate.event_family,
        economic_unit_key=candidate.economic_unit_key,
        customer_token=candidate.customer_token,
        reason_code=cast(ValueGapReason, reason_code),
        detail=detail,
        currency=currency,
        source_references=source_references,
    )


def build_value_ledger(
    candidate_set: RecoveryCandidateSetV1,
    source_snapshots: Mapping[str, Any] | None = None,
    *,
    modeled_inputs: Mapping[str, Mapping[str, Any]] | None = None,
    built_at: datetime | None = None,
) -> ContributionLedgerV1:
    """Build all-family selected values from caller-owned normalized snapshots.

    This function is deliberately pure. It accepts already-normalized records,
    makes no provider calls, and writes no upstream state. Missing or ambiguous
    money becomes a value data gap; it is never coerced to zero.
    """
    if source_snapshots is None:
        source_snapshots = {}
    if not isinstance(source_snapshots, Mapping):
        raise ValueError("source_snapshots must be an object")
    if modeled_inputs is None:
        modeled_inputs = {}
    if not isinstance(modeled_inputs, Mapping):
        raise ValueError("modeled_inputs must be an object")
    when = built_at or datetime(2026, 7, 29, 18, 0, 15, tzinfo=timezone.utc)
    records = list(_walk_source_records(source_snapshots))
    candidates = list(candidate_set.candidates)
    drafts: dict[str, ContributionV1] = {}
    gaps: dict[str, ValueDataGapV1] = {}
    for candidate in candidates:
        if candidate.event_family not in EVENT_FAMILIES:
            gaps[candidate.candidate_key] = _gap_for_candidate(
                candidate,
                "unsupported_event_family",
                f"unsupported event family: {candidate.event_family}",
                None,
                [],
            )
            continue
        amount, currency, reason, references = _observed_value(candidate, records)
        if reason is None and amount is not None and currency is not None:
            drafts[candidate.candidate_key] = ContributionV1(
                economic_unit_key=candidate.economic_unit_key,
                pile_id=candidate.event_family,
                value_basis="observed_face_value",
                currency=currency,
                amount_minor=amount,
                customer_token=candidate.customer_token,
                candidate_economic_unit_key=candidate.economic_unit_key,
                candidate_key=candidate.candidate_key,
            )
            continue
        model_input = _model_input_for_candidate(candidate, modeled_inputs)
        modeled: ModeledOpportunityV1 | None = None
        model_currency: str | None = None
        model_reason: str | None = None
        if model_input is not None:
            modeled, model_currency, model_reason = _model_candidate(candidate, model_input)
        if modeled is not None:
            drafts[candidate.candidate_key] = ContributionV1(
                economic_unit_key=candidate.economic_unit_key,
                pile_id=candidate.event_family,
                value_basis="modeled_opportunity",
                currency=modeled.currency,
                amount_minor=modeled.expected_value_minor,
                customer_token=candidate.customer_token,
                candidate_economic_unit_key=candidate.economic_unit_key,
                candidate_key=candidate.candidate_key,
                modeled_opportunity=modeled,
            )
            continue
        gap_reason = model_reason or reason or "missing_amount"
        detail = (
            "modeled history is below 30 comparables and 5 positive outcomes"
            if gap_reason == "insufficient_history"
            else "value is not safely quantifiable from the supplied source evidence"
        )
        gaps[candidate.candidate_key] = _gap_for_candidate(
            candidate,
            gap_reason,
            detail,
            model_currency or currency,
            references,
        )

    grouped: dict[str, list[RecoveryCandidateV1]] = {}
    for candidate in candidates:
        grouped.setdefault(_overlap_group_key(candidate, candidates), []).append(candidate)

    selected: dict[str, ContributionV1] = {}
    overlap_records: list[OverlapRecordV1] = []
    for group_key, group_candidates in sorted(grouped.items()):
        quantified = [
            (candidate, drafts[candidate.candidate_key])
            for candidate in group_candidates
            if candidate.candidate_key in drafts
        ]
        if not quantified:
            continue
        quantified.sort(key=lambda item: _selection_key(*item))
        selected_candidate, selected_contribution = quantified[0]
        selected[selected_candidate.candidate_key] = selected_contribution
        for excluded_candidate in group_candidates:
            if excluded_candidate.candidate_key == selected_candidate.candidate_key:
                continue
            excluded_contribution = drafts.get(excluded_candidate.candidate_key)
            excluded_gap = gaps.get(excluded_candidate.candidate_key)
            if excluded_contribution is None and excluded_gap is None:
                continue
            excluded_basis = (
                excluded_contribution.value_basis if excluded_contribution is not None else None
            )
            if excluded_contribution is not None:
                reason_code = (
                    "duplicate_economic_unit"
                    if excluded_contribution.economic_unit_key
                    == selected_contribution.economic_unit_key
                    else (
                        "observed_preferred"
                        if selected_contribution.value_basis == "observed_face_value"
                        and excluded_contribution.value_basis == "modeled_opportunity"
                        else "deterministic_tiebreak"
                    )
                )
                drafts.pop(excluded_candidate.candidate_key, None)
            else:
                assert excluded_gap is not None
                reason_code = "headline_overlap"
                gaps[excluded_candidate.candidate_key] = excluded_gap.model_copy(
                    update={
                        "reason_code": "overlap_excluded",
                        "detail": "candidate shares a headline economic unit with a selected value",
                    }
                )
            overlap_records.append(
                OverlapRecordV1(
                    overlap_group_key=group_key,
                    selected_candidate_key=selected_candidate.candidate_key,
                    excluded_candidate_key=excluded_candidate.candidate_key,
                    selected_economic_unit_key=selected_contribution.economic_unit_key,
                    excluded_economic_unit_key=(
                        excluded_contribution.economic_unit_key
                        if excluded_contribution is not None
                        else excluded_candidate.economic_unit_key
                    ),
                    reason_code=cast(OverlapReason, reason_code),
                    selected_value_basis=selected_contribution.value_basis,
                    excluded_value_basis=excluded_basis,
                )
            )

    # A candidate with a non-overlap draft is selected; drafts removed by an
    # overlap decision are intentionally absent from the headline ledger.
    for candidate_key, contribution in drafts.items():
        selected.setdefault(candidate_key, contribution)
    contributions = sorted(selected.values(), key=lambda item: item.economic_unit_key)
    data_gaps = sorted(gaps.values(), key=lambda item: item.candidate_key)
    overlap_ledger = OverlapLedgerV1(
        run_id=candidate_set.run_id,
        built_at=when,
        records=overlap_records,
    )
    return ContributionLedgerV1(
        run_id=candidate_set.run_id,
        built_at=when,
        contributions=contributions,
        data_gaps=data_gaps,
        overlap_ledger=overlap_ledger if overlap_records else None,
    )
