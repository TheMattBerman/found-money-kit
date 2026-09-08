"""FM-030 safety contracts: allowlist, audit, no-mutation, and release evidence."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Literal, Mapping, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
LiveStatus = Literal["fixture-only", "live-unverified", "live-verified"]
NetworkFamily = Literal["hubspot_read", "stripe_read", "model_request"]
AllowedMethod = Literal["GET", "POST"]


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


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _normalize_hash(value: str) -> str:
    text = value.strip().lower()
    if not _SHA256_RE.fullmatch(text):
        raise ValueError("hash must be a lowercase 64-char SHA-256 hex digest")
    return text


def _normalize_identifier(value: str) -> str:
    text = value.strip()
    if not text:
        raise ValueError("identifier must be non-empty after normalization")
    return text


class SafeRequestAuditRecordV1(BaseModel):
    """One network request as method/host/path only — never headers or payloads."""

    model_config = ConfigDict(extra="forbid")

    method: AllowedMethod
    host: str
    path: str
    family: NetworkFamily

    @field_validator("method", mode="before")
    @classmethod
    def _method(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("method must be a string")
        text = value.strip().upper()
        if text not in {"GET", "POST"}:
            raise ValueError("method must be GET or POST")
        return text

    @field_validator("host", "path", mode="before")
    @classmethod
    def _host_path(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("host/path must be a string")
        text = value.strip()
        if not text:
            raise ValueError("host/path must be non-empty")
        if any(ord(char) < 32 or ord(char) == 127 for char in text):
            raise ValueError("host/path contains control characters")
        lowered = text.casefold()
        for forbidden in (
            "authorization",
            "bearer ",
            "api_key",
            "api-key",
            "password",
            "secret",
            "token=",
        ):
            if forbidden in lowered:
                raise ValueError("audit record must not contain credential-shaped text")
        return text

    @field_validator("family", mode="before")
    @classmethod
    def _family(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("family must be a string")
        text = value.strip()
        if text not in {"hubspot_read", "stripe_read", "model_request"}:
            raise ValueError("family is not a known network family")
        return text


class SafeRequestAuditLogV1(BaseModel):
    """Ordered, redacted request audit for one bounded connector or model session."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["safe-request-audit.v1"] = "safe-request-audit.v1"
    allowlist_version: str
    allowlist_hash: str
    records: list[SafeRequestAuditRecordV1] = Field(default_factory=list)

    @field_validator("allowlist_version", mode="before")
    @classmethod
    def _version(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("allowlist_version must be a string")
        return _normalize_identifier(value)

    @field_validator("allowlist_hash", mode="before")
    @classmethod
    def _hash(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("allowlist_hash must be a string")
        return _normalize_hash(value)

    @model_validator(mode="after")
    def _bound_to_allowlist(self) -> Self:
        from found_money.safety.allowlist import (
            ALLOWLIST_VERSION,
            allowlist_hash,
            classify_network_request,
        )

        if self.allowlist_version != ALLOWLIST_VERSION:
            raise ValueError("audit allowlist_version is not current")
        if self.allowlist_hash != allowlist_hash():
            raise ValueError("audit allowlist_hash is not current")
        for record in self.records:
            family = classify_network_request(record.method, record.host, record.path)
            if family != record.family:
                raise ValueError("audit family does not match method/host/path")
        return self

    def canonical_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="python")

    def to_canonical_json(self) -> bytes:
        text = json.dumps(
            self.canonical_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return (text + "\n").encode("utf-8")


class NoMutationAssertionV1(BaseModel):
    """Canonical no-mutation assertion attachable to run/source/model receipts."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["no-mutation-assertion.v1"] = "no-mutation-assertion.v1"
    no_mutation: Literal[True] = True
    allowlist_version: str
    allowlist_hash: str
    allowed_methods_summary: dict[str, int]
    request_count: int = Field(ge=0)
    scope_counts: dict[str, int] = Field(default_factory=dict)
    audit_digest: str
    attached_receipt_hashes: dict[str, str] = Field(default_factory=dict)
    live_status: LiveStatus
    built_at: datetime

    @field_validator("allowlist_version", mode="before")
    @classmethod
    def _version(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("allowlist_version must be a string")
        return _normalize_identifier(value)

    @field_validator("allowlist_hash", "audit_digest", mode="before")
    @classmethod
    def _hashes(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("hash must be a string")
        return _normalize_hash(value)

    @field_validator("allowed_methods_summary", "scope_counts", mode="before")
    @classmethod
    def _int_maps(cls, value: Any) -> dict[str, int]:
        if not isinstance(value, Mapping):
            raise ValueError("count maps must be objects")
        out: dict[str, int] = {}
        for key, raw in value.items():
            if not isinstance(key, str) or not isinstance(raw, int) or isinstance(raw, bool):
                raise ValueError("count map entries must be string -> non-bool int")
            if raw < 0:
                raise ValueError("counts must be non-negative")
            out[_normalize_identifier(key)] = raw
        return out

    @field_validator("attached_receipt_hashes", mode="before")
    @classmethod
    def _receipt_hashes(cls, value: Any) -> dict[str, str]:
        if not isinstance(value, Mapping):
            raise ValueError("attached_receipt_hashes must be an object")
        out: dict[str, str] = {}
        for key, raw in value.items():
            if not isinstance(key, str) or not isinstance(raw, str):
                raise ValueError("attached_receipt_hashes entries must be strings")
            out[_normalize_identifier(key)] = _normalize_hash(raw)
        return out

    @field_validator("live_status", mode="before")
    @classmethod
    def _live(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("live_status must be a string")
        text = value.strip()
        if text not in {"fixture-only", "live-unverified", "live-verified"}:
            raise ValueError("live_status is invalid")
        return text

    @field_validator("built_at", mode="before")
    @classmethod
    def _built(cls, value: Any) -> datetime:
        return _parse_utc(value)

    @model_validator(mode="after")
    def _consistency(self) -> Self:
        from found_money.safety.allowlist import ALLOWLIST_VERSION, allowlist_hash

        if self.no_mutation is not True:
            raise ValueError("no_mutation must be true")
        if self.allowlist_version != ALLOWLIST_VERSION:
            raise ValueError("assertion allowlist_version is not current")
        if self.allowlist_hash != allowlist_hash():
            raise ValueError("assertion allowlist_hash is not current")
        if self.request_count != sum(self.allowed_methods_summary.values()):
            raise ValueError("request_count must equal allowed_methods_summary total")
        if "POST" in self.allowed_methods_summary and "model_request" not in self.scope_counts:
            raise ValueError("POST requests require model_request scope evidence")
        if any(method not in {"GET", "POST"} for method in self.allowed_methods_summary):
            raise ValueError("allowed_methods_summary may only include GET or POST")
        if self.live_status != "fixture-only" and (
            self.request_count == 0 or not self.scope_counts
        ):
            raise ValueError("live assertions require request and redacted scope/count evidence")
        return self

    def canonical_dict(self) -> dict[str, Any]:
        payload = self.model_dump(mode="python")
        payload["built_at"] = _format_utc(payload["built_at"])
        return payload

    def to_canonical_json(self) -> bytes:
        text = json.dumps(
            self.canonical_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return (text + "\n").encode("utf-8")


class ReleaseSafetyEvidencePacketV1(BaseModel):
    """Deterministic package-wide safety proof; never claims a real-business run."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["release-safety-evidence.v1"] = "release-safety-evidence.v1"
    built_at: datetime
    commit_hash: str
    allowlist_version: str
    allowlist_hash: str
    implementation_hash: str
    source_tree_hash: str
    check_evidence: dict[str, str]
    check_hashes: dict[str, str]
    scan_hashes: dict[str, str]
    live_status: LiveStatus
    claims_real_business_run: Literal[False] = False
    claims_publication: Literal[False] = False
    no_mutation_assertion_digest: str | None = None
    linked_live_receipt_hashes: dict[str, str] = Field(default_factory=dict)

    @field_validator("built_at", mode="before")
    @classmethod
    def _built(cls, value: Any) -> datetime:
        return _parse_utc(value)

    @field_validator("commit_hash", mode="before")
    @classmethod
    def _commit(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("commit_hash must be a string")
        text = value.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", text):
            raise ValueError("commit_hash must be a 40- or 64-char hex digest")
        return text

    @field_validator(
        "allowlist_hash",
        "implementation_hash",
        "source_tree_hash",
        mode="before",
    )
    @classmethod
    def _required_hashes(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("hash must be a string")
        return _normalize_hash(value)

    @field_validator("no_mutation_assertion_digest", mode="before")
    @classmethod
    def _optional_digest(cls, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("digest must be a string or null")
        return _normalize_hash(value)

    @field_validator("allowlist_version", mode="before")
    @classmethod
    def _version(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("allowlist_version must be a string")
        return _normalize_identifier(value)

    @field_validator("check_hashes", "scan_hashes", "linked_live_receipt_hashes", mode="before")
    @classmethod
    def _hash_maps(cls, value: Any) -> dict[str, str]:
        if not isinstance(value, Mapping):
            raise ValueError("hash maps must be objects")
        out: dict[str, str] = {}
        for key, raw in value.items():
            if not isinstance(key, str) or not isinstance(raw, str):
                raise ValueError("hash map entries must be strings")
            out[_normalize_identifier(key)] = _normalize_hash(raw)
        return out

    @field_validator("check_evidence", mode="before")
    @classmethod
    def _check_evidence(cls, value: Any) -> dict[str, str]:
        if not isinstance(value, Mapping):
            raise ValueError("check_evidence must be an object")
        out: dict[str, str] = {}
        for key, raw in value.items():
            if not isinstance(key, str) or not isinstance(raw, str) or not raw.strip():
                raise ValueError("check_evidence entries must be non-blank strings")
            out[_normalize_identifier(key)] = raw
        return out

    @field_validator("live_status", mode="before")
    @classmethod
    def _live(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("live_status must be a string")
        text = value.strip()
        if text not in {"fixture-only", "live-unverified", "live-verified"}:
            raise ValueError("live_status is invalid")
        return text

    @model_validator(mode="after")
    def _honest_claims(self) -> Self:
        if self.claims_real_business_run is not False:
            raise ValueError("packet must not claim a real-business run")
        if self.claims_publication is not False:
            raise ValueError("packet must not claim publication")
        if self.live_status == "live-verified":
            raise ValueError(
                "release packet cannot mark live-verified without separate human L evidence"
            )
        if self.live_status == "live-unverified" and not self.linked_live_receipt_hashes:
            raise ValueError("live-unverified packets require linked live receipt hashes")
        if self.live_status == "fixture-only" and self.linked_live_receipt_hashes:
            raise ValueError("fixture-only packets cannot link live receipt hashes")
        expected = {
            key: hashlib.sha256(value.encode("utf-8")).hexdigest()
            for key, value in self.check_evidence.items()
        }
        if self.check_hashes != expected:
            raise ValueError("check_hashes must be derived from exact check_evidence")
        return self

    def canonical_dict(self) -> dict[str, Any]:
        payload = self.model_dump(mode="python")
        payload["built_at"] = _format_utc(payload["built_at"])
        return payload

    def to_canonical_json(self) -> bytes:
        text = json.dumps(
            self.canonical_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return (text + "\n").encode("utf-8")
