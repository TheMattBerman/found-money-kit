"""FM-059 offer ladder contract and build-path guardrails."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from found_money.contracts.campaign import (
    GroundedCopyV1,
    OfferLadderV1,
)
from found_money.map import build_thin_slice_money_map
from found_money.strategy import (
    CompleteStrategyGroundingError,
    build_canonical_saas_recovery_strategy,
    validate_complete_recovery_play_set,
)
from found_money.strategy.offer_ladder_checks import offer_ladder_violations


def _run():
    return build_canonical_saas_recovery_strategy(
        build_thin_slice_money_map(run_id="run_fm059_ladder")
    )


def _core_rung(play):
    return next(rung for rung in play.offer_ladder.rungs if rung.role == "core")


def test_canonical_plays_carry_three_role_ordered_rungs():
    run = _run()
    for play in run.recovery_plays.plays:
        roles = [rung.role for rung in play.offer_ladder.rungs]
        assert roles == ["high_anchor", "core", "downsell"]
        assert play.offer_ladder.selected_offer_rung_id == _core_rung(play).id
        assert len(play.rung_copy_packages) == 3


def test_two_rung_ladder_fails_validation():
    run = _run()
    play = run.recovery_plays.plays[0]
    ladder = play.offer_ladder.model_dump()
    ladder["rungs"] = ladder["rungs"][:2]
    with pytest.raises(ValidationError):
        OfferLadderV1.model_validate(ladder)


def test_reordered_rungs_fail_validation():
    run = _run()
    play = run.recovery_plays.plays[0]
    rungs = list(play.offer_ladder.rungs)
    with pytest.raises(ValidationError):
        OfferLadderV1.model_validate(
            play.offer_ladder.model_dump()
            | {"rungs": [rungs[1].model_dump(), rungs[0].model_dump(), rungs[2].model_dump()]}
        )


def test_role_confusion_fixture_fails_with_named_error():
    run = _run()
    play = run.recovery_plays.plays[0]
    core = _core_rung(play)
    anchor = play.offer_ladder.rungs[0]
    confused_core = core.model_copy(
        update={
            "scope": anchor.scope,
            "support": anchor.support,
            "commitment": anchor.commitment,
            "concedes_money": True,
            "discount_percent": 10.0,
            "commitment_term": None,
            "rationale": GroundedCopyV1(
                text="Relative to the anchor and downsell, this core rung is cheaper only",
                evidence_ids=core.rationale.evidence_ids,
            ),
        }
    )
    ladder = play.offer_ladder.model_copy(
        update={"rungs": [anchor, confused_core, play.offer_ladder.rungs[2]]}
    )
    violations = offer_ladder_violations(ladder)
    assert any("role_confusion" in item for item in violations)
    assert any("discount_requires_commitment_term" in item for item in violations)


def test_downsell_same_scope_as_core_fails():
    run = _run()
    play = run.recovery_plays.plays[0]
    core = _core_rung(play)
    downsell = play.offer_ladder.rungs[2].model_copy(
        update={
            "scope": core.scope,
            "scope_reduction": core.scope.model_copy(
                update={"text": "Different prose that does not change the scope field"}
            ),
        }
    )
    ladder = play.offer_ladder.model_copy(
        update={"rungs": [play.offer_ladder.rungs[0], play.offer_ladder.rungs[1], downsell]}
    )
    assert any("downsell_reduces_scope" in item for item in offer_ladder_violations(ladder))


def test_downsell_without_scope_delta_fails():
    run = _run()
    play = run.recovery_plays.plays[0]
    downsell = play.offer_ladder.rungs[2].model_copy(update={"scope_reduction": None})
    ladder = play.offer_ladder.model_copy(
        update={"rungs": [play.offer_ladder.rungs[0], play.offer_ladder.rungs[1], downsell]}
    )
    assert any("downsell_reduces_scope" in item for item in offer_ladder_violations(ladder))


def test_unpriced_pile_money_concession_fails():
    run = _run()
    play = run.recovery_plays.plays[0]
    downsell = play.offer_ladder.rungs[2].model_copy(update={"concedes_money": True})
    ladder = play.offer_ladder.model_copy(
        update={
            "pile_value_basis": "modeled_opportunity",
            "rungs": [play.offer_ladder.rungs[0], play.offer_ladder.rungs[1], downsell],
        }
    )
    assert any("unpriced_piles_concede_nothing" in item for item in offer_ladder_violations(ladder))


def test_rung_copy_packages_reference_ladder_and_rung_ids():
    run = _run()
    for play in run.recovery_plays.plays:
        ladder_id = play.offer_ladder.offer_ladder_id
        rung_ids = {rung.id for rung in play.offer_ladder.rungs}
        for package in play.rung_copy_packages:
            assert package.offer_ladder_id == ladder_id
            assert package.offer_rung_id in rung_ids


def test_validate_complete_recovery_play_set_enforces_ladder_rules():
    run = _run()
    play = run.recovery_plays.plays[0]
    downsell = play.offer_ladder.rungs[2].model_copy(update={"scope_reduction": None})
    ladder = play.offer_ladder.model_copy(
        update={"rungs": [play.offer_ladder.rungs[0], play.offer_ladder.rungs[1], downsell]}
    )
    changed = run.recovery_plays.model_copy(
        update={
            "plays": [
                play.model_copy(update={"offer_ladder": ladder}),
                *run.recovery_plays.plays[1:],
            ]
        }
    )
    with pytest.raises(CompleteStrategyGroundingError):
        validate_complete_recovery_play_set(changed, run.packet)
