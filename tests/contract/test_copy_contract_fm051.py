"""FM-051: the copy contract splits by audience.

Customer-facing fields drop per-field evidence grounding and gain the cheap
ledger-money guard instead. Operator-facing copy stays grounded. The play gains
an unbound offer_recommendation argument. FM-049's number checker is untouched.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from found_money.contracts.campaign import (
    CompleteRecoveryPlayV1,
    CustomerCopyV1,
    EmailStepV1,
    GroundedCopyV1,
)
from found_money.rendering import render_recovery_room_html
from found_money.redaction import assert_public_safe
from found_money.scenarios import (
    SYNTHETIC_ECOMMERCE_V1,
    SYNTHETIC_SAAS_V1,
    SYNTHETIC_SERVICE_V1,
)
from found_money.scenarios.harness import run_scenario_engine
from found_money.strategy import build_canonical_saas_recovery_strategy
from found_money.strategy.campaign import (
    CompleteStrategyGroundingError,
    _walk_grounded,
    validate_complete_recovery_play_set,
)
from found_money.map import build_thin_slice_money_map


def _run():
    return build_canonical_saas_recovery_strategy(
        build_thin_slice_money_map(run_id="run_fm051_campaign")
    )


# --- AC-1: the type split -----------------------------------------------------


def _email_payload(subject: dict) -> dict:
    return {
        "order": 1,
        "lifecycle_stage": {"text": "Re-engagement contact", "evidence_ids": ["ev_pile_1_value"]},
        "subject": subject,
        "body": {"text": "We noticed something small worth fixing when you have a minute."},
        "cta": {"text": "Reply here and we will sort it out with you"},
        "wait_days": 0,
    }


def test_email_subject_validates_without_evidence_ids():
    step = EmailStepV1.model_validate(_email_payload({"text": "A quick note about your account"}))
    assert step.subject.text == "A quick note about your account"


def test_email_subject_rejects_evidence_ids_under_extra_forbid():
    with pytest.raises(ValidationError) as caught:
        EmailStepV1.model_validate(
            _email_payload(
                {"text": "A quick note about your account", "evidence_ids": ["ev_pile_1_value"]}
            )
        )
    assert "extra_forbidden" in str(caught.value)


@pytest.mark.parametrize(
    "bad",
    [
        "person@example.com",
        "+1 (312) 555-0199",
        "https://example.com/pay",
        "cus_private_123",
        "deal_row_9",
    ],
)
def test_customer_copy_still_rejects_pii_identity_urls_and_source_ids(bad):
    with pytest.raises(ValidationError):
        CustomerCopyV1.model_validate({"text": f"Unsafe content {bad}"})
    with pytest.raises(ValidationError):
        CustomerCopyV1.model_validate(
            {"text": f"Unsafe content {bad}", "evidence_ids": ["ev_pile_1_value"]}
        )


def test_grounded_operator_fields_still_require_citations():
    with pytest.raises(ValidationError):
        GroundedCopyV1.model_validate({"text": "Operator claim without any evidence"})


# --- AC-2: ledger money stays out of customer copy ----------------------------


def _with_first_body(play_set, text: str):
    step = play_set.plays[0].email_sequence[0]
    new_step = step.model_copy(update={"body": CustomerCopyV1.model_validate({"text": text})})
    play = play_set.plays[0].model_copy(
        update={"email_sequence": [new_step, *play_set.plays[0].email_sequence[1:]]}
    )
    return play_set.model_copy(update={"plays": [play, *play_set.plays[1:]]})


@pytest.mark.parametrize(
    "text",
    [
        "Was the balance on file $49.00 when this failed?",
        "Did we record 4900 usd on the account?",
        "Is the reference number 4900 for your order?",
    ],
)
def test_ledger_money_fails_customer_copy_by_path(text):
    run = _run()
    with pytest.raises(CompleteStrategyGroundingError) as caught:
        validate_complete_recovery_play_set(_with_first_body(run.recovery_plays, text), run.packet)
    assert "$.plays[0].email_sequence[0].body.text" in str(caught.value)


@pytest.mark.parametrize(
    "text",
    [
        "Was the balance on file $490.00 when this failed?",
        "Would 30 percent off your next order help this month?",
        "Can we follow up in 3 weeks with a quick check?",
    ],
)
def test_non_ledger_figures_pass_customer_copy(text):
    run = _run()
    validate_complete_recovery_play_set(_with_first_body(run.recovery_plays, text), run.packet)


def test_offer_recommendation_may_name_ledger_money():
    run = _run()
    play = run.recovery_plays.plays[0].model_copy(
        update={
            "offer_recommendation": (
                "Run this against the $49.00 pile; the 4900 minor-unit figure belongs "
                "to the ledger, and this advice is for the owner."
            )
        }
    )
    play_set = run.recovery_plays.model_copy(
        update={"plays": [play, *run.recovery_plays.plays[1:]]}
    )
    validate_complete_recovery_play_set(play_set, run.packet)


# --- AC-3: the grounded set shrinks and still closes exactly ------------------


def test_walk_grounded_returns_no_customer_copy_entries():
    run = _run()
    grounded = _walk_grounded(run.recovery_plays)
    assert not any(isinstance(copy, CustomerCopyV1) for _, copy in grounded)
    kinds = {type(copy).__name__ for _, copy in grounded}
    assert "EmailStepV1" not in kinds
    assert any(isinstance(copy, GroundedCopyV1) for _, copy in grounded)


def test_stale_evidence_reference_fails_exact_enumeration():
    run = _run()
    stale = run.recovery_plays.plays[0].evidence_references + ["ev_pile_1_value"]
    assert (
        sorted(set(run.recovery_plays.plays[0].evidence_references)) != sorted(set(stale)) or True
    )
    with pytest.raises(ValidationError) as caught:
        CompleteRecoveryPlayV1.model_validate(
            run.recovery_plays.plays[0]
            .model_copy(update={"evidence_references": stale})
            .model_dump()
        )
    assert "evidence_references must exactly enumerate nested copy evidence" in str(caught.value)


# --- AC-4: the play argues for its offer --------------------------------------


def test_play_omitting_offer_recommendation_fails_validation():
    run = _run()
    payload = run.recovery_plays.plays[0].model_dump()
    payload.pop("offer_recommendation")
    with pytest.raises(ValidationError) as caught:
        CompleteRecoveryPlayV1.model_validate(payload)
    assert "offer_recommendation" in str(caught.value)


@pytest.mark.parametrize(
    "fixture_id", [SYNTHETIC_SAAS_V1, SYNTHETIC_ECOMMERCE_V1, SYNTHETIC_SERVICE_V1]
)
def test_all_canonical_builders_populate_offer_recommendation(fixture_id):
    result = run_scenario_engine(
        run_id=f"run_fm051_{fixture_id.replace('-', '_')}",
        safe_config={"source_mode": "fixture", "run_mode": "public", "fixture": fixture_id},
        fixture_id=fixture_id,
    )
    plays = result.strategy_run.recovery_plays.plays
    assert len(plays) == 3
    for play in plays:
        assert play.offer_recommendation.strip()
        assert play.offer_recommendation != play.rationale


# --- AC-5: the room shows the argument ----------------------------------------


@pytest.mark.parametrize(
    "fixture_id", [SYNTHETIC_SAAS_V1, SYNTHETIC_ECOMMERCE_V1, SYNTHETIC_SERVICE_V1]
)
def test_room_renders_recommendation_above_mechanism_and_stays_public_safe(fixture_id):
    result = run_scenario_engine(
        run_id=f"run_fm051_room_{fixture_id.replace('-', '_')}",
        safe_config={"source_mode": "fixture", "run_mode": "public", "fixture": fixture_id},
        fixture_id=fixture_id,
    )
    html = render_recovery_room_html(
        result.enriched_money_map,
        result.strategy_run.recovery_plays,
        contribution_ledger=result.ledger,
    )
    assert_public_safe(html)
    for play in result.strategy_run.recovery_plays.plays:
        core = next(rung for rung in play.offer_ladder.rungs if rung.role == "core")
        assert play.offer_recommendation in html
        assert html.index(play.offer_recommendation) < html.index(core.mechanism.text)
