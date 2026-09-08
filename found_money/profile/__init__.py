"""Owner-supplied business profile intake (FM-061)."""

from found_money.profile.gate import (
    ProfileGateError,
    business_model_mismatch,
    discount_violations,
    load_table_guardrails,
    prebuild_profile_gate_violations,
    scenario_business_model_from_config,
)
from found_money.profile.intake import (
    BusinessProfileV1,
    ProfileIntakeError,
    load_intake_table,
    profile_intake_violations,
    validate_or_raise,
)
from found_money.profile.strategy_adapter import adapt_business_profile

__all__ = [
    "BusinessProfileV1",
    "ProfileGateError",
    "ProfileIntakeError",
    "adapt_business_profile",
    "business_model_mismatch",
    "discount_violations",
    "load_intake_table",
    "load_table_guardrails",
    "prebuild_profile_gate_violations",
    "profile_intake_violations",
    "scenario_business_model_from_config",
    "validate_or_raise",
]
