"""FM-038 release-evidence contracts: fresh-clone, AC ledger, publication review."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Literal, Mapping, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_TRAVERSAL_RE = re.compile(r"(^|/)\.\.(/|$)")
_WINDOWS_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")
_AC_ID_RE = re.compile(r"^AC-(?:[1-9]|[1-3][0-9]|40)$")

EvidenceClass = Literal["T", "C", "R", "B", "H", "L"]
EvidencePresence = Literal["present", "missing", "incomplete"]
CriterionStatus = Literal["incomplete", "complete"]
PublicationReviewStatus = Literal["not_ready", "ready_for_publication_review"]
FreshCloneActor = Literal["ci", "agent"]
CleanMachineStatus = Literal["absent", "recorded"]
UsefulnessLinkStatus = Literal["absent", "linked"]
NaDetermination = Literal["none"]  # workers may never invent N/A
NOT_READY_PACKET_STATUS = "not_ready"
READY_FOR_PUBLICATION_REVIEW_PACKET_STATUS = "ready_for_publication_review"

FRESH_CLONE_RECEIPT_SCHEMA = "found-money-fresh-clone-receipt.v1"
AC_EVIDENCE_LEDGER_SCHEMA = "found-money-ac-evidence-ledger.v1"
PUBLICATION_REVIEW_PACKET_SCHEMA = "found-money-publication-review.v1"
RELEASE_DOC_SCAN_SCHEMA = "found-money-release-doc-scan.v1"
RUNBOOK_VERSION = "clean-machine-runbook.v1"

GLOBAL_AC_IDS = tuple(f"AC-{index}" for index in range(1, 41))
DECLARED_CHECK_NAMES = (
    "quality",
    "unit-and-contract",
    "public-safety",
    "render-proof",
)
CANONICAL_PUBLIC_SCENARIOS = (
    "synthetic-saas-v1",
    "synthetic-ecommerce-v1",
    "synthetic-service-v1",
)
BANNED_FABRICATED_STATES = (
    "n/a",
    "not applicable",
    "sent",
    "recovered",
    "published",
    "ready_for_publication_review",
    "live-verified",
    "useful",
)
# Packet-only child commits may rebind these fixtures after an implementation head.
# found_money/*.py and other implementation sources are never permitted here.
PERMITTED_PACKET_ONLY_DELTA = frozenset(
    {
        "tests/contract/test_safety_fm030.py",
        "tests/fixtures/saas/safety/release-safety-evidence.json",
        "tests/fixtures/release/fm038/ac-evidence-ledger.json",
        "tests/fixtures/release/fm038/publication-review-packet.json",
        "tests/fixtures/release/fm038/fresh-clone-receipt.json",
        "tests/fixtures/release/fm038/release-doc-scan.json",
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


def _normalize_commit(value: str) -> str:
    text = value.strip().lower()
    if not _COMMIT_RE.fullmatch(text):
        raise ValueError("commit hash must be a lowercase 40-char hex digest")
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


def _normalize_evidence_class(value: Any) -> EvidenceClass:
    if not isinstance(value, str):
        raise ValueError("evidence class must be a string")
    text = value.strip().upper()
    if text not in {"T", "C", "R", "B", "H", "L"}:
        raise ValueError("evidence class must be one of T,C,R,B,H,L")
    return text  # type: ignore[return-value]


class FreshCloneStepV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_id: str
    exit_code: int
    sha256: str

    @field_validator("step_id", mode="before")
    @classmethod
    def _step_id(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("step_id must be a string")
        return _normalize_identifier(value)

    @field_validator("exit_code")
    @classmethod
    def _exit(cls, value: int) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError("exit_code must be a non-negative int")
        return value

    @field_validator("sha256", mode="before")
    @classmethod
    def _hash(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("sha256 must be a string")
        return _normalize_hash(value)


class FreshCloneReceiptV1(BaseModel):
    """Redacted deterministic receipt for an isolated fresh-clone proof."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["found-money-fresh-clone-receipt.v1"] = (
        "found-money-fresh-clone-receipt.v1"
    )
    runbook_version: Literal["clean-machine-runbook.v1"] = "clean-machine-runbook.v1"
    actor: FreshCloneActor
    claims_human_clean_machine: Literal[False] = False
    exact_head: str
    python_version: str
    uv_version: str
    supported_python: str
    built_at: datetime
    steps: list[FreshCloneStepV1]
    scenario_manifest_hashes: dict[str, str]
    public_proof_manifest_sha256: str | None = None
    doc_scan_sha256: str
    overall_exit_code: int
    redacted: Literal[True] = True
    # doc_scan_sha256 is required; public_proof may be null only when that step failed.

    @field_validator("exact_head", mode="before")
    @classmethod
    def _head(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("exact_head must be a string")
        return _normalize_commit(value)

    @field_validator("python_version", "uv_version", "supported_python", mode="before")
    @classmethod
    def _versions(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("version fields must be strings")
        return _normalize_identifier(value)

    @field_validator("built_at", mode="before")
    @classmethod
    def _built(cls, value: Any) -> datetime:
        return _parse_utc(value)

    @field_validator("scenario_manifest_hashes", mode="before")
    @classmethod
    def _scenario_hashes(cls, value: Any) -> dict[str, str]:
        if not isinstance(value, Mapping):
            raise ValueError("scenario_manifest_hashes must be an object")
        out: dict[str, str] = {}
        for key, raw in value.items():
            if not isinstance(key, str) or not isinstance(raw, str):
                raise ValueError("scenario hash entries must be strings")
            out[_normalize_identifier(key)] = _normalize_hash(raw)
        if set(out) != set(CANONICAL_PUBLIC_SCENARIOS):
            raise ValueError("scenario_manifest_hashes must cover the three canonical scenarios")
        return {name: out[name] for name in CANONICAL_PUBLIC_SCENARIOS}

    @field_validator("public_proof_manifest_sha256", mode="before")
    @classmethod
    def _optional_hash(cls, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("hash must be a string or null")
        return _normalize_hash(value)

    @field_validator("doc_scan_sha256", mode="before")
    @classmethod
    def _required_hash(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("doc_scan_sha256 must be a string")
        return _normalize_hash(value)

    @field_validator("overall_exit_code")
    @classmethod
    def _overall(cls, value: int) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError("overall_exit_code must be a non-negative int")
        return value

    @model_validator(mode="after")
    def _honesty(self) -> Self:
        if self.actor not in {"ci", "agent"}:
            raise ValueError("fresh-clone receipt actor must be ci or agent")
        if self.claims_human_clean_machine is not False:
            raise ValueError("receipt may not claim human clean-machine H evidence")
        if not self.steps:
            raise ValueError("fresh-clone receipt requires recorded steps")
        names = [step.step_id for step in self.steps]
        if len(names) != len(set(names)):
            raise ValueError("fresh-clone step ids must be unique")
        return self

    def canonical_dict(self) -> dict[str, Any]:
        payload = self.model_dump(mode="python")
        payload["built_at"] = _format_utc(payload["built_at"])
        return payload

    def to_canonical_json(self) -> bytes:
        return _canonical_json_bytes(self.canonical_dict())


class AcEvidenceLinkV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_class: EvidenceClass
    presence: EvidencePresence
    path: str | None = None
    note: str
    sha256: str | None = None

    @field_validator("evidence_class", mode="before")
    @classmethod
    def _klass(cls, value: Any) -> EvidenceClass:
        return _normalize_evidence_class(value)

    @field_validator("path", mode="before")
    @classmethod
    def _path(cls, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("path must be a string or null")
        return _normalize_relative_path(value)

    @field_validator("note", mode="before")
    @classmethod
    def _note(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("note must be a string")
        text = value.strip()
        if not text:
            raise ValueError("note must be non-empty")
        lowered = text.casefold()
        fabricated = (
            "marked complete without evidence",
            "self-approved",
            "n/a approved",
            "not applicable approved",
        )
        if any(token in lowered for token in fabricated):
            raise ValueError("evidence note must not fabricate completion state")
        return text

    @field_validator("sha256", mode="before")
    @classmethod
    def _hash(cls, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("sha256 must be a string or null")
        return _normalize_hash(value)

    @model_validator(mode="after")
    def _consistency(self) -> Self:
        if self.presence == "present":
            if self.path is None or self.sha256 is None:
                raise ValueError("present evidence requires path and sha256")
        if self.presence in {"missing", "incomplete"} and self.sha256 is not None:
            raise ValueError("missing/incomplete evidence must not carry a content hash")
        if self.presence == "missing" and self.path is not None:
            raise ValueError("missing evidence must not cite a path")
        return self


class AcEvidenceEntryV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ac_id: str
    required_evidence: list[EvidenceClass]
    links: list[AcEvidenceLinkV1]
    status: CriterionStatus
    na_determination: NaDetermination = "none"

    @field_validator("ac_id", mode="before")
    @classmethod
    def _ac(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("ac_id must be a string")
        text = value.strip().upper()
        if not _AC_ID_RE.fullmatch(text):
            raise ValueError("ac_id must be AC-1 through AC-40")
        return text

    @field_validator("required_evidence", mode="before")
    @classmethod
    def _required(cls, value: Any) -> list[EvidenceClass]:
        if not isinstance(value, list) or not value:
            raise ValueError("required_evidence must be a non-empty list")
        items = [_normalize_evidence_class(item) for item in value]
        if len(items) != len(set(items)):
            raise ValueError("required_evidence classes must be unique")
        return items

    @model_validator(mode="after")
    def _honesty(self) -> Self:
        if self.na_determination != "none":
            raise ValueError("workers may not invent N/A determinations")
        linked_classes = [link.evidence_class for link in self.links]
        if set(linked_classes) != set(self.required_evidence):
            raise ValueError("links must cover exactly the required evidence classes")
        if len(linked_classes) != len(set(linked_classes)):
            raise ValueError("links must not duplicate evidence classes")
        present = {link.evidence_class for link in self.links if link.presence == "present"}
        expected_complete = present == set(self.required_evidence)
        if expected_complete and self.status != "complete":
            raise ValueError("all required evidence present requires status=complete")
        if not expected_complete and self.status != "incomplete":
            raise ValueError("missing required evidence must remain incomplete")
        return self


class AcEvidenceLedgerV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["found-money-ac-evidence-ledger.v1"] = (
        "found-money-ac-evidence-ledger.v1"
    )
    acceptance_doc_path: str
    acceptance_doc_sha256: str
    built_at: datetime
    exact_head: str
    entries: list[AcEvidenceEntryV1]
    complete_count: int
    incomplete_count: int
    claims_publication: Literal[False] = False
    invents_na: Literal[False] = False

    @field_validator("acceptance_doc_path", mode="before")
    @classmethod
    def _doc_path(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("acceptance_doc_path must be a string")
        return _normalize_relative_path(value)

    @field_validator("acceptance_doc_sha256", mode="before")
    @classmethod
    def _doc_hash(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("acceptance_doc_sha256 must be a string")
        return _normalize_hash(value)

    @field_validator("built_at", mode="before")
    @classmethod
    def _built(cls, value: Any) -> datetime:
        return _parse_utc(value)

    @field_validator("exact_head", mode="before")
    @classmethod
    def _head(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("exact_head must be a string")
        return _normalize_commit(value)

    @model_validator(mode="after")
    def _coverage(self) -> Self:
        ids = [entry.ac_id for entry in self.entries]
        if tuple(ids) != GLOBAL_AC_IDS:
            raise ValueError("ledger must include AC-1 through AC-40 in order")
        complete = sum(1 for entry in self.entries if entry.status == "complete")
        incomplete = sum(1 for entry in self.entries if entry.status == "incomplete")
        if self.complete_count != complete or self.incomplete_count != incomplete:
            raise ValueError("ledger complete/incomplete counts must match entries")
        if complete + incomplete != 40:
            raise ValueError("ledger must account for all 40 criteria")
        return self

    def canonical_dict(self) -> dict[str, Any]:
        payload = self.model_dump(mode="python")
        payload["built_at"] = _format_utc(payload["built_at"])
        return payload

    def to_canonical_json(self) -> bytes:
        return _canonical_json_bytes(self.canonical_dict())


class PublicationReviewReferenceV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    path: str | None = None
    sha256: str | None = None
    status: EvidencePresence
    note: str

    @field_validator("label", "note", mode="before")
    @classmethod
    def _text(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("text fields must be strings")
        text = value.strip()
        if not text:
            raise ValueError("text fields must be non-empty")
        return text

    @field_validator("path", mode="before")
    @classmethod
    def _path(cls, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("path must be a string or null")
        return _normalize_relative_path(value)

    @field_validator("sha256", mode="before")
    @classmethod
    def _hash(cls, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("sha256 must be a string or null")
        return _normalize_hash(value)

    @model_validator(mode="after")
    def _consistency(self) -> Self:
        if self.status == "present" and (self.path is None or self.sha256 is None):
            raise ValueError("present references require path and sha256")
        if self.status == "missing" and (self.path is not None or self.sha256 is not None):
            raise ValueError("missing references must not cite path or hash")
        return self


class PublicationReviewPacketV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["found-money-publication-review.v1"] = (
        "found-money-publication-review.v1"
    )
    built_at: datetime
    exact_head: str
    status: PublicationReviewStatus
    runbook_version: Literal["clean-machine-runbook.v1"] = "clean-machine-runbook.v1"
    declared_checks: list[str]
    ledger_sha256: str
    fresh_clone_receipt_sha256: str | None = None
    references: list[PublicationReviewReferenceV1]
    fm036_usefulness_verdict: UsefulnessLinkStatus
    human_clean_machine_dry_run: CleanMachineStatus
    missing_gates: list[str]
    claims_publication: Literal[False] = False
    claims_sent: Literal[False] = False
    claims_recovered: Literal[False] = False
    # Observed exact-output receipts for the declared checks. Required when a packet
    # claims ready_for_publication_review; never synthesized or inferred.
    check_evidence: dict[str, str] = Field(default_factory=dict)
    check_hashes: dict[str, str] = Field(default_factory=dict)

    @field_validator("built_at", mode="before")
    @classmethod
    def _built(cls, value: Any) -> datetime:
        return _parse_utc(value)

    @field_validator("exact_head", mode="before")
    @classmethod
    def _head(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("exact_head must be a string")
        return _normalize_commit(value)

    @field_validator("declared_checks", mode="before")
    @classmethod
    def _checks(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            raise ValueError("declared_checks must be a list")
        items = [_normalize_identifier(item) if isinstance(item, str) else item for item in value]
        if items != list(DECLARED_CHECK_NAMES):
            raise ValueError("declared_checks must match the five required checks")
        return items

    @field_validator("ledger_sha256", "fresh_clone_receipt_sha256", mode="before")
    @classmethod
    def _hashes(cls, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("hash must be a string or null")
        return _normalize_hash(value)

    @field_validator("missing_gates", mode="before")
    @classmethod
    def _gates(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            raise ValueError("missing_gates must be a list")
        out = [_normalize_identifier(item) if isinstance(item, str) else item for item in value]
        if len(out) != len(set(out)):
            raise ValueError("missing_gates must be unique")
        return out

    @field_validator("check_evidence", mode="before")
    @classmethod
    def _check_evidence(cls, value: Any) -> dict[str, str]:
        if not isinstance(value, Mapping):
            raise ValueError("check_evidence must be an object")
        out: dict[str, str] = {}
        for key, raw in value.items():
            if not isinstance(key, str) or not isinstance(raw, str):
                raise ValueError("check_evidence entries must be strings")
            out[_normalize_identifier(key)] = raw
        return out

    @field_validator("check_hashes", mode="before")
    @classmethod
    def _check_hashes(cls, value: Any) -> dict[str, str]:
        if not isinstance(value, Mapping):
            raise ValueError("check_hashes must be an object")
        out: dict[str, str] = {}
        for key, raw in value.items():
            if not isinstance(key, str) or not isinstance(raw, str):
                raise ValueError("check_hashes entries must be strings")
            out[_normalize_identifier(key)] = _normalize_hash(raw)
        return out

    @model_validator(mode="after")
    def _honesty(self) -> Self:
        if self.ledger_sha256 is None:
            raise ValueError("ledger_sha256 is required")
        if self.status == "ready_for_publication_review":
            if self.missing_gates:
                raise ValueError("ready status forbids missing gates")
            if self.fm036_usefulness_verdict != "linked":
                raise ValueError("ready status requires linked FM-036 usefulness verdict")
            if self.human_clean_machine_dry_run != "recorded":
                raise ValueError("ready status requires recorded human clean-machine dry run")
            if set(self.check_evidence) != set(DECLARED_CHECK_NAMES):
                raise ValueError("ready status requires observed evidence for every declared check")
            for name, value in self.check_evidence.items():
                if not value.strip():
                    raise ValueError(f"ready status requires non-blank evidence for {name}")
            if set(self.check_hashes) != set(DECLARED_CHECK_NAMES):
                raise ValueError("ready status requires hashes for every declared check")
            import hashlib

            for name, digest in self.check_hashes.items():
                expected = hashlib.sha256(self.check_evidence[name].encode("utf-8")).hexdigest()
                if digest != expected:
                    raise ValueError(
                        f"ready status check hash for {name} does not match the recorded evidence"
                    )
        else:
            if not self.missing_gates:
                raise ValueError("not_ready status requires explicit missing gates")
            if self.check_evidence or self.check_hashes:
                raise ValueError("not_ready status forbids check evidence")
            if (
                self.fm036_usefulness_verdict == "absent"
                and "fm036_usefulness_verdict" not in self.missing_gates
            ):
                raise ValueError("absent FM-036 usefulness must be listed in missing_gates")
            if (
                self.human_clean_machine_dry_run == "absent"
                and "human_clean_machine_dry_run" not in self.missing_gates
            ):
                raise ValueError("absent human clean-machine dry run must be listed")
        labels = [item.label for item in self.references]
        if len(labels) != len(set(labels)):
            raise ValueError("reference labels must be unique")
        return self

    def canonical_dict(self) -> dict[str, Any]:
        payload = self.model_dump(mode="python")
        payload["built_at"] = _format_utc(payload["built_at"])
        # Empty check maps stay out of the canonical form so existing not_ready
        # fixtures remain byte-identical; only ready packets carry evidence.
        if not payload.get("check_evidence"):
            payload.pop("check_evidence", None)
        if not payload.get("check_hashes"):
            payload.pop("check_hashes", None)
        return payload

    def to_canonical_json(self) -> bytes:
        return _canonical_json_bytes(self.canonical_dict())


class ReleaseDocScanFindingV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    category: str
    excerpt: str

    @field_validator("path", mode="before")
    @classmethod
    def _path(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("path must be a string")
        return _normalize_relative_path(value)

    @field_validator("category", "excerpt", mode="before")
    @classmethod
    def _text(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("text fields must be strings")
        text = value.strip()
        if not text:
            raise ValueError("text fields must be non-empty")
        return text


class ReleaseDocScanReportV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["found-money-release-doc-scan.v1"] = "found-money-release-doc-scan.v1"
    built_at: datetime
    scanned_paths: list[str]
    findings: list[ReleaseDocScanFindingV1]
    clean: bool

    @field_validator("built_at", mode="before")
    @classmethod
    def _built(cls, value: Any) -> datetime:
        return _parse_utc(value)

    @field_validator("scanned_paths", mode="before")
    @classmethod
    def _paths(cls, value: Any) -> list[str]:
        if not isinstance(value, list) or not value:
            raise ValueError("scanned_paths must be a non-empty list")
        return [_normalize_relative_path(item) if isinstance(item, str) else item for item in value]

    @model_validator(mode="after")
    def _clean_flag(self) -> Self:
        expected = not self.findings
        if self.clean != expected:
            raise ValueError("clean flag must match findings emptiness")
        return self

    def canonical_dict(self) -> dict[str, Any]:
        payload = self.model_dump(mode="python")
        payload["built_at"] = _format_utc(payload["built_at"])
        return payload

    def to_canonical_json(self) -> bytes:
        return _canonical_json_bytes(self.canonical_dict())


def parse_fresh_clone_receipt(data: bytes) -> FreshCloneReceiptV1:
    return _parse_canonical(data, FreshCloneReceiptV1)


def parse_ac_evidence_ledger(data: bytes) -> AcEvidenceLedgerV1:
    return _parse_canonical(data, AcEvidenceLedgerV1)


def parse_publication_review_packet(data: bytes) -> PublicationReviewPacketV1:
    return _parse_canonical(data, PublicationReviewPacketV1)


def parse_release_doc_scan_report(data: bytes) -> ReleaseDocScanReportV1:
    return _parse_canonical(data, ReleaseDocScanReportV1)
