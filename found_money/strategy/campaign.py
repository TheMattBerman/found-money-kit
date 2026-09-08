"""Deterministic three-play SaaS strategy fixture and proof helpers."""

from __future__ import annotations

import hashlib
import html
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from itertools import combinations
from typing import Any, cast

from found_money.contracts.campaign import (
    CompleteRecoveryPlaySetV1,
    CompleteRecoveryPlayV1,
    DifferentiationCheckV1,
    DifferentiationReportV1,
    CustomerCopyV1,
    GroundedCopyV1,
    StrategyReviewerPacketV2,
)
from found_money.contracts.map import MoneyMapV1
from found_money.contracts.strategy import (
    GroundedStrategyEvidencePacketV1,
    RecoveryPlaySetV1,
    StrategyAuditReceiptV1,
    StrategyBusinessProfileV1,
    StrategyTokenUsageV1,
)
from found_money.strategy.boundary import build_grounded_strategy_packet
from found_money.strategy.intelligence import (
    no_email_play_for,
    reengage_days_for,
    touch_ceiling_for,
)
from found_money.strategy.copy_rules import (
    first_touch_violations,
    pile_prohibition_violations,
    pile_prohibits_email,
)
from found_money.strategy.card_checks import card_violations
from found_money.strategy.offer_ladder_checks import offer_ladder_set_violations
from found_money.contracts.value import (
    CURRENCY_EXPONENTS,
    format_major_units,
    normalize_currency,
)

_AXES = (
    "diagnosis",
    "offer_mechanism",
    "lifecycle_sequence",
    "primary_cta",
    "channel_emphasis",
    "creative_big_idea",
)
_KIND_PREFIXES = {
    "product": ("ev_business_product",),
    "proof": ("ev_business_proof",),
    "objection": ("ev_business_product", "ev_business_proof"),
    "urgency": ("ev_business_capacity",),
    "capacity": ("ev_business_capacity",),
    "destination": ("ev_business_destination",),
}
_NUMBER_WORD = (
    r"(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|"
    r"thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|million|billion)"
)
_WORD_NUMBER = rf"{_NUMBER_WORD}(?:[\s-]+{_NUMBER_WORD})*"
# The magnitude suffix must be a whole token. Without the boundary, "$49 bonus"
# parses as 49 billion and "$220.00 membership" as 220 million, because the
# suffix class swallows the first letter of the following word.
_SCALE_WORD = r"(?:hundred|thousand|million|billion)"
_CLAIM_AMOUNT = rf"(?:\d[\d,]*(?:\.\d+)?(?:\s?[kKmMbB]\b|\s+{_SCALE_WORD}\b)?|{_WORD_NUMBER})"
_CURRENCY_SYMBOL_RE = re.compile(
    rf"(?P<currency>[$£€])\s*(?P<amount>{_CLAIM_AMOUNT})", re.IGNORECASE
)
_CURRENCY_PREFIX_RE = re.compile(
    rf"\b(?P<currency>usd|gbp|eur)\s+(?P<amount>{_CLAIM_AMOUNT})\b", re.IGNORECASE
)
_CURRENCY_WORD_RE = re.compile(
    rf"\b(?P<amount>{_CLAIM_AMOUNT})\s+(?P<currency>dollars?|usd|bucks?|pounds?|gbp|euros?|eur)\b",
    re.IGNORECASE,
)
_PERCENT_RE = re.compile(rf"\b(?P<amount>{_CLAIM_AMOUNT})\s*(?:%|percent\b)", re.IGNORECASE)
_CAPACITY_RE = re.compile(
    rf"\b(?P<amount>{_CLAIM_AMOUNT})\s+recovery reviews per week\b", re.IGNORECASE
)
_WORD_VALUES = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}
_WORD_SCALES = {
    "hundred": 100,
    "thousand": 1_000,
    "million": 1_000_000,
    "billion": 1_000_000_000,
}
_MAGNITUDE_SCALES = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}
_CURRENCY_CODES = {
    "$": "usd",
    "dollar": "usd",
    "dollars": "usd",
    "buck": "usd",
    "bucks": "usd",
    "usd": "usd",
    "£": "gbp",
    "pound": "gbp",
    "pounds": "gbp",
    "gbp": "gbp",
    "€": "eur",
    "euro": "eur",
    "euros": "eur",
    "eur": "eur",
}


@dataclass(frozen=True)
class _NumericClaim:
    kind: str
    value: Decimal


def _claim_amount(value: str) -> Decimal:
    normalized = value.casefold().replace(",", "").strip()
    compact = normalized.replace(" ", "")
    if re.fullmatch(r"\d+(?:\.\d+)?[kmb]?", compact):
        suffix = compact[-1] if compact[-1] in _MAGNITUDE_SCALES else ""
        number = compact[:-1] if suffix else compact
        return Decimal(number) * _MAGNITUDE_SCALES.get(suffix, 1)

    digits_then_scale = re.fullmatch(rf"(\d[\d,]*(?:\.\d+)?)\s+({_SCALE_WORD})", normalized)
    if digits_then_scale is not None:
        number, scale_word = digits_then_scale.groups()
        return Decimal(number.replace(",", "")) * _WORD_SCALES[scale_word]

    current = 0
    total = 0
    for word in normalized.replace("-", " ").split():
        if word in _WORD_VALUES:
            current += _WORD_VALUES[word]
        elif word == "hundred":
            current = (current or 1) * _WORD_SCALES[word]
        else:
            total += (current or 1) * _WORD_SCALES[word]
            current = 0
    return Decimal(total + current)


def _numeric_claims(text: str) -> set[_NumericClaim]:
    claims: set[_NumericClaim] = set()
    for match in _CURRENCY_SYMBOL_RE.finditer(text):
        currency = _CURRENCY_CODES[match.group("currency").casefold()]
        claims.add(_NumericClaim(f"currency:{currency}", _claim_amount(match.group("amount"))))
    for pattern in (_CURRENCY_PREFIX_RE, _CURRENCY_WORD_RE):
        for match in pattern.finditer(text):
            currency = _CURRENCY_CODES[match.group("currency").casefold()]
            claims.add(_NumericClaim(f"currency:{currency}", _claim_amount(match.group("amount"))))
    for match in _PERCENT_RE.finditer(text):
        claims.add(_NumericClaim("percent", _claim_amount(match.group("amount"))))
    for match in _CAPACITY_RE.finditer(text):
        claims.add(
            _NumericClaim(
                "capacity:recovery_reviews_per_week", _claim_amount(match.group("amount"))
            )
        )
    return claims


def _has_unsupported_numeric_claim(text: str, evidence_values: list[str]) -> bool:
    supported = {
        claim for evidence_value in evidence_values for claim in _numeric_claims(evidence_value)
    }
    return not _numeric_claims(text).issubset(supported)


class CompleteStrategyGroundingError(ValueError):
    def __init__(self, paths: list[str]):
        self.paths = tuple(paths)
        super().__init__("unsupported recovery-play content at: " + ", ".join(paths))


@dataclass(frozen=True)
class CanonicalSaasStrategyRun:
    packet: GroundedStrategyEvidencePacketV1
    recovery_plays: CompleteRecoveryPlaySetV1 | RecoveryPlaySetV1
    receipt: StrategyAuditReceiptV1
    differentiation: DifferentiationReportV1 | None

    def complete_plays(self) -> CompleteRecoveryPlaySetV1:
        if not isinstance(self.recovery_plays, CompleteRecoveryPlaySetV1):
            raise ValueError("recovery plays are withheld pending missing payment")
        return self.recovery_plays


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _copy(text: str, *refs: str, kind: str = "general") -> dict[str, Any]:
    return {"text": text, "kind": kind, "evidence_ids": list(refs)}


def _ccopy(text: str, kind: str = "general") -> dict[str, Any]:
    """Customer-facing copy payload: ungrounded, bounded only by the ledger check."""
    return {"text": text, "kind": kind}


def _core_rung(play_id: str) -> str:
    return f"{play_id}-core"


