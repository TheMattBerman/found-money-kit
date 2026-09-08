"""Auditable, fail-closed strategy-provider boundary (FM-025)."""

from __future__ import annotations

import hashlib
import html
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Protocol, cast

from pydantic import ValidationError

if TYPE_CHECKING:
    from found_money.safety.audit import SafeRequestAuditor

from found_money.contracts.map import MoneyMapV1
from found_money.contracts.strategy import (
    DecisionFlagName,
    GroundedStrategyEvidencePacketV1,
    StrategyAssetV1,
    StrategyAuditReceiptV1,
    StrategyBoundaryResultV1,
    StrategyBusinessProfileV1,
    StrategyClaimV1,
    StrategyDecisionFlagV1,
    StrategyDraftV1,
    StrategyEvidenceItemV1,
    StrategyProviderRequestV1,
    StrategyProviderResponseV1,
    StrategyTokenUsageV1,
)

PROMPT_VERSION = "found-money-strategy.v1"
PROMPT_TEXT = "Use only cited evidence IDs. Never invent facts or identity."
OUTPUT_SCHEMA_VERSION = "strategy-draft.v1"
OUTPUT_SCHEMA_DESCRIPTION = "summary, grounded claims, and renderable or withheld assets"

_FORBIDDEN_KEYS = {
    "customer_token",
    "economic_unit_key",
    "email",
    "phone",
    "external_ids",
    "invoice_id",
    "source_id",
    "source_url",
    "url",
    "lineage",
    "qualifying_evidence",
}
_EMAIL_RE = re.compile(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b")
_PHONE_RE = re.compile(r"(?<!\w)\+?\d[\d .()/-]{7,}\d(?!\w)")
_URL_RE = re.compile(r"https?://", re.IGNORECASE)
_SOURCE_ID_RE = re.compile(r"\b(?:cus|src|inv|contact|deal|rec)_[a-z0-9_-]+\b", re.IGNORECASE)
_PLACEHOLDER_RE = re.compile(r"(?:\[todo\]|\{\{.+?\}\}|\btbd\b)", re.IGNORECASE)
_HEADING_ONLY_RE = re.compile(r"^(?:#{1,6}\s+.+|[^.!?]+:)$")

_DEPENDENCIES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("product", "confirm_product", ("offer", "copy")),
    ("proof", "confirm_proof", ("proof_asset",)),
    ("margin", "confirm_margin", ("offer",)),
    ("channel", "confirm_channel", ("channel_assets",)),
    ("capacity", "confirm_capacity", ("urgency",)),
    ("destination", "confirm_destination", ("cta",)),
)
_CLAIM_EVIDENCE_PREFIXES: dict[str, tuple[str, ...]] = {
    "price": ("ev_business_margin", "ev_business_product"),
    "proof": ("ev_business_proof",),
    "objection": ("ev_business_product", "ev_business_proof"),
    "urgency": ("ev_business_capacity",),
    "destination": ("ev_business_destination",),
    "capacity": ("ev_business_capacity",),
    "product": ("ev_business_product",),
}


class ProviderRefusalError(RuntimeError):
    pass


class ProviderTimeoutError(RuntimeError):
    pass


class GroundingError(ValueError):
    """A provider claim is not supported by the frozen evidence packet."""

    def __init__(self, paths: list[str]):
        self.paths = tuple(paths)
        super().__init__("ungrounded strategy claims at: " + ", ".join(paths))


class StrategyProvider(Protocol):
    def invoke(self, request: StrategyProviderRequestV1) -> StrategyProviderResponseV1: ...


