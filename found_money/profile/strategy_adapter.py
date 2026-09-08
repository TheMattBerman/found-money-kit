"""Adapter mapping FM-061 BusinessProfileV1 to StrategyBusinessProfileV1."""

from __future__ import annotations

from found_money.contracts.strategy import StrategyBusinessProfileV1
from found_money.profile.intake import BusinessProfileV1


def adapt_business_profile(profile: BusinessProfileV1) -> StrategyBusinessProfileV1:
    """Map validated owner intake answers to strategy generation business profile.

    Money fields stay unquantified except an explicit LTV override, per the intake contract.
    """
    return StrategyBusinessProfileV1(
        product=profile.product,
        proof=profile.proof,
        margin=f"{profile.margin_percent} percent contribution margin",
        channel="email and sms with a human review task",
        capacity=f"{profile.capacity} recovery reviews per week",
        destination=profile.destination,
        business_model=profile.business_model,
        tenure_multiple_months=profile.tenure_multiple_months,
        ltv_override_minor=profile.ltv_override_minor,
    )
