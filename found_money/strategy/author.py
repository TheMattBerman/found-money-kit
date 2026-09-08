"""Skill-based recovery play set generator (FM-062).

Generates compliant CompleteRecoveryPlaySetV1 instances dynamically from a
GroundedStrategyEvidencePacketV1 and StrategyBusinessProfileV1 according to
the recovery-intelligence skill rules and tables.
"""

from __future__ import annotations

import re
from typing import Any

from found_money.contracts.campaign import (
    CompleteRecoveryPlaySetV1,
    CompleteRecoveryPlayV1,
)
from found_money.contracts.strategy import (
    GroundedStrategyEvidencePacketV1,
    StrategyBusinessProfileV1,
    StrategyProviderName,
)
from found_money.contracts.value import CURRENCY_EXPONENTS, format_major_units, normalize_currency
from found_money.strategy.campaign import (
    _MINOR_UNIT_RE,
    _apply_pile_plan,
    _ccopy,
    _copy,
    _ladder_for_play,
    build_differentiation_report,
    validate_complete_recovery_play_set,
)
from found_money.strategy.intelligence import (
    load_table,
    no_email_play_for,
)

_COMPLETE_PLAY_ORDER = (
    "payment_rescue",
    "canceled_customer",
    "disappeared_high_value_customer",
)


def _evidence_map_value_text(value_str: str) -> str:
    match = _MINOR_UNIT_RE.match(value_str)
    if match is None:
        return value_str
    amount, code = match.groups()
    try:
        currency = normalize_currency(code)
    except (KeyError, ValueError):
        return value_str
    if currency not in CURRENCY_EXPONENTS:
        return value_str
    major = format_major_units(amount, currency)
    whole, dot, fraction = major.partition(".")
    formatted = f"{int(whole):,}" + (dot + fraction if dot else "")
    return f"${formatted}" if currency == "usd" else f"{currency.upper()} {formatted}"


def _packet_value_of(packet: GroundedStrategyEvidencePacketV1, evidence_id: str) -> str | None:
    for item in packet.evidence:
        if item.evidence_id == evidence_id:
            return str(item.value)
    return None


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
    product: str,
    proof_ref: str,
    destination: str,
    basis_ref: str,
    value_ref: str,
    proof_extra_refs: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "card_id": f"concept-{play_rank}-{card_rank}",
        "card_name": name,
        "audience_tension": _copy(tension, product, kind="product"),
        "big_idea": _copy(idea, product, kind="product"),
        "hook": _copy(hook, product, kind="product"),
        "opening_visual": _copy(visual, product, kind="product"),
        "proof_device": _copy(proof, proof_ref, *proof_extra_refs, kind="proof"),
        "format_style": style,
        "cta": _copy(cta, destination, kind="destination"),
        "pile_fit": _copy(fit, basis_ref, value_ref, kind="general"),
        "production_requirements": [
            "Use synthetic interface graphics and aggregate figures only",
            "Prepare static frames suitable for later vertical production",
        ],
    }


