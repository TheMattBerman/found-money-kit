"""FM-036 private-run config, receipt, aggregate, and operator-packet contracts."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal, Mapping, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from found_money.contracts.map import CurrencyBasisCountsV1
from found_money.contracts.value import normalize_currency, normalize_minor_units

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_TRAVERSAL_RE = re.compile(r"(^|/)\.\.(/|$)")
ALLOWED_SYSTEMS = ("hubspot", "stripe")
REQUIRED_ENV_BINDINGS = {
    "hubspot": "HUBSPOT_PRIVATE_APP_TOKEN",
    "stripe": "STRIPE_API_KEY",
}
REQUIRED_CHECKS = (
    "quality",
    "unit-and-contract",
    "public-safety",
    "render-proof",
    "verify-build-packet",
)
PROOF_CLASS_NAMES = ("fixture_proof", "live_proof", "human_verdict")
BINDABLE_PROOF_CLASSES = ("fixture_proof", "live_proof")
UsefulnessVerdict = Literal["not_started", "useful", "not_useful", "waived"]
ProofClass = Literal["fixture", "live", "human"]
PrivateRunMode = Literal["fixture", "live"]
ProofStatus = Literal["completed", "pending", "not_started"]
CheckResultStatus = Literal["pending", "completed"]
IssuerKind = Literal["fixture", "human"]
StageState = Literal["completed", "not_started", "failed"]
TransportProvenance = Literal["native", "simulated"]
NON_HUMAN_ISSUERS = frozenset({"agent", "worker", "system", "ci", "model", "found-money"})
MAX_PAGE_LIMIT = 50
MAX_RECORD_LIMIT = 10_000
DEFAULT_PAGE_SIZE = 100
PRIVATE_INPUT_PREFIX = "private/"
PRIVATE_INPUT_ARTIFACTS = (
    "private/config.json",
    "private/sources/hubspot.fetched.json",
    "private/sources/hubspot.snapshot.json",
    "private/sources/stripe.fetched.json",
    "private/sources/stripe.snapshot.json",
)
UNSUPPORTED_DATE_QUERY_KEYS = frozenset(
    {
        "created",
        "created_gte",
        "created_lte",
        "created[gte]",
        "created[lte]",
        "createdate",
        "hs_createdate",
        "hs_timestamp",
        "startDate",
        "endDate",
        "updatedAt",
        "updated_gte",
        "updated_lte",
    }
)


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


def _canonical_json_bytes(payload: Any) -> bytes:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return (text + "\n").encode("utf-8")


def _parse_canonical(data: bytes, model: type[BaseModel]) -> Any:
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("canonical JSON must be bytes")
    try:
        payload = json.loads(bytes(data).decode("utf-8"), object_pairs_hook=_object_pairs_hook)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"malformed JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("canonical JSON root must be an object")
    parsed = model.model_validate(payload)
    canonical = parsed.to_canonical_json()  # type: ignore[attr-defined]
    if canonical != bytes(data):
        raise ValueError("serialized bytes are not exactly canonical")
    return parsed


class PrivateRunBoundsV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    window_start: datetime
    window_end: datetime
    page_limits: dict[str, int]
    record_limits: dict[str, int]

    @field_validator("window_start", "window_end", mode="before")
    @classmethod
    def _window(cls, value: Any) -> datetime:
        return _parse_utc(value)

    @field_validator("page_limits", "record_limits", mode="before")
    @classmethod
    def _limits(cls, value: Any) -> dict[str, int]:
        if not isinstance(value, Mapping):
            raise ValueError("bounds limits must be objects")
        out: dict[str, int] = {}
        for key, raw in value.items():
            if not isinstance(key, str) or isinstance(raw, bool) or not isinstance(raw, int):
                raise ValueError("bounds limits must be positive integers")
            system = _normalize_identifier(key)
            if system not in ALLOWED_SYSTEMS:
                raise ValueError("bounds limits must use allowlisted systems")
            if raw < 1:
                raise ValueError("bounds limits must be positive integers")
            out[system] = raw
        return out

    @model_validator(mode="after")
    def _ordered_window(self) -> Self:
        if self.window_end <= self.window_start:
            raise ValueError("bounds window_end must be after window_start")
        if set(self.page_limits) != set(self.record_limits):
            raise ValueError("page and record limits must name the same systems")
        for system, pages in self.page_limits.items():
            if pages < 1 or pages > MAX_PAGE_LIMIT:
                raise ValueError("page limits are out of bounds")
            records = self.record_limits[system]
            if records < 1 or records > MAX_RECORD_LIMIT:
                raise ValueError("record limits are out of bounds")
        return self


class PrivateRunScopeV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: PrivateRunMode
    systems: list[str]
    fixture: str | None = None
    runtime_mode: Literal["test", "live"] = "test"
    authorization: Literal["not-authorized", "matthew-authorized"]
    bounds: PrivateRunBoundsV1 | None = None

    @field_validator("systems", mode="before")
    @classmethod
    def _systems(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            raise ValueError("systems must be a list")
        items = [_normalize_identifier(item) if isinstance(item, str) else item for item in value]
        if any(not isinstance(item, str) for item in items):
            raise ValueError("systems entries must be strings")
        if len(items) < 2:
            raise ValueError("scope must name at least two matching systems")
        if len(items) != len(set(items)):
            raise ValueError("systems must be unique")
        unknown = [item for item in items if item not in ALLOWED_SYSTEMS]
        if unknown:
            raise ValueError("systems must be allowlisted hubspot and stripe reads")
        return items

    @field_validator("fixture", mode="before")
    @classmethod
    def _fixture(cls, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("fixture must be a string")
        return _normalize_identifier(value)

    @model_validator(mode="after")
    def _mode_rules(self) -> Self:
        if self.mode == "fixture":
            if not self.fixture:
                raise ValueError("fixture mode requires an explicit fixture id")
            if self.authorization == "matthew-authorized":
                raise ValueError("fixture mode cannot claim Matthew live authorization")
            if self.bounds is not None:
                raise ValueError("fixture mode must not declare live read bounds")
        if self.mode == "live":
            if self.fixture is not None:
                raise ValueError("live mode must not select a synthetic fixture")
            if self.bounds is None:
                raise ValueError("live mode requires explicit window and limit bounds")
            if set(self.bounds.page_limits) != set(self.systems):
                raise ValueError("bounds limits must match the declared systems")
        return self


class PrivateRunCredentialsV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["environment"]
    bindings: dict[str, str]

    @field_validator("bindings", mode="before")
    @classmethod
    def _bindings(cls, value: Any) -> dict[str, str]:
        if not isinstance(value, Mapping):
            raise ValueError("credential bindings must be an object")
        out: dict[str, str] = {}
        for key, raw in value.items():
            if not isinstance(key, str) or not isinstance(raw, str):
                raise ValueError("credential bindings must be strings")
            system = _normalize_identifier(key)
            env_name = raw.strip()
            if system not in REQUIRED_ENV_BINDINGS:
                raise ValueError("credential binding system is not allowlisted")
            if not re.fullmatch(r"[A-Z][A-Z0-9_]+", env_name):
                raise ValueError("credential binding must be an environment variable name")
            if any(ord(char) < 32 or ord(char) == 127 for char in env_name):
                raise ValueError("credential binding is malformed")
            out[system] = env_name
        return out


class PrivateRunConfigV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["found-money-private-run-config.v1"] = (
        "found-money-private-run-config.v1"
    )
    scope: PrivateRunScopeV1
    credentials: PrivateRunCredentialsV1

    @model_validator(mode="after")
    def _bindings_match_systems(self) -> Self:
        expected = set(self.scope.systems)
        if set(self.credentials.bindings) != expected:
            raise ValueError("credential bindings must match the declared systems")
        return self

    def canonical_dict(self) -> dict[str, Any]:
        payload = self.model_dump(mode="python")
        bounds = self.scope.bounds
        if bounds is not None:
            payload["scope"]["bounds"] = {
                "window_start": _format_utc(bounds.window_start),
                "window_end": _format_utc(bounds.window_end),
                "page_limits": dict(sorted(bounds.page_limits.items())),
                "record_limits": dict(sorted(bounds.record_limits.items())),
            }
        return payload

    def to_canonical_json(self) -> bytes:
        return _canonical_json_bytes(self.canonical_dict())


class PrivateRunArtifactV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    sha256: str

    @field_validator("path", mode="before")
    @classmethod
    def _path(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("path must be a string")
        text = value.strip().replace("\\", "/")
        if (
            not text
            or text.startswith("/")
            or text.startswith("~/")
            or _TRAVERSAL_RE.search(text)
            or text.endswith("/")
        ):
            raise ValueError("path must be relative and traversal-free")
        return text

    @field_validator("sha256", mode="before")
    @classmethod
    def _sha(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("hash must be a string")
        return _normalize_hash(value)


class RequiredCheckResultV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    check: str
    status: CheckResultStatus
    evidence_hash: str | None = None

    @field_validator("check", mode="before")
    @classmethod
    def _check(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("check name must be a string")
        return _normalize_identifier(value)

    @field_validator("evidence_hash", mode="before")
    @classmethod
    def _hash(cls, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("hash must be a string")
        return _normalize_hash(value)

    @model_validator(mode="after")
    def _hash_matches_status(self) -> Self:
        if self.status == "completed" and self.evidence_hash is None:
            raise ValueError("completed checks require structured evidence hashes")
        if self.status == "pending" and self.evidence_hash is not None:
            raise ValueError("pending checks must not claim evidence hashes")
        return self


class PassingCheckReceiptV1(BaseModel):
    """Typed passing CI/check receipt. FAIL and other statuses are rejected."""

    model_config = ConfigDict(extra="forbid")

    check: str
    status: Literal["passed"]
    commit: str
    command: str
    evidence_sha256: str

    @field_validator("check", mode="before")
    @classmethod
    def _check(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("check name must be a string")
        text = _normalize_identifier(value)
        if text not in REQUIRED_CHECKS:
            raise ValueError("check name is not in the declared CI check set")
        return text

    @field_validator("status", mode="before")
    @classmethod
    def _status(cls, value: Any) -> str:
        if not isinstance(value, str) or value.strip().casefold() != "passed":
            raise ValueError("check receipt status must be passed")
        return "passed"

    @field_validator("commit", mode="before")
    @classmethod
    def _commit(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("commit_hash must be a string")
        text = value.strip().lower()
        if not _COMMIT_RE.fullmatch(text):
            raise ValueError("commit_hash must be a 40-char hex digest")
        return text

    @field_validator("command", mode="before")
    @classmethod
    def _command(cls, value: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("command or CI run reference must be non-blank")
        return value.strip()

    @field_validator("evidence_sha256", mode="before")
    @classmethod
    def _evidence(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("hash must be a string")
        return _normalize_hash(value)

    def canonical_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="python")

    def to_canonical_json(self) -> bytes:
        return _canonical_json_bytes(self.canonical_dict())


class ProofClassRecordV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: ProofStatus
    required_checks: list[str]
    required_check_results: list[RequiredCheckResultV1]
    commit_hash: str | None = None

    @field_validator("required_checks")
    @classmethod
    def _checks(cls, value: Any) -> list[str]:
        if not isinstance(value, list) or list(value) != list(REQUIRED_CHECKS):
            raise ValueError("required_checks must be the exact declared CI check set")
        return list(REQUIRED_CHECKS)

    @field_validator("commit_hash", mode="before")
    @classmethod
    def _commit(cls, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("commit_hash must be a string")
        text = value.strip().lower()
        if not _COMMIT_RE.fullmatch(text):
            raise ValueError("commit_hash must be a 40-char hex digest")
        return text

    @model_validator(mode="after")
    def _status_commit(self) -> Self:
        names = [item.check for item in self.required_check_results]
        if names != list(REQUIRED_CHECKS):
            raise ValueError("required_check_results must cover the exact declared CI check set")
        completed = [item for item in self.required_check_results if item.status == "completed"]
        if self.status == "not_started":
            if self.commit_hash is not None:
                raise ValueError("not_started proof classes must not bind a commit hash")
            if completed:
                raise ValueError("not_started proof classes cannot complete checks")
        if self.status == "pending":
            if self.commit_hash is None:
                raise ValueError("pending proof classes require an exact HEAD commit hash")
            if completed:
                raise ValueError("pending proof classes cannot mark checks complete")
        if self.status == "completed":
            if self.commit_hash is None:
                raise ValueError("completed proof classes require an exact commit hash")
            if len(completed) != len(REQUIRED_CHECKS):
                raise ValueError("completed proof classes require structured check evidence")
            if any(item.evidence_hash is None for item in self.required_check_results):
                raise ValueError("completed proof classes require structured check evidence")
        return self


class PrivateRunManifestV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["found-money-private-run.v1"] = "found-money-private-run.v1"
    run_id: str
    built_at: datetime
    mode: PrivateRunMode
    systems: list[str]
    fixture: str | None = None
    commit_hash: str
    required_checks: list[str]
    proof_classes: dict[str, ProofClassRecordV1]
    source_receipt_paths: list[str]
    source_hashes: dict[str, str]
    source_set_hash: str
    source_transport_provenance: dict[str, str]
    config_sha256: str
    fetched_snapshot_sha256: dict[str, str]
    source_snapshot_sha256: dict[str, str]
    artifacts: list[PrivateRunArtifactV1]
    claims_real_business_run: Literal[False] = False
    no_send: Literal[True] = True
    no_write: Literal[True] = True
    no_audience: Literal[True] = True
    no_spend: Literal[True] = True
    no_recovered_revenue_claim: Literal[True] = True

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

    @field_validator("systems")
    @classmethod
    def _systems(cls, value: Any) -> list[str]:
        return PrivateRunScopeV1._systems(value)

    @field_validator("commit_hash", mode="before")
    @classmethod
    def _commit(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("commit_hash must be a string")
        text = value.strip().lower()
        if not _COMMIT_RE.fullmatch(text):
            raise ValueError("commit_hash must be a 40-char hex digest")
        return text

    @field_validator("required_checks")
    @classmethod
    def _checks(cls, value: Any) -> list[str]:
        if not isinstance(value, list) or list(value) != list(REQUIRED_CHECKS):
            raise ValueError("required_checks must be the exact declared CI check set")
        return list(REQUIRED_CHECKS)

    @field_validator("proof_classes", mode="before")
    @classmethod
    def _classes(cls, value: Any) -> dict[str, Any]:
        if not isinstance(value, Mapping) or set(value) != set(PROOF_CLASS_NAMES):
            raise ValueError("proof_classes must separately label fixture, live, and human proof")
        return dict(value)

    @field_validator("source_receipt_paths")
    @classmethod
    def _paths(cls, value: Any) -> list[str]:
        if not isinstance(value, list) or len(value) < 2:
            raise ValueError("source_receipt_paths must include at least two receipts")
        return [PrivateRunArtifactV1._path(item) for item in value]

    @field_validator("source_hashes", mode="before")
    @classmethod
    def _hashes(cls, value: Any) -> dict[str, str]:
        if not isinstance(value, Mapping):
            raise ValueError("source_hashes must be an object")
        out: dict[str, str] = {}
        for key, raw in value.items():
            if not isinstance(key, str) or not isinstance(raw, str):
                raise ValueError("source_hashes entries must be strings")
            out[_normalize_identifier(key)] = _normalize_hash(raw)
        return out

    @field_validator("source_set_hash", "config_sha256", mode="before")
    @classmethod
    def _set_hash(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("source_set_hash must be a string")
        return _normalize_hash(value)

    @field_validator(
        "source_transport_provenance",
        mode="before",
    )
    @classmethod
    def _provenance(cls, value: Any) -> dict[str, str]:
        if not isinstance(value, Mapping):
            raise ValueError("source_transport_provenance must be an object")
        out: dict[str, str] = {}
        for key, raw in value.items():
            if not isinstance(key, str) or not isinstance(raw, str):
                raise ValueError("source_transport_provenance entries must be strings")
            system = _normalize_identifier(key)
            kind = raw.strip()
            if kind not in {"native", "simulated"}:
                raise ValueError("transport provenance must be native or simulated")
            out[system] = kind
        return out

    @field_validator("fetched_snapshot_sha256", "source_snapshot_sha256", mode="before")
    @classmethod
    def _snapshot_hashes(cls, value: Any) -> dict[str, str]:
        return PrivateRunManifestV1._hashes(value)

    @model_validator(mode="after")
    def _honest_labels(self) -> Self:
        if self.claims_real_business_run is not False:
            raise ValueError("manifest must not claim a real-business run")
        if self.mode == "fixture" and self.proof_classes["live_proof"].status != "not_started":
            raise ValueError("fixture runs cannot complete live proof")
        if self.mode == "live" and self.proof_classes["fixture_proof"].status != "not_started":
            raise ValueError("live runs cannot complete fixture proof")
        if set(self.source_transport_provenance) != set(self.systems):
            raise ValueError("transport provenance must cover the declared systems")
        if set(self.fetched_snapshot_sha256) != set(self.systems):
            raise ValueError("fetched snapshot hashes must cover the declared systems")
        if set(self.source_snapshot_sha256) != set(self.systems):
            raise ValueError("source snapshot hashes must cover the declared systems")
        if self.mode == "fixture" and any(
            kind != "simulated" for kind in self.source_transport_provenance.values()
        ):
            raise ValueError("fixture runs cannot claim native transport provenance")
        if self.proof_classes["live_proof"].status == "completed" and any(
            kind != "native" for kind in self.source_transport_provenance.values()
        ):
            raise ValueError("completed live proof requires native source provenance")
        paths = [item.path for item in self.artifacts]
        if len(paths) != len(set(paths)):
            raise ValueError("artifact paths must be unique")
        if "private-run.json" in paths:
            raise ValueError("manifest must not self-hash")
        return self

    def canonical_dict(self) -> dict[str, Any]:
        payload = self.model_dump(mode="python")
        payload["built_at"] = _format_utc(self.built_at)
        payload["artifacts"] = sorted(payload["artifacts"], key=lambda item: item["path"])
        return payload

    def to_canonical_json(self) -> bytes:
        return _canonical_json_bytes(self.canonical_dict())


class PrivateRunAggregateProofV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["found-money-private-run-aggregate.v1"] = (
        "found-money-private-run-aggregate.v1"
    )
    run_id: str
    built_at: datetime
    public_safe: Literal[True] = True
    public_safety_scan: Literal["pass"] = "pass"
    systems: list[str]
    source_record_counts: dict[str, int]
    fetched_record_counts: dict[str, int]
    windowed_record_counts: dict[str, int]
    window_excluded_record_counts: dict[str, int]
    window_unknown_timestamp_counts: dict[str, int]
    resolved_customer_count: int = Field(ge=0)
    ambiguous_cluster_count: int = Field(ge=0)
    candidates_by_family: dict[str, int]
    exclusion_count: int = Field(ge=0)
    data_gap_count: int = Field(ge=0)
    suppression_count: int = Field(ge=0)
    identified_opportunity_minor: dict[str, Decimal]
    basis_counts_by_currency: dict[str, CurrencyBasisCountsV1]
    run_scope: dict[str, str]
    claims_real_business_run: Literal[False] = False
    no_send: Literal[True] = True
    no_write: Literal[True] = True
    no_audience: Literal[True] = True
    no_spend: Literal[True] = True
    no_recovered_revenue_claim: Literal[True] = True

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

    @field_validator("systems")
    @classmethod
    def _systems(cls, value: Any) -> list[str]:
        return PrivateRunScopeV1._systems(value)

    @field_validator(
        "source_record_counts",
        "fetched_record_counts",
        "windowed_record_counts",
        "window_excluded_record_counts",
        "window_unknown_timestamp_counts",
        "candidates_by_family",
        mode="before",
    )
    @classmethod
    def _counts(cls, value: Any) -> dict[str, int]:
        if not isinstance(value, Mapping):
            raise ValueError("count maps must be objects")
        out: dict[str, int] = {}
        for key, raw in value.items():
            if (
                not isinstance(key, str)
                or isinstance(raw, bool)
                or not isinstance(raw, int)
                or raw < 0
            ):
                raise ValueError("count map entries must be non-negative integers")
            out[_normalize_identifier(key)] = raw
        return dict(sorted(out.items()))

    @field_validator("identified_opportunity_minor", mode="before")
    @classmethod
    def _totals(cls, value: Any) -> dict[str, Decimal]:
        if not isinstance(value, Mapping):
            raise ValueError("identified_opportunity_minor must be an object")
        out: dict[str, Decimal] = {}
        for key, raw in value.items():
            if not isinstance(key, str):
                raise ValueError("currency keys must be strings")
            out[normalize_currency(key)] = normalize_minor_units(raw)
        return dict(sorted(out.items()))

    @field_validator("run_scope", mode="before")
    @classmethod
    def _scope(cls, value: Any) -> dict[str, str]:
        if not isinstance(value, Mapping):
            raise ValueError("run_scope must be an object")
        out: dict[str, str] = {}
        for key, raw in value.items():
            if not isinstance(key, str) or not isinstance(raw, str) or not raw.strip():
                raise ValueError("run_scope entries must be non-blank strings")
            out[_normalize_identifier(key)] = raw.strip()
        return out

    @model_validator(mode="after")
    def _no_identity_keys(self) -> Self:
        blob = json.dumps(self.canonical_dict(), sort_keys=True)
        for banned in ('"email"', '"phone"', '"source_id"', '"record_id"', '"contact_id"'):
            if banned in blob.casefold().replace(" ", ""):
                raise ValueError("aggregate proof must not contain record-level identifiers")
        return self

    def canonical_dict(self) -> dict[str, Any]:
        payload = self.model_dump(mode="python")
        payload["built_at"] = _format_utc(self.built_at)
        payload["identified_opportunity_minor"] = {
            key: str(value) for key, value in self.identified_opportunity_minor.items()
        }
        payload["basis_counts_by_currency"] = {
            key: value.model_dump(mode="python")
            for key, value in sorted(self.basis_counts_by_currency.items())
        }
        return payload

    def to_canonical_json(self) -> bytes:
        return _canonical_json_bytes(self.canonical_dict())


class PrivateRunDiagnosticsV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["found-money-private-run-diagnostics.v1"] = (
        "found-money-private-run-diagnostics.v1"
    )
    run_id: str
    stages: dict[str, StageState]
    resolved_customer_count: int = Field(ge=0)
    ambiguous_cluster_count: int = Field(ge=0)
    candidates_by_family: dict[str, int]
    exclusion_count: int = Field(ge=0)
    data_gap_count: int = Field(ge=0)
    overlap_count: int = Field(ge=0)
    source_record_counts: dict[str, int]
    fetched_record_counts: dict[str, int]
    windowed_record_counts: dict[str, int]
    window_excluded_record_counts: dict[str, int]
    window_unknown_timestamp_counts: dict[str, int]
    identified_opportunity_minor: dict[str, Decimal]
    basis_counts_by_currency: dict[str, CurrencyBasisCountsV1]
    rendering_html_sha256: str
    activation_manifest_sha256: str
    send_performed: Literal[False] = False
    audience_created: Literal[False] = False
    provider_write_performed: Literal[False] = False

    @field_validator("run_id", mode="before")
    @classmethod
    def _run(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("run_id must be a string")
        return _normalize_identifier(value)

    @field_validator("stages", mode="before")
    @classmethod
    def _stages(cls, value: Any) -> dict[str, str]:
        required = {
            "identity",
            "ambiguity",
            "event",
            "exclusion",
            "value",
            "overlap",
            "ranking",
            "strategy",
            "rendering",
            "activation",
        }
        if not isinstance(value, Mapping) or set(value) != required:
            raise ValueError("diagnostics must record every required stage")
        out: dict[str, str] = {}
        for key, raw in value.items():
            if raw not in {"completed", "not_started", "failed"}:
                raise ValueError("stage state is invalid")
            out[str(key)] = str(raw)
        return out

    @field_validator(
        "candidates_by_family",
        "source_record_counts",
        "fetched_record_counts",
        "windowed_record_counts",
        "window_excluded_record_counts",
        "window_unknown_timestamp_counts",
        mode="before",
    )
    @classmethod
    def _counts(cls, value: Any) -> dict[str, int]:
        return PrivateRunAggregateProofV1._counts(value)

    @field_validator("identified_opportunity_minor", mode="before")
    @classmethod
    def _totals(cls, value: Any) -> dict[str, Decimal]:
        return PrivateRunAggregateProofV1._totals(value)

    @field_validator("rendering_html_sha256", "activation_manifest_sha256", mode="before")
    @classmethod
    def _hashes(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("hash must be a string")
        return _normalize_hash(value)

    def canonical_dict(self) -> dict[str, Any]:
        payload = self.model_dump(mode="python")
        payload["identified_opportunity_minor"] = {
            key: str(value) for key, value in self.identified_opportunity_minor.items()
        }
        payload["basis_counts_by_currency"] = {
            key: value.model_dump(mode="python")
            for key, value in sorted(self.basis_counts_by_currency.items())
        }
        return payload

    def to_canonical_json(self) -> bytes:
        return _canonical_json_bytes(self.canonical_dict())


class PrivateRunSourceStageEvidenceV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["found-money-private-run-source-stage.v1"] = (
        "found-money-private-run-source-stage.v1"
    )
    run_id: str
    built_at: datetime
    systems: list[str]
    source_receipt_hashes: dict[str, str]
    source_set_hash: str
    config_sha256: str
    fetched_snapshot_sha256: dict[str, str]
    source_snapshot_sha256: dict[str, str]
    source_transport_provenance: dict[str, str]
    source_record_counts: dict[str, int]
    fetched_record_counts: dict[str, int]
    windowed_record_counts: dict[str, int]
    window_excluded_record_counts: dict[str, int]
    window_unknown_timestamp_counts: dict[str, int]
    resolved_customer_count: int = Field(ge=0)
    ambiguous_cluster_count: int = Field(ge=0)
    candidates_by_family: dict[str, int]
    exclusion_count: int = Field(ge=0)
    data_gap_count: int = Field(ge=0)
    suppression_count: int = Field(ge=0)
    identified_opportunity_minor: dict[str, Decimal]
    basis_counts_by_currency: dict[str, CurrencyBasisCountsV1]
    run_scope: dict[str, str]
    identity_aggregate_sha256: str
    event_public_sha256: str
    diagnostics_sha256: str

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

    @field_validator("systems")
    @classmethod
    def _systems(cls, value: Any) -> list[str]:
        return PrivateRunScopeV1._systems(value)

    @field_validator("source_receipt_hashes", mode="before")
    @classmethod
    def _receipts(cls, value: Any) -> dict[str, str]:
        return PrivateRunManifestV1._hashes(value)

    @field_validator(
        "source_set_hash",
        "config_sha256",
        "identity_aggregate_sha256",
        "event_public_sha256",
        "diagnostics_sha256",
        mode="before",
    )
    @classmethod
    def _digest(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("hash must be a string")
        return _normalize_hash(value)

    @field_validator("fetched_snapshot_sha256", "source_snapshot_sha256", mode="before")
    @classmethod
    def _snapshot_hashes(cls, value: Any) -> dict[str, str]:
        return PrivateRunManifestV1._hashes(value)

    @field_validator("source_transport_provenance", mode="before")
    @classmethod
    def _provenance(cls, value: Any) -> dict[str, str]:
        return PrivateRunManifestV1._provenance(value)

    @field_validator(
        "source_record_counts",
        "fetched_record_counts",
        "windowed_record_counts",
        "window_excluded_record_counts",
        "window_unknown_timestamp_counts",
        "candidates_by_family",
        mode="before",
    )
    @classmethod
    def _counts(cls, value: Any) -> dict[str, int]:
        return PrivateRunAggregateProofV1._counts(value)

    @field_validator("identified_opportunity_minor", mode="before")
    @classmethod
    def _totals(cls, value: Any) -> dict[str, Decimal]:
        return PrivateRunAggregateProofV1._totals(value)

    @field_validator("run_scope", mode="before")
    @classmethod
    def _scope(cls, value: Any) -> dict[str, str]:
        return PrivateRunAggregateProofV1._scope(value)

    def canonical_dict(self) -> dict[str, Any]:
        payload = self.model_dump(mode="python")
        payload["built_at"] = _format_utc(self.built_at)
        payload["identified_opportunity_minor"] = {
            key: str(value) for key, value in self.identified_opportunity_minor.items()
        }
        payload["basis_counts_by_currency"] = {
            key: value.model_dump(mode="python")
            for key, value in sorted(self.basis_counts_by_currency.items())
        }
        return payload

    def to_canonical_json(self) -> bytes:
        return _canonical_json_bytes(self.canonical_dict())


class OperatorReviewPacketV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["found-money-operator-review-packet.v1"] = (
        "found-money-operator-review-packet.v1"
    )
    run_id: str
    usefulness_verdict: UsefulnessVerdict
    rationale: str
    limitations: list[str]
    unresolved_data_gaps: list[str]
    evidence_refs: dict[str, str]
    no_send: Literal[True] = True
    no_write: Literal[True] = True
    no_audience: Literal[True] = True
    no_spend: Literal[True] = True
    no_recovered_revenue_claim: Literal[True] = True
    claims_real_business_run: Literal[False] = False
    human_issued: bool
    proof_class: ProofClass
    issuer_kind: IssuerKind
    operator_id: str | None = None
    issued_at: datetime | None = None
    check_evidence_hashes: dict[str, str] | None = None
    bound_commit_hash: str | None = None

    @field_validator("rationale", mode="before")
    @classmethod
    def _rationale(cls, value: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("rationale must be a non-empty string")
        text = value.strip()
        lowered = text.casefold()
        for banned in ("message sent", "audience created", "recovered revenue guaranteed"):
            if banned in lowered:
                raise ValueError("operator packet must not claim send, write, or recovered revenue")
        return text

    @field_validator("limitations", "unresolved_data_gaps")
    @classmethod
    def _lists(cls, value: Any) -> list[str]:
        if not isinstance(value, list) or not value:
            raise ValueError("list must be a non-empty list of strings")
        items = [_normalize_identifier(item) if isinstance(item, str) else item for item in value]
        if any(not isinstance(item, str) for item in items):
            raise ValueError("list entries must be strings")
        return items

    @field_validator("evidence_refs", mode="before")
    @classmethod
    def _refs(cls, value: Any) -> dict[str, str]:
        if not isinstance(value, Mapping) or not value:
            raise ValueError("evidence_refs must be a non-empty object")
        out: dict[str, str] = {}
        for key, raw in value.items():
            if not isinstance(key, str) or not isinstance(raw, str):
                raise ValueError("evidence_refs entries must be strings")
            out[PrivateRunArtifactV1._path(key)] = _normalize_hash(raw)
        return out

    @field_validator("run_id", mode="before")
    @classmethod
    def _run(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("run_id must be a string")
        return _normalize_identifier(value)

    @field_validator("operator_id", mode="before")
    @classmethod
    def _operator(cls, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("operator_id must be a string")
        text = _normalize_identifier(value)
        if text.casefold() in NON_HUMAN_ISSUERS or text.casefold().startswith("agent:"):
            raise ValueError("operator identity must be human")
        return text

    @field_validator("issued_at", mode="before")
    @classmethod
    def _issued(cls, value: Any) -> datetime | None:
        if value is None:
            return None
        return _parse_utc(value)

    @field_validator("bound_commit_hash", mode="before")
    @classmethod
    def _bound_commit(cls, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("commit_hash must be a string")
        text = value.strip().lower()
        if not _COMMIT_RE.fullmatch(text):
            raise ValueError("commit_hash must be a 40-char hex digest")
        return text

    @field_validator("check_evidence_hashes", mode="before")
    @classmethod
    def _check_hashes(cls, value: Any) -> dict[str, str] | None:
        if value is None:
            return None
        if not isinstance(value, Mapping):
            raise ValueError("check_evidence_hashes must be an object")
        out: dict[str, str] = {}
        for key, raw in value.items():
            if not isinstance(key, str) or not isinstance(raw, str):
                raise ValueError("check_evidence_hashes entries must be strings")
            out[_normalize_identifier(key)] = _normalize_hash(raw)
        if set(out) != set(REQUIRED_CHECKS):
            raise ValueError("check_evidence_hashes must cover the exact declared CI check set")
        return {name: out[name] for name in REQUIRED_CHECKS}

    @model_validator(mode="after")
    def _honest_verdict(self) -> Self:
        if self.claims_real_business_run is not False:
            raise ValueError("operator packet must not claim a real-business run")
        if any(
            flag is not True
            for flag in (
                self.no_send,
                self.no_write,
                self.no_audience,
                self.no_spend,
                self.no_recovered_revenue_claim,
            )
        ):
            raise ValueError("operator packet must assert no-send/no-write/no-audience/no-spend")
        if self.issuer_kind in NON_HUMAN_ISSUERS:
            raise ValueError("operator identity must be human")
        if self.proof_class == "fixture":
            if self.human_issued or self.issuer_kind != "fixture":
                raise ValueError("fixture operator packets cannot be human-issued")
            if self.usefulness_verdict != "not_started":
                raise ValueError("fixture operator packets cannot issue a usefulness verdict")
            if self.operator_id is not None or self.issued_at is not None:
                raise ValueError("fixture operator packets must not bind a human issuer")
            if self.check_evidence_hashes is not None or self.bound_commit_hash is not None:
                raise ValueError("fixture operator packets must not claim check completion")
        if self.proof_class == "live":
            if self.human_issued or self.issuer_kind != "fixture":
                raise ValueError("live operator packets cannot be human-issued")
            if self.usefulness_verdict != "not_started":
                raise ValueError("live operator packets cannot issue a usefulness verdict")
        if self.proof_class == "human":
            if not self.human_issued or self.issuer_kind != "human":
                raise ValueError("human proof_class requires a human issuer")
            if self.usefulness_verdict not in {"useful", "not_useful", "waived"}:
                raise ValueError("human packets require a usefulness verdict")
            if self.operator_id is None or self.issued_at is None:
                raise ValueError("human packets require operator identity and issued_at")
            if self.bound_commit_hash is None or self.check_evidence_hashes is None:
                raise ValueError("human packets require bound commit and check evidence")
        return self

    def canonical_dict(self) -> dict[str, Any]:
        payload = self.model_dump(mode="python")
        if self.issued_at is not None:
            payload["issued_at"] = _format_utc(self.issued_at)
        return payload

    def to_canonical_json(self) -> bytes:
        return _canonical_json_bytes(self.canonical_dict())


def parse_private_run_config(data: bytes) -> PrivateRunConfigV1:
    return _parse_canonical(data, PrivateRunConfigV1)


def parse_private_run_manifest(data: bytes) -> PrivateRunManifestV1:
    return _parse_canonical(data, PrivateRunManifestV1)


def parse_private_run_aggregate(data: bytes) -> PrivateRunAggregateProofV1:
    return _parse_canonical(data, PrivateRunAggregateProofV1)


def parse_private_run_diagnostics(data: bytes) -> PrivateRunDiagnosticsV1:
    return _parse_canonical(data, PrivateRunDiagnosticsV1)


def parse_private_run_source_stage(data: bytes) -> PrivateRunSourceStageEvidenceV1:
    return _parse_canonical(data, PrivateRunSourceStageEvidenceV1)


def parse_operator_packet(data: bytes) -> OperatorReviewPacketV1:
    return _parse_canonical(data, OperatorReviewPacketV1)


def parse_passing_check_receipt(data: bytes) -> PassingCheckReceiptV1:
    return _parse_canonical(data, PassingCheckReceiptV1)
