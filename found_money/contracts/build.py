"""Unified build manifest contract (schema_version=found-money-build.v1)."""

from __future__ import annotations

import json
import re
from typing import Any, Literal, Mapping, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from found_money.contracts.run import RunMode, StageState

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TRAVERSAL_RE = re.compile(r"(^|/)\.\.(/|$)")
_WINDOWS_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")
_SENSITIVE_KEY_RE = re.compile(
    r"(?i)^(password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|"
    r"authorization|credential|credentials|auth|bearer|client[_-]?secret)$"
)
_ABS_PATH_RE = re.compile(r"(?i)(^|[\\s\"'])(/|[A-Za-z]:[\\/])")


def _object_pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate JSON object key: {key}")
        out[key] = value
    return out


def _normalize_identifier(value: str) -> str:
    text = value.strip()
    if not text:
        raise ValueError("identifier must be non-empty")
    return text


def _normalize_hash(value: str) -> str:
    text = value.strip().lower()
    if not _SHA256_RE.fullmatch(text):
        raise ValueError("hash must be a lowercase 64-char SHA-256 hex digest")
    return text


def _normalize_relative_path(value: str) -> str:
    text = value.strip().replace("\\", "/")
    if text.startswith("./"):
        text = text[2:]
    if (
        not text
        or text == "."
        or text.startswith("/")
        or text.startswith("~/")
        or text.endswith("/")
        or _WINDOWS_ABS_RE.match(text)
        or text == ".."
        or _TRAVERSAL_RE.search(text)
    ):
        raise ValueError("path must be relative and traversal-free")
    return text


def _reject_sensitive_config_keys(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if _SENSITIVE_KEY_RE.fullmatch(str(key)):
                raise ValueError("source_config must not contain credential fields")
            _reject_sensitive_config_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_sensitive_config_keys(nested)


def _validate_public_source_config(value: Mapping[str, Any]) -> dict[str, Any]:
    _reject_sensitive_config_keys(value)
    try:
        text = json.dumps(value, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValueError("source_config must contain JSON-compatible values") from exc
    lowered = text.lower()
    forbidden_fragments = ("secret", "api_key", "password", "://")
    if (
        any(item in lowered for item in forbidden_fragments)
        or _ABS_PATH_RE.search(text)
        or _TRAVERSAL_RE.search(text)
    ):
        raise ValueError("source_config must be public-safe")
    mode = value.get("mode")
    source_mode = value.get("source_mode")
    run_mode = value.get("run_mode")
    if isinstance(mode, str) and mode in {"fixture", "file"}:
        if source_mode is not None and source_mode != mode:
            raise ValueError("source_config source modes are inconsistent")
    elif isinstance(mode, str) and mode in {"public", "private"}:
        if not isinstance(source_mode, str) or source_mode not in {"fixture", "file"}:
            raise ValueError("source_config source_mode is required")
        if run_mode is not None and run_mode != mode:
            raise ValueError("source_config public/private modes are inconsistent")
    elif mode is not None:
        raise ValueError("source_config mode is unsupported")
    if run_mode is not None and (
        not isinstance(run_mode, str) or run_mode not in {"public", "private"}
    ):
        raise ValueError("source_config run_mode is unsupported")
    strategy_provider = value.get("strategy_provider")
    if strategy_provider is not None and strategy_provider not in {"stub", "skill", "fixture"}:
        raise ValueError("source_config strategy_provider is unsupported")
    return dict(value)


class BuildArtifactV1(BaseModel):
    """One finalized public build artifact."""

    model_config = ConfigDict(extra="forbid")

    path: str
    sha256: str

    @field_validator("path", mode="before")
    @classmethod
    def _path(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("artifact path must be a string")
        return _normalize_relative_path(value)

    @field_validator("sha256", mode="before")
    @classmethod
    def _sha(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("artifact hash must be a string")
        return _normalize_hash(value)


class BuildDeferredStageV1(BaseModel):
    """Explicit future scope that is intentionally not started in this issue."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["not_started"] = "not_started"
    reason: str
    depends_on_future_issue: bool = True

    @field_validator("reason", mode="before")
    @classmethod
    def _reason(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("deferred reason must be a string")
        return _normalize_identifier(value)


class BuildRunManifestV1(BaseModel):
    """Public-safe manifest for the caller-owned V1 artifact tree."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["found-money-build.v1"] = "found-money-build.v1"
    run_id: str
    mode: RunMode = "public"
    source_config: dict[str, Any]
    stages: dict[str, StageState]
    deferred_stages: dict[str, BuildDeferredStageV1] = Field(default_factory=dict)
    source_receipt_paths: list[str]
    source_hashes: dict[str, str]
    source_set_hash: str
    artifacts: list[BuildArtifactV1]

    @field_validator("schema_version", mode="before")
    @classmethod
    def _schema(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("schema_version must be a string")
        text = value.strip()
        if text != "found-money-build.v1":
            raise ValueError('schema_version must be exactly "found-money-build.v1"')
        return text

    @field_validator("run_id", mode="before")
    @classmethod
    def _run(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("run_id must be a string")
        return _normalize_identifier(value)

    @field_validator("source_config", mode="before")
    @classmethod
    def _source_config(cls, value: Any) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise ValueError("source_config must be an object")
        return _validate_public_source_config(value)

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
                raise ValueError("stage state must be not_started, completed, or failed")
            out[_normalize_identifier(key)] = state
        return out

    @field_validator("source_receipt_paths", mode="before")
    @classmethod
    def _receipt_paths(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            raise ValueError("source_receipt_paths must be a list")
        return [_normalize_relative_path(item) for item in value]

    @field_validator("source_hashes", mode="before")
    @classmethod
    def _source_hashes(cls, value: Any) -> dict[str, str]:
        if not isinstance(value, Mapping):
            raise ValueError("source_hashes must be an object")
        out: dict[str, str] = {}
        for key, raw in value.items():
            if not isinstance(key, str) or not isinstance(raw, str):
                raise ValueError("source_hashes entries must be strings")
            source_key = _normalize_identifier(key)
            if "/" in source_key or "\\" in source_key or source_key in {".", ".."}:
                raise ValueError("source hash keys must be simple identifiers")
            out[source_key] = _normalize_hash(raw)
        return out

    @field_validator("source_set_hash", mode="before")
    @classmethod
    def _source_set_hash(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("source_set_hash must be a string")
        return _normalize_hash(value)

    @model_validator(mode="after")
    def _artifact_paths_unique(self) -> Self:
        paths = [artifact.path for artifact in self.artifacts]
        if len(paths) != len(set(paths)):
            raise ValueError("artifact paths must be unique")
        return self

    def canonical_dict(self) -> dict[str, Any]:
        payload = self.model_dump(mode="python")
        payload["artifacts"] = sorted(payload["artifacts"], key=lambda item: item["path"])
        return payload

    def to_canonical_json(self) -> bytes:
        text = json.dumps(
            self.canonical_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return (text + "\n").encode("utf-8")


def parse_canonical_json(data: bytes) -> BuildRunManifestV1:
    """Parse only canonical ``found-money-build.v1`` manifest bytes."""
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("canonical JSON must be bytes")
    try:
        payload = json.loads(data.decode("utf-8"), object_pairs_hook=_object_pairs_hook)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"malformed JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("canonical JSON root must be an object")
    manifest = BuildRunManifestV1.model_validate(payload)
    canonical = manifest.to_canonical_json()
    if canonical != bytes(data):
        raise ValueError("serialized bytes are not exactly canonical")
    return manifest
