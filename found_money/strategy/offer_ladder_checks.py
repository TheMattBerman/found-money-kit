"""Deterministic offer-ladder checks (FM-059).

Guardrails live in ``skills/recovery-intelligence/tables/offer-direction.json``.
This module loads them and enforces ladder shape during generation. Reuse ``load_table``; do not duplicate
rule text in Python except where a named error code is required.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from found_money.profile.gate import load_table_guardrails
from found_money.strategy.intelligence import load_table

if TYPE_CHECKING:  # pragma: no cover - typing only
    from found_money.contracts.campaign import CompleteRecoveryPlaySetV1, OfferLadderV1, OfferRungV1

_ROLE_ALIASES: dict[str, tuple[str, ...]] = {
    "high_anchor": ("high_anchor", "anchor", "high anchor"),
    "core": ("core", "recommended"),
    "downsell": ("downsell", "save path", "fallback"),
}


def _normalized(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.casefold()))


def _rung_tuple(rung: OfferRungV1) -> tuple[str, str, str]:
    return (
        _normalized(rung.scope.text),
        _normalized(rung.support.text),
        _normalized(rung.commitment.text),
    )


def _rationale_names_other_roles(rung: OfferRungV1, others: list[OfferRungV1]) -> bool:
    rationale = rung.rationale.text.casefold()
    for other in others:
        if any(alias in rationale for alias in _ROLE_ALIASES[other.role]):
            return True
    return False


def offer_ladder_violations(ladder: OfferLadderV1) -> list[str]:
    """Return named build-path violations for one offer ladder."""

    guardrails = load_table_guardrails()
    errors: list[str] = []
    rungs_by_role = {rung.role: rung for rung in ladder.rungs}
    core = rungs_by_role["core"]
    downsell = rungs_by_role["downsell"]
    anchor = rungs_by_role["high_anchor"]

    ladder_refs = set(ladder.evidence_references)
    for index, rung in enumerate(ladder.rungs):
        prefix = f"$.offer_ladder.rungs[{index}]"
        if not (rung.condition.text or "").strip():
            rule = guardrails.get("every_rung_carries_a_condition", "")
            errors.append(f"{prefix}.condition:every_rung_carries_a_condition:{rule}")
        for constraint in rung.constraints:
            for ref in constraint.evidence_ids:
                if ref not in ladder_refs:
                    errors.append(f"{prefix}.constraints:evidence_outside_ladder_set")
        for field in (rung.scope, rung.support, rung.commitment, rung.rationale, rung.condition):
            for ref in field.evidence_ids:
                if ref not in ladder_refs:
                    errors.append(f"{prefix}:evidence_outside_ladder_set")
        others = [item for item in ladder.rungs if item.id != rung.id]
        if not _rationale_names_other_roles(rung, others):
            errors.append(f"{prefix}.rationale:role_delta_not_named")

    anchor_tuple = _rung_tuple(anchor)
    core_tuple = _rung_tuple(core)
    downsell_tuple = _rung_tuple(downsell)
    if core_tuple == anchor_tuple and core.concedes_money != anchor.concedes_money:
        errors.append("$.offer_ladder:role_confusion:core_differs_from_anchor_by_price_only")
    if core_tuple == downsell_tuple and core.concedes_money != downsell.concedes_money:
        errors.append("$.offer_ladder:role_confusion:core_differs_from_downsell_by_price_only")

    downsell_rule = guardrails.get("downsell_reduces_scope", "")
    if downsell.scope_reduction is None:
        errors.append(f"$.offer_ladder.rungs[2]:downsell_reduces_scope:{downsell_rule}")
    elif _normalized(downsell.scope_reduction.text) == _normalized(core.scope.text):
        errors.append(f"$.offer_ladder.rungs[2]:downsell_reduces_scope:{downsell_rule}")
    elif _normalized(downsell.scope.text) == _normalized(core.scope.text):
        errors.append(f"$.offer_ladder.rungs[2]:downsell_reduces_scope:{downsell_rule}")

    if downsell_tuple == core_tuple and (
        downsell.discount_percent is not None
        or downsell.concedes_money != core.concedes_money
        or downsell.commitment_term != core.commitment_term
    ):
        errors.append("$.offer_ladder:role_confusion:downsell_differs_from_core_by_price_only")

    discount_rule = guardrails.get("discount_requires_commitment_term", "")
    for index, rung in enumerate(ladder.rungs):
        if rung.discount_percent is None:
            continue
        if not (rung.commitment_term or "").strip():
            errors.append(
                f"$.offer_ladder.rungs[{index}]:discount_requires_commitment_term:{discount_rule}"
            )

    unpriced_rule = guardrails.get("unpriced_piles_concede_nothing", "")
    if ladder.pile_value_basis in {"modeled_opportunity", "mixed"} or (
        ladder.pile_confidence_class == "mixed"
    ):
        for index, rung in enumerate(ladder.rungs):
            if rung.concedes_money or rung.discount_percent is not None:
                errors.append(
                    f"$.offer_ladder.rungs[{index}]:unpriced_piles_concede_nothing:{unpriced_rule}"
                )

    return sorted(set(errors))


def offer_ladder_set_violations(play_set: CompleteRecoveryPlaySetV1) -> list[str]:
    errors: list[str] = []
    for play_index, play in enumerate(play_set.plays):
        prefix = f"$.plays[{play_index}]"
        errors.extend(
            violation.replace("$.offer_ladder", f"{prefix}.offer_ladder")
            for violation in offer_ladder_violations(play.offer_ladder)
        )
    return errors


def load_offer_direction_guardrails() -> dict[str, str]:
    """Expose guardrail text for tests; table is the authority."""

    guardrails = load_table_guardrails()
    return {key: str(value) for key, value in guardrails.items() if isinstance(value, str)}


def discount_margin_ceiling_percent() -> float:
    table = load_table("offer-direction")
    return float(table["discount_margin_ceiling_percent"])
