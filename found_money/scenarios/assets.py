"""Packaged scenario fixture loading that does not depend on a source checkout."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from found_money.contracts.cart import omitted_cart_binding, parse_optional_cart
from found_money.contracts.scenarios import ScenarioDefinitionV1, parse_scenario_definition
from found_money.receipts import sha256_bytes
from found_money.scenarios.registry import (
    ECOMMERCE_V1_ROOT,
    SCENARIO_ARMS,
    SYNTHETIC_ECOMMERCE_V1,
    SYNTHETIC_ECOMMERCE_V1_CART_OMITTED,
    SYNTHETIC_SAAS_V1,
    is_ecommerce_fixture,
    is_service_fixture,
    scenario_arm,
)

SCENARIO_FIXTURE_ROOTS = {fixture_id: arm.root for fixture_id, arm in SCENARIO_ARMS.items()}


def scenario_fixture_root(fixture_id: str = SYNTHETIC_SAAS_V1) -> Path:
    arm = scenario_arm(fixture_id)
    if not arm.root.is_dir():
        raise ValueError(f"packaged {fixture_id} scenario fixtures are unavailable")
    return arm.root


def load_scenario_source_config_bytes(fixture_id: str = SYNTHETIC_SAAS_V1) -> bytes:
    arm = scenario_arm(fixture_id)
    return (arm.root / arm.source_config_file).read_bytes()


def load_scenario_definition_bytes(fixture_id: str = SYNTHETIC_SAAS_V1) -> bytes:
    arm = scenario_arm(fixture_id)
    return (arm.root / arm.definition_file).read_bytes()


def load_optional_cart_binding(fixture_id: str):
    if fixture_id == SYNTHETIC_ECOMMERCE_V1_CART_OMITTED:
        return omitted_cart_binding()
    if fixture_id != SYNTHETIC_ECOMMERCE_V1:
        raise ValueError("optional cart binding is only defined for ecommerce fixtures")
    path = ECOMMERCE_V1_ROOT / "optional" / "high-value-cart.json"
    return parse_optional_cart(path.read_bytes())


def optional_cart_hash_for_fixture(fixture_id: str) -> str:
    return load_optional_cart_binding(fixture_id).sha256()


def load_scenario_definition(fixture_id: str = SYNTHETIC_SAAS_V1) -> ScenarioDefinitionV1:
    from found_money.scenarios.transports import connector_schema_hashes, packaged_fixture_hashes

    raw = load_scenario_definition_bytes(fixture_id)
    definition = parse_scenario_definition(raw)
    if definition.fixture_id != fixture_id:
        raise ValueError("scenario definition fixture_id does not match the requested fixture")
    if packaged_fixture_hashes(fixture_id) != definition.fixture_hashes:
        raise ValueError("scenario fixture hashes do not match the locked definition")
    config_hash = sha256_bytes(load_scenario_source_config_bytes(fixture_id))
    if config_hash != definition.source_config_hash:
        raise ValueError("scenario source-config hash does not match the locked definition")
    if connector_schema_hashes(fixture_id) != definition.schema_hashes:
        raise ValueError("scenario schema hashes do not match the locked definition")
    arm = scenario_arm(fixture_id)
    if is_ecommerce_fixture(fixture_id):
        cart_hash = optional_cart_hash_for_fixture(fixture_id)
        if definition.high_value_cart_hash != cart_hash:
            raise ValueError("scenario cart hash does not match the locked optional cart binding")
        expected_state = (
            "omitted" if fixture_id == SYNTHETIC_ECOMMERCE_V1_CART_OMITTED else "supplied"
        )
        if definition.high_value_cart_state != expected_state:
            raise ValueError("scenario cart state does not match the locked variant")
    if is_service_fixture(fixture_id):
        omitted = tuple(definition.omitted_service_sources or ())
        if omitted != arm.omitted_service_sources:
            raise ValueError("service omitted sources do not match the locked registry arm")
    return definition


def load_scenario_snapshot_bytes(fixture_id: str = SYNTHETIC_SAAS_V1) -> dict[str, bytes]:
    definition = load_scenario_definition(fixture_id)
    from found_money.scenarios.transports import load_normalized_scenario_snapshots

    _snapshots, raw, _receipts = load_normalized_scenario_snapshots(
        fixture_id, retrieved_at=definition.clock
    )
    return raw


def load_scenario_snapshots(fixture_id: str = SYNTHETIC_SAAS_V1) -> dict[str, dict[str, Any]]:
    definition = load_scenario_definition(fixture_id)
    from found_money.scenarios.transports import load_normalized_scenario_snapshots

    snapshots, _raw, _receipts = load_normalized_scenario_snapshots(
        fixture_id, retrieved_at=definition.clock
    )
    return snapshots
