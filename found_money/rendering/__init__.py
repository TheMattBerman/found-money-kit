"""Deterministic Recovery Room and print-report renderers."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, ROUND_HALF_UP
import os
import re
import tempfile
import hashlib
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from found_money.contracts.map import MoneyMapV1
from found_money.contracts.campaign import CompleteRecoveryPlaySetV1, CompleteRecoveryPlayV1
from found_money.contracts.rendering import (
    RecoveryRoomAssetV1,
    RecoveryRoomLinkV1,
    RecoveryRoomManifestV1,
)
from found_money.contracts.strategy import RecoveryPlaySetV1, RecoveryPlayV1
from found_money.contracts.value import format_major_units
from found_money.rendering.webgl_atlas import (
    _TEXTURE_URI_JS_EXPR,
    render_webgl_atlas_fragment,
    render_webgl_atlas_html,
)
from found_money.strategy import build_thin_slice_strategized_money_map

FIND_PROMISE = "Your next customers are already in the business."
FIND_HEADLINE_PREFIX = "We found"
UNQUANTIFIED_LABEL = "Unquantified"
NO_SEND_SENTENCE = "No messages are sent and no upstream system is changed."
FIND_WALKTHROUGH_NEXT = "Copy the first email, insert your own destination, and send by hand."
LAUNCH_CHECKLIST_ITEMS: tuple[tuple[str, str, frozenset[str], frozenset[str]], ...] = (
    ("audience", "Audience", frozenset({"private_segments"}), frozenset()),
    ("offer", "Offer", frozenset({"offer_landing_briefs"}), frozenset()),
    ("copy", "Copy", frozenset({"complete_copy"}), frozenset()),
    (
        "destination",
        "Destination",
        frozenset({"offer_landing_briefs"}),
        frozenset({"destination"}),
    ),
    ("tracking", "Tracking", frozenset({"tracking_plan"}), frozenset()),
    ("sender", "Sender", frozenset({"complete_copy"}), frozenset({"channel"})),
)
EVIDENCE_BASIS_LABELS: dict[str, str] = {
    "observed_face_value": "Observed",
    "modeled_opportunity": "Modeled",
    "unquantified": "Unquantified",
    "mixed": "Mixed",
}
PILE_DISPLAY_NAMES: dict[str, str] = {
    "payment_rescue": "Payment Rescue",
    "failed_payment": "Payment Rescue",
    "expired_trial": "Expired Trial",
    "trial_no_convert": "Second Start",
    "closed_lost_stale_deal": "Lost Deal Reopen",
    "canceled_customer": "Comeback Offer",
    "lapsed_repeat_buyer": "Lapsed Repeat Buyer",
    "silent_proposal": "Proposal Wake-Up",
    "no_show_rebook": "Easy Rebook",
    "disappeared_high_value_customer": "VIP Return",
    "overdue_reorder": "Right on Time",
    "renewal_upsell": "Next Best Move",
    "engaged_unbooked": "Finish the Booking",
}

_TRAVERSAL_RE = re.compile(r"(^|/)\.\.(/|$)")
_WINDOWS_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")
_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
_STATIC_DIR = Path(__file__).resolve().parent / "static"

_ENV = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=select_autoescape(enabled_extensions=("html", "htm", "xml")),
    trim_blocks=True,
    lstrip_blocks=True,
)

_PRINT_REQUIRED_PLAY_COUNT = 3
_PRINT_MISSING_TEXT = "Not present in this run"
_PRINT_URL_RE = re.compile(r"(?i)\bhttps?://\S+")
_PRINT_EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")


def _model_mapping(value: Any) -> dict[str, Any]:
    """Return a JSON-shaped mapping without coupling the renderer to one model revision."""
    if isinstance(value, Mapping):
        return dict(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(mode="json")
        if isinstance(dumped, Mapping):
            return dict(dumped)
    raw = getattr(value, "__dict__", None)
    if isinstance(raw, Mapping):
        return {key: item for key, item in raw.items() if not key.startswith("_")}
    return {}


def _first_value(mapping: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value is None or value == "" or value == []:
            continue
        return value
    return default


def _safe_print_text(value: Any) -> str:
    """Flatten a report value and remove unsafe destinations/contacts from public proof."""
    if value is None:
        return ""
    if isinstance(value, Mapping):
        # GroundedCopyV1 carries proof metadata beside its public text. The
        # report prints the supported copy, not evidence IDs or claim kinds.
        grounded_text = value.get("text")
        if isinstance(grounded_text, str):
            return _safe_print_text(grounded_text)
        pieces = []
        for key, item in value.items():
            text = _safe_print_text(item)
            if text:
                pieces.append(f"{str(key).replace('_', ' ').title()}: {text}")
        return "; ".join(pieces)
    if isinstance(value, (list, tuple)):
        return "; ".join(text for item in value if (text := _safe_print_text(item)))
    text = str(value).strip()
    text = _PRINT_URL_RE.sub("[destination withheld from public print proof]", text)
    return _PRINT_EMAIL_RE.sub("[contact withheld from public print proof]", text)


def _display_value(value: Any, *, fallback: str = _PRINT_MISSING_TEXT) -> str:
    text = _safe_print_text(value)
    return text or fallback


def _rows(value: Any, label: str) -> list[dict[str, str]]:
    """Normalize future play sequence shapes into a stable print-row shape."""
    if value is None or value == "" or value == []:
        return []
    if isinstance(value, Mapping):
        nested = _first_value(value, "items", "steps", "messages", "entries")
        if nested is not None:
            return _rows(nested, label)
        values = [value]
    elif isinstance(value, (list, tuple)):
        values = list(value)
    else:
        values = [value]

    rows: list[dict[str, str]] = []
    for index, item in enumerate(values):
        item_map = _model_mapping(item)
        row_label = _first_value(
            item_map,
            "label",
            "name",
            "channel",
            "stage",
            "day",
            "subject",
            "title",
            default=f"{label} {index + 1}",
        )
        if "objection" in item_map and "response" in item_map:
            row_text = (
                f"{_display_value(item_map['objection'])} — {_display_value(item_map['response'])}"
            )
        elif "action" in item_map and "stop_condition" in item_map:
            # The complete stop-condition list is printed separately below the
            # calendar on the same page, so do not duplicate it in every row.
            row_text = _display_value(item_map["action"])
        else:
            row_text = _first_value(
                item_map,
                "body",
                "copy",
                "text",
                "message",
                "description",
                "value",
                default=item,
            )
        rows.append(
            {
                "label": _display_value(row_label, fallback=f"{label} {index + 1}"),
                "text": _display_value(row_text),
            }
        )
    return rows


def _concept_card_context(card: Any, index: int) -> dict[str, Any]:
    data = _model_mapping(card)
    fields = (
        ("Audience tension", ("audience_tension", "tension")),
        ("Big idea", ("big_idea", "idea")),
        ("Hook", ("hook",)),
        ("Opening visual", ("first_three_seconds", "opening_visual", "opening")),
        ("Proof device", ("proof_device", "proof")),
        ("Format / style", ("format_style", "format", "style")),
        ("CTA", ("cta", "primary_cta")),
        ("Pile fit", ("pile_specific_fit", "pile_fit", "fit")),
        ("Production requirements", ("production_requirements", "requirements")),
    )
    return {
        "number": index + 1,
        "card_id": _display_value(_first_value(data, "card_id")),
        "name": _display_value(_first_value(data, "name", "card_name", "title")),
        "fields": [
            {"label": label, "value": _display_value(_first_value(data, *aliases))}
            for label, aliases in fields
        ],
    }


def _play_context(play: Any, pile_by_id: Mapping[str, Any]) -> dict[str, Any]:
    data = _model_mapping(play)
    pile_id = _display_value(_first_value(data, "pile_id"))
    pile = _model_mapping(pile_by_id.get(pile_id))
    value_basis = _first_value(data, "value_basis", default=pile.get("value_basis"))
    value_minor = _first_value(
        data, "selected_value_minor", default=pile.get("selected_value_minor")
    )
    currency = _first_value(data, "currency", default=pile.get("currency"))
    value_basis_text = _display_value(value_basis)
    if value_minor is not None and currency is not None:
        value_text = format_find_money(value_minor, currency)
    else:
        value_text = _PRINT_MISSING_TEXT
    concept_cards_raw = _first_value(data, "concept_cards", "concepts", default=[])
    concept_cards = [
        _concept_card_context(card, index) for index, card in enumerate(concept_cards_raw or [])
    ]
    campaign = _first_value(data, "campaign_name", "campaign", "strategy_name")
    strategy = _first_value(data, "strategy", "campaign_strategy")
    lifecycle_sequence = _first_value(data, "lifecycle_sequence")
    channel_emphasis = _first_value(data, "channel_emphasis")
    creative_big_idea = _first_value(data, "creative_big_idea")
    offer_raw = _first_value(data, "offer")
    offer_data = _model_mapping(offer_raw)
    offer_ladder = _model_mapping(_first_value(data, "offer_ladder"))
    if offer_ladder and not offer_data:
        rungs = offer_ladder.get("rungs") or []
        core = next(
            (_model_mapping(rung) for rung in rungs if _model_mapping(rung).get("role") == "core"),
            None,
        )
        if core:
            offer_data = core
            offer_raw = _model_mapping(core.get("mechanism"))
    offer = _first_value(
        data,
        "offer_mechanism",
        default=offer_data.get("mechanism") if offer_data else offer_raw,
    )
    offer_rationale = _first_value(data, "offer_rationale", default=offer_data.get("rationale"))
    constraints = _first_value(
        data, "constraints", "offer_constraints", default=offer_data.get("constraints")
    )
    calendar = _first_value(data, "calendar", "calendar_conditions")
    stop_conditions = _first_value(data, "stop_conditions", "stop_condition")
    tracking_raw = _first_value(data, "tracking")
    tracking_data = _model_mapping(tracking_raw)
    tracking = _first_value(
        data,
        "tracking_plan",
        default=tracking_data.get("tracked_signals") if tracking_data else tracking_raw,
    )
    success_event = _first_value(
        data, "success_event", "success", default=tracking_data.get("success_event")
    )

    required = (
        ("Campaign name / strategy", campaign or strategy),
        ("Value basis", value_basis),
        ("Audience", _first_value(data, "audience")),
        ("Recoverability", _first_value(data, "recoverability")),
        ("Diagnosis", _first_value(data, "diagnosis")),
        ("Offer and constraints", offer and constraints),
        ("Email sequence", _first_value(data, "email_sequence", "email")),
        ("Task / talk track", _first_value(data, "task_talk_track", "talk_track", "task")),
        ("CTA", _first_value(data, "cta", "primary_cta")),
        ("Objections", _first_value(data, "objections")),
        ("Urgency", _first_value(data, "urgency")),
        ("Calendar / stop conditions", calendar and stop_conditions),
        ("Tracking / success event", tracking and success_event),
        ("Three Concept Cards", len(concept_cards) >= 3),
    )
    missing = [label for label, value in required if not value]
    return {
        "play_id": _display_value(_first_value(data, "play_id")),
        "pile_id": pile_id,
        "rank": _display_value(_first_value(data, "rank")),
        "title": _display_value(_first_value(data, "title", "campaign_name")),
        "campaign": _display_value(campaign or strategy),
        "strategy": _display_value(strategy),
        "lifecycle_sequence": _display_value(lifecycle_sequence),
        "channel_emphasis": _display_value(channel_emphasis),
        "creative_big_idea": _display_value(creative_big_idea),
        "rationale": _display_value(_first_value(data, "rationale", "diagnosis")),
        "value": value_text,
        "value_basis": value_basis_text,
        "audience": _display_value(_first_value(data, "audience")),
        "recoverability": _display_value(_first_value(data, "recoverability")),
        "diagnosis": _display_value(_first_value(data, "diagnosis")),
        "offer": _display_value(offer),
        "offer_rationale": _display_value(offer_rationale),
        "constraints": _display_value(constraints),
        "email_sequence": _rows(_first_value(data, "email_sequence", "email"), "Email"),
        "sms": _rows(_first_value(data, "sms", "sms_sequence"), "SMS"),
        "task_talk_track": _rows(
            _first_value(data, "task_talk_track", "talk_track", "task"), "Task / talk track"
        ),
        "cta": _display_value(_first_value(data, "cta", "primary_cta")),
        "objections": _rows(_first_value(data, "objections"), "Objection"),
        "urgency": _display_value(_first_value(data, "urgency")),
        "calendar": _rows(calendar, "Calendar"),
        "stop_conditions": _rows(stop_conditions, "Stop condition"),
        "tracking": _display_value(tracking),
        "success_event": _display_value(success_event),
        "recommended_actions": [
            _display_value(item) for item in (_first_value(data, "recommended_actions") or [])
        ],
        "concept_cards": concept_cards,
        "missing": missing,
        "complete": not missing,
    }


def _display_minor_amount(value: object) -> str:
    """Format a contract minor-unit value without changing its meaning."""
    text = format(value, ",")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _display_play_title(title: str, pile_id: str) -> str:
    """Remove a redundant fixture pile suffix from the visual heading only."""
    suffix = f" for {pile_id}"
    if title.casefold().endswith(suffix.casefold()):
        return title[: -len(suffix)]
    return title


def _public_safe_scan(html: str) -> None:
    forbidden = (
        "customer_token",
        "economic_unit_key",
        "external_ids",
        "inv_failed_001",
        "cus_synth_001",
        "qualifying_evidence",
    )
    for token in forbidden:
        if token in html:
            raise ValueError(f"rendered HTML contains forbidden identity token: {token}")
    from found_money.activation.intake import html_for_public_scan

    scrubbed = html_for_public_scan(html)
    if re.search(r"(?i)\bhttps?://", scrubbed) or "?=" in scrubbed:
        raise ValueError("rendered HTML contains URL/query string")
    if re.search(r"(?i)(/Users/|/home/|[A-Za-z]:\\)", html):
        raise ValueError("rendered HTML contains absolute local path")
    if re.search(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", html):
        raise ValueError("rendered HTML contains email")


def render_money_map_html(money_map: MoneyMapV1) -> str:
    """Render the Money Map overview HTML from public-safe map fields."""
    identified = [
        {
            "currency": currency,
            "amount_minor": str(amount),
            "amount_display": _display_minor_amount(amount),
        }
        for currency, amount in sorted(money_map.identified_opportunity_minor.items())
    ]
    basis = [
        {
            "currency": currency,
            "observed_event_count": counts.observed_event_count,
            "modeled_event_count": counts.modeled_event_count,
            "unquantified_event_count": counts.unquantified_event_count,
        }
        for currency, counts in sorted(money_map.basis_counts_by_currency.items())
    ]
    piles = [
        {
            "pile_id": pile.pile_id,
            "rank": pile.rank,
            "currency": pile.currency,
            "selected_value_minor": str(pile.selected_value_minor),
            "amount_display": _display_minor_amount(pile.selected_value_minor),
            "value_basis": pile.value_basis,
            "confidence_class": pile.confidence_class,
            "customer_count": pile.customer_count,
            "economic_unit_count": pile.economic_unit_count,
            "source_count": pile.source_count if pile.source_count is not None else 0,
            "readiness": pile.readiness or "needs_strategy_review",
            "why_recoverable": pile.why_recoverable,
            "navigation_target_id": (
                pile.navigation.target_id
                if pile.navigation is not None
                else f"pile/{pile.pile_id}/{pile.currency}"
            ),
            "navigation_state": (
                pile.navigation.state if pile.navigation is not None else "deferred"
            ),
            "navigation_play_id": (
                pile.navigation.play_id if pile.navigation is not None else None
            ),
            "navigation_next_action": (
                pile.navigation.next_action
                if pile.navigation is not None
                else "review_strategy_when_available"
            ),
            "rank_explanation": (
                {
                    "total_score": str(pile.rank_explanation.total_score),
                    "tie_break_key": pile.rank_explanation.tie_break_key,
                    "factors": [
                        {
                            "name": factor.name,
                            "score": str(factor.score),
                            "weight": str(factor.weight),
                            "contribution": str(factor.contribution),
                            "source_count": factor.source_count,
                            "explanation": factor.explanation,
                        }
                        for factor in pile.rank_explanation.factors
                    ],
                }
                if pile.rank_explanation is not None
                else None
            ),
        }
        for pile in sorted(money_map.piles, key=lambda item: item.rank)
    ]
    html = _ENV.get_template("money_map.html").render(
        run_id=money_map.run_id,
        strategy_stage=money_map.strategy_stage,
        identified_opportunity=identified,
        basis_counts=basis,
        piles=piles,
        recommended_play_ids=list(money_map.recommended_play_ids),
        customer_count=money_map.customer_count,
        source_count=money_map.source_count,
        data_gap_count=money_map.data_gap_count,
        overlap_exclusion_count=money_map.overlap_exclusion_count,
        named_data_gaps=list(getattr(money_map, "named_data_gaps", None) or []),
        event_exclusions_by_reason=dict(
            getattr(money_map, "event_exclusions_by_reason", None) or {}
        ),
        next_action=money_map.next_action or "review_data_gaps",
    )
    if "Money Map" not in html:
        raise ValueError("money map HTML missing required title text")
    _public_safe_scan(html)
    return html


def _select_top_play(money_map: MoneyMapV1, play_set: RecoveryPlaySetV1) -> RecoveryPlayV1:
    if money_map.run_id != play_set.run_id:
        raise ValueError("money map run_id must match recovery play set run_id")
    if not money_map.recommended_play_ids:
        raise ValueError("recommended_play_ids is empty; refusing top-play HTML")
    selected_id = money_map.recommended_play_ids[0]
    matched = next(
        (candidate for candidate in play_set.plays if candidate.play_id == selected_id), None
    )
    if matched is None:
        raise ValueError(f"selected play id missing from play set: {selected_id}")
    play = matched
    pile_ids = {pile.pile_id for pile in money_map.piles}
    if play.pile_id not in pile_ids and money_map.strategy_stage != "needs_strategy_review":
        raise ValueError(f"play references unknown pile_id: {play.pile_id}")
    return play


def render_top_play_html(money_map: MoneyMapV1, play_set: RecoveryPlaySetV1) -> str:
    """Render the top-play HTML from public-safe play fields."""
    if money_map.run_id != play_set.run_id:
        raise ValueError("money map run_id must match recovery play set run_id")
    if (
        not money_map.recommended_play_ids or not play_set.plays
    ) and money_map.strategy_stage == "needs_strategy_review":
        html = _ENV.get_template("top_play_deferred.html").render(
            run_id=money_map.run_id,
            strategy_stage=money_map.strategy_stage,
            named_data_gaps=list(getattr(money_map, "named_data_gaps", None) or []),
        )
        if "Top Play" not in html:
            raise ValueError("top play HTML missing required title text")
        _public_safe_scan(html)
        return html
    play = _select_top_play(money_map, play_set)
    pile = next((item for item in money_map.piles if item.pile_id == play.pile_id), None)
    if pile is None:
        pile = sorted(money_map.piles, key=lambda item: item.rank)[0]
    html = _ENV.get_template("top_play.html").render(
        run_id=money_map.run_id,
        play_id=play.play_id,
        pile_id=play.pile_id,
        rank=play.rank,
        title=play.title,
        display_title=_display_play_title(play.title, play.pile_id),
        rationale=play.rationale,
        action_labels=list(play.recommended_actions),
        pile={
            "currency": pile.currency,
            "amount_display": _display_minor_amount(pile.selected_value_minor),
            "value_basis": pile.value_basis,
            "customer_count": pile.customer_count,
            "source_count": pile.source_count if pile.source_count is not None else 0,
            "confidence_class": pile.confidence_class,
            "why_recoverable": pile.why_recoverable,
            "navigation_state": (
                pile.navigation.state if pile.navigation is not None else "deferred"
            ),
        },
    )
    if "Top Play" not in html:
        raise ValueError("top play HTML missing required title text")
    _public_safe_scan(html)
    return html


def render_print_report_html(
    money_map: MoneyMapV1,
    play_set: RecoveryPlaySetV1 | CompleteRecoveryPlaySetV1,
    *,
    contribution_ledger: Any | None = None,
) -> str:
    """Render the traceable print packet without changing interactive pages.

    The print packet supports both the legacy thin fixture and the canonical
    three-play campaign contract. Incomplete inputs remain a visible dependency
    hold instead of causing missing plays or cards to be invented.
    """
    if money_map.run_id != play_set.run_id:
        raise ValueError("money map run_id must match recovery play set run_id")
    identified = [
        {
            "currency": currency,
            "amount_minor": str(amount),
            "amount_display": format_find_money(amount, currency),
        }
        for currency, amount in sorted(money_map.identified_opportunity_minor.items())
    ]
    basis = [
        {
            "currency": currency,
            "observed_event_count": counts.observed_event_count,
            "modeled_event_count": counts.modeled_event_count,
            "unquantified_event_count": counts.unquantified_event_count,
        }
        for currency, counts in sorted(money_map.basis_counts_by_currency.items())
    ]
    pile_by_id: dict[str, Any] = {str(pile.pile_id): pile for pile in money_map.piles}
    plays = [
        _play_context(play, pile_by_id)
        for play in sorted(
            play_set.plays,
            key=lambda item: _first_value(_model_mapping(item), "rank", default=0),
        )
    ]
    complete_play_count = sum(1 for play in plays if play["complete"])
    report_complete = len(plays) == _PRINT_REQUIRED_PLAY_COUNT and complete_play_count == len(plays)
    report_status = "ready-for-human-review" if report_complete else "dependency-hold"
    report_status_label = (
        "Ready for fresh human print review"
        if report_complete
        else "Dependency hold — upstream strategy output is incomplete"
    )
    piles = [
        {
            "pile_id": pile.pile_id,
            "display_name": pile_display_name(pile.pile_id),
            "rank": pile.rank,
            "value": format_find_money(pile.selected_value_minor, pile.currency),
            "basis": pile.value_basis,
            "confidence": pile.confidence_class,
        }
        for pile in sorted(money_map.piles, key=lambda item: item.rank)
    ]
    if contribution_ledger is not None:
        ranked = [row["rank"] for row in piles if isinstance(row["rank"], int)]
        next_rank = (max(ranked) if ranked else 0) + 1
        for pile_id in unquantified_find_pile_ids(contribution_ledger, money_map):
            piles.append(
                {
                    "pile_id": pile_id,
                    "display_name": pile_display_name(pile_id),
                    "rank": next_rank,
                    "value": UNQUANTIFIED_LABEL,
                    "basis": "unquantified",
                    "confidence": "unquantified",
                }
            )
            next_rank += 1
    html = _ENV.get_template("print_report.html").render(
        run_id=money_map.run_id,
        strategy_stage=money_map.strategy_stage,
        identified_opportunity=identified,
        find_promise=FIND_PROMISE,
        pile_count=len(piles),
        customer_count=money_map.customer_count if money_map.customer_count is not None else 0,
        play_count=len(plays),
        basis_counts=basis,
        piles=piles,
        separate_pile_pages=len(piles) > 7 or len(identified) > 1,
        pile_pages=[piles[i : i + 8] for i in range(0, len(piles), 8)],
        plays=plays,
        available_play_count=len(plays),
        required_play_count=_PRINT_REQUIRED_PLAY_COUNT,
        complete_play_count=complete_play_count,
        report_complete=report_complete,
        report_status=report_status,
        report_status_label=report_status_label,
        print_missing_text=_PRINT_MISSING_TEXT,
        data_gap_count=money_map.data_gap_count,
        overlap_exclusion_count=money_map.overlap_exclusion_count,
        named_data_gaps=list(getattr(money_map, "named_data_gaps", None) or []),
        event_exclusions_by_reason=dict(
            getattr(money_map, "event_exclusions_by_reason", None) or {}
        ),
        next_action=money_map.next_action or "review_strategy_when_available",
    )
    if "Money Map" not in html:
        raise ValueError("print report HTML missing Money Map title text")
    for play in plays:
        if play["play_id"] not in html:
            raise ValueError(f"print report HTML missing play id: {play['play_id']}")
    if 'data-print-report="fm029"' not in html:
        raise ValueError("print report HTML missing FM-029 contract marker")
    _public_safe_scan(html)
    return html


def recovery_room_static_assets() -> dict[str, bytes]:
    """Return the complete local-only asset set copied beside index.html."""
    assets = {
        f"assets/{path.name}": path.read_bytes()
        for path in sorted(_STATIC_DIR.iterdir())
        if path.is_file()
    }
    vendor_dir = Path(__file__).resolve().parent / "static" / "vendor" / "three"
    for name in ("three.min.js", "OrbitControls.js", "tween.umd.js"):
        assets[f"assets/{name}"] = (vendor_dir / name).read_bytes()
    assets["assets/continent-texture.jpg"] = (
        Path(__file__).resolve().parent / "static" / "atlas" / "continent-texture.jpg"
    ).read_bytes()
    assets["assets/atlas-texture.js"] = (
        "window.FoundMoneyAtlasTexture = " + _TEXTURE_URI_JS_EXPR + ";\n"
    ).encode("utf-8")
    if set(assets) != {
        "assets/recovery-room.css",
        "assets/recovery-room.js",
        "assets/three.min.js",
        "assets/OrbitControls.js",
        "assets/tween.umd.js",
        "assets/continent-texture.jpg",
        "assets/atlas-texture.js",
    }:
        raise ValueError("Recovery Room local asset set is incomplete")
    return assets


def _room_plays(play_set: RecoveryPlaySetV1 | CompleteRecoveryPlaySetV1) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    plays: list[RecoveryPlayV1 | CompleteRecoveryPlayV1] = list(play_set.plays)
    for play in sorted(plays, key=lambda item: item.rank):
        if isinstance(play, CompleteRecoveryPlayV1):
            campaign_strategy = play.campaign_strategy.text
            value_basis = play.value_basis.text
            audience = play.audience.text
            recoverability = play.recoverability.text
            diagnosis = play.diagnosis.text
            core_rung = next(rung for rung in play.offer_ladder.rungs if rung.role == "core")
            offer = {
                "recommendation": play.offer_recommendation,
                "ladder_id": play.offer_ladder.offer_ladder_id,
                "selected_rung_id": play.offer_ladder.selected_offer_rung_id,
                "mechanism": core_rung.mechanism.text,
                "rationale": core_rung.rationale.text,
                "constraints": [item.text for item in core_rung.constraints],
                "rungs": [
                    {
                        "id": rung.id,
                        "role": rung.role,
                        "mechanism": rung.mechanism.text,
                        "terms": rung.terms.text,
                        "rationale": rung.rationale.text,
                        "is_recommended": rung.role == "core",
                        "is_save_path": rung.role == "downsell",
                    }
                    for rung in play.offer_ladder.rungs
                ],
            }
            lifecycle_sequence = play.lifecycle_sequence.text
            email_sequence = [
                {
                    "order": item.order,
                    "lifecycle_stage": getattr(item.lifecycle_stage, "text", item.lifecycle_stage),
                    "subject": item.subject.text,
                    "body": item.body.text,
                    "cta": item.cta.text,
                }
                for item in play.email_sequence
            ]
            sms = {
                "available": play.sms.available,
                "messages": [item.text for item in play.sms.messages],
                "unavailable_reason": play.sms.unavailable_reason,
            }
            task_talk_track = play.task_talk_track.text
            primary_cta = play.primary_cta.text
            objections = [
                {"objection": item.objection.text, "response": item.response.text}
                for item in play.objections
            ]
            urgency = play.urgency.text
            calendar = [
                {
                    "day": item.day,
                    "action": item.action.text,
                    "stop_condition": item.stop_condition.text,
                }
                for item in play.calendar
            ]
            stop_conditions = "; ".join(item.text for item in play.stop_conditions)
            tracking = {
                "success_event": play.tracking.success_event.text,
                "tracked_signals": [item.text for item in play.tracking.tracked_signals],
            }
            channel_emphasis = play.channel_emphasis.text
            creative_big_idea = play.creative_big_idea.text
            cards = [
                {
                    "card_id": card.card_id,
                    "card_name": card.card_name,
                    "audience_tension": card.audience_tension.text,
                    "big_idea": card.big_idea.text,
                    "hook": card.hook.text,
                    "opening_visual": card.opening_visual.text,
                    "proof_device": card.proof_device.text,
                    "format_style": card.format_style,
                    "cta": card.cta.text,
                    "pile_fit": card.pile_fit.text,
                    "production_requirements": card.production_requirements,
                }
                for card in play.concept_cards
            ]
        else:
            campaign_strategy = ""
            value_basis = ""
            audience = ""
            recoverability = ""
            diagnosis = ""
            offer = {}
            lifecycle_sequence = ""
            email_sequence = []
            sms = {}
            task_talk_track = ""
            primary_cta = ""
            objections = []
            urgency = "needs_strategy_review"
            calendar = []
            stop_conditions = ""
            tracking = {}
            channel_emphasis = ""
            creative_big_idea = ""
            cards = []
        rows.append(
            {
                "play_id": play.play_id,
                "pile_id": play.pile_id,
                "title": play.title,
                "rationale": play.rationale,
                "complete": isinstance(play, CompleteRecoveryPlayV1),
                "campaign_strategy": campaign_strategy,
                "value_basis": value_basis,
                "audience": audience,
                "recoverability": recoverability,
                "diagnosis": diagnosis,
                "offer": offer,
                "lifecycle_sequence": lifecycle_sequence,
                "email_sequence": email_sequence,
                "sms": sms,
                "task_talk_track": task_talk_track,
                "primary_cta": primary_cta,
                "objections": objections,
                "urgency": urgency,
                "calendar": calendar,
                "stop_conditions": stop_conditions,
                "tracking": tracking,
                "channel_emphasis": channel_emphasis,
                "creative_big_idea": creative_big_idea,
                "cards": cards,
            }
        )
    return rows


def pile_display_name(pile_id: str) -> str:
    """Return the locked Find label for a machine pile id."""
    return PILE_DISPLAY_NAMES.get(pile_id, pile_id)


def evidence_basis_label(value_basis: str) -> str:
    """Return the Evidence-room Observed / Modeled / Unquantified label."""
    return EVIDENCE_BASIS_LABELS.get(value_basis, value_basis)


def launch_calendar_rows(plays: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten complete-play calendar rows for The Launch, preserving play order."""
    rows: list[dict[str, Any]] = []
    for play in plays:
        label = str(play.get("pile_display_name") or play.get("title") or play["play_id"])
        for item in play.get("calendar") or []:
            rows.append(
                {
                    "play_id": play["play_id"],
                    "label": label,
                    "day": item["day"],
                    "action": item["action"],
                    "stop_condition": item["stop_condition"],
                }
            )
    return rows


