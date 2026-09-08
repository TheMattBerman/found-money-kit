"""Validated order evidence contract (schema_version=orders.v1)."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from found_money.contracts.value import normalize_currency

_INTEGER_RE = re.compile(r"^[0-9]+$")


def _normalize_id(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("identifier must be a string")
    text = value.strip()
    if not text:
        raise ValueError("identifier must be non-empty after normalization")
    return text


def _parse_ordered_at(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError("ordered_at must be an ISO-8601 timestamp") from exc
    else:
        raise ValueError("ordered_at must be an ISO-8601 timestamp")
    if parsed.tzinfo is None:
        raise ValueError("ordered_at must be timezone-aware")
    as_utc = parsed.astimezone(timezone.utc)
    return as_utc.replace(microsecond=(as_utc.microsecond // 1000) * 1000)


def _normalize_total_minor(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("total_minor must be a non-negative integer")
    if isinstance(value, int):
        amount = value
    elif isinstance(value, str):
        text = value.strip()
        if not _INTEGER_RE.fullmatch(text):
            raise ValueError("total_minor must be a non-negative integer")
        amount = int(text)
    else:
        raise ValueError("total_minor must be a non-negative integer")
    if amount < 0:
        raise ValueError("total_minor must be a non-negative integer")
    return amount


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class OrderV1(BaseModel):
    """One normalized order record."""

    model_config = ConfigDict(extra="forbid")

    order_id: str
    customer_id: str
    ordered_at: datetime
    currency: str
    total_minor: int

    @field_validator("order_id", "customer_id", mode="before")
    @classmethod
    def _ids(cls, value: Any) -> str:
        return _normalize_id(value)

    @field_validator("ordered_at", mode="before")
    @classmethod
    def _ordered_at(cls, value: Any) -> datetime:
        return _parse_ordered_at(value)

    @field_validator("currency", mode="before")
    @classmethod
    def _currency(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("currency must be a string")
        return normalize_currency(value)

    @field_validator("total_minor", mode="before")
    @classmethod
    def _total_minor(cls, value: Any) -> int:
        return _normalize_total_minor(value)

    def canonical_dict(self) -> dict[str, Any]:
        payload = self.model_dump(mode="python")
        payload["ordered_at"] = _format_utc(payload["ordered_at"])
        return payload


class OrdersV1(BaseModel):
    """Canonical, duplicate-free normalized order evidence."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["orders.v1"] = "orders.v1"
    orders: list[OrderV1] = Field(default_factory=list)

    @field_validator("schema_version", mode="before")
    @classmethod
    def _schema_version(cls, value: Any) -> str:
        if not isinstance(value, str) or value.strip() != "orders.v1":
            raise ValueError('schema_version must be exactly "orders.v1"')
        return "orders.v1"

    @model_validator(mode="after")
    def _unique_order_ids(self) -> Self:
        order_ids = [order.order_id for order in self.orders]
        if len(order_ids) != len(set(order_ids)):
            raise ValueError("order_id values must be unique")
        return self

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "orders": [order.canonical_dict() for order in self.orders],
        }

    def to_canonical_json(self) -> bytes:
        return (
            json.dumps(
                self.canonical_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            + "\n"
        ).encode("utf-8")