def _ladder_for_play(
    ladder_id: str,
    play_id: str,
    *,
    diagnosis_copy: dict[str, Any],
    product: str,
    margin: str,
    proof: str,
    anchor_mechanism: str,
    core_mechanism: str,
    downsell_mechanism: str,
    outcome_text: str,
    ordering_basis_text: str,
    rung_email_copy: tuple[tuple[str, str, str], tuple[str, str, str], tuple[str, str, str]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Build one anchored ladder and the three rung copy packages for a play."""

    ladder_refs = sorted(
        set(
            _all_refs(diagnosis_copy)
            + [product, margin, proof, "ev_pile_1_basis", "ev_pile_1_value"]
        )
    )
    anchor_id = f"{play_id}-anchor"
    core_id = _core_rung(play_id)
    downsell_id = f"{play_id}-downsell"

    def rung(
        rung_id: str,
        role: str,
        mechanism: str,
        terms: str,
        scope: str,
        support: str,
        commitment: str,
        rationale: str,
        condition: str,
        *,
        scope_reduction: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": rung_id,
            "role": role,
            "mechanism": _ccopy(mechanism, kind="product"),
            "terms": _ccopy(terms, kind="product"),
            "scope": _copy(scope, product, margin, kind="product"),
            "support": _copy(support, product, kind="product"),
            "commitment": _copy(commitment, product, margin, kind="product"),
            "constraints": [
                _copy(
                    "Do not add an unsupported discount or promise beyond the evidence packet",
                    product,
                    margin,
                    kind="product",
                )
            ],
            "rationale": _copy(rationale, product, margin, kind="product"),
            "condition_kind": "implementation_step",
            "condition": _copy(condition, product, margin, kind="product"),
            "concedes_money": False,
        }
        if scope_reduction is not None:
            payload["scope_reduction"] = _copy(scope_reduction, product, kind="product")
        return payload

    ladder = {
        "offer_ladder_id": ladder_id,
        "diagnosis": diagnosis_copy,
        "outcome": _copy(outcome_text, product, "ev_pile_1_value", kind="product"),
        "ordering_basis": _copy(ordering_basis_text, product, margin, kind="product"),
        "evidence_references": ladder_refs,
        "pile_value_basis": "observed_face_value",
        "pile_confidence_class": "observed",
        "selected_offer_rung_id": core_id,
        "rungs": [
            rung(
                anchor_id,
                "high_anchor",
                anchor_mechanism,
                "Twelve-month commitment with full onboarding included in the annual term",
                "Full product scope with onboarding and every feature tier enabled",
                "White-glove onboarding and a dedicated implementation review",
                "Twelve-month committed term at the standard annual rate",
                "Relative to the core and downsell rungs, the anchor establishes the fullest scope and commitment ceiling",
                "Complete onboarding before the first billing cycle closes",
            ),
            rung(
                core_id,
                "core",
                core_mechanism,
                "Month-to-month flexibility with guided destination access",
                "Standard product scope with the features the evidence names",
                "Guided destination support without a full onboarding program",
                "Month-to-month continuation through the approved destination",
                "Relative to the anchor and downsell, the core is the recommended balance of scope, support, and commitment",
                "Update details through the approved destination before the review window closes",
            ),
            rung(
                downsell_id,
                "downsell",
                downsell_mechanism,
                "Self-serve access only with no onboarding or human review slot",
                "Essential billing access without premium feature tiers",
                "Self-serve portal instructions with no human onboarding",
                "No commitment term beyond completing the billing update",
                "Relative to the anchor and core, the downsell preserves the recovery outcome with reduced scope and support",
                "Complete the self-serve update without requesting onboarding",
                scope_reduction="Reduced to essential billing access without premium tiers or human onboarding",
            ),
        ],
    }
    roles = ("high_anchor", "core", "downsell")
    rung_ids = (anchor_id, core_id, downsell_id)
    packages = []
    for role, rung_id, (subject, body, cta) in zip(roles, rung_ids, rung_email_copy, strict=True):
        packages.append(
            {
                "offer_ladder_id": ladder_id,
                "offer_rung_id": rung_id,
                "subject": _ccopy(subject, kind="product"),
                "body": _ccopy(body, kind="general"),
                "cta": _ccopy(cta, kind="destination"),
            }
        )
    return ladder, packages


def _all_refs(value: Any) -> list[str]:
    refs: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "evidence_ids" and isinstance(item, list):
                refs.extend(item)
            else:
                refs.extend(_all_refs(item))
    elif isinstance(value, list):
        for item in value:
            refs.extend(_all_refs(item))
    return refs


_MINOR_UNIT_EVIDENCE_CATEGORIES = frozenset({"map_value"})
_MINOR_UNIT_RE = re.compile(r"^\s*(\d+)\s+([A-Za-z]{3})\s*$")


def _evidence_text(category: str, value: str) -> str:
    """Render one evidence value in the units human copy uses.

    Ledger evidence carries minor units: ``build_grounded_strategy_packet`` writes
    ``f"{pile.selected_value_minor} {pile.currency}"``, so a pile worth $49.00 is
    recorded as ``$49.00``. Human copy says ``$49.00``. Comparing the two raw
    magnitudes both blocks correct copy and admits a 100x overstatement, because
    ``$4,900`` matches the minor-unit digits exactly.

    Normalizing at the comparison boundary keeps evidence production unchanged.
    Only money categories are scaled: percent and capacity values are not
    currency and must be left alone.
    """

    if category not in _MINOR_UNIT_EVIDENCE_CATEGORIES:
        return value
    match = _MINOR_UNIT_RE.match(value)
    if match is None:
        return value
    amount, code = match.groups()
    try:
        currency = normalize_currency(code)
    except (KeyError, ValueError):
        return value
    if currency not in CURRENCY_EXPONENTS:
        return value
    return f"{format_major_units(amount, currency)} {currency}"


def _apply_pile_plan(payload: dict[str, Any]) -> dict[str, Any]:
    """Stamp the sequence plan and trim the sequence to what the pile warrants.

    Length is a property of the pile, not of the schema. The ceiling comes from
    ``skills/recovery-intelligence/tables/pile-cohort.json`` so the rule can be
    edited as expertise rather than as code. Evidence references are recomputed
    after trimming, because dropping a step can orphan a reference.
    """

    pile_id = payload.get("pile_id", "payment_rescue")
    ceiling = touch_ceiling_for(pile_id)
    steps = payload["email_sequence"]
    if len(steps) > ceiling:
        payload["email_sequence"] = steps[:ceiling]
        payload["calendar"] = payload["calendar"][:ceiling]
    remaining = len(payload["email_sequence"])
    payload["sequence_plan"] = {
        "intent": "single_touch" if remaining == 1 else "multi_touch",
        "stops_after_last": True,
        "reengage_days": reengage_days_for(pile_id),
    }
    payload["evidence_references"] = sorted(set(_all_refs(payload)))
    return payload


def _card(
    play_rank: int,
    card_rank: int,
    *,
    name: str,
    tension: str,
    idea: str,
    hook: str,
    visual: str,
    proof: str,
    style: str,
    cta: str,
    fit: str,
    proof_extra_refs: tuple[str, ...] = (),
) -> dict[str, Any]:
    product = "ev_business_product"
    proof_ref = "ev_business_proof"
    destination = "ev_business_destination"
    return {
        "card_id": f"concept-{play_rank}-{card_rank}",
        "card_name": name,
        "audience_tension": _copy(tension, product, kind="product"),
        "big_idea": _copy(idea, product, kind="product"),
        # Customer-facing creative. Grounded in the product, not the ledger:
        # forcing ev_pile_1_value here is what made every hook quote a
        # dollar figure at the audience. See
        "hook": _copy(hook, product, kind="product"),
        "opening_visual": _copy(visual, product, kind="product"),
        "proof_device": _copy(proof, proof_ref, *proof_extra_refs, kind="proof"),
        "format_style": style,
        "cta": _copy(cta, destination, kind="destination"),
        "pile_fit": _copy(fit, "ev_pile_1_basis", "ev_pile_1_value", kind="general"),
        "production_requirements": [
            "Use synthetic interface graphics and aggregate figures only",
            "Prepare static frames suitable for later vertical production",
        ],
    }


def _play_payload(
    rank: int,
    *,
    play_id: str,
    campaign: str,
    strategy: str,
    diagnosis: str,
    offer_recommendation: str,
    mechanism: str,
    anchor_mechanism: str,
    downsell_mechanism: str,
    ladder_outcome: str,
    ladder_ordering_basis: str,
    rung_email_copy: tuple[tuple[str, str, str], tuple[str, str, str], tuple[str, str, str]],
    sequence: str,
    cta: str,
    channel: str,
    big_idea: str,
    email_steps: tuple[tuple[str, str, str, int], ...],
    sms_messages: tuple[str, ...],
    calendar_steps: tuple[tuple[int, str], ...],
    task_track: str,
    cards: list[dict[str, Any]],
    real_urgency: bool,
) -> dict[str, Any]:
    product = "ev_business_product"
    proof = "ev_business_proof"
    margin = "ev_business_margin"
    capacity = "ev_business_capacity"
    destination = "ev_business_destination"
    amount = "ev_pile_1_value"
    basis = "ev_pile_1_basis"
    payload: dict[str, Any] = {
        "play_id": play_id,
        "rank": rank,
        "title": campaign,
        "rationale": diagnosis,
        "offer_recommendation": offer_recommendation,
        "recommended_actions": [
            "Review the aggregate payment rescue evidence before approval",
            "Approve or withhold the complete sequence without sending it",
        ],
        "campaign_name": campaign,
        "campaign_strategy": _copy(strategy, product, amount, kind="product"),
        "value_basis": _copy(
            "Use the observed face value basis without treating it as recovered revenue",
            basis,
        ),
        "audience": _copy(
            "Account holders with an observed failed payment for the Annual subscription",
            product,
            amount,
            kind="product",
        ),
        "recoverability": _copy(
            "The observed $49.00 payment rescue opportunity remains reviewable",
            amount,
            kind="number",
        ),
        "diagnosis": _copy(diagnosis, product, amount, capacity, kind="product"),
    }
    diagnosis_copy = payload["diagnosis"]
    ladder_id = f"{play_id}-ladder"
    ladder, rung_packages = _ladder_for_play(
        ladder_id,
        play_id,
        diagnosis_copy=diagnosis_copy,
        product=product,
        margin=margin,
        proof=proof,
        anchor_mechanism=anchor_mechanism,
        core_mechanism=mechanism,
        downsell_mechanism=downsell_mechanism,
        outcome_text=ladder_outcome,
        ordering_basis_text=ladder_ordering_basis,
        rung_email_copy=rung_email_copy,
    )
    payload["offer_ladder"] = ladder
    payload["rung_copy_packages"] = rung_packages
    payload.update(
        {
            "lifecycle_sequence": _copy(sequence, product, kind="product"),
            "email_sequence": [
                {
                    "order": index,
                    "lifecycle_stage": _copy(stage, product, kind="product"),
                    "subject": _ccopy(subject, kind="product"),
                    "body": _ccopy(body, kind="general"),
                    "cta": _ccopy(cta, kind="destination"),
                    "wait_days": wait,
                }
                for index, (stage, subject, body, wait) in enumerate(email_steps, start=1)
            ],
            "sms": {
                "available": True,
                "messages": [_ccopy(message, kind="destination") for message in sms_messages],
            },
            "task_talk_track": _copy(task_track, product, destination, kind="destination"),
            "primary_cta": _copy(cta, destination, kind="destination"),
            "objections": [
                {
                    "objection": _copy(
                        "The Annual subscription may no longer feel necessary",
                        product,
                        kind="objection",
                    ),
                    "response": _copy(
                        "Use the Three approved case studies as approved proof without adding claims",
                        proof,
                        kind="proof",
                    ),
                },
                {
                    "objection": _copy(
                        "The Annual subscription payment timing may be inconvenient",
                        product,
                        kind="objection",
                    ),
                    "response": _copy(
                        "Offer the billing portal as the approved destination without inventing terms",
                        destination,
                        kind="destination",
                    ),
                },
            ],
            "urgency": (
                {
                    "text": "Respect the 100 recovery reviews per week capacity",
                    "evidence_ids": [capacity],
                }
                if real_urgency
                else {"text": "none", "evidence_ids": []}
            ),
            "calendar": [
                {
                    "day": day,
                    "action": _copy(action, product, kind="product"),
                    "stop_condition": _copy(
                        "Stop when the Annual subscription payment state changes or approval is withdrawn",
                        product,
                        kind="product",
                    ),
                }
                for day, action in calendar_steps
            ],
            "stop_conditions": [
                _copy(
                    "Stop after an Annual subscription payment state change",
                    product,
                    kind="product",
                ),
                _copy(
                    "Stop when approval for the Annual subscription is withdrawn",
                    product,
                    kind="product",
                ),
            ],
            "tracking": {
                "success_event": _copy(
                    "Record an observed Annual subscription payment state change without claiming recovery",
                    product,
                    kind="product",
                ),
                "tracked_signals": [
                    _copy(
                        "Track aggregate billing portal review completion",
                        destination,
                        kind="destination",
                    ),
                    _copy("Track observed face value status separately", basis, kind="general"),
                ],
            },
            "channel_emphasis": _copy(channel, "ev_business_channel", kind="general"),
            "creative_big_idea": _copy(big_idea, product, kind="product"),
            "concept_cards": cards,
        }
    )
    payload["evidence_references"] = sorted(set(_all_refs(payload)))
    return payload


def _canonical_plays(run_id: str, built_at: datetime) -> CompleteRecoveryPlaySetV1:
    plays = [
        _play_payload(
            1,
            play_id="payment-rescue-friction-fix",
            campaign="Fix the Friction",
            strategy="Remove payment friction around the Annual subscription before discussing value",
            diagnosis="A solvable payment interruption is blocking the Annual subscription despite a $49.00 observed opportunity",
            offer_recommendation="Run the guided billing-update offer against the $49.00 payment_rescue pile first: it removes the only friction the evidence names and asks the customer for nothing new. If you would rather not send customers to the billing portal, switch to the proof-led reset and keep the same sequence shape.",
            mechanism="A guided billing update for the Annual subscription with no invented discount",
            anchor_mechanism="Annual subscription with full onboarding and a twelve-month commitment at the standard rate",
            downsell_mechanism="Self-serve billing portal update with no guided support or onboarding program",
            ladder_outcome="Restore Annual subscription billing without conceding an unsupported discount",
            ladder_ordering_basis="The anchor establishes fullest scope, the core is the recommended path, and the downsell is the save path with less scope",
            rung_email_copy=(
                (
                    "full Annual subscription onboarding?",
                    "The premium Annual subscription path includes full onboarding if you want the complete program.",
                    "Review the Annual subscription details in the billing portal",
                ),
                (
                    "billing glitch?",
                    "Did the card on file for the Annual subscription stop working?",
                    "Review the Annual subscription details in the billing portal",
                ),
                (
                    "quick self-serve fix",
                    "The billing portal has a self-serve path if you only need to update the card without onboarding.",
                    "Review the Annual subscription details in the billing portal",
                ),
            ),
            sequence="Recognize the interruption, guide resolution, then confirm closure for the Annual subscription",
            cta="Review the Annual subscription details in the billing portal",
            channel="Lead with email instructions, reinforce with concise SMS, and reserve tasks for unresolved reviews",
            big_idea="The Annual subscription is paused by a fixable payment snag rather than lost intent",
            email_steps=(
                (
                    "Recognition",
                    "billing glitch?",
                    "Did the card on file for the Annual subscription stop working?",
                    0,
                ),
                (
                    "Guided update",
                    "the billing link",
                    "The billing portal has the Annual subscription card details if that is easier than hunting for them.",
                    1,
                ),
                (
                    "Closure decision",
                    "closing this out",
                    "I will close the Annual subscription billing review on our side unless you would rather keep it open.",
                    3,
                ),
            ),
            sms_messages=(
                "Send one SMS after email one pointing the Annual subscription review to the billing portal",
            ),
            calendar_steps=(
                (0, "Send the Annual subscription recognition email"),
                (1, "Send the Annual subscription guided-update email"),
                (3, "Send the Annual subscription closure email"),
            ),
            task_track="Open a human review task only if the Annual subscription billing portal update stays unresolved after the email sequence",
            cards=[
                _card(
                    1,
                    1,
                    name="Dead Login",
                    tension="You only find out the card failed at the moment you actually need the thing",
                    idea="Show the moment access disappears, not the billing notice that preceded it",
                    hook="Deck is due at nine and the login is dead",
                    visual="A laptop on a desk at night showing a locked-out login and a half-written deck",
                    proof="A before-and-after billing-state comparison that uses Three approved case studies as the only proof source",
                    style="First-frame screenshot static, phone-shot, no brand chrome",
                    cta="Open the billing portal for the Annual subscription review",
                    fit="A locked-screen still fits payment_rescue because the failed Annual subscription payment is a billing retry rather than a decision to leave, on a $49.00 opportunity",
                ),
                _card(
                    1,
                    2,
                    name="Nine Days",
                    tension="Nobody on the team wants to be the one who says the billing broke",
                    idea="Put the silence between the failure and the discovery on screen",
                    hook="We were locked out nine days before anyone told me",
                    visual="A team chat thread where the problem finally surfaces nine days late",
                    proof="An on-screen route marker that displays Three approved case studies as the only proof",
                    style="Social comment screenshot, native thread crop, aggregate labels only",
                    cta="Continue the Annual subscription review in the billing portal",
                    fit="A thread screenshot fits payment_rescue because nobody announces a card update retry, so the failed Annual subscription payment surfaces late on a $49.00 opportunity",
                ),
                _card(
                    1,
                    3,
                    name="Back to Sheets",
                    tension="The workaround quietly becomes permanent while nobody makes a decision",
                    idea="Name the substitution people are slightly embarrassed to admit to",
                    hook="How to tell if your team quietly moved back to a spreadsheet",
                    visual="A spreadsheet open in the window where the product used to be",
                    proof="A completed-step stamp that displays Three approved case studies as the only proof",
                    style="Founder letter, text-only static, no design treatment",
                    cta="Complete the Annual subscription review in the billing portal",
                    fit="A plain letter fits payment_rescue because the last billing retry on a failed Annual subscription payment needs candour rather than design, on a $49.00 opportunity",
                ),
            ],
            real_urgency=False,
        ),
        _play_payload(
            2,
            play_id="payment-rescue-proof-reset",
            campaign="Remember the Win",
            strategy="Re-establish the Annual subscription value before requesting a billing decision",
            diagnosis="The Annual subscription payment interruption may reflect faded value memory around a $49.00 observed opportunity",
            offer_recommendation="For the $49.00 payment_rescue pile I would lead with proof instead of the billing link, because a paused decision can be value memory rather than card mechanics. If you have no approved case studies to show, this offer weakens and the friction-fix play is the safer default.",
            mechanism="A proof-led value reset for the Annual subscription using approved case studies",
            anchor_mechanism="Annual subscription with full proof review, onboarding, and a twelve-month commitment",
            downsell_mechanism="A lightweight proof summary with self-serve billing access and no onboarding",
            ladder_outcome="Reconnect the Annual subscription decision to approved proof without conceding margin",
            ladder_ordering_basis="The anchor carries the fullest proof and commitment, the core leads with proof, and the downsell reduces scope to self-serve access",
            rung_email_copy=(
                (
                    "full proof review?",
                    "The premium path includes a full proof review and onboarding if you want the complete program.",
                    "Review the Annual subscription proof and choose the next step in the billing portal",
                ),
                (
                    "quick question",
                    "Are you still trying to get what the Annual subscription was for?",
                    "Review the Annual subscription proof and choose the next step in the billing portal",
                ),
                (
                    "proof summary only",
                    "A short proof summary is available if you only need a reminder before updating billing yourself.",
                    "Review the Annual subscription proof and choose the next step in the billing portal",
                ),
            ),
            sequence="Revisit the outcome, present approved proof, then invite an Annual subscription decision",
            cta="Review the Annual subscription proof and choose the next step in the billing portal",
            channel="Open with a short SMS, carry the proof in email, and skip the review task entirely",
            big_idea="Reconnect the Annual subscription payment decision to the outcome it was meant to create",
            email_steps=(
                (
                    "Outcome recall",
                    "quick question",
                    "Are you still trying to get what the Annual subscription was for?",
                    0,
                ),
                (
                    "Proof stack",
                    "how others handled it",
                    "Three approved case studies cover the same Annual subscription snag if they are any use.",
                    3,
                ),
                (
                    "Decision invite",
                    "where to go from here",
                    "The billing portal has the Annual subscription details whenever you want to pick this up.",
                    7,
                ),
            ),
            sms_messages=(
                "Send one SMS reminder only after the proof email, not as a parallel Annual subscription sequence",
            ),
            calendar_steps=(
                (0, "Send the Annual subscription outcome-recall email"),
                (3, "Send the Annual subscription proof-stack email"),
                (7, "Send the Annual subscription decision-invite email"),
            ),
            task_track="Assign a contextual follow-up task after the proof stack so a human can review the Annual subscription decision in the billing portal",
            cards=[
                _card(
                    2,
                    1,
                    name="Built It Twice",
                    tension="Redoing work you already paid once to have done",
                    idea="Frame the cost as the hours spent again, not the invoice that lapsed",
                    hook="POV you are rebuilding the report you already built once",
                    visual="Two identical reports side by side, one of them dated months earlier",
                    proof="A reflected-outcome panel built from Three approved case studies as the only proof surface",
                    style="Tweet-style static, native post crop, aggregate labels only",
                    cta="Review Annual subscription proof in the billing portal",
                    fit="A native post fits payment_rescue because duplicated work is the felt cost of a failed Annual subscription payment awaiting a billing retry, on a $49.00 opportunity",
                ),
                _card(
                    2,
                    2,
                    name="Two Quarters",
                    tension="The sunk cost is time, which is easier to count than to admit",
                    idea="Let the arithmetic of the wasted quarters carry the whole argument",
                    hook="We spent two quarters rebuilding what we were already paying for",
                    visual="A dense block of plain text with the two quarters marked in the margin",
                    proof="Three approved case studies shown as the three separate proof tiles",
                    style="Wall of text static, high contrast, no imagery, aggregate labels only",
                    cta="Choose the Annual subscription next step in the billing portal",
                    fit="A text wall fits payment_rescue because the argument for a card update retry after a failed Annual subscription payment is arithmetic, on a $49.00 opportunity",
                ),
                _card(
                    2,
                    3,
                    name="Sunday Night",
                    tension="The week starts with a scramble that was supposed to be solved already",
                    idea="Sell back the recovered evening rather than the restored feature",
                    hook="I want Monday numbers without Sunday night",
                    visual="A dark kitchen table late at night with a laptop still open on it",
                    proof="A then-to-next caption that displays Three approved case studies as the only proof",
                    style="Us versus them static, before and after split, aggregate labels only",
                    cta="Set the Annual subscription next step in the billing portal",
                    fit="A before-and-after split fits payment_rescue because the relief is what a billing card update retry restores after a failed Annual subscription payment, on a $49.00 opportunity",
                ),
            ],
            real_urgency=False,
        ),
        _play_payload(
            3,
            play_id="payment-rescue-capacity-window",
            campaign="Reserve the Restart",
            strategy="Use verified review capacity to create an honest Annual subscription decision window",
            diagnosis="The Annual subscription needs a bounded decision path while 100 recovery reviews per week remain available",
            offer_recommendation="The capacity offer suits the $49.00 payment_rescue pile only while the weekly review capacity is real to your team, because the customer is being asked to accept a human review slot. If capacity is soft right now, hold this play and run the friction fix instead.",
            mechanism="A capacity-backed Annual subscription restart review without artificial scarcity",
            anchor_mechanism="Annual subscription with priority onboarding, dedicated review, and twelve-month commitment",
            downsell_mechanism="Self-serve billing portal restart with no human review slot reserved",
            ladder_outcome="Restart the Annual subscription through a bounded human review without inventing urgency",
            ladder_ordering_basis="The anchor reserves the fullest support, the core offers the recommended review slot, and the downsell removes human capacity",
            rung_email_copy=(
                (
                    "priority review slot?",
                    "The premium path reserves priority onboarding and a dedicated review if capacity is available.",
                    "Reserve an Annual subscription restart review through the billing portal",
                ),
                (
                    "still on?",
                    "Have you given up on the Annual subscription for now?",
                    "Reserve an Annual subscription restart review through the billing portal",
                ),
                (
                    "self-serve restart",
                    "You can restart through the billing portal without reserving a human review slot.",
                    "Reserve an Annual subscription restart review through the billing portal",
                ),
            ),
            sequence="Name the review window, reserve capacity, then close the Annual subscription decision",
            cta="Reserve an Annual subscription restart review through the billing portal",
            channel="Lead with a verified-capacity window email, transition to a reserved human review task, and use SMS only after reservation",
            big_idea="An honest Annual subscription restart window replaces vague urgency with verified capacity",
            email_steps=(
                (
                    "Window notice",
                    "still on?",
                    "Have you given up on the Annual subscription for now?",
                    0,
                ),
                (
                    "Capacity reservation",
                    "holding a review slot",
                    "I can hold one of the 100 recovery reviews per week for the Annual subscription in the billing portal.",
                    2,
                ),
                (
                    "Final close",
                    "closing the restart",
                    "I will close the Annual subscription restart review unless you would rather I keep the slot held.",
                    6,
                ),
            ),
            sms_messages=(
                "SMS one: confirm the reserved Annual subscription review window",
                "SMS two: close the confirmed Annual subscription window without adding scarcity",
            ),
            calendar_steps=(
                (0, "Send the Annual subscription window-notice email"),
                (2, "Reserve Annual subscription review capacity and open the human review task"),
                (6, "Close the Annual subscription restart decision"),
            ),
            task_track="Open a reserved human review task only after the Annual subscription window-notice email, then support reservation and close through the billing portal",
            cards=[
                _card(
                    3,
                    1,
                    name="Renewal Friday",
                    tension="Renewing something nobody has opened is harder than cancelling it",
                    idea="Put the unused months and the renewal date inside the same frame",
                    hook="Renewal is Friday and nobody has opened it since March",
                    visual="A wall calendar with March circled and Friday circled and nothing in between",
                    proof="An on-screen capacity-window overlay showing 100 recovery reviews per week",
                    style="Founder customer call, split screen, phone audio, unscripted",
                    cta="Reserve the Annual subscription review in the billing portal",
                    fit="A founder call fits payment_rescue because a human review before the billing retry suits a failed Annual subscription payment nobody has looked at, on a $49.00 opportunity",
                    proof_extra_refs=("ev_business_capacity",),
                ),
                _card(
                    3,
                    2,
                    name="The Other Team",
                    tension="Finding out a team like yours quietly kept the thing you dropped",
                    idea="Make the comparison land as a discovery rather than as a pitch",
                    hook="The other team never stopped using it and nobody mentioned that",
                    visual="Two identical dashboards, one lit and active, one dark and dormant",
                    proof="An on-screen reserved-seat placard that displays three explicitly labeled markers: Three approved case studies as the only proof",
                    style="Use-case expansion, three short scenes, aggregate labels only",
                    cta="Book the Annual subscription review through the billing portal",
                    fit="Short scenes fit payment_rescue because a reserved human review before the billing retry shows the account still in use despite a failed Annual subscription payment, on a $49.00 opportunity",
                ),
                _card(
                    3,
                    3,
                    name="Whole Time",
                    tension="Paying for something whose most useful part you never actually found",
                    idea="Reveal the part of the account that was sitting there the whole time",
                    hook="Since when was that sitting in the account the whole time",
                    visual="One line of text over a flat field, nothing else in frame",
                    proof="Three on-screen lane markers, each explicitly labeled as one of the Three approved case studies as the only proof",
                    style="Headline static, one line over a flat field, aggregate labels only",
                    cta="Enter the Annual subscription restart lane through the billing portal",
                    fit="A one-line static fits payment_rescue because human review into a billing retry needs no explanation on a failed Annual subscription payment, on a $49.00 opportunity",
                ),
            ],
            real_urgency=True,
        ),
    ]
    return CompleteRecoveryPlaySetV1(
        run_id=run_id,
        built_at=built_at,
        provider="fixture",
        plays=[CompleteRecoveryPlayV1.model_validate(_apply_pile_plan(play)) for play in plays],
    )


def _walk_grounded(value: Any, path: str = "$") -> list[tuple[str, GroundedCopyV1]]:
    found: list[tuple[str, GroundedCopyV1]] = []
    if isinstance(value, GroundedCopyV1):
        return [(path, value)]
    if hasattr(value.__class__, "model_fields"):
        for name in value.__class__.model_fields:
            found.extend(_walk_grounded(getattr(value, name), f"{path}.{name}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(_walk_grounded(item, f"{path}[{index}]"))
    return found


def _walk_customer(value: Any, path: str = "$") -> list[tuple[str, CustomerCopyV1]]:
    found: list[tuple[str, CustomerCopyV1]] = []
    if isinstance(value, CustomerCopyV1):
        return [(path, value)]
    if hasattr(value.__class__, "model_fields"):
        for name in value.__class__.model_fields:
            found.extend(_walk_customer(getattr(value, name), f"{path}.{name}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(_walk_customer(item, f"{path}[{index}]"))
    return found


def validate_complete_recovery_play_set(
    play_set: CompleteRecoveryPlaySetV1,
    packet: GroundedStrategyEvidencePacketV1,
) -> None:
    allowed = set(packet.allowed_evidence_ids)
    evidence = {
        item.evidence_id: _evidence_text(item.category, str(item.value)) for item in packet.evidence
    }
    ledger_minor: set[tuple[str, int]] = set()
    for item in packet.evidence:
        if item.category != "map_value":
            continue
        match = _MINOR_UNIT_RE.match(str(item.value))
        if match is None:
            continue
        amount, code = match.groups()
        try:
            currency = normalize_currency(code)
        except (KeyError, ValueError):
            continue
        if currency in CURRENCY_EXPONENTS:
            ledger_minor.add((currency, int(amount)))

    errors: list[str] = []
    for path, copy in _walk_grounded(play_set):
        if any(ref not in allowed for ref in copy.evidence_ids):
            errors.append(f"{path}.evidence_ids")
            continue
        prefixes = _KIND_PREFIXES.get(copy.kind)
        if prefixes and not any(ref.startswith(prefixes) for ref in copy.evidence_ids):
            errors.append(f"{path}.evidence_ids")
        referenced_values = [evidence[ref] for ref in copy.evidence_ids]
        if _has_unsupported_numeric_claim(copy.text, referenced_values):
            errors.append(f"{path}.text")
    ledger_minor_amounts = {amount for _, amount in ledger_minor}
    for path, customer in _walk_customer(play_set):
        for claim in _numeric_claims(customer.text):
            if not claim.kind.startswith("currency:"):
                continue
            code = claim.kind.split(":", 1)[1]
            exponent = CURRENCY_EXPONENTS.get(code)
            if exponent is None:
                continue
            minor = int(claim.value * Decimal(10) ** exponent)
            if (code, minor) in ledger_minor:
                errors.append(f"{path}.text:ledger_money_in_customer_copy")
        # Bare integers are only treated as ledger money when they resolve to the
        # packet's own minor-unit amount. "$49.00" and "4900 usd" fail through the
        # currency extraction above; a bare "4900" fails here. "in 3 weeks" and
        # "30 percent off" pass because their figures are not ledger amounts.
        for match in re.finditer(r"(?<!\d)(\d[\d,]*)(?!\d)", customer.text):
            if int(match.group(1).replace(",", "")) in ledger_minor_amounts:
                errors.append(f"{path}.text:ledger_money_in_customer_copy")

    for play_index, play in enumerate(play_set.plays):
        if no_email_play_for(play.pile_id) or pile_prohibits_email(play.pile_id):
            errors.append(f"$.plays[{play_index}]:no_email_play_permitted")
        if len(play.email_sequence) > touch_ceiling_for(play.pile_id):
            errors.append(f"$.plays[{play_index}].email_sequence:sequence_ceiling_exceeded")
        if play.sequence_plan.reengage_days != reengage_days_for(play.pile_id):
            errors.append(f"$.plays[{play_index}].sequence_plan:reengage_days_mismatch")
        first = play.email_sequence[0]
        for rule in first_touch_violations(
            subject=first.subject.text, body=first.body.text, cta=first.cta.text
        ):
            errors.append(f"$.plays[{play_index}].email_sequence[0]:{rule}")
        for step_index, step in enumerate(play.email_sequence):
            joined = " ".join([step.subject.text, step.body.text, step.cta.text])
            for rule in pile_prohibition_violations(play.pile_id, joined):
                errors.append(f"$.plays[{play_index}].email_sequence[{step_index}]:{rule}")
    for play_index, play in enumerate(play_set.plays):
        if any(ref not in allowed for ref in play.evidence_references):
            errors.append(f"$.plays[{play_index}].evidence_references")
        urgency_path = f"$.plays[{play_index}].urgency"
        if play.urgency.text != "none":
            if any(ref not in allowed for ref in play.urgency.evidence_ids) or not any(
                ref.startswith("ev_business_capacity") for ref in play.urgency.evidence_ids
            ):
                errors.append(f"{urgency_path}.evidence_ids")
            else:
                referenced_values = [evidence[ref] for ref in play.urgency.evidence_ids]
                if _has_unsupported_numeric_claim(play.urgency.text, referenced_values):
                    errors.append(f"{urgency_path}.text")
    # FM-057: schema facts are enforced here rather than handed to a blind
    # reviewer, which is what produced whole-field reversals between rounds.
    errors.extend(card_violations(play_set))
    errors.extend(offer_ladder_set_violations(play_set))
    if errors:
        raise CompleteStrategyGroundingError(sorted(set(errors)))


def _normalized(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.casefold()))


def _materially_different(left: str, right: str) -> bool:
    left_tokens = set(_normalized(left).split())
    right_tokens = set(_normalized(right).split())
    if not left_tokens or not right_tokens:
        return False
    similarity = len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
    return similarity < 0.75


def _axis_value(play: CompleteRecoveryPlayV1, axis: str) -> str:
    if axis == "offer_mechanism":
        core = next(rung for rung in play.offer_ladder.rungs if rung.role == "core")
        return core.mechanism.text
    return getattr(play, axis).text


def build_differentiation_report(
    play_set: CompleteRecoveryPlaySetV1,
) -> DifferentiationReportV1:
    checks: list[DifferentiationCheckV1] = []
    findings: list[str] = []
    for left, right in combinations(sorted(play_set.plays, key=lambda play: play.rank), 2):
        for axis in _AXES:
            left_value = _normalized(_axis_value(left, axis))
            right_value = _normalized(_axis_value(right, axis))
            passed = left_value != right_value and _materially_different(left_value, right_value)
            finding = None if passed else f"{left.play_id} and {right.play_id} collide on {axis}"
            if finding:
                findings.append(finding)
            checks.append(
                DifferentiationCheckV1(
                    left_play_id=left.play_id,
                    right_play_id=right.play_id,
                    axis=cast(Any, axis),
                    passed=passed,
                    finding=finding,
                )
            )
    return DifferentiationReportV1(
        recovery_plays_sha256=_sha(play_set.to_canonical_json()),
        checks=checks,
        passed=not findings,
        actionable_findings=findings,
    )


def canonical_saas_business_profile() -> StrategyBusinessProfileV1:
    return StrategyBusinessProfileV1(
        product="Annual subscription",
        proof="Three approved case studies",
        margin="40 percent contribution margin",
        channel="email and sms with a human review task",
        capacity="100 recovery reviews per week",
        destination="billing portal",
    )


def build_canonical_saas_recovery_strategy(
    money_map: MoneyMapV1,
    *,
    built_at: datetime | None = None,
) -> CanonicalSaasStrategyRun:
    when = built_at or datetime(2026, 8, 10, 13, 0, tzinfo=timezone.utc)
    packet = build_grounded_strategy_packet(
        money_map, canonical_saas_business_profile(), built_at=when
    )
    plays = _canonical_plays(money_map.run_id, when)
    validate_complete_recovery_play_set(plays, packet)
    report = build_differentiation_report(plays)
    receipt = StrategyAuditReceiptV1(
        run_id=money_map.run_id,
        status="completed",
        prompt_version="found-money-three-play.v1",
        prompt_hash=_sha(b"deterministic three-play SaaS fixture from frozen evidence"),
        output_schema_version="recovery-plays.v1",
        output_schema_hash=_sha(b"complete recovery play and concept card contract v1"),
        configured_model_id="fixture-strategy-v1",
        returned_model_id="fixture-strategy-v1",
        response_id="fixture-three-play-response-v1",
        token_usage=StrategyTokenUsageV1(input_tokens=0, output_tokens=0),
        attempt_count=1,
        evidence_packet_hash=_sha(packet.to_canonical_json()),
        output_hash=_sha(plays.to_canonical_json()),
    )
    return CanonicalSaasStrategyRun(packet, plays, receipt, report)


def canonical_ecommerce_business_profile() -> StrategyBusinessProfileV1:
    return StrategyBusinessProfileV1(
        product="Reorder catalog",
        proof="Four catalog return studies",
        margin="35 percent contribution margin",
        channel="email and sms with a human review task",
        capacity="80 recovery reviews per week",
        destination="account reorder page",
    )


def _ecommerce_card(
    play_rank: int,
    card_rank: int,
    *,
    name: str,
    tension: str,
    idea: str,
    hook: str,
    visual: str,
    proof: str,
    style: str,
    cta: str,
    fit: str,
    proof_extra_refs: tuple[str, ...] = (),
) -> dict[str, Any]:
    payload = _card(
        play_rank,
        card_rank,
        name=name,
        tension=tension,
        idea=idea,
        hook=hook,
        visual=visual,
        proof=proof,
        style=style,
        cta=cta,
        fit=fit,
        proof_extra_refs=proof_extra_refs,
    )
    payload["pile_id"] = "disappeared_high_value_customer"
    return payload


def _ladder_kwargs(
    *,
    product_label: str,
    mechanism: str,
    cta: str,
    core_subject: str,
    core_body: str,
) -> dict[str, Any]:
    label = product_label
    return {
        "anchor_mechanism": f"{label} with full onboarding and a twelve-month commitment at the standard rate",
        "downsell_mechanism": (
            f"Self-serve {label.lower()} access with no guided support or onboarding program"
        ),
        "ladder_outcome": (
            f"Restore the {label} relationship without conceding an unsupported discount"
        ),
        "ladder_ordering_basis": (
            "The anchor establishes fullest scope, the core is the recommended path, "
            "and the downsell is the save path with less scope"
        ),
        "rung_email_copy": (
            (
                f"full {label.lower()} onboarding?",
                f"The premium {label} path includes full onboarding if you want the complete program.",
                cta,
            ),
            (core_subject, core_body, cta),
            (
                "quick self-serve path",
                "The self-serve path is available if you only need to update details without onboarding.",
                cta,
            ),
        ),
    }


def _ecommerce_play_payload(
    rank: int,
    *,
    play_id: str,
    campaign: str,
    strategy: str,
    diagnosis: str,
    offer_recommendation: str,
    mechanism: str,
    anchor_mechanism: str,
    downsell_mechanism: str,
    ladder_outcome: str,
    ladder_ordering_basis: str,
    rung_email_copy: tuple[tuple[str, str, str], tuple[str, str, str], tuple[str, str, str]],
    sequence: str,
    cta: str,
    channel: str,
    big_idea: str,
    email_steps: tuple[tuple[str, str, str, int], ...],
    sms_messages: tuple[str, ...],
    calendar_steps: tuple[tuple[int, str], ...],
    task_track: str,
    cards: list[dict[str, Any]],
    real_urgency: bool,
) -> dict[str, Any]:
    product = "ev_business_product"
    proof = "ev_business_proof"
    margin = "ev_business_margin"
    capacity = "ev_business_capacity"
    destination = "ev_business_destination"
    amount = "ev_pile_1_value"
    basis = "ev_pile_1_basis"
    payload: dict[str, Any] = {
        "play_id": play_id,
        "pile_id": "disappeared_high_value_customer",
        "rank": rank,
        "title": campaign,
        "rationale": diagnosis,
        "offer_recommendation": offer_recommendation,
        "recommended_actions": [
            "Review the aggregate reorder catalog evidence before approval",
            "Approve or withhold the complete sequence without sending it",
        ],
        "campaign_name": campaign,
        "campaign_strategy": _copy(strategy, product, amount, kind="product"),
        "value_basis": _copy(
            "Use the observed face value basis without treating it as recovered revenue",
            basis,
        ),
        "audience": _copy(
            "Quiet high-value catalog buyers with an observed $185.00 reorder opportunity",
            product,
            amount,
            kind="product",
        ),
        "recoverability": _copy(
            "The observed $185.00 catalog opportunity remains reviewable",
            amount,
            kind="number",
        ),
        "diagnosis": _copy(diagnosis, product, amount, capacity, kind="product"),
    }
    diagnosis_copy = payload["diagnosis"]
    ladder_id = f"{play_id}-ladder"
    ladder, rung_packages = _ladder_for_play(
        ladder_id,
        play_id,
        diagnosis_copy=diagnosis_copy,
        product=product,
        margin=margin,
        proof=proof,
        anchor_mechanism=anchor_mechanism,
        core_mechanism=mechanism,
        downsell_mechanism=downsell_mechanism,
        outcome_text=ladder_outcome,
        ordering_basis_text=ladder_ordering_basis,
        rung_email_copy=rung_email_copy,
    )
    payload["offer_ladder"] = ladder
    payload["rung_copy_packages"] = rung_packages
    payload.update(
        {
            "lifecycle_sequence": _copy(sequence, product, kind="product"),
            "email_sequence": [
                {
                    "order": index,
                    "lifecycle_stage": _copy(stage, product, kind="product"),
                    "subject": _ccopy(subject, kind="product"),
                    "body": _ccopy(body, kind="general"),
                    "cta": _ccopy(cta, kind="destination"),
                    "wait_days": wait,
                }
                for index, (stage, subject, body, wait) in enumerate(email_steps, start=1)
            ],
            "sms": {
                "available": True,
                "messages": [_ccopy(message, kind="destination") for message in sms_messages],
            },
            "task_talk_track": _copy(
                task_track, product, destination, capacity, kind="destination"
            ),
            "primary_cta": _copy(cta, destination, kind="destination"),
            "objections": [
                {
                    "objection": _copy(
                        "The Reorder catalog may no longer match current needs",
                        product,
                        kind="objection",
                    ),
                    "response": _copy(
                        "Use the Four catalog return studies as approved proof without adding claims",
                        proof,
                        kind="proof",
                    ),
                },
                {
                    "objection": _copy(
                        "The Reorder catalog timing may feel inconvenient",
                        product,
                        kind="objection",
                    ),
                    "response": _copy(
                        "Offer the account reorder page as the approved destination without inventing terms",
                        destination,
                        kind="destination",
                    ),
                },
            ],
            "urgency": (
                {
                    "text": "Respect the 80 recovery reviews per week capacity",
                    "evidence_ids": [capacity],
                }
                if real_urgency
                else {"text": "none", "evidence_ids": []}
            ),
            "calendar": [
                {
                    "day": day,
                    "action": _copy(action, product, kind="product"),
                    "stop_condition": _copy(
                        "Stop when the Reorder catalog state changes or approval is withdrawn",
                        product,
                        kind="product",
                    ),
                }
                for day, action in calendar_steps
            ],
            "stop_conditions": [
                _copy("Stop after a Reorder catalog state change", product, kind="product"),
                _copy(
                    "Stop when approval for the Reorder catalog is withdrawn",
                    product,
                    kind="product",
                ),
            ],
            "tracking": {
                "success_event": _copy(
                    "Record an observed Reorder catalog state change without claiming recovery",
                    product,
                    kind="product",
                ),
                "tracked_signals": [
                    _copy(
                        "Track aggregate account reorder page review completion",
                        destination,
                        kind="destination",
                    ),
                    _copy("Track observed face value status separately", basis, kind="general"),
                ],
            },
            "channel_emphasis": _copy(channel, "ev_business_channel", kind="general"),
            "creative_big_idea": _copy(big_idea, product, kind="product"),
            "concept_cards": cards,
        }
    )
    payload["evidence_references"] = sorted(set(_all_refs(payload)))
    return payload


def _canonical_ecommerce_plays(run_id: str, built_at: datetime) -> CompleteRecoveryPlaySetV1:
    plays = [
        _ecommerce_play_payload(
            1,
            play_id="vip-silence-reopen",
            campaign="Reopen the Quiet Lane",
            strategy="Reopen the Reorder catalog path for a quiet high-value buyer before discussing new offers",
            diagnosis="A quiet high-value catalog relationship is stalled around an $185.00 observed opportunity",
            offer_recommendation="Against the $185.00 pile I would reopen the reorder lane with the account page and no discount, because the evidence says silence rather than price stalled the relationship. If the quiet cohort turns out to be price-sensitive, add your own incentive at approval time; the contract does not invent one.",
            mechanism="A guided reorder-lane return for the Reorder catalog with no invented discount",
            **_ladder_kwargs(
                product_label="Reorder catalog",
                mechanism="A guided reorder-lane return for the Reorder catalog with no invented discount",
                cta="Review the Reorder catalog details in the account reorder page",
                core_subject="running low?",
                core_body="Are you still going through the Reorder catalog items?",
            ),
            sequence="Name the silence, reopen the catalog lane, then confirm a human-reviewed close",
            cta="Review the Reorder catalog details in the account reorder page",
            channel="Lead with email instructions, reinforce with concise SMS, and reserve tasks for unresolved reviews",
            big_idea="The Reorder catalog is paused by silence rather than lost intent",
            email_steps=(
                (
                    "Recognition",
                    "running low?",
                    "Are you still going through the Reorder catalog items?",
                    0,
                ),
                (
                    "Lane return",
                    "the reorder page",
                    "The account reorder page still has your Reorder catalog items if that is simpler than starting over.",
                    1,
                ),
                (
                    "Closure decision",
                    "Closing the Reorder catalog review",
                    "Email three: ask for an approval-only close of the Reorder catalog review",
                    3,
                ),
            ),
            sms_messages=(
                "Send one SMS after email one pointing the Reorder catalog review to the account reorder page",
            ),
            calendar_steps=(
                (0, "Send the Reorder catalog recognition email"),
                (1, "Send the Reorder catalog lane-return email"),
                (3, "Send the Reorder catalog closure email"),
            ),
            task_track="Open a human review task only if the Reorder catalog account reorder page update stays unresolved after the email sequence",
            cards=[
                _ecommerce_card(
                    1,
                    1,
                    name="Six In The Morning",
                    tension="You only notice you are out of it when it is already too late to fix",
                    idea="Put the exact moment of running out on screen instead of the reorder prompt",
                    hook="Why do I only notice we are out at six in the morning",
                    visual="A hand reaching into an empty box on a shelf, early light, no styling",
                    proof="A before-and-after catalog-state comparison that uses Four catalog return studies as the only proof source",
                    style="First-frame screenshot static, phone-shot, no brand chrome",
                    cta="Open the account reorder page for the Reorder catalog review",
                    fit="Fits disappeared_high_value_customer by catalog-lane retry of the quiet Reorder catalog for observed_face_value $185.00",
                ),
                _ecommerce_card(
                    1,
                    2,
                    name="Three Lists",
                    tension="Writing it down again is easier than actually ordering it",
                    idea="Show the note-taking as the avoidance behaviour it actually is",
                    hook="Wrote it on three lists and ordered it zero times",
                    visual="Three handwritten notes on a counter with the same item on each",
                    proof="An on-screen route marker that displays Four catalog return studies as the only proof",
                    style="Social comment screenshot, native thread crop, aggregate labels only",
                    cta="Continue the Reorder catalog review in the account reorder page",
                    fit="Fits disappeared_high_value_customer by reorder-lane retry of the quiet Reorder catalog for observed_face_value $185.00",
                ),
                _ecommerce_card(
                    1,
                    3,
                    name="Nothing Ran Out",
                    tension="A week where nothing ran out is a week you never think about",
                    idea="Lead with the calm week, not the mechanism that produced it",
                    hook="Nobody texted me about stock for six weeks and we had been out for five",
                    visual="A full shelf, nobody in frame, nothing happening",
                    proof="A completed-step stamp that displays Four catalog return studies as the only proof",
                    style="Founder letter, text-only static, no design treatment",
                    cta="Complete the Reorder catalog review in the account reorder page",
                    fit="Fits disappeared_high_value_customer as the last catalog retry of the quiet Reorder catalog for observed_face_value $185.00",
                ),
            ],
            real_urgency=False,
        ),
        _ecommerce_play_payload(
            2,
            play_id="vip-catalog-proof",
            campaign="Remember the Shelf",
            strategy="Re-establish the Reorder catalog value before requesting a next-step decision",
            diagnosis="The quiet Reorder catalog pause may reflect faded catalog memory around an $185.00 observed opportunity",
            offer_recommendation="For the $185.00 pile, proof-first beats discount-first: the buyer already knew what they were buying, so the mechanism should remind rather than convince. If approved return studies are thin, swap to the reopen-lane play and keep the sequence.",
            mechanism="A proof-led catalog reset for the Reorder catalog using approved return studies",
            **_ladder_kwargs(
                product_label="Reorder catalog",
                mechanism="A proof-led catalog reset for the Reorder catalog using approved return studies",
                cta="Review the Reorder catalog proof and choose the next step in the account reorder page",
                core_subject="quick question",
                core_body="Did something change with what you were reordering?",
            ),
            sequence="Revisit the outcome, present approved catalog proof, then invite a Reorder catalog decision",
            cta="Review the Reorder catalog proof and choose the next step in the account reorder page",
            channel="Lead with proof-rich email, use SMS only as a reminder, and assign a contextual follow-up task",
            big_idea="Reconnect the Reorder catalog decision to the outcome it was meant to create",
            email_steps=(
                (
                    "Outcome recall",
                    "quick question",
                    "Did something change with what you were reordering?",
                    0,
                ),
                (
                    "Proof stack",
                    "what others reordered",
                    "Four catalog return studies cover the same Reorder catalog gap on the account reorder page.",
                    3,
                ),
                (
                    "Decision invite",
                    "Choose the Reorder catalog next step",
                    "Email three: invite an account reorder page decision after the proof stack",
                    7,
                ),
            ),
            sms_messages=(
                "Send one SMS reminder only after the proof email, not as a parallel Reorder catalog sequence",
            ),
            calendar_steps=(
                (0, "Send the Reorder catalog outcome-recall email"),
                (3, "Send the Reorder catalog proof-stack email"),
                (7, "Send the Reorder catalog decision-invite email"),
            ),
            task_track="Assign a contextual follow-up task after the proof stack so a human can review the Reorder catalog decision in the account reorder page",
            cards=[
                _ecommerce_card(
                    2,
                    1,
                    name="Rush Shipping",
                    tension="Paying express fees on something you buy on the same schedule every month",
                    idea="Make the premium you keep paying for lateness the whole argument",
                    hook="POV you are paying rush shipping on something you buy every month",
                    visual="A receipt with the expedited line item circled",
                    proof="A reflected-outcome panel built from Four catalog return studies as the only proof surface",
                    style="Tweet-style static, native post crop, aggregate labels only",
                    cta="Review Reorder catalog proof in the account reorder page",
                    fit="Fits disappeared_high_value_customer by catalog-state update of the quiet Reorder catalog for observed_face_value $185.00",
                ),
                _ecommerce_card(
                    2,
                    2,
                    name="Two Apps",
                    tension="You tried to fix it with tools before admitting it was a habit",
                    idea="Walk through the failed attempts before naming what actually worked",
                    hook="We tried two reminder apps before admitting the problem was us",
                    visual="A dense block of plain text listing what was tried and dropped",
                    proof="Four catalog return studies shown as the approved proof tiles",
                    style="Wall of text static, high contrast, no imagery, aggregate labels only",
                    cta="Choose the Reorder catalog next step in the account reorder page",
                    fit="Fits disappeared_high_value_customer by proof-led catalog retry of the quiet Reorder catalog for observed_face_value $185.00",
                ),
                _ecommerce_card(
                    2,
                    3,
                    name="One List",
                    tension="The fix turns out to be smaller than the effort spent avoiding it",
                    idea="Reduce the whole thing to the single mechanism that ends the problem",
                    hook="One list fixed the thing I kept forgetting",
                    visual="One list on a phone screen, thumb about to tap it",
                    proof="A then-and-next panel that displays Four catalog return studies as the only proof",
                    style="Us versus them static, before and after split, aggregate labels only",
                    cta="Continue the Reorder catalog review in the account reorder page",
                    fit="Fits disappeared_high_value_customer by intent-bridge retry of the quiet Reorder catalog for observed_face_value $185.00",
                ),
            ],
            real_urgency=False,
        ),
        _ecommerce_play_payload(
            3,
            play_id="vip-capacity-lane",
            campaign="Hold the Review Window",
            strategy="Bound the Reorder catalog restart inside a finite human review window",
            diagnosis="The $185.00 Reorder catalog opportunity needs a capacity-bounded human review rather than an open-ended chase",
            offer_recommendation="I would cap this $185.00 lane with a bounded human review, because the win here is a completed decision rather than a sale. If your team cannot genuinely staff the window, the capacity claim collapses, so run the proof play instead.",
            mechanism="A capacity-windowed human review for the Reorder catalog with no invented urgency discount",
            **_ladder_kwargs(
                product_label="Reorder catalog",
                mechanism="A capacity-windowed human review for the Reorder catalog with no invented urgency discount",
                cta="Enter the Reorder catalog restart lane through the account reorder page",
                core_subject="still need these?",
                core_body="Have you given up on restocking the Reorder catalog?",
            ),
            sequence="Open a bounded window, staff the human review, then close the Reorder catalog lane",
            cta="Enter the Reorder catalog restart lane through the account reorder page",
            channel="Lead with a capacity-aware email, keep SMS brief, and use a human task as the control gate",
            big_idea="A defined review window can focus the Reorder catalog restart",
            email_steps=(
                (
                    "Window open",
                    "still need these?",
                    "Have you given up on restocking the Reorder catalog?",
                    0,
                ),
                (
                    "Staffed review",
                    "someone can look",
                    "A person can go through the Reorder catalog with you on the account reorder page rather than an automated pass.",
                    2,
                ),
                (
                    "Window close",
                    "Closing the Reorder catalog window",
                    "Email three: close the Reorder catalog window after the staffed review",
                    5,
                ),
            ),
            sms_messages=(
                "Send one SMS only after the window-open email pointing the Reorder catalog review to the account reorder page",
            ),
            calendar_steps=(
                (0, "Send the Reorder catalog window-open email"),
                (2, "Send the Reorder catalog staffed-review email"),
                (5, "Send the Reorder catalog window-close email"),
            ),
            task_track="Keep a human review task as the control gate for the Reorder catalog restart inside the 80 recovery reviews per week capacity",
            cards=[
                _ecommerce_card(
                    3,
                    1,
                    name="Monday Stock Check",
                    tension="The count happens on a fixed day whether you are ready for it or not",
                    idea="Put the fixed deadline and the empty shelf in the same frame",
                    hook="Stock check is Monday and the shelf is already empty",
                    visual="A clipboard count sheet against a visibly empty rack",
                    proof="A window marker that displays Four catalog return studies as the only proof",
                    style="Founder customer call, split screen, phone audio, unscripted",
                    cta="Enter the Reorder catalog window through the account reorder page",
                    fit="Fits disappeared_high_value_customer by capacity-bounded catalog review of the quiet Reorder catalog for observed_face_value $185.00",
                    proof_extra_refs=("ev_business_capacity",),
                ),
                _ecommerce_card(
                    3,
                    2,
                    name="Forgot Again",
                    tension="Everyone thinks they are the only one who keeps forgetting",
                    idea="Use other customers own words to make the habit feel ordinary",
                    hook="I forgot again, said every month, by everyone",
                    visual="A stack of near-identical messages saying the same thing",
                    proof="A gated-lane stamp that displays Four catalog return studies as the only proof",
                    style="Use-case expansion, three short scenes, aggregate labels only",
                    cta="Hold the Reorder catalog review in the account reorder page",
                    fit="Fits disappeared_high_value_customer by human-gated catalog review of the quiet Reorder catalog for observed_face_value $185.00",
                    proof_extra_refs=("ev_business_capacity",),
                ),
                _ecommerce_card(
                    3,
                    3,
                    name="Before You Notice",
                    tension="Ordering when you notice is always later than ordering before you notice",
                    idea="Contrast the two orders of operation and let the reader place themselves",
                    hook="A customer told me we were out and nobody inside had noticed",
                    visual="One line of text over a flat field, nothing else in frame",
                    proof="Three on-screen lane markers, each explicitly labeled from Four catalog return studies as the only proof",
                    style="Headline static, one line, aggregate labels only",
                    cta="Enter the Reorder catalog restart lane through the account reorder page",
                    fit="Fits disappeared_high_value_customer by human review into catalog retry of the quiet Reorder catalog for observed_face_value $185.00",
                    proof_extra_refs=("ev_business_capacity",),
                ),
            ],
            real_urgency=True,
        ),
    ]
    return CompleteRecoveryPlaySetV1(
        run_id=run_id,
        built_at=built_at,
        provider="fixture",
        plays=[CompleteRecoveryPlayV1.model_validate(_apply_pile_plan(play)) for play in plays],
    )


def build_canonical_ecommerce_recovery_strategy(
    money_map: MoneyMapV1,
    *,
    built_at: datetime | None = None,
) -> CanonicalSaasStrategyRun:
    when = built_at or datetime(2026, 8, 10, 13, 0, tzinfo=timezone.utc)
    packet = build_grounded_strategy_packet(
        money_map, canonical_ecommerce_business_profile(), built_at=when
    )
    plays = _canonical_ecommerce_plays(money_map.run_id, when)
    validate_complete_recovery_play_set(plays, packet)
    report = build_differentiation_report(plays)
    receipt = StrategyAuditReceiptV1(
        run_id=money_map.run_id,
        status="completed",
        prompt_version="found-money-three-play.v1",
        prompt_hash=_sha(b"deterministic three-play ecommerce fixture from frozen evidence"),
        output_schema_version="recovery-plays.v1",
        output_schema_hash=_sha(b"complete recovery play and concept card contract v1"),
        configured_model_id="fixture-strategy-v1",
        returned_model_id="fixture-strategy-v1",
        response_id="fixture-three-play-ecommerce-response-v1",
        token_usage=StrategyTokenUsageV1(input_tokens=0, output_tokens=0),
        attempt_count=1,
        evidence_packet_hash=_sha(packet.to_canonical_json()),
        output_hash=_sha(plays.to_canonical_json()),
    )
    return CanonicalSaasStrategyRun(packet, plays, receipt, report)


def canonical_service_business_profile() -> StrategyBusinessProfileV1:
    return StrategyBusinessProfileV1(
        product="Membership studio",
        proof="Three return-visit studies",
        margin="45 percent contribution margin",
        channel="email and sms with a human review task",
        capacity="60 recovery reviews per week",
        destination="membership booking page",
    )


def _service_card(
    play_rank: int,
    card_rank: int,
    *,
    name: str,
    tension: str,
    idea: str,
    hook: str,
    visual: str,
    proof: str,
    style: str,
    cta: str,
    fit: str,
    proof_extra_refs: tuple[str, ...] = (),
) -> dict[str, Any]:
    payload = _card(
        play_rank,
        card_rank,
        name=name,
        tension=tension,
        idea=idea,
        hook=hook,
        visual=visual,
        proof=proof,
        style=style,
        cta=cta,
        fit=fit,
        proof_extra_refs=proof_extra_refs,
    )
    payload["pile_id"] = "disappeared_high_value_customer"
    return payload


def _service_play_payload(
    rank: int,
    *,
    play_id: str,
    campaign: str,
    strategy: str,
    diagnosis: str,
    offer_recommendation: str,
    mechanism: str,
    anchor_mechanism: str,
    downsell_mechanism: str,
    ladder_outcome: str,
    ladder_ordering_basis: str,
    rung_email_copy: tuple[tuple[str, str, str], tuple[str, str, str], tuple[str, str, str]],
    sequence: str,
    cta: str,
    channel: str,
    big_idea: str,
    email_steps: tuple[tuple[str, str, str, int], ...],
    sms_messages: tuple[str, ...],
    calendar_steps: tuple[tuple[int, str], ...],
    task_track: str,
    cards: list[dict[str, Any]],
    real_urgency: bool,
) -> dict[str, Any]:
    product = "ev_business_product"
    proof = "ev_business_proof"
    margin = "ev_business_margin"
    capacity = "ev_business_capacity"
    destination = "ev_business_destination"
    amount = "ev_pile_1_value"
    basis = "ev_pile_1_basis"
    payload: dict[str, Any] = {
        "play_id": play_id,
        "pile_id": "disappeared_high_value_customer",
        "rank": rank,
        "title": campaign,
        "rationale": diagnosis,
        "offer_recommendation": offer_recommendation,
        "recommended_actions": [
            "Review the aggregate membership studio evidence before approval",
            "Approve or withhold the complete sequence without sending it",
        ],
        "campaign_name": campaign,
        "campaign_strategy": _copy(strategy, product, amount, kind="product"),
        "value_basis": _copy(
            "Use the observed face value basis without treating it as recovered revenue",
            basis,
        ),
        "audience": _copy(
            "Quiet high-value Membership studio members with an observed $220.00 return opportunity",
            product,
            amount,
            kind="product",
        ),
        "recoverability": _copy(
            "The observed $220.00 membership opportunity remains reviewable",
            amount,
            kind="number",
        ),
        "diagnosis": _copy(diagnosis, product, amount, capacity, kind="product"),
    }
    diagnosis_copy = payload["diagnosis"]
    ladder_id = f"{play_id}-ladder"
    ladder, rung_packages = _ladder_for_play(
        ladder_id,
        play_id,
        diagnosis_copy=diagnosis_copy,
        product=product,
        margin=margin,
        proof=proof,
        anchor_mechanism=anchor_mechanism,
        core_mechanism=mechanism,
        downsell_mechanism=downsell_mechanism,
        outcome_text=ladder_outcome,
        ordering_basis_text=ladder_ordering_basis,
        rung_email_copy=rung_email_copy,
    )
    payload["offer_ladder"] = ladder
    payload["rung_copy_packages"] = rung_packages
    payload.update(
        {
            "lifecycle_sequence": _copy(sequence, product, kind="product"),
            "email_sequence": [
                {
                    "order": index,
                    "lifecycle_stage": _copy(stage, product, kind="product"),
                    "subject": _ccopy(subject, kind="product"),
                    "body": _ccopy(body, kind="general"),
                    "cta": _ccopy(cta, kind="destination"),
                    "wait_days": wait,
                }
                for index, (stage, subject, body, wait) in enumerate(email_steps, start=1)
            ],
            "sms": {
                "available": True,
                "messages": [_ccopy(message, kind="destination") for message in sms_messages],
            },
            "task_talk_track": _copy(
                task_track, product, destination, capacity, kind="destination"
            ),
            "primary_cta": _copy(cta, destination, kind="destination"),
            "objections": [
                {
                    "objection": _copy(
                        "The Membership studio may no longer match current needs",
                        product,
                        kind="objection",
                    ),
                    "response": _copy(
                        "Use the Three return-visit studies as approved proof without adding claims",
                        proof,
                        kind="proof",
                    ),
                },
                {
                    "objection": _copy(
                        "The Membership studio timing may feel inconvenient",
                        product,
                        kind="objection",
                    ),
                    "response": _copy(
                        "Offer the membership booking page as the approved destination without inventing terms",
                        destination,
                        kind="destination",
                    ),
                },
            ],
            "urgency": (
                {
                    "text": "Respect the 60 recovery reviews per week capacity",
                    "evidence_ids": [capacity],
                }
                if real_urgency
                else {"text": "none", "evidence_ids": []}
            ),
            "calendar": [
                {
                    "day": day,
                    "action": _copy(action, product, kind="product"),
                    "stop_condition": _copy(
                        "Stop when the Membership studio state changes or approval is withdrawn",
                        product,
                        kind="product",
                    ),
                }
                for day, action in calendar_steps
            ],
            "stop_conditions": [
                _copy("Stop after a Membership studio state change", product, kind="product"),
                _copy(
                    "Stop when approval for the Membership studio is withdrawn",
                    product,
                    kind="product",
                ),
            ],
            "tracking": {
                "success_event": _copy(
                    "Record an observed Membership studio state change without claiming recovery",
                    product,
                    kind="product",
                ),
                "tracked_signals": [
                    _copy(
                        "Track aggregate membership booking page review completion",
                        destination,
                        kind="destination",
                    ),
                    _copy("Track observed face value status separately", basis, kind="general"),
                ],
            },
            "channel_emphasis": _copy(channel, "ev_business_channel", kind="general"),
            "creative_big_idea": _copy(big_idea, product, kind="product"),
            "concept_cards": cards,
        }
    )
    payload["evidence_references"] = sorted(set(_all_refs(payload)))
    return payload


def _canonical_service_plays(run_id: str, built_at: datetime) -> CompleteRecoveryPlaySetV1:
    plays = [
        _service_play_payload(
            1,
            play_id="membership-silence-reopen",
            campaign="Reopen the Quiet Lane",
            strategy="Reopen the Membership studio path for a quiet high-value member before discussing new visits",
            diagnosis="A quiet high-value membership relationship is stalled around a $220.00 observed opportunity",
            offer_recommendation="Against the $220.00 pile, a guided membership-lane return asks the member to do nothing new, which fits a relationship that went quiet on its own. If the studio prefers an incentive-led return, decide that before approval; the contract will not invent one.",
            mechanism="A guided membership-lane return for the Membership studio with no invented discount",
            **_ladder_kwargs(
                product_label="Membership studio",
                mechanism="A guided membership-lane return for the Membership studio with no invented discount",
                cta="Review the Membership studio details in the membership booking page",
                core_subject="still coming in?",
                core_body="Are you still trying to get back to the Membership studio?",
            ),
            sequence="Name the silence, reopen the membership lane, then confirm a human-reviewed close",
            cta="Review the Membership studio details in the membership booking page",
            channel="Lead with email instructions, reinforce with concise SMS, and reserve tasks for unresolved reviews",
            big_idea="The Membership studio is paused by silence rather than lost intent",
            email_steps=(
                (
                    "Recognition",
                    "still coming in?",
                    "Are you still trying to get back to the Membership studio?",
                    0,
                ),
                (
                    "Lane return",
                    "the booking page",
                    "The membership booking page has open Membership studio times if that helps more than a reply.",
                    1,
                ),
                (
                    "Closure decision",
                    "Closing the Membership studio review",
                    "Email three: ask for an approval-only close of the Membership studio review",
                    3,
                ),
            ),
            sms_messages=(
                "Send one SMS after email one pointing the Membership studio review to the membership booking page",
            ),
            calendar_steps=(
                (0, "Send the Membership studio recognition email"),
                (1, "Send the Membership studio lane-return email"),
                (3, "Send the Membership studio closure email"),
            ),
            task_track="Open a human review task only if the Membership studio membership booking page update stays unresolved after the email sequence",
            cards=[
                _service_card(
                    1,
                    1,
                    name="Four Months",
                    tension="Paying every month for a place you have not walked into since winter",
                    idea="Let the arithmetic of the unused months carry the whole confession",
                    hook="Four months paid and zero times through the door",
                    visual="A membership card on a kitchen counter beside a stack of unopened post",
                    proof="A before-and-after membership-state comparison that uses Three return-visit studies as the only proof source",
                    style="First-frame screenshot static, phone-shot, no brand chrome",
                    cta="Open the membership booking page for the Membership studio review",
                    fit="Fits disappeared_high_value_customer by membership-lane retry of the quiet Membership studio for observed_face_value $220.00",
                ),
                _service_card(
                    1,
                    2,
                    name="Drive Past",
                    tension="Looking the other way is easier than actually cancelling it",
                    idea="Show the avoidance rather than the absence",
                    hook="POV you drive past it and look the other way",
                    visual="A windscreen shot passing a lit building at dusk, no signage readable",
                    proof="An on-screen route marker that displays Three return-visit studies as the only proof",
                    style="Social comment screenshot, native thread crop, aggregate labels only",
                    cta="Continue the Membership studio review in the membership booking page",
                    fit="Fits disappeared_high_value_customer by visit-lane retry of the quiet Membership studio for observed_face_value $220.00",
                ),
                _service_card(
                    1,
                    3,
                    name="First Week",
                    tension="What stops you is not motivation, it is knowing the first week hurts",
                    idea="Name the real obstacle instead of implying missing willpower",
                    hook="I want to go back without the first week hurting",
                    visual="An empty changing room bench with a kit bag still zipped shut",
                    proof="A single-visit marker that cites Three return-visit studies only",
                    style="Founder letter, text-only static, no design treatment",
                    cta="Finish the Membership studio review in the membership booking page",
                    fit="Fits disappeared_high_value_customer as the last membership retry of the quiet Membership studio for observed_face_value $220.00",
                ),
            ],
            real_urgency=False,
        ),
        _service_play_payload(
            2,
            play_id="membership-visit-proof",
            campaign="Show the Return Visit",
            strategy="Lead with approved Membership studio proof before asking for a booking-page review",
            diagnosis="The quiet $220.00 membership stall needs visible return-visit proof rather than a new offer",
            offer_recommendation="For the $220.00 pile I would show proof before any price and skip the review task entirely, because this member needs a reason to come back, not another reminder thread. If the proof library is thin, the quiet-lane play is the safer default.",
            mechanism="A proof-led membership reminder that stays inside the 45 percent contribution margin",
            **_ladder_kwargs(
                product_label="Membership studio",
                mechanism="A proof-led membership reminder that stays inside the 45 percent contribution margin",
                cta="Reply to say which Membership studio time would work before opening the booking page",
                core_subject="quick question",
                core_body="Did the Membership studio stop working for your schedule?",
            ),
            sequence="Share approved proof, invite a booking-page look, then hold for human review",
            cta="Reply to say which Membership studio time would work before opening the booking page",
            channel="Open with a short SMS, carry the proof in email, and skip the review task entirely",
            big_idea="The Membership studio returns when proof is shown, not when pressure is added",
            email_steps=(
                (
                    "Proof first",
                    "quick question",
                    "Did the Membership studio stop working for your schedule?",
                    0,
                ),
                (
                    "Proof reminder",
                    "still there",
                    "Three return-visit studies still cover the Membership studio on the membership booking page.",
                    2,
                ),
                (
                    "Human gate",
                    "A reviewer can close the Membership studio proof path",
                    "Email three: move the Membership studio proof review to a human gate",
                    4,
                ),
            ),
            sms_messages=(
                "Send one SMS after email one pointing to the membership booking page for Membership studio proof",
            ),
            calendar_steps=(
                (0, "Send the Membership studio proof email"),
                (2, "Send the Membership studio proof reminder"),
                (4, "Send the Membership studio human-gate email"),
            ),
            task_track="Open a human review task if the Membership studio proof in the membership booking page stays unread",
            cards=[
                _service_card(
                    2,
                    1,
                    name="Third January",
                    tension="You have restarted this before and you already know how it ends",
                    idea="Say the failed restarts out loud before offering anything at all",
                    hook="Third January, same plan, same February",
                    visual="Three January pages torn from three different years of calendar",
                    proof="A still frame that displays Three return-visit studies as the only proof",
                    style="Tweet-style static, native post crop, aggregate labels only",
                    cta="Review Membership studio proof in the membership booking page",
                    fit="Fits disappeared_high_value_customer by proof-led membership retry of the quiet Membership studio for observed_face_value $220.00",
                ),
                _service_card(
                    2,
                    2,
                    name="Less Busy",
                    tension="The sentence you say in March is the one you said in January",
                    idea="Use the members own postponement as the only evidence needed",
                    hook="I will go back when I am less busy, said in March",
                    visual="A dense block of plain text with one date marked in the margin",
                    proof="A timeline graphic sourced only from Three return-visit studies",
                    style="Wall of text static, high contrast, no imagery, aggregate labels only",
                    cta="Continue the Membership studio proof in the membership booking page",
                    fit="Fits disappeared_high_value_customer by visit-proof retry of the quiet Membership studio for observed_face_value $220.00",
                ),
                _service_card(
                    2,
                    3,
                    name="Not A Decision",
                    tension="Deciding every single time is the part that actually exhausts you",
                    idea="Sell back the week where showing up stopped being a choice",
                    hook="A week where showing up was not a decision",
                    visual="A quiet morning street with someone already walking, no building in frame",
                    proof="A human-review badge that cites Three return-visit studies only",
                    style="Us versus them static, before and after split, aggregate labels only",
                    cta="Hand the Membership studio proof to human review via the membership booking page",
                    fit="Fits disappeared_high_value_customer by human-gated proof of the quiet Membership studio for observed_face_value $220.00",
                ),
            ],
            real_urgency=False,
        ),
        _service_play_payload(
            3,
            play_id="membership-capacity-lane",
            campaign="Keep the Capacity Lane",
            strategy="Bound the Membership studio return to approved weekly review capacity",
            diagnosis="The $220.00 quiet membership opportunity must stay inside approved weekly capacity",
            offer_recommendation="The capacity lane suits the $220.00 pile when the studio can genuinely hold review slots, because that bounded window is the only urgency the evidence supports. If weekly capacity is not real, do not run this play; use the proof-led return instead.",
            mechanism="A capacity-bounded membership review that does not invent urgency",
            **_ladder_kwargs(
                product_label="Membership studio",
                mechanism="A capacity-bounded membership review that does not invent urgency",
                cta="Request a Membership studio review slot in the membership booking page",
                core_subject="still want a spot?",
                core_body="Have you given up on the Membership studio for this season?",
            ),
            sequence="Name the capacity bound, offer the booking page, then stop if capacity is full",
            cta="Request a Membership studio review slot in the membership booking page",
            channel="Start with the human review task, confirm by email, and use SMS only after a slot is held",
            big_idea="The Membership studio returns through a bounded review lane, not through invented urgency",
            email_steps=(
                (
                    "Capacity named",
                    "still want a spot?",
                    "Have you given up on the Membership studio for this season?",
                    0,
                ),
                (
                    "Slot reminder",
                    "the slot",
                    "A Membership studio review slot is still inside the weekly capacity on the membership booking page.",
                    2,
                ),
                (
                    "Stop or review",
                    "Close or hold the Membership studio capacity lane",
                    "Email three: stop if capacity is full or complete the Membership studio review",
                    5,
                ),
            ),
            sms_messages=(
                "Send one SMS after email one naming the Membership studio capacity lane and the membership booking page",
            ),
            calendar_steps=(
                (0, "Send the Membership studio capacity email"),
                (2, "Send the Membership studio slot reminder"),
                (5, "Send the Membership studio stop-or-review email"),
            ),
            task_track="Open a human review task when the Membership studio capacity lane is full or the membership booking page stays unresolved",
            cards=[
                _service_card(
                    3,
                    1,
                    name="Nobody Noticed",
                    tension="Being unmissed turns out to be worse than being unfit",
                    idea="Let the silence be the thing the ad is willing to admit",
                    hook="Nobody noticed I stopped going and that was the worst part",
                    visual="A sign-in sheet with one name that simply stops partway down the page",
                    proof="An on-screen panel placing Three return-visit studies beside the weekly count",
                    style="Founder customer call, split screen, phone audio, unscripted",
                    cta="Request a Membership studio slot in the membership booking page",
                    fit="Fits disappeared_high_value_customer by capacity-bounded membership review of the quiet Membership studio for observed_face_value $220.00",
                ),
                _service_card(
                    3,
                    2,
                    name="Still Paying",
                    tension="You have not decided to quit, you have only stopped going",
                    idea="Give the reader a self-check they cannot answer comfortably",
                    hook="How to tell if you have quit something you still pay for",
                    visual="A bank statement line repeating down a phone screen",
                    proof="A slot card that cites Three return-visit studies without extra claims",
                    style="Use-case expansion, three short scenes, aggregate labels only",
                    cta="Hold the Membership studio slot through the membership booking page",
                    fit="Fits disappeared_high_value_customer by human-gated capacity review of the quiet Membership studio for observed_face_value $220.00",
                ),
                _service_card(
                    3,
                    3,
                    name="Week Three",
                    tension="You assume everyone else kept going and only you stopped",
                    idea="Reveal the ordinary restart that nobody mentions out loud",
                    hook="Most people restart in week three and nobody says that part",
                    visual="One line of text over a flat field, nothing else in frame",
                    proof="A closed-lane marker that still cites Three return-visit studies",
                    style="Headline static, one line, aggregate labels only",
                    cta="Take the Membership studio slot on the membership booking page",
                    fit="Fits disappeared_high_value_customer by capacity-stop membership review of the quiet Membership studio for observed_face_value $220.00",
                ),
            ],
            real_urgency=True,
        ),
    ]
    return CompleteRecoveryPlaySetV1(
        run_id=run_id,
        built_at=built_at,
        provider="fixture",
        plays=[CompleteRecoveryPlayV1.model_validate(_apply_pile_plan(play)) for play in plays],
    )


def build_canonical_service_recovery_strategy(
    money_map: MoneyMapV1,
    *,
    built_at: datetime | None = None,
) -> CanonicalSaasStrategyRun:
    when = built_at or datetime(2026, 7, 29, 18, 0, tzinfo=timezone.utc)
    packet = build_grounded_strategy_packet(
        money_map, canonical_service_business_profile(), built_at=when
    )
    plays = _canonical_service_plays(money_map.run_id, when)
    validate_complete_recovery_play_set(plays, packet)
    report = build_differentiation_report(plays)
    receipt = StrategyAuditReceiptV1(
        run_id=money_map.run_id,
        status="completed",
        prompt_version="found-money-three-play.v1",
        prompt_hash=_sha(b"deterministic three-play service fixture from frozen evidence"),
        output_schema_version="recovery-plays.v1",
        output_schema_hash=_sha(b"complete recovery play and concept card contract v1"),
        configured_model_id="fixture-strategy-v1",
        returned_model_id="fixture-strategy-v1",
        response_id="fixture-three-play-service-response-v1",
        token_usage=StrategyTokenUsageV1(input_tokens=0, output_tokens=0),
        attempt_count=1,
        evidence_packet_hash=_sha(packet.to_canonical_json()),
        output_hash=_sha(plays.to_canonical_json()),
        failure_code=None,
    )
    return CanonicalSaasStrategyRun(packet, plays, receipt, report)


def build_withheld_payment_dependent_service_strategy(
    money_map: MoneyMapV1,
    *,
    built_at: datetime | None = None,
) -> CanonicalSaasStrategyRun:
    when = built_at or datetime(2026, 7, 29, 18, 0, tzinfo=timezone.utc)
    packet = build_grounded_strategy_packet(
        money_map, canonical_service_business_profile(), built_at=when
    )
    plays = RecoveryPlaySetV1(
        run_id=money_map.run_id,
        built_at=when,
        provider="fixture",
        plays=[],
    )
    receipt = StrategyAuditReceiptV1(
        run_id=money_map.run_id,
        status="needs_strategy_review",
        prompt_version="found-money-three-play.v1",
        prompt_hash=_sha(b"deterministic three-play service fixture from frozen evidence"),
        output_schema_version="recovery-plays.v1",
        output_schema_hash=_sha(b"complete recovery play and concept card contract v1"),
        configured_model_id="fixture-strategy-v1",
        returned_model_id="fixture-strategy-v1",
        response_id="fixture-three-play-service-response-v1",
        token_usage=StrategyTokenUsageV1(input_tokens=0, output_tokens=0),
        attempt_count=1,
        evidence_packet_hash=_sha(packet.to_canonical_json()),
        output_hash=_sha(plays.to_canonical_json()),
        failure_code="missing_payment",
    )
    return CanonicalSaasStrategyRun(packet, plays, receipt, None)


def validate_reviewer_packet(
    packet: StrategyReviewerPacketV2,
    play_set: CompleteRecoveryPlaySetV1,
) -> None:
    """Bind a certified review to the artifact it claims to have reviewed.

    The deterministic checks run here too, and they run regardless of what the
    reviewer said. Rubric v2 removed the schema half from model judgment, so a
    reviewer pass no longer carries any statement about it; without this the
    packet would certify an artifact nothing had checked.
    """
    from found_money.strategy.card_checks import card_violations

    if packet.reviewed_artifact_sha256 != _sha(play_set.to_canonical_json()):
        raise ValueError("reviewer packet does not bind to the recovery-play artifact")
    if packet.passed:
        violations = card_violations(play_set)
        if violations:
            raise ValueError(
                "deterministic card checks fail, so no reviewer pass certifies this "
                f"artifact: {', '.join(violations)}"
            )


def public_complete_recovery_play_projection(
    play_set: CompleteRecoveryPlaySetV1,
) -> dict[str, Any]:
    """Public-safe campaign index without evidence, copy, or provider metadata."""
    from found_money.redaction import assert_public_safe

    projection = {
        "schema_version": "recovery-plays-public.v1",
        "run_id": play_set.run_id,
        "plays": [
            {
                "play_id": play.play_id,
                "rank": play.rank,
                "campaign_title": play.campaign_name,
                "concept_card_ids": [card.card_id for card in play.concept_cards],
            }
            for play in sorted(play_set.plays, key=lambda item: item.rank)
        ],
    }
    assert_public_safe(projection)
    return projection


def render_recovery_plays_field_presence_html(play_set: CompleteRecoveryPlaySetV1) -> str:
    sections: list[str] = ["<!doctype html><html><body><main><h1>Recovery Plays</h1>"]
    labels = (
        "Campaign strategy",
        "Value basis",
        "Audience",
        "Recoverability",
        "Diagnosis",
        "Offer mechanism",
        "Lifecycle sequence",
        "Email sequence",
        "SMS",
        "Task talk track",
        "Primary CTA",
        "Objections",
        "Urgency",
        "Calendar",
        "Stop conditions",
        "Tracking and success event",
        "Channel emphasis",
        "Creative big idea",
    )
    for play in sorted(play_set.plays, key=lambda item: item.rank):
        sections.append(f'<article data-play-id="{html.escape(play.play_id)}">')
        sections.append(f"<h2>{html.escape(play.campaign_name)}</h2>")
        for label in labels:
            sections.append(f"<h3>{label}</h3>")
        sections.append(f"<p>{html.escape(play.diagnosis.text)}</p>")
        sections.append(f"<p>{html.escape(play.urgency.text)}</p>")
        for card in play.concept_cards:
            sections.append(f'<section data-card-id="{html.escape(card.card_id)}">')
            sections.append(f"<h3>Concept Card: {html.escape(card.card_name)}</h3>")
            for label in (
                "Audience tension",
                "Big idea",
                "Hook",
                "Opening visual",
                "Proof device",
                "Format and style",
                "CTA",
                "Pile fit",
                "Production requirements",
            ):
                sections.append(f"<h4>{label}</h4>")
            sections.append(f"<p>{html.escape(card.big_idea.text)}</p></section>")
        sections.append("</article>")
    sections.append("</main></body></html>")
    return "".join(sections)


def parse_complete_recovery_plays(data: bytes) -> CompleteRecoveryPlaySetV1:
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"malformed JSON: {exc}") from exc
    model = CompleteRecoveryPlaySetV1.model_validate(payload)
    if model.to_canonical_json() != bytes(data):
        raise ValueError("serialized bytes are not exactly canonical")
    return model
