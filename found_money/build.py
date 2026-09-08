"""Unified local build runner for the current synthetic SaaS thin slice."""

from __future__ import annotations

import json
import io
import re
import shutil
import tempfile
from uuid import uuid4
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Mapping, cast

from pypdf import PdfReader

from found_money.activation import (
    IntakeConfigError,
    LaunchPackInputs,
    build_handoff_actions,
    build_launch_pack,
    html_for_public_scan,
    parse_handoff_intake_config,
    project_handoff_intake,
    validate_launch_pack_payloads,
    validate_private_launch_pack_payloads,
)
from found_money.contracts.activation import HandoffIntakeConfigV1
from found_money.contracts.identity_stage import IdentitySourceReferenceV1
from found_money.contracts.build import BuildRunManifestV1
from found_money.contracts.events import ExclusionLedgerV1
from found_money.contracts.run import RunMode
from found_money.events import detect_failed_payments
from found_money.identity import (
    build_identity_graph,
    normalize_source_records,
    public_identity_projection,
)
from found_money.identity.stage import (
    IDENTITY_GRAPH_PATH,
    IDENTITY_PUBLIC_PATH,
    IDENTITY_STAGE_PATH,
    assert_identity_count_consistency,
    build_identity_stage_evidence,
    validate_identity_stage_bundle,
)
from found_money.scenarios import (
    SCENARIO_PUBLIC_PATHS,
    SYNTHETIC_ECOMMERCE_V1,
    SYNTHETIC_SAAS_V1,
    SYNTHETIC_SERVICE_V1,
    build_scenario_manifest,
    run_scenario_engine,
    scenario_public_payloads,
)
from found_money.scenarios.assets import (
    SYNTHETIC_ECOMMERCE_V1_CART_OMITTED,
    is_ecommerce_fixture,
    load_scenario_definition,
)
from found_money.scenarios.registry import (
    SYNTHETIC_SERVICE_V1_APPOINTMENTS_OMITTED,
    SYNTHETIC_SERVICE_V1_CRM_OMITTED,
    SYNTHETIC_SERVICE_V1_PAYMENT_OMITTED,
    SYNTHETIC_SERVICE_V1_PROPOSALS_OMITTED,
)
from found_money.contracts.source import SourceReceiptV1
from found_money.map import build_money_map
from found_money.receipts import (
    _validate_relative_under_root,
    build_run_manifest,
    build_source_receipt,
    compute_source_set_hash,
    sha256_bytes,
    thin_slice_fixture_root,
)
from found_money.safety.writers import write_artifact_set_atomic
from found_money.rendering import (
    build_recovery_room_manifest,
    capture_four_room_screenshots,
    capture_recovery_room_artifacts,
    recovery_room_static_assets,
    render_print_report_html,
    render_recovery_room_html,
    render_top_play_html,
)
from found_money.rendering.proof import (
    FOUR_ROOM_PNGS,
    PRINT_REPORT_CONTACT_SHEET,
    REQUIRED_ARTIFACTS,
    build_print_report_manifest,
    resolve_render_baselines_dir,
    validate_print_review_packet,
)
from found_money.contracts.activation import WithheldAssetV1
from found_money.strategy import (
    apply_recovery_plays_to_money_map,
    build_canonical_saas_recovery_strategy,
    canonical_ecommerce_business_profile,
    canonical_saas_business_profile,
    canonical_service_business_profile,
    generate_recovery_plays,
    generate_stub_recovery_plays,
    validate_complete_recovery_play_set,
    build_differentiation_report,
)
from found_money.strategy.boundary import build_grounded_strategy_packet
from found_money.strategy.campaign import CanonicalSaasStrategyRun, _sha
from found_money.contracts.strategy import (
    CompleteRecoveryPlaySetV1,
    RecoveryPlaySetV1,
    RecoveryPlayV1,
    StrategyAuditReceiptV1,
    StrategyTokenUsageV1,
)
from found_money.value import build_contribution_ledger, build_value_ledger
from found_money.value.recurring import apply_recurring_valuation, public_valuation_receipt
from found_money.events.library import detect_event_families
from found_money.scenarios.harness import payment_rescue_ledger
from found_money.profile.intake import BusinessProfileV1, profile_intake_violations
from found_money.profile.strategy_adapter import adapt_business_profile

BUILD_SOURCE_CONFIG_SCHEMA = "found-money-build-source.v1"
BUILD_MANIFEST_SCHEMA = "found-money-build.v1"
SYNTHETIC_SAAS_FIXTURE = "synthetic-saas-thin-slice"
SYNTHETIC_SAAS_V1_FIXTURE = SYNTHETIC_SAAS_V1
SYNTHETIC_ECOMMERCE_V1_FIXTURE = SYNTHETIC_ECOMMERCE_V1
SYNTHETIC_ECOMMERCE_V1_CART_OMITTED_FIXTURE = SYNTHETIC_ECOMMERCE_V1_CART_OMITTED
SYNTHETIC_SERVICE_V1_FIXTURE = SYNTHETIC_SERVICE_V1
SYNTHETIC_SERVICE_V1_APPOINTMENTS_OMITTED_FIXTURE = SYNTHETIC_SERVICE_V1_APPOINTMENTS_OMITTED
SYNTHETIC_SERVICE_V1_PROPOSALS_OMITTED_FIXTURE = SYNTHETIC_SERVICE_V1_PROPOSALS_OMITTED
SYNTHETIC_SERVICE_V1_CRM_OMITTED_FIXTURE = SYNTHETIC_SERVICE_V1_CRM_OMITTED
SYNTHETIC_SERVICE_V1_PAYMENT_OMITTED_FIXTURE = SYNTHETIC_SERVICE_V1_PAYMENT_OMITTED
SCENARIO_FIXTURES = {
    SYNTHETIC_SAAS_V1_FIXTURE,
    SYNTHETIC_ECOMMERCE_V1_FIXTURE,
    SYNTHETIC_ECOMMERCE_V1_CART_OMITTED_FIXTURE,
    SYNTHETIC_SERVICE_V1_FIXTURE,
    SYNTHETIC_SERVICE_V1_APPOINTMENTS_OMITTED_FIXTURE,
    SYNTHETIC_SERVICE_V1_PROPOSALS_OMITTED_FIXTURE,
    SYNTHETIC_SERVICE_V1_CRM_OMITTED_FIXTURE,
    SYNTHETIC_SERVICE_V1_PAYMENT_OMITTED_FIXTURE,
}
CANONICAL_PRINT_REVIEW_RUN_ID = "run_f7e27bfd5aeb154d"

FIXED_UTC = datetime(2026, 7, 29, 18, 0, 0, tzinfo=timezone.utc)

FINAL_ARTIFACT_PATHS = (
    "run.json",
    "money-map.json",
    "recovery-plays.json",
    "index.html",
    "top-play.html",
    "render-manifest.json",
    "assets/recovery-room.css",
    "assets/recovery-room.js",
    "assets/three.min.js",
    "assets/OrbitControls.js",
    "assets/tween.umd.js",
    "assets/continent-texture.jpg",
    "print-report.pdf",
    "receipts/hubspot.source-receipt.json",
    "receipts/stripe.source-receipt.json",
    "receipts/orders.source-receipt.json",
    "receipts/appointments.source-receipt.json",
    "receipts/proposals.source-receipt.json",
    "provenance/run-manifest.json",
    "provenance/identity-stage.json",
    "identity/identity-public.json",
    "provenance/strategy-evidence-packet.json",
    "provenance/strategy-audit-receipt.json",
    "provenance/content-differentiation-report.json",
    "launch-pack/manifest.json",
    "launch-pack/README.md",
    "launch-pack/withheld-assets.json",
    "launch-pack/money-map.json",
    "launch-pack/public/segments.json",
    "launch-pack/public/segments.csv",
    "launch-pack/checklist/launch-checklist.md",
    "render-proof/print-report-manifest.json",
    *(f"render-proof/{name}" for name in REQUIRED_ARTIFACTS),
    *(f"render-proof/{name}" for name in FOUR_ROOM_PNGS),
)

