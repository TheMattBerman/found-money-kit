"""Reusable scenario definition, aggregate receipt, and manifest contracts."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal, Mapping, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from found_money.contracts.events import EVENT_FAMILIES, PUBLIC_NAMED_DATA_GAPS
from found_money.contracts.run import RunMode
from found_money.contracts.value import normalize_currency, normalize_minor_units

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TRAVERSAL_RE = re.compile(r"(^|/)\.\.(/|$)")
_WINDOWS_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")
_CANONICAL_SAAS_FAMILIES = (
    "failed_payment",
    "expired_trial",
    "canceled_customer",
    "closed_lost_stale_deal",
    "renewal_upsell",
)
_CANONICAL_ECOMMERCE_FAMILIES = (
    "lapsed_repeat_buyer",
    "overdue_reorder",
    "disappeared_high_value_customer",
)
_CANONICAL_PLAY_IDS = (
    "payment-rescue-friction-fix",
    "payment-rescue-proof-reset",
    "payment-rescue-capacity-window",
)
_CANONICAL_ECOMMERCE_PLAY_IDS = (
    "vip-silence-reopen",
    "vip-catalog-proof",
    "vip-capacity-lane",
)
_CANONICAL_SERVICE_FAMILIES = (
    "no_show_rebook",
    "trial_no_convert",
    "canceled_customer",
    "silent_proposal",
    "disappeared_high_value_customer",
)
_CANONICAL_SERVICE_PLAY_IDS = (
    "membership-silence-reopen",
    "membership-visit-proof",
    "membership-capacity-lane",
)
_SERVICE_SOURCE_ORDER = ("hubspot", "appointments", "proposals", "stripe")
_SERVICE_OMITTABLE_SOURCES = frozenset(_SERVICE_SOURCE_ORDER)
_PAYMENT_DEPENDENT_FAMILIES = (
    "trial_no_convert",
    "canceled_customer",
    "disappeared_high_value_customer",
)
_CANONICAL_CARD_IDS = tuple(
    f"concept-{play}-{card}" for play in range(1, 4) for card in range(1, 4)
)


def _object_pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate JSON object key: {key}")
        out[key] = value
    return out


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


class ScenarioExpectedValueV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_family: str
    pile_id: str
    currency: str
    amount_minor: Decimal | None = None
    value_basis: Literal["observed_face_value", "modeled_opportunity", "unquantified"]

    @field_validator("event_family", "pile_id", mode="before")
    @classmethod
    def _ids(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("identifier must be a string")
        text = _normalize_identifier(value)
        if text not in EVENT_FAMILIES and text != "payment_rescue":
            raise ValueError(f"unsupported family or pile: {text}")
        return text

    @field_validator("currency", mode="before")
    @classmethod
    def _currency(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("currency must be a string")
        return normalize_currency(value)

    @field_validator("amount_minor", mode="before")
    @classmethod
    def _amount(cls, value: Any) -> Decimal | None:
        if value is None:
            return None
        return normalize_minor_units(value)

    @model_validator(mode="after")
    def _basis(self) -> Self:
        if self.value_basis == "unquantified":
            if self.amount_minor is not None:
                raise ValueError("unquantified values must not declare an amount")
        elif self.amount_minor is None:
            raise ValueError("quantified values require amount_minor")
        return self


class ScenarioProofContractV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["found-money-scenario-proof-contract.v1"] = (
        "found-money-scenario-proof-contract.v1"
    )
    inherited_from: tuple[str, ...] = ("FM-028", "FM-029")
    claims_new_visual_baseline: Literal[False] = False
    claims_print_ratification: Literal[False] = False
    mechanical_no_console: Literal[True] = True
    mechanical_no_clip: Literal[True] = True
    mechanical_responsive: Literal[True] = True
    mechanical_a11y: Literal[True] = True
    mechanical_us_letter: Literal[True] = True


class ScenarioDefinitionV1(BaseModel):
    """Locked extra-forbid definition reused by FM-033/034/035."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["found-money-scenario-definition.v1"] = (
        "found-money-scenario-definition.v1"
    )
    scenario_id: str
    business_model: Literal["saas", "ecommerce", "service"]
    public_safe: Literal[True] = True
    sources: list[str]
    fixture_id: str
    fixture_hashes: dict[str, str]
    schema_versions: dict[str, str]
    schema_hashes: dict[str, str]
    source_config_hash: str
    expected_event_families: list[str]
    expected_play_ids: list[str]
    expected_card_ids: list[str]
    expected_resolved_accounts: int = Field(ge=1)
    clock: datetime
    expected_values: list[ScenarioExpectedValueV1]
    inherited_proof_contract: ScenarioProofContractV1
    strategy_producer_model_family: str
    high_value_cart_state: Literal["supplied", "omitted"] | None = None
    high_value_cart_hash: str | None = None
    omitted_service_sources: list[str] | None = None

    @field_validator("scenario_id", "fixture_id", "strategy_producer_model_family", mode="before")
    @classmethod
    def _text(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("value must be a string")
        return _normalize_identifier(value)

    @field_validator("sources", "expected_event_families", "expected_play_ids", "expected_card_ids")
    @classmethod
    def _unique_list(cls, value: Any) -> list[str]:
        if not isinstance(value, list) or not value:
            raise ValueError("list must be a non-empty list")
        items = [_normalize_identifier(item) if isinstance(item, str) else item for item in value]
        if any(not isinstance(item, str) for item in items):
            raise ValueError("list entries must be strings")
        if len(items) != len(set(items)):
            raise ValueError("list entries must be unique")
        return items

    @field_validator("fixture_hashes", "schema_versions", "schema_hashes", mode="before")
    @classmethod
    def _maps(cls, value: Any, info: Any) -> dict[str, str]:
        if not isinstance(value, Mapping):
            raise ValueError(f"{info.field_name} must be an object")
        out: dict[str, str] = {}
        for key, raw in value.items():
            if not isinstance(key, str) or not isinstance(raw, str):
                raise ValueError(f"{info.field_name} entries must be strings")
            name = _normalize_identifier(key)
            out[name] = (
                _normalize_hash(raw)
                if info.field_name != "schema_versions"
                else _normalize_identifier(raw)
            )
        return dict(sorted(out.items()))

    @field_validator("source_config_hash", "high_value_cart_hash", mode="before")
    @classmethod
    def _config_hash(cls, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("hash must be a string")
        return _normalize_hash(value)

    @field_validator("clock", mode="before")
    @classmethod
    def _clock(cls, value: Any) -> datetime:
        return _parse_utc(value)

    @model_validator(mode="after")
    def _saas_lock(self) -> Self:
        if self.business_model == "saas":
            if self.sources != ["hubspot", "stripe"]:
                raise ValueError("SaaS scenario sources must be hubspot then stripe")
            if sorted(self.expected_event_families) != sorted(_CANONICAL_SAAS_FAMILIES):
                raise ValueError("SaaS scenario must declare the five canonical families")
            if self.expected_play_ids != list(_CANONICAL_PLAY_IDS):
                raise ValueError("SaaS scenario must declare the three canonical play IDs")
            if self.expected_card_ids != list(_CANONICAL_CARD_IDS):
                raise ValueError("SaaS scenario must declare the nine canonical card IDs")
            if self.expected_resolved_accounts != 5:
                raise ValueError("SaaS scenario must declare five resolved accounts")
            families = {item.event_family for item in self.expected_values}
            if families != set(_CANONICAL_SAAS_FAMILIES):
                raise ValueError("SaaS expected values must cover each required family once")
            failed = next(
                item for item in self.expected_values if item.event_family == "failed_payment"
            )
            if (
                failed.pile_id != "payment_rescue"
                or failed.currency != "usd"
                or failed.amount_minor != Decimal("4900")
                or failed.value_basis != "observed_face_value"
            ):
                raise ValueError("failed_payment must be observed 4900 usd on payment_rescue")
            if self.high_value_cart_state is not None or self.high_value_cart_hash is not None:
                raise ValueError("SaaS scenario must not declare high-value cart bindings")
        if self.business_model == "ecommerce":
            if self.sources != ["stripe", "orders"]:
                raise ValueError("ecommerce scenario sources must be stripe then orders")
            if sorted(self.expected_event_families) != sorted(_CANONICAL_ECOMMERCE_FAMILIES):
                raise ValueError("ecommerce scenario must declare the three canonical families")
            if self.expected_play_ids != list(_CANONICAL_ECOMMERCE_PLAY_IDS):
                raise ValueError("ecommerce scenario must declare the three canonical play IDs")
            if self.expected_card_ids != list(_CANONICAL_CARD_IDS):
                raise ValueError("ecommerce scenario must declare the nine canonical card IDs")
            if self.expected_resolved_accounts != 5:
                raise ValueError("ecommerce scenario must declare five resolved accounts")
            if self.high_value_cart_state not in {"supplied", "omitted"}:
                raise ValueError("ecommerce scenario must declare high_value_cart_state")
            if self.high_value_cart_hash is None:
                raise ValueError("ecommerce scenario must declare high_value_cart_hash")
            families = {item.event_family for item in self.expected_values}
            if not set(_CANONICAL_ECOMMERCE_FAMILIES).issubset(families):
                raise ValueError("ecommerce expected values must cover each required family")
        if self.business_model == "service":
            omitted = [_normalize_identifier(item) for item in (self.omitted_service_sources or [])]
            if len(omitted) != len(set(omitted)):
                raise ValueError("omitted service sources must be unique")
            if any(item not in _SERVICE_OMITTABLE_SOURCES for item in omitted):
                raise ValueError(
                    "service omitted sources must be hubspot, appointments, proposals, or stripe"
                )
            if set(omitted).intersection(self.sources):
                raise ValueError("omitted service sources must not also be listed as sources")
            expected_sources = [name for name in _SERVICE_SOURCE_ORDER if name not in set(omitted)]
            if not expected_sources:
                raise ValueError("service scenario must retain at least one source")
            if self.sources != expected_sources:
                raise ValueError(
                    "service scenario sources must follow canonical order minus omitted sources"
                )
            self.omitted_service_sources = sorted(omitted) or None
            if self.high_value_cart_state is not None or self.high_value_cart_hash is not None:
                raise ValueError("service scenario must not declare high-value cart bindings")
            if self.expected_play_ids != list(_CANONICAL_SERVICE_PLAY_IDS):
                raise ValueError("service scenario must declare the three canonical play IDs")
            if self.expected_card_ids != list(_CANONICAL_CARD_IDS):
                raise ValueError("service scenario must declare the nine canonical card IDs")
            if self.expected_resolved_accounts != 5:
                raise ValueError("service scenario must declare five resolved accounts")
            expected_families = set(_CANONICAL_SERVICE_FAMILIES)
            if "appointments" in omitted:
                expected_families.discard("no_show_rebook")
            if "proposals" in omitted:
                expected_families.discard("silent_proposal")
            if "stripe" in omitted:
                expected_families.difference_update(_PAYMENT_DEPENDENT_FAMILIES)
            if not expected_families:
                raise ValueError("service scenario omitted too many required families")
            if sorted(self.expected_event_families) != sorted(expected_families):
                raise ValueError(
                    "service scenario must declare the canonical families for its source state"
                )
            families = {item.event_family for item in self.expected_values}
            if families != expected_families:
                raise ValueError("service expected values must cover each required family once")
        elif self.omitted_service_sources:
            raise ValueError("only the service scenario may omit required service sources")
        if set(self.fixture_hashes) != set(self.sources):
            raise ValueError("fixture_hashes keys must match sources")
        if set(self.schema_hashes) != set(self.schema_versions.values()):
            raise ValueError("schema_hashes keys must match declared schema versions")
        return self

    def canonical_dict(self) -> dict[str, Any]:
        payload = self.model_dump(mode="python", exclude_none=True)
        payload["clock"] = _format_utc(self.clock)
        for expected, row in zip(self.expected_values, payload["expected_values"], strict=True):
            if expected.amount_minor is None:
                row["amount_minor"] = None
            else:
                row["amount_minor"] = str(expected.amount_minor)
        return payload

    def to_canonical_json(self) -> bytes:
        return _canonical_json_bytes(self.canonical_dict())


class ScenarioIdentityAggregateV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["scenario-identity-aggregate.v1"] = "scenario-identity-aggregate.v1"
    run_id: str
    resolved_customer_count: int = Field(ge=0)
    ambiguous_cluster_count: int = Field(ge=0)
    source_systems: list[str]

    @field_validator("run_id", mode="before")
    @classmethod
    def _run(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("run_id must be a string")
        return _normalize_identifier(value)

    def to_canonical_json(self) -> bytes:
        return _canonical_json_bytes(self.model_dump(mode="python"))


class ScenarioValuePileAggregateV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pile_id: str
    currency: str
    amount_minor: Decimal
    value_basis: str
    event_count: int = Field(ge=1)

    @field_validator("pile_id", "value_basis", mode="before")
    @classmethod
    def _text(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("value must be a string")
        return _normalize_identifier(value)

    @field_validator("currency", mode="before")
    @classmethod
    def _currency(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("currency must be a string")
        return normalize_currency(value)

    @field_validator("amount_minor", mode="before")
    @classmethod
    def _amount(cls, value: Any) -> Decimal:
        return normalize_minor_units(value)


class ScenarioValueAggregateV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["scenario-value-aggregate.v1"] = "scenario-value-aggregate.v1"
    run_id: str
    totals_minor_by_currency: dict[str, Decimal]
    piles: list[ScenarioValuePileAggregateV1]
    unquantified_count: int = Field(ge=0)

    @field_validator("run_id", mode="before")
    @classmethod
    def _run(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("run_id must be a string")
        return _normalize_identifier(value)

    @field_validator("totals_minor_by_currency", mode="before")
    @classmethod
    def _totals(cls, value: Any) -> dict[str, Decimal]:
        if not isinstance(value, Mapping):
            raise ValueError("totals_minor_by_currency must be an object")
        out: dict[str, Decimal] = {}
        for key, raw in value.items():
            if not isinstance(key, str):
                raise ValueError("currency keys must be strings")
            out[normalize_currency(key)] = normalize_minor_units(raw)
        return dict(sorted(out.items()))

    def canonical_dict(self) -> dict[str, Any]:
        payload = self.model_dump(mode="python")
        payload["totals_minor_by_currency"] = {
            key: str(value) for key, value in self.totals_minor_by_currency.items()
        }
        for row in payload["piles"]:
            row["amount_minor"] = str(row["amount_minor"])
        return payload

    def to_canonical_json(self) -> bytes:
        return _canonical_json_bytes(self.canonical_dict())


class ScenarioAggregateReceiptV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["found-money-scenario-aggregate.v1"] = (
        "found-money-scenario-aggregate.v1"
    )
    run_id: str
    scenario_id: str
    built_at: datetime
    mode: RunMode
    public_safe: Literal[True] = True
    sources: list[str]
    resolved_customer_count: int = Field(ge=0)
    ambiguous_cluster_count: int = Field(ge=0)
    candidates_by_family: dict[str, int]
    exclusion_count: int = Field(ge=0)
    data_gap_count: int = Field(ge=0)
    named_data_gaps: list[str] | None = None
    exclusions_by_reason: dict[str, int] | None = None
    identified_opportunity_minor: dict[str, Decimal]
    play_ids: list[str]
    card_ids: list[str]
    primary_play_id: str | None = None
    inherited_proof_contract: ScenarioProofContractV1
    high_value_cart_state: Literal["supplied", "omitted"] | None = None
    high_value_cart_hash: str | None = None
    situation_ids: list[str] | None = None

    @field_validator("run_id", "scenario_id", mode="before")
    @classmethod
    def _ids(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("identifier must be a string")
        return _normalize_identifier(value)

    @field_validator("primary_play_id", mode="before")
    @classmethod
    def _primary_id(cls, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("identifier must be a string")
        return _normalize_identifier(value)

    @field_validator("high_value_cart_hash", mode="before")
    @classmethod
    def _cart_hash(cls, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("hash must be a string")
        return _normalize_hash(value)

    @field_validator("built_at", mode="before")
    @classmethod
    def _built(cls, value: Any) -> datetime:
        return _parse_utc(value)

    @field_validator("sources")
    @classmethod
    def _sources(cls, value: Any) -> list[str]:
        if not isinstance(value, list) or not value:
            raise ValueError("list must be a non-empty list")
        items = [_normalize_identifier(item) if isinstance(item, str) else item for item in value]
        if any(not isinstance(item, str) for item in items):
            raise ValueError("list entries must be strings")
        if len(items) != len(set(items)):
            raise ValueError("list entries must be unique")
        return items

    @field_validator("play_ids", "card_ids")
    @classmethod
    def _play_card_lists(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            raise ValueError("list must be a list")
        items = [_normalize_identifier(item) if isinstance(item, str) else item for item in value]
        if any(not isinstance(item, str) for item in items):
            raise ValueError("list entries must be strings")
        if len(items) != len(set(items)):
            raise ValueError("list entries must be unique")
        return items

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

    @model_validator(mode="after")
    def _primary(self) -> Self:
        payment_withheld = "missing_payment" in (self.named_data_gaps or [])
        if payment_withheld:
            if self.play_ids or self.card_ids or self.primary_play_id is not None:
                raise ValueError("missing payment must not publish play or card IDs")
        elif not self.play_ids or self.primary_play_id not in self.play_ids:
            raise ValueError("primary_play_id must be one of play_ids")
        if self.situation_ids is not None:
            items = [_normalize_identifier(item) for item in self.situation_ids]
            if len(items) != len(set(items)):
                raise ValueError("situation_ids must be unique")
            self.situation_ids = items
        if self.high_value_cart_state is not None and self.high_value_cart_hash is None:
            raise ValueError("high_value_cart_hash is required when cart state is declared")
        if self.named_data_gaps is not None:
            items = [_normalize_identifier(item) for item in self.named_data_gaps]
            if len(items) != len(set(items)):
                raise ValueError("named_data_gaps must be unique")
            if any(item not in PUBLIC_NAMED_DATA_GAPS for item in items):
                raise ValueError("named_data_gaps must use public-safe source-gap codes")
            self.named_data_gaps = sorted(items) or None
        if self.exclusions_by_reason is not None and not self.exclusions_by_reason:
            self.exclusions_by_reason = None
        return self

    def canonical_dict(self) -> dict[str, Any]:
        payload = self.model_dump(mode="python")
        payload["built_at"] = _format_utc(self.built_at)
        payload["identified_opportunity_minor"] = {
            key: str(value) for key, value in self.identified_opportunity_minor.items()
        }
        for key in (
            "high_value_cart_state",
            "high_value_cart_hash",
            "situation_ids",
            "named_data_gaps",
            "exclusions_by_reason",
            "primary_play_id",
        ):
            if payload.get(key) is None:
                payload.pop(key, None)
        return payload

    def to_canonical_json(self) -> bytes:
        return _canonical_json_bytes(self.canonical_dict())


class ScenarioManifestArtifactV1(BaseModel):
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


class ScenarioManifestV1(BaseModel):
    """Hashes every output except itself and run.json. Never self-hashes."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["found-money-scenario-manifest.v1"] = "found-money-scenario-manifest.v1"
    run_id: str
    scenario_id: str
    public_safe: Literal[True] = True
    mode: RunMode
    business_model: Literal["saas", "ecommerce", "service"]
    synthetic: Literal[True] = True
    sources: list[str]
    connector_schema_versions: dict[str, str]
    fixture_schema_versions: dict[str, str]
    fixture_hashes: dict[str, str]
    content_hashes: dict[str, str]
    source_counts: dict[str, int]
    expected_event_families: list[str]
    candidates_by_family: dict[str, int]
    play_ids: list[str]
    card_ids: list[str]
    definition_sha256: str
    aggregate_receipt_sha256: str
    inherited_proof_contract: ScenarioProofContractV1
    artifacts: list[ScenarioManifestArtifactV1]
    high_value_cart_state: Literal["supplied", "omitted"] | None = None
    high_value_cart_hash: str | None = None
    situation_ids: list[str] | None = None
    named_data_gaps: list[str] | None = None
    exclusions_by_reason: dict[str, int] | None = None

    @field_validator("run_id", "scenario_id", mode="before")
    @classmethod
    def _ids(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("identifier must be a string")
        return _normalize_identifier(value)

    @field_validator("sources", "expected_event_families")
    @classmethod
    def _lists(cls, value: Any) -> list[str]:
        if not isinstance(value, list) or not value:
            raise ValueError("list must be a non-empty list")
        items = [_normalize_identifier(item) if isinstance(item, str) else item for item in value]
        if any(not isinstance(item, str) for item in items):
            raise ValueError("list entries must be strings")
        if len(items) != len(set(items)):
            raise ValueError("list entries must be unique")
        return items

    @field_validator("play_ids", "card_ids")
    @classmethod
    def _play_card_lists(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            raise ValueError("list must be a list")
        items = [_normalize_identifier(item) if isinstance(item, str) else item for item in value]
        if any(not isinstance(item, str) for item in items):
            raise ValueError("list entries must be strings")
        if len(items) != len(set(items)):
            raise ValueError("list entries must be unique")
        return items

    @field_validator(
        "connector_schema_versions",
        "fixture_schema_versions",
        "fixture_hashes",
        "content_hashes",
        mode="before",
    )
    @classmethod
    def _maps(cls, value: Any, info: Any) -> dict[str, str]:
        if not isinstance(value, Mapping):
            raise ValueError(f"{info.field_name} must be an object")
        out: dict[str, str] = {}
        for key, raw in value.items():
            if not isinstance(key, str) or not isinstance(raw, str):
                raise ValueError(f"{info.field_name} entries must be strings")
            name = _normalize_identifier(key)
            out[name] = (
                _normalize_hash(raw)
                if info.field_name.endswith("hashes")
                else _normalize_identifier(raw)
            )
        return dict(sorted(out.items()))

    @field_validator("source_counts", "candidates_by_family", mode="before")
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
                raise ValueError("counts must be non-negative integers")
            out[_normalize_identifier(key)] = raw
        return dict(sorted(out.items()))

    @field_validator(
        "definition_sha256", "aggregate_receipt_sha256", "high_value_cart_hash", mode="before"
    )
    @classmethod
    def _hashes(cls, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("hash must be a string")
        return _normalize_hash(value)

    @model_validator(mode="after")
    def _no_self_hash(self) -> Self:
        paths = [item.path for item in self.artifacts]
        if len(paths) != len(set(paths)):
            raise ValueError("artifact paths must be unique")
        forbidden = {"run.json", "scenario/manifest.json"}
        if forbidden.intersection(paths):
            raise ValueError("scenario manifest must not hash itself or run.json")
        if set(self.connector_schema_versions) != set(self.sources):
            raise ValueError("connector schema versions must cover sources")
        if set(self.fixture_schema_versions) != set(self.sources):
            raise ValueError("fixture schema versions must cover sources")
        if set(self.fixture_hashes) != set(self.sources):
            raise ValueError("fixture hashes must cover sources")
        if set(self.content_hashes) != set(self.sources):
            raise ValueError("content hashes must cover sources")
        if set(self.source_counts) != set(self.sources):
            raise ValueError("source counts must cover sources")
        if self.business_model == "saas":
            if self.play_ids != list(_CANONICAL_PLAY_IDS):
                raise ValueError("SaaS scenario must declare the three canonical play IDs")
            if self.card_ids != list(_CANONICAL_CARD_IDS):
                raise ValueError("SaaS scenario must declare the nine canonical card IDs")
        if self.business_model == "ecommerce":
            if self.play_ids != list(_CANONICAL_ECOMMERCE_PLAY_IDS):
                raise ValueError("ecommerce scenario must declare the three canonical play IDs")
            if self.card_ids != list(_CANONICAL_CARD_IDS):
                raise ValueError("ecommerce scenario must declare the nine canonical card IDs")
            if self.high_value_cart_state not in {"supplied", "omitted"}:
                raise ValueError("ecommerce scenario manifest must declare cart state")
            if self.high_value_cart_hash is None:
                raise ValueError("ecommerce scenario manifest must declare cart hash")
        if self.business_model == "service":
            payment_withheld = "missing_payment" in (self.named_data_gaps or [])
            if payment_withheld:
                if self.play_ids or self.card_ids:
                    raise ValueError("missing payment must not publish service play or card IDs")
            else:
                if self.play_ids != list(_CANONICAL_SERVICE_PLAY_IDS):
                    raise ValueError("service scenario must declare the three canonical play IDs")
                if self.card_ids != list(_CANONICAL_CARD_IDS):
                    raise ValueError("service scenario must declare the nine canonical card IDs")
        if self.business_model != "ecommerce" and (
            self.high_value_cart_state is not None or self.high_value_cart_hash is not None
        ):
            raise ValueError("non-ecommerce scenario manifest must not declare cart bindings")
        if self.situation_ids is not None:
            items = [_normalize_identifier(item) for item in self.situation_ids]
            if len(items) != len(set(items)):
                raise ValueError("situation_ids must be unique")
            self.situation_ids = items
        if self.named_data_gaps is not None:
            items = [_normalize_identifier(item) for item in self.named_data_gaps]
            if len(items) != len(set(items)):
                raise ValueError("named_data_gaps must be unique")
            if any(item not in PUBLIC_NAMED_DATA_GAPS for item in items):
                raise ValueError("named_data_gaps must use public-safe source-gap codes")
            self.named_data_gaps = sorted(items) or None
        if self.exclusions_by_reason is not None and not self.exclusions_by_reason:
            self.exclusions_by_reason = None
        return self

    def canonical_dict(self) -> dict[str, Any]:
        payload = self.model_dump(mode="python")
        payload["artifacts"] = sorted(payload["artifacts"], key=lambda item: item["path"])
        for key in (
            "high_value_cart_state",
            "high_value_cart_hash",
            "situation_ids",
            "named_data_gaps",
            "exclusions_by_reason",
        ):
            if payload.get(key) is None:
                payload.pop(key, None)
        return payload

    def to_canonical_json(self) -> bytes:
        return _canonical_json_bytes(self.canonical_dict())


class ScenarioSituationEvidenceV1(BaseModel):
    """Public-safe aggregate situation evidence. No customer-level rows or IDs."""

    model_config = ConfigDict(extra="forbid")

    situation_id: Literal["refunded_buyer_next_move", "high_value_cart"]
    status: Literal["present", "withheld", "omitted"]
    qualifying_count: int = Field(ge=0)
    totals_minor_by_currency: dict[str, Decimal] = Field(default_factory=dict)
    evidence_digest: str
    state: str | None = None
    withhold_reason: str | None = None

    @field_validator("evidence_digest", mode="before")
    @classmethod
    def _hash(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("hash must be a string")
        return _normalize_hash(value)

    @field_validator("state", "withhold_reason", mode="before")
    @classmethod
    def _optional_text(cls, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("value must be a string")
        return _normalize_identifier(value)

    @field_validator("totals_minor_by_currency", mode="before")
    @classmethod
    def _totals(cls, value: Any) -> dict[str, Decimal]:
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            raise ValueError("totals_minor_by_currency must be an object")
        out: dict[str, Decimal] = {}
        for key, raw in value.items():
            if not isinstance(key, str):
                raise ValueError("currency keys must be strings")
            out[normalize_currency(key)] = normalize_minor_units(raw)
        return dict(sorted(out.items()))

    @model_validator(mode="after")
    def _status_shape(self) -> Self:
        if self.status == "present":
            if self.qualifying_count < 1 or not self.totals_minor_by_currency:
                raise ValueError(
                    "present situations require a qualifying count and currency totals"
                )
            if self.withhold_reason is not None:
                raise ValueError("present situations must not declare a withhold reason")
        else:
            if self.withhold_reason is None:
                raise ValueError("omitted or withheld situations require a withhold reason")
        return self

    def canonical_dict(self) -> dict[str, Any]:
        payload = self.model_dump(mode="python", exclude_none=True)
        payload["totals_minor_by_currency"] = {
            key: str(value) for key, value in self.totals_minor_by_currency.items()
        }
        return payload


class ScenarioSituationSetV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["found-money-scenario-situations.v1"] = (
        "found-money-scenario-situations.v1"
    )
    run_id: str
    high_value_cart_state: Literal["supplied", "omitted"]
    high_value_cart_hash: str
    situations: list[ScenarioSituationEvidenceV1]

    @field_validator("run_id", mode="before")
    @classmethod
    def _run(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("run_id must be a string")
        return _normalize_identifier(value)

    @field_validator("high_value_cart_hash", mode="before")
    @classmethod
    def _hash(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("hash must be a string")
        return _normalize_hash(value)

    @model_validator(mode="after")
    def _unique(self) -> Self:
        ids = [item.situation_id for item in self.situations]
        if len(ids) != len(set(ids)):
            raise ValueError("situation_id values must be unique")
        self.situations = sorted(self.situations, key=lambda item: item.situation_id)
        return self

    def canonical_dict(self) -> dict[str, Any]:
        payload = self.model_dump(mode="python")
        payload["situations"] = [item.canonical_dict() for item in self.situations]
        return payload

    def to_canonical_json(self) -> bytes:
        return _canonical_json_bytes(self.canonical_dict())

    def present_ids(self) -> list[str]:
        return [item.situation_id for item in self.situations if item.status == "present"]


def parse_scenario_definition(data: bytes) -> ScenarioDefinitionV1:
    payload = json.loads(data.decode("utf-8"), object_pairs_hook=_object_pairs_hook)
    model = ScenarioDefinitionV1.model_validate(payload)
    if model.to_canonical_json() != bytes(data):
        raise ValueError("serialized bytes are not exactly canonical")
    return model


def parse_scenario_manifest(data: bytes) -> ScenarioManifestV1:
    payload = json.loads(data.decode("utf-8"), object_pairs_hook=_object_pairs_hook)
    model = ScenarioManifestV1.model_validate(payload)
    if model.to_canonical_json() != bytes(data):
        raise ValueError("serialized bytes are not exactly canonical")
    return model


def parse_scenario_aggregate(data: bytes) -> ScenarioAggregateReceiptV1:
    payload = json.loads(data.decode("utf-8"), object_pairs_hook=_object_pairs_hook)
    model = ScenarioAggregateReceiptV1.model_validate(payload)
    if model.to_canonical_json() != bytes(data):
        raise ValueError("serialized bytes are not exactly canonical")
    return model


def parse_scenario_situations(data: bytes) -> ScenarioSituationSetV1:
    payload = json.loads(data.decode("utf-8"), object_pairs_hook=_object_pairs_hook)
    model = ScenarioSituationSetV1.model_validate(payload)
    if model.to_canonical_json() != bytes(data):
        raise ValueError("serialized bytes are not exactly canonical")
    return model
