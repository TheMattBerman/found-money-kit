"""Recovery candidate contracts (schema_version=recovery-candidate.v1)."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Literal, Self

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

EventFamily = Literal[
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
ConfidenceClass = Literal["observed", "derived"]

EVENT_FAMILIES: tuple[str, ...] = (
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
)

SuppressionReason = Literal[
    "later_payment",
    "reactivation",
    "rebooking",
    "purchase",
    "active_negotiation",
    "active_service_issue",
    "fraud_dispute",
    "explicit_disqualification",
    "recent_owner_activity",
]

# Lower numbers win. This is a contract, not an implementation detail: a source
# dictionary's insertion order can never change the chosen exclusion reason.
SUPPRESSION_PRIORITY: dict[str, int] = {
    "explicit_disqualification": 10,
    "fraud_dispute": 20,
    "later_payment": 30,
    "reactivation": 40,
    "rebooking": 50,
    "purchase": 60,
    "active_negotiation": 70,
    "active_service_issue": 80,
    "recent_owner_activity": 90,
}

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


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


class RecoveryCandidateV1(BaseModel):
    """One deterministic recovery candidate tied to an immutable economic unit."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    event_family: EventFamily
    confidence_class: ConfidenceClass
    economic_unit_key: str
    customer_token: str
    lineage: dict[str, str]
    qualifying_evidence: dict[str, str]
    # These fields are optional for backwards compatibility with FM-003's
    # failed-payment contract. FM-022 detectors always populate them.
    event_at: datetime | None = Field(
        default=None,
        validation_alias=AliasChoices("event_at", "occurred_at"),
    )
    recency_days: int | None = Field(default=None, ge=0)
    evidence_references: list[str] = Field(default_factory=list)

    @field_validator("run_id", "economic_unit_key", "customer_token", mode="before")
    @classmethod
    def _ids(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("value must be a string")
        return _normalize_identifier(value)

    @field_validator("lineage", "qualifying_evidence", mode="before")
    @classmethod
    def _maps(cls, value: Any) -> dict[str, str]:
        if not isinstance(value, dict) or not value:
            raise ValueError("map must be a non-empty object")
        out: dict[str, str] = {}
        for key, raw in value.items():
            if not isinstance(key, str) or not isinstance(raw, str):
                raise ValueError("map entries must be strings")
            out[_normalize_identifier(key)] = _normalize_identifier(raw)
        return out

    @field_validator("event_at", mode="before")
    @classmethod
    def _event_at(cls, value: Any) -> datetime | None:
        if value is None:
            return None
        return _parse_utc(value)

    @field_validator("evidence_references", mode="before")
    @classmethod
    def _evidence_references(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError("evidence_references must be a list")
        values = sorted({_normalize_identifier(item) for item in value})
        return values

    @model_validator(mode="after")
    def _recency_consistency(self) -> Self:
        if self.event_at is None and self.recency_days is not None:
            raise ValueError("recency_days requires event_at")
        return self

    @property
    def candidate_key(self) -> str:
        return f"{self.event_family}:{self.economic_unit_key}"

    def canonical_dict(self) -> dict[str, Any]:
        payload = self.model_dump(mode="python", exclude_none=True)
        if self.event_at is not None:
            payload["event_at"] = _format_utc(self.event_at)
        # Keep FM-003's byte shape stable when the richer FM-022 fields are
        # absent; all new detector output includes them.
        if not self.evidence_references:
            payload.pop("evidence_references", None)
        return payload


class RecoveryCandidateSetV1(BaseModel):
    """Ordered set of recovery candidates for one run."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["recovery-candidate.v1"] = "recovery-candidate.v1"
    run_id: str
    built_at: datetime
    candidates: list[RecoveryCandidateV1] = Field(default_factory=list)

    @field_validator("schema_version", mode="before")
    @classmethod
    def _schema(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("schema_version must be a string")
        text = value.strip()
        if text != "recovery-candidate.v1":
            raise ValueError('schema_version must be exactly "recovery-candidate.v1"')
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
    def _consistency(self) -> Self:
        keys = [item.candidate_key for item in self.candidates]
        if len(keys) != len(set(keys)):
            raise ValueError(
                "event family/economic unit keys must be unique within a candidate set"
            )
        for item in self.candidates:
            if item.run_id != self.run_id:
                raise ValueError("candidate run_id must match set run_id")
        self.candidates = sorted(
            self.candidates,
            key=lambda item: (item.event_family, item.economic_unit_key),
        )
        return self

    def canonical_dict(self) -> dict[str, Any]:
        payload = self.model_dump(mode="python")
        payload["built_at"] = _format_utc(payload["built_at"])
        payload["candidates"] = [item.canonical_dict() for item in self.candidates]
        return payload

    def to_canonical_json(self) -> bytes:
        text = json.dumps(
            self.canonical_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return (text + "\n").encode("utf-8")


class ExclusionRecordV1(BaseModel):
    """One candidate removed from headline output by deterministic evidence."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    run_id: str
    candidate_key: str
    event_family: EventFamily
    economic_unit_key: str
    customer_token: str
    reason_code: SuppressionReason = Field(
        validation_alias=AliasChoices("reason_code", "suppression_reason")
    )
    priority: int = Field(ge=0)
    lineage: dict[str, str]
    disqualifying_evidence: dict[str, str]
    evidence_references: list[str] = Field(default_factory=list)
    competing_reason_codes: list[SuppressionReason] = Field(default_factory=list)

    @field_validator(
        "run_id", "candidate_key", "economic_unit_key", "customer_token", mode="before"
    )
    @classmethod
    def _record_ids(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("exclusion identifiers must be strings")
        return _normalize_identifier(value)

    @field_validator("lineage", "disqualifying_evidence", mode="before")
    @classmethod
    def _record_maps(cls, value: Any) -> dict[str, str]:
        if not isinstance(value, dict) or not value:
            raise ValueError("exclusion maps must be non-empty objects")
        output: dict[str, str] = {}
        for key, raw in value.items():
            if not isinstance(key, str) or not isinstance(raw, str):
                raise ValueError("exclusion map entries must be strings")
            output[_normalize_identifier(key)] = _normalize_identifier(raw)
        return output

    @field_validator("evidence_references", "competing_reason_codes", mode="before")
    @classmethod
    def _record_lists(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError("exclusion lists must be arrays")
        return sorted({_normalize_identifier(item) for item in value})

    @model_validator(mode="after")
    def _record_consistency(self) -> Self:
        expected_key = f"{self.event_family}:{self.economic_unit_key}"
        if self.candidate_key != expected_key:
            raise ValueError("candidate_key must combine event_family and economic_unit_key")
        expected_priority = SUPPRESSION_PRIORITY[self.reason_code]
        if self.priority != expected_priority:
            raise ValueError("exclusion priority does not match the suppression contract")
        if self.reason_code in self.competing_reason_codes:
            raise ValueError("competing_reason_codes must exclude the selected reason")
        return self

    def canonical_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="python")


class ExclusionLedgerV1(BaseModel):
    """Deterministic suppression ledger for one event run."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["event-exclusion-ledger.v1"] = "event-exclusion-ledger.v1"
    run_id: str
    built_at: datetime
    exclusions: list[ExclusionRecordV1] = Field(default_factory=list)

    @field_validator("schema_version", mode="before")
    @classmethod
    def _schema(cls, value: Any) -> str:
        if not isinstance(value, str) or value.strip() != "event-exclusion-ledger.v1":
            raise ValueError('schema_version must be exactly "event-exclusion-ledger.v1"')
        return "event-exclusion-ledger.v1"

    @field_validator("run_id", mode="before")
    @classmethod
    def _ledger_run(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("run_id must be a string")
        return _normalize_identifier(value)

    @field_validator("built_at", mode="before")
    @classmethod
    def _ledger_built(cls, value: Any) -> datetime:
        return _parse_utc(value)

    @model_validator(mode="after")
    def _ledger_consistency(self) -> Self:
        keys = [item.candidate_key for item in self.exclusions]
        if len(keys) != len(set(keys)):
            raise ValueError("candidate_key values must be unique in the exclusion ledger")
        if any(item.run_id != self.run_id for item in self.exclusions):
            raise ValueError("exclusion run_id must match ledger run_id")
        self.exclusions = sorted(self.exclusions, key=lambda item: item.candidate_key)
        return self

    def canonical_dict(self) -> dict[str, Any]:
        payload = self.model_dump(mode="python")
        payload["built_at"] = _format_utc(self.built_at)
        payload["exclusions"] = [item.canonical_dict() for item in self.exclusions]
        return payload

    def to_canonical_json(self) -> bytes:
        return (
            json.dumps(
                self.canonical_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            + "\n"
        ).encode("utf-8")


class EventDataGapV1(BaseModel):
    """Non-candidate diagnostics kept when identity or source evidence is unusable."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    reason_code: Literal["ambiguous_identity", "missing_identity", "missing_evidence"]
    source_reference: str
    detail: str

    @field_validator("run_id", "source_reference", "detail", mode="before")
    @classmethod
    def _gap_text(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("data-gap values must be strings")
        return _normalize_identifier(value)


class EventDataGapLedgerV1(BaseModel):
    """Canonical deterministic data-gap surface for one event run."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["event-data-gaps.v1"] = "event-data-gaps.v1"
    run_id: str
    built_at: datetime
    gaps: list[EventDataGapV1] = Field(default_factory=list)

    @field_validator("run_id", mode="before")
    @classmethod
    def _gap_run(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("run_id must be a string")
        return _normalize_identifier(value)

    @field_validator("built_at", mode="before")
    @classmethod
    def _gap_built(cls, value: Any) -> datetime:
        return _parse_utc(value)

    @model_validator(mode="after")
    def _gap_consistency(self) -> Self:
        if any(item.run_id != self.run_id for item in self.gaps):
            raise ValueError("data-gap run_id must match ledger run_id")
        self.gaps = sorted(
            self.gaps,
            key=lambda item: (item.reason_code, item.source_reference, item.detail),
        )
        return self

    def canonical_dict(self) -> dict[str, Any]:
        payload = self.model_dump(mode="python")
        payload["built_at"] = _format_utc(self.built_at)
        return payload

    def to_canonical_json(self) -> bytes:
        return (
            json.dumps(
                self.canonical_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            + "\n"
        ).encode("utf-8")


PUBLIC_NAMED_DATA_GAPS = frozenset(
    {
        "missing_appointments",
        "missing_proposals",
        "missing_crm",
        "missing_payment",
    }
)


def public_named_data_gaps(details: list[str] | tuple[str, ...]) -> list[str]:
    """Stable public-safe named gap codes; unknown details are dropped."""
    names = sorted({item for item in details if item in PUBLIC_NAMED_DATA_GAPS})
    return names


class PublicEventProjectionV1(BaseModel):
    """Aggregate-only event view; it contains no customer or source identifiers."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["event-public.v1"] = "event-public.v1"
    run_id: str
    built_at: datetime
    candidate_count: int = Field(ge=0)
    exclusion_count: int = Field(ge=0)
    data_gap_count: int = Field(ge=0)
    candidates_by_family: dict[str, int] = Field(default_factory=dict)
    exclusions_by_reason: dict[str, int] = Field(default_factory=dict)
    named_data_gaps: list[str] = Field(default_factory=list)

    @field_validator("run_id", mode="before")
    @classmethod
    def _public_run(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("run_id must be a string")
        return _normalize_identifier(value)

    @field_validator("built_at", mode="before")
    @classmethod
    def _public_built(cls, value: Any) -> datetime:
        return _parse_utc(value)

    @field_validator("candidates_by_family", "exclusions_by_reason", mode="before")
    @classmethod
    def _public_counts(cls, value: Any) -> dict[str, int]:
        if not isinstance(value, dict):
            raise ValueError("public event counts must be objects")
        result: dict[str, int] = {}
        for key, count in value.items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError("public event count keys must be strings")
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ValueError("public event counts must be non-negative integers")
            result[key.strip()] = count
        return dict(sorted(result.items()))

    @field_validator("named_data_gaps", mode="before")
    @classmethod
    def _named_gaps(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError("named_data_gaps must be a list")
        items = [_normalize_identifier(item) if isinstance(item, str) else item for item in value]
        if any(not isinstance(item, str) for item in items):
            raise ValueError("named_data_gaps entries must be strings")
        if any(item not in PUBLIC_NAMED_DATA_GAPS for item in items):
            raise ValueError("named_data_gaps must use public-safe source-gap codes")
        if len(items) != len(set(items)):
            raise ValueError("named_data_gaps must be unique")
        return sorted(items)

    def canonical_dict(self) -> dict[str, Any]:
        payload = self.model_dump(mode="python")
        payload["built_at"] = _format_utc(self.built_at)
        if not payload.get("named_data_gaps"):
            payload.pop("named_data_gaps", None)
        return payload

    def to_canonical_json(self) -> bytes:
        return (
            json.dumps(
                self.canonical_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            + "\n"
        ).encode("utf-8")
