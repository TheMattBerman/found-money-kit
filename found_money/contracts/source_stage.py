"""Versioned manifest and source-set contracts for FM-021.

The manifest is configuration, not a data export.  It names caller-owned
relative inputs and the schema each adapter must produce; credentials and
provider URLs are deliberately not part of the contract.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, Literal, Self

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from found_money.contracts.run import compute_source_set_hash

SOURCE_MANIFEST_SCHEMA = "found-money-source-manifest.v1"
LEGACY_SOURCE_CONFIG_SCHEMA = "found-money-build-source.v1"
SOURCE_SET_SCHEMA = "source-set.v1"

FILE_SOURCE_SCHEMAS = frozenset({"orders.v1", "appointments.v1", "proposals.v1"})
NATIVE_SOURCE_SCHEMAS = {
    "hubspot": "hubspot-crm.2026-03.v1",
    "stripe": "stripe.2026-02-25.clover.v1",
}
FILE_SOURCE_TYPES = frozenset({"orders", "appointments", "proposals", "csv", "json"})

_SOURCE_TYPE_ALIASES = {
    "universal-csv": "csv",
    "universal_csv": "csv",
    "universal-json": "json",
    "universal_json": "json",
}
_SOURCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TRAVERSAL_RE = re.compile(r"(^|/)\.\.(/|$)")
_WINDOWS_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")
_SENSITIVE_KEY_RE = re.compile(
    r"(?i)^(?:password|passwd|secret|token|api[_-]?key|access[_-]?key|"
    r"private[_-]?key|authorization|credential|credentials|auth|bearer|client[_-]?secret)$"
)


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
        or "\x00" in text
        or "?" in text
        or "#" in text
        or "://" in text
    ):
        raise ValueError(f"{label} must be a traversal-free relative path")
    return text


def _identifier(value: Any, *, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    text = value.strip()
    if not _SOURCE_ID_RE.fullmatch(text):
        raise ValueError(f"{label} must be a simple non-empty identifier")
    return text


def _safe_option_value(value: Any, *, label: str) -> Any:
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, nested in value.items():
            if not isinstance(key, str) or _SENSITIVE_KEY_RE.fullmatch(key):
                raise ValueError(f"{label} contains a forbidden credential field")
            normalized[key] = _safe_option_value(nested, label=f"{label}.{key}")
        return normalized
    if isinstance(value, list):
        return [_safe_option_value(item, label=label) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        if isinstance(value, str) and (
            "\x00" in value or "://" in value or _TRAVERSAL_RE.search(value)
        ):
            raise ValueError(f"{label} contains an unsafe path or URL")
        return value
    raise ValueError(f"{label} must contain only JSON-compatible values")


class SourceDeclarationV1(BaseModel):
    """One source selected by a :class:`SourceManifestV1`."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    source_id: str = Field(validation_alias=AliasChoices("source_id", "id"))
    source_type: str = Field(validation_alias=AliasChoices("source_type", "type"))
    schema_version: str
    path: str | None = Field(default=None, validation_alias=AliasChoices("path", "input_path"))
    format: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)

    @field_validator("source_id", mode="before")
    @classmethod
    def _source_id(cls, value: Any) -> str:
        return _identifier(value, label="source_id")

    @field_validator("source_type", mode="before")
    @classmethod
    def _source_type(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("source_type must be a string")
        normalized = value.strip().lower().replace(" ", "-")
        return _SOURCE_TYPE_ALIASES.get(normalized, normalized)

    @field_validator("schema_version", mode="before")
    @classmethod
    def _schema_version(cls, value: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("schema_version must be a non-empty string")
        return value.strip()

    @field_validator("path", mode="before")
    @classmethod
    def _path(cls, value: Any) -> str | None:
        if value is None:
            return None
        return _relative_path(value, label="source path")

    @field_validator("format", mode="before")
    @classmethod
    def _format(cls, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or value.strip().lower() not in {"csv", "json"}:
            raise ValueError("source format must be csv or json")
        return value.strip().lower()

    @field_validator("options", mode="before")
    @classmethod
    def _options(cls, value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            raise ValueError("source options must be an object")
        normalized = _safe_option_value(value, label="source options")
        assert isinstance(normalized, dict)
        return normalized

    @model_validator(mode="after")
    def _contract(self) -> Self:
        if self.source_type in NATIVE_SOURCE_SCHEMAS:
            if self.schema_version != NATIVE_SOURCE_SCHEMAS[self.source_type]:
                raise ValueError(
                    f"{self.source_type} requires schema {NATIVE_SOURCE_SCHEMAS[self.source_type]}"
                )
            if self.path is not None or self.format is not None:
                raise ValueError(f"{self.source_type} sources do not accept file paths")
            return self

        if self.source_type not in FILE_SOURCE_TYPES:
            raise ValueError(f"unsupported source type: {self.source_type}")
        if self.schema_version not in FILE_SOURCE_SCHEMAS:
            raise ValueError(
                "file source schema must be orders.v1, appointments.v1, or proposals.v1"
            )
        if self.path is None:
            raise ValueError(f"{self.source_type} source path is required")
        suffix = self.path.rsplit(".", 1)[-1].lower() if "." in self.path else ""
        if suffix not in {"csv", "json"}:
            raise ValueError("file source path must end in .csv or .json")
        if self.format is not None and self.format != suffix:
            raise ValueError("source format does not match source path extension")
        if self.source_type in {"csv", "json"} and self.source_type != suffix:
            raise ValueError(f"{self.source_type} source requires a .{self.source_type} path")
        return self

    @property
    def effective_source_type(self) -> str:
        if self.source_type in {"csv", "json"}:
            return self.schema_version.split(".", 1)[0]
        return self.source_type

    def canonical_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "source_id": self.source_id,
            "source_type": self.source_type,
            "schema_version": self.schema_version,
        }
        if self.path is not None:
            payload["path"] = self.path
        if self.format is not None:
            payload["format"] = self.format
        if self.options:
            payload["options"] = self.options
        return payload


class SourceManifestV1(BaseModel):
    """Complete, public-safe source selection for one source stage."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = SOURCE_MANIFEST_SCHEMA
    source_root: str | None = None
    sources: list[SourceDeclarationV1]

    @field_validator("schema_version", mode="before")
    @classmethod
    def _schema(cls, value: Any) -> str:
        if not isinstance(value, str) or value not in {
            SOURCE_MANIFEST_SCHEMA,
            LEGACY_SOURCE_CONFIG_SCHEMA,
        }:
            raise ValueError(f"schema_version must be {SOURCE_MANIFEST_SCHEMA}")
        return value

    @field_validator("source_root", mode="before")
    @classmethod
    def _source_root(cls, value: Any) -> str | None:
        if value is None:
            return None
        return _relative_path(value, label="source_root")

    @model_validator(mode="after")
    def _unique_sources(self) -> Self:
        if not self.sources:
            raise ValueError("sources must contain at least one declaration")
        ids = [source.source_id for source in self.sources]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate source_id declarations are not allowed")
        paths = [source.path for source in self.sources if source.path is not None]
        if len(paths) != len(set(paths)):
            raise ValueError("duplicate source path declarations are not allowed")
        return self

    def canonical_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"schema_version": self.schema_version}
        if self.source_root is not None:
            payload["source_root"] = self.source_root
        payload["sources"] = [
            source.canonical_dict()
            for source in sorted(self.sources, key=lambda item: item.source_id)
        ]
        return payload

    def to_canonical_json(self) -> bytes:
        return (
            json.dumps(
                self.canonical_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            + "\n"
        ).encode("utf-8")


class SourceSetEntryV1(BaseModel):
    """Safe metadata for one successfully normalized source."""

    model_config = ConfigDict(extra="forbid")

    source_id: str
    source_type: str
    connector_schema_version: str
    normalized_path: str
    receipt_path: str
    content_hash: str
    page_or_row_count: int
    record_count: int

    @field_validator("source_id", mode="before")
    @classmethod
    def _entry_id(cls, value: Any) -> str:
        return _identifier(value, label="source_id")

    @field_validator("source_type", "connector_schema_version", mode="before")
    @classmethod
    def _entry_identifier(cls, value: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("source-set identifiers must be non-empty strings")
        return value.strip()

    @field_validator("normalized_path", "receipt_path", mode="before")
    @classmethod
    def _entry_path(cls, value: Any) -> str:
        return _relative_path(value, label="source-set path")

    @field_validator("content_hash", mode="before")
    @classmethod
    def _entry_hash(cls, value: Any) -> str:
        if not isinstance(value, str) or not _SHA256_RE.fullmatch(value.strip().lower()):
            raise ValueError("source-set content_hash must be a SHA-256 hex digest")
        return value.strip().lower()

    @field_validator("page_or_row_count", "record_count", mode="before")
    @classmethod
    def _entry_count(cls, value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("source-set counts must be non-negative integers")
        return value


class SourceSetManifestV1(BaseModel):
    """Deterministic run-level source receipt index."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["source-set.v1"] = "source-set.v1"
    source_set_hash: str
    sources: list[SourceSetEntryV1]

    @field_validator("source_set_hash", mode="before")
    @classmethod
    def _hash(cls, value: Any) -> str:
        if not isinstance(value, str) or not _SHA256_RE.fullmatch(value.strip().lower()):
            raise ValueError("source_set_hash must be a SHA-256 hex digest")
        return value.strip().lower()

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if not self.sources:
            raise ValueError("source-set must contain at least one source")
        ids = [entry.source_id for entry in self.sources]
        if len(ids) != len(set(ids)):
            raise ValueError("source-set source IDs must be unique")
        receipt_paths = {entry.receipt_path: entry.content_hash for entry in self.sources}
        expected = compute_source_set_hash(receipt_paths)
        if expected != self.source_set_hash:
            raise ValueError("source_set_hash does not match source receipts")
        return self

    def canonical_dict(self) -> dict[str, Any]:
        payload = self.model_dump(mode="python")
        payload["sources"] = sorted(payload["sources"], key=lambda item: item["source_id"])
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


def parse_source_manifest(data: bytes | bytearray | Mapping[str, Any]) -> SourceManifestV1:
    """Parse a manifest and reject malformed or duplicate JSON before execution."""
    if isinstance(data, Mapping):
        payload: Any = dict(data)
    elif isinstance(data, (bytes, bytearray)):
        try:
            payload = json.loads(
                bytes(data).decode("utf-8"), object_pairs_hook=_duplicate_key_object
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"source manifest is malformed JSON: {exc}") from exc
    else:
        raise TypeError("source manifest must be JSON bytes or an object")
    if not isinstance(payload, Mapping):
        raise ValueError("source manifest root must be an object")
    try:
        return SourceManifestV1.model_validate(payload)
    except ValueError as exc:
        raise ValueError(f"source manifest is invalid: {exc}") from exc


__all__ = [
    "FILE_SOURCE_SCHEMAS",
    "LEGACY_SOURCE_CONFIG_SCHEMA",
    "NATIVE_SOURCE_SCHEMAS",
    "SOURCE_MANIFEST_SCHEMA",
    "SOURCE_SET_SCHEMA",
    "SourceDeclarationV1",
    "SourceManifestV1",
    "SourceSetEntryV1",
    "SourceSetManifestV1",
    "parse_source_manifest",
]
