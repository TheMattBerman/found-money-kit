"""Deterministic, explainable Money Map ranking contracts."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Literal, Self

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

RankFactorName = Literal[
    "value",
    "event_strength",
    "recency",
    "reachable_count",
    "offer_fit",
    "friction",
    "proof_context",
    "readiness",
]

RANK_FACTOR_NAMES: tuple[str, ...] = (
    "value",
    "event_strength",
    "recency",
    "reachable_count",
    "offer_fit",
    "friction",
    "proof_context",
    "readiness",
)

# Percent weights intentionally sum to 100. They are dimensions, not currency
# conversions, so no FX assumption is hidden in the rank.
DEFAULT_RANK_WEIGHTS: dict[str, Decimal] = {
    "value": Decimal("30"),
    "event_strength": Decimal("20"),
    "recency": Decimal("15"),
    "reachable_count": Decimal("10"),
    "offer_fit": Decimal("10"),
    "friction": Decimal("5"),
    "proof_context": Decimal("5"),
    "readiness": Decimal("5"),
}

RANK_SCORE_QUANTUM = Decimal("0.000001")


def quantize_rank(value: Decimal) -> Decimal:
    """Keep rank arithmetic deterministic and readable in canonical JSON."""
    return value.quantize(RANK_SCORE_QUANTUM, rounding=ROUND_HALF_UP)


def _decimal(value: Any, *, label: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError(f"{label} must be a Decimal or decimal string")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, (int, str)):
        try:
            result = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError(f"{label} must be a Decimal or decimal string") from exc
    else:
        raise ValueError(f"{label} must be a Decimal or decimal string")
    if not result.is_finite():
        raise ValueError(f"{label} must be finite")
    return result


def _score(value: Any) -> Decimal:
    result = _decimal(value, label="rank score")
    if result < 0 or result > 1:
        raise ValueError("rank score must be between 0 and 1")
    return quantize_rank(result)


def _weight(value: Any) -> Decimal:
    result = _decimal(value, label="rank weight")
    if result < 0 or result > 100:
        raise ValueError("rank weight must be between 0 and 100")
    return quantize_rank(result)


def _contribution(value: Any) -> Decimal:
    result = _decimal(value, label="rank contribution")
    if result < 0 or result > 100:
        raise ValueError("rank contribution must be between 0 and 100")
    return quantize_rank(result)


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    result = value.strip()
    if not result:
        raise ValueError(f"{label} must be non-empty")
    return result


def _refs(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("evidence_references must be a list")
    result = {_text(item, "evidence reference") for item in value}
    return sorted(result)


class RankFactorV1(BaseModel):
    """One inspectable factor in a deterministic pile rank."""

    model_config = ConfigDict(extra="forbid")

    name: RankFactorName = Field(validation_alias=AliasChoices("name", "factor"))
    score: Decimal
    weight: Decimal
    contribution: Decimal
    source_count: int = Field(ge=0)
    explanation: str
    evidence_references: list[str] = Field(default_factory=list)

    @field_validator("score", mode="before")
    @classmethod
    def _score(cls, value: Any) -> Decimal:
        return _score(value)

    @field_validator("weight", mode="before")
    @classmethod
    def _weight(cls, value: Any) -> Decimal:
        return _weight(value)

    @field_validator("contribution", mode="before")
    @classmethod
    def _contribution(cls, value: Any) -> Decimal:
        return _contribution(value)

    @field_validator("source_count", mode="before")
    @classmethod
    def _source_count(cls, value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("source_count must be a non-negative integer")
        return value

    @field_validator("explanation", mode="before")
    @classmethod
    def _explanation(cls, value: Any) -> str:
        return _text(value, "explanation")

    @field_validator("evidence_references", mode="before")
    @classmethod
    def _evidence_references(cls, value: Any) -> list[str]:
        return _refs(value)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        expected = quantize_rank(self.score * self.weight)
        if self.contribution != expected:
            raise ValueError("rank contribution must equal score multiplied by weight")
        if self.score > 0 and (self.source_count < 1 or not self.evidence_references):
            raise ValueError(
                "a non-zero rank factor requires a source count and evidence reference"
            )
        return self


class RankExplanationV1(BaseModel):
    """Complete weighted rank explanation for one Money Map pile."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["rank-explanation.v1"] = "rank-explanation.v1"
    total_score: Decimal
    weights: dict[str, Decimal]
    factors: list[RankFactorV1]
    tie_break_key: str

    @field_validator("schema_version", mode="before")
    @classmethod
    def _schema(cls, value: Any) -> str:
        if not isinstance(value, str) or value.strip() != "rank-explanation.v1":
            raise ValueError('schema_version must be exactly "rank-explanation.v1"')
        return value.strip()

    @field_validator("total_score", mode="before")
    @classmethod
    def _total_score(cls, value: Any) -> Decimal:
        result = _contribution(value)
        if result > 100:
            raise ValueError("total_score must be between 0 and 100")
        return result

    @field_validator("weights", mode="before")
    @classmethod
    def _weights(cls, value: Any) -> dict[str, Decimal]:
        if not isinstance(value, dict):
            raise ValueError("weights must be an object")
        if set(value) != set(RANK_FACTOR_NAMES):
            raise ValueError("weights must declare every rank factor exactly once")
        return {key: _weight(value[key]) for key in RANK_FACTOR_NAMES}

    @field_validator("tie_break_key", mode="before")
    @classmethod
    def _tie_break_key(cls, value: Any) -> str:
        return _text(value, "tie_break_key")

    @model_validator(mode="after")
    def _complete(self) -> Self:
        if sum(self.weights.values(), Decimal(0)) != Decimal(100):
            raise ValueError("rank weights must sum to 100")
        names = [factor.name for factor in self.factors]
        if set(names) != set(RANK_FACTOR_NAMES) or len(names) != len(RANK_FACTOR_NAMES):
            raise ValueError("rank explanation must contain every factor exactly once")
        self.factors = sorted(
            self.factors,
            key=lambda factor: RANK_FACTOR_NAMES.index(factor.name),
        )
        expected = quantize_rank(sum((item.contribution for item in self.factors), Decimal(0)))
        if self.total_score != expected:
            raise ValueError("total_score must equal the sum of factor contributions")
        for factor in self.factors:
            if factor.weight != self.weights[factor.name]:
                raise ValueError(f"factor weight mismatch for {factor.name}")
        return self


__all__ = [
    "DEFAULT_RANK_WEIGHTS",
    "RANK_FACTOR_NAMES",
    "RANK_SCORE_QUANTUM",
    "RankFactorName",
    "RankFactorV1",
    "RankExplanationV1",
    "quantize_rank",
]
