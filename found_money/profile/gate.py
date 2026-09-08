"""Build-path guardrails consuming the typed profile (FM-061).

Thin enforcement only: business-model gating and the contribution-margin discount
check. The domain rules live in the skill tables (``offer-direction.json``); this
module loads them and applies them in the build path. It holds no domain knowledge
of its own.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any


class ProfileGateError(ValueError):
    """Raised when a build-path profile gate fails closed on table shape."""


def business_model_mismatch(
    profile_business_model: str, scenario_business_model: str | None
) -> str | None:
    """Named gate: the build refuses to run a profile against a different arm."""

    if scenario_business_model is None:
        return "business-model gate: scenario defines no business_model to check against"
    if profile_business_model != scenario_business_model:
        return (
            "business-model gate: profile business_model "
            f"{profile_business_model!r} does not match scenario arm "
            f"{scenario_business_model!r}; refusing to serve one strategy to "
            "every business"
        )
    return None


def load_table_guardrails() -> dict[str, Any]:
    from found_money.strategy.intelligence import load_table

    guardrails = load_table("offer-direction").get("guardrails")
    if not isinstance(guardrails, dict):
        raise ProfileGateError("offer-direction guardrails missing")
    return guardrails


def _discount_commitment_rule_text() -> str:
    guardrails = load_table_guardrails()
    rule_key = "discount_requires_commitment_term"
    if rule_key not in guardrails:
        raise ProfileGateError(f"offer-direction guardrails missing {rule_key!r}")
    rule_text = guardrails[rule_key]
    if not isinstance(rule_text, str) or not rule_text.strip():
        raise ProfileGateError(f"offer-direction guardrails[{rule_key!r}] must be non-empty text")
    return rule_text


def _discount_margin_ceiling_percent() -> Decimal:
    from found_money.strategy.intelligence import load_table

    table = load_table("offer-direction")
    ceiling_key = "discount_margin_ceiling_percent"
    if ceiling_key not in table:
        raise ProfileGateError(f"offer-direction missing {ceiling_key!r}")
    return Decimal(str(table[ceiling_key]))


def discount_violations(rung: dict[str, Any], margin_percent: Decimal | None) -> list[str]:
    """Enforce the contribution-margin discount guardrail on one offer rung.

    A discount is legitimate only when it is bound to a commitment term, per
    ``offer-direction.json`` ``discount_requires_commitment_term``. A discount
    against a margin the packet cannot support, or an unresolved margin, fails
    closed. This is the build-path consumer; the rule text is never duplicated.
    """

    rule_text = _discount_commitment_rule_text()
    ceiling = _discount_margin_ceiling_percent()
    errors: list[str] = []
    discount = rung.get("discount_percent")
    if discount is None:
        return errors
    discount_dec = Decimal(str(discount))
    commitment = rung.get("commitment_term")
    if not commitment:
        errors.append(f"discount_requires_commitment_term: {rule_text}")
    if discount_dec > ceiling:
        errors.append(
            f"discount_margin_ceiling: discount {discount}% exceeds table ceiling {ceiling}%"
        )
    if margin_percent is None:
        errors.append(
            "discount_requires_supported_margin: margin is unresolved and a "
            "discount cannot be validated against it; refusing to concede"
        )
    elif discount_dec > margin_percent:
        errors.append(
            f"discount_margin_floor: discount {discount}% exceeds the "
            f"contribution margin {margin_percent}% and cannot be funded"
        )
    return errors


def scenario_business_model_from_config(config_path: str | Path) -> str:
    """Resolve the scenario arm business model named by a build source config."""

    payload = json.loads(Path(config_path).read_text(encoding="utf-8"))
    fixture_id = payload.get("fixture")
    if not isinstance(fixture_id, str) or not fixture_id:
        raise ProfileGateError("source config missing fixture id")
    from found_money.scenarios.registry import scenario_arm

    return scenario_arm(fixture_id).business_model


def prebuild_profile_gate_violations(
    profile_business_model: str,
    margin_percent: Decimal,
    config_path: str | Path,
) -> list[str]:
    """Named errors that must fail closed before a guided build subprocess runs."""

    errors: list[str] = []
    scenario_model = scenario_business_model_from_config(config_path)
    mismatch = business_model_mismatch(profile_business_model, scenario_model)
    if mismatch is not None:
        errors.append(mismatch)
    load_table_guardrails()
    errors.extend(
        discount_violations(
            {"discount_percent": Decimal("1"), "commitment_term": "annual"},
            margin_percent,
        )
    )
    return errors
