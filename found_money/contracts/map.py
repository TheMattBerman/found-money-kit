"""Money Map contracts (schema_version=money-map.v1)."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from found_money.contracts.events import PUBLIC_NAMED_DATA_GAPS
from found_money.contracts.ranking import RankExplanationV1
from found_money.contracts.value import normalize_currency, normalize_minor_units

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
ValueBasis = Literal["observed_face_value", "modeled_opportunity", "mixed"]
ConfidenceClass = Literal["observed", "modeled", "mixed"]
StrategyStage = Literal["not_started", "completed", "failed", "needs_strategy_review"]
ReadinessClass = Literal["ready_for_strategy", "needs_data", "needs_strategy_review"]
NavigationState = Literal["available", "deferred", "unavailable"]

_CURRENCY_RE = re.compile(r"^[a-z]{3}$")
_PILE_IDS = {
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
}


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


def _stringify_decimals(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: _stringify_decimals(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_stringify_decimals(item) for item in value]
    return value


class CurrencyBasisCountsV1(BaseModel):
    """Per-currency event basis counts for the Money Map headline."""

    model_config = ConfigDict(extra="forbid")

    observed_event_count: int
    modeled_event_count: int
    unquantified_event_count: int

    @field_validator(
        "observed_event_count",
        "modeled_event_count",
        "unquantified_event_count",
        mode="before",
    )
    @classmethod
    def _non_negative_int(cls, value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("count must be a non-negative integer")
        if value < 0:
            raise ValueError("count must be a non-negative integer")
        return value


class MoneyMapNavigationV1(BaseModel):
    """Safe, deterministic pile-to-play navigation state."""

    model_config = ConfigDict(extra="forbid")

    target_id: str
    state: NavigationState
    play_id: str | None = None
    alternative_play_ids: list[str] | None = None
    next_action: str

    @field_validator("target_id", "next_action", mode="before")
    @classmethod
    def _text(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("navigation text must be a string")
        text = value.strip()
        if not text:
            raise ValueError("navigation text must be non-empty")
        return text

    @field_validator("play_id", mode="before")
    @classmethod
    def _play(cls, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise ValueError("navigation play_id must be a non-empty string")
        return value.strip()

    @model_validator(mode="after")
    def _state(self) -> Self:
        alternatives = self.alternative_play_ids or []
        if alternatives and self.state != "available":
            raise ValueError("only available navigation can carry alternative plays")
        if (
            any(not p.strip() or p != p.strip() for p in alternatives)
            or len(set(alternatives)) != len(alternatives)
            or self.play_id in alternatives
        ):
            raise ValueError("alternative play ids must be distinct non-empty ids")
        if self.state == "available" and self.play_id is None:
            raise ValueError("available navigation requires a play_id")
        if self.state != "available" and self.play_id is not None:
            raise ValueError("deferred or unavailable navigation cannot carry a play_id")
        return self


class MoneyMapPileV1(BaseModel):
    """One ranked recovery pile on the Money Map."""

    model_config = ConfigDict(extra="forbid")

    pile_id: PileId
    rank: int
    value_basis: ValueBasis
    currency: str
    selected_value_minor: Decimal
    customer_count: int
    economic_unit_count: int
    confidence_class: ConfidenceClass
    why_recoverable: str
    source_count: int | None = Field(default=None, ge=0)
    readiness: ReadinessClass | None = None
    rank_explanation: RankExplanationV1 | None = None
    navigation: MoneyMapNavigationV1 | None = None

    @field_validator("pile_id", mode="before")
    @classmethod
    def _pile(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("pile_id must be a string")
        text = value.strip()
        if text not in _PILE_IDS:
            raise ValueError("pile_id is not a supported event-family pile")
        return text

    @field_validator("rank", "customer_count", "economic_unit_count", mode="before")
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

    @field_validator("why_recoverable", mode="before")
    @classmethod
    def _why(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("why_recoverable must be a string")
        text = value.strip()
        if not text:
            raise ValueError("why_recoverable must be non-empty")
        return text

    @field_validator("source_count", mode="before")
    @classmethod
    def _source_count(cls, value: Any) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("source_count must be a non-negative integer")
        return value


class MoneyMapV1(BaseModel):
    """Deterministic Money Map for one run."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["money-map.v1"] = "money-map.v1"
    run_id: str
    built_at: datetime
    identified_opportunity_minor: dict[str, Decimal] = Field(default_factory=dict)
    basis_counts_by_currency: dict[str, CurrencyBasisCountsV1] = Field(default_factory=dict)
    piles: list[MoneyMapPileV1] = Field(default_factory=list)
    recommended_play_ids: list[str] = Field(default_factory=list)
    strategy_stage: StrategyStage = "not_started"
    data_gap_count: int = 0
    overlap_exclusion_count: int = 0
    named_data_gaps: list[str] = Field(default_factory=list)
    event_exclusions_by_reason: dict[str, int] = Field(default_factory=dict)
    customer_count: int | None = Field(default=None, ge=0)
    source_count: int | None = Field(default=None, ge=0)
    next_action: str | None = None

    @field_validator("schema_version", mode="before")
    @classmethod
    def _schema(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("schema_version must be a string")
        text = value.strip()
        if text != "money-map.v1":
            raise ValueError('schema_version must be exactly "money-map.v1"')
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

    @field_validator("identified_opportunity_minor", mode="before")
    @classmethod
    def _headline(cls, value: Any) -> dict[str, Decimal]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("identified_opportunity_minor must be an object")
        out: dict[str, Decimal] = {}
        for key, raw in value.items():
            if not isinstance(key, str):
                raise ValueError("currency keys must be strings")
            out[normalize_currency(key)] = normalize_minor_units(raw)
        return out

    @field_validator("basis_counts_by_currency", mode="before")
    @classmethod
    def _basis(cls, value: Any) -> dict[str, CurrencyBasisCountsV1]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("basis_counts_by_currency must be an object")
        out: dict[str, CurrencyBasisCountsV1] = {}
        for key, raw in value.items():
            if not isinstance(key, str):
                raise ValueError("currency keys must be strings")
            currency = normalize_currency(key)
            if isinstance(raw, CurrencyBasisCountsV1):
                out[currency] = raw
            elif isinstance(raw, dict):
                out[currency] = CurrencyBasisCountsV1.model_validate(raw)
            else:
                raise ValueError("basis count entries must be objects")
        return out

    @field_validator("recommended_play_ids", mode="before")
    @classmethod
    def _plays(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError("recommended_play_ids must be a list")
        out: list[str] = []
        for item in value:
            if not isinstance(item, str):
                raise ValueError("recommended_play_ids entries must be strings")
            out.append(_normalize_identifier(item))
        return out

    @field_validator("strategy_stage", mode="before")
    @classmethod
    def _stage(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("strategy_stage must be a string")
        text = value.strip()
        if text not in {"not_started", "completed", "failed", "needs_strategy_review"}:
            raise ValueError(
                "strategy_stage must be not_started, completed, failed, or needs_strategy_review"
            )
        return text

    @field_validator("data_gap_count", "overlap_exclusion_count", mode="before")
    @classmethod
    def _non_negative_count(cls, value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("map counts must be non-negative integers")
        return value

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

    @field_validator("event_exclusions_by_reason", mode="before")
    @classmethod
    def _event_exclusions(cls, value: Any) -> dict[str, int]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("event_exclusions_by_reason must be an object")
        result: dict[str, int] = {}
        for key, count in value.items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError("event exclusion keys must be strings")
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ValueError("event exclusion counts must be non-negative integers")
            result[key.strip()] = count
        return dict(sorted(result.items()))

    @field_validator("next_action", mode="before")
    @classmethod
    def _next_action(cls, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise ValueError("next_action must be a non-empty string")
        return value.strip()

    @model_validator(mode="after")
    def _ranks(self) -> Self:
        ranks = [pile.rank for pile in self.piles]
        if len(ranks) != len(set(ranks)):
            raise ValueError("pile ranks must be unique")
        if len(self.recommended_play_ids) != len(set(self.recommended_play_ids)):
            raise ValueError("recommended_play_ids must be unique")
        available_play_ids = {
            pile.navigation.play_id
            for pile in self.piles
            if pile.navigation is not None
            and pile.navigation.state == "available"
            and pile.navigation.play_id is not None
        }
        for pile in self.piles:
            if pile.navigation is not None and pile.navigation.state == "available":
                available_play_ids.update(pile.navigation.alternative_play_ids or [])
        if not set(self.recommended_play_ids).issubset(available_play_ids):
            raise ValueError("recommended play ids must resolve to available pile navigation")
        return self

    def canonical_dict(self) -> dict[str, Any]:
        payload = self.model_dump(mode="python")
        payload["built_at"] = _format_utc(payload["built_at"])
        if payload["data_gap_count"] == 0:
            payload.pop("data_gap_count")
        if payload["overlap_exclusion_count"] == 0:
            payload.pop("overlap_exclusion_count")
        if not payload.get("named_data_gaps"):
            payload.pop("named_data_gaps", None)
        if not payload.get("event_exclusions_by_reason"):
            payload.pop("event_exclusions_by_reason", None)
        for key in ("customer_count", "source_count", "next_action"):
            if payload.get(key) is None:
                payload.pop(key, None)
        payload["identified_opportunity_minor"] = {
            key: str(value) for key, value in sorted(self.identified_opportunity_minor.items())
        }
        payload["basis_counts_by_currency"] = {
            key: (
                value.model_dump(mode="python")
                if isinstance(value, CurrencyBasisCountsV1)
                else value
            )
            for key, value in sorted(self.basis_counts_by_currency.items())
        }
        for pile in payload["piles"]:
            pile["selected_value_minor"] = str(pile["selected_value_minor"])
            for key in ("source_count", "readiness", "rank_explanation", "navigation"):
                if pile.get(key) is None:
                    pile.pop(key, None)
            explanation = pile.get("rank_explanation")
            if isinstance(explanation, dict):
                for factor in explanation.get("factors", []):
                    if isinstance(factor, dict) and "name" in factor:
                        factor["factor"] = factor.pop("name")
        return _stringify_decimals(payload)

    def to_canonical_json(self) -> bytes:
        text = json.dumps(
            self.canonical_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return (text + "\n").encode("utf-8")


class PublicMoneyMapPileV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pile_id: PileId
    rank: int
    currency: str
    selected_value_minor: Decimal
    value_basis: ValueBasis | None = None
    confidence_class: ConfidenceClass | None = None
    customer_count: int | None = Field(default=None, ge=0)
    source_count: int | None = Field(default=None, ge=0)
    readiness: ReadinessClass | None = None
    navigation: MoneyMapNavigationV1 | None = None

    @field_validator("pile_id", mode="before")
    @classmethod
    def _pile(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("pile_id must be a string")
        text = value.strip()
        if text not in _PILE_IDS:
            raise ValueError("pile_id is not a supported event-family pile")
        return text

    @field_validator("rank", mode="before")
    @classmethod
    def _rank(cls, value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("rank must be a positive integer")
        if value < 1:
            raise ValueError("rank must be a positive integer")
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

    @field_validator("customer_count", "source_count", mode="before")
    @classmethod
    def _counts(cls, value: Any) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("public counts must be non-negative integers")
        return value


class PublicMoneyMapProjectionV1(BaseModel):
    """Public-safe aggregate view of the Money Map."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["money-map-public.v1"] = "money-map-public.v1"
    run_id: str
    built_at: datetime
    identified_opportunity_minor: dict[str, Decimal] = Field(default_factory=dict)
    basis_counts_by_currency: dict[str, CurrencyBasisCountsV1] = Field(default_factory=dict)
    piles: list[PublicMoneyMapPileV1] = Field(default_factory=list)
    data_gap_count: int = 0
    overlap_exclusion_count: int = 0
    named_data_gaps: list[str] = Field(default_factory=list)
    event_exclusions_by_reason: dict[str, int] = Field(default_factory=dict)
    strategy_stage: StrategyStage | None = None
    customer_count: int | None = Field(default=None, ge=0)
    source_count: int | None = Field(default=None, ge=0)
    next_action: str | None = None

    @field_validator("schema_version", mode="before")
    @classmethod
    def _schema(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("schema_version must be a string")
        text = value.strip()
        if text != "money-map-public.v1":
            raise ValueError('schema_version must be exactly "money-map-public.v1"')
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

    @field_validator("identified_opportunity_minor", mode="before")
    @classmethod
    def _headline(cls, value: Any) -> dict[str, Decimal]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("identified_opportunity_minor must be an object")
        out: dict[str, Decimal] = {}
        for key, raw in value.items():
            if not isinstance(key, str):
                raise ValueError("currency keys must be strings")
            out[normalize_currency(key)] = normalize_minor_units(raw)
        return out

    @field_validator("basis_counts_by_currency", mode="before")
    @classmethod
    def _basis(cls, value: Any) -> dict[str, CurrencyBasisCountsV1]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("basis_counts_by_currency must be an object")
        out: dict[str, CurrencyBasisCountsV1] = {}
        for key, raw in value.items():
            if not isinstance(key, str):
                raise ValueError("currency keys must be strings")
            currency = normalize_currency(key)
            if isinstance(raw, CurrencyBasisCountsV1):
                out[currency] = raw
            elif isinstance(raw, dict):
                out[currency] = CurrencyBasisCountsV1.model_validate(raw)
            else:
                raise ValueError("basis count entries must be objects")
        return out

    @field_validator("next_action", mode="before")
    @classmethod
    def _next_action(cls, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise ValueError("next_action must be a non-empty string")
        return value.strip()

    @field_validator("data_gap_count", "overlap_exclusion_count", mode="before")
    @classmethod
    def _non_negative_count(cls, value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("public map counts must be non-negative integers")
        return value

    def canonical_dict(self) -> dict[str, Any]:
        payload = self.model_dump(mode="python")
        payload["built_at"] = _format_utc(payload["built_at"])
        if payload["data_gap_count"] == 0:
            payload.pop("data_gap_count")
        if payload["overlap_exclusion_count"] == 0:
            payload.pop("overlap_exclusion_count")
        if not payload.get("named_data_gaps"):
            payload.pop("named_data_gaps", None)
        if not payload.get("event_exclusions_by_reason"):
            payload.pop("event_exclusions_by_reason", None)
        for key in ("strategy_stage", "customer_count", "source_count", "next_action"):
            if payload.get(key) is None:
                payload.pop(key, None)
        for pile in payload["piles"]:
            for key in (
                "value_basis",
                "confidence_class",
                "customer_count",
                "source_count",
                "readiness",
                "navigation",
            ):
                if pile.get(key) is None:
                    pile.pop(key, None)
        payload["identified_opportunity_minor"] = {
            key: str(value)
            for key, value in sorted(payload["identified_opportunity_minor"].items())
        }
        basis: dict[str, Any] = {}
        for key, value in sorted(payload["basis_counts_by_currency"].items()):
            if isinstance(value, CurrencyBasisCountsV1):
                basis[key] = value.model_dump(mode="python")
            else:
                basis[key] = value
        payload["basis_counts_by_currency"] = basis
        for pile in payload["piles"]:
            pile["selected_value_minor"] = str(pile["selected_value_minor"])
        return _stringify_decimals(payload)

    def to_canonical_json(self) -> bytes:
        text = json.dumps(
            self.canonical_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return (text + "\n").encode("utf-8")