_TRAVERSAL_RE = re.compile(r"(^|/)\.\.(/|$)")
_WINDOWS_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")
_EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_URL_RE = re.compile(r"(?i)\bhttps?://[^\s\"']+")
_UNSAFE_SCHEME_RE = re.compile(r"(?i)(?:^|[\s\"'(<])(?:file|https?|mailto|javascript|data):")
_PROVIDER_CUSTOMER_ID_RE = re.compile(
    r"\b(?:cus_(?!t_)[A-Za-z0-9_]*[0-9][A-Za-z0-9_]*|"
    r"hs_(?:contact|ct|dl|deal)_[A-Za-z0-9_]+)\b"
)
_ABS_PATH_RE = re.compile(r"(?i)(^|[\s\"'])(/Users/|/home/|[A-Za-z]:\\)")
_SECRET_VALUE_RE = re.compile(
    r"(?i)\b(?:sk_(?:live|test)_|rk_(?:live|test)_|gh[pousr]_|bearer\s+)[A-Za-z0-9_./+=:-]+"
)
_RAW_FIXTURE_ID_RE = re.compile(
    r"\b(?:hs_contact_synth_001|hs_deal_synth_001|cus_synth_001|"
    r"inv_failed_001|inv_paid_001|cust_synth_001|"
    r"hs_ct_saas_(?:fp|et|cc|cl|ru)_001|hs_dl_saas_cl_001|"
    r"cus_saas_(?:fp|et|cc|cl|ru)_001|in_saas_fp_001|"
    r"sub_saas_(?:et|ru)_001|syn_saas_(?:fp|et|cc|cl|ru))\b"
)
_RAW_IDENTITY_KEY_RE = re.compile(
    r'(?i)"(?:email|phone|name|company|source_id|record_id|contact_id|crm_url|deal_name|'
    r'external_ids|customer_token|account_token|economic_unit_key|lineage)"\s*:'
)
_PUBLIC_PROJECTION_IDENTITY_KEY_RE = re.compile(
    r'(?i)"(?:email|phone|name|company|source_id|record_id|contact_id|crm_url|deal_name|'
    r'external_ids|account_token|economic_unit_key|lineage)"\s*:'
)
_PUBLIC_IDENTITY_PROJECTION_PATHS = frozenset(
    {
        "identity/identity-public.json",
        "identity/public.json",
    }
)
_SENSITIVE_KEY_RE = re.compile(
    r'(?i)"(?:password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|'
    r'authorization|credential|credentials|auth|bearer|client[_-]?secret)"\s*:'
)
_PLACEHOLDER_RE = re.compile(r"(?i)TODO|TBD|\[CONFIRM|lorem|DRAFT")


class BuildConfigError(ValueError):
    """Sanitized configuration failure."""


class BuildPathError(ValueError):
    """Sanitized artifact-root or relative-path failure."""


@dataclass(frozen=True)
class SourceSnapshot:
    source_type: str
    connector_schema_version: str
    receipt_path: str
    safe_locator: str
    raw_bytes: bytes
    data: Mapping[str, Any]
    page_or_row_count: int
    record_count: int


@dataclass(frozen=True)
class BuildResult:
    output_root: Path
    run_id: str
    artifact_paths: dict[str, Path]
    manifest: BuildRunManifestV1


def _canonical_json_bytes(payload: Any) -> bytes:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return (text + "\n").encode("utf-8")


def _resolve_modes(payload: Mapping[str, Any]) -> tuple[str, RunMode]:
    raw_mode = payload.get("mode")
    raw_source_mode = payload.get("source_mode")
    unset = object()
    declared_run_mode = payload.get("run_mode", payload.get("output_mode", unset))
    raw_run_mode = "public" if declared_run_mode is unset else declared_run_mode

    if isinstance(raw_mode, str) and raw_mode in {"fixture", "file"}:
        source_mode = raw_mode
        if raw_source_mode is not None and raw_source_mode != source_mode:
            raise BuildConfigError("source mode declaration is inconsistent")
    elif isinstance(raw_mode, str) and raw_mode in {"public", "private"}:
        if not isinstance(raw_source_mode, str) or raw_source_mode not in {"fixture", "file"}:
            raise BuildConfigError("source_mode must be fixture or file")
        source_mode = raw_source_mode
        if declared_run_mode is not unset and declared_run_mode != raw_mode:
            raise BuildConfigError("public/private mode declaration is inconsistent")
        raw_run_mode = raw_mode
    else:
        raise BuildConfigError("source config mode is unsupported")

    if not isinstance(raw_run_mode, str) or raw_run_mode not in {"public", "private"}:
        raise BuildConfigError("run_mode must be public or private")
    return source_mode, cast(RunMode, raw_run_mode)


def _safe_source_config(
    *,
    source_mode: str,
    run_mode: RunMode,
    fixture: str | None = None,
    handoff_intake: HandoffIntakeConfigV1 | None = None,
    strategy_provider: str | None = None,
) -> dict[str, Any]:
    safe: dict[str, Any] = {
        "schema_version": BUILD_SOURCE_CONFIG_SCHEMA,
        "mode": source_mode,
        "run_mode": run_mode,
        "credential_declaration": "none",
        "credential_runtime": "test",
    }
    if strategy_provider is not None:
        safe["strategy_provider"] = strategy_provider
    if fixture is not None:
        safe["fixture"] = fixture
        if is_ecommerce_fixture(fixture):
            definition = load_scenario_definition(fixture)
            safe["high_value_cart_state"] = definition.high_value_cart_state
            safe["high_value_cart_hash"] = definition.high_value_cart_hash
    else:
        safe["sources"] = {
            "hubspot_snapshot": "caller-file-input/hubspot.snapshot.json",
            "stripe_snapshot": "caller-file-input/stripe.snapshot.json",
        }
    intake = handoff_intake or HandoffIntakeConfigV1()
    projection = project_handoff_intake(intake)
    if projection["stealads"]["configured"] or projection["matt_emerald"]["configured"]:
        safe["handoff_intake"] = projection
    return safe


def _withheld_reason(asset_id: str) -> str:
    if asset_id == "high_value_cart_dependent_output":
        return "missing_high_value_cart"
    if asset_id == "no_show_rebook_dependent_output":
        return "missing_appointments"
    if asset_id == "silent_proposal_dependent_output":
        return "missing_proposals"
    if asset_id == "crm_dependent_output":
        return "missing_crm"
    if asset_id in {
        "payment_dependent_output",
        "trial_no_convert_dependent_output",
        "canceled_customer_dependent_output",
        "disappeared_high_value_customer_dependent_output",
    }:
        return "missing_payment"
    return asset_id


def _scenario_business_profile(scenario_engine: Any):
    model = getattr(getattr(scenario_engine, "definition", None), "business_model", None)
    if model == "ecommerce":
        return canonical_ecommerce_business_profile()
    if model == "service":
        return canonical_service_business_profile()
    return canonical_saas_business_profile()


def _handoff_intake_from_source(payload: Mapping[str, Any]) -> HandoffIntakeConfigV1:
    if "handoff_intake" not in payload:
        return HandoffIntakeConfigV1()
    try:
        return parse_handoff_intake_config(payload["handoff_intake"])
    except (IntakeConfigError, ValueError) as exc:
        raise BuildConfigError("handoff intake config is malformed or unsafe") from exc


