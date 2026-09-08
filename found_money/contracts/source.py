"""Source receipt contract (schema_version=source-receipt.v1)."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

LocatorKind = Literal["endpoint", "input_path"]

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ENDPOINT_QUERY_RE = re.compile(r"[?#]")
_TRAVERSAL_RE = re.compile(r"(^|/)\.\.(/|$)")
_WINDOWS_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware UTC")
    as_utc = value.astimezone(timezone.utc)
    # Canonical millisecond precision for stable JSON encoding.
    return as_utc.replace(microsecond=(as_utc.microsecond // 1000) * 1000)


def _normalize_identifier(value: str) -> str:
    text = value.strip()
    if not text:
        raise ValueError("identifier must be non-empty after normalization")
    return text


def _normalize_hash(value: str) -> str:
    text = value.strip().lower()
    if not _SHA256_RE.fullmatch(text):
        raise ValueError("content_hash must be a lowercase 64-char SHA-256 hex digest")
    return text


def _normalize_schema_version(value: str) -> str:
    text = value.strip()
    if text != "source-receipt.v1":
        raise ValueError('schema_version must be exactly "source-receipt.v1"')
    return text


def _normalize_locator(kind: LocatorKind, raw: str) -> str:
    text = raw.strip().replace("\\", "/")
    if not text:
        raise ValueError("locator must be non-empty after normalization")
    if _WINDOWS_ABS_RE.match(text) or text.startswith("~/") or text.startswith("file:"):
        raise ValueError("locator must not be an absolute local path")
    if _TRAVERSAL_RE.search(text) or text == "..":
        raise ValueError("locator must not contain path traversal")
    if kind == "endpoint":
        if _ENDPOINT_QUERY_RE.search(text):
            raise ValueError("endpoint locator must be query-free")
        if "//" in text:
            raise ValueError("endpoint locator path is invalid")
        return text
    # input_path: traversal-free relative path only
    if text.startswith("/"):
        raise ValueError("input_path locator must be a relative path")
    if text.startswith("./"):
        text = text[2:]
    if not text or text.startswith("/") or _ENDPOINT_QUERY_RE.search(text):
        raise ValueError("input_path locator must be a traversal-free relative path")
    return text


class SourceReceiptV1(BaseModel):
    """Deterministic provenance receipt for one retrieved source snapshot."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["source-receipt.v1"] = "source-receipt.v1"
    source_type: str
    connector_schema_version: str
    retrieved_at: datetime
    locator_kind: LocatorKind
    locator: str
    page_or_row_count: int = Field(ge=0)
    record_count: int = Field(ge=0)
    content_hash: str
    request_id: str | None = None
    correlation_id: str | None = None

    @field_validator("schema_version", mode="before")
    @classmethod
    def _schema_version(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("schema_version must be a string")
        return _normalize_schema_version(value)

    @field_validator("source_type", "connector_schema_version", mode="before")
    @classmethod
    def _identifiers(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("value must be a string")
        return _normalize_identifier(value)

    @field_validator("retrieved_at", mode="before")
    @classmethod
    def _retrieved_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _require_utc(value)
        if isinstance(value, str):
            text = value.strip()
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            parsed = datetime.fromisoformat(text)
            return _require_utc(parsed)
        raise ValueError("retrieved_at must be an ISO-8601 UTC timestamp")

    @field_validator("content_hash", mode="before")
    @classmethod
    def _content_hash(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("content_hash must be a string")
        return _normalize_hash(value)

    @field_validator("request_id", "correlation_id", mode="before")
    @classmethod
    def _optional_ids(cls, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("optional id must be a string or null")
        return _normalize_identifier(value)

    @model_validator(mode="after")
    def _locator_rules(self) -> Self:
        normalized = _normalize_locator(self.locator_kind, self.locator)
        if normalized != self.locator:
            return self.model_copy(update={"locator": normalized})
        return self

    def canonical_dict(self) -> dict[str, Any]:
        payload = self.model_dump(mode="python")
        retrieved = payload["retrieved_at"]
        assert isinstance(retrieved, datetime)
        payload["retrieved_at"] = (
            retrieved.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        )
        return payload

    def to_canonical_json(self) -> bytes:
        text = json.dumps(
            self.canonical_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return (text + "\n").encode("utf-8")
