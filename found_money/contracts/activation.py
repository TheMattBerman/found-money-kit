"""Activation launch-pack contracts (FM-031)."""

from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TRAVERSAL_RE = re.compile(r"(^|/)\.\.(/|$)")
_WINDOWS_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")

LaunchPackMode = Literal["public", "private"]
LaunchPackStatus = Literal["approval_ready", "partial", "withheld"]
AssetStatus = Literal["ready", "withheld"]
ExclusionStatus = Literal["included"]
ActivationChannel = Literal["email", "sms", "task"]
MemberValueBasisKind = Literal["observed_face_value", "modeled_opportunity", "unquantified"]

PAYMENT_RESCUE_SEGMENT_ID = "seg_payment_rescue"
PAYMENT_RESCUE_PILE_ID = "payment_rescue"


def _normalize_identifier(value: Any, *, field_name: str = "identifier") -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    text = value.strip()
    if not text:
        raise ValueError(f"{field_name} must be non-empty")
    return text


def _normalize_hash(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("hash must be a string")
    text = value.strip().lower()
    if not _SHA256_RE.fullmatch(text):
        raise ValueError("hash must be a lowercase 64-char SHA-256 hex digest")
    return text


def _normalize_relative_path(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("path must be a string")
    text = value.strip().replace("\\", "/")
    if text.startswith("./"):
        text = text[2:]
    if (
        not text
        or text == "."
        or text.startswith("/")
        or text.startswith("~/")
        or text.endswith("/")
        or "//" in text
        or _WINDOWS_ABS_RE.match(text)
        or text == ".."
        or _TRAVERSAL_RE.search(text)
        or "?" in text
        or "#" in text
        or "%" in text
    ):
        raise ValueError("path must be relative, canonical, and traversal-free")
    return text


def _canonical_json_bytes(payload: Any) -> bytes:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return (text + "\n").encode("utf-8")


class CanonicalModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    def canonical_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="python")

    def to_canonical_json(self) -> bytes:
        return _canonical_json_bytes(self.canonical_dict())


class ActivationSourceObjectRuleV1(CanonicalModel):
    """One allowed or required source object type for activation export."""

    source_system: str
    object_type: str

    @field_validator("source_system", "object_type", mode="before")
    @classmethod
    def _ids(cls, value: Any) -> str:
        return _normalize_identifier(value)


class ActivationPolicyV1(CanonicalModel):
    """Strict export policy: only required platform IDs for configured channels."""

    schema_version: Literal["activation-policy.v1"] = "activation-policy.v1"
    channels: list[ActivationChannel] = Field(min_length=1)
    required_source_objects: list[ActivationSourceObjectRuleV1] = Field(min_length=1)
    allowed_source_objects: list[ActivationSourceObjectRuleV1] = Field(min_length=1)
    personalization_field_allowlist: list[str] = Field(default_factory=list)

    @field_validator("channels", mode="before")
    @classmethod
    def _channels(cls, value: Any) -> list[str]:
        if not isinstance(value, list) or not value:
            raise ValueError("channels must be a non-empty list")
        allowed = {"email", "sms", "task"}
        channels = sorted({_normalize_identifier(item) for item in value})
        if any(item not in allowed for item in channels):
            raise ValueError("activation channel is unsupported")
        return channels

    @field_validator("personalization_field_allowlist", mode="before")
    @classmethod
    def _fields(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError("personalization_field_allowlist must be a list")
        fields = sorted({_normalize_identifier(item) for item in value})
        # Field names only — never treat activation channel names as fields.
        forbidden_channels = {"email", "sms", "task"}
        if any(item in forbidden_channels for item in fields):
            raise ValueError(
                "personalization allowlist must use approved field names, not channels"
            )
        return fields

    @model_validator(mode="after")
    def _allowed_covers_required(self) -> Self:
        allowed = {(item.source_system, item.object_type) for item in self.allowed_source_objects}
        required = {(item.source_system, item.object_type) for item in self.required_source_objects}
        if not required.issubset(allowed):
            raise ValueError("required_source_objects must be a subset of allowed_source_objects")
        return self

    def canonical_dict(self) -> dict[str, Any]:
        payload = self.model_dump(mode="python")
        payload["channels"] = sorted(payload["channels"])
        payload["personalization_field_allowlist"] = sorted(
            payload["personalization_field_allowlist"]
        )
        payload["required_source_objects"] = sorted(
            payload["required_source_objects"],
            key=lambda item: (item["source_system"], item["object_type"]),
        )
        payload["allowed_source_objects"] = sorted(
            payload["allowed_source_objects"],
            key=lambda item: (item["source_system"], item["object_type"]),
        )
        return payload


def canonical_email_task_activation_policy() -> ActivationPolicyV1:
    """Canonical V1 email/task profile: HubSpot contact IDs only."""
    hubspot_contact = ActivationSourceObjectRuleV1(
        source_system="hubspot",
        object_type="contact",
    )
    return ActivationPolicyV1(
        channels=["email", "task"],
        required_source_objects=[hubspot_contact],
        allowed_source_objects=[hubspot_contact],
        # Explicit field allowlist only; never infer first_name/email from payloads.
        personalization_field_allowlist=[],
    )


class ActivationSourceIdV1(CanonicalModel):
    """One source identifier required for private activation membership."""

    source_system: str
    object_type: str
    source_id: str

    @field_validator("source_system", "object_type", "source_id", mode="before")
    @classmethod
    def _ids(cls, value: Any) -> str:
        return _normalize_identifier(value)


class MemberValueBasisV1(CanonicalModel):
    """Honest typed value basis: quantified amount or explicit unquantified."""

    basis: MemberValueBasisKind
    currency: str | None = None
    amount_minor: str | None = None

    @field_validator("basis", mode="before")
    @classmethod
    def _basis(cls, value: Any) -> str:
        return _normalize_identifier(value)

    @field_validator("currency", mode="before")
    @classmethod
    def _currency(cls, value: Any) -> str | None:
        if value is None:
            return None
        return _normalize_identifier(value).lower()

    @field_validator("amount_minor", mode="before")
    @classmethod
    def _amount(cls, value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, bool):
            raise ValueError("amount_minor must be numeric")
        if isinstance(value, Decimal):
            text = format(value, "f")
        elif isinstance(value, int):
            text = str(value)
        elif isinstance(value, str):
            text = value.strip()
        else:
            raise ValueError("amount_minor must be a decimal string")
        if not text:
            raise ValueError("amount_minor must be non-empty when present")
        amount = Decimal(text)
        if amount < 0:
            raise ValueError("amount_minor must be non-negative")
        return format(amount, "f")

    @model_validator(mode="after")
    def _coherence(self) -> Self:
        if self.basis == "unquantified":
            if self.currency is not None or self.amount_minor is not None:
                raise ValueError("unquantified value basis cannot carry currency or amount")
            return self
        if self.currency is None or self.amount_minor is None:
            raise ValueError("quantified value basis requires currency and amount_minor")
        # Never synthesize a zero stand-in for missing source value.
        if Decimal(self.amount_minor) == 0:
            raise ValueError("quantified value basis must not synthesize zero")
        return self


class IdentityOverrideBindingV1(CanonicalModel):
    """Explicit binding to one FM-017 override envelope by canonical hash/reference."""

    target_cluster_hash: str
    integrity: str
    member_node_ids: list[str] = Field(min_length=1)

    @field_validator("target_cluster_hash", "integrity", mode="before")
    @classmethod
    def _hashes(cls, value: Any) -> str:
        return _normalize_hash(value)

    @field_validator("member_node_ids", mode="before")
    @classmethod
    def _members(cls, value: Any) -> list[str]:
        if not isinstance(value, list) or not value:
            raise ValueError("member_node_ids must be a non-empty list")
        return sorted({_normalize_identifier(item) for item in value})


class PrivateSegmentMemberV1(CanonicalModel):
    """Eligible private activation row only; excluded/ambiguous members are absent."""

    segment_id: str
    play_ids: list[str] = Field(min_length=1)
    customer_token: str
    source_ids: list[ActivationSourceIdV1] = Field(min_length=1)
    value_basis: MemberValueBasisV1
    personalization_allowlist: list[str]
    exclusion_status: ExclusionStatus = "included"

    @field_validator("segment_id", "customer_token", mode="before")
    @classmethod
    def _text(cls, value: Any) -> str:
        return _normalize_identifier(value)

    @field_validator("play_ids", mode="before")
    @classmethod
    def _play_ids(cls, value: Any) -> list[str]:
        if not isinstance(value, list) or not value:
            raise ValueError("play_ids must be a non-empty list")
        plays = [_normalize_identifier(item) for item in value]
        if len(plays) != len(set(plays)):
            raise ValueError("play_ids must be unique")
        return plays

    @field_validator("personalization_allowlist", mode="before")
    @classmethod
    def _allowlist(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            raise ValueError("personalization_allowlist must be a list")
        fields = sorted({_normalize_identifier(item) for item in value})
        forbidden = {"email", "sms", "task"}
        if any(item in forbidden for item in fields):
            raise ValueError("personalization_allowlist contains channel names")
        return fields

    @model_validator(mode="after")
    def _eligible_only(self) -> Self:
        if self.exclusion_status != "included":
            raise ValueError("private segment members must be eligible/included only")
        return self


class PrivateSegmentFileV1(CanonicalModel):
    schema_version: Literal["activation-segment-private.v1"] = "activation-segment-private.v1"
    run_id: str
    segment_id: str
    play_ids: list[str] = Field(min_length=1)
    money_map_sha256: str
    recovery_plays_sha256: str
    evidence_packet_sha256: str
    value_ledger_sha256: str
    activation_policy_sha256: str
    identity_override: IdentityOverrideBindingV1 | None = None
    members: list[PrivateSegmentMemberV1]

    @field_validator("run_id", "segment_id", mode="before")
    @classmethod
    def _ids(cls, value: Any) -> str:
        return _normalize_identifier(value)

    @field_validator("play_ids", mode="before")
    @classmethod
    def _play_ids(cls, value: Any) -> list[str]:
        if not isinstance(value, list) or not value:
            raise ValueError("play_ids must be a non-empty list")
        plays = [_normalize_identifier(item) for item in value]
        if len(plays) != len(set(plays)):
            raise ValueError("play_ids must be unique")
        return plays

    @field_validator(
        "money_map_sha256",
        "recovery_plays_sha256",
        "evidence_packet_sha256",
        "value_ledger_sha256",
        "activation_policy_sha256",
        mode="before",
    )
    @classmethod
    def _hashes(cls, value: Any) -> str:
        return _normalize_hash(value)

    @model_validator(mode="after")
    def _member_coherence(self) -> Self:
        tokens = [member.customer_token for member in self.members]
        if len(tokens) != len(set(tokens)):
            raise ValueError("private segment members must be unique by customer_token")
        for member in self.members:
            if member.segment_id != self.segment_id:
                raise ValueError("member segment_id must match file segment_id")
            if set(member.play_ids) != set(self.play_ids):
                raise ValueError("member play_ids must match segment play_ids")
        return self


class PublicSegmentAggregateV1(CanonicalModel):
    """Public-safe aggregate counts only — no member rows or identity tokens."""

    segment_id: str
    play_ids: list[str] = Field(default_factory=list)
    eligible_member_count: int = Field(ge=0)
    withheld_exclusion_count: int = Field(ge=0)
    withheld_ambiguous_count: int = Field(ge=0)

    @field_validator("segment_id", mode="before")
    @classmethod
    def _ids(cls, value: Any) -> str:
        return _normalize_identifier(value)

    @field_validator("play_ids", mode="before")
    @classmethod
    def _play_ids(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            raise ValueError("play_ids must be a list")
        plays = [_normalize_identifier(item) for item in value]
        if len(plays) != len(set(plays)):
            raise ValueError("play_ids must be unique")
        return plays


class PublicSegmentFileV1(CanonicalModel):
    schema_version: Literal["activation-segment-public.v1"] = "activation-segment-public.v1"
    run_id: str
    money_map_sha256: str
    recovery_plays_sha256: str
    evidence_packet_sha256: str
    value_ledger_sha256: str
    segments: list[PublicSegmentAggregateV1]

    @field_validator("run_id", mode="before")
    @classmethod
    def _run(cls, value: Any) -> str:
        return _normalize_identifier(value)

    @field_validator(
        "money_map_sha256",
        "recovery_plays_sha256",
        "evidence_packet_sha256",
        "value_ledger_sha256",
        mode="before",
    )
    @classmethod
    def _hashes(cls, value: Any) -> str:
        return _normalize_hash(value)


class WithheldAssetV1(CanonicalModel):
    asset_id: str
    asset_class: str
    status: Literal["withheld"] = "withheld"
    reason: str
    depends_on: list[str] = Field(default_factory=list)
    play_id: str | None = None
    segment_id: str | None = None
    count: int | None = Field(default=None, ge=0)
    reason_codes: list[str] = Field(default_factory=list)

    @field_validator("asset_id", "asset_class", "reason", mode="before")
    @classmethod
    def _text(cls, value: Any) -> str:
        return _normalize_identifier(value)

    @field_validator("depends_on", "reason_codes", mode="before")
    @classmethod
    def _lists(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError("list field must be an array")
        return sorted({_normalize_identifier(item) for item in value})

    @field_validator("play_id", "segment_id", mode="before")
    @classmethod
    def _optional_ids(cls, value: Any) -> str | None:
        if value is None:
            return None
        return _normalize_identifier(value)


class WithheldAssetLedgerV1(CanonicalModel):
    schema_version: Literal["launch-pack-withheld-assets.v1"] = "launch-pack-withheld-assets.v1"
    run_id: str
    assets: list[WithheldAssetV1] = Field(default_factory=list)

    @field_validator("run_id", mode="before")
    @classmethod
    def _run(cls, value: Any) -> str:
        return _normalize_identifier(value)


class LaunchPackFileEntryV1(CanonicalModel):
    path: str
    sha256: str
    asset_class: str
    status: AssetStatus
    play_id: str | None = None
    segment_id: str | None = None

    @field_validator("path", mode="before")
    @classmethod
    def _path(cls, value: Any) -> str:
        text = _normalize_relative_path(value)
        if text == "manifest.json":
            raise ValueError("manifest must not hash itself")
        return text

    @field_validator("sha256", mode="before")
    @classmethod
    def _sha(cls, value: Any) -> str:
        return _normalize_hash(value)

    @field_validator("asset_class", mode="before")
    @classmethod
    def _asset_class(cls, value: Any) -> str:
        return _normalize_identifier(value)

    @field_validator("play_id", "segment_id", mode="before")
    @classmethod
    def _optional_ids(cls, value: Any) -> str | None:
        if value is None:
            return None
        return _normalize_identifier(value)


class LaunchPackManifestV1(CanonicalModel):
    schema_version: Literal["launch-pack-manifest.v1"] = "launch-pack-manifest.v1"
    run_id: str
    mode: LaunchPackMode
    status: LaunchPackStatus
    approval_only: Literal[True] = True
    export_only: Literal[True] = True
    not_activated: Literal[True] = True
    send_performed: Literal[False] = False
    schedule_performed: Literal[False] = False
    audience_created: Literal[False] = False
    provider_write_performed: Literal[False] = False
    reason: str
    available_play_count: int = Field(ge=0)
    required_v1_play_count: int = Field(ge=1)
    money_map_sha256: str
    recovery_plays_sha256: str
    evidence_packet_sha256: str
    value_ledger_sha256: str
    activation_policy_sha256: str
    play_ids: list[str]
    segment_ids: list[str]
    files: list[LaunchPackFileEntryV1]
    withheld_assets: list[str] = Field(default_factory=list)
    relative_links: list[str] = Field(default_factory=list)

    @field_validator("run_id", "reason", mode="before")
    @classmethod
    def _text(cls, value: Any) -> str:
        return _normalize_identifier(value)

    @field_validator(
        "money_map_sha256",
        "recovery_plays_sha256",
        "evidence_packet_sha256",
        "value_ledger_sha256",
        "activation_policy_sha256",
        mode="before",
    )
    @classmethod
    def _hashes(cls, value: Any) -> str:
        return _normalize_hash(value)

    @field_validator("play_ids", "segment_ids", "withheld_assets", "relative_links", mode="before")
    @classmethod
    def _lists(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            raise ValueError("list field must be an array")
        return [_normalize_identifier(item) for item in value]

    @model_validator(mode="after")
    def _consistency(self) -> Self:
        if len(self.play_ids) != len(set(self.play_ids)):
            raise ValueError("play_ids must be unique")
        if len(self.segment_ids) != len(set(self.segment_ids)):
            raise ValueError("segment_ids must be unique")
        paths = [entry.path for entry in self.files]
        if len(paths) != len(set(paths)):
            raise ValueError("manifest file paths must be unique")
        if any(path == "manifest.json" for path in paths):
            raise ValueError("manifest must not include itself in the file index")
        if self.mode == "public" and any(path.startswith("private/") for path in paths):
            raise ValueError("public launch pack must not index private paths")
        return self

    def canonical_dict(self) -> dict[str, Any]:
        payload = self.model_dump(mode="python")
        payload["files"] = sorted(payload["files"], key=lambda item: item["path"])
        payload["play_ids"] = sorted(payload["play_ids"])
        payload["segment_ids"] = sorted(payload["segment_ids"])
        payload["withheld_assets"] = sorted(payload["withheld_assets"])
        payload["relative_links"] = sorted(payload["relative_links"])
        return payload

    def to_canonical_json(self) -> bytes:
        return _canonical_json_bytes(self.canonical_dict())


_HANDOFF_EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_HANDOFF_PHONE_RE = re.compile(
    r"(?<![0-9a-fA-F])(?:\+\d{1,3}[\s.-]?)?(?:\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4})(?![0-9a-fA-F])"
)
_HANDOFF_URL_RE = re.compile(r"(?i)\b(?:https?|javascript|data|file):")
_HANDOFF_IDENTITY_RE = re.compile(
    r"(?i)\b(?:cus_|hs_contact_|inv_|src_|contact_id|source_id|customer_token|record_id)\w*"
)
_HANDOFF_SECRET_RE = re.compile(
    r"(?i)(?:\b(?:sk_live_|sk_test_|rk_live_|rk_test_|ghp_|xox[baprs]-)[A-Za-z0-9_-]{8,}"
    r"|\b(?:password|passwd|api[_-]?key|client[_-]?secret|authorization|bearer)\b)"
)
_HANDOFF_SAFE_ID_RE = re.compile(r"^(?:play|card|seg)_[a-z0-9_]+$")
_HANDOFF_SAFE_LITERALS = frozenset(
    {
        "creative-handoff.v1",
        "production-brief.v1",
        "approval_only_export",
    }
)


def _reject_handoff_string(text: str, *, path: str) -> None:
    if _SHA256_RE.fullmatch(text) or _HANDOFF_SAFE_ID_RE.fullmatch(text):
        return
    if text in _HANDOFF_SAFE_LITERALS:
        return
    if _HANDOFF_EMAIL_RE.search(text):
        raise ValueError(f"{path}: contains email")
    if _HANDOFF_PHONE_RE.search(text):
        raise ValueError(f"{path}: contains phone")
    if _HANDOFF_URL_RE.search(text) or "://" in text:
        raise ValueError(f"{path}: contains unsafe URL")
    if _HANDOFF_IDENTITY_RE.search(text):
        raise ValueError(f"{path}: contains raw identity or source id")
    if _HANDOFF_SECRET_RE.search(text):
        raise ValueError(f"{path}: contains credential or secret")


def _reject_handoff_tree(value: Any, *, path: str) -> None:
    if isinstance(value, str):
        _reject_handoff_string(value, path=path or "value")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            child = f"{path}.{index}" if path else str(index)
            _reject_handoff_tree(item, path=child)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{path}.{key}" if path else str(key)
            _reject_handoff_tree(item, path=child)


_AUTO_INTEGRATION_PHRASE_RE = re.compile(
    r"(?i)\b(?:automatic(?:ally)?\s+integrat(?:e|ion|es|ed)|live\s+integration|"
    r"api\s+sync|auto[- ](?:submit|send|import|sync))\b"
)
_MANUAL_IMPORT_RE = re.compile(r"(?i)\bmanual(?:ly)?\s+import\b")
_NEGATION_PREFIX_RE = re.compile(r"(?i)\b(?:no|not|never|without)\s+$")


def _claims_automatic_integration(text: str) -> bool:
    """Return True when copy asserts an automatic/live integration (not a denial)."""
    for match in _AUTO_INTEGRATION_PHRASE_RE.finditer(text):
        prefix = text[max(0, match.start() - 24) : match.start()]
        if _NEGATION_PREFIX_RE.search(prefix):
            continue
        return True
    return False


class CreativeHandoffCardV1(CanonicalModel):
    card_id: str
    card_name: str
    format_style: str

    @field_validator("card_id", "card_name", "format_style", mode="before")
    @classmethod
    def _text(cls, value: Any, info: Any) -> str:
        text = _normalize_identifier(value)
        _reject_handoff_string(text, path=getattr(info, "field_name", "card") or "card")
        return text


class CreativeHandoffV1(CanonicalModel):
    """Approval-only creative handoff export. Not a live StealAds integration."""

    schema_version: Literal["creative-handoff.v1"] = "creative-handoff.v1"
    play_id: str
    segment_id: str
    campaign_name: str
    status: Literal["approval_only_export"] = "approval_only_export"
    live_integration: Literal[False] = False
    money_map_sha256: str
    recovery_plays_sha256: str
    evidence_packet_sha256: str
    value_ledger_sha256: str
    concept_cards: list[CreativeHandoffCardV1] = Field(min_length=1)
    import_instructions: str

    @field_validator("play_id", "segment_id", "campaign_name", "import_instructions", mode="before")
    @classmethod
    def _text(cls, value: Any, info: Any) -> str:
        text = _normalize_identifier(value)
        _reject_handoff_string(text, path=getattr(info, "field_name", "field") or "field")
        return text

    @field_validator(
        "money_map_sha256",
        "recovery_plays_sha256",
        "evidence_packet_sha256",
        "value_ledger_sha256",
        mode="before",
    )
    @classmethod
    def _hashes(cls, value: Any) -> str:
        return _normalize_hash(value)

    @model_validator(mode="after")
    def _honest_manual_import(self) -> Self:
        if not _MANUAL_IMPORT_RE.search(self.import_instructions):
            raise ValueError("creative handoff must include manual import instructions")
        if _claims_automatic_integration(self.import_instructions):
            raise ValueError("creative handoff must not claim automatic integration")
        card_ids = [card.card_id for card in self.concept_cards]
        if len(card_ids) != len(set(card_ids)):
            raise ValueError("creative handoff concept card ids must be unique")
        _reject_handoff_tree(self.canonical_dict(), path="")
        return self


class ProductionBriefCardV1(CanonicalModel):
    card_id: str
    card_name: str
    format_style: str
    production_requirements: list[str] = Field(min_length=1)

    @field_validator("card_id", "card_name", "format_style", mode="before")
    @classmethod
    def _text(cls, value: Any, info: Any) -> str:
        text = _normalize_identifier(value)
        _reject_handoff_string(text, path=getattr(info, "field_name", "card") or "card")
        return text

    @field_validator("production_requirements", mode="before")
    @classmethod
    def _requirements(cls, value: Any) -> list[str]:
        if not isinstance(value, list) or not value:
            raise ValueError("production requirements must be a non-empty list")
        items = [_normalize_identifier(item) for item in value]
        for index, item in enumerate(items):
            _reject_handoff_string(item, path=f"production_requirements.{index}")
        return items


class ProductionBriefV1(CanonicalModel):
    """Approval-only Matt/Emerald production brief. Not a live integration."""

    schema_version: Literal["production-brief.v1"] = "production-brief.v1"
    play_id: str
    segment_id: str
    campaign_name: str
    status: Literal["approval_only_export"] = "approval_only_export"
    live_integration: Literal[False] = False
    money_map_sha256: str
    recovery_plays_sha256: str
    evidence_packet_sha256: str
    value_ledger_sha256: str
    creative_big_idea: str
    concept_cards: list[ProductionBriefCardV1] = Field(min_length=1)
    export_instructions: str

    @field_validator(
        "play_id",
        "segment_id",
        "campaign_name",
        "creative_big_idea",
        "export_instructions",
        mode="before",
    )
    @classmethod
    def _text(cls, value: Any, info: Any) -> str:
        text = _normalize_identifier(value)
        _reject_handoff_string(text, path=getattr(info, "field_name", "field") or "field")
        return text

    @field_validator(
        "money_map_sha256",
        "recovery_plays_sha256",
        "evidence_packet_sha256",
        "value_ledger_sha256",
        mode="before",
    )
    @classmethod
    def _hashes(cls, value: Any) -> str:
        return _normalize_hash(value)

    @model_validator(mode="after")
    def _honest_export(self) -> Self:
        if _claims_automatic_integration(self.export_instructions):
            raise ValueError("production brief must not claim automatic integration")
        finished_re = re.compile(r"(?i)\b(?:finished\s+video\s+script|media[- ]buying\s+plan)\b")
        for match in finished_re.finditer(self.export_instructions):
            prefix = self.export_instructions[max(0, match.start() - 24) : match.start()]
            if _NEGATION_PREFIX_RE.search(prefix):
                continue
            # Also allow list denials after "no ... , X, or Y"
            window = self.export_instructions[max(0, match.start() - 80) : match.start()].casefold()
            if re.search(r"\b(?:no|not|never|without)\b", window):
                continue
            raise ValueError("production brief must not claim finished production assets")
        card_ids = [card.card_id for card in self.concept_cards]
        if len(card_ids) != len(set(card_ids)):
            raise ValueError("production brief concept card ids must be unique")
        _reject_handoff_tree(self.canonical_dict(), path="")
        return self


class HandoffIntakeConfigV1(CanonicalModel):
    """Optional intake-link configuration. Absence means unconfigured export-only."""

    schema_version: Literal["handoff-intake-config.v1"] = "handoff-intake-config.v1"
    stealads_intake_url: str | None = None
    matt_emerald_intake_url: str | None = None

    @field_validator("stealads_intake_url", "matt_emerald_intake_url", mode="before")
    @classmethod
    def _optional_url(cls, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("intake URL must be a string")
        text = value.strip()
        if not text:
            return None
        return text


REQUIRED_ARTIFACT_CLASSES: tuple[str, ...] = (
    "money_map",
    "play_pages",
    "public_segments",
    "complete_copy",
    "calendar",
    "offer_landing_briefs",
    "tracking_plan",
    "launch_checklist",
    "creative_handoff",
    "production_brief",
    "withheld_ledger",
)

PRIVATE_ONLY_ARTIFACT_CLASSES: tuple[str, ...] = ("private_segments",)
