"""Identity graph contract (schema_version=identity-graph.v1)."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Literal, Sequence, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MatchRule = Literal[
    "same_source_object_id",
    "declared_external_id",
    "unique_exact_email",
    "unique_exact_e164_phone",
]

AmbiguousReason = Literal[
    "household_email",
    "recycled_phone",
    "conflicting_stronger_identifier",
]

OverrideResolution = Literal["merge_members"]

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


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


def normalize_email(value: str) -> str:
    text = value.strip().lower()
    if not text or not _EMAIL_RE.fullmatch(text):
        raise ValueError("email must be a normalized exact address")
    return text


def normalize_e164(value: str) -> str:
    text = value.strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    if text.startswith("00"):
        text = "+" + text[2:]
    if not _E164_RE.fullmatch(text):
        raise ValueError("phone must be unique exact E.164")
    return text


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _canonical_json_bytes(payload: Any) -> bytes:
    text = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return (text + "\n").encode("utf-8")


def ambiguous_cluster_hash(member_node_ids: Sequence[str], reason: str) -> str:
    """Lowercase SHA-256 of canonical {member_node_ids, reason}."""
    members = sorted({_normalize_identifier(item) for item in member_node_ids})
    if not members:
        raise ValueError("member_node_ids must be a non-empty list")
    payload = {"member_node_ids": members, "reason": reason}
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def override_envelope_integrity(payload: dict[str, Any]) -> str:
    """Lowercase SHA-256 of canonical envelope with integrity omitted."""
    body = {key: value for key, value in payload.items() if key != "integrity"}
    return hashlib.sha256(_canonical_json_bytes(body)).hexdigest()


class IdentityNodeV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str
    source_system: str
    object_type: str
    source_id: str
    observed_at: datetime
    payload_hash: str
    email: str | None = None
    phone: str | None = None
    external_ids: dict[str, str] = Field(default_factory=dict)
    display_name: str | None = None

    @field_validator("node_id", "source_system", "object_type", "source_id", mode="before")
    @classmethod
    def _ids(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("value must be a string")
        return _normalize_identifier(value)

    @field_validator("observed_at", mode="before")
    @classmethod
    def _observed(cls, value: Any) -> datetime:
        return _parse_utc(value)

    @field_validator("payload_hash", mode="before")
    @classmethod
    def _hash(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("payload_hash must be a string")
        return _normalize_hash(value)

    @field_validator("email", mode="before")
    @classmethod
    def _email(cls, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("email must be a string or null")
        return normalize_email(value)

    @field_validator("phone", mode="before")
    @classmethod
    def _phone(cls, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("phone must be a string or null")
        return normalize_e164(value)

    @field_validator("external_ids", mode="before")
    @classmethod
    def _external(cls, value: Any) -> dict[str, str]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("external_ids must be an object")
        out: dict[str, str] = {}
        for key, raw in value.items():
            if not isinstance(key, str) or not isinstance(raw, str):
                raise ValueError("external_ids entries must be strings")
            out[_normalize_identifier(key)] = _normalize_identifier(raw)
        return out

    @field_validator("display_name", mode="before")
    @classmethod
    def _display(cls, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("display_name must be a string or null")
        text = value.strip()
        return text or None


class IdentityEdgeV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    left_node_id: str
    right_node_id: str
    match_rule: MatchRule
    match_namespace: str | None = None
    match_value: str
    lineage: dict[str, str]

    @field_validator("left_node_id", "right_node_id", "match_value", mode="before")
    @classmethod
    def _req(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("value must be a string")
        return _normalize_identifier(value)

    @field_validator("match_namespace", mode="before")
    @classmethod
    def _ns(cls, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("match_namespace must be a string or null")
        return _normalize_identifier(value)

    @field_validator("lineage", mode="before")
    @classmethod
    def _lineage(cls, value: Any) -> dict[str, str]:
        if not isinstance(value, dict) or not value:
            raise ValueError("lineage must be a non-empty object")
        out: dict[str, str] = {}
        for key, raw in value.items():
            if not isinstance(key, str) or not isinstance(raw, str):
                raise ValueError("lineage entries must be strings")
            out[_normalize_identifier(key)] = _normalize_identifier(raw)
        return out

    @model_validator(mode="after")
    def _order(self) -> Self:
        if self.left_node_id == self.right_node_id:
            raise ValueError("edge endpoints must differ")
        left, right = sorted((self.left_node_id, self.right_node_id))
        if (left, right) != (self.left_node_id, self.right_node_id):
            return self.model_copy(update={"left_node_id": left, "right_node_id": right})
        return self


class CustomerClusterV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_token: str
    member_node_ids: list[str]

    @field_validator("customer_token", mode="before")
    @classmethod
    def _token(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("customer_token must be a string")
        return _normalize_identifier(value)

    @field_validator("member_node_ids", mode="before")
    @classmethod
    def _members(cls, value: Any) -> list[str]:
        if not isinstance(value, list) or not value:
            raise ValueError("member_node_ids must be a non-empty list")
        members = sorted({_normalize_identifier(item) for item in value})
        if len(members) != len(value):
            # allow input duplicates after normalization uniqueness
            pass
        return members


class AmbiguousIdentityClusterV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    member_node_ids: list[str]
    reason: AmbiguousReason
    cluster_hash: str

    @field_validator("member_node_ids", mode="before")
    @classmethod
    def _members(cls, value: Any) -> list[str]:
        if not isinstance(value, list) or not value:
            raise ValueError("member_node_ids must be a non-empty list")
        return sorted({_normalize_identifier(item) for item in value})

    @field_validator("reason", mode="before")
    @classmethod
    def _reason(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("reason must be a string")
        text = value.strip()
        allowed = {
            "household_email",
            "recycled_phone",
            "conflicting_stronger_identifier",
        }
        if text not in allowed:
            raise ValueError(
                "reason must be household_email, recycled_phone, or conflicting_stronger_identifier"
            )
        return text

    @field_validator("cluster_hash", mode="before")
    @classmethod
    def _hash(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("cluster_hash must be a string")
        return _normalize_hash(value)

    @model_validator(mode="after")
    def _hash_matches(self) -> Self:
        expected = ambiguous_cluster_hash(self.member_node_ids, self.reason)
        if self.cluster_hash != expected:
            raise ValueError("cluster_hash does not match canonical member_node_ids/reason")
        return self

    def canonical_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="python")

    def to_canonical_json(self) -> bytes:
        return _canonical_json_bytes(self.canonical_dict())


class IdentityOverrideEnvelopeV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["identity-override.v1"] = "identity-override.v1"
    target_cluster_hash: str
    member_node_ids: list[str]
    resolution: OverrideResolution
    decided_by: str
    decided_at: datetime
    integrity: str

    @field_validator("schema_version", mode="before")
    @classmethod
    def _schema(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("schema_version must be a string")
        text = value.strip()
        if text != "identity-override.v1":
            raise ValueError('schema_version must be exactly "identity-override.v1"')
        return text

    @field_validator("target_cluster_hash", "integrity", mode="before")
    @classmethod
    def _hashes(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("hash must be a string")
        return _normalize_hash(value)

    @field_validator("member_node_ids", mode="before")
    @classmethod
    def _members(cls, value: Any) -> list[str]:
        if not isinstance(value, list) or not value:
            raise ValueError("member_node_ids must be a non-empty list")
        return sorted({_normalize_identifier(item) for item in value})

    @field_validator("resolution", mode="before")
    @classmethod
    def _resolution(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("resolution must be a string")
        text = value.strip()
        if text != "merge_members":
            raise ValueError('resolution must be exactly "merge_members"')
        return text

    @field_validator("decided_by", mode="before")
    @classmethod
    def _decided_by(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("decided_by must be a string")
        return _normalize_identifier(value)

    @field_validator("decided_at", mode="before")
    @classmethod
    def _decided_at(cls, value: Any) -> datetime:
        return _parse_utc(value)

    @model_validator(mode="after")
    def _integrity_matches(self) -> Self:
        expected = override_envelope_integrity(self.canonical_dict())
        if self.integrity != expected:
            raise ValueError("integrity does not match canonical envelope body")
        return self

    def canonical_dict(self) -> dict[str, Any]:
        payload = self.model_dump(mode="python")
        payload["decided_at"] = _format_utc(payload["decided_at"])
        return payload

    def to_canonical_json(self) -> bytes:
        return _canonical_json_bytes(self.canonical_dict())


class IdentityGraphV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["identity-graph.v1"] = "identity-graph.v1"
    run_id: str
    built_at: datetime
    nodes: list[IdentityNodeV1]
    edges: list[IdentityEdgeV1]
    customers: list[CustomerClusterV1]
    ambiguous_identities: list[AmbiguousIdentityClusterV1] = Field(default_factory=list)

    @field_validator("schema_version", mode="before")
    @classmethod
    def _schema(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("schema_version must be a string")
        text = value.strip()
        if text != "identity-graph.v1":
            raise ValueError('schema_version must be exactly "identity-graph.v1"')
        return text

    @field_validator("run_id", mode="before")
    @classmethod
    def _run(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("run_id must be a string")
        return _normalize_identifier(value)

    @field_validator("built_at", mode="before")
    @classmethod
    def _built(cls, value: Any) -> datetime:
        return _parse_utc(value)

    @model_validator(mode="after")
    def _consistency(self) -> Self:
        node_ids = [node.node_id for node in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("node_id values must be unique")
        known = set(node_ids)
        for edge in self.edges:
            if edge.left_node_id not in known or edge.right_node_id not in known:
                raise ValueError("edge references unknown node_id")
        customer_members: set[str] = set()
        for customer in self.customers:
            members = set(customer.member_node_ids)
            if not members.issubset(known):
                raise ValueError("customer cluster references unknown node_id")
            overlap = customer_members & members
            if overlap:
                raise ValueError("customer clusters must be disjoint")
            customer_members.update(members)
        seen_hashes: set[str] = set()
        ambiguous_members: set[str] = set()
        for ambiguous in self.ambiguous_identities:
            members = set(ambiguous.member_node_ids)
            if not members.issubset(known):
                raise ValueError("ambiguous cluster references unknown node_id")
            if ambiguous.cluster_hash in seen_hashes:
                raise ValueError("ambiguous cluster_hash values must be unique")
            seen_hashes.add(ambiguous.cluster_hash)
            overlap = ambiguous_members & members
            if overlap:
                raise ValueError("ambiguous clusters must be disjoint")
            ambiguous_members.update(members)
        if customer_members & ambiguous_members:
            raise ValueError("customers and ambiguous_identities must be disjoint")
        return self

    def canonical_dict(self) -> dict[str, Any]:
        payload = self.model_dump(mode="python")
        payload["built_at"] = _format_utc(payload["built_at"])
        for node in payload["nodes"]:
            node["observed_at"] = _format_utc(node["observed_at"])
        return payload

    def to_canonical_json(self) -> bytes:
        return _canonical_json_bytes(self.canonical_dict())


class PublicCustomerProjectionV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_token: str
    member_count: int = Field(ge=1)


class PublicIdentityProjectionV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["identity-public.v1"] = "identity-public.v1"
    run_id: str
    built_at: datetime
    customer_count: int = Field(ge=0)
    customers: list[PublicCustomerProjectionV1]

    @field_validator("schema_version", mode="before")
    @classmethod
    def _schema(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("schema_version must be a string")
        text = value.strip()
        if text != "identity-public.v1":
            raise ValueError('schema_version must be exactly "identity-public.v1"')
        return text

    @field_validator("run_id", mode="before")
    @classmethod
    def _run(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("run_id must be a string")
        return _normalize_identifier(value)

    @field_validator("built_at", mode="before")
    @classmethod
    def _built(cls, value: Any) -> datetime:
        return _parse_utc(value)

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
