"""Strategy evidence and recovery-play contracts."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from found_money.contracts.value import normalize_currency, normalize_minor_units

PileId = str
_ALLOWED_STRATEGY_PILE_IDS = {
    "payment_rescue",
    "lapsed_repeat_buyer",
    "overdue_reorder",
    "disappeared_high_value_customer",
    "canceled_customer",
}
PlayId = str
ValueBasis = Literal["observed_face_value"]
ConfidenceClass = Literal["observed"]
StrategyProviderName = Literal["fixture", "stub", "skill"]


_FORBIDDEN_IDENTITY_KEYS = frozenset(
    {
        "customer_token",
        "economic_unit_key",
        "email",
        "phone",
        "external_ids",
        "invoice_id",
        "source_id",
        "lineage",
        "qualifying_evidence",
    }
)

StrategyClaimKind = Literal[
    "number",
    "price",
    "proof",
    "objection",
    "urgency",
    "url",
    "destination",
    "capacity",
    "product",
    "identity",
    "general",
]
DecisionFlagName = Literal[
    "confirm_product",
    "confirm_proof",
    "confirm_margin",
    "confirm_channel",
    "confirm_capacity",
    "confirm_destination",
]


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


def _reject_forbidden_keys(payload: dict[str, Any]) -> None:
    for key in payload:
        if key in _FORBIDDEN_IDENTITY_KEYS:
            raise ValueError(f"forbidden identity field in strategy payload: {key}")


class StrategyEvidencePileFactV1(BaseModel):
    """Aggregate, PII-free pile facts for strategy providers."""

    model_config = ConfigDict(extra="forbid")

    pile_id: PileId
    currency: str
    selected_value_minor: Decimal
    customer_count: int
    economic_unit_count: int
    value_basis: ValueBasis
    confidence_class: ConfidenceClass

    @field_validator("pile_id", mode="before")
    @classmethod
    def _pile(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("pile_id must be a string")
        text = value.strip()
        if text not in _ALLOWED_STRATEGY_PILE_IDS:
            raise ValueError("pile_id is not a supported strategy pile")
        return text

    @field_validator("customer_count", "economic_unit_count", mode="before")
    @classmethod
    def _positive_int(cls, value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("value must be a positive integer")
        if value < 1:
            raise ValueError("value must be a positive integer")
        return value

    @field_validator("currency", mode="before")
    @classmethod
    def _currency(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("currency must be a string")
        return normalize_currency(value)

    @field_validator("selected_value_minor", mode="before")
    @classmethod
    def _amount(cls, value: Any) -> Decimal:
        return normalize_minor_units(value)

    @field_validator("value_basis", mode="before")
    @classmethod
    def _basis(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("value_basis must be a string")
        text = value.strip()
        if text != "observed_face_value":
            raise ValueError('value_basis must be exactly "observed_face_value"')
        return text

    @field_validator("confidence_class", mode="before")
    @classmethod
    def _confidence(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("confidence_class must be a string")
        text = value.strip()
        if text != "observed":
            raise ValueError('confidence_class must be exactly "observed"')
        return text


class StrategyEvidencePacketV1(BaseModel):
    """Frozen PII-free evidence packet for strategy providers."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["strategy-evidence-packet.v1"] = "strategy-evidence-packet.v1"
    run_id: str
    built_at: datetime
    pile_facts: list[StrategyEvidencePileFactV1] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _no_forbidden(cls, value: Any) -> Any:
        if isinstance(value, dict):
            _reject_forbidden_keys(value)
            for fact in value.get("pile_facts") or []:
                if isinstance(fact, dict):
                    _reject_forbidden_keys(fact)
        return value

    @field_validator("schema_version", mode="before")
    @classmethod
    def _schema(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("schema_version must be a string")
        text = value.strip()
        if text != "strategy-evidence-packet.v1":
            raise ValueError('schema_version must be exactly "strategy-evidence-packet.v1"')
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

    def canonical_dict(self) -> dict[str, Any]:
        payload = self.model_dump(mode="python")
        payload["built_at"] = _format_utc(payload["built_at"])
        for fact in payload["pile_facts"]:
            fact["selected_value_minor"] = str(fact["selected_value_minor"])
        return payload

    def to_canonical_json(self) -> bytes:
        text = json.dumps(
            self.canonical_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return (text + "\n").encode("utf-8")


class RecoveryPlayV1(BaseModel):
    """One deterministic recovery play grounded on a Money Map pile."""

    model_config = ConfigDict(extra="forbid")

    play_id: PlayId
    pile_id: PileId
    rank: int
    title: str
    rationale: str
    recommended_actions: list[str]

    @field_validator("play_id", mode="before")
    @classmethod
    def _play(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("play_id must be a string")
        text = value.strip()
        if not text or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", text):
            raise ValueError("play_id must be a normalized slug")
        return text

    @field_validator("pile_id", mode="before")
    @classmethod
    def _pile(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("pile_id must be a string")
        text = value.strip()
        if text not in _ALLOWED_STRATEGY_PILE_IDS:
            raise ValueError("pile_id is not a supported strategy pile")
        return text

    @field_validator("rank", mode="before")
    @classmethod
    def _rank(cls, value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("rank must be a positive integer")
        if value < 1:
            raise ValueError("rank must be a positive integer")
        return value

    @field_validator("title", "rationale", mode="before")
    @classmethod
    def _nonempty(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("value must be a string")
        text = value.strip()
        if not text:
            raise ValueError("value must be non-empty")
        return text

    @field_validator("recommended_actions", mode="before")
    @classmethod
    def _actions(cls, value: Any) -> list[str]:
        if not isinstance(value, list) or not value:
            raise ValueError("recommended_actions must be a non-empty list")
        out: list[str] = []
        for item in value:
            if not isinstance(item, str):
                raise ValueError("recommended_actions entries must be strings")
            text = item.strip()
            if not text:
                raise ValueError("recommended_actions entries must be non-empty")
            out.append(text)
        return out


class RecoveryPlaySetV1(BaseModel):
    """Ordered recovery plays emitted by a strategy provider."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["recovery-plays.v1"] = "recovery-plays.v1"
    run_id: str
    built_at: datetime
    provider: StrategyProviderName
    plays: list[RecoveryPlayV1] = Field(default_factory=list)

    @field_validator("schema_version", mode="before")
    @classmethod
    def _schema(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("schema_version must be a string")
        text = value.strip()
        if text != "recovery-plays.v1":
            raise ValueError('schema_version must be exactly "recovery-plays.v1"')
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

    @field_validator("provider", mode="before")
    @classmethod
    def _provider(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("provider must be a string")
        text = value.strip()
        if text not in {"fixture", "stub", "skill"}:
            raise ValueError('provider must be one of "fixture", "stub", "skill"')
        return text

    @model_validator(mode="after")
    def _ranks(self) -> Self:
        ranks = [play.rank for play in self.plays]
        if len(ranks) != len(set(ranks)):
            raise ValueError("play ranks must be unique")
        play_ids = [play.play_id for play in self.plays]
        if len(play_ids) != len(set(play_ids)):
            raise ValueError("play ids must be unique")
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


class PublicRecoveryPlayRowV1(BaseModel):
    """Public-safe recovery play row."""

    model_config = ConfigDict(extra="forbid")

    play_id: PlayId
    pile_id: PileId
    rank: int
    title: str
    action_labels: list[str]

    @field_validator("play_id", mode="before")
    @classmethod
    def _play(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("play_id must be a string")
        text = value.strip()
        if not text or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", text):
            raise ValueError("play_id must be a normalized slug")
        return text

    @field_validator("pile_id", mode="before")
    @classmethod
    def _pile(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("pile_id must be a string")
        text = value.strip()
        if text not in _ALLOWED_STRATEGY_PILE_IDS:
            raise ValueError("pile_id is not a supported strategy pile")
        return text

    @field_validator("rank", mode="before")
    @classmethod
    def _rank(cls, value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("rank must be a positive integer")
        if value < 1:
            raise ValueError("rank must be a positive integer")
        return value

    @field_validator("title", mode="before")
    @classmethod
    def _title(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("title must be a string")
        text = value.strip()
        if not text:
            raise ValueError("title must be non-empty")
        return text

    @field_validator("action_labels", mode="before")
    @classmethod
    def _labels(cls, value: Any) -> list[str]:
        if not isinstance(value, list) or not value:
            raise ValueError("action_labels must be a non-empty list")
        out: list[str] = []
        for item in value:
            if not isinstance(item, str):
                raise ValueError("action_labels entries must be strings")
            text = item.strip()
            if not text:
                raise ValueError("action_labels entries must be non-empty")
            out.append(text)
        return out


class PublicRecoveryPlayProjectionV1(BaseModel):
    """Public-safe aggregate view of recovery plays."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["recovery-plays-public.v1"] = "recovery-plays-public.v1"
    run_id: str
    built_at: datetime
    provider: StrategyProviderName
    plays: list[PublicRecoveryPlayRowV1] = Field(default_factory=list)

    @field_validator("schema_version", mode="before")
    @classmethod
    def _schema(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("schema_version must be a string")
        text = value.strip()
        if text != "recovery-plays-public.v1":
            raise ValueError('schema_version must be exactly "recovery-plays-public.v1"')
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

    @field_validator("provider", mode="before")
    @classmethod
    def _provider(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("provider must be a string")
        text = value.strip()
        if text != "fixture":
            raise ValueError('provider must be exactly "fixture"')
        return text

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


class StrategyEvidenceItemV1(BaseModel):
    """One addressable, PII-free fact available to strategy claims."""

    model_config = ConfigDict(extra="forbid")

    evidence_id: str
    category: Literal["map_value", "map_basis", "exclusion", "data_gap", "business_input"]
    value: str | int | bool

    @field_validator("evidence_id", mode="before")
    @classmethod
    def _evidence_id(cls, value: Any) -> str:
        if not isinstance(value, str) or not value.strip().startswith("ev_"):
            raise ValueError("evidence_id must be a non-empty ev_ identifier")
        return value.strip()


class GroundedStrategyEvidencePacketV1(BaseModel):
    """Frozen boundary between deterministic analysis and provider strategy."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["grounded-strategy-evidence.v1"] = "grounded-strategy-evidence.v1"
    run_id: str
    built_at: datetime
    map_hash: str
    generated_after: Literal["deterministic_map_and_exclusions"] = (
        "deterministic_map_and_exclusions"
    )
    evidence: list[StrategyEvidenceItemV1]
    allowed_evidence_ids: list[str]

    @field_validator("run_id", mode="before")
    @classmethod
    def _run_id(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("run_id must be a string")
        return _normalize_identifier(value)

    @field_validator("built_at", mode="before")
    @classmethod
    def _built_at(cls, value: Any) -> datetime:
        return _parse_utc(value)

    @field_validator("map_hash", mode="before")
    @classmethod
    def _map_hash(cls, value: Any) -> str:
        if not isinstance(value, str) or len(value.strip()) != 64:
            raise ValueError("map_hash must be a sha256 hex digest")
        text = value.strip().lower()
        if any(char not in "0123456789abcdef" for char in text):
            raise ValueError("map_hash must be a sha256 hex digest")
        return text

    @model_validator(mode="after")
    def _evidence_catalog(self) -> Self:
        ids = [item.evidence_id for item in self.evidence]
        if len(ids) != len(set(ids)):
            raise ValueError("evidence ids must be unique")
        if self.allowed_evidence_ids != ids:
            raise ValueError("allowed_evidence_ids must exactly match evidence order")
        return self

    def canonical_dict(self) -> dict[str, Any]:
        payload = self.model_dump(mode="python")
        payload["built_at"] = _format_utc(payload["built_at"])
        return payload

    def to_canonical_json(self) -> bytes:
        text = json.dumps(self.canonical_dict(), sort_keys=True, separators=(",", ":"))
        return (text + "\n").encode("utf-8")


class StrategyBusinessProfileV1(BaseModel):
    """Optional human-supplied business facts; absent facts become decisions."""

    model_config = ConfigDict(extra="forbid")

    product: str | None = None
    proof: str | None = None
    margin: str | None = None
    channel: str | None = None
    capacity: str | None = None
    destination: str | None = None
    business_model: Literal["saas", "ecommerce", "service"] | None = None
    tenure_multiple_months: Decimal | None = None
    ltv_override_minor: Decimal | None = None

    @field_validator("product", "proof", "margin", "channel", "capacity", "destination")
    @classmethod
    def _optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        return text or None

    @field_validator("tenure_multiple_months", "ltv_override_minor", mode="before")
    @classmethod
    def _optional_decimal(cls, value: Any) -> Any:
        if value is None:
            return None
        return Decimal(str(value)) if not isinstance(value, Decimal) else value


class StrategyDecisionFlagV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    flag: DecisionFlagName
    withheld_assets: list[str]


class StrategyClaimV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    kind: StrategyClaimKind
    value: str
    evidence_ids: list[str]


class StrategyAssetV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: str
    status: Literal["renderable", "withheld"]
    content: dict[str, str] = Field(default_factory=dict)
    withheld_by: list[DecisionFlagName] = Field(default_factory=list)


class StrategyDraftV1(BaseModel):
    """Provider draft. Every factual claim must cite frozen evidence."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["strategy-draft.v1"] = "strategy-draft.v1"
    summary: str
    claims: list[StrategyClaimV1]
    assets: list[StrategyAssetV1]


class StrategyProviderRequestV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["strategy-provider-request.v1"] = "strategy-provider-request.v1"
    packet: GroundedStrategyEvidencePacketV1
    prompt_version: str
    prompt_hash: str
    output_schema_version: Literal["strategy-draft.v1"] = "strategy-draft.v1"
    output_schema_hash: str
    configured_model_id: str

    def canonical_dict(self) -> dict[str, Any]:
        payload = self.model_dump(mode="python")
        payload["packet"]["built_at"] = _format_utc(self.packet.built_at)
        return payload

    def to_canonical_json(self) -> bytes:
        text = json.dumps(self.canonical_dict(), sort_keys=True, separators=(",", ":"))
        return (text + "\n").encode("utf-8")


class StrategyTokenUsageV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


class StrategyProviderResponseV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["strategy-provider-response.v1"] = "strategy-provider-response.v1"
    returned_model_id: str
    response_id: str
    token_usage: StrategyTokenUsageV1
    draft: StrategyDraftV1

    def to_canonical_json(self) -> bytes:
        text = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return (text + "\n").encode("utf-8")


class StrategyAuditReceiptV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["strategy-audit-receipt.v1"] = "strategy-audit-receipt.v1"
    run_id: str
    status: Literal["completed", "needs_strategy_review"]
    prompt_version: str
    prompt_hash: str
    output_schema_version: str
    output_schema_hash: str
    configured_model_id: str
    returned_model_id: str | None
    response_id: str | None
    token_usage: StrategyTokenUsageV1
    attempt_count: int = Field(ge=1, le=2)
    evidence_packet_hash: str
    output_hash: str | None
    failure_code: str | None = None

    def to_canonical_json(self) -> bytes:
        text = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return (text + "\n").encode("utf-8")


class StrategyBoundaryResultV1(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    schema_version: Literal["strategy-boundary-result.v1"] = "strategy-boundary-result.v1"
    status: Literal["completed", "needs_strategy_review"]
    packet: GroundedStrategyEvidencePacketV1
    draft: StrategyDraftV1 | None
    decisions: list[StrategyDecisionFlagV1]
    receipt: StrategyAuditReceiptV1

    def canonical_dict(self) -> dict[str, Any]:
        payload = self.model_dump(mode="python")
        payload["packet"]["built_at"] = _format_utc(self.packet.built_at)
        return payload

    def to_canonical_json(self) -> bytes:
        text = json.dumps(self.canonical_dict(), sort_keys=True, separators=(",", ":"))
        return (text + "\n").encode("utf-8")


# Complete campaign contracts are split out to keep the FM-005/FM-025 module compatible.
from found_money.contracts.campaign import (  # noqa: E402, F401
    CalendarStepV1,
    CompleteRecoveryPlaySetV1,
    CompleteRecoveryPlayV1,
    ConceptCardV1,
    DifferentiationCheckV1,
    DifferentiationReportV1,
    EmailStepV1,
    GroundedCopyV1,
    ObjectionV1,
    OfferV1,
    ReviewerScoreV2,
    SmsPlanV1,
    StrategyReviewerPacketV2,
    TrackingPlanV1,
    UrgencyV1,
)
