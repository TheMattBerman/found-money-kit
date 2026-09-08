"""Service source/proper-noun H-audit contracts."""

from __future__ import annotations

import json
import re
from typing import Any, Literal, Mapping, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TRAVERSAL_RE = re.compile(r"(^|/)\.\.(/|$)")
_WINDOWS_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")

SERVICE_SOURCE_PROPER_NOUN_AUDIT_SCHEMA = "service-source-proper-noun-audit.v1"
BINARY_AUDIT_EXTRACTIONS_SCHEMA = "binary-audit-extractions.v1"
SERVICE_SOURCE_PROPER_NOUN_AUDIT_CHECK_IDS = (
    "raw_source_identifier_absence",
    "customer_identity_absence",
    "proper_noun_absence",
    "credential_endpoint_path_absence",
    "prior_scenario_copy_absence",
    "aggregate_only_public_projection",
)
SERVICE_SOURCE_AUDIT_TEXT_FILE_COUNT = 59
SERVICE_SOURCE_AUDIT_PDF_COUNT = 4
SERVICE_SOURCE_AUDIT_PNG_COUNT = 30
SERVICE_SOURCE_AUDIT_EXTRACTIONS_SHA256 = (
    "d9a2a4188f46618d3bf326efcb84e237e7415a42daef401e6f1ef7ec7e8bb921"
)
AUDITED_SOURCE_AUDIT_PLATFORMS = frozenset({"linux", "macos"})
PUBLIC_AUDIT_TEXT_SUFFIXES = frozenset(
    {
        ".json",
        ".html",
        ".htm",
        ".txt",
        ".md",
        ".csv",
        ".log",
        ".yml",
        ".yaml",
        ".toml",
        ".xml",
        ".css",
        ".js",
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


def _canonical_json_bytes(payload: Any) -> bytes:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return (text + "\n").encode("utf-8")


class ServiceSourceProperNounAuditCheckV1(BaseModel):
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


class ServiceSourceProperNounAuditV1(BaseModel):
    """Locked extra-forbid H-audit for the canonical service public tree."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["service-source-proper-noun-audit.v1"] = (
        "service-source-proper-noun-audit.v1"
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
    checks: list[ServiceSourceProperNounAuditCheckV1]
    actionable_findings: list[str] = Field(default_factory=list)

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
        return _normalize_platform_digest_map(value)

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
        required_platforms = AUDITED_SOURCE_AUDIT_PLATFORMS
        if set(self.audited_text_tree_sha256_by_platform) != required_platforms:
            raise ValueError("text tree must cover all audited platforms")
        if set(self.audited_binary_tree_sha256_by_platform) != required_platforms:
            raise ValueError("binary tree must cover all audited platforms")
        if self.audited_text_tree_sha256_by_platform["macos"] != self.audited_text_tree_sha256:
            raise ValueError("legacy text tree hash must equal the macOS audited digest")
        if self.audited_text_file_count != SERVICE_SOURCE_AUDIT_TEXT_FILE_COUNT:
            raise ValueError("audit must declare the locked service text-file count")
        if self.audited_pdf_count != SERVICE_SOURCE_AUDIT_PDF_COUNT:
            raise ValueError("audit must declare the locked service PDF count")
        if self.audited_png_count != SERVICE_SOURCE_AUDIT_PNG_COUNT:
            raise ValueError("audit must declare the locked service PNG count")
        ids = [item.check_id for item in self.checks]
        if ids != list(SERVICE_SOURCE_PROPER_NOUN_AUDIT_CHECK_IDS):
            raise ValueError("audit must exactly cover the six required check_ids")
        derived = all(item.passed for item in self.checks)
        if self.passed != derived:
            raise ValueError("audit pass must be derived from checks")
        if derived:
            if self.actionable_findings:
                raise ValueError("passed audit must not declare actionable findings")
        elif not self.actionable_findings:
            raise ValueError("failed audit requires actionable findings")
        return self

    def canonical_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="python")

    def to_canonical_json(self) -> bytes:
        return _canonical_json_bytes(self.canonical_dict())


def _normalize_platform_digest_map(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping) or not value:
        raise ValueError("platform digest map must be a non-empty object")
    extra = set(value) - AUDITED_SOURCE_AUDIT_PLATFORMS
    if extra:
        raise ValueError("unsupported audit platform")
    out: dict[str, str] = {}
    for key, raw in value.items():
        if not isinstance(key, str):
            raise ValueError("platform name must be a string")
        out[key] = _normalize_hash(raw if isinstance(raw, str) else raw)
    return out


class BinaryAuditPdfExtractionV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    sha256: str
    page_count: int = Field(ge=1)
    text: str
    metadata: dict[str, str]
    annotations: list[dict[str, Any]]

    @field_validator("path", mode="before")
    @classmethod
    def _path(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("path must be a string")
        return _normalize_relative_path(value)

    @field_validator("sha256", mode="before")
    @classmethod
    def _hash(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("hash must be a string")
        return _normalize_hash(value)

    @field_validator("text", mode="before")
    @classmethod
    def _text(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("text must be a string")
        return value

    @field_validator("metadata", mode="before")
    @classmethod
    def _metadata(cls, value: Any) -> dict[str, str]:
        if not isinstance(value, Mapping):
            raise ValueError("metadata must be an object")
        out: dict[str, str] = {}
        for key, raw in value.items():
            if not isinstance(key, str) or not isinstance(raw, str):
                raise ValueError("metadata entries must be strings")
            out[key] = raw
        return out

    @field_validator("annotations", mode="before")
    @classmethod
    def _annotations(cls, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            raise ValueError("annotations must be a list")
        rows: list[dict[str, Any]] = []
        for item in value:
            if not isinstance(item, Mapping):
                raise ValueError("annotation must be an object")
            extra = set(item) - {"dest", "page", "uri"}
            if extra:
                raise ValueError("annotation has unexpected fields")
            page = item.get("page")
            if not isinstance(page, int) or isinstance(page, bool) or page < 1:
                raise ValueError("annotation page must be a positive integer")
            dest = item.get("dest")
            uri = item.get("uri")
            if dest is not None and not isinstance(dest, str):
                raise ValueError("annotation dest must be a string or null")
            if uri is not None and not isinstance(uri, str):
                raise ValueError("annotation uri must be a string or null")
            rows.append({"dest": dest, "page": page, "uri": uri})
        return rows

    def semantic_dict(self) -> dict[str, Any]:
        return {
            "annotations": self.annotations,
            "metadata": self.metadata,
            "page_count": self.page_count,
            "path": self.path,
            "text": self.text,
        }


class BinaryAuditPngExtractionV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    sha256: str
    metadata: dict[str, str]
    mode: str
    size: tuple[int, int]

    @field_validator("path", mode="before")
    @classmethod
    def _path(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("path must be a string")
        return _normalize_relative_path(value)

    @field_validator("sha256", mode="before")
    @classmethod
    def _hash(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("hash must be a string")
        return _normalize_hash(value)

    @field_validator("mode", mode="before")
    @classmethod
    def _mode(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("mode must be a string")
        return _normalize_identifier(value)

    @field_validator("metadata", mode="before")
    @classmethod
    def _metadata(cls, value: Any) -> dict[str, str]:
        if not isinstance(value, Mapping):
            raise ValueError("metadata must be an object")
        out: dict[str, str] = {}
        for key, raw in value.items():
            if not isinstance(key, str) or not isinstance(raw, str):
                raise ValueError("metadata entries must be strings")
            out[key] = raw
        return out

    @field_validator("size", mode="before")
    @classmethod
    def _size(cls, value: Any) -> tuple[int, int]:
        if isinstance(value, tuple):
            value = list(value)
        if not isinstance(value, list) or len(value) != 2:
            raise ValueError("size must be a two-integer width/height pair")
        width, height = value
        if (
            isinstance(width, bool)
            or isinstance(height, bool)
            or not isinstance(width, int)
            or not isinstance(height, int)
            or width < 1
            or height < 1
        ):
            raise ValueError("size must be positive integers")
        return (width, height)

    def semantic_dict(self) -> dict[str, Any]:
        return {
            "metadata": self.metadata,
            "mode": self.mode,
            "path": self.path,
            "size": [self.size[0], self.size[1]],
        }


class BinaryAuditExtractionsV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["binary-audit-extractions.v1"] = "binary-audit-extractions.v1"
    pdfs: list[BinaryAuditPdfExtractionV1]
    pngs: list[BinaryAuditPngExtractionV1]

    @model_validator(mode="after")
    def _unique_paths(self) -> Self:
        paths = [item.path for item in self.pdfs] + [item.path for item in self.pngs]
        if len(paths) != len(set(paths)):
            raise ValueError("binary extraction paths must be unique")
        if len(self.pdfs) != SERVICE_SOURCE_AUDIT_PDF_COUNT:
            raise ValueError("binary extractions must cover the locked service PDF count")
        if len(self.pngs) != SERVICE_SOURCE_AUDIT_PNG_COUNT:
            raise ValueError("binary extractions must cover the locked service PNG count")
        return self

    def semantic_dict(self) -> dict[str, Any]:
        return {
            "pdfs": [item.semantic_dict() for item in self.pdfs],
            "pngs": [item.semantic_dict() for item in self.pngs],
            "schema_version": self.schema_version,
        }

    def semantic_canonical_json(self) -> bytes:
        return _canonical_json_bytes(self.semantic_dict())


def parse_service_source_proper_noun_audit(data: bytes) -> ServiceSourceProperNounAuditV1:
    payload = json.loads(data.decode("utf-8"), object_pairs_hook=_object_pairs_hook)
    model = ServiceSourceProperNounAuditV1.model_validate(payload)
    if model.to_canonical_json() != bytes(data):
        raise ValueError("serialized bytes are not exactly canonical")
    return model


def parse_binary_audit_extractions(data: bytes) -> BinaryAuditExtractionsV1:
    payload = json.loads(data.decode("utf-8"), object_pairs_hook=_object_pairs_hook)
    return BinaryAuditExtractionsV1.model_validate(payload)