def launch_checklist_rows(withheld_assets: list[str] | None) -> list[dict[str, str]]:
    """Derive the six Launch checklist rows from withheld-asset flags."""
    classes: set[str] = set()
    gaps: set[str] = set()
    for asset in withheld_assets or []:
        head, separator, rest = asset.partition(":")
        if head:
            classes.add(head)
        if separator and rest:
            gaps.add(rest.rsplit(":", 1)[-1])
    rows: list[dict[str, str]] = []
    for key, label, asset_classes, extra_gaps in LAUNCH_CHECKLIST_ITEMS:
        withheld = bool(classes & asset_classes) or bool(gaps & extra_gaps)
        rows.append(
            {
                "key": key,
                "label": label,
                "status": "withheld" if withheld else "ready",
                "display": f"{label}: withheld" if withheld else label,
            }
        )
    return rows


def format_find_money(amount_minor: Any, currency: Any) -> str:
    """Format source money without converting or relabeling currencies."""
    amount = format_major_units(amount_minor, currency)
    whole, dot, fraction = amount.partition(".")
    amount = f"{int(whole):,}" + (dot + fraction if dot else "")
    return f"${amount}" if str(currency).lower() == "usd" else f"{str(currency).upper()} {amount}"


def unquantified_find_pile_ids(ledger: Any, money_map: MoneyMapV1) -> list[str]:
    """Machine pile ids that have a value gap and no quantified map pile."""
    existing = {pile.pile_id for pile in money_map.piles}
    found: list[str] = []
    for gap in getattr(ledger, "data_gaps", None) or []:
        family = getattr(gap, "event_family", None)
        if not isinstance(family, str) or not family.strip():
            continue
        pile_id = family.strip()
        if pile_id in existing or pile_id in found:
            continue
        if getattr(gap, "reason_code", None) == "overlap_excluded":
            continue
        found.append(pile_id)
    return found


