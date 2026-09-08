"""Optional high-value cart contract (schema_version=ecommerce-optional-cart.v1)."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from found_money.contracts.value import normalize_currency, normalize_minor_units
from found_money.receipts import sha256_bytes


def _object_pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate JSON object key: {key}")
        out[key] = value
    return out


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware UTC")
    as_utc = value.astimezone(timezone.utc)
    return as_utc.replace(microsecond=(as_utc.microsecond // 1000) * 1000)


def _parse_utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        return _require_utc(value)
    if not isinstance(value, str):
        raise ValueError("timestamp must be an ISO-8601 UTC value")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return _require_utc(datetime.fromisoformat(text))


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


CART_SCHEMA_VERSION = "ecommerce-optional-cart.v1"
HIGH_VALUE_CART_THRESHOLD_MINOR = Decimal("10000")
CART_RECENT_WINDOW = timedelta(days=1)
OMITTED_CART_STATE = "omitted"
SUPPLIED_CART_STATE = "supplied"
_CART_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{2,}$")
_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{2,}$")


def _normalize_cart_id(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("cart_id must be a string")
    text = value.strip()
    if not _CART_ID_RE.fullmatch(text):
        raise ValueError("cart_id must be a validated identifier")
    return text


def _normalize_external_id(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("customer_external_id must be a string")
    text = value.strip()
    if not _ID_RE.fullmatch(text):
        raise ValueError("customer_external_id must be a declared customer external ID")
    return text


def _canonical_json_bytes(payload: Any) -> bytes:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return (text + "\n").encode("utf-8")


class HighValueCartRecordV1(BaseModel):
    """One optional high-value cart row with money, currency, and declared join key."""

    model_config = ConfigDict(extra="forbid")

    cart_id: str
    customer_external_id: str
    currency: str
    total_minor: Decimal
    state: Literal["open", "abandoned", "completed"]
    observed_at: datetime

    @field_validator("cart_id", mode="before")
    @classmethod
    def _cart_id(cls, value: Any) -> str:
        return _normalize_cart_id(value)

    @field_validator("customer_external_id", mode="before")
    @classmethod
    def _external(cls, value: Any) -> str:
        return _normalize_external_id(value)

    @field_validator("currency", mode="before")
    @classmethod
    def _currency(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("currency must be a string")
        return normalize_currency(value)

    @field_validator("total_minor", mode="before")
    @classmethod
    def _amount(cls, value: Any) -> Decimal:
        return normalize_minor_units(value)

    @field_validator("observed_at", mode="before")
    @classmethod
    def _observed(cls, value: Any) -> datetime:
        return _parse_utc(value)

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "cart_id": self.cart_id,
            "currency": self.currency,
            "customer_external_id": self.customer_external_id,
            "observed_at": _format_utc(self.observed_at),
            "state": self.state,
            "total_minor": str(self.total_minor),
        }


class EcommerceOptionalCartV1(BaseModel):
    """Fail-closed optional cart document. Empty objects and extra keys are rejected."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["ecommerce-optional-cart.v1"] = "ecommerce-optional-cart.v1"
    state: Literal["supplied", "omitted"]
    carts: list[HighValueCartRecordV1] = Field(default_factory=list)

    @field_validator("schema_version", mode="before")
    @classmethod
    def _schema(cls, value: Any) -> Literal["ecommerce-optional-cart.v1"]:
        if not isinstance(value, str) or value.strip() != CART_SCHEMA_VERSION:
            raise ValueError(f'schema_version must be exactly "{CART_SCHEMA_VERSION}"')
        return "ecommerce-optional-cart.v1"

    @model_validator(mode="after")
    def _supplied_or_omitted(self) -> Self:
        ids = [row.cart_id for row in self.carts]
        if len(ids) != len(set(ids)):
            raise ValueError("cart_id values must be unique")
        if self.state == "omitted":
            if self.carts:
                raise ValueError("omitted cart binding must not include cart rows")
        elif not self.carts:
            raise ValueError("supplied cart binding requires at least one validated cart")
        return self

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "carts": [row.canonical_dict() for row in self.carts],
            "schema_version": self.schema_version,
            "state": self.state,
        }

    def to_canonical_json(self) -> bytes:
        return _canonical_json_bytes(self.canonical_dict())

    def sha256(self) -> str:
        return sha256_bytes(self.to_canonical_json())


def omitted_cart_binding() -> EcommerceOptionalCartV1:
    return EcommerceOptionalCartV1(state="omitted", carts=[])


def parse_optional_cart(data: bytes) -> EcommerceOptionalCartV1:
    try:
        payload = json.loads(data.decode("utf-8"), object_pairs_hook=_object_pairs_hook)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("optional cart input is malformed") from exc
    if not isinstance(payload, dict):
        raise ValueError("optional cart input must be an object")
    model = EcommerceOptionalCartV1.model_validate(payload)
    if model.to_canonical_json() != bytes(data):
        raise ValueError("serialized cart bytes are not exactly canonical")
    return model


def cart_qualification_reason(
    cart: HighValueCartRecordV1,
    *,
    clock: datetime,
    joined: bool,
) -> str | None:
    """Return a withhold reason, or None when the cart qualifies as high-value."""
    if not joined:
        return "wrong_customer_high_value_cart"
    if cart.state == "completed":
        return "completed_high_value_cart"
    if clock - cart.observed_at < CART_RECENT_WINDOW:
        return "recent_high_value_cart"
    amount = normalize_minor_units(cart.total_minor)
    if amount < HIGH_VALUE_CART_THRESHOLD_MINOR:
        return "below_threshold_high_value_cart"
    return None