def _author_payment_rescue_play(
    packet: GroundedStrategyEvidencePacketV1,
    rank: int,
    pile_rank: int,
    product: str,
    proof: str,
    margin: str,
    channel: str,
    capacity: str | None,
    destination: str,
    pile_value_str: str,
    variant: int = 1,
) -> dict[str, Any]:
    amount_ref = f"ev_pile_{pile_rank}_value"
    basis_ref = f"ev_pile_{pile_rank}_basis"
    product_name = _packet_value_of(packet, product) or "Annual subscription"
    proof_name = _packet_value_of(packet, proof) or "Three approved case studies"
    dest_name = _packet_value_of(packet, destination) or destination

    if variant == 2:
        play_id = "payment-rescue-proof-reset"
        title = "Remember the Win"
        campaign_name = "Remember the Win"
        card_specs = [
            (
                "Built It Twice Outcome",
                "Redoing work you already paid once to have done",
                "Frame the cost as the hours spent again, not the invoice that lapsed",
                "POV you are rebuilding the report you already built once",
                "Two identical reports side by side, one of them dated months earlier",
                "Tweet-style static, native post crop, aggregate labels only",
            ),
            (
                "Two Quarters Elapsed",
                "The sunk cost is time, which is easier to count than to admit",
                "Let the arithmetic of the wasted quarters carry the whole argument",
                "We spent two quarters rebuilding what we were already paying for",
                "A dense block of plain text with the two quarters marked in the margin",
                "Wall of text static, high contrast, no imagery, aggregate labels only",
            ),
            (
                "Sunday Night Scramble",
                "The week starts with a scramble that was supposed to be solved already",
                "Sell back the recovered evening rather than the restored feature",
                "I want Monday numbers without Sunday night",
                "A dark kitchen table late at night with a laptop still open on it",
                "Us versus them static, before and after split, aggregate labels only",
            ),
        ]
    elif variant == 3:
        play_id = "payment-rescue-capacity-window"
        title = "Reserve the Restart"
        campaign_name = "Reserve the Restart"
        card_specs = [
            (
                "Renewal Friday Looming",
                "Renewing something nobody has opened is harder than cancelling it",
                "Put the unused months and the renewal date inside the same frame",
                "Renewal is Friday and nobody has opened it since March",
                "A wall calendar with March circled and Friday circled and nothing in between",
                "Founder customer call, split screen, phone audio, unscripted",
            ),
            (
                "The Other Team Usage",
                "Finding out a team like yours quietly kept the thing you dropped",
                "Make the comparison land as a discovery rather than as a pitch",
                "The other team never stopped using it and nobody mentioned that",
                "Two identical dashboards, one lit and active, one dark and dormant",
                "Use-case expansion, three short scenes, aggregate labels only",
            ),
            (
                "Whole Time Discovery",
                "Paying for something whose most useful part you never actually found",
                "Reveal the part of the account that was sitting there the whole time",
                "Since when was that sitting in the account the whole time",
                "One line of text over a flat field, nothing else in frame",
                "Headline static, one line over a flat field, aggregate labels only",
            ),
        ]
    else:
        play_id = "payment-rescue-friction-fix"
        title = "Fix the Friction"
        campaign_name = "Fix the Friction"
        card_specs = [
            (
                "Dead Login Snag",
                "You only find out the card failed at the moment you actually need the thing",
                "Show the moment access disappears, not the billing notice that preceded it",
                "Deck is due at nine and the login is dead",
                "A laptop on a desk at night showing a locked-out login and a half-written deck",
                "First-frame screenshot static, phone-shot, no brand chrome",
            ),
            (
                "Nine Days Delay",
                "Nobody on the team wants to be the one who says the billing broke",
                "Put the silence between the failure and the discovery on screen",
                "We were locked out nine days before anyone told me",
                "A team chat thread where the problem finally surfaces nine days late",
                "Social comment screenshot, native thread crop, aggregate labels only",
            ),
            (
                "Silent Failure",
                "Assuming everything is running fine until an essential workflow halts",
                "Contrast smooth daily operations with an unexpected payment pause",
                "Everything was running until the card expired silently",
                "A calendar notification popping up beside an active dashboard screen",
                "Founder customer call, split screen, phone audio, unscripted",
            ),
        ]

    payload: dict[str, Any] = {
        "play_id": play_id,
        "pile_id": "payment_rescue",
        "rank": rank,
        "title": title,
        "rationale": f"A solvable payment snag is interrupting the {product_name} despite a {pile_value_str} observed opportunity",
        "offer_recommendation": f"Run the guided billing-update offer against the {pile_value_str} payment_rescue pile first: it removes the only friction the evidence names and asks the customer for nothing new. If you would rather not send customers to the {dest_name}, switch to the proof-led reset and keep the same sequence shape.",
        "recommended_actions": [
            "Review the aggregate payment rescue evidence before approval",
            "Approve or withhold the complete sequence without sending it",
        ],
        "campaign_name": campaign_name,
        "campaign_strategy": _copy(
            f"Remove payment friction around the {product_name} before discussing value",
            product,
            amount_ref,
            kind="product",
        ),
        "value_basis": _copy(
            "Use the observed face value basis without treating it as recovered revenue",
            basis_ref,
        ),
        "audience": _copy(
            f"Account holders with an observed failed payment for the {product_name}",
            product,
            amount_ref,
            kind="product",
        ),
        "recoverability": _copy(
            f"The observed {pile_value_str} payment rescue opportunity remains reviewable",
            amount_ref,
            kind="number",
        ),
        "diagnosis": _copy(
            f"A solvable payment snag is interrupting the {product_name} despite a {pile_value_str} observed opportunity",
            product,
            amount_ref,
            *([capacity] if capacity is not None else []),
            kind="product",
        ),
    }

    ladder_id = f"{play_id}-ladder"
    ladder, rung_packages = _ladder_for_play(
        ladder_id,
        play_id,
        diagnosis_copy=payload["diagnosis"],
        product=product,
        margin=margin,
        proof=proof,
        anchor_mechanism=f"{product_name} with full onboarding and a twelve-month commitment at the standard rate",
        core_mechanism=f"A guided billing update for the {product_name} with no invented discount",
        downsell_mechanism=f"Self-serve {dest_name} update with no guided support or onboarding program",
        outcome_text=f"Restore {product_name} billing without conceding an unsupported discount",
        ordering_basis_text="The anchor establishes fullest scope, the core is the recommended path, and the downsell is the save path with less scope",
        rung_email_copy=(
            (
                "full annual subscription onboarding?",
                f"The premium {product_name} path includes full onboarding if you want the complete program.",
                f"Review the {product_name} details in the {dest_name}",
            ),
            (
                "billing glitch?",
                f"Did the card on file for the {product_name} stop working?",
                f"Review the {product_name} details in the {dest_name}",
            ),
            (
                "quick self-serve fix",
                f"The {dest_name} has a self-serve path if you only need to update the card without onboarding.",
                f"Review the {product_name} details in the {dest_name}",
            ),
        ),
    )
    payload["offer_ladder"] = ladder
    payload["rung_copy_packages"] = rung_packages

    cta_text = f"Review the {product_name} details in the {dest_name}"
    email_steps = (
        (
            "Recognition",
            "billing glitch?",
            f"Did the card on file for the {product_name} stop working?",
            0,
        ),
        (
            "Guided update",
            "the billing link",
            f"The {dest_name} has the {product_name} card details if that is easier than hunting for them.",
            1,
        ),
        (
            "Closure decision",
            "closing this out",
            f"I will close the {product_name} billing review on our side unless you would rather keep it open.",
            3,
        ),
    )

    cards = [
        _card(
            rank,
            c_idx,
            name=c_name,
            tension=c_tension,
            idea=c_idea,
            hook=c_hook,
            visual=c_visual,
            proof=f"A before-and-after billing-state comparison that uses {proof_name} as the only proof source",
            style=c_style,
            cta=f"Review the {product_name} in the {dest_name}",
            fit=f"The creative fits payment_rescue because the failed {product_name} payment is a billing retry rather than a decision to leave, on a {pile_value_str} opportunity",
            product=product,
            proof_ref=proof,
            destination=destination,
            basis_ref=basis_ref,
            value_ref=amount_ref,
        )
        for c_idx, (c_name, c_tension, c_idea, c_hook, c_visual, c_style) in enumerate(
            card_specs, start=1
        )
    ]

    payload.update(
        {
            "lifecycle_sequence": _copy(
                f"Recognize the interruption, guide resolution, then confirm closure for the {product_name}",
                product,
                kind="product",
            ),
            "email_sequence": [
                {
                    "order": idx,
                    "lifecycle_stage": _copy(stage, product, kind="product"),
                    "subject": _ccopy(sub, kind="product"),
                    "body": _ccopy(bod, kind="general"),
                    "cta": _ccopy(cta_text, kind="destination"),
                    "wait_days": wait,
                }
                for idx, (stage, sub, bod, wait) in enumerate(email_steps, start=1)
            ],
            "sms": {
                "available": True,
                "messages": [
                    _ccopy(
                        f"Send one SMS after email one pointing the {product_name} review to the {dest_name}",
                        kind="destination",
                    )
                ],
            },
            "task_talk_track": _copy(
                f"Open a human review task only if the {product_name} {dest_name} update stays unresolved after the email sequence",
                product,
                destination,
                kind="destination",
            ),
            "primary_cta": _copy(cta_text, destination, kind="destination"),
            "objections": [
                {
                    "objection": _copy(
                        f"The {product_name} may no longer feel necessary",
                        product,
                        kind="objection",
                    ),
                    "response": _copy(
                        f"Use the {proof_name} as approved proof without adding claims",
                        proof,
                        kind="proof",
                    ),
                },
                {
                    "objection": _copy(
                        f"The {product_name} payment timing may be inconvenient",
                        product,
                        kind="objection",
                    ),
                    "response": _copy(
                        f"Offer the {dest_name} as the approved destination without inventing terms",
                        destination,
                        kind="destination",
                    ),
                },
            ],
            "urgency": {"text": "none", "evidence_ids": []},
            "calendar": [
                {
                    "day": day,
                    "action": _copy(act, product, kind="product"),
                    "stop_condition": _copy(
                        f"Stop when the {product_name} payment state changes or approval is withdrawn",
                        product,
                        kind="product",
                    ),
                }
                for day, act in (
                    (0, f"Send the {product_name} recognition email"),
                    (1, f"Send the {product_name} guided-update email"),
                    (3, f"Send the {product_name} closure email"),
                )
            ],
            "stop_conditions": [
                _copy(
                    f"Stop after an {product_name} payment state change", product, kind="product"
                ),
                _copy(
                    f"Stop when approval for the {product_name} is withdrawn",
                    product,
                    kind="product",
                ),
            ],
            "tracking": {
                "success_event": _copy(
                    f"Record an observed {product_name} payment state change without claiming recovery",
                    product,
                    kind="product",
                ),
                "tracked_signals": [
                    _copy(
                        f"Track aggregate {dest_name} review completion",
                        destination,
                        kind="destination",
                    ),
                    _copy("Track observed face value status separately", basis_ref, kind="general"),
                ],
            },
            "channel_emphasis": _copy(
                "Lead with email instructions, reinforce with concise SMS, and reserve tasks for unresolved reviews",
                channel,
                kind="general",
            ),
            "creative_big_idea": _copy(
                f"The {product_name} is paused by a fixable payment snag rather than lost intent",
                product,
                kind="product",
            ),
            "concept_cards": cards,
        }
    )
    return _apply_pile_plan(payload)