def _share_of_total_label(amount_minor: Decimal, total_minor: Decimal) -> str | None:
    """Percent of rendered quantified pile totals; omitted when the share is undefined."""
    if total_minor <= 0:
        return None
    percent = (amount_minor * Decimal(100) / total_minor).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    )
    return f"{int(percent)}% of the find"


def _bar_style(amount_minor: Decimal | None, max_minor: Decimal) -> str:
    if amount_minor is None or max_minor <= 0:
        return "--w:0%"
    percent = (amount_minor * Decimal(100) / max_minor).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    )
    return f"--w:{int(percent)}%"


def _annotate_find_piles(piles: list[dict[str, Any]]) -> None:
    """Attach share-of-total labels and magnitude-bar widths from rendered pile totals."""
    quantified: list[Decimal] = []
    for pile in piles:
        raw = pile.get("selected_value_minor")
        if raw is None:
            continue
        quantified.append(Decimal(raw))
    total = sum(quantified, Decimal(0))
    peak = max(quantified) if quantified else Decimal(0)
    for pile in piles:
        raw = pile.get("selected_value_minor")
        amount = Decimal(raw) if raw is not None else None
        pile["share_label"] = _share_of_total_label(amount, total) if amount is not None else None
        pile["bar_style"] = _bar_style(amount, peak)


