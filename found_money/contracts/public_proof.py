"""FM-037 public-safe demo/newsletter proof contracts."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal, Mapping, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from found_money.contracts.source_audit import (
    BinaryAuditPdfExtractionV1,
    BinaryAuditPngExtractionV1,
)
from found_money.contracts.value import normalize_currency, normalize_minor_units

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TRAVERSAL_RE = re.compile(r"(^|/)\.\.(/|$)")
_WINDOWS_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")
PUBLIC_PROOF_CONFIG_SCHEMA = "found-money-public-proof-config.v1"
PUBLIC_PROOF_MANIFEST_SCHEMA = "found-money-public-proof.v1"
PUBLIC_PROOF_AGGREGATE_SCHEMA = "found-money-public-proof-aggregate.v1"
PUBLIC_PROOF_RECOMPUTE_SCHEMA = "found-money-public-proof-recompute.v1"
PUBLIC_PROOF_AUDIT_SCHEMA = "found-money-public-proof-audit.v1"
PUBLIC_PROOF_BINARY_EXTRACTIONS_SCHEMA = "found-money-public-proof-binary-extractions.v1"
CANONICAL_PUBLIC_PROOF_SCENARIOS = (
    "synthetic-saas-v1",
    "synthetic-ecommerce-v1",
    "synthetic-service-v1",
)
AUDITED_PUBLIC_PROOF_PLATFORMS = frozenset({"linux", "macos"})
PUBLIC_PROOF_AUDIT_CHECK_IDS = (
    "raw_source_identifier_absence",
    "customer_identity_absence",
    "proper_noun_absence",
    "credential_endpoint_path_absence",
    "unsupported_proof_absence",
    "aggregate_only_public_projection",
    "state_language_separation",
)
PUBLIC_PROOF_AUDIT_TEXT_FILE_COUNT = 10
PUBLIC_PROOF_AUDIT_PDF_COUNT = 1
PUBLIC_PROOF_AUDIT_PNG_COUNT = 1
PUBLIC_PROOF_AUDIT_EXTRACTIONS_SHA256 = (
    "dc09dae77aae2c342b9b5f96ff515ea2fb8818d2c624b2d48197f211138e7b57"
)
PUBLIC_PROOF_AUDIT_PACKET_SHA256 = (
    "196bd906515e3545cd7b1ed6e2103988bc51e1ebbd565994b6e8616b309a29fd"
)
EvidenceClass = Literal["synthetic"]
RealBusinessAggregateStatus = Literal["absent"]
ValueBasisLabel = Literal["observed", "modeled", "unquantified"]
UsefulState = Literal["not_started"]
SentState = Literal["not_performed"]
RecoveredState = Literal["not_claimed"]
PublicationState = Literal["not_performed"]

# Private client proper nouns that must never reach a public artifact. The built-in
# value is a neutral placeholder on purpose: a deployment supplies its own client
# names through FOUND_MONEY_CLIENT_NOUNS (comma-separated) so that no real client
# name ever ships in this source tree.
VAULT_CLIENT_NOUNS = ("ExampleClientCo", "example client co")


def client_nouns() -> tuple[str, ...]:
    raw = os.environ.get("FOUND_MONEY_CLIENT_NOUNS", "")
    configured = tuple(part.strip() for part in raw.split(",") if part.strip())
    return VAULT_CLIENT_NOUNS + configured


BANNED_STATE_PHRASES = (
    "recovered revenue guaranteed",
    "message sent",
    "newsletter sent",
    "audience created",
    "published to customers",
    "live real-business aggregate included",
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


def _reject_banned_blob(blob: str) -> None:
    lowered = blob.casefold()
    for noun in client_nouns():
        if noun.casefold() in lowered:
            raise ValueError("public proof must not contain Vault-only client names")
    for phrase in BANNED_STATE_PHRASES:
        if phrase in lowered:
            raise ValueError("public proof must not claim send, publication, or recovered revenue")


class PublicProofConfigV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["found-money-public-proof-config.v1"] = (
        "found-money-public-proof-config.v1"
    )
    scenarios: list[str]
    evidence_class: EvidenceClass = "synthetic"
    real_business_aggregate: RealBusinessAggregateStatus = "absent"
    fm036_aggregate_status: RealBusinessAggregateStatus = "absent"

    @field_validator("scenarios")
    @classmethod
    def _scenarios(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            raise ValueError("scenarios must be a list")
        items = [_normalize_identifier(item) if isinstance(item, str) else item for item in value]
        if items != list(CANONICAL_PUBLIC_PROOF_SCENARIOS):
            raise ValueError("public proof must select the three canonical synthetic scenarios")
        return items

    @model_validator(mode="after")
    def _synthetic_only(self) -> Self:
        if self.real_business_aggregate != "absent" or self.fm036_aggregate_status != "absent":
            raise ValueError("FM-036 live aggregate is absent and must remain explicit")
        if self.evidence_class != "synthetic":
            raise ValueError("this public proof is synthetic-only")
        return self

    def canonical_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="python")

    def to_canonical_json(self) -> bytes:
        return _canonical_json_bytes(self.canonical_dict())


class PublicProofArtifactV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    sha256: str

    @field_validator("path", mode="before")
    @classmethod
    def _path(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("path must be a string")
        return _normalize_relative_path(value)

    @field_validator("sha256", mode="before")
    @classmethod
    def _sha(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("hash must be a string")
        return _normalize_hash(value)


class PublicProofScenarioSummaryV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario_id: str
    business_model: Literal["saas", "ecommerce", "service"]
    evidence_class: EvidenceClass = "synthetic"
    run_id: str
    source_set_hash: str
    aggregate_sha256: str
    resolved_customer_count: int = Field(ge=0)
    ambiguous_cluster_count: int = Field(ge=0)
    event_count: int = Field(ge=0)
    play_count: int = Field(ge=0)
    card_count: int = Field(ge=0)
    unquantified_count: int = Field(ge=0)
    identified_opportunity_minor: dict[str, Decimal]
    candidates_by_family: dict[str, int]
    observed_count: int = Field(ge=0)
    modeled_count: int = Field(ge=0)

    @field_validator("scenario_id", "run_id", mode="before")
    @classmethod
    def _ids(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("identifier must be a string")
        return _normalize_identifier(value)

    @field_validator("source_set_hash", "aggregate_sha256", mode="before")
    @classmethod
    def _hashes(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("hash must be a string")
        return _normalize_hash(value)

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

    @field_validator("candidates_by_family", mode="before")
    @classmethod
    def _families(cls, value: Any) -> dict[str, int]:
        if not isinstance(value, Mapping):
            raise ValueError("candidates_by_family must be an object")
        out: dict[str, int] = {}
        for key, raw in value.items():
            if (
                not isinstance(key, str)
                or isinstance(raw, bool)
                or not isinstance(raw, int)
                or raw < 0
            ):
                raise ValueError("family counts must be non-negative integers")
            out[_normalize_identifier(key)] = raw
        return dict(sorted(out.items()))


class PublicProofStateV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_class: EvidenceClass = "synthetic"
    real_business_aggregate: RealBusinessAggregateStatus = "absent"
    fm036_aggregate_status: RealBusinessAggregateStatus = "absent"
    identified_opportunity_is_not_recovered: Literal[True] = True
    useful: UsefulState = "not_started"
    sent: SentState = "not_performed"
    recovered: RecoveredState = "not_claimed"
    publication: PublicationState = "not_performed"
    no_send: Literal[True] = True
    no_publish: Literal[True] = True
    no_write: Literal[True] = True
    no_audience: Literal[True] = True
    no_spend: Literal[True] = True
    claims_real_business_run: Literal[False] = False
    claims_new_visual_baseline: Literal[False] = False


class PublicProofManifestV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["found-money-public-proof.v1"] = "found-money-public-proof.v1"
    run_id: str
    built_at: datetime
    evidence_class: EvidenceClass = "synthetic"
    scenarios: list[str]
    scenario_run_ids: dict[str, str]
    scenario_source_set_hashes: dict[str, str]
    source_set_hash: str
    config_sha256: str
    aggregate_sha256: str
    recompute_sha256: str
    artifacts: list[PublicProofArtifactV1]
    state: PublicProofStateV1

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

    @field_validator("scenarios")
    @classmethod
    def _scenarios(cls, value: Any) -> list[str]:
        return PublicProofConfigV1._scenarios(value)

    @field_validator(
        "source_set_hash",
        "config_sha256",
        "aggregate_sha256",
        "recompute_sha256",
        mode="before",
    )
    @classmethod
    def _digest(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("hash must be a string")
        return _normalize_hash(value)

    @field_validator("scenario_run_ids", mode="before")
    @classmethod
    def _run_ids(cls, value: Any) -> dict[str, str]:
        if not isinstance(value, Mapping):
            raise ValueError("scenario_run_ids must be an object")
        out: dict[str, str] = {}
        for key, raw in value.items():
            if not isinstance(key, str) or not isinstance(raw, str):
                raise ValueError("scenario_run_ids entries must be strings")
            out[_normalize_identifier(key)] = _normalize_identifier(raw)
        return out

    @field_validator("scenario_source_set_hashes", mode="before")
    @classmethod
    def _source_hashes(cls, value: Any) -> dict[str, str]:
        if not isinstance(value, Mapping):
            raise ValueError("scenario_source_set_hashes must be an object")
        out: dict[str, str] = {}
        for key, raw in value.items():
            if not isinstance(key, str) or not isinstance(raw, str):
                raise ValueError("scenario_source_set_hashes entries must be strings")
            out[_normalize_identifier(key)] = _normalize_hash(raw)
        return out

    @model_validator(mode="after")
    def _closure(self) -> Self:
        if set(self.scenario_run_ids) != set(CANONICAL_PUBLIC_PROOF_SCENARIOS):
            raise ValueError("scenario_run_ids must cover the three canonical scenarios")
        if set(self.scenario_source_set_hashes) != set(CANONICAL_PUBLIC_PROOF_SCENARIOS):
            raise ValueError("scenario source-set hashes must cover the three canonical scenarios")
        for name, digest in self.scenario_source_set_hashes.items():
            self.scenario_source_set_hashes[name] = _normalize_hash(digest)
        for name, run_id in self.scenario_run_ids.items():
            self.scenario_run_ids[name] = _normalize_identifier(run_id)
        paths = [item.path for item in self.artifacts]
        if len(paths) != len(set(paths)):
            raise ValueError("artifact paths must be unique")
        if "public-proof.json" in paths:
            raise ValueError("manifest must not self-hash")
        if self.state.real_business_aggregate != "absent":
            raise ValueError("manifest must keep FM-036 aggregate absent")
        if self.state.claims_new_visual_baseline is not False:
            raise ValueError("public proof must not ratify a visual baseline")
        return self

    def canonical_dict(self) -> dict[str, Any]:
        payload = self.model_dump(mode="python")
        payload["built_at"] = _format_utc(self.built_at)
        payload["artifacts"] = sorted(payload["artifacts"], key=lambda item: item["path"])
        return payload

    def to_canonical_json(self) -> bytes:
        return _canonical_json_bytes(self.canonical_dict())


class PublicProofAggregateV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["found-money-public-proof-aggregate.v1"] = (
        "found-money-public-proof-aggregate.v1"
    )
    run_id: str
    built_at: datetime
    public_safe: Literal[True] = True
    evidence_class: EvidenceClass = "synthetic"
    real_business_aggregate: RealBusinessAggregateStatus = "absent"
    fm036_aggregate_status: RealBusinessAggregateStatus = "absent"
    scenario_count: int = Field(ge=3, le=3)
    scenarios: list[PublicProofScenarioSummaryV1]
    identified_opportunity_minor: dict[str, Decimal]
    observed_count: int = Field(ge=0)
    modeled_count: int = Field(ge=0)
    unquantified_count: int = Field(ge=0)
    event_count: int = Field(ge=0)
    play_count: int = Field(ge=0)
    card_count: int = Field(ge=0)
    resolved_customer_count: int = Field(ge=0)
    state: PublicProofStateV1

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

    @field_validator("identified_opportunity_minor", mode="before")
    @classmethod
    def _totals(cls, value: Any) -> dict[str, Decimal]:
        return PublicProofScenarioSummaryV1._totals(value)

    @model_validator(mode="after")
    def _honest(self) -> Self:
        ids = [item.scenario_id for item in self.scenarios]
        if ids != list(CANONICAL_PUBLIC_PROOF_SCENARIOS):
            raise ValueError("aggregate must list the three canonical synthetic scenarios")
        if self.real_business_aggregate != "absent" or self.fm036_aggregate_status != "absent":
            raise ValueError("aggregate must keep FM-036 evidence absent")
        blob = json.dumps(self.canonical_dict(), sort_keys=True)
        _reject_banned_blob(blob)
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
        for row in payload["scenarios"]:
            row["identified_opportunity_minor"] = {
                key: str(value) for key, value in row["identified_opportunity_minor"].items()
            }
        return payload

    def to_canonical_json(self) -> bytes:
        return _canonical_json_bytes(self.canonical_dict())


class PublicProofDisplayedNumberV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    number_id: str
    value: str
    unit: str
    origin: str
    surfaces: list[str]

    @field_validator("number_id", "value", "unit", "origin", mode="before")
    @classmethod
    def _text(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("value must be a string")
        return _normalize_identifier(value)

    @field_validator("surfaces")
    @classmethod
    def _surfaces(cls, value: Any) -> list[str]:
        if not isinstance(value, list) or not value:
            raise ValueError("surfaces must be a non-empty list")
        items = [
            _normalize_relative_path(item) if isinstance(item, str) else item for item in value
        ]
        if any(not isinstance(item, str) for item in items):
            raise ValueError("surfaces must be relative paths")
        if len(items) != len(set(items)):
            raise ValueError("surfaces must be unique")
        return items


class PublicProofRecomputeReportV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["found-money-public-proof-recompute.v1"] = (
        "found-money-public-proof-recompute.v1"
    )
    run_id: str
    matched: Literal[True] = True
    aggregate_sha256: str
    displayed_numbers: list[PublicProofDisplayedNumberV1]
    state: PublicProofStateV1

    @field_validator("run_id", mode="before")
    @classmethod
    def _run(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("run_id must be a string")
        return _normalize_identifier(value)

    @field_validator("aggregate_sha256", mode="before")
    @classmethod
    def _hash(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("hash must be a string")
        return _normalize_hash(value)

    @model_validator(mode="after")
    def _unique_numbers(self) -> Self:
        ids = [item.number_id for item in self.displayed_numbers]
        if not ids:
            raise ValueError("recompute report must list displayed numbers")
        if len(ids) != len(set(ids)):
            raise ValueError("displayed number ids must be unique")
        return self

    def canonical_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="python")

    def to_canonical_json(self) -> bytes:
        return _canonical_json_bytes(self.canonical_dict())


class PublicProofAuditCheckV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    check_id: str
    rubric: str
    passed: bool
    finding: str | None = None

    @field_validator("check_id", "rubric", mode="before")
    @classmethod
    def _text(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("value must be a string")
        return _normalize_identifier(value)

    @field_validator("finding", mode="before")
    @classmethod
    def _finding(cls, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("finding must be a string or null")
        text = value.strip()
        if not text:
            raise ValueError("finding must be null or non-empty")
        return text

    @model_validator(mode="after")
    def _pass_finding(self) -> Self:
        if self.passed:
            if self.finding is not None:
                raise ValueError("passed check must have a null finding")
        elif self.finding is None:
            raise ValueError("failed check requires an actionable finding")
        return self


class PublicProofAuditV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["found-money-public-proof-audit.v1"] = (
        "found-money-public-proof-audit.v1"
    )
    run_id: str
    source_set_hash: str
    audited_text_tree_sha256: str
    audited_text_tree_sha256_by_platform: dict[str, str]
    audited_binary_tree_sha256_by_platform: dict[str, str]
    audited_text_file_count: int = Field(ge=1)
    binary_extractions_sha256: str
    audited_pdf_count: int = Field(ge=1)
    audited_png_count: int = Field(ge=1)
    reviewer_runtime: str
    reviewer_model_family: str
    passed: bool
    checks: list[PublicProofAuditCheckV1]
    actionable_findings: list[str] = Field(default_factory=list)
    claims_human_review: Literal[False] = False
    claims_new_visual_baseline: Literal[False] = False

    @field_validator("run_id", "reviewer_runtime", "reviewer_model_family", mode="before")
    @classmethod
    def _ids(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("value must be a string")
        return _normalize_identifier(value)

    @field_validator(
        "source_set_hash",
        "audited_text_tree_sha256",
        "binary_extractions_sha256",
        mode="before",
    )
    @classmethod
    def _hashes(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("hash must be a string")
        return _normalize_hash(value)

    @field_validator(
        "audited_text_tree_sha256_by_platform",
        "audited_binary_tree_sha256_by_platform",
        mode="before",
    )
    @classmethod
    def _platform_hashes(cls, value: Any) -> dict[str, str]:
        if not isinstance(value, Mapping) or not value:
            raise ValueError("platform digest map must be a non-empty object")
        extra = set(value) - AUDITED_PUBLIC_PROOF_PLATFORMS
        if extra:
            raise ValueError("unsupported audit platform")
        out: dict[str, str] = {}
        for key, raw in value.items():
            if not isinstance(key, str) or not isinstance(raw, str):
                raise ValueError("platform digest entries must be strings")
            out[key] = _normalize_hash(raw)
        if set(out) != AUDITED_PUBLIC_PROOF_PLATFORMS:
            raise ValueError("audit must cover linux and macos")
        return out

    @field_validator("actionable_findings", mode="before")
    @classmethod
    def _findings(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError("actionable_findings must be a list")
        items = [_normalize_identifier(item) if isinstance(item, str) else item for item in value]
        if any(not isinstance(item, str) for item in items):
            raise ValueError("actionable findings must be strings")
        if len(items) != len(set(items)):
            raise ValueError("actionable findings must be unique")
        return items

    @model_validator(mode="after")
    def _coverage(self) -> Self:
        if self.audited_text_tree_sha256_by_platform["macos"] != self.audited_text_tree_sha256:
            raise ValueError("legacy text tree hash must equal the macOS audited digest")
        if self.audited_text_file_count != PUBLIC_PROOF_AUDIT_TEXT_FILE_COUNT:
            raise ValueError("audit must declare the locked public-proof text-file count")
        if self.audited_pdf_count != PUBLIC_PROOF_AUDIT_PDF_COUNT:
            raise ValueError("audit must declare the locked public-proof PDF count")
        if self.audited_png_count != PUBLIC_PROOF_AUDIT_PNG_COUNT:
            raise ValueError("audit must declare the locked public-proof PNG count")
        ids = [item.check_id for item in self.checks]
        if ids != list(PUBLIC_PROOF_AUDIT_CHECK_IDS):
            raise ValueError("audit must exactly cover the required check_ids")
        derived = all(item.passed for item in self.checks)
        if self.passed != derived:
            raise ValueError("audit pass must be derived from checks")
        if derived:
            if self.actionable_findings:
                raise ValueError("passed audit must not declare actionable findings")
        elif not self.actionable_findings:
            raise ValueError("failed audit requires actionable findings")
        if self.claims_human_review is not False:
            raise ValueError("Cursor Auto audit cannot claim human review")
        if self.claims_new_visual_baseline is not False:
            raise ValueError("audit must not ratify a visual baseline")
        return self

    def canonical_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="python")

    def to_canonical_json(self) -> bytes:
        return _canonical_json_bytes(self.canonical_dict())


class PublicProofBinaryExtractionsV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["found-money-public-proof-binary-extractions.v1"] = (
        "found-money-public-proof-binary-extractions.v1"
    )
    pdfs: list[BinaryAuditPdfExtractionV1]
    pngs: list[BinaryAuditPngExtractionV1]

    @model_validator(mode="after")
    def _unique_paths(self) -> Self:
        paths = [item.path for item in self.pdfs] + [item.path for item in self.pngs]
        if len(paths) != len(set(paths)):
            raise ValueError("binary extraction paths must be unique")
        if not self.pdfs or not self.pngs:
            raise ValueError("binary extractions must include PDF and PNG proof")
        return self

    def semantic_dict(self) -> dict[str, Any]:
        return {
            "pdfs": [item.semantic_dict() for item in self.pdfs],
            "pngs": [item.semantic_dict() for item in self.pngs],
            "schema_version": self.schema_version,
        }

    def canonical_dict(self) -> dict[str, Any]:
        payload = self.model_dump(mode="python")
        payload["pdfs"] = [item.model_dump(mode="python") for item in self.pdfs]
        payload["pngs"] = [
            {
                **item.model_dump(mode="python"),
                "size": [item.size[0], item.size[1]],
            }
            for item in self.pngs
        ]
        return payload

    def to_canonical_json(self) -> bytes:
        return _canonical_json_bytes(self.canonical_dict())


def parse_public_proof_config(data: bytes) -> PublicProofConfigV1:
    return _parse_canonical(data, PublicProofConfigV1)


def parse_public_proof_manifest(data: bytes) -> PublicProofManifestV1:
    return _parse_canonical(data, PublicProofManifestV1)


def parse_public_proof_aggregate(data: bytes) -> PublicProofAggregateV1:
    return _parse_canonical(data, PublicProofAggregateV1)


def parse_public_proof_recompute(data: bytes) -> PublicProofRecomputeReportV1:
    return _parse_canonical(data, PublicProofRecomputeReportV1)


def parse_public_proof_audit(data: bytes) -> PublicProofAuditV1:
    return _parse_canonical(data, PublicProofAuditV1)


def parse_public_proof_binary_extractions(data: bytes) -> PublicProofBinaryExtractionsV1:
    payload = json.loads(data.decode("utf-8"), object_pairs_hook=_object_pairs_hook)
    model = PublicProofBinaryExtractionsV1.model_validate(payload)
    if model.to_canonical_json() != bytes(data):
        raise ValueError("serialized bytes are not exactly canonical")
    return model