def _author_canceled_customer_play(
    packet: GroundedStrategyEvidencePacketV1,
    rank: int,
    pile_rank: int,
    product: str,
    proof: str,
    margin: str,
    channel: str,
    capacity: str | None,
    destination: str,
    pile_value_str: str,
    variant: int = 1,
) -> dict[str, Any]:
    amount_ref = f"ev_pile_{pile_rank}_value"
    basis_ref = f"ev_pile_{pile_rank}_basis"
    product_name = _packet_value_of(packet, product) or "Annual subscription"
    proof_name = _packet_value_of(packet, proof) or "Three approved case studies"
    dest_name = _packet_value_of(packet, destination) or destination

    if variant == 2:
        play_id = "canceled-customer-proof-reset"
        title = "Proof-Led Renewal"
        campaign_name = "Proof-Led Renewal"
        card_specs = [
            (
                "New Proven Capabilities",
                "Former teams assume product capabilities have remained static",
                "Highlight recent major capabilities validated by peers",
                "What changed since your team stepped away",
                "A modern dashboard comparison highlighting new workflows",
                "First-frame screenshot static, phone-shot, no brand chrome",
            ),
            (
                "Peer Benchmark Comparison",
                "Teams leave assuming their operational bottlenecks were unique",
                "Present comparative efficiency benchmarks from active peers",
                "How peer operators solved the exact bottleneck",
                "A benchmark metric callout with aggregate percentages",
                "Social comment screenshot, native thread crop, aggregate labels only",
            ),
            (
                "Second Look Invitation",
                "Returning feels harder than staying away without a low-stakes path",
                "Offer an exploratory low-stakes evaluation window",
                "Take a second look without committing upfront",
                "A simple invitation card showing standard evaluation steps",
                "Founder letter, text-only static, no design treatment",
            ),
        ]
    elif variant == 3:
        play_id = "canceled-customer-capacity-window"
        title = "Waived-Fee Review Window"
        campaign_name = "Waived-Fee Review Window"
        card_specs = [
            (
                "Priority Onboarding Slot",
                "Re-implementation overhead delays the restart decision",
                "Offer dedicated onboarding support within a bounded window",
                "Restart without the implementation headache",
                "A calendar view showing a reserved onboarding slot",
                "Founder customer call, split screen, phone audio, unscripted",
            ),
            (
                "Waived Implementation Terms",
                "Setup fees make re-evaluating the product cost-prohibitive",
                "Waive all setup charges when renewing annual terms",
                "Zero friction restart for previously active accounts",
                "A clear terms summary showing waived implementation line",
                "Use-case expansion, three short scenes, aggregate labels only",
            ),
            (
                "Executive Sponsor Seat",
                "Restarting lacks executive momentum after an account closure",
                "Provide an executive alignment session on resumption",
                "Executive oversight for your restored workflow",
                "A clean headline card focusing on strategic alignment",
                "Headline static, one line over flat field, aggregate labels only",
            ),
        ]
    else:
        play_id = "canceled-customer-reconnect"
        title = "Reconnect Lapsed Accounts"
        campaign_name = "Reconnect Lapsed Accounts"
        card_specs = [
            (
                "Torn Calendar",
                "Canceling seemed like a clean break until the workload shifted right back to the team",
                "Highlight the renewed operational burden after canceling rather than pitching the tool",
                "Two months after canceling and the manual spreadsheets are back",
                "A cluttered desktop screen showing multiple mismatched spreadsheet tabs",
                "Tweet-style static, native post crop, aggregate labels only",
            ),
            (
                "Unfinished Build",
                "Starting over from scratch costs more time and energy than anyone anticipated",
                "Put the real arithmetic of rebuilding lost workflows into focus",
                "We spent two quarters rebuilding what was already configured",
                "A dense block of plain text laying out the timeline of wasted engineering hours",
                "Wall of text static, high contrast, no imagery, aggregate labels only",
            ),
            (
                "Sunday Evening Reset",
                "Giving up the subscription brought back the weekend scramble",
                "Show the personal relief of automated workflows restored",
                "I want Monday morning clarity without Sunday night scrambling",
                "A quiet office desk before dawn with a clean summary report on screen",
                "Us versus them static, before and after split, aggregate labels only",
            ),
        ]

    payload: dict[str, Any] = {
        "play_id": play_id,
        "pile_id": "canceled_customer",
        "rank": rank,
        "title": title,
        "rationale": f"Lapsed accounts who voluntarily canceled the {product_name} represent a {pile_value_str} opportunity warranting a step-up renewal offer",
        "offer_recommendation": f"Run the waived-fee continuity offer against the {pile_value_str} canceled_customer pile: hold full price and offer an implementation bonus for renewal. If customers prefer self-serve re-activation, point them to the {dest_name}.",
        "recommended_actions": [
            "Review canceled accounts before sending re-activation outreach",
            "Ensure waived setup fee continuity applies only with committed renewal terms",
        ],
        "campaign_name": campaign_name,
        "campaign_strategy": _copy(
            f"Step up lapsed customer relationship for the {product_name} with value-add continuity",
            product,
            amount_ref,
            kind="product",
        ),
        "value_basis": _copy(
            "Use the observed face value basis without treating it as recovered revenue",
            basis_ref,
        ),
        "audience": _copy(
            f"Former account holders who voluntarily canceled their {product_name}",
            product,
            amount_ref,
            kind="product",
        ),
        "recoverability": _copy(
            f"The observed {pile_value_str} canceled customer opportunity remains reviewable",
            amount_ref,
            kind="number",
        ),
        "diagnosis": _copy(
            f"Lapsed accounts who voluntarily canceled the {product_name} represent a {pile_value_str} opportunity warranting a step-up renewal offer",
            product,
            amount_ref,
            kind="product",
        ),
    }

    ladder_id = f"{play_id}-ladder"
    ladder, rung_packages = _ladder_for_play(
        ladder_id,
        play_id,
        diagnosis_copy=payload["diagnosis"],
        product=product,
        margin=margin,
        proof=proof,
        anchor_mechanism=f"{product_name} renewal with executive implementation review and twelve-month term",
        core_mechanism=f"Waived setup fee continuity for the {product_name} upon annual renewal",
        downsell_mechanism=f"Quarterly renewal terms for the {product_name} without onboarding assistance",
        outcome_text=f"Reactivate {product_name} accounts through commitment-backed continuity without discounting core fees",
        ordering_basis_text="The anchor establishes the fullest renewal scope, the core is the recommended step-up continuity offer, and the downsell reduces scope to quarterly flexibility",
        rung_email_copy=(
            (
                "executive renewal path?",
                f"Our full executive renewal path includes implementation review for your {product_name}.",
                f"Explore renewal options in the {dest_name}",
            ),
            (
                "quick question",
                f"Have you given up on the {product_name} for your team?",
                f"Explore renewal options in the {dest_name}",
            ),
            (
                "quarterly flexibility",
                f"We can offer a quarterly renewal path for the {product_name} if twelve months is not the right fit.",
                f"Explore renewal options in the {dest_name}",
            ),
        ),
    )
    payload["offer_ladder"] = ladder
    payload["rung_copy_packages"] = rung_packages

    cta_text = f"Explore renewal options in the {dest_name}"
    email_steps = (
        (
            "Low-friction check-in",
            "quick question",
            f"Have you given up on the {product_name} for your team?",
            0,
        ),
        (
            "Continuity offer",
            "restart terms",
            f"We can waive the implementation fee if you decide to restart the {product_name} this quarter.",
            3,
        ),
    )

    cards = [
        _card(
            rank,
            c_idx,
            name=c_name,
            tension=c_tension,
            idea=c_idea,
            hook=c_hook,
            visual=c_visual,
            proof=f"A verified case comparison backed by {proof_name} without additional claims",
            style=c_style,
            cta=f"Review renewal options in the {dest_name}",
            fit=f"The creative fits canceled_customer because lapsed accounts realize manual friction after leaving the {product_name}, on a {pile_value_str} opportunity",
            product=product,
            proof_ref=proof,
            destination=destination,
            basis_ref=basis_ref,
            value_ref=amount_ref,
        )
        for c_idx, (c_name, c_tension, c_idea, c_hook, c_visual, c_style) in enumerate(
            card_specs, start=1
        )
    ]

    payload.update(
        {
            "lifecycle_sequence": _copy(
                f"Inquire with low friction, present continuity value, then respect definitive silence for the {product_name}",
                product,
                kind="product",
            ),
            "email_sequence": [
                {
                    "order": idx,
                    "lifecycle_stage": _copy(stage, product, kind="product"),
                    "subject": _ccopy(sub, kind="product"),
                    "body": _ccopy(bod, kind="general"),
                    "cta": _ccopy(cta_text, kind="destination"),
                    "wait_days": wait,
                }
                for idx, (stage, sub, bod, wait) in enumerate(email_steps, start=1)
            ],
            "sms": {
                "available": True,
                "messages": [
                    _ccopy(
                        f"Send one SMS after email one pointing renewal inquiries to the {dest_name}",
                        kind="destination",
                    )
                ],
            },
            "task_talk_track": _copy(
                f"Open a human renewal review task only if the customer replies or engages the {dest_name}",
                product,
                destination,
                kind="destination",
            ),
            "primary_cta": _copy(cta_text, destination, kind="destination"),
            "objections": [
                {
                    "objection": _copy(
                        f"The team found an alternative to the {product_name}",
                        product,
                        kind="objection",
                    ),
                    "response": _copy(
                        f"Use the {proof_name} as approved proof without adding claims",
                        proof,
                        kind="proof",
                    ),
                },
                {
                    "objection": _copy(
                        f"The {product_name} pricing may require budgetary re-approval",
                        product,
                        kind="objection",
                    ),
                    "response": _copy(
                        f"Offer the {dest_name} as the approved destination without inventing terms",
                        destination,
                        kind="destination",
                    ),
                },
            ],
            "urgency": {"text": "none", "evidence_ids": []},
            "calendar": [
                {
                    "day": day,
                    "action": _copy(act, product, kind="product"),
                    "stop_condition": _copy(
                        f"Stop when the {product_name} renewal state changes or approval is withdrawn",
                        product,
                        kind="product",
                    ),
                }
                for day, act in (
                    (0, f"Send the {product_name} low-friction check-in email"),
                    (3, f"Send the {product_name} continuity offer email"),
                )
            ],
            "stop_conditions": [
                _copy(
                    f"Stop after an {product_name} renewal confirmation", product, kind="product"
                ),
                _copy(
                    f"Stop when approval for the {product_name} is withdrawn",
                    product,
                    kind="product",
                ),
            ],
            "tracking": {
                "success_event": _copy(
                    f"Record an observed {product_name} renewal state change without claiming recovery",
                    product,
                    kind="product",
                ),
                "tracked_signals": [
                    _copy(
                        f"Track aggregate {dest_name} renewal page visits",
                        destination,
                        kind="destination",
                    ),
                    _copy("Track observed face value status separately", basis_ref, kind="general"),
                ],
            },
            "channel_emphasis": _copy(
                "Lead with plain-text inquiry, follow with a value-add continuity offer, and stop immediately on silence",
                channel,
                kind="general",
            ),
            "creative_big_idea": _copy(
                f"Past {product_name} customers churned from drifting focus rather than dissatisfaction",
                product,
                kind="product",
            ),
            "concept_cards": cards,
        }
    )
    return _apply_pile_plan(payload)