def _find_walkthrough(
    piles: list[dict[str, Any]], plays: list[dict[str, Any]]
) -> dict[str, str] | None:
    if not piles:
        return None
    top = piles[0]
    selected = next(
        (
            play
            for play in plays
            if play.get("complete") and play.get("play_id") == top.get("play_id")
        ),
        None,
    )
    if selected is None:
        selected = next((play for play in plays if play.get("complete")), None)
    constraints = []
    if selected:
        offer = selected.get("offer") or {}
        constraints = list(offer.get("constraints") or [])
    return {
        "why_recoverable": str(top.get("why_recoverable") or ""),
        "constraints": "; ".join(constraints),
        "next_step": FIND_WALKTHROUGH_NEXT,
    }


def _pile_amount_label(amount_minor: Any, currency: Any, *, quantified: bool) -> str:
    if not quantified:
        return UNQUANTIFIED_LABEL
    amount = format_major_units(amount_minor, currency)
    if amount in {"0", "0.0", "0.00", "0.000"}:
        return UNQUANTIFIED_LABEL
    return f"${amount}" if str(currency).lower() == "usd" else f"{str(currency).upper()} {amount}"


def _find_headline(money_map: MoneyMapV1) -> str:
    amounts = [
        format_find_money(amount, currency)
        for currency, amount in sorted(money_map.identified_opportunity_minor.items())
    ]
    if not amounts:
        return f"{FIND_HEADLINE_PREFIX} no quantified opportunity"
    return f"{FIND_HEADLINE_PREFIX} {'; '.join(amounts)}"


