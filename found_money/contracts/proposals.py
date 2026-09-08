"""Validated proposal evidence contract (schema_version=proposals.v1)."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from found_money.contracts.value import normalize_currency


_INTEGER_RE = re.compile(r"^[0-9]+$")
_ALLOWED_STATUSES = frozenset({"draft", "sent", "accepted", "rejected", "expired"})


def _identifier(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("identifier must be a string")
    value = value.strip()
    if not value:
        raise ValueError("identifier must be non-empty after normalization")
    return value


def _timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError("proposed_at must be an ISO-8601 timestamp") from exc
    else:
        raise ValueError("proposed_at must be an ISO-8601 timestamp")
    if parsed.tzinfo is None:
        raise ValueError("proposed_at must be timezone-aware")
    parsed = parsed.astimezone(timezone.utc)
    return parsed.replace(microsecond=(parsed.microsecond // 1000) * 1000)


def _status(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("status must be a string")
    value = value.strip().lower()
    if not value:
        raise ValueError("status must be non-empty after normalization")
    if value not in _ALLOWED_STATUSES:
        raise ValueError(f"unsupported proposal status: {value}")
    return value


def _amount(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("amount_minor must be a non-negative integer")
    if isinstance(value, int):
        amount = value
    elif isinstance(value, str) and _INTEGER_RE.fullmatch(value.strip()):
        amount = int(value.strip())
    else:
        raise ValueError("amount_minor must be a non-negative integer")
    if amount < 0:
        raise ValueError("amount_minor must be a non-negative integer")
    return amount


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class ProposalV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_id: str
    customer_id: str
    proposed_at: datetime
    status: str
    amount_minor: int
    currency: str

    @field_validator("proposal_id", "customer_id", mode="before")
    @classmethod
    def _ids(cls, value: Any) -> str:
        return _identifier(value)

    @field_validator("proposed_at", mode="before")
    @classmethod
    def _proposed_at(cls, value: Any) -> datetime:
        return _timestamp(value)

    @field_validator("status", mode="before")
    @classmethod
    def _proposal_status(cls, value: Any) -> str:
        return _status(value)

    @field_validator("amount_minor", mode="before")
    @classmethod
    def _amount_minor(cls, value: Any) -> int:
        return _amount(value)

    @field_validator("currency", mode="before")
    @classmethod
    def _currency(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("currency must be a string")
        return normalize_currency(value)

    def canonical_dict(self) -> dict[str, Any]:
        result = self.model_dump(mode="python")
        result["proposed_at"] = _format_utc(result["proposed_at"])
        return result


class ProposalsV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["proposals.v1"] = "proposals.v1"
    proposals: list[ProposalV1] = Field(default_factory=list)

    @field_validator("schema_version", mode="before")
    @classmethod
    def _schema(cls, value: Any) -> str:
        if not isinstance(value, str) or value.strip() != "proposals.v1":
            raise ValueError('schema_version must be exactly "proposals.v1"')
        return "proposals.v1"

    @model_validator(mode="after")
    def _unique_ids(self) -> Self:
        ids = [item.proposal_id for item in self.proposals]
        if len(ids) != len(set(ids)):
            raise ValueError("proposal_id values must be unique")
        return self

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "proposals": [x.canonical_dict() for x in self.proposals],
        }

    def to_canonical_json(self) -> bytes:
        return (
            json.dumps(
                self.canonical_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            + "\n"
        ).encode("utf-8")
