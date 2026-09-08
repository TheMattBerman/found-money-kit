"""Run manifest contract (schema_version=run-manifest.v1)."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Literal, Mapping, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    field_validator,
    model_validator,
)

StageState = Literal["not_started", "completed", "failed"]
RunMode = Literal["public", "private"]

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TRAVERSAL_RE = re.compile(r"(^|/)\.\.(/|$)")
_WINDOWS_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")
_CREDENTIAL_KEY_RE = re.compile(
    r"(?i)^(password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|"
    r"authorization|credential|credentials|auth|bearer|client[_-]?secret)$"
)


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


def _normalize_hash(value: str) -> str:
    text = value.strip().lower()
    if not _SHA256_RE.fullmatch(text):
        raise ValueError("hash must be a lowercase 64-char SHA-256 hex digest")
    return text


def _normalize_relative_path(value: str) -> str:
    text = value.strip().replace("\\", "/")
    if not text:
        raise ValueError("path must be non-empty after normalization")
    if text.startswith("/") or _WINDOWS_ABS_RE.match(text) or text.startswith("~/"):
        raise ValueError("path must be relative")
    if text.startswith("./"):
        text = text[2:]
    if not text or _TRAVERSAL_RE.search(text) or text == "..":
        raise ValueError("path must not contain traversal")
    return text


def _normalize_schema_version(value: str) -> str:
    text = value.strip()
    if text != "run-manifest.v1":
        raise ValueError('schema_version must be exactly "run-manifest.v1"')
    return text


def _reject_credential_keys(payload: Mapping[str, Any]) -> None:
    for key in payload:
        if _CREDENTIAL_KEY_RE.fullmatch(str(key)):
            raise ValueError(f"credential field is forbidden: {key}")


class RunManifestV1(BaseModel):
    """Deterministic provenance manifest for one Found Money run."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["run-manifest.v1"] = "run-manifest.v1"
    run_id: str
    referenced_schema_versions: dict[str, str]
    started_at: datetime
    completed_at: datetime
    mode: RunMode
    source_receipt_paths: list[str]
    stages: dict[str, StageState]
    artifact_hashes: dict[str, str]
    source_set_hash: str

    @model_validator(mode="before")
    @classmethod
    def _reject_credentials_and_unknown(cls, value: Any) -> Any:
        if isinstance(value, Mapping):
            _reject_credential_keys(value)
        return value

    @field_validator("schema_version", mode="before")
    @classmethod
    def _schema_version(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("schema_version must be a string")
        return _normalize_schema_version(value)

    @field_validator("run_id", mode="before")
    @classmethod
    def _run_id(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("run_id must be a string")
        return _normalize_identifier(value)

    @field_validator("referenced_schema_versions", mode="before")
    @classmethod
    def _schema_versions(cls, value: Any) -> dict[str, str]:
        if not isinstance(value, Mapping):
            raise ValueError("referenced_schema_versions must be an object")
        out: dict[str, str] = {}
        for key, raw in value.items():
            if not isinstance(key, str) or not isinstance(raw, str):
                raise ValueError("referenced_schema_versions entries must be strings")
            out[_normalize_identifier(key)] = _normalize_identifier(raw)
        return out

    @field_validator("started_at", "completed_at", mode="before")
    @classmethod
    def _timestamps(cls, value: Any) -> datetime:
        return _parse_utc(value)

    @field_validator("source_receipt_paths", mode="before")
    @classmethod
    def _receipt_paths(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            raise ValueError("source_receipt_paths must be a list")
        return [_normalize_relative_path(item) for item in value]

    @field_validator("stages", mode="before")
    @classmethod
    def _stages(cls, value: Any) -> dict[str, str]:
        if not isinstance(value, Mapping):
            raise ValueError("stages must be an object")
        allowed = {"not_started", "completed", "failed"}
        out: dict[str, str] = {}
        for key, raw in value.items():
            if not isinstance(key, str) or not isinstance(raw, str):
                raise ValueError("stage entries must be strings")
            state = raw.strip()
            if state not in allowed:
                raise ValueError(f"stage state must be one of {sorted(allowed)}")
            out[_normalize_identifier(key)] = state
        return out

    @field_validator("artifact_hashes", mode="before")
    @classmethod
    def _artifact_hashes(cls, value: Any) -> dict[str, str]:
        if not isinstance(value, Mapping):
            raise ValueError("artifact_hashes must be an object")
        out: dict[str, str] = {}
        for key, raw in value.items():
            if not isinstance(key, str) or not isinstance(raw, str):
                raise ValueError("artifact_hashes entries must be strings")
            out[_normalize_relative_path(key)] = _normalize_hash(raw)
        return out

    @field_validator("source_set_hash", mode="before")
    @classmethod
    def _source_set_hash(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("source_set_hash must be a string")
        return _normalize_hash(value)

    @model_validator(mode="after")
    def _ordering(self) -> Self:
        if self.completed_at < self.started_at:
            raise ValueError("completed_at must not be before started_at")
        return self

    def canonical_dict(self) -> dict[str, Any]:
        payload = self.model_dump(mode="python")
        for key in ("started_at", "completed_at"):
            value = payload[key]
            assert isinstance(value, datetime)
            payload[key] = (
                value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
            )
        # Stable nested object key order is handled by json.dumps(sort_keys=True).
        return payload

    def to_canonical_json(self) -> bytes:
        text = json.dumps(
            self.canonical_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return (text + "\n").encode("utf-8")


def compute_source_set_hash(receipt_path_and_hash: Mapping[str, str]) -> str:
    """SHA-256 over the canonical sorted sequence of receipt path and content hash."""
    import hashlib

    lines: list[str] = []
    for path in sorted(receipt_path_and_hash):
        digest = _normalize_hash(receipt_path_and_hash[path])
        rel = _normalize_relative_path(path)
        lines.append(f"{rel}:{digest}")
    payload = "\n".join(lines) + ("\n" if lines else "")
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
