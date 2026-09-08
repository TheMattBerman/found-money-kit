"""Validated appointment evidence contract (schema_version=appointments.v1)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_ALLOWED_STATUSES = frozenset({"scheduled", "completed", "canceled", "no_show"})


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
            raise ValueError("scheduled_at must be an ISO-8601 timestamp") from exc
    else:
        raise ValueError("scheduled_at must be an ISO-8601 timestamp")
    if parsed.tzinfo is None:
        raise ValueError("scheduled_at must be timezone-aware")
    parsed = parsed.astimezone(timezone.utc)
    return parsed.replace(microsecond=(parsed.microsecond // 1000) * 1000)


def _status(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("status must be a string")
    value = value.strip().lower()
    if not value:
        raise ValueError("status must be non-empty after normalization")
    if value not in _ALLOWED_STATUSES:
        raise ValueError(f"unsupported appointment status: {value}")
    return value


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class AppointmentV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    appointment_id: str
    customer_id: str
    scheduled_at: datetime
    status: str

    @field_validator("appointment_id", "customer_id", mode="before")
    @classmethod
    def _ids(cls, value: Any) -> str:
        return _identifier(value)

    @field_validator("scheduled_at", mode="before")
    @classmethod
    def _scheduled_at(cls, value: Any) -> datetime:
        return _timestamp(value)

    @field_validator("status", mode="before")
    @classmethod
    def _appointment_status(cls, value: Any) -> str:
        return _status(value)

    def canonical_dict(self) -> dict[str, Any]:
        result = self.model_dump(mode="python")
        result["scheduled_at"] = _format_utc(result["scheduled_at"])
        return result


class AppointmentsV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["appointments.v1"] = "appointments.v1"
    appointments: list[AppointmentV1] = Field(default_factory=list)

    @field_validator("schema_version", mode="before")
    @classmethod
    def _schema(cls, value: Any) -> str:
        if not isinstance(value, str) or value.strip() != "appointments.v1":
            raise ValueError('schema_version must be exactly "appointments.v1"')
        return "appointments.v1"

    @model_validator(mode="after")
    def _unique_ids(self) -> Self:
        ids = [item.appointment_id for item in self.appointments]
        if len(ids) != len(set(ids)):
            raise ValueError("appointment_id values must be unique")
        return self

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "appointments": [x.canonical_dict() for x in self.appointments],
        }

    def to_canonical_json(self) -> bytes:
        return (
            json.dumps(
                self.canonical_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            + "\n"
        ).encode("utf-8")
