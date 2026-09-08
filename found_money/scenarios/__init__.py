"""Reusable Found Money scenario harness for synthetic business-model proofs."""

from found_money.scenarios.assets import (
    SYNTHETIC_ECOMMERCE_V1,
    SYNTHETIC_ECOMMERCE_V1_CART_OMITTED,
    SYNTHETIC_SAAS_V1,
    load_scenario_definition,
    load_scenario_snapshot_bytes,
    load_scenario_snapshots,
    load_scenario_source_config_bytes,
    scenario_fixture_root,
)
from found_money.scenarios.registry import (
    SYNTHETIC_SERVICE_V1,
    SYNTHETIC_SERVICE_V1_APPOINTMENTS_OMITTED,
    SYNTHETIC_SERVICE_V1_CRM_OMITTED,
    SYNTHETIC_SERVICE_V1_PAYMENT_OMITTED,
    SYNTHETIC_SERVICE_V1_PROPOSALS_OMITTED,
)
from found_money.scenarios.harness import (
    RAW_ECOMMERCE_SOURCE_IDS,
    RAW_SCENARIO_SOURCE_IDS,
    RAW_SERVICE_SOURCE_IDS,
    SCENARIO_AGGREGATE_PATH,
    SCENARIO_DEFINITION_PATH,
    SCENARIO_EVENTS_PUBLIC_PATH,
    SCENARIO_IDENTITY_PUBLIC_PATH,
    SCENARIO_MANIFEST_PATH,
    SCENARIO_PUBLIC_PATHS,
    SCENARIO_SITUATIONS_PATH,
    SCENARIO_VALUE_PUBLIC_PATH,
    ScenarioEngineResult,
    build_scenario_manifest,
    payment_rescue_ledger,
    run_scenario_engine,
    scenario_public_payloads,
)
from found_money.scenarios.review import validate_scenario_reviewer_packet
from found_money.scenarios.source_audit import (
    build_canonical_service_public_output,
    current_service_run_binding,
    validate_service_source_proper_noun_audit,
)
from found_money.scenarios.validate import validate_scenario_output_tree
from found_money.strategy import apply_complete_plays_to_money_map

__all__ = [
    "RAW_ECOMMERCE_SOURCE_IDS",
    "RAW_SCENARIO_SOURCE_IDS",
    "RAW_SERVICE_SOURCE_IDS",
    "SYNTHETIC_ECOMMERCE_V1",
    "SYNTHETIC_ECOMMERCE_V1_CART_OMITTED",
    "SYNTHETIC_SERVICE_V1",
    "SYNTHETIC_SERVICE_V1_APPOINTMENTS_OMITTED",
    "SYNTHETIC_SERVICE_V1_CRM_OMITTED",
    "SYNTHETIC_SERVICE_V1_PAYMENT_OMITTED",
    "SYNTHETIC_SERVICE_V1_PROPOSALS_OMITTED",
    "SCENARIO_AGGREGATE_PATH",
    "SCENARIO_DEFINITION_PATH",
    "SCENARIO_EVENTS_PUBLIC_PATH",
    "SCENARIO_IDENTITY_PUBLIC_PATH",
    "SCENARIO_MANIFEST_PATH",
    "SCENARIO_PUBLIC_PATHS",
    "SCENARIO_SITUATIONS_PATH",
    "SCENARIO_VALUE_PUBLIC_PATH",
    "SYNTHETIC_SAAS_V1",
    "ScenarioEngineResult",
    "apply_complete_plays_to_money_map",
    "build_scenario_manifest",
    "load_scenario_definition",
    "load_scenario_snapshot_bytes",
    "load_scenario_snapshots",
    "load_scenario_source_config_bytes",
    "payment_rescue_ledger",
    "run_scenario_engine",
    "scenario_fixture_root",
    "scenario_public_payloads",
    "build_canonical_service_public_output",
    "current_service_run_binding",
    "validate_scenario_output_tree",
    "validate_scenario_reviewer_packet",
    "validate_service_source_proper_noun_audit",
]