@dataclass(frozen=True)
class StrategyExecution:
    """Runtime result retaining the exact deterministic map supplied by the caller."""

    money_map: MoneyMapV1
    boundary: StrategyBoundaryResultV1


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _assert_pii_free(value: Any, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{path}.{key}"
            if key.lower() in _FORBIDDEN_KEYS:
                raise ValueError(f"forbidden identity field at {child}")
            _assert_pii_free(item, child)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _assert_pii_free(item, f"{path}[{index}]")
    elif isinstance(value, str):
        if (
            _EMAIL_RE.search(value)
            or _PHONE_RE.search(value)
            or _URL_RE.search(value)
            or _SOURCE_ID_RE.search(value)
        ):
            raise ValueError(f"raw identity or URL at {path}")


def decision_flags(profile: StrategyBusinessProfileV1) -> list[StrategyDecisionFlagV1]:
    """Turn only missing business facts into exact, mechanically usable decisions."""
    flags: list[StrategyDecisionFlagV1] = []
    for field, flag, assets in _DEPENDENCIES:
        if getattr(profile, field) is None:
            flags.append(
                StrategyDecisionFlagV1(
                    flag=cast(DecisionFlagName, flag), withheld_assets=list(assets)
                )
            )
    return flags


def build_grounded_strategy_packet(
    money_map: MoneyMapV1,
    profile: StrategyBusinessProfileV1,
    *,
    built_at: datetime | None = None,
) -> GroundedStrategyEvidencePacketV1:
    """Freeze the completed deterministic map, exclusions, gaps, and business facts."""
    if money_map.strategy_stage != "not_started":
        raise ValueError("strategy evidence requires a deterministic map before strategy")
    if any(pile.readiness is None or pile.rank_explanation is None for pile in money_map.piles):
        raise ValueError("strategy evidence requires completed deterministic ranking")

    evidence: list[StrategyEvidenceItemV1] = []
    for pile in sorted(money_map.piles, key=lambda item: (item.rank, item.pile_id, item.currency)):
        prefix = f"ev_pile_{pile.rank}"
        evidence.extend(
            [
                StrategyEvidenceItemV1(
                    evidence_id=f"{prefix}_value",
                    category="map_value",
                    value=f"{pile.selected_value_minor} {pile.currency}",
                ),
                StrategyEvidenceItemV1(
                    evidence_id=f"{prefix}_basis",
                    category="map_basis",
                    value=pile.value_basis,
                ),
            ]
        )
    evidence.extend(
        [
            StrategyEvidenceItemV1(
                evidence_id="ev_overlap_exclusion_count",
                category="exclusion",
                value=money_map.overlap_exclusion_count,
            ),
            StrategyEvidenceItemV1(
                evidence_id="ev_data_gap_count",
                category="data_gap",
                value=money_map.data_gap_count,
            ),
        ]
    )
    for gap in list(getattr(money_map, "named_data_gaps", []) or []):
        evidence.append(
            StrategyEvidenceItemV1(
                evidence_id=f"ev_named_gap_{gap}",
                category="data_gap",
                value=gap,
            )
        )
    exclusion_reasons = dict(getattr(money_map, "event_exclusions_by_reason", {}) or {})
    if exclusion_reasons:
        evidence.append(
            StrategyEvidenceItemV1(
                evidence_id="ev_event_exclusion_reasons",
                category="exclusion",
                value=",".join(
                    f"{reason}:{count}" for reason, count in sorted(exclusion_reasons.items())
                ),
            )
        )
    for field, _, _ in _DEPENDENCIES:
        value = getattr(profile, field)
        if value is not None:
            evidence.append(
                StrategyEvidenceItemV1(
                    evidence_id=f"ev_business_{field}",
                    category="business_input",
                    value=value,
                )
            )
    packet = GroundedStrategyEvidencePacketV1(
        run_id=money_map.run_id,
        built_at=built_at or datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc),
        map_hash=_sha256(money_map.to_canonical_json()),
        evidence=evidence,
        allowed_evidence_ids=[item.evidence_id for item in evidence],
    )
    _assert_pii_free(packet.canonical_dict())
    return packet


def build_provider_request(
    packet: GroundedStrategyEvidencePacketV1,
    *,
    configured_model_id: str,
) -> StrategyProviderRequestV1:
    request = StrategyProviderRequestV1(
        packet=packet,
        prompt_version=PROMPT_VERSION,
        prompt_hash=_sha256(PROMPT_TEXT.encode()),
        output_schema_hash=_sha256(OUTPUT_SCHEMA_DESCRIPTION.encode()),
        configured_model_id=configured_model_id,
    )
    _assert_pii_free(request.canonical_dict())
    return request