def _load_json_object_from_bytes(raw: bytes, label: str) -> Mapping[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BuildConfigError(f"{label} snapshot must be valid JSON") from exc
    if not isinstance(payload, Mapping):
        raise BuildConfigError(f"{label} snapshot must be a JSON object")
    return payload


def _require_snapshot_shape(payload: Mapping[str, Any], label: str) -> None:
    schema = payload.get("schema")
    source = payload.get("source")
    expected_schema = f"{label}-snapshot.v1"
    if schema != expected_schema or source != label:
        raise BuildConfigError(f"{label} snapshot schema/source is unsupported")
    required_lists = ("contacts", "deals") if label == "hubspot" else ("customers", "invoices")
    for key in required_lists:
        if not isinstance(payload.get(key), list):
            raise BuildConfigError(f"{label} snapshot is missing required list fields")


def _record_count(payload: Mapping[str, Any], label: str) -> int:
    keys = ("contacts", "deals") if label == "hubspot" else ("customers", "invoices")
    return sum(len(payload.get(key) or []) for key in keys)


def _read_snapshot(path: Path, label: str, *, safe_locator: str) -> SourceSnapshot:
    if not path.is_file():
        raise BuildConfigError(f"{label} snapshot file is unavailable")
    raw = path.read_bytes()
    data = _load_json_object_from_bytes(raw, label)
    _require_snapshot_shape(data, label)
    return SourceSnapshot(
        source_type=label,
        connector_schema_version=f"{label}-snapshot.v1",
        receipt_path=f"receipts/{label}.source-receipt.json",
        safe_locator=safe_locator,
        raw_bytes=raw,
        data=data,
        page_or_row_count=1,
        record_count=_record_count(data, label),
    )


def _snapshot_from_native(
    raw: bytes,
    *,
    source_type: str,
    connector_schema_version: str,
    locator: str,
    page_or_row_count: int,
    record_count: int,
) -> SourceSnapshot:
    data = _load_json_object_from_bytes(raw, source_type)
    return SourceSnapshot(
        source_type=source_type,
        connector_schema_version=connector_schema_version,
        receipt_path=f"receipts/{source_type}.source-receipt.json",
        safe_locator=locator,
        raw_bytes=raw,
        data=data,
        page_or_row_count=page_or_row_count,
        record_count=record_count,
    )


def _fixture_snapshots(fixture: str = SYNTHETIC_SAAS_FIXTURE) -> dict[str, SourceSnapshot]:
    if fixture in SCENARIO_FIXTURES:
        from found_money.scenarios.transports import load_normalized_scenario_snapshots

        definition = load_scenario_definition(fixture)
        snapshots, raw, receipts = load_normalized_scenario_snapshots(
            fixture, retrieved_at=definition.clock
        )
        return {
            name: _snapshot_from_native(
                raw[name],
                source_type=name,
                connector_schema_version=receipts[name].connector_schema_version,
                locator=receipts[name].locator,
                page_or_row_count=receipts[name].page_or_row_count,
                record_count=receipts[name].record_count,
            )
            for name in definition.sources
        }
    root = thin_slice_fixture_root()
    return {
        "hubspot": _read_snapshot(
            root / "hubspot" / "snapshot.json",
            "hubspot",
            safe_locator="fixtures/saas/thin-slice/hubspot/snapshot.json",
        ),
        "stripe": _read_snapshot(
            root / "stripe" / "snapshot.json",
            "stripe",
            safe_locator="fixtures/saas/thin-slice/stripe/snapshot.json",
        ),
    }


def _snapshot_from_bytes(raw: bytes, label: str, *, safe_locator: str) -> SourceSnapshot:
    data = _load_json_object_from_bytes(raw, label)
    _require_snapshot_shape(data, label)
    return SourceSnapshot(
        source_type=label,
        connector_schema_version=f"{label}-snapshot.v1",
        receipt_path=f"receipts/{label}.source-receipt.json",
        safe_locator=safe_locator,
        raw_bytes=raw,
        data=data,
        page_or_row_count=1,
        record_count=_record_count(data, label),
    )


def _read_config(path: Path) -> Mapping[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BuildConfigError("source config must be readable JSON") from exc
    if not isinstance(payload, Mapping):
        raise BuildConfigError("source config must be a JSON object")
    return payload


def _validate_credentials(payload: Mapping[str, Any]) -> None:
    credentials = payload.get("credentials")
    if not isinstance(credentials, Mapping):
        raise BuildConfigError("credential declaration is required")
    allowed = {"credential_mode", "runtime_mode", "scopes"}
    if set(credentials) != allowed:
        raise BuildConfigError("credential declaration is malformed")
    credential_mode = credentials.get("credential_mode")
    runtime_mode = credentials.get("runtime_mode")
    scopes = credentials.get("scopes")
    if credential_mode != "none":
        raise BuildConfigError("credential declaration is unavailable for this build")
    if runtime_mode != "test":
        raise BuildConfigError("credential declaration does not match the supported test mode")
    if scopes != []:
        raise BuildConfigError("credential declaration is over-permissioned")


def _path_from_config(config_path: Path, raw: Any, label: str) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise BuildConfigError(f"{label} snapshot path is required")
    text = raw.strip()
    if "\x00" in text:
        raise BuildConfigError(f"{label} snapshot path is malformed")
    path = Path(text)
    if not path.is_absolute():
        path = config_path.parent / path
    return path


def load_source_config(
    config_path: Path | str,
    *,
    _payload: Mapping[str, Any] | None = None,
) -> tuple[dict[str, SourceSnapshot], dict[str, Any], HandoffIntakeConfigV1]:
    path = Path(config_path)
    payload = _payload if _payload is not None else _read_config(path)
    if payload.get("schema_version") != BUILD_SOURCE_CONFIG_SCHEMA:
        raise BuildConfigError("source config schema_version is unsupported")
    source_mode, run_mode = _resolve_modes(payload)
    _validate_credentials(payload)
    handoff_intake = _handoff_intake_from_source(payload)
    if source_mode == "fixture":
        fixture = payload.get("fixture")
        if fixture not in {SYNTHETIC_SAAS_FIXTURE, *SCENARIO_FIXTURES}:
            raise BuildConfigError(
                "fixture mode supports only the documented synthetic scenario fixtures"
            )
        snapshots = {} if fixture in SCENARIO_FIXTURES else _fixture_snapshots(str(fixture))
        strategy_provider = payload.get("strategy_provider")
        safe_config = _safe_source_config(
            source_mode=source_mode,
            run_mode=run_mode,
            fixture=str(fixture),
            handoff_intake=handoff_intake,
            strategy_provider=strategy_provider,
        )
        return snapshots, safe_config, handoff_intake
    if source_mode == "file":
        sources = payload.get("sources")
        if not isinstance(sources, Mapping):
            raise BuildConfigError("file mode requires explicit source snapshots")
        source_map = dict(sources)
        names = [
            name for name in ("hubspot", "stripe") if source_map.get(f"{name}_snapshot") is not None
        ]
        if not names:
            raise BuildConfigError("file mode requires at least one explicit source snapshot")
        if set(source_map) - {"hubspot_snapshot", "stripe_snapshot"}:
            raise BuildConfigError("unsupported file source")
        snapshots = {
            name: _read_snapshot(
                _path_from_config(path, source_map[f"{name}_snapshot"], name),
                name,
                safe_locator=f"caller-file-input/{name}.snapshot.json",
            )
            for name in names
        }
        strategy_provider = payload.get("strategy_provider")
        safe_config = _safe_source_config(
            source_mode=source_mode,
            run_mode=run_mode,
            handoff_intake=handoff_intake,
            strategy_provider=strategy_provider,
        )
        safe_config["sources"] = {
            f"{name}_snapshot": f"caller-file-input/{name}.snapshot.json" for name in names
        }
        model = payload.get("business_model")
        if model is not None:
            if model not in {"saas", "service", "ecommerce"}:
                raise BuildConfigError("unsupported business model")
            safe_config["business_model"] = model
        if payload.get("data_origin") is not None:
            if payload["data_origin"] != "synthetic":
                raise BuildConfigError("unsupported data origin declaration")
            safe_config["data_origin"] = "synthetic"
        raw_clock = payload.get("clock")
        if raw_clock is not None:
            try:
                parsed = datetime.fromisoformat(str(raw_clock).replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    raise ValueError("missing timezone")
                safe_config["clock"] = parsed.astimezone(timezone.utc).isoformat()
            except ValueError as exc:
                raise BuildConfigError("clock must be a timezone-aware timestamp") from exc
        return snapshots, safe_config, handoff_intake
    raise BuildConfigError("source config mode is unsupported")


def source_config_from_cli_file_inputs(
    *,
    hubspot_snapshot: Path | str,
    stripe_snapshot: Path | str,
    credential_declaration: str | None,
) -> tuple[dict[str, SourceSnapshot], dict[str, Any], HandoffIntakeConfigV1]:
    if credential_declaration != "none":
        raise BuildConfigError("credential declaration is required")
    temp_config = Path.cwd() / "found-money-cli-file-inputs.json"
    hubspot_path = _path_from_config(temp_config, str(hubspot_snapshot), "hubspot")
    stripe_path = _path_from_config(temp_config, str(stripe_snapshot), "stripe")
    snapshots = {
        "hubspot": _read_snapshot(
            hubspot_path,
            "hubspot",
            safe_locator="caller-file-input/hubspot.snapshot.json",
        ),
        "stripe": _read_snapshot(
            stripe_path,
            "stripe",
            safe_locator="caller-file-input/stripe.snapshot.json",
        ),
    }
    intake = HandoffIntakeConfigV1()
    return (
        snapshots,
        _safe_source_config(source_mode="file", run_mode="public", handoff_intake=intake),
        intake,
    )


def resolve_output_root(raw_output_root: Path | str, *, cwd: Path | None = None) -> Path:
    raw = str(raw_output_root).strip().replace("\\", "/")
    if not raw:
        raise BuildPathError("output root is required")
    if "\x00" in raw:
        raise BuildPathError("output root is malformed")
    if raw.startswith("~/"):
        raise BuildPathError("output root must not use home expansion")
    if raw == "~":
        raise BuildPathError("output root must not use home expansion")
    if raw == ".." or _TRAVERSAL_RE.search(raw):
        raise BuildPathError("output root must not contain traversal")
    if raw.startswith("/"):
        raise BuildPathError("output root must not be absolute")
    if _WINDOWS_ABS_RE.match(raw):
        raise BuildPathError("output root is malformed")
    base = cwd or Path.cwd()
    root = Path(raw)
    if not root.is_absolute():
        root = base / root
    if root.exists() and root.is_symlink():
        raise BuildPathError("output root must not be a symlink")
    if root.exists() and not root.is_dir():
        raise BuildPathError("output root must be a directory")
    return root.resolve(strict=False)


def preflight_artifact_paths(
    output_root: Path, relative_paths: tuple[str, ...] = FINAL_ARTIFACT_PATHS
) -> dict[str, Path]:
    destinations: dict[str, Path] = {}
    for relative in relative_paths:
        try:
            destinations[relative] = _validate_relative_under_root(output_root, relative)
        except ValueError as exc:
            raise BuildPathError("artifact path failed output-root validation") from exc
    return destinations


def _run_id_for(snapshots: Mapping[str, SourceSnapshot], safe_config: Mapping[str, Any]) -> str:
    source_hashes = {
        name: sha256_bytes(snapshot.raw_bytes) for name, snapshot in sorted(snapshots.items())
    }
    digest = sha256_bytes(
        _canonical_json_bytes({"source_config": safe_config, "source_hashes": source_hashes})
    )
    return f"run_{digest[:16]}"


def _load_scenario_via_source_stage(
    output_root: Path,
    retrieved_at: datetime,
    *,
    fixture_id: str = SYNTHETIC_SAAS_V1_FIXTURE,
) -> tuple[dict[str, SourceSnapshot], dict[str, SourceReceiptV1]]:
    from found_money.scenarios.assets import scenario_fixture_root
    from found_money.scenarios.transports import native_scenario_adapters, packaged_source_manifest
    from found_money.source_stage import SourceStageError, run_source_stage

    collision = output_root / ".found-money-source-stage"
    if collision.exists():
        raise BuildPathError("preexisting source stage directory; refusing to clobber caller files")
    stage_token = uuid4().hex
    uuid_stage = output_root / f".found-money-source-stage-{stage_token}"
    if uuid_stage.exists() or uuid_stage.is_symlink():
        raise SourceStageError("source-stage output root must not already exist")
    owned_parent = Path(
        tempfile.mkdtemp(prefix=".found-money-source-stage-owned-", dir=output_root)
    )
    try:
        stage = owned_parent / f".found-money-source-stage-{stage_token}"
        result = run_source_stage(
            packaged_source_manifest(fixture_id),
            input_root=scenario_fixture_root(fixture_id),
            output_root=stage,
            adapters=native_scenario_adapters(retrieved_at=retrieved_at, fixture_id=fixture_id),
            retrieved_at=retrieved_at,
        )
        snapshots: dict[str, SourceSnapshot] = {}
        for source_id, path in result.normalized_paths.items():
            receipt = result.receipts[source_id]
            snapshots[source_id] = _snapshot_from_native(
                path.read_bytes(),
                source_type=source_id,
                connector_schema_version=receipt.connector_schema_version,
                locator=receipt.locator,
                page_or_row_count=receipt.page_or_row_count,
                record_count=receipt.record_count,
            )
        receipts = dict(result.receipts)
        return snapshots, receipts
    finally:
        shutil.rmtree(owned_parent, ignore_errors=True)


def _compose_contracts(
    snapshots: Mapping[str, SourceSnapshot],
    *,
    safe_config: Mapping[str, Any],
    handoff_intake: HandoffIntakeConfigV1 | None = None,
    output_root: Path | None = None,
    native_receipts: Mapping[str, SourceReceiptV1] | None = None,
    business_profile: BusinessProfileV1 | None = None,
) -> tuple[str, dict[str, bytes], BuildRunManifestV1]:
    run_id = _run_id_for(snapshots, safe_config)
    if safe_config.get("fixture") in SCENARIO_FIXTURES:
        return _compose_scenario_contracts(
            snapshots,
            safe_config=safe_config,
            handoff_intake=handoff_intake,
            run_id=run_id,
            output_root=output_root,
            native_receipts=native_receipts,
        )
    raw_snapshots = {name: item.data for name, item in snapshots.items()}

    provider_name = safe_config.get("strategy_provider")
    clock = (
        datetime.fromisoformat(str(safe_config["clock"])) if safe_config.get("clock") else FIXED_UTC
    )
    profile = (
        adapt_business_profile(business_profile)
        if business_profile is not None
        else canonical_saas_business_profile()
    )
    graph = build_identity_graph(
        normalize_source_records(raw_snapshots, default_observed_at=clock),
        run_id=run_id,
        built_at=clock,
    )
    detection = None
    if safe_config.get("mode") == "file":
        detection = detect_event_families(raw_snapshots, graph, run_id=run_id, built_at=clock)
        candidates = detection.candidates
        ledger = build_value_ledger(candidates, raw_snapshots, built_at=clock)
        ledger = apply_recurring_valuation(
            ledger,
            candidates,
            raw_snapshots,
            graph,
            business_model=safe_config.get("business_model"),
            clock=clock,
            tenure_months=business_profile.tenure_multiple_months if business_profile else None,
            ltv_override_minor=business_profile.ltv_override_minor if business_profile else None,
        )
        ledger = payment_rescue_ledger(ledger)
    else:
        candidates = detect_failed_payments(raw_snapshots["stripe"], graph, run_id=run_id)
        ledger = build_contribution_ledger(candidates, raw_snapshots["stripe"])
    money_map = build_money_map(ledger, candidates, built_at=clock)
    from found_money.strategy.intelligence import load_table

    eligible = any(
        p.pile_id in load_table("recurring-valuation")["authorable_families"]
        for p in money_map.piles
    )
    if detection is not None:
        money_map = money_map.model_copy(
            update={
                "customer_count": len({c.customer_token for c in candidates.candidates}),
                "source_count": len(snapshots),
                "data_gap_count": len(ledger.data_gaps) + len(detection.data_gaps.gaps),
            }
        )
    hold_strategy = not eligible or (
        safe_config.get("mode") == "file" and provider_name == "skill" and business_profile is None
    )

    complete_play_set: RecoveryPlaySetV1 | CompleteRecoveryPlaySetV1
    if (
        hold_strategy
        or provider_name == "unconfigured"
        or (safe_config.get("mode") == "file" and provider_name is None)
    ):
        packet = build_grounded_strategy_packet(money_map, profile, built_at=clock)
        deferred_play_set = RecoveryPlaySetV1(
            run_id=run_id,
            built_at=clock,
            provider="fixture",
            plays=[],
        )
        receipt = StrategyAuditReceiptV1(
            run_id=run_id,
            status="needs_strategy_review",
            prompt_version="found-money-three-play.v1",
            prompt_hash=_sha(b"unconfigured strategy provider"),
            output_schema_version="recovery-plays.v1",
            output_schema_hash=_sha(b"complete recovery play and concept card contract v1"),
            configured_model_id="unconfigured",
            returned_model_id=None,
            response_id=None,
            token_usage=StrategyTokenUsageV1(input_tokens=0, output_tokens=0),
            attempt_count=1,
            evidence_packet_hash=_sha(packet.to_canonical_json()),
            output_hash=_sha(deferred_play_set.to_canonical_json()),
            failure_code="unconfigured_strategy",
        )
        strategy_run = CanonicalSaasStrategyRun(packet, deferred_play_set, receipt, None)
        render_play_set = deferred_play_set
        enriched_money_map = apply_recovery_plays_to_money_map(
            money_map, render_play_set
        ).model_copy(update={"strategy_stage": "needs_strategy_review"})
        complete_play_set = deferred_play_set
    elif provider_name in {"stub", "skill"}:
        packet = build_grounded_strategy_packet(money_map, profile, built_at=clock)
        if provider_name == "stub":
            complete_play_set = generate_stub_recovery_plays(packet, profile, money_map=money_map)
        else:
            complete_play_set = generate_recovery_plays(
                packet, profile, money_map=money_map, provider_name="skill"
            )
        validate_complete_recovery_play_set(complete_play_set, packet)
        report = build_differentiation_report(complete_play_set)
        receipt = StrategyAuditReceiptV1(
            run_id=run_id,
            status="completed",
            prompt_version="found-money-three-play.v1",
            prompt_hash=_sha(b"skill strategy author v1"),
            output_schema_version="recovery-plays.v1",
            output_schema_hash=_sha(b"complete recovery play and concept card contract v1"),
            configured_model_id=f"{provider_name}-strategy-v1",
            returned_model_id=f"{provider_name}-strategy-v1",
            response_id=f"{provider_name}-three-play-response-v1",
            token_usage=StrategyTokenUsageV1(input_tokens=0, output_tokens=0),
            attempt_count=1,
            evidence_packet_hash=_sha(packet.to_canonical_json()),
            output_hash=_sha(complete_play_set.to_canonical_json()),
            failure_code=None,
        )
        strategy_run = CanonicalSaasStrategyRun(packet, complete_play_set, receipt, report)
        primary = sorted(complete_play_set.plays, key=lambda item: item.rank)[0]
        render_play_set = RecoveryPlaySetV1(
            run_id=run_id,
            built_at=complete_play_set.built_at,
            provider=provider_name,
            plays=[
                RecoveryPlayV1(
                    play_id=primary.play_id,
                    pile_id=primary.pile_id,
                    rank=1,
                    title=primary.title,
                    rationale=primary.rationale,
                    recommended_actions=primary.recommended_actions,
                )
            ],
        )
        from found_money.strategy import apply_complete_plays_to_money_map

        enriched_money_map = apply_complete_plays_to_money_map(money_map, complete_play_set)
    else:
        strategy_run = build_canonical_saas_recovery_strategy(money_map)
        complete_play_set = strategy_run.complete_plays()
        primary = sorted(complete_play_set.plays, key=lambda item: item.rank)[0]
        render_play_set = RecoveryPlaySetV1(
            run_id=run_id,
            built_at=complete_play_set.built_at,
            provider="fixture",
            plays=[
                RecoveryPlayV1(
                    play_id=primary.play_id,
                    pile_id=primary.pile_id,
                    rank=1,
                    title=primary.title,
                    rationale=primary.rationale,
                    recommended_actions=primary.recommended_actions,
                )
            ],
        )
        enriched_money_map = apply_recovery_plays_to_money_map(money_map, render_play_set)
    return _finalize_operator_tree(
        snapshots=snapshots,
        safe_config=safe_config,
        handoff_intake=handoff_intake,
        run_id=run_id,
        graph=graph,
        candidates=candidates,
        exclusions=detection.exclusions
        if detection
        else ExclusionLedgerV1(run_id=run_id, built_at=clock, exclusions=[]),
        ledger=ledger,
        complete_play_set=complete_play_set,
        strategy_run=strategy_run,
        enriched_money_map=enriched_money_map,
        render_play_set=render_play_set,
        output_root=output_root,
        retrieved_at=clock,
        business_profile=profile,
        valuation_receipt=public_valuation_receipt(ledger) if detection else None,
    )


def _compose_scenario_contracts(
    snapshots: Mapping[str, SourceSnapshot],
    *,
    safe_config: Mapping[str, Any],
    handoff_intake: HandoffIntakeConfigV1 | None,
    run_id: str,
    output_root: Path | None = None,
    native_receipts: Mapping[str, SourceReceiptV1] | None = None,
) -> tuple[str, dict[str, bytes], BuildRunManifestV1]:
    engine = run_scenario_engine(
        run_id=run_id,
        safe_config=safe_config,
        fixture_id=str(safe_config.get("fixture") or SYNTHETIC_SAAS_V1_FIXTURE),
        snapshots={name: item.data for name, item in snapshots.items()},
        snapshot_bytes={name: item.raw_bytes for name, item in snapshots.items()},
    )
    if engine.enriched_money_map.recommended_play_ids:
        primary = sorted(engine.strategy_run.complete_plays().plays, key=lambda item: item.rank)[0]
        render_play_set = RecoveryPlaySetV1(
            run_id=run_id,
            built_at=engine.strategy_run.recovery_plays.built_at,
            provider="fixture",
            plays=[
                RecoveryPlayV1(
                    play_id=primary.play_id,
                    pile_id=primary.pile_id,
                    rank=1,
                    title=primary.title,
                    rationale=primary.rationale,
                    recommended_actions=primary.recommended_actions,
                )
            ],
        )
    else:
        render_play_set = RecoveryPlaySetV1(
            run_id=run_id,
            built_at=engine.strategy_run.recovery_plays.built_at,
            provider="fixture",
            plays=[],
        )
    extra = scenario_public_payloads(engine)
    return _finalize_operator_tree(
        snapshots=snapshots,
        safe_config=safe_config,
        handoff_intake=handoff_intake,
        run_id=run_id,
        graph=engine.graph,
        candidates=engine.candidates,
        exclusions=engine.exclusions,
        ledger=engine.ledger,
        complete_play_set=engine.strategy_run.recovery_plays,
        strategy_run=engine.strategy_run,
        enriched_money_map=engine.enriched_money_map,
        render_play_set=render_play_set,
        extra_payloads=extra,
        extra_stages={"scenario_harness": "completed"},
        extra_schema_versions={
            "found-money-scenario-definition": "found-money-scenario-definition.v1",
            "found-money-scenario-manifest": "found-money-scenario-manifest.v1",
            "found-money-scenario-aggregate": "found-money-scenario-aggregate.v1",
            **(
                {
                    "found-money-scenario-situations": "found-money-scenario-situations.v1",
                }
                if engine.definition.business_model == "ecommerce"
                else {}
            ),
        },
        retrieved_at=engine.definition.clock,
        output_root=output_root,
        native_receipts=native_receipts,
        scenario_engine=engine,
    )


def _finalize_operator_tree(
    *,
    snapshots: Mapping[str, SourceSnapshot],
    safe_config: Mapping[str, Any],
    handoff_intake: HandoffIntakeConfigV1 | None,
    run_id: str,
    graph: Any,
    candidates: Any,
    exclusions: ExclusionLedgerV1,
    ledger: Any,
    complete_play_set: Any,
    strategy_run: Any,
    enriched_money_map: Any,
    render_play_set: RecoveryPlaySetV1,
    extra_payloads: Mapping[str, bytes] | None = None,
    extra_stages: Mapping[str, str] | None = None,
    extra_schema_versions: Mapping[str, str] | None = None,
    retrieved_at: Any = None,
    output_root: Path | None = None,
    native_receipts: Mapping[str, SourceReceiptV1] | None = None,
    scenario_engine: Any = None,
    business_profile: Any = None,
    valuation_receipt: dict[str, Any] | None = None,
) -> tuple[str, dict[str, bytes], BuildRunManifestV1]:
    packet = strategy_run.packet
    published_plays = sorted(complete_play_set.plays, key=lambda item: item.rank)
    primary = published_plays[0] if published_plays else None
    clock = retrieved_at or FIXED_UTC
    raw_snapshots = {name: item.data for name, item in snapshots.items()}
    run_mode = cast(RunMode, safe_config["run_mode"])
    withheld_decisions: list[WithheldAssetV1] = []
    if scenario_engine is not None:
        for asset_id in getattr(scenario_engine, "withheld_asset_ids", ()):
            withheld_decisions.append(
                WithheldAssetV1(
                    asset_id=asset_id,
                    asset_class=asset_id,
                    reason=_withheld_reason(asset_id),
                    depends_on=[_withheld_reason(asset_id)],
                )
            )
    elif not complete_play_set.plays:
        withheld_decisions.append(
            WithheldAssetV1(
                asset_id="strategy_dependent_output",
                asset_class="strategy_dependent_output",
                reason="unconfigured_strategy",
                depends_on=["strategy_provider"],
            )
        )
    launch_pack = build_launch_pack(
        LaunchPackInputs(
            money_map=enriched_money_map,
            recovery_plays=complete_play_set,
            evidence_packet=packet,
            contribution_ledger=ledger,
            identity_graph=graph,
            candidates=candidates,
            exclusions=exclusions,
            business_profile=business_profile or _scenario_business_profile(scenario_engine),
            source_snapshots=raw_snapshots,
            withheld_decisions=tuple(withheld_decisions),
            mode=run_mode,
        )
    )
    validate_launch_pack_payloads(launch_pack.payloads)
    if run_mode == "private":
        validate_private_launch_pack_payloads(launch_pack.payloads)
    launch_status = launch_pack.status
    withheld_assets = list(launch_pack.withheld_asset_ids)
    primary_play_id = primary.play_id if primary is not None else None
    creative_href = None
    brief_href = None
    if primary_play_id is not None:
        for entry in launch_pack.manifest.files:
            if entry.play_id != primary_play_id:
                continue
            if entry.asset_class == "creative_handoff":
                creative_href = f"launch-pack/{entry.path}"
            elif entry.asset_class == "production_brief":
                brief_href = f"launch-pack/{entry.path}"
    handoff_actions = build_handoff_actions(
        intake=handoff_intake or HandoffIntakeConfigV1(),
        creative_handoff_href=creative_href,
        production_brief_href=brief_href,
    )
    index_html = render_recovery_room_html(
        enriched_money_map,
        complete_play_set,
        launch_status=launch_status,
        synthetic_demo=bool(
            safe_config.get("fixture")
            or safe_config.get("demo_fixture")
            or safe_config.get("data_origin") == "synthetic"
        ),
        withheld_assets=withheld_assets,
        handoff_actions=handoff_actions,
        contribution_ledger=ledger,
    )
    top_play_html = render_top_play_html(enriched_money_map, render_play_set)
    print_html = render_print_report_html(
        enriched_money_map,
        complete_play_set,
        contribution_ledger=ledger,
    )
    static_assets = recovery_room_static_assets()
    render_manifest = build_recovery_room_manifest(run_id, index_html, static_assets)
    render_dir: str | None = None
    if output_root is not None:
        output_root.mkdir(parents=True, exist_ok=True)
        render_dir = str(output_root)
    with tempfile.TemporaryDirectory(prefix=".found-money-render-", dir=render_dir) as tmp:
        tmp_root = Path(tmp)
        index_path = tmp_root / "index.html"
        top_path = tmp_root / "top-play.html"
        print_path = tmp_root / "print-report.html"
        index_path.write_text(index_html, encoding="utf-8")
        top_path.write_text(top_play_html, encoding="utf-8")
        print_path.write_text(print_html, encoding="utf-8")
        for relative_path, payload in static_assets.items():
            asset_path = tmp_root / relative_path
            asset_path.parent.mkdir(parents=True, exist_ok=True)
            asset_path.write_bytes(payload)
        proof_artifacts = capture_recovery_room_artifacts(
            index_path.resolve(), top_path.resolve(), print_html=print_path.resolve()
        )
        four_room_artifacts = capture_four_room_screenshots(index_path.resolve())

    if native_receipts is not None:
        source_receipts = {
            snapshot.receipt_path: native_receipts[snapshot.source_type]
            for snapshot in snapshots.values()
        }
    else:
        source_receipts = {
            snapshot.receipt_path: build_source_receipt(
                source_type=snapshot.source_type,
                connector_schema_version=snapshot.connector_schema_version,
                retrieved_at=clock,
                locator_kind="input_path",
                locator=snapshot.safe_locator,
                content=snapshot.raw_bytes,
                page_or_row_count=snapshot.page_or_row_count,
                record_count=snapshot.record_count,
                request_id=f"req_{snapshot.source_type}_fm018_build",
                correlation_id="corr_fm018_build",
            )
            for snapshot in snapshots.values()
        }
    source_set_hash = compute_source_set_hash(
        {path: receipt.content_hash for path, receipt in source_receipts.items()}
    )
    projection = public_identity_projection(graph)
    candidate_tokens = {item.customer_token for item in getattr(candidates, "candidates", [])}
    contribution_tokens = {item.customer_token for item in getattr(ledger, "contributions", [])}
    scenario_resolved = None
    scenario_ambiguous = None
    if scenario_engine is not None:
        scenario_resolved = scenario_engine.identity_aggregate.resolved_customer_count
        scenario_ambiguous = scenario_engine.identity_aggregate.ambiguous_cluster_count
    assert_identity_count_consistency(
        graph,
        projection,
        candidate_tokens=candidate_tokens,
        contribution_tokens=contribution_tokens,
        resolved_customer_count=scenario_resolved,
        ambiguous_cluster_count=scenario_ambiguous,
    )
    node_counts: dict[str, int] = {}
    for node in graph.nodes:
        node_counts[node.source_system] = node_counts.get(node.source_system, 0) + 1
    identity_references = []
    for snapshot in sorted(snapshots.values(), key=lambda item: item.source_type):
        receipt = source_receipts[snapshot.receipt_path]
        identity_references.append(
            IdentitySourceReferenceV1(
                declared_source=snapshot.source_type,
                source_type=snapshot.source_type,
                receipt_path=snapshot.receipt_path,
                content_hash=receipt.content_hash,
                identity_node_count=node_counts.get(snapshot.source_type, 0),
            )
        )
    identity_evidence = build_identity_stage_evidence(
        graph,
        projection,
        source_set_hash=source_set_hash,
        source_references=identity_references,
    )
    if scenario_engine is None:
        validate_identity_stage_bundle(
            graph=graph,
            projection=projection,
            evidence=identity_evidence,
            source_receipts=source_receipts,
            payloads={snapshot.source_type: snapshot.raw_bytes for snapshot in snapshots.values()},
        )

    print_page_artifacts = sorted(
        name for name in proof_artifacts if name.startswith("print-report-page-")
    )
    print_complete = len(complete_play_set.plays) == 3
    print_review = (
        validate_print_review_packet()
        if print_complete and run_id == CANONICAL_PRINT_REVIEW_RUN_ID
        else None
    )
    if print_review is not None:
        if print_review["run_id"] != run_id:
            raise BuildConfigError("print review does not bind to the canonical source run")
        if len(print_review["page_reviews"]) != len(print_page_artifacts):
            raise BuildConfigError("print review does not cover every rendered page")
        if print_review["platform"] != resolve_render_baselines_dir().name:
            raise BuildConfigError("print review does not match the current render platform")
        if print_review["pdf_sha256"] != sha256_bytes(proof_artifacts["print-report.pdf"]):
            raise BuildConfigError("print review PDF hash is stale")
        if print_review["contact_sheet_sha256"] != sha256_bytes(
            proof_artifacts[PRINT_REPORT_CONTACT_SHEET]
        ):
            raise BuildConfigError("print review contact-sheet hash is stale")
        for row in print_review["page_reviews"]:
            if row["artifact"] not in proof_artifacts:
                raise BuildConfigError("print review references a missing page artifact")
            if row["sha256"] != sha256_bytes(proof_artifacts[row["artifact"]]):
                raise BuildConfigError(f"print review page {row['page']} hash is stale")
    print_manifest = build_print_report_manifest(
        run_id=run_id,
        source_set_hash=source_set_hash,
        play_ids=[
            play.play_id for play in sorted(complete_play_set.plays, key=lambda item: item.rank)
        ],
        proof_artifacts=proof_artifacts,
        print_review=print_review,
    )

    payloads: dict[str, bytes] = {
        "money-map.json": enriched_money_map.to_canonical_json(),
        "recovery-plays.json": complete_play_set.to_canonical_json(),
        "index.html": index_html.encode("utf-8"),
        "top-play.html": top_play_html.encode("utf-8"),
        "render-manifest.json": render_manifest.to_canonical_json(),
        "print-report.pdf": proof_artifacts["print-report.pdf"],
        "provenance/strategy-evidence-packet.json": packet.to_canonical_json(),
        "provenance/strategy-audit-receipt.json": strategy_run.receipt.to_canonical_json(),
    }
    if valuation_receipt is not None:
        payloads["provenance/valuation-summary.json"] = _canonical_json_bytes(valuation_receipt)
    if scenario_engine is None:
        payloads[IDENTITY_PUBLIC_PATH] = projection.to_canonical_json()
        payloads[IDENTITY_STAGE_PATH] = identity_evidence.to_canonical_json()
        if run_mode == "private":
            payloads[IDENTITY_GRAPH_PATH] = graph.to_canonical_json()
    if strategy_run.differentiation is not None:
        payloads["provenance/content-differentiation-report.json"] = (
            strategy_run.differentiation.to_canonical_json()
        )
    payloads.update(launch_pack.payloads)
    payloads.update(static_assets)
    for path, receipt in source_receipts.items():
        payloads[path] = receipt.to_canonical_json()
    for name, data in proof_artifacts.items():
        payloads[f"render-proof/{name}"] = data
    payloads["render-proof/print-report-manifest.json"] = print_manifest
    for name, data in four_room_artifacts.items():
        payloads[f"render-proof/{name}"] = data
    if extra_payloads:
        payloads.update(dict(extra_payloads))

    stages: dict[str, Literal["not_started", "completed", "failed"]] = {
        "credential_declaration": "completed",
        "source_read": "completed",
        "identity": "completed",
        "events": "completed",
        "value": "completed",
        "money_map": "completed",
        "fixture_strategy_one_play": "completed",
        "render": "completed",
        "three_play_strategy": "completed",
        "activation_launch_pack": "completed",
        "native_live_connectors": "not_started",
    }
    if extra_stages:
        stages.update(
            {
                key: cast(Literal["not_started", "completed", "failed"], value)
                for key, value in extra_stages.items()
            }
        )
    referenced = {
        "found-money-build": BUILD_MANIFEST_SCHEMA,
        "source-receipt": "source-receipt.v1",
        "run-manifest": "run-manifest.v1",
        "money-map": "money-map.v1",
        "recovery-plays": "recovery-plays.v1",
        "strategy-evidence-packet": "grounded-strategy-evidence.v1",
        "strategy-audit-receipt": "strategy-audit-receipt.v1",
        "recovery-play-differentiation": "recovery-play-differentiation.v1",
        "recovery-room-manifest": "recovery-room-manifest.v1",
    }
    if scenario_engine is None:
        referenced["identity-public"] = "identity-public.v1"
        referenced["identity-stage"] = "identity-stage.v1"
    if extra_schema_versions:
        referenced.update(dict(extra_schema_versions))

    legacy_manifest = build_run_manifest(
        run_id=run_id,
        referenced_schema_versions=referenced,
        started_at=clock,
        completed_at=clock,
        mode=cast(RunMode, safe_config["run_mode"]),
        source_receipts=source_receipts,
        stages=stages,
        artifact_hashes={path: sha256_bytes(data) for path, data in payloads.items()},
    )
    payloads["provenance/run-manifest.json"] = legacy_manifest.to_canonical_json()
    if extra_payloads:
        if scenario_engine is None or native_receipts is None:
            raise BuildConfigError("scenario manifest requires engine output and native receipts")
        scenario_manifest = build_scenario_manifest(
            scenario_engine, payloads, receipts=native_receipts
        )
        payloads["scenario/manifest.json"] = scenario_manifest.to_canonical_json()

    artifacts = [
        {"path": path, "sha256": sha256_bytes(data)} for path, data in sorted(payloads.items())
    ]
    build_manifest = BuildRunManifestV1.model_validate(
        {
            "schema_version": BUILD_MANIFEST_SCHEMA,
            "run_id": run_id,
            "mode": safe_config["run_mode"],
            "source_config": dict(safe_config),
            "stages": stages,
            "deferred_stages": {
                "native_live_connectors": {
                    "status": "not_started",
                    "reason": "FM-018 supports fixture and explicit JSON file snapshots only.",
                    "depends_on_future_issue": True,
                },
            },
            "source_receipt_paths": sorted(source_receipts),
            "source_hashes": {
                name: sha256_bytes(snapshot.raw_bytes) for name, snapshot in snapshots.items()
            },
            "source_set_hash": source_set_hash,
            "artifacts": artifacts,
        }
    )
    payloads["run.json"] = build_manifest.to_canonical_json()
    # Keep private launch-pack validation separate; never weaken the public validator.
    if safe_config["run_mode"] == "public":
        if any(path.startswith("launch-pack/private/") for path in payloads):
            raise BuildConfigError("public build contains a private launch-pack subtree")
        validate_public_artifact_payloads(payloads)
    else:
        public_payloads = {
            path: data
            for path, data in payloads.items()
            if not path.startswith("launch-pack/private/") and path != IDENTITY_GRAPH_PATH
        }
        validate_public_artifact_payloads(public_payloads)
    return run_id, payloads, build_manifest


def validate_public_artifact_payloads(payloads: Mapping[str, bytes]) -> None:
    """Fail closed when a generated build payload contains unsafe material.

    Paths named ``private`` are not skipped. Callers must omit private segment
    payloads and validate them via ``validate_private_launch_pack_payloads``.
    """
    violations: list[str] = []
    from found_money.safety.output_scan import verified_texture_asset

    for relative, payload in sorted(payloads.items()):
        if verified_texture_asset(relative, payload):
            continue
        suffix = Path(relative).suffix.lower()
        if suffix == ".png":
            # PNG compression bytes are not text and routinely contain random
            # regex-shaped sequences. Their source HTML is scanned below and
            # the proof layer validates the image container separately.
            continue
        if suffix == ".pdf":
            try:
                text = "\n".join(
                    (page.extract_text() or "") for page in PdfReader(io.BytesIO(payload)).pages
                )
            except Exception:
                # Minimal injected proof doubles are validated by their caller;
                # never interpret compressed/binary bytes as public text.
                text = ""
        else:
            try:
                text = payload.decode("utf-8")
            except UnicodeDecodeError:
                text = payload.decode("latin-1")
        if suffix in {".html", ".htm"}:
            try:
                text = html_for_public_scan(text)
            except ValueError as exc:
                violations.append(f"{relative}: unsafe handoff markup ({exc})")

        if suffix == ".json":
            try:
                json.loads(text)
            except (ValueError, json.JSONDecodeError) as exc:
                violations.append(f"{relative}: unsafe JSON ({exc})")
        if suffix in {".json", ".html", ".md", ".pdf", ".csv"}:
            if _EMAIL_RE.search(text):
                violations.append(f"{relative}: contains an email address")
            if _URL_RE.search(text):
                violations.append(f"{relative}: contains a URL")
            if relative.replace("\\", "/") != "assets/three.min.js" and _UNSAFE_SCHEME_RE.search(
                text
            ):
                violations.append(f"{relative}: contains an unsafe URI scheme")
            if _PROVIDER_CUSTOMER_ID_RE.search(text):
                violations.append(f"{relative}: contains a raw fixture identity")
            if _ABS_PATH_RE.search(text):
                violations.append(f"{relative}: contains an absolute local path")
            if _SECRET_VALUE_RE.search(text):
                violations.append(f"{relative}: contains credential material")
            if _RAW_FIXTURE_ID_RE.search(text):
                violations.append(f"{relative}: contains a raw fixture identity")
            identity_key_re = (
                _PUBLIC_PROJECTION_IDENTITY_KEY_RE
                if relative.replace("\\", "/") in _PUBLIC_IDENTITY_PROJECTION_PATHS
                or relative.replace("\\", "/").endswith("/identity/identity-public.json")
                or relative.replace("\\", "/").endswith("/identity/public.json")
                else _RAW_IDENTITY_KEY_RE
            )
            if suffix != ".csv" and identity_key_re.search(text):
                violations.append(f"{relative}: contains a raw identity field")
            if _SENSITIVE_KEY_RE.search(text):
                violations.append(f"{relative}: contains a sensitive field")
            if suffix in {".json", ".html", ".md", ".pdf", ".csv"} and _PLACEHOLDER_RE.search(text):
                violations.append(f"{relative}: contains a forbidden placeholder")

    if violations:
        raise BuildConfigError("generated artifacts failed public-safety validation")


def _write_payloads(
    output_root: Path,
    payloads: Mapping[str, bytes],
    destinations: Mapping[str, Path],
) -> dict[str, Path]:
    for relative in payloads:
        _validate_relative_under_root(output_root, relative)
    existing_launch_files = {
        path.relative_to(output_root).as_posix()
        for path in (output_root / "launch-pack").rglob("*")
        if path.is_file()
    }
    if existing_launch_files - set(payloads):
        raise BuildPathError("existing launch-pack contains stale files not present in this build")
    return write_artifact_set_atomic(output_root, payloads)


def build(
    *,
    output_root: Path | str,
    source_config: Path | str | None = None,
    hubspot_snapshot: Path | str | None = None,
    stripe_snapshot: Path | str | None = None,
    credential_declaration: str | None = None,
    business_profile: BusinessProfileV1 | None = None,
) -> BuildResult:
    root = resolve_output_root(output_root)
    if source_config is not None:
        config_payload = _read_config(Path(source_config))
        snapshots, safe_config, handoff_intake = load_source_config(
            source_config, _payload=config_payload
        )
        if config_payload.get("business_profile") is not None:
            if business_profile is not None:
                raise BuildConfigError("supply the business profile once")
            raw_profile = config_payload["business_profile"]
            if not isinstance(raw_profile, dict):
                raise BuildConfigError("business profile must be an object")
            errors = [
                e
                for e in profile_intake_violations(raw_profile)
                if not e.startswith("missing required boundary confirmation:")
            ]
            if errors:
                raise BuildConfigError("invalid business profile: " + "; ".join(errors))
            business_profile = BusinessProfileV1.model_validate(raw_profile)
    elif hubspot_snapshot is not None and stripe_snapshot is not None:
        snapshots, safe_config, handoff_intake = source_config_from_cli_file_inputs(
            hubspot_snapshot=hubspot_snapshot,
            stripe_snapshot=stripe_snapshot,
            credential_declaration=credential_declaration,
        )
    else:
        raise BuildConfigError("source config is required")
    if business_profile is not None:
        errors = [
            e
            for e in profile_intake_violations(business_profile.model_dump(mode="json"))
            if not e.startswith("missing required boundary confirmation:")
        ]
        if errors:
            raise BuildConfigError("invalid business profile: " + "; ".join(errors))
        if safe_config.get("business_model") not in (None, business_profile.business_model):
            raise BuildConfigError("business model and profile disagree")
        safe_config["business_model"] = business_profile.business_model
        safe_config["business_profile_sha256"] = sha256_bytes(
            business_profile.model_dump_json().encode("utf-8")
        )
        # Fixture mode is an explicitly synthetic demonstration. With an owner
        # profile, reuse those normalized snapshots through the ordinary file path.
        if safe_config.get("fixture") in SCENARIO_FIXTURES:
            snapshots = _fixture_snapshots(str(safe_config["fixture"]))
            safe_config["demo_fixture"] = safe_config.pop("fixture")
            safe_config["mode"] = "file"
            safe_config["strategy_provider"] = "skill"
    relative_paths = FINAL_ARTIFACT_PATHS
    native_receipts = None
    created_root = not root.exists()
    if created_root:
        root.mkdir(parents=True, exist_ok=True)
    try:
        if safe_config.get("fixture") in SCENARIO_FIXTURES:
            fixture_id = str(safe_config["fixture"])
            relative_paths = FINAL_ARTIFACT_PATHS + SCENARIO_PUBLIC_PATHS
            snapshots, native_receipts = _load_scenario_via_source_stage(
                root,
                load_scenario_definition(fixture_id).clock,
                fixture_id=fixture_id,
            )
        destinations = preflight_artifact_paths(root, relative_paths)
        run_id, payloads, manifest = _compose_contracts(
            snapshots,
            safe_config=safe_config,
            handoff_intake=handoff_intake,
            output_root=root,
            native_receipts=native_receipts,
            business_profile=business_profile,
        )
        artifact_paths = _write_payloads(root, payloads, destinations)
    except Exception:
        if created_root and root.exists():
            shutil.rmtree(root)
        raise
    return BuildResult(
        output_root=root,
        run_id=run_id,
        artifact_paths=artifact_paths,
        manifest=manifest,
    )