def render_recovery_room_html(
    money_map: MoneyMapV1,
    play_set: RecoveryPlaySetV1 | CompleteRecoveryPlaySetV1,
    *,
    launch_status: str = "not_started",
    withheld_assets: list[str] | None = None,
    handoff_actions: Any | None = None,
    contribution_ledger: Any | None = None,
    synthetic_demo: bool = False,
) -> str:
    """Render all four offline rooms with core content visible without JavaScript."""
    if money_map.run_id != play_set.run_id:
        raise ValueError("money map run_id must match recovery play set run_id")
    plays = _room_plays(play_set)
    pile_by_id = {pile.pile_id: pile for pile in money_map.piles}
    for play in plays:
        mapped = pile_by_id.get(play["pile_id"])
        play["pile_display_name"] = pile_display_name(play["pile_id"])
        if mapped is None:
            play["amount_label"] = UNQUANTIFIED_LABEL
        else:
            play["amount_label"] = _pile_amount_label(
                mapped.selected_value_minor, mapped.currency, quantified=True
            )
    play_ids = {play["play_id"] for play in plays}
    piles = [
        {
            "pile_id": pile.pile_id,
            "display_name": pile_display_name(pile.pile_id),
            "rank": pile.rank,
            "currency": pile.currency,
            "selected_value_minor": str(pile.selected_value_minor),
            "amount_label": _pile_amount_label(
                pile.selected_value_minor, pile.currency, quantified=True
            ),
            "value_basis": pile.value_basis,
            "basis_label": evidence_basis_label(pile.value_basis),
            "confidence_class": pile.confidence_class,
            "why_recoverable": pile.why_recoverable,
            "readiness": pile.readiness or "needs_strategy_review",
            "play_id": (
                pile.navigation.play_id
                if pile.navigation is not None and pile.navigation.play_id in play_ids
                else None
            ),
        }
        for pile in sorted(money_map.piles, key=lambda item: item.rank)
    ]
    next_rank = len(piles) + 1
    for pile_id in unquantified_find_pile_ids(contribution_ledger, money_map):
        piles.append(
            {
                "pile_id": pile_id,
                "display_name": pile_display_name(pile_id),
                "rank": next_rank,
                "currency": "usd",
                "selected_value_minor": None,
                "amount_label": UNQUANTIFIED_LABEL,
                "value_basis": "unquantified",
                "basis_label": evidence_basis_label("unquantified"),
                "confidence_class": "mixed",
                "why_recoverable": "Source value is missing, so this pile stays unquantified.",
                "readiness": "needs_data",
                "play_id": None,
            }
        )
        next_rank += 1
    _annotate_find_piles(piles)
    walkthrough = _find_walkthrough(piles, plays)
    handoff = _handoff_template_context(handoff_actions)
    atlas_fragment = render_webgl_atlas_fragment(
        {
            "money_map": money_map,
            "play_set": play_set,
            "piles": piles,
            "contribution_ledger": contribution_ledger,
        }
    )
    html_text = _ENV.get_template("recovery_room.html").render(
        run_id=money_map.run_id,
        synthetic_demo=synthetic_demo,
        strategy_stage=money_map.strategy_stage,
        find_promise=FIND_PROMISE,
        find_headline=(
            f"{FIND_HEADLINE_PREFIX} {format_find_money(money_map.identified_opportunity_minor['usd'], 'usd')}"
            if "usd" in money_map.identified_opportunity_minor
            and len(money_map.identified_opportunity_minor) > 1
            else _find_headline(money_map)
        ),
        find_other_currencies=[
            format_find_money(amount, currency)
            for currency, amount in sorted(money_map.identified_opportunity_minor.items())
            if currency != "usd"
        ]
        if "usd" in money_map.identified_opportunity_minor
        and len(money_map.identified_opportunity_minor) > 1
        else [],
        find_pile_count=len(piles),
        find_play_count=len(plays),
        atlas_fragment=atlas_fragment,
        identified_opportunity=[
            {
                "currency": currency,
                "amount_minor": str(amount),
                "amount_display": format_find_money(amount, currency),
            }
            for currency, amount in sorted(money_map.identified_opportunity_minor.items())
        ],
        basis_counts=[
            {
                "currency": currency,
                "observed_event_count": counts.observed_event_count,
                "modeled_event_count": counts.modeled_event_count,
                "unquantified_event_count": counts.unquantified_event_count,
            }
            for currency, counts in sorted(money_map.basis_counts_by_currency.items())
        ],
        piles=piles,
        walkthrough=walkthrough,
        plays=plays,
        customer_count=money_map.customer_count,
        source_count=money_map.source_count,
        overlap_exclusion_count=money_map.overlap_exclusion_count,
        data_gap_count=money_map.data_gap_count,
        named_data_gaps=list(getattr(money_map, "named_data_gaps", None) or []),
        event_exclusions_by_reason=dict(
            getattr(money_map, "event_exclusions_by_reason", None) or {}
        ),
        launch_status=launch_status,
        withheld_assets=withheld_assets or [],
        launch_calendar=launch_calendar_rows(plays),
        launch_checklist=launch_checklist_rows(withheld_assets),
        no_send_sentence=NO_SEND_SENTENCE,
        handoff=handoff,
    )
    for room_id in ("room-find", "room-evidence", "room-play", "room-launch"):
        if html_text.count(f'id="{room_id}"') != 1:
            raise ValueError(f"Recovery Room missing unique room id: {room_id}")
    _public_safe_scan(html_text)
    return html_text