def validate_grounded_draft(
    draft: StrategyDraftV1,
    packet: GroundedStrategyEvidencePacketV1,
) -> None:
    """Reject unsupported or structurally incomplete provider output by JSON path."""
    errors: list[str] = []
    allowed = set(packet.allowed_evidence_ids)
    evidence_by_id = {item.evidence_id: str(item.value) for item in packet.evidence}
    present_business_fields = {
        item.evidence_id.removeprefix("ev_business_")
        for item in packet.evidence
        if item.category == "business_input"
    }
    missing_fields = {
        field for field, _, _ in _DEPENDENCIES if field not in present_business_fields
    }
    expected_withheld = {
        asset for field, _, assets in _DEPENDENCIES if field in missing_fields for asset in assets
    }
    if not draft.summary.strip() or _PLACEHOLDER_RE.search(draft.summary):
        errors.append("$.draft.summary")
    asset_ids = [asset.asset_id for asset in draft.assets]
    if "diagnosis" not in asset_ids or len(asset_ids) != len(set(asset_ids)):
        errors.append("$.draft.assets")
    for index, asset in enumerate(draft.assets):
        path = f"$.draft.assets[{index}]"
        if asset.status == "renderable" and not asset.content:
            errors.append(f"{path}.content")
        if asset.status == "withheld" and not asset.withheld_by:
            errors.append(f"{path}.withheld_by")
        if asset.asset_id in expected_withheld and asset.status != "withheld":
            errors.append(f"{path}.status")
        if asset.status == "withheld" and asset.asset_id not in expected_withheld:
            errors.append(f"{path}.status")
        if any(
            _PLACEHOLDER_RE.search(value) or _HEADING_ONLY_RE.fullmatch(value.strip())
            for value in asset.content.values()
        ):
            errors.append(f"{path}.content")
    for index, claim in enumerate(draft.claims):
        path = f"$.draft.claims[{index}]"
        refs = claim.evidence_ids
        if claim.kind == "identity" or not refs or any(ref not in allowed for ref in refs):
            errors.append(path)
            continue
        required_prefixes = _CLAIM_EVIDENCE_PREFIXES.get(claim.kind)
        if required_prefixes is not None and not any(
            ref.startswith(required_prefixes) for ref in refs
        ):
            errors.append(f"{path}.evidence_ids")
            continue
        if _URL_RE.search(claim.value):
            errors.append(f"{path}.value")
            continue
        supported_values = [evidence_by_id[ref].casefold() for ref in refs]
        claim_value = claim.value.casefold()
        supported = any(
            (
                re.search(rf"(?<!\d){re.escape(value)}(?!\d)", claim_value) is not None
                if value.isdigit()
                else value in claim_value
            )
            for value in supported_values
        )
        if not supported:
            errors.append(f"{path}.value")
    try:
        _assert_pii_free(draft.model_dump(mode="json"), "$.draft")
    except ValueError as exc:
        errors.append(str(exc).split(" at ")[-1])
    if errors:
        raise GroundingError(sorted(set(errors)))


class FixtureGroundedStrategyProvider:
    """Byte-stable offline provider using only the supplied evidence catalog."""

    configured_model_id = "fixture-strategy-v1"

    def invoke(self, request: StrategyProviderRequestV1) -> StrategyProviderResponseV1:
        value_fact = next(item for item in request.packet.evidence if item.category == "map_value")
        business_values = {
            item.evidence_id.removeprefix("ev_business_"): str(item.value)
            for item in request.packet.evidence
            if item.category == "business_input"
        }
        flags = decision_flags(StrategyBusinessProfileV1.model_validate(business_values))
        withheld = sorted({asset for flag in flags for asset in flag.withheld_assets})
        assets = [
            StrategyAssetV1(
                asset_id="diagnosis",
                status="renderable",
                content={"body": f"Observed opportunity: {value_fact.value}."},
            )
        ]
        assets.extend(
            StrategyAssetV1(
                asset_id=asset_id,
                status="withheld",
                withheld_by=[flag.flag for flag in flags if asset_id in flag.withheld_assets],
            )
            for asset_id in withheld
        )
        draft = StrategyDraftV1(
            summary=f"Review the observed {value_fact.value} opportunity.",
            claims=[
                StrategyClaimV1(
                    path="$.summary",
                    kind="number",
                    value=str(value_fact.value),
                    evidence_ids=[value_fact.evidence_id],
                )
            ],
            assets=assets,
        )
        return StrategyProviderResponseV1(
            returned_model_id=self.configured_model_id,
            response_id="fixture-response-v1",
            token_usage=StrategyTokenUsageV1(input_tokens=0, output_tokens=0),
            draft=draft,
        )