def _author_disappeared_play(
    packet: GroundedStrategyEvidencePacketV1,
    rank: int,
    pile_rank: int,
    product: str,
    proof: str,
    margin: str,
    channel: str,
    capacity: str | None,
    destination: str,
    pile_value_str: str,
    variant: int = 1,
) -> dict[str, Any]:
    amount_ref = f"ev_pile_{pile_rank}_value"
    basis_ref = f"ev_pile_{pile_rank}_basis"
    product_name = _packet_value_of(packet, product) or "Annual subscription"
    proof_name = _packet_value_of(packet, proof) or "Three approved case studies"
    dest_name = _packet_value_of(packet, destination) or destination

    if variant == 2:
        play_id = "disappeared-proof-reset"
        title = "VIP Proof Check-in"
        campaign_name = "VIP Proof Check-in"
        card_specs = [
            (
                "Benchmark Insights Panel",
                "Enterprise leaders want to know where they stand relative to peers",
                "Share high-level benchmark insights relevant to tier",
                "Where your benchmark numbers sit this quarter",
                "A sleek executive dashboard showing quartile standing",
                "First-frame screenshot static, phone-shot, no brand chrome",
            ),
            (
                "Strategic Impact Snapshot",
                "High-value accounts lose visibility into overall account value",
                "Summarize aggregate value realized across the team",
                "The aggregate impact your team created this year",
                "A quarterly executive briefing summary graphic",
                "Wall of text static, high contrast, no imagery, aggregate labels only",
            ),
            (
                "Quarterly Retrospective Review",
                "Account drift happens quietly when reviews are missed",
                "Schedule an executive retrospective to realign roadmap",
                "A dedicated retrospective on your account roadmap",
                "An invitation screen for an executive retrospective session",
                "Founder letter, text-only static, no design treatment",
            ),
        ]
    elif variant == 3:
        play_id = "disappeared-capacity-window"
        title = "Dedicated VIP Window"
        campaign_name = "Dedicated VIP Window"
        card_specs = [
            (
                "Held Review Slot Reserved",
                "Key accounts require dedicated human attention and bandwidth",
                "Reserve an exclusive executive review slot",
                "Your reserved executive review window is open",
                "A clean calendar invitation showing an executive session",
                "Founder customer call, split screen, phone audio, unscripted",
            ),
            (
                "Founder Office Hours Access",
                "High-value partners appreciate direct executive dialogue",
                "Provide direct access to product leadership",
                "Direct dialogue with product leadership",
                "A video call interface mockup showing founder access",
                "Use-case expansion, three short scenes, aggregate labels only",
            ),
            (
                "Dedicated Lead Analyst",
                "Complex enterprise accounts need tailored guidance",
                "Assign a dedicated senior analyst for account optimization",
                "Dedicated guidance for your enterprise workflows",
                "A clean headline card highlighting senior analyst review",
                "Headline static, one line over flat field, aggregate labels only",
            ),
        ]
    else:
        play_id = "disappeared-high-value-vip-reset"
        title = "VIP Account Check-In"
        campaign_name = "VIP Account Check-In"
        card_specs = [
            (
                "Quiet Dashboard",
                "Paying for a premium account while nobody logs in feels like quiet neglect",
                "Acknowledge the silence before offering any solution",
                "Nobody logged in since March and renewal is coming up",
                "A wall calendar with March circled and today marked with an empty workspace screen",
                "Founder customer call, split screen, phone audio, unscripted",
            ),
            (
                "Quiet Champion",
                "The person who originally championed the tool transitioned and nobody took over",
                "Make the orphaned workflow visible so the current team can easily reclaim it",
                "The original champion moved on and the account went dormant",
                "A handoff memo sitting beside an active database dashboard with unassigned admin rights",
                "Use-case expansion, three short scenes, aggregate labels only",
            ),
            (
                "Untapped Tier",
                "Paying top tier prices while only utilizing basic introductory features",
                "Reveal the dormant advanced features that were included all along",
                "The highest-value workflow was waiting in the account the entire time",
                "One clean sentence displayed in bold typography over a neutral background",
                "Headline static, one line over a flat field, aggregate labels only",
            ),
        ]

    payload: dict[str, Any] = {
        "play_id": play_id,
        "pile_id": "disappeared_high_value_customer",
        "rank": rank,
        "title": title,
        "rationale": f"High-value {product_name} accounts that went quiet represent a {pile_value_str} opportunity warranting a direct founder check-in",
        "offer_recommendation": f"Run the capacity-backed check-in offer against the {pile_value_str} disappeared_high_value_customer pile: reserve dedicated review capacity for quiet high-value accounts. Direct self-serve resets to the {dest_name}.",
        "recommended_actions": [
            "Confirm quiet account status before scheduling founder check-in",
            "Hold reserved review slots to real team capacity without artificial urgency",
        ],
        "campaign_name": campaign_name,
        "campaign_strategy": _copy(
            f"Direct founder inquiry and reserved account review for quiet {product_name} accounts",
            product,
            amount_ref,
            kind="product",
        ),
        "value_basis": _copy(
            "Use the observed face value basis without treating it as recovered revenue",
            basis_ref,
        ),
        "audience": _copy(
            f"Quiet accounts with historical high usage of the {product_name}",
            product,
            amount_ref,
            kind="product",
        ),
        "recoverability": _copy(
            f"The observed {pile_value_str} disappeared high value customer opportunity remains reviewable",
            amount_ref,
            kind="number",
        ),
        "diagnosis": _copy(
            f"High-value {product_name} accounts that went quiet represent a {pile_value_str} opportunity warranting a direct founder check-in",
            product,
            amount_ref,
            *([capacity] if capacity is not None else []),
            kind="product",
        ),
    }

    ladder_id = f"{play_id}-ladder"
    ladder, rung_packages = _ladder_for_play(
        ladder_id,
        play_id,
        diagnosis_copy=payload["diagnosis"],
        product=product,
        margin=margin,
        proof=proof,
        anchor_mechanism=f"{product_name} with dedicated strategy partner and priority quarterly review",
        core_mechanism=f"Direct founder account audit and custom roadmap review for the {product_name}",
        downsell_mechanism=f"Self-serve {dest_name} account check without reserved team review slot",
        outcome_text=f"Re-engage quiet high-value {product_name} accounts through bounded review capacity",
        ordering_basis_text="The anchor provides dedicated executive partnership, the core offers an audited roadmap review slot, and the downsell provides self-serve reactivation",
        rung_email_copy=(
            (
                "dedicated strategy partner?",
                f"The executive partnership includes a dedicated strategy review for your {product_name}.",
                f"Schedule your account review through the {dest_name}",
            ),
            (
                "still on?",
                f"Have you given up on the {product_name} for your team?",
                f"Schedule your account review through the {dest_name}",
            ),
            (
                "self-serve review",
                f"You can review account status in the {dest_name} without scheduling a call.",
                f"Schedule your account review through the {dest_name}",
            ),
        ),
    )
    payload["offer_ladder"] = ladder
    payload["rung_copy_packages"] = rung_packages

    cta_text = f"Schedule your account review through the {dest_name}"
    email_steps = (
        (
            "Quiet account check-in",
            "still on?",
            f"Have you given up on the {product_name} for your team?",
            0,
        ),
        (
            "Capacity window offer",
            "account audit slot",
            f"I have a review window open this week if you want to inspect the {product_name} account together.",
            3,
        ),
    )

    cards = [
        _card(
            rank,
            c_idx,
            name=c_name,
            tension=c_tension,
            idea=c_idea,
            hook=c_hook,
            visual=c_visual,
            proof=f"An on-screen capacity placard citing {proof_name} as the only proof",
            style=c_style,
            cta=f"Book your account review in the {dest_name}",
            fit=f"The creative fits disappeared_high_value_customer because quiet accounts warrant direct human outreach for the {product_name}, on a {pile_value_str} opportunity",
            product=product,
            proof_ref=proof,
            destination=destination,
            basis_ref=basis_ref,
            value_ref=amount_ref,
        )
        for c_idx, (c_name, c_tension, c_idea, c_hook, c_visual, c_style) in enumerate(
            card_specs, start=1
        )
    ]

    payload.update(
        {
            "lifecycle_sequence": _copy(
                f"Personal founder inquiry, reserve human review window, then stop cleanly for the {product_name}",
                product,
                kind="product",
            ),
            "email_sequence": [
                {
                    "order": idx,
                    "lifecycle_stage": _copy(stage, product, kind="product"),
                    "subject": _ccopy(sub, kind="product"),
                    "body": _ccopy(bod, kind="general"),
                    "cta": _ccopy(cta_text, kind="destination"),
                    "wait_days": wait,
                }
                for idx, (stage, sub, bod, wait) in enumerate(email_steps, start=1)
            ],
            "sms": {
                "available": True,
                "messages": [
                    _ccopy(
                        f"Send one SMS after email one pointing quiet account reviews to the {dest_name}",
                        kind="destination",
                    )
                ],
            },
            "task_talk_track": _copy(
                f"Open a founder review task only if the quiet account responds or accesses the {dest_name}",
                product,
                destination,
                kind="destination",
            ),
            "primary_cta": _copy(cta_text, destination, kind="destination"),
            "objections": [
                {
                    "objection": _copy(
                        f"The team does not have time for a full {product_name} audit right now",
                        product,
                        kind="objection",
                    ),
                    "response": _copy(
                        f"Use the {proof_name} as approved proof without adding claims",
                        proof,
                        kind="proof",
                    ),
                },
                {
                    "objection": _copy(
                        f"The account needs internal review before re-engaging the {product_name}",
                        product,
                        kind="objection",
                    ),
                    "response": _copy(
                        f"Offer the {dest_name} as the approved destination without inventing terms",
                        destination,
                        kind="destination",
                    ),
                },
            ],
            "urgency": {"text": "none", "evidence_ids": []},
            "calendar": [
                {
                    "day": day,
                    "action": _copy(act, product, kind="product"),
                    "stop_condition": _copy(
                        f"Stop when the {product_name} account state changes or approval is withdrawn",
                        product,
                        kind="product",
                    ),
                }
                for day, act in (
                    (0, f"Send the {product_name} quiet account check-in email"),
                    (3, f"Send the {product_name} review window offer email"),
                )
            ],
            "stop_conditions": [
                _copy(
                    f"Stop after an {product_name} account review is scheduled",
                    product,
                    kind="product",
                ),
                _copy(
                    f"Stop when approval for the {product_name} is withdrawn",
                    product,
                    kind="product",
                ),
            ],
            "tracking": {
                "success_event": _copy(
                    f"Record an observed {product_name} review booking without claiming recovery",
                    product,
                    kind="product",
                ),
                "tracked_signals": [
                    _copy(
                        f"Track aggregate {dest_name} schedule page views",
                        destination,
                        kind="destination",
                    ),
                    _copy("Track observed face value status separately", basis_ref, kind="general"),
                ],
            },
            "channel_emphasis": _copy(
                "Lead with founder inquiry, reserve human capacity for responsive accounts, and suppress on silence",
                channel,
                kind="general",
            ),
            "creative_big_idea": _copy(
                f"Quiet high-value {product_name} accounts need human stewardship rather than automated marketing",
                product,
                kind="product",
            ),
            "concept_cards": cards,
        }
    )
    return _apply_pile_plan(payload)