def _handoff_template_context(handoff_actions: Any | None) -> dict[str, Any]:
    """Normalize optional handoff actions for The Launch room."""
    from found_money.activation.intake import coerce_handoff_actions

    handoff_actions = coerce_handoff_actions(handoff_actions)
    stealads = handoff_actions.stealads
    matt = handoff_actions.matt_emerald
    return {
        "stealads": {
            "configured": stealads.configured,
            "label": stealads.label,
            "intake_url": stealads.intake_url,
            "export_href": stealads.export_href,
            "instructions": stealads.instructions,
        },
        "matt_emerald": {
            "configured": matt.configured,
            "label": matt.label,
            "intake_url": matt.intake_url,
            "export_href": matt.export_href,
            "instructions": matt.instructions,
        },
    }


def build_recovery_room_manifest(
    run_id: str,
    html_text: str,
    assets: dict[str, bytes],
) -> RecoveryRoomManifestV1:
    """Hash the local resource set and record the four resolvable room links."""
    actions = {
        "room-find": "#room-evidence",
        "room-evidence": "#room-play",
        "room-play": "#room-launch",
        "room-launch": "#room-find",
    }
    for room_id, target in actions.items():
        section = re.search(
            rf'<section id="{re.escape(room_id)}".*?</section>', html_text, re.DOTALL
        )
        if section is None or section.group(0).count("data-recommended-action") != 1:
            raise ValueError(f"room must contain exactly one recommended action: {room_id}")
        if f'href="{target}"' not in section.group(0):
            raise ValueError(f"room action target is not resolvable: {room_id}")
    return RecoveryRoomManifestV1(
        run_id=run_id,
        room_ids=list(actions),
        room_headings={
            "room-find": "The Find",
            "room-evidence": "The Evidence",
            "room-play": "The Play",
            "room-launch": "The Launch",
        },
        recommended_actions=actions,
        links=[
            RecoveryRoomLinkV1(source_room_id=room_id, target=target)
            for room_id, target in actions.items()
        ],
        local_assets=[
            RecoveryRoomAssetV1(path=path, sha256=hashlib.sha256(payload).hexdigest())
            for path, payload in sorted(assets.items())
        ],
        linked_artifacts=[
            "money-map.json",
            "recovery-plays.json",
            "provenance/run-manifest.json",
            "launch-pack/manifest.json",
        ],
    )