class ConfiguredStrategyProvider:
    """Configured adapter shell; transport is injected and credentials are never persisted."""

    def __init__(
        self,
        configured_model_id: str,
        transport: Callable[
            [StrategyProviderRequestV1], StrategyProviderResponseV1 | dict[str, Any]
        ],
    ) -> None:
        from found_money.safety.audit import SafeRequestAuditor

        self.configured_model_id = configured_model_id
        self._transport = transport
        self.auditor = SafeRequestAuditor()

    def invoke(self, request: StrategyProviderRequestV1) -> StrategyProviderResponseV1:
        from found_money.safety.allowlist import MODEL_HOST

        self.auditor.record(method="POST", host=MODEL_HOST, path="/v1/responses")
        raw = self._transport(request)
        if isinstance(raw, StrategyProviderResponseV1):
            return raw
        return StrategyProviderResponseV1.model_validate(raw)


def run_strategy_boundary(
    money_map: MoneyMapV1,
    profile: StrategyBusinessProfileV1,
    provider: StrategyProvider,
    *,
    configured_model_id: str,
    built_at: datetime | None = None,
) -> StrategyExecution:
    """Run at most two attempts and fail closed without changing the Money Map."""
    packet = build_grounded_strategy_packet(money_map, profile, built_at=built_at)
    request = build_provider_request(packet, configured_model_id=configured_model_id)
    response: StrategyProviderResponseV1 | None = None
    failure_code: str | None = None
    attempts = 0
    for attempts in range(1, 3):
        try:
            response = provider.invoke(request)
            validate_grounded_draft(response.draft, packet)
            failure_code = None
            break
        except ProviderRefusalError:
            failure_code = "provider_refusal"
        except ProviderTimeoutError:
            failure_code = "provider_timeout"
        except (ValidationError, GroundingError, ValueError, TypeError):
            failure_code = "malformed_or_ungrounded_output"
        response = None

    decisions = decision_flags(profile)
    completed = response is not None
    receipt = StrategyAuditReceiptV1(
        run_id=money_map.run_id,
        status="completed" if completed else "needs_strategy_review",
        prompt_version=request.prompt_version,
        prompt_hash=request.prompt_hash,
        output_schema_version=request.output_schema_version,
        output_schema_hash=request.output_schema_hash,
        configured_model_id=request.configured_model_id,
        returned_model_id=response.returned_model_id if response else None,
        response_id=response.response_id if response else None,
        token_usage=response.token_usage
        if response
        else StrategyTokenUsageV1(input_tokens=0, output_tokens=0),
        attempt_count=attempts,
        evidence_packet_hash=_sha256(packet.to_canonical_json()),
        output_hash=(
            _sha256(
                (
                    json.dumps(
                        response.draft.model_dump(mode="json"),
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                ).encode()
            )
            if response
            else None
        ),
        failure_code=failure_code,
    )
    result = StrategyBoundaryResultV1(
        status="completed" if completed else "needs_strategy_review",
        packet=packet,
        draft=response.draft if response else None,
        decisions=decisions,
        receipt=receipt,
    )
    return StrategyExecution(money_map=money_map, boundary=result)


def write_strategy_boundary_artifact(
    output_root: str,
    relative_path: str,
    result: StrategyBoundaryResultV1,
) -> str:
    """Write via the existing fail-closed safe writer without duplicating path policy."""
    from pathlib import Path

    from found_money.strategy import _atomic_write_bytes, _validate_relative_under_root

    destination = _validate_relative_under_root(Path(output_root), relative_path)
    _assert_pii_free(result.canonical_dict())
    _atomic_write_bytes(destination, result.to_canonical_json())
    return str(destination)


def _strategy_destination(output_root: str, relative_path: str):
    from pathlib import Path

    from found_money.strategy import _validate_relative_under_root

    return _validate_relative_under_root(Path(output_root), relative_path)


def _write_strategy_artifact(output_root: str, relative_path: str, payload: bytes) -> str:
    from found_money.strategy import _atomic_write_bytes

    destination = _strategy_destination(output_root, relative_path)
    _atomic_write_bytes(destination, payload)
    return str(destination)


def write_strategy_provider_request(
    output_root: str, relative_path: str, request: StrategyProviderRequestV1
) -> str:
    _assert_pii_free(request.canonical_dict())
    return _write_strategy_artifact(output_root, relative_path, request.to_canonical_json())


def write_strategy_provider_response(
    output_root: str, relative_path: str, response: StrategyProviderResponseV1
) -> str:
    _assert_pii_free(response.model_dump(mode="json"))
    return _write_strategy_artifact(output_root, relative_path, response.to_canonical_json())


def write_strategy_audit_receipt(
    output_root: str,
    relative_path: str,
    receipt: StrategyAuditReceiptV1,
    *,
    auditor: SafeRequestAuditor,
) -> str:
    from pathlib import Path

    from found_money.safety.audit import build_no_mutation_assertion
    from found_money.safety.writers import write_artifact_set_atomic

    _assert_pii_free(receipt.model_dump(mode="json"))
    attached = {
        "strategy-audit-receipt": _sha256(receipt.to_canonical_json()),
        "evidence-packet": receipt.evidence_packet_hash,
    }
    assertion = build_no_mutation_assertion(auditor, attached_receipt_hashes=attached)
    parent = Path(relative_path).parent.as_posix()
    assertion_rel = (
        "strategy/no-mutation-assertion.json"
        if parent in {"", "."}
        else f"{parent}/no-mutation-assertion.json"
    )
    if Path(relative_path).as_posix() == assertion_rel:
        raise ValueError("strategy receipt and no-mutation assertion paths must differ")
    destinations = write_artifact_set_atomic(
        Path(output_root),
        {
            relative_path: receipt.to_canonical_json(),
            assertion_rel: assertion.to_canonical_json(),
        },
    )
    return str(destinations[relative_path])


def parse_strategy_boundary_json(data: bytes) -> StrategyBoundaryResultV1:
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"malformed JSON: {exc}") from exc
    model = StrategyBoundaryResultV1.model_validate(payload)
    if model.to_canonical_json() != bytes(data):
        raise ValueError("serialized bytes are not exactly canonical")
    _assert_pii_free(model.canonical_dict())
    return model


