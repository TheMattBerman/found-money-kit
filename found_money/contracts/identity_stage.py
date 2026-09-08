"""Identity-stage evidence contract (schema_version=identity-stage.v1)."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Literal, Mapping, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from found_money.contracts.identity import MatchRule, _format_utc, _parse_utc

IDENTITY_STAGE_SCHEMA = "identity-stage.v1"
ALLOWED_MATCH_RULES: tuple[MatchRule, ...] = (
    "same_source_object_id",
    "declared_external_id",
    "unique_exact_email",
    "unique_exact_e164_phone",
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_TRAVERSAL_RE = re.compile(r"(^|/)\.\.(/|$)")
_WINDOWS_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")


def _identifier(value: Any, *, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    text = value.strip()
    if not text:
        raise ValueError(f"{label} must be non-empty")
    return text


def _source_id(value: Any) -> str:
    text = _identifier(value, label="source_id")
    if not _SOURCE_ID_RE.fullmatch(text):
        raise ValueError("source_id must be a simple non-empty identifier")
    return text


def _hash(value: Any, *, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    text = value.strip().lower()
    if not _SHA256_RE.fullmatch(text):
        raise ValueError(f"{label} must be a lowercase 64-char SHA-256 hex digest")
    return text


def _relative_path(value: Any, *, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    text = value.strip().replace("\\", "/")
    if text.startswith("./"):
        text = text[2:]
    if (
        not text
        or text == "."
        or text == ".."
        or text.startswith("/")
        or text.startswith("~/")
        or _WINDOWS_ABS_RE.match(text)
        or _TRAVERSAL_RE.search(text)
    ):
        raise ValueError(f"{label} must be a traversal-free relative path")
    return text


def _non_negative_int(value: Any, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


class IdentitySourceReferenceV1(BaseModel):
    """Public-safe pointer from identity nodes back to one source-set receipt."""

    model_config = ConfigDict(extra="forbid")

    declared_source: str
    source_type: str
    receipt_path: str
    content_hash: str
    identity_node_count: int = Field(ge=0)

    @field_validator("declared_source", mode="before")
    @classmethod
    def _declared_source(cls, value: Any) -> str:
        return _source_id(value)

    @field_validator("source_type", mode="before")
    @classmethod
    def _source_type(cls, value: Any) -> str:
        return _identifier(value, label="source_type")

    @field_validator("receipt_path", mode="before")
    @classmethod
    def _receipt_path(cls, value: Any) -> str:
        return _relative_path(value, label="receipt_path")

    @field_validator("content_hash", mode="before")
    @classmethod
    def _content_hash(cls, value: Any) -> str:
        return _hash(value, label="content_hash")

    @field_validator("identity_node_count", mode="before")
    @classmethod
    def _count(cls, value: Any) -> int:
        return _non_negative_int(value, label="identity_node_count")


class IdentityStageEvidenceV1(BaseModel):
    """Canonical, public-safe identity-stage receipt for unified build artifacts."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["identity-stage.v1"] = "identity-stage.v1"
    run_id: str
    built_at: datetime
    source_set_hash: str
    private_graph_path: str
    private_graph_sha256: str
    public_projection_path: str
    public_projection_sha256: str
    resolved_customer_count: int = Field(ge=0)
    ambiguous_cluster_count: int = Field(ge=0)
    quarantined_member_count: int = Field(ge=0)
    match_rule_counts: dict[str, int]
    source_references: list[IdentitySourceReferenceV1]

    @field_validator("schema_version", mode="before")
    @classmethod
    def _schema(cls, value: Any) -> str:
        if not isinstance(value, str) or value.strip() != IDENTITY_STAGE_SCHEMA:
            raise ValueError(f'schema_version must be exactly "{IDENTITY_STAGE_SCHEMA}"')
        return IDENTITY_STAGE_SCHEMA

    @field_validator("run_id", mode="before")
    @classmethod
    def _run_id(cls, value: Any) -> str:
        return _identifier(value, label="run_id")

    @field_validator("built_at", mode="before")
    @classmethod
    def _built_at(cls, value: Any) -> datetime:
        return _parse_utc(value)

    @field_validator(
        "source_set_hash", "private_graph_sha256", "public_projection_sha256", mode="before"
    )
    @classmethod
    def _hashes(cls, value: Any) -> str:
        return _hash(value, label="hash")

    @field_validator("private_graph_path", "public_projection_path", mode="before")
    @classmethod
    def _paths(cls, value: Any) -> str:
        return _relative_path(value, label="identity artifact path")

    @field_validator(
        "resolved_customer_count",
        "ambiguous_cluster_count",
        "quarantined_member_count",
        mode="before",
    )
    @classmethod
    def _counts(cls, value: Any) -> int:
        return _non_negative_int(value, label="identity count")

    @field_validator("match_rule_counts", mode="before")
    @classmethod
    def _rules(cls, value: Any) -> dict[str, int]:
        if not isinstance(value, Mapping):
            raise ValueError("match_rule_counts must be an object")
        out: dict[str, int] = {}
        for key, raw in value.items():
            if not isinstance(key, str):
                raise ValueError("match_rule_counts keys must be strings")
            rule = key.strip()
            if rule not in ALLOWED_MATCH_RULES:
                raise ValueError(f"unsupported identity match rule: {rule}")
            out[rule] = _non_negative_int(raw, label="match_rule_counts value")
        return {rule: out.get(rule, 0) for rule in ALLOWED_MATCH_RULES}

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        ids = [entry.declared_source for entry in self.source_references]
        if len(ids) != len(set(ids)):
            raise ValueError("identity source references must be unique")
        return self

    def canonical_dict(self) -> dict[str, Any]:
        payload = self.model_dump(mode="python")
        payload["built_at"] = _format_utc(payload["built_at"])
        payload["source_references"] = sorted(
            payload["source_references"], key=lambda item: item["declared_source"]
        )
        payload["match_rule_counts"] = {
            rule: int(self.match_rule_counts.get(rule, 0)) for rule in ALLOWED_MATCH_RULES
        }
        return payload

    def to_canonical_json(self) -> bytes:
        return (
            json.dumps(
                self.canonical_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            + "\n"
        ).encode("utf-8")


def _duplicate_key_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def parse_identity_stage_evidence(
    data: bytes | bytearray | Mapping[str, Any],
) -> IdentityStageEvidenceV1:
    """Parse canonical identity-stage evidence and reject malformed bytes."""
    if isinstance(data, Mapping):
        payload: Any = dict(data)
        encoded: bytes | None = None
    elif isinstance(data, (bytes, bytearray)):
        encoded = bytes(data)
        try:
            payload = json.loads(encoded.decode("utf-8"), object_pairs_hook=_duplicate_key_object)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"identity-stage evidence is malformed JSON: {exc}") from exc
    else:
        raise TypeError("identity-stage evidence must be JSON bytes or an object")
    if not isinstance(payload, Mapping):
        raise ValueError("identity-stage evidence root must be an object")
    model = IdentityStageEvidenceV1.model_validate(payload)
    if encoded is not None and model.to_canonical_json() != encoded:
        raise ValueError("serialized bytes are not exactly canonical")
    return model


__all__ = [
    "ALLOWED_MATCH_RULES",
    "IDENTITY_STAGE_SCHEMA",
    "IdentitySourceReferenceV1",
    "IdentityStageEvidenceV1",
    "parse_identity_stage_evidence",
]