def build_thin_slice_recovery_room(
    *,
    run_id: str = "run_thin_slice_recovery_room",
) -> tuple[MoneyMapV1, RecoveryPlaySetV1, str, str]:
    """Compose FM-005 thin-slice artifacts into Recovery Room HTML."""
    money_map, _packet, play_set = build_thin_slice_strategized_money_map(run_id=run_id)
    index_html = render_money_map_html(money_map)
    top_play_html = render_top_play_html(money_map, play_set)
    return money_map, play_set, index_html, top_play_html


def _validate_relative_under_root(output_root: Path, relative_path: str) -> Path:
    if not isinstance(relative_path, str):
        raise ValueError("relative path must be a string")
    raw = relative_path.strip().replace("\\", "/")
    if not raw:
        raise ValueError("relative path must be non-empty")
    if raw.startswith("/") or _WINDOWS_ABS_RE.match(raw) or raw.startswith("~/"):
        raise ValueError("absolute paths are rejected")
    if raw.startswith("./"):
        raw = raw[2:]
    if raw.endswith("/"):
        raw = raw[:-1]
    if not raw or _TRAVERSAL_RE.search(raw) or raw == ".." or Path(raw).is_absolute():
        raise ValueError("traversal or absolute paths are rejected")
    root = output_root.expanduser().resolve(strict=False)
    candidate = root.joinpath(*Path(raw).parts)
    try:
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise ValueError("path escapes the caller-owned output root") from exc
    probe = root
    for part in Path(raw).parts[:-1]:
        probe = probe / part
        if probe.is_symlink():
            try:
                probe.resolve(strict=True).relative_to(root)
            except (OSError, ValueError) as exc:
                raise ValueError("symlink escapes the caller-owned output root") from exc
        if probe.exists() and not probe.is_dir():
            raise ValueError("parent path component is not a directory")
    if candidate.exists() and candidate.is_symlink():
        try:
            candidate.resolve(strict=True).relative_to(root)
        except (OSError, ValueError) as exc:
            raise ValueError("symlink escapes the caller-owned output root") from exc
    return candidate