def public_strategy_boundary_projection(result: StrategyBoundaryResultV1) -> dict[str, Any]:
    """Minimal public-safe proof without evidence, business facts, or provider metadata."""
    from found_money.redaction import assert_public_safe

    projection = {
        "schema_version": "strategy-boundary-public.v1",
        "status": result.status,
        "decision_flags": [decision.flag for decision in result.decisions],
        "renderable_asset_ids": [
            asset.asset_id
            for asset in (result.draft.assets if result.draft else [])
            if asset.status == "renderable"
        ],
        "withheld_asset_ids": sorted(
            {asset for decision in result.decisions for asset in decision.withheld_assets}
        ),
    }
    assert_public_safe(projection)
    return projection


def render_strategy_boundary_mechanical_html(result: StrategyBoundaryResultV1) -> str:
    """Mechanical proof that renderable content survives selective withholding."""
    rows: list[str] = []
    for asset in result.draft.assets if result.draft else []:
        if asset.status == "renderable":
            body = " ".join(asset.content.values())
            rows.append(
                f'<section data-asset="{html.escape(asset.asset_id)}">{html.escape(body)}</section>'
            )
    withheld_by: dict[str, list[str]] = {}
    for decision in result.decisions:
        for asset_id in decision.withheld_assets:
            withheld_by.setdefault(asset_id, []).append(decision.flag)
    for asset_id, flags in sorted(withheld_by.items()):
        reason = ", ".join(flags)
        rows.append(
            f'<section data-asset="{html.escape(asset_id)}" data-status="withheld">'
            f"Withheld pending {html.escape(reason)}</section>"
        )
    return "<!doctype html><html><body>" + "".join(rows) + "</body></html>"
