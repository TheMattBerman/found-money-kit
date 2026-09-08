"""Pure, Decimal-only, explainable ranking for Money Map piles."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation
from found_money.contracts.events import RecoveryCandidateSetV1, RecoveryCandidateV1
from found_money.contracts.map import MoneyMapPileV1, MoneyMapV1
from found_money.contracts.ranking import (
    DEFAULT_RANK_WEIGHTS,
    RANK_FACTOR_NAMES,
    RankFactorV1,
    RankFactorName,
    RankExplanationV1,
    quantize_rank,
)
from found_money.contracts.value import ContributionLedgerV1, ContributionV1

RECENCY_WINDOW_DAYS = Decimal("180")

# These are deterministic event-strength priors, not probabilities or model
# output. They are deliberately documented in the operating contract.
EVENT_STRENGTH_SCORES: dict[str, Decimal] = {
    "failed_payment": Decimal("1.00"),
    "payment_rescue": Decimal("1.00"),
    "renewal_upsell": Decimal("0.95"),
    "canceled_customer": Decimal("0.90"),
    "trial_no_convert": Decimal("0.85"),
    "expired_trial": Decimal("0.85"),
    "closed_lost_stale_deal": Decimal("0.80"),
    "silent_proposal": Decimal("0.78"),
    "no_show_rebook": Decimal("0.75"),
    "disappeared_high_value_customer": Decimal("0.72"),
    "overdue_reorder": Decimal("0.70"),
    "lapsed_repeat_buyer": Decimal("0.68"),
    "engaged_unbooked": Decimal("0.60"),
}

_OFFER_KEYS = ("offer_fit", "offer_fit_score", "offer_fit_rate")
_FRICTION_KEYS = ("friction", "friction_score")


def _source_names(candidate: RecoveryCandidateV1) -> set[str]:
    names: set[str] = set()
    for key in candidate.lineage:
        label = "".join(char if char.isalnum() else "_" for char in key.casefold())
        label = label.strip("_")
        if label and label not in {"customer_token", "economic_unit_key", "lineage"}:
            names.add(label)
    return names or {"lineage"}


def _evidence_references(candidates: list[RecoveryCandidateV1]) -> list[str]:
    # Keep the explanation inspectable without serializing candidate keys,
    # lineage values, or arbitrary provider reference strings into public map
    # artifacts. These labels identify evidence classes, not people or records.
    references = {"evidence:qualifying_fields"}
    references.update(
        f"source:{source}" for candidate in candidates for source in _source_names(candidate)
    )
    return sorted(references)


def _optional_decimal(candidate: RecoveryCandidateV1, keys: tuple[str, ...]) -> Decimal | None:
    values = {key.casefold(): value for key, value in candidate.qualifying_evidence.items()}
    for key in keys:
        raw = values.get(key.casefold())
        if raw is None:
            continue
        try:
            value = Decimal(raw)
        except (InvalidOperation, ValueError):
            return None
        if not value.is_finite() or value < 0 or value > 1:
            return None
        return value
    return None


def _candidate_for_contribution(
    contribution: ContributionV1,
    candidate_set: RecoveryCandidateSetV1,
) -> RecoveryCandidateV1:
    by_candidate_key = {item.candidate_key: item for item in candidate_set.candidates}
    by_economic_key: dict[str, list[RecoveryCandidateV1]] = defaultdict(list)
    for candidate_item in candidate_set.candidates:
        by_economic_key[candidate_item.economic_unit_key].append(candidate_item)

    candidate: RecoveryCandidateV1 | None = (
        by_candidate_key.get(contribution.candidate_key)
        if contribution.candidate_key is not None
        else None
    )
    if candidate is None:
        matches = by_economic_key.get(contribution.economic_unit_key, [])
        if len(matches) != 1:
            raise ValueError(
                f"contribution {contribution.economic_unit_key} does not resolve to one candidate"
            )
        candidate = matches[0]

    expected_pile = (
        "payment_rescue" if candidate.event_family == "failed_payment" else candidate.event_family
    )
    if contribution.pile_id not in {expected_pile, "failed_payment"}:
        raise ValueError(
            f"pile {contribution.pile_id} does not match candidate event family "
            f"{candidate.event_family}"
        )
    return candidate


def _recency_score(candidate: RecoveryCandidateV1, built_at: datetime) -> Decimal:
    days: int | None = candidate.recency_days
    if days is None and candidate.event_at is not None:
        delta = built_at - candidate.event_at
        days = max(0, delta.days)
    if days is None:
        return Decimal(0)
    return quantize_rank(max(Decimal(0), Decimal(1) - Decimal(days) / RECENCY_WINDOW_DAYS))


def _average(values: list[Decimal]) -> Decimal:
    if not values:
        return Decimal(0)
    return quantize_rank(sum(values, Decimal(0)) / Decimal(len(values)))


def _factor(
    name: RankFactorName,
    score: Decimal,
    weight: Decimal,
    *,
    source_count: int,
    explanation: str,
    references: list[str],
) -> RankFactorV1:
    return RankFactorV1(
        name=name,
        score=score,
        weight=weight,
        contribution=quantize_rank(score * weight),
        source_count=source_count,
        explanation=explanation,
        evidence_references=references,
    )


def _normalized_weights(weights: Mapping[str, Decimal] | None) -> dict[str, Decimal]:
    values = dict(DEFAULT_RANK_WEIGHTS if weights is None else weights)
    if set(values) != set(RANK_FACTOR_NAMES):
        raise ValueError("rank weights must declare every factor exactly once")
    normalized: dict[str, Decimal] = {}
    for name in RANK_FACTOR_NAMES:
        value = values[name]
        if isinstance(value, float) or isinstance(value, bool):
            raise ValueError("rank weights must not use binary floating point")
        value = Decimal(value)
        if not value.is_finite() or value < 0 or value > 100:
            raise ValueError("rank weights must be finite values between 0 and 100")
        normalized[name] = quantize_rank(value)
    if sum(normalized.values(), Decimal(0)) != Decimal(100):
        raise ValueError("rank weights must sum to 100")
    return normalized


def _target_id(pile: MoneyMapPileV1) -> str:
    return f"pile/{pile.pile_id}/{pile.currency}"


def rank_money_map(
    money_map: MoneyMapV1,
    ledger: ContributionLedgerV1,
    candidate_set: RecoveryCandidateSetV1,
    *,
    weights: Mapping[str, Decimal] | None = None,
) -> MoneyMapV1:
    """Apply deterministic ranking and navigation defaults to a Money Map.

    All comparisons are dimensionless or within one currency. The function is
    pure: it reads only the supplied models and never opens a socket or reads a
    credential.
    """
    if money_map.run_id != ledger.run_id or money_map.run_id != candidate_set.run_id:
        raise ValueError("money map, ledger, and candidate set run_id values must match")
    rank_weights = _normalized_weights(weights)

    grouped: dict[tuple[str, str], list[tuple[ContributionV1, RecoveryCandidateV1]]] = defaultdict(
        list
    )
    for contribution in ledger.contributions:
        candidate = _candidate_for_contribution(contribution, candidate_set)
        grouped[(contribution.pile_id, contribution.currency)].append((contribution, candidate))

    value_max_by_currency: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
    customer_count_by_group: dict[tuple[str, str], int] = {}
    for key, rows in grouped.items():
        value_max_by_currency[key[1]] = max(
            value_max_by_currency[key[1]],
            sum((row.amount_minor for row, _candidate in rows), Decimal(0)),
        )
        customer_count_by_group[key] = len({candidate.customer_token for _, candidate in rows})
    max_customer_count = max(customer_count_by_group.values(), default=0)

    ranked_rows: list[tuple[MoneyMapPileV1, RankExplanationV1]] = []
    for pile in money_map.piles:
        key = (pile.pile_id, pile.currency)
        rows = grouped.get(key, [])
        candidates = [candidate for _row, candidate in rows]
        references = _evidence_references(candidates)
        source_count = len(set().union(*(_source_names(candidate) for candidate in candidates)))

        currency_max = value_max_by_currency[pile.currency]
        value_score = (
            quantize_rank(pile.selected_value_minor / currency_max)
            if currency_max > 0
            else Decimal(0)
        )
        event_values = [
            EVENT_STRENGTH_SCORES.get(candidate.event_family, Decimal(0))
            for candidate in candidates
        ]
        event_score = _average(event_values)
        recency_values = [_recency_score(candidate, money_map.built_at) for candidate in candidates]
        recency_sources = sum(
            candidate.event_at is not None or candidate.recency_days is not None
            for candidate in candidates
        )
        recency_score = _average(recency_values)

        customer_count = customer_count_by_group.get(key, pile.customer_count)
        reachable_score = (
            quantize_rank(Decimal(customer_count) / Decimal(max_customer_count))
            if max_customer_count > 0
            else Decimal(0)
        )

        offer_values = [
            value
            for candidate in candidates
            if (value := _optional_decimal(candidate, _OFFER_KEYS)) is not None
        ]
        offer_score = _average(offer_values)
        friction_values = [
            Decimal(1) - value
            for candidate in candidates
            if (value := _optional_decimal(candidate, _FRICTION_KEYS)) is not None
        ]
        friction_score = _average(friction_values)
        proof_values = [
            min(
                Decimal(1),
                Decimal(len(candidate.qualifying_evidence) + len(candidate.evidence_references))
                / Decimal(3),
            )
            for candidate in candidates
        ]
        proof_score = _average(proof_values)

        readiness_values = [
            Decimal("1.00") if candidate.confidence_class == "observed" else Decimal("0.75")
            for candidate in candidates
        ]
        readiness_score = _average(readiness_values)
        readiness = (
            "ready_for_strategy"
            if pile.value_basis in {"observed_face_value", "modeled_opportunity"}
            and pile.confidence_class in {"observed", "modeled"}
            else "needs_strategy_review"
        )

        factors = [
            _factor(
                "value",
                value_score,
                rank_weights["value"],
                source_count=len(candidates),
                explanation=(
                    f"{pile.selected_value_minor} {pile.currency} ranked relative to "
                    "other piles in the same currency; currencies are never converted"
                ),
                references=references,
            ),
            _factor(
                "event_strength",
                event_score,
                rank_weights["event_strength"],
                source_count=len(candidates),
                explanation="Declared event-family strength prior from deterministic detector output",
                references=references,
            ),
            _factor(
                "recency",
                recency_score,
                rank_weights["recency"],
                source_count=recency_sources,
                explanation=(
                    f"Event age is measured from the map built_at over a {RECENCY_WINDOW_DAYS}-day "
                    "deterministic window"
                    if recency_sources
                    else "No event timestamp or recency evidence supplied; factor withheld"
                ),
                references=references if recency_sources else [],
            ),
            _factor(
                "reachable_count",
                reachable_score,
                rank_weights["reachable_count"],
                source_count=customer_count,
                explanation=f"{customer_count} distinct reachable customer tokens in this pile",
                references=references,
            ),
            _factor(
                "offer_fit",
                offer_score,
                rank_weights["offer_fit"],
                source_count=len(offer_values),
                explanation=(
                    "Explicit qualifying evidence offer-fit values averaged"
                    if offer_values
                    else "No offer-fit evidence supplied; factor withheld"
                ),
                references=references if offer_values else [],
            ),
            _factor(
                "friction",
                friction_score,
                rank_weights["friction"],
                source_count=len(friction_values),
                explanation=(
                    "Explicit friction evidence inverted so lower friction scores higher"
                    if friction_values
                    else "No friction evidence supplied; factor withheld"
                ),
                references=references if friction_values else [],
            ),
            _factor(
                "proof_context",
                proof_score,
                rank_weights["proof_context"],
                source_count=len(candidates),
                explanation="Qualifying evidence and evidence-reference coverage, capped at three items",
                references=references,
            ),
            _factor(
                "readiness",
                readiness_score,
                rank_weights["readiness"],
                source_count=len(candidates),
                explanation=f"Readiness follows candidate confidence and is {readiness}",
                references=references,
            ),
        ]
        explanation = RankExplanationV1(
            total_score=quantize_rank(sum((factor.contribution for factor in factors), Decimal(0))),
            weights=rank_weights,
            factors=factors,
            tie_break_key=f"{pile.pile_id}|{pile.currency}",
        )
        navigation = pile.navigation
        if navigation is None:
            from found_money.contracts.map import MoneyMapNavigationV1

            navigation = MoneyMapNavigationV1(
                target_id=_target_id(pile),
                state="deferred",
                next_action="review_strategy_when_available",
            )
        ranked_rows.append(
            (
                pile.model_copy(
                    update={
                        "source_count": source_count,
                        "readiness": readiness,
                        "rank_explanation": explanation,
                        "navigation": navigation,
                    }
                ),
                explanation,
            )
        )

    ranked_rows.sort(key=lambda item: (-item[1].total_score, item[1].tie_break_key))
    ranked_piles = [
        pile.model_copy(update={"rank": index})
        for index, (pile, _explanation) in enumerate(ranked_rows, 1)
    ]
    if money_map.recommended_play_ids:
        next_action = "review_recommended_play"
    elif money_map.piles:
        next_action = "review_top_ranked_pile"
    elif money_map.data_gap_count:
        next_action = "review_data_gaps"
    else:
        next_action = "no_recoverable_opportunity"

    all_candidates = candidate_set.candidates
    all_sources = set().union(*(_source_names(candidate) for candidate in all_candidates))
    return money_map.model_copy(
        update={
            "piles": ranked_piles,
            "customer_count": len({candidate.customer_token for candidate in all_candidates}),
            "source_count": len(all_sources),
            "next_action": next_action,
        }
    )


__all__ = [
    "EVENT_STRENGTH_SCORES",
    "RECENCY_WINDOW_DAYS",
    "rank_money_map",
]
