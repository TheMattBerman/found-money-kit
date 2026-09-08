"""Explicit scenario registry. FM-035 adds a service/membership arm without a second harness."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Literal

PACKAGE_ROOT = Path(__file__).resolve().parent
FIXTURES_ROOT = PACKAGE_ROOT / "fixtures"

SYNTHETIC_SAAS_V1 = "synthetic-saas-v1"
SYNTHETIC_ECOMMERCE_V1 = "synthetic-ecommerce-v1"
SYNTHETIC_ECOMMERCE_V1_CART_OMITTED = "synthetic-ecommerce-v1-cart-omitted"
SYNTHETIC_SERVICE_V1 = "synthetic-service-v1"
SYNTHETIC_SERVICE_V1_APPOINTMENTS_OMITTED = "synthetic-service-v1-appointments-omitted"
SYNTHETIC_SERVICE_V1_PROPOSALS_OMITTED = "synthetic-service-v1-proposals-omitted"
SYNTHETIC_SERVICE_V1_CRM_OMITTED = "synthetic-service-v1-crm-omitted"
SYNTHETIC_SERVICE_V1_PAYMENT_OMITTED = "synthetic-service-v1-payment-omitted"

SAAS_V1_ROOT = FIXTURES_ROOT / "saas-v1"
ECOMMERCE_V1_ROOT = FIXTURES_ROOT / "ecommerce-v1"
SERVICE_V1_ROOT = FIXTURES_ROOT / "service-v1"

StrategyKind = Literal["saas", "ecommerce", "service"]
BusinessModel = Literal["saas", "ecommerce", "service"]


@dataclass(frozen=True)
class ScenarioArm:
    """One locked business-model arm of the shared scenario pipeline."""

    fixture_id: str
    business_model: BusinessModel
    root: Path
    definition_file: str
    source_config_file: str
    source_manifest_file: str
    strategy_kind: StrategyKind
    primary_pile_id: str
    primary_amount_minor: Decimal
    one_family_per_account: bool
    stripe_request_id: str
    stripe_correlation_id: str
    hubspot_transport_rel: str | None = None
    stripe_pages_rel: str | None = None
    orders_input_rel: str | None = None
    appointments_input_rel: str | None = None
    proposals_input_rel: str | None = None
    omitted_service_sources: tuple[str, ...] = ()
    forbidden_copy: tuple[str, ...] = ()

    def hubspot_transport_root(self) -> Path | None:
        if self.hubspot_transport_rel is None:
            return None
        return self.root / self.hubspot_transport_rel

    def stripe_pages_path(self) -> Path | None:
        if self.stripe_pages_rel is None:
            return None
        return self.root / self.stripe_pages_rel

    def orders_input_path(self) -> Path | None:
        if self.orders_input_rel is None:
            return None
        return self.root / self.orders_input_rel

    def appointments_input_path(self) -> Path | None:
        if self.appointments_input_rel is None:
            return None
        return self.root / self.appointments_input_rel

    def proposals_input_path(self) -> Path | None:
        if self.proposals_input_rel is None:
            return None
        return self.root / self.proposals_input_rel


_SAAS_FORBIDDEN: tuple[str, ...] = ()
_ECOMMERCE_FORBIDDEN = (
    "payment_rescue",
    "payment rescue",
    "4900",
    "card-retry",
    "card retry",
    "billing portal",
    "billing-portal",
    "annual subscription",
)
_SERVICE_FORBIDDEN = _ECOMMERCE_FORBIDDEN + (
    "catalog",
    "reorder catalog",
    "vip-silence-reopen",
    "vip-catalog-proof",
    "vip-capacity-lane",
    "payment-rescue-friction-fix",
    "payment-rescue-proof-reset",
    "payment-rescue-capacity-window",
    "18500",
    "ecommerce",
)


def _saas_arm(fixture_id: str = SYNTHETIC_SAAS_V1) -> ScenarioArm:
    return ScenarioArm(
        fixture_id=fixture_id,
        business_model="saas",
        root=SAAS_V1_ROOT,
        definition_file="definition.json",
        source_config_file="source-config.json",
        source_manifest_file="source-manifest.json",
        strategy_kind="saas",
        primary_pile_id="payment_rescue",
        primary_amount_minor=Decimal("4900"),
        one_family_per_account=True,
        stripe_request_id="req_saas_v1",
        stripe_correlation_id="corr_saas_v1",
        hubspot_transport_rel="transports/hubspot",
        stripe_pages_rel="transports/stripe/pages.json",
        forbidden_copy=_SAAS_FORBIDDEN,
    )


def _ecommerce_arm(
    fixture_id: str,
    *,
    definition_file: str,
    source_config_file: str,
) -> ScenarioArm:
    return ScenarioArm(
        fixture_id=fixture_id,
        business_model="ecommerce",
        root=ECOMMERCE_V1_ROOT,
        definition_file=definition_file,
        source_config_file=source_config_file,
        source_manifest_file="source-manifest.json",
        strategy_kind="ecommerce",
        primary_pile_id="disappeared_high_value_customer",
        primary_amount_minor=Decimal("18500"),
        one_family_per_account=False,
        stripe_request_id="req_ecom_v1",
        stripe_correlation_id="corr_ecom_v1",
        stripe_pages_rel="transports/stripe/pages.json",
        orders_input_rel="inputs/orders.csv",
        forbidden_copy=_ECOMMERCE_FORBIDDEN,
    )


def _service_arm(
    fixture_id: str,
    *,
    definition_file: str,
    source_config_file: str,
    source_manifest_file: str,
    hubspot: bool = True,
    stripe: bool = True,
    appointments: bool = True,
    proposals: bool = True,
    omitted: tuple[str, ...] = (),
    primary_pile_id: str = "disappeared_high_value_customer",
    primary_amount_minor: Decimal = Decimal("22000"),
) -> ScenarioArm:
    family_omitted = set(omitted).intersection({"appointments", "proposals", "stripe"})
    return ScenarioArm(
        fixture_id=fixture_id,
        business_model="service",
        root=SERVICE_V1_ROOT,
        definition_file=definition_file,
        source_config_file=source_config_file,
        source_manifest_file=source_manifest_file,
        strategy_kind="service",
        primary_pile_id=primary_pile_id,
        primary_amount_minor=primary_amount_minor,
        one_family_per_account=not family_omitted,
        stripe_request_id="req_svc_v1",
        stripe_correlation_id="corr_svc_v1",
        hubspot_transport_rel="transports/hubspot" if hubspot else None,
        stripe_pages_rel="transports/stripe/pages.json" if stripe else None,
        appointments_input_rel="inputs/appointments.csv" if appointments else None,
        proposals_input_rel="inputs/proposals.csv" if proposals else None,
        omitted_service_sources=omitted,
        forbidden_copy=_SERVICE_FORBIDDEN,
    )


SCENARIO_ARMS: dict[str, ScenarioArm] = {
    SYNTHETIC_SAAS_V1: _saas_arm(),
    SYNTHETIC_ECOMMERCE_V1: _ecommerce_arm(
        SYNTHETIC_ECOMMERCE_V1,
        definition_file="definition.json",
        source_config_file="source-config.json",
    ),
    SYNTHETIC_ECOMMERCE_V1_CART_OMITTED: _ecommerce_arm(
        SYNTHETIC_ECOMMERCE_V1_CART_OMITTED,
        definition_file="definition-cart-omitted.json",
        source_config_file="source-config-cart-omitted.json",
    ),
    SYNTHETIC_SERVICE_V1: _service_arm(
        SYNTHETIC_SERVICE_V1,
        definition_file="definition.json",
        source_config_file="source-config.json",
        source_manifest_file="source-manifest.json",
        appointments=True,
        proposals=True,
    ),
    SYNTHETIC_SERVICE_V1_APPOINTMENTS_OMITTED: _service_arm(
        SYNTHETIC_SERVICE_V1_APPOINTMENTS_OMITTED,
        definition_file="definition-appointments-omitted.json",
        source_config_file="source-config-appointments-omitted.json",
        source_manifest_file="source-manifest-appointments-omitted.json",
        appointments=False,
        proposals=True,
        omitted=("appointments",),
    ),
    SYNTHETIC_SERVICE_V1_PROPOSALS_OMITTED: _service_arm(
        SYNTHETIC_SERVICE_V1_PROPOSALS_OMITTED,
        definition_file="definition-proposals-omitted.json",
        source_config_file="source-config-proposals-omitted.json",
        source_manifest_file="source-manifest-proposals-omitted.json",
        appointments=True,
        proposals=False,
        omitted=("proposals",),
    ),
    SYNTHETIC_SERVICE_V1_CRM_OMITTED: _service_arm(
        SYNTHETIC_SERVICE_V1_CRM_OMITTED,
        definition_file="definition-crm-omitted.json",
        source_config_file="source-config-crm-omitted.json",
        source_manifest_file="source-manifest-crm-omitted.json",
        hubspot=False,
        omitted=("hubspot",),
    ),
    SYNTHETIC_SERVICE_V1_PAYMENT_OMITTED: _service_arm(
        SYNTHETIC_SERVICE_V1_PAYMENT_OMITTED,
        definition_file="definition-payment-omitted.json",
        source_config_file="source-config-payment-omitted.json",
        source_manifest_file="source-manifest-payment-omitted.json",
        stripe=False,
        omitted=("stripe",),
        primary_pile_id="silent_proposal",
        primary_amount_minor=Decimal("12500"),
    ),
}

ECOMMERCE_FIXTURES = frozenset({SYNTHETIC_ECOMMERCE_V1, SYNTHETIC_ECOMMERCE_V1_CART_OMITTED})
SERVICE_FIXTURES = frozenset(
    {
        SYNTHETIC_SERVICE_V1,
        SYNTHETIC_SERVICE_V1_APPOINTMENTS_OMITTED,
        SYNTHETIC_SERVICE_V1_PROPOSALS_OMITTED,
        SYNTHETIC_SERVICE_V1_CRM_OMITTED,
        SYNTHETIC_SERVICE_V1_PAYMENT_OMITTED,
    }
)


def scenario_arm(fixture_id: str) -> ScenarioArm:
    arm = SCENARIO_ARMS.get(fixture_id)
    if arm is None:
        raise ValueError("unknown scenario fixture")
    return arm


def is_ecommerce_fixture(fixture_id: str) -> bool:
    return fixture_id in ECOMMERCE_FIXTURES


def is_service_fixture(fixture_id: str) -> bool:
    return fixture_id in SERVICE_FIXTURES