def generate_recovery_plays(
    packet: GroundedStrategyEvidencePacketV1,
    profile: StrategyBusinessProfileV1,
    *,
    money_map: Any = None,
    provider_name: StrategyProviderName = "skill",
) -> CompleteRecoveryPlaySetV1:
    """Draft a CompleteRecoveryPlaySetV1 from evidence and profile obeying skill tables."""
    product = "ev_business_product"
    proof = "ev_business_proof"
    margin = "ev_business_margin"
    channel = "ev_business_channel"
    capacity = "ev_business_capacity" if getattr(profile, "capacity", None) is not None else None
    destination = "ev_business_destination"

    pile_values: dict[int, str] = {}
    for item in packet.evidence:
        if item.category == "map_value" and item.evidence_id.startswith("ev_pile_"):
            m = re.match(r"^ev_pile_(\d+)_value$", item.evidence_id)
            if m:
                r = int(m.group(1))
                val_text = _evidence_map_value_text(str(item.value))
                pile_values[r] = val_text

    pile_kinds: dict[int, str] = {}
    if money_map is not None:
        for pile in getattr(money_map, "piles", []):
            pile_kinds[pile.rank] = pile.pile_id

    if not pile_kinds:
        for item in packet.evidence:
            if item.evidence_id.startswith("ev_pile_"):
                m = re.match(r"^ev_pile_(\d+)_", item.evidence_id)
                if m:
                    r = int(m.group(1))
                    val_lower = str(item.value).casefold()
                    if "cancel" in val_lower or "lapsed" in val_lower:
                        pile_kinds[r] = "canceled_customer"
                    elif (
                        "disappear" in val_lower
                        or "quiet" in val_lower
                        or "high_value" in val_lower
                    ):
                        pile_kinds[r] = "disappeared_high_value_customer"
                    elif "payment" in val_lower or "failed" in val_lower or "rescue" in val_lower:
                        pile_kinds[r] = "payment_rescue"

    piles_to_generate: list[tuple[str, int, str]] = []
    for r in sorted(pile_values):
        kind = pile_kinds.get(r)
        if kind is None:
            # Default mapping if not found in basis
            if r == 1:
                kind = "payment_rescue"
            elif r == 2:
                kind = "canceled_customer"
            elif r == 3:
                kind = "disappeared_high_value_customer"
            else:
                continue
        piles_to_generate.append((kind, r, pile_values[r]))

    # Filter out piles where no_email_play_for is True
    eligible_piles = [
        p for p in piles_to_generate if p[0] in _COMPLETE_PLAY_ORDER and not no_email_play_for(p[0])
    ]
    if not eligible_piles:
        raise ValueError("no supported recovery campaign for the supplied money map")

    if len(eligible_piles) >= 3:
        spec = [
            (eligible_piles[0], 1, 1),
            (eligible_piles[1], 2, 1),
            (eligible_piles[2], 3, 1),
        ]
    elif len(eligible_piles) == 2:
        spec = [
            (eligible_piles[0], 1, 1),
            (eligible_piles[1], 2, 1),
            (eligible_piles[0], 3, 2),
        ]
    else:  # len(eligible_piles) == 1
        spec = [
            (eligible_piles[0], 1, 1),
            (eligible_piles[0], 2, 2),
            (eligible_piles[0], 3, 3),
        ]

    plays_payload: list[dict[str, Any]] = []
    for (pile_id, p_rank, p_val), play_rank, variant in spec:
        if pile_id == "payment_rescue":
            play = _author_payment_rescue_play(
                packet,
                play_rank,
                p_rank,
                product=product,
                proof=proof,
                margin=margin,
                channel=channel,
                capacity=capacity,
                destination=destination,
                pile_value_str=p_val,
                variant=variant,
            )
            plays_payload.append(play)
        elif pile_id == "canceled_customer":
            play = _author_canceled_customer_play(
                packet,
                play_rank,
                p_rank,
                product=product,
                proof=proof,
                margin=margin,
                channel=channel,
                capacity=capacity,
                destination=destination,
                pile_value_str=p_val,
                variant=variant,
            )
            plays_payload.append(play)
        elif pile_id == "disappeared_high_value_customer":
            play = _author_disappeared_play(
                packet,
                play_rank,
                p_rank,
                product=product,
                proof=proof,
                margin=margin,
                channel=channel,
                capacity=capacity,
                destination=destination,
                pile_value_str=p_val,
                variant=variant,
            )
            plays_payload.append(play)

    basis_by_family = {pile.pile_id: pile.value_basis for pile in getattr(money_map, "piles", [])}
    wordings = load_table("recurring-valuation")["operator_basis_wording"]

    def align_grounded(value: Any, replacements: dict[str, str]) -> None:
        if isinstance(value, dict):
            if (
                "text" in value
                and "evidence_ids" in value
                and any(ref.endswith(("_value", "_basis")) for ref in value["evidence_ids"])
            ):
                for original, replacement in replacements.items():
                    value["text"] = value["text"].replace(original, replacement)
            for nested in value.values():
                align_grounded(nested, replacements)
        elif isinstance(value, list):
            for nested in value:
                align_grounded(nested, replacements)

    for payload in plays_payload:
        replacements = wordings.get(basis_by_family.get(payload["pile_id"], ""), {})
        align_grounded(payload, replacements)
        for field in ("rationale", "offer_recommendation"):
            for original, replacement in replacements.items():
                payload[field] = payload[field].replace(original, replacement)
    play_models = [CompleteRecoveryPlayV1.model_validate(p) for p in plays_payload]
    play_set = CompleteRecoveryPlaySetV1(
        run_id=packet.run_id,
        built_at=packet.built_at,
        provider=provider_name,
        plays=play_models,
    )
    validate_complete_recovery_play_set(play_set, packet)
    build_differentiation_report(play_set)
    return play_set


def generate_stub_recovery_plays(
    packet: GroundedStrategyEvidencePacketV1,
    profile: StrategyBusinessProfileV1,
    *,
    money_map: Any = None,
) -> CompleteRecoveryPlaySetV1:
    """Stub generator for CI and deterministic unit testing."""
    return generate_recovery_plays(packet, profile, money_map=money_map, provider_name="stub")