def _atomic_write_bytes(destination: Path, payload: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=str(destination.parent),
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, destination)
    except Exception:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)
        raise


def write_recovery_room(
    output_root: Path | str,
    index_html: str,
    top_play_html: str,
    *,
    relative_dir: str = "recovery-room",
) -> tuple[Path, Path]:
    """Atomically write Recovery Room HTML under a caller-owned output root."""
    if not isinstance(index_html, str) or not isinstance(top_play_html, str):
        raise TypeError("HTML payloads must be strings")
    if not isinstance(relative_dir, str):
        raise ValueError("relative directory must be a string")
    raw_dir = relative_dir.strip().replace("\\", "/")
    if raw_dir.endswith("/"):
        raw_dir = raw_dir[:-1]
    if not raw_dir:
        raise ValueError("relative directory must be non-empty")
    index_dest = _validate_relative_under_root(Path(output_root), f"{raw_dir}/index.html")
    top_dest = _validate_relative_under_root(Path(output_root), f"{raw_dir}/top-play.html")
    _atomic_write_bytes(index_dest, index_html.encode("utf-8"))
    try:
        _atomic_write_bytes(top_dest, top_play_html.encode("utf-8"))
    except Exception:
        if index_dest.exists():
            index_dest.unlink(missing_ok=True)
        raise
    return index_dest, top_dest


def __getattr__(name: str):
    if name in {
        "build_canonical_four_room_visual_proof",
        "build_thin_slice_recovery_room_proof",
        "capture_four_room_screenshots",
        "capture_recovery_room_artifacts",
        "capture_print_report_artifacts",
        "normalize_pdf_metadata",
        "read_baseline_ratification_status",
        "resolve_render_baselines_dir",
        "validate_pdf_bytes",
        "validate_print_review_packet",
        "validate_png_bytes",
        "write_named_png_baselines",
        "write_render_proof_metadata",
        "write_recovery_room_artifacts",
    }:
        from found_money.rendering import proof

        return getattr(proof, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "build_recovery_room_manifest",
    "build_canonical_four_room_visual_proof",
    "build_thin_slice_recovery_room",
    "build_thin_slice_recovery_room_proof",
    "capture_four_room_screenshots",
    "capture_recovery_room_artifacts",
    "capture_print_report_artifacts",
    "normalize_pdf_metadata",
    "read_baseline_ratification_status",
    "resolve_render_baselines_dir",
    "FIND_WALKTHROUGH_NEXT",
    "evidence_basis_label",
    "format_find_money",
    "launch_calendar_rows",
    "launch_checklist_rows",
    "pile_display_name",
    "render_money_map_html",
    "render_recovery_room_html",
    "render_webgl_atlas_fragment",
    "render_webgl_atlas_html",
    "unquantified_find_pile_ids",
    "render_print_report_html",
    "render_top_play_html",
    "validate_pdf_bytes",
    "validate_print_review_packet",
    "validate_png_bytes",
    "write_named_png_baselines",
    "write_render_proof_metadata",
    "write_recovery_room",
    "write_recovery_room_artifacts",
    "recovery_room_static_assets",
]
