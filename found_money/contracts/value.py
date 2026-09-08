"""Contribution ledger contracts (schema_version=contribution-ledger.v1)."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

PileId = Literal[
    "payment_rescue",
    "failed_payment",
    "expired_trial",
    "trial_no_convert",
    "closed_lost_stale_deal",
    "canceled_customer",
    "lapsed_repeat_buyer",
    "silent_proposal",
    "no_show_rebook",
    "disappeared_high_value_customer",
    "overdue_reorder",
    "renewal_upsell",
    "engaged_unbooked",
]
ValueBasis = Literal["observed_face_value", "modeled_opportunity"]
ValueGapReason = Literal[
    "missing_amount",
    "missing_currency",
    "missing_source",
    "ambiguous_value",
    "insufficient_history",
    "missing_model_value",
    "unsupported_event_family",
    "overlap_excluded",
]
OverlapReason = Literal[
    "duplicate_economic_unit",
    "headline_overlap",
    "observed_preferred",
    "deterministic_tiebreak",
]

_CURRENCY_RE = re.compile(r"^[a-z]{3}$")

# Deterministic ISO-4217 exponent metadata for every supported currency.
# Keys are lowercase ISO codes; values are minor-unit decimal exponents.
CURRENCY_EXPONENTS: dict[str, int] = {
    "aud": 2,
    "cad": 2,
    "eur": 2,
    "gbp": 2,
    "jpy": 0,
    "kwd": 3,
    "usd": 2,
}
SUPPORTED_CURRENCIES: frozenset[str] = frozenset(CURRENCY_EXPONENTS)


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware UTC")
    as_utc = value.astimezone(timezone.utc)
    return as_utc.replace(microsecond=(as_utc.microsecond // 1000) * 1000)


def _parse_utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        return _require_utc(value)
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return _require_utc(datetime.fromisoformat(text))
    raise ValueError("timestamp must be an ISO-8601 UTC value")


def _normalize_identifier(value: str) -> str:
    text = value.strip()
    if not text:
        raise ValueError("identifier must be non-empty after normalization")
    return text


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def normalize_currency(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("currency must be a string")
    text = value.strip().lower()
    if not _CURRENCY_RE.fullmatch(text):
        raise ValueError("currency must be a 3-letter ISO code")
    if text not in SUPPORTED_CURRENCIES:
        raise ValueError(f"unsupported currency code: {text}")
    return text


def currency_exponent(currency: Any) -> int:
    """Return the minor-unit exponent for a supported currency code."""
    return CURRENCY_EXPONENTS[normalize_currency(currency)]


def normalize_minor_units(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("minor units must be an integer Decimal amount")
    if isinstance(value, float):
        raise ValueError("minor units must be an integer Decimal amount")
    if isinstance(value, Decimal):
        amount = value
    elif isinstance(value, int):
        amount = Decimal(value)
    elif isinstance(value, str):
        try:
            amount = Decimal(value.strip())
        except InvalidOperation as exc:
            raise ValueError("minor units must be an integer Decimal amount") from exc
    else:
        raise ValueError("minor units must be an integer Decimal amount")
    if amount != amount.to_integral_value() or amount < 0:
        raise ValueError("minor units must be a non-negative integer Decimal")
    return amount


def normalize_rate(value: Any) -> Decimal:
    """Normalize a probability without passing through binary floating point."""
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError("rate must be a Decimal or decimal string between 0 and 1")
    if isinstance(value, Decimal):
        rate = value
    elif isinstance(value, (int, str)):
        try:
            rate = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError("rate must be a Decimal or decimal string between 0 and 1") from exc
    else:
        raise ValueError("rate must be a Decimal or decimal string between 0 and 1")
    if not rate.is_finite() or rate < 0 or rate > 1:
        raise ValueError("rate must be between 0 and 1")
    return rate


def _normalize_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    text = value.strip()
    if not text:
        raise ValueError(f"{field_name} must be non-empty")
    return text


def format_major_units(amount_minor: Any, currency: Any) -> str:
    """Format integer minor units as an exact major-unit Decimal string.

    Uses Decimal arithmetic only. Does not convert through binary floats and
    does not alter minor-unit serialization contracts.
    """
    code = normalize_currency(currency)
    amount = normalize_minor_units(amount_minor)
    exponent = CURRENCY_EXPONENTS[code]
    scale = Decimal(10) ** exponent
    major = (amount / scale).quantize(Decimal(10) ** -exponent)
    return format(major, "f")


class RecurringValuationV1(BaseModel):
    """Recomputable customer lifetime sizing, not a recovery-probability estimate."""

    model_config = ConfigDict(extra="forbid")
    currency: str
    monthly_amount_minor: Decimal
    tenure_multiple_months: Decimal
    expected_value_minor: Decimal
    basis: Literal["source_tenure", "owner_tenure", "owner_ltv_override"]
    history_count: int = Field(ge=0)
    source_digest: str
    ltv_override_minor: Decimal | None = None

    @field_validator("currency", mode="before")
    @classmethod
    def _currency(cls, value: Any) -> str:
        return normalize_currency(value)

    @field_validator(
        "monthly_amount_minor", "expected_value_minor", "ltv_override_minor", mode="before"
    )
    @classmethod
    def _minor(cls, value: Any) -> Decimal | None:
        return None if value is None else normalize_minor_units(value)

    @field_validator("tenure_multiple_months", mode="before")
    @classmethod
    def _tenure(cls, value: Any) -> Decimal:
        result = Decimal(str(value))
        if not result.is_finite() or result <= 0:
            raise ValueError("tenure must be finite and positive")
        return result

    @model_validator(mode="after")
    def _calculation(self) -> Self:
        if not re.fullmatch(r"[0-9a-f]{64}", self.source_digest):
            raise ValueError("recurring valuation requires a source digest")
        if self.monthly_amount_minor <= 0:
            raise ValueError("monthly source amount must be positive")
        expected = (self.monthly_amount_minor * self.tenure_multiple_months).quantize(
            Decimal("1"), rounding="ROUND_HALF_UP"
        )
        if self.basis == "owner_ltv_override":
            if self.ltv_override_minor is None or self.ltv_override_minor <= 0:
                raise ValueError("owner LTV override must be positive")
            expected = self.ltv_override_minor
        elif self.ltv_override_minor is not None:
            raise ValueError("an override requires the owner override basis")
        if self.basis == "source_tenure" and self.history_count < 1:
            raise ValueError("derived tenure requires source history")
        if self.expected_value_minor != expected:
            raise ValueError("recurring value does not equal its declared inputs")
        return self


class ContributionV1(BaseModel):
    """One selected quantified contribution for a single economic unit."""

    model_config = ConfigDict(extra="forbid")

    economic_unit_key: str
    pile_id: PileId
    value_basis: ValueBasis
    currency: str
    amount_minor: Decimal
    customer_token: str
    candidate_economic_unit_key: str
    # FM-023 adds the family-qualified key without changing FM-003's legacy
    # serialized shape when this field is absent.
    candidate_key: str | None = None
    modeled_opportunity: ModeledOpportunityV1 | None = None
    recurring_valuation: RecurringValuationV1 | None = None

    @field_validator(
        "economic_unit_key",
        "customer_token",
        "candidate_economic_unit_key",
        mode="before",
    )
    @classmethod
    def _ids(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("value must be a string")
        return _normalize_identifier(value)

    @field_validator("currency", mode="before")
    @classmethod
    def _currency(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("currency must be a string")
        return normalize_currency(value)

    @field_validator("amount_minor", mode="before")
    @classmethod
    def _amount(cls, value: Any) -> Decimal:
        return normalize_minor_units(value)

    @field_validator("candidate_key", mode="before")
    @classmethod
    def _candidate_key(cls, value: Any) -> str | None:
        if value is None:
            return None
        return _normalize_identifier(value)

    @model_validator(mode="after")
    def _refs(self) -> Self:
        if self.candidate_economic_unit_key != self.economic_unit_key:
            raise ValueError("candidate_economic_unit_key must equal economic_unit_key")
        if self.candidate_key is not None and not self.candidate_key.endswith(
            f":{self.economic_unit_key}"
        ):
            raise ValueError("candidate_key must end with economic_unit_key")
        models = [m for m in (self.modeled_opportunity, self.recurring_valuation) if m is not None]
        if self.value_basis == "observed_face_value" and models:
            raise ValueError("observed contributions cannot carry modeled opportunity metadata")
        if self.value_basis == "modeled_opportunity":
            if len(models) != 1:
                raise ValueError(
                    "modeled contributions require exactly one modeled opportunity metadata record"
                )
            if models[0].currency != self.currency:
                raise ValueError("modeled opportunity currency must match contribution currency")
            if models[0].expected_value_minor != self.amount_minor:
                raise ValueError("modeled opportunity value must match contribution amount")
        return self


class ModeledOpportunityV1(BaseModel):
    """A threshold-qualified, non-revenue probability model for one candidate."""

    model_config = ConfigDict(extra="forbid")

    candidate_key: str
    pile_id: PileId
    currency: str
    cohort: str
    window: str
    empirical_rate: Decimal
    numerator: int
    denominator: int
    basis: str
    wilson_interval_low: Decimal
    wilson_interval_high: Decimal
    average_value_minor: Decimal
    expected_value_minor: Decimal

    @field_validator("candidate_key", "cohort", "window", "basis", mode="before")
    @classmethod
    def _text(cls, value: Any, info: Any) -> str:
        return _normalize_text(value, info.field_name)

    @field_validator("currency", mode="before")
    @classmethod
    def _currency(cls, value: Any) -> str:
        return normalize_currency(value)

    @field_validator("empirical_rate", "wilson_interval_low", "wilson_interval_high", mode="before")
    @classmethod
    def _rate(cls, value: Any) -> Decimal:
        return normalize_rate(value)

    @field_validator("average_value_minor", "expected_value_minor", mode="before")
    @classmethod
    def _amount(cls, value: Any) -> Decimal:
        return normalize_minor_units(value)

    @field_validator("numerator", "denominator", mode="before")
    @classmethod
    def _count(cls, value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("modeled history counts must be non-negative integers")
        return value

    @model_validator(mode="after")
    def _threshold_and_interval(self) -> Self:
        if self.denominator < 30 or self.numerator < 5:
            raise ValueError("modeled opportunity requires at least 30 comparables and 5 positives")
        if self.numerator > self.denominator:
            raise ValueError("numerator cannot exceed denominator")
        if self.wilson_interval_low > self.empirical_rate:
            raise ValueError("Wilson lower bound cannot exceed empirical rate")
        if self.wilson_interval_high < self.empirical_rate:
            raise ValueError("Wilson upper bound cannot be below empirical rate")
        return self


class ContributionLedgerV1(BaseModel):
    """Uniqueness-enforced contribution ledger for one run."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["contribution-ledger.v1"] = "contribution-ledger.v1"
    run_id: str
    built_at: datetime
    contributions: list[ContributionV1] = Field(default_factory=list)
    data_gaps: list[ValueDataGapV1] = Field(default_factory=list)
    overlap_ledger: OverlapLedgerV1 | None = None

    @field_validator("schema_version", mode="before")
    @classmethod
    def _schema(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("schema_version must be a string")
        text = value.strip()
        if text != "contribution-ledger.v1":
            raise ValueError('schema_version must be exactly "contribution-ledger.v1"')
        return text

    @field_validator("run_id", mode="before")
    @classmethod
    def _run(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("run_id must be a string")
        return _normalize_identifier(value)

    @field_validator("built_at", mode="before")
    @classmethod
    def _built(cls, value: Any) -> datetime:
        return _parse_utc(value)

    @model_validator(mode="after")
    def _unique_keys(self) -> Self:
        keys = [item.economic_unit_key for item in self.contributions]
        if len(keys) != len(set(keys)):
            raise ValueError("economic_unit_key values must be unique in the ledger")
        candidate_keys = [item.candidate_key for item in self.contributions if item.candidate_key]
        if len(candidate_keys) != len(set(candidate_keys)):
            raise ValueError("candidate_key values must be unique in the ledger")
        if any(item.run_id != self.run_id for item in self.data_gaps):
            raise ValueError("value data-gap run_id must match ledger run_id")
        if self.overlap_ledger is not None and self.overlap_ledger.run_id != self.run_id:
            raise ValueError("overlap ledger run_id must match contribution ledger run_id")
        return self

    def canonical_dict(self) -> dict[str, Any]:
        payload = self.model_dump(mode="python", exclude_none=True)
        payload["built_at"] = _format_utc(payload["built_at"])
        if not payload.get("data_gaps"):
            payload.pop("data_gaps", None)
        if not payload.get("overlap_ledger", {}).get("records"):
            payload.pop("overlap_ledger", None)
        _stringify_decimals(payload)
        return payload

    def to_canonical_json(self) -> bytes:
        text = json.dumps(
            self.canonical_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return (text + "\n").encode("utf-8")


class PublicContributionRowV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_token: str
    pile_id: PileId
    currency: str
    amount_minor: Decimal

    @field_validator("customer_token", mode="before")
    @classmethod
    def _token(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("customer_token must be a string")
        return _normalize_identifier(value)

    @field_validator("currency", mode="before")
    @classmethod
    def _currency(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("currency must be a string")
        return normalize_currency(value)

    @field_validator("amount_minor", mode="before")
    @classmethod
    def _amount(cls, value: Any) -> Decimal:
        return normalize_minor_units(value)


class PublicContributionProjectionV1(BaseModel):
    """Public-safe aggregate view of contribution ledger rows."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["contribution-public.v1"] = "contribution-public.v1"
    run_id: str
    built_at: datetime
    rows: list[PublicContributionRowV1] = Field(default_factory=list)
    total_minor_by_currency: dict[str, Decimal] = Field(default_factory=dict)

    @field_validator("schema_version", mode="before")
    @classmethod
    def _schema(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("schema_version must be a string")
        text = value.strip()
        if text != "contribution-public.v1":
            raise ValueError('schema_version must be exactly "contribution-public.v1"')
        return text

    @field_validator("run_id", mode="before")
    @classmethod
    def _run(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("run_id must be a string")
        return _normalize_identifier(value)

    @field_validator("built_at", mode="before")
    @classmethod
    def _built(cls, value: Any) -> datetime:
        return _parse_utc(value)

    @field_validator("total_minor_by_currency", mode="before")
    @classmethod
    def _totals(cls, value: Any) -> dict[str, Decimal]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("total_minor_by_currency must be an object")
        out: dict[str, Decimal] = {}
        for key, raw in value.items():
            if not isinstance(key, str):
                raise ValueError("currency keys must be strings")
            out[normalize_currency(key)] = normalize_minor_units(raw)
        return out

    def canonical_dict(self) -> dict[str, Any]:
        payload = self.model_dump(mode="python")
        payload["built_at"] = _format_utc(payload["built_at"])
        for item in payload["rows"]:
            item["amount_minor"] = str(item["amount_minor"])
        payload["total_minor_by_currency"] = {
            key: str(value) for key, value in payload["total_minor_by_currency"].items()
        }
        return payload

    def to_canonical_json(self) -> bytes:
        text = json.dumps(
            self.canonical_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return (text + "\n").encode("utf-8")


class ValueDataGapV1(BaseModel):
    """Explicit value-stage uncertainty; it is never silently assigned zero."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    candidate_key: str
    event_family: str
    economic_unit_key: str
    customer_token: str
    reason_code: ValueGapReason
    detail: str
    currency: str | None = None
    source_references: list[str] = Field(default_factory=list)

    @field_validator(
        "run_id",
        "candidate_key",
        "event_family",
        "economic_unit_key",
        "customer_token",
        "detail",
        mode="before",
    )
    @classmethod
    def _gap_text(cls, value: Any) -> str:
        return _normalize_text(value, "value data-gap field")

    @field_validator("currency", mode="before")
    @classmethod
    def _gap_currency(cls, value: Any) -> str | None:
        if value is None:
            return None
        return normalize_currency(value)

    @field_validator("source_references", mode="before")
    @classmethod
    def _gap_refs(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError("source_references must be a list")
        return sorted({_normalize_identifier(item) for item in value})


class OverlapRecordV1(BaseModel):
    """One candidate excluded so a shared economic unit is counted once."""

    model_config = ConfigDict(extra="forbid")

    overlap_group_key: str
    selected_candidate_key: str
    excluded_candidate_key: str
    selected_economic_unit_key: str
    excluded_economic_unit_key: str
    reason_code: OverlapReason
    selected_value_basis: ValueBasis | None = None
    excluded_value_basis: ValueBasis | None = None

    @field_validator(
        "overlap_group_key",
        "selected_candidate_key",
        "excluded_candidate_key",
        "selected_economic_unit_key",
        "excluded_economic_unit_key",
        mode="before",
    )
    @classmethod
    def _overlap_text(cls, value: Any) -> str:
        return _normalize_text(value, "overlap field")

    @model_validator(mode="after")
    def _different(self) -> Self:
        if self.selected_candidate_key == self.excluded_candidate_key:
            raise ValueError("selected and excluded candidates must differ")
        return self


class OverlapLedgerV1(BaseModel):
    """Canonical deterministic record of headline-overlap decisions."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["value-overlap-ledger.v1"] = "value-overlap-ledger.v1"
    run_id: str
    built_at: datetime
    records: list[OverlapRecordV1] = Field(default_factory=list)

    @field_validator("run_id", mode="before")
    @classmethod
    def _run(cls, value: Any) -> str:
        return _normalize_text(value, "run_id")

    @field_validator("built_at", mode="before")
    @classmethod
    def _built(cls, value: Any) -> datetime:
        return _parse_utc(value)

    @model_validator(mode="after")
    def _consistency(self) -> Self:
        if len({record.excluded_candidate_key for record in self.records}) != len(self.records):
            raise ValueError("each excluded candidate may appear once in the overlap ledger")
        self.records = sorted(
            self.records,
            key=lambda item: (
                item.overlap_group_key,
                item.selected_candidate_key,
                item.excluded_candidate_key,
            ),
        )
        return self

    def canonical_dict(self) -> dict[str, Any]:
        payload = self.model_dump(mode="python")
        payload["built_at"] = _format_utc(payload["built_at"])
        return payload

    def to_canonical_json(self) -> bytes:
        text = json.dumps(
            self.canonical_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return (text + "\n").encode("utf-8")


def _stringify_decimals(value: Any) -> Any:
    """Convert Decimal values recursively for canonical JSON payloads."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return _format_utc(value)
    if isinstance(value, dict):
        for key, item in list(value.items()):
            value[key] = _stringify_decimals(item)
        return value
    if isinstance(value, list):
        return [_stringify_decimals(item) for item in value]
    return value
