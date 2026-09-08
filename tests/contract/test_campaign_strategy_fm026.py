"""FM-026 complete Recovery Play and Concept Card contract tests."""

from __future__ import annotations

import hashlib
from pathlib import Path
import socket

import pytest
from pydantic import ValidationError

from found_money.contracts.campaign import (
    CompleteRecoveryPlaySetV1,
    GroundedCopyV1,
    ReviewerRunV1,
    ReviewerScoreV2,
    StrategyReviewerPacketV2,
    expected_review_items,
)
from found_money.contracts.strategy import StrategyEvidenceItemV1
from found_money.map import build_thin_slice_money_map
from found_money.strategy import (
    CompleteStrategyGroundingError,
    build_canonical_saas_recovery_strategy,
    build_differentiation_report,
    parse_canonical_json,
    parse_complete_recovery_plays,
    public_complete_recovery_play_projection,
    validate_complete_recovery_play_set,
    validate_reviewer_packet,
)


def _run():
    return build_canonical_saas_recovery_strategy(
        build_thin_slice_money_map(run_id="run_fm026_campaign")
    )


def _packet_with_evidence(run, evidence_id: str, value: str):
    evidence = StrategyEvidenceItemV1(
        evidence_id=evidence_id,
        category="business_input",
        value=value,
    )
    return run.packet.model_copy(
        update={
            "evidence": [*run.packet.evidence, evidence],
            "allowed_evidence_ids": [*run.packet.allowed_evidence_ids, evidence_id],
        }
    )


def _replace_first_diagnosis(run, text: str, evidence_id: str):
    diagnosis = GroundedCopyV1(text=text, evidence_ids=[evidence_id])
    first = run.recovery_plays.plays[0].model_copy(update={"diagnosis": diagnosis})
    return run.recovery_plays.model_copy(update={"plays": [first, *run.recovery_plays.plays[1:]]})


def _with_broken_card(run):
    """A card whose format_style names no permitted family: a schema defect."""

    play = run.recovery_plays.plays[0]
    card = play.concept_cards[0].model_copy(update={"format_style": "Something invented"})
    broken = play.model_copy(update={"concept_cards": [card, *play.concept_cards[1:]]})
    return run.recovery_plays.model_copy(update={"plays": [broken, *run.recovery_plays.plays[1:]]})


FIXTURE = Path("tests/fixtures/saas/strategy/fm026")


def _review_scores() -> list[ReviewerScoreV2]:
    """A clean rubric v2 sweep: the 81 judgment items, all passing."""

    return [
        ReviewerScoreV2(target_id=target, rubric=rubric, passed=True)
        for target, rubric in sorted(expected_review_items())
    ]


def _review_run(context_id: str, *, family: str = "gpt-5.6-luna") -> ReviewerRunV1:
    return ReviewerRunV1(
        context_id=context_id,
        reviewer_runtime="codex-cli",
        reviewer_model_family=family,
        scores=_review_scores(),
    )


def test_canonical_saas_has_three_complete_plays_and_nine_cards():
    run = _run()
    plays = sorted(run.recovery_plays.plays, key=lambda play: play.rank)
    assert [play.rank for play in plays] == [1, 2, 3]
    assert len({play.play_id for play in plays}) == 3
    assert sum(len(play.concept_cards) for play in plays) == 9
    for play in plays:
        assert play.email_sequence and len(play.email_sequence) >= 3
        assert play.sms.available and play.sms.messages
        assert play.objections and play.calendar and play.stop_conditions
        assert play.tracking.success_event.text
        assert play.urgency.text == "none" or play.urgency.evidence_ids
        assert play.evidence_references == sorted(set(play.evidence_references))
        for card in play.concept_cards:
            assert card.card_name and card.audience_tension.text and card.big_idea.text
            assert card.hook.text and card.opening_visual.text and card.proof_device.text
            assert card.format_style and card.cta.text and card.pile_fit.text
            assert card.production_requirements


@pytest.mark.parametrize(
    "bad",
    [
        "DRAFT content here",
        "TBD content here",
        "[TODO] write this",
        "{{ placeholder }}",
        "Section heading:",
    ],
)
def test_required_copy_rejects_headings_and_placeholders(bad: str):
    with pytest.raises(ValidationError):
        GroundedCopyV1(text=bad, evidence_ids=["ev_business_product"])


def test_complete_play_rejects_missing_required_field_and_wrong_card_count():
    payload = _run().recovery_plays.model_dump(mode="json")
    del payload["plays"][0]["primary_cta"]
    with pytest.raises(ValidationError):
        CompleteRecoveryPlaySetV1.model_validate(payload)

    payload = _run().recovery_plays.model_dump(mode="json")
    payload["plays"][0]["concept_cards"].pop()
    with pytest.raises(ValidationError):
        CompleteRecoveryPlaySetV1.model_validate(payload)


@pytest.mark.parametrize(
    "path,copy",
    [
        (
            "unknown",
            GroundedCopyV1(
                text="Unsupported product language", kind="product", evidence_ids=["ev_unknown"]
            ),
        ),
        (
            "product",
            GroundedCopyV1(
                text="Unsupported product language",
                kind="product",
                evidence_ids=["ev_pile_1_basis"],
            ),
        ),
        (
            "number",
            GroundedCopyV1(
                text="Unsupported 9999 usd amount", kind="number", evidence_ids=["ev_pile_1_value"]
            ),
        ),
        (
            "capacity",
            GroundedCopyV1(
                text="Unsupported capacity language",
                kind="capacity",
                evidence_ids=["ev_pile_1_basis"],
            ),
        ),
        (
            "destination",
            GroundedCopyV1(
                text="Unsupported destination language",
                kind="destination",
                evidence_ids=["ev_pile_1_basis"],
            ),
        ),
        (
            "proof",
            GroundedCopyV1(
                text="Unsupported proof language", kind="proof", evidence_ids=["ev_pile_1_basis"]
            ),
        ),
        (
            "objection",
            GroundedCopyV1(
                text="Unsupported objection language",
                kind="objection",
                evidence_ids=["ev_pile_1_basis"],
            ),
        ),
    ],
)
def test_nested_grounding_rejects_unknown_wrong_kind_and_unsupported_numbers(path, copy):
    run = _run()
    play = run.recovery_plays.plays[0].model_copy(update={"diagnosis": copy})
    changed = run.recovery_plays.model_copy(update={"plays": [play, *run.recovery_plays.plays[1:]]})
    with pytest.raises(CompleteStrategyGroundingError) as caught:
        validate_complete_recovery_play_set(changed, run.packet)
    assert "$.plays[0].diagnosis" in str(caught.value)


@pytest.mark.parametrize(
    "bad",
    ["person@example.com", "+1 (312) 555-0199", "https://example.com/pay", "cus_private_123"],
)
def test_nested_copy_rejects_raw_identity_source_ids_and_urls(bad: str):
    with pytest.raises(ValidationError):
        GroundedCopyV1(text=f"Unsafe content {bad}", evidence_ids=["ev_business_product"])


def test_urgency_rejects_unsupported_number_by_exact_json_path():
    run = _run()
    urgency = run.recovery_plays.plays[2].urgency.model_copy(
        update={"text": "Respect the 999 recovery reviews per week capacity"}
    )
    play = run.recovery_plays.plays[2].model_copy(update={"urgency": urgency})
    changed = run.recovery_plays.model_copy(update={"plays": [*run.recovery_plays.plays[:2], play]})
    with pytest.raises(CompleteStrategyGroundingError) as caught:
        validate_complete_recovery_play_set(changed, run.packet)
    assert "$.plays[2].urgency.text" in str(caught.value)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("We recovered $60", "reject"),
        ("a 60 percent lift", "reject"),
        ("Give it 6 minutes", "pass"),
        ("Give it 5 minutes", "pass"),
        ("in 3 weeks", "pass"),
    ],
)
def test_fm049_pinned_number_check_table(text: str, expected: str):
    run = _run()
    evidence_id = "ev_business_capacity_fm049"
    packet = _packet_with_evidence(run, evidence_id, "60 recovery reviews per week")
    changed = _replace_first_diagnosis(run, text, evidence_id)

    if expected == "reject":
        with pytest.raises(CompleteStrategyGroundingError) as caught:
            validate_complete_recovery_play_set(changed, packet)
        assert "$.plays[0].diagnosis.text" in caught.value.paths
    else:
        validate_complete_recovery_play_set(changed, packet)


@pytest.mark.parametrize(
    "evidence,text",
    [
        ("45 percent contribution margin", "45 percent contribution margin"),
        ("45 percent contribution margin", "forty-five percent contribution margin"),
        ("60 usd", "The opportunity is $60"),
        ("60 usd", "The opportunity is sixty dollars"),
    ],
)
def test_fm049_exact_claim_values_accept_symbol_numeric_and_word_forms(evidence: str, text: str):
    run = _run()
    evidence_id = "ev_business_margin_fm049"
    packet = _packet_with_evidence(run, evidence_id, evidence)
    changed = _replace_first_diagnosis(run, text, evidence_id)
    validate_complete_recovery_play_set(changed, packet)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("We recovered $60", "reject"),
        ("a sixty percent lift", "reject"),
        ("in 3 weeks", "pass"),
    ],
)
def test_fm049_urgency_uses_the_shared_claim_checker(text: str, expected: str):
    run = _run()
    evidence_id = "ev_business_capacity_fm049"
    packet = _packet_with_evidence(run, evidence_id, "60 recovery reviews per week")
    urgency = run.recovery_plays.plays[2].urgency.model_copy(
        update={"text": text, "evidence_ids": [evidence_id]}
    )
    play = run.recovery_plays.plays[2].model_copy(update={"urgency": urgency})
    changed = run.recovery_plays.model_copy(update={"plays": [*run.recovery_plays.plays[:2], play]})

    if expected == "reject":
        with pytest.raises(CompleteStrategyGroundingError) as caught:
            validate_complete_recovery_play_set(changed, packet)
        assert "$.plays[2].urgency.text" in caught.value.paths
    else:
        validate_complete_recovery_play_set(changed, packet)


def test_concept_cards_forbid_finished_script_surface_and_script_like_requirements():
    payload = _run().recovery_plays.plays[0].concept_cards[0].model_dump(mode="json")
    payload["script"] = "finished"
    with pytest.raises(ValidationError):
        type(_run().recovery_plays.plays[0].concept_cards[0]).model_validate(payload)
    payload.pop("script")
    payload["production_requirements"] = ["Scene 1 voiceover: deliver finished dialogue"]
    with pytest.raises(ValidationError):
        type(_run().recovery_plays.plays[0].concept_cards[0]).model_validate(payload)


@pytest.mark.parametrize(
    "axis",
    [
        "diagnosis",
        "offer_mechanism",
        "lifecycle_sequence",
        "primary_cta",
        "channel_emphasis",
        "creative_big_idea",
    ],
)
def test_each_differentiation_axis_fails_on_pairwise_collision(axis: str):
    run = _run()
    left, right, third = run.recovery_plays.plays
    if axis == "offer_mechanism":
        left_core = next(rung for rung in left.offer_ladder.rungs if rung.role == "core")
        right_ladder = right.offer_ladder
        new_rungs = [
            rung.model_copy(update={"mechanism": left_core.mechanism})
            if rung.role == "core"
            else rung
            for rung in right_ladder.rungs
        ]
        right = right.model_copy(
            update={"offer_ladder": right_ladder.model_copy(update={"rungs": new_rungs})}
        )
    else:
        right = right.model_copy(update={axis: getattr(left, axis)})
    changed = run.recovery_plays.model_copy(update={"plays": [left, right, third]})
    report = build_differentiation_report(changed)
    assert report.passed is False
    assert any(check.axis == axis and not check.passed for check in report.checks)


def test_clone_with_renamed_campaign_fails_differentiation():
    run = _run()
    left = run.recovery_plays.plays[0]
    clones = [
        left.model_copy(
            update={
                "play_id": f"clone-play-{rank}",
                "rank": rank,
                "campaign_name": f"Renamed Campaign {rank}",
                "concept_cards": [
                    card.model_copy(update={"card_id": f"clone-{rank}-{index}"})
                    for index, card in enumerate(left.concept_cards, start=1)
                ],
            }
        )
        for rank in range(1, 4)
    ]
    changed = run.recovery_plays.model_copy(update={"plays": clones})
    assert build_differentiation_report(changed).passed is False


def test_near_copy_with_one_word_changed_fails_differentiation():
    run = _run()
    left, right, third = run.recovery_plays.plays
    near_copy = left.diagnosis.model_copy(
        update={"text": left.diagnosis.text.replace("solvable", "minor")}
    )
    right = right.model_copy(update={"diagnosis": near_copy})
    report = build_differentiation_report(
        run.recovery_plays.model_copy(update={"plays": [left, right, third]})
    )
    assert any(check.axis == "diagnosis" and not check.passed for check in report.checks)


def test_public_projection_is_aggregate_and_identity_free():
    projection = public_complete_recovery_play_projection(_run().recovery_plays)
    text = str(projection).casefold()
    assert len(projection["plays"]) == 3
    assert "evidence" not in text and "customer" not in text and "source_id" not in text


def test_reviewer_packet_is_blind_cross_family_complete_and_hash_bound():
    run = _run()
    digest = hashlib.sha256(run.recovery_plays.to_canonical_json()).hexdigest()
    packet = StrategyReviewerPacketV2(
        reviewed_artifact_sha256=digest,
        blind=True,
        producer_context_supplied=False,
        producer_model_family="codex",
        runs=[_review_run("round-1", family="cursor-auto"), _review_run("round-2")],
        passed=True,
    )
    validate_reviewer_packet(packet, run.recovery_plays)
    with pytest.raises(ValueError, match="does not bind"):
        validate_reviewer_packet(
            packet.model_copy(update={"reviewed_artifact_sha256": "0" * 64}), run.recovery_plays
        )

    payload = packet.model_dump(mode="json")
    with pytest.raises(ValidationError, match="differ from producer"):
        StrategyReviewerPacketV2.model_validate({**payload, "producer_model_family": "cursor-auto"})
    with pytest.raises(ValidationError, match="two consecutive runs"):
        StrategyReviewerPacketV2.model_validate({**payload, "runs": payload["runs"][:1]})
    with pytest.raises(ValidationError, match="independent contexts"):
        StrategyReviewerPacketV2.model_validate(
            {**payload, "runs": [payload["runs"][0], payload["runs"][0]]}
        )
    short = {**payload["runs"][1], "scores": payload["runs"][1]["scores"][:-1]}
    with pytest.raises(ValidationError, match="exactly cover"):
        StrategyReviewerPacketV2.model_validate({**payload, "runs": [payload["runs"][0], short]})


def test_reviewer_packet_rejects_the_three_fields_code_now_decides():
    """Rubric v2 removed them; a packet still scoring them is scoring a stale rubric."""

    run = _run()
    payload = StrategyReviewerPacketV2(
        reviewed_artifact_sha256=hashlib.sha256(run.recovery_plays.to_canonical_json()).hexdigest(),
        blind=True,
        producer_context_supplied=False,
        producer_model_family="fixture-strategy-v1",
        runs=[_review_run("round-1"), _review_run("round-2")],
        passed=True,
    ).model_dump(mode="json")
    for retired in ("format_style", "cta", "production_requirements_and_no_finished_script"):
        stale = dict(payload["runs"][0])
        stale["scores"] = [
            *payload["runs"][0]["scores"],
            {
                "target_id": "card:1-1",
                "rubric": retired,
                "passed": True,
                "finding": None,
                "evidence": None,
            },
        ]
        with pytest.raises(ValidationError, match="exactly cover"):
            StrategyReviewerPacketV2.model_validate(
                {**payload, "runs": [stale, payload["runs"][1]]}
            )


def test_a_failure_without_cited_artifact_text_is_not_a_finding():
    with pytest.raises(ValidationError, match="cited artifact text"):
        ReviewerScoreV2(target_id="card:1-1", rubric="hook", passed=False, finding="weak hook")
    with pytest.raises(ValidationError, match="actionable finding"):
        ReviewerScoreV2(target_id="card:1-1", rubric="hook", passed=False, evidence="Ready?")


def test_deterministic_card_failure_blocks_any_reviewer_pass():
    """The reviewer no longer judges schema facts, so its pass cannot cover them."""

    run = _run()
    broken = _with_broken_card(run)
    digest = hashlib.sha256(broken.to_canonical_json()).hexdigest()
    packet = StrategyReviewerPacketV2(
        reviewed_artifact_sha256=digest,
        blind=True,
        producer_context_supplied=False,
        producer_model_family="fixture-strategy-v1",
        runs=[_review_run("round-1"), _review_run("round-2")],
        passed=True,
    )
    with pytest.raises(ValueError, match="deterministic card checks fail"):
        validate_reviewer_packet(packet, broken)


def test_whole_field_dispute_carries_an_adjudication_and_upheld_blocks():
    run = _run()
    digest = hashlib.sha256(run.recovery_plays.to_canonical_json()).hexdigest()
    targets = [f"card:{play}-{card}" for play in range(1, 4) for card in range(1, 4)]
    scores = [
        score.model_copy(
            update={
                "passed": False,
                "finding": "names no artifact a viewer would see",
                "evidence": "a short proof clip",
            }
        )
        if score.rubric == "proof_device"
        else score
        for score in _review_scores()
    ]
    disputed_run = ReviewerRunV1(
        context_id="round-1",
        reviewer_runtime="codex-cli",
        reviewer_model_family="gpt-5.6-luna",
        scores=scores,
    )

    def build(adjudication: str, **overrides):
        dispute = {
            "rubric": "proof_device",
            "target_ids": targets,
            "shared_rationale": "names no artifact a viewer would see",
            "adjudication": adjudication,
            "adjudication_rationale": "the rubric forbids judging persuasiveness",
            "adjudicated_by": "matthew",
            **overrides,
        }
        return StrategyReviewerPacketV2(
            reviewed_artifact_sha256=digest,
            blind=True,
            producer_context_supplied=False,
            producer_model_family="fixture-strategy-v1",
            runs=[disputed_run, _review_run("round-2")],
            disputes=[dispute],
            passed=adjudication == "rejected",
            actionable_findings=(
                [] if adjudication == "rejected" else ["proof_device: whole-field dispute upheld"]
            ),
        )

    assert build("rejected").passed is True
    assert build("upheld").passed is False

    # Nine failures with no dispute at all cannot quietly pass.
    with pytest.raises(ValidationError, match="derived from scores"):
        StrategyReviewerPacketV2(
            reviewed_artifact_sha256=digest,
            blind=True,
            producer_context_supplied=False,
            producer_model_family="fixture-strategy-v1",
            runs=[disputed_run, _review_run("round-2")],
            passed=True,
        )
    # A dispute cannot fold items nobody failed.
    with pytest.raises(ValidationError, match="folds items no run actually failed"):
        build("rejected", rubric="hook")


def test_fixture_replay_is_byte_stable_and_hashes_exact_output(monkeypatch):
    def blocked(*_args, **_kwargs):
        raise AssertionError("network access is forbidden")

    monkeypatch.setattr(socket, "socket", blocked)
    first = _run()
    second = _run()
    assert first.packet.to_canonical_json() == second.packet.to_canonical_json()
    assert first.recovery_plays.to_canonical_json() == second.recovery_plays.to_canonical_json()
    assert first.receipt.to_canonical_json() == second.receipt.to_canonical_json()
    assert first.differentiation.to_canonical_json() == second.differentiation.to_canonical_json()
    assert (
        first.receipt.evidence_packet_hash
        == hashlib.sha256(first.packet.to_canonical_json()).hexdigest()
    )
    assert (
        first.receipt.output_hash
        == hashlib.sha256(first.recovery_plays.to_canonical_json()).hexdigest()
    )
    assert first.receipt.output_schema_version == "recovery-plays.v1"
    assert first.receipt.configured_model_id == first.receipt.returned_model_id
    assert (
        parse_complete_recovery_plays(first.recovery_plays.to_canonical_json()).to_canonical_json()
        == first.recovery_plays.to_canonical_json()
    )
    assert (
        parse_canonical_json(first.recovery_plays.to_canonical_json()).to_canonical_json()
        == first.recovery_plays.to_canonical_json()
    )


def test_play_three_lifecycle_executes_one_coherent_differentiated_order():
    plays = sorted(_run().recovery_plays.plays, key=lambda play: play.rank)
    play_one, play_two, play_three = plays
    email_days = [step.wait_days for step in play_three.email_sequence]
    calendar_days = [item.day for item in play_three.calendar]
    assert email_days == calendar_days
    assert email_days == sorted(email_days)
    assert len(set(email_days)) == 3
    assert tuple(step.wait_days for step in play_one.email_sequence) == (0, 1, 3)
    assert tuple(step.wait_days for step in play_two.email_sequence) == (0, 3, 7)
    assert tuple(email_days) not in {(0, 1, 3), (0, 3, 7)}
    stages = [step.lifecycle_stage.text.casefold() for step in play_three.email_sequence]
    assert "window" in stages[0] and "notice" in stages[0]
    assert "reserv" in stages[1]
    assert "close" in stages[2]
    sequence = play_three.lifecycle_sequence.text.casefold()
    window_at = sequence.find("window")
    reserve_at = sequence.find("reserve")
    close_at = sequence.find("close")
    assert 0 <= window_at < reserve_at < close_at
    first_body = play_three.email_sequence[0].body.text.casefold()
    first_calendar = play_three.calendar[0].action.text.casefold()
    assert "after the reserved task" not in first_body
    assert "task" not in first_calendar
    assert "notice" in first_calendar or "window" in first_calendar
    last_body = play_three.email_sequence[-1].body.text.casefold()
    last_calendar = play_three.calendar[-1].action.text.casefold()
    assert "close" in last_body and "close" in last_calendar
    assert (
        "lead with a reserved human review task" not in play_three.task_talk_track.text.casefold()
    )
    channel = play_three.channel_emphasis.text.casefold()
    assert "lead with a verified-capacity window email" in channel
    assert "transition to a reserved human review task" in channel
    assert "sms only after reservation" in channel


def test_concept_cards_have_definite_proof_and_payment_rescue_action_semantics():
    plays = sorted(_run().recovery_plays.plays, key=lambda play: play.rank)
    hedges = (" may ", "may cite", "may use", "labeled with", " citing ", "drawn from")
    cards = [card for play in plays for card in play.concept_cards]
    assert len(cards) == 9
    devices = [card.proof_device.text for card in cards]
    fits = [card.pile_fit.text for card in cards]
    assert len(set(devices)) == 9
    assert len(set(fits)) == 9
    for device in devices:
        folded = f" {device.casefold()} "
        assert not any(hedge in folded for hedge in hedges)
        assert "on-screen" in device.casefold() or any(
            marker in device.casefold()
            for marker in (
                "comparison",
                "callout",
                "stamp",
                "panel",
                "tile",
                "caption",
                "overlay",
                "placard",
                "marker",
            )
        )
        uses_capacity = "100 recovery reviews per week" in device
        uses_case_studies = "Three approved case studies" in device
        assert uses_capacity or uses_case_studies
        if uses_case_studies:
            assert "as the" in device
            assert "explicitly labeled" in device.casefold() or play_index_for(plays, device) != 3
    play_three_devices = [card.proof_device.text for card in plays[2].concept_cards]
    assert any("100 recovery reviews per week" in device for device in play_three_devices)
    assert any(
        "explicitly labeled" in device.casefold() and "Three approved case studies" in device
        for device in play_three_devices
    )
    for fit in fits:
        folded = fit.casefold()
        assert "payment_rescue" in fit or "payment rescue" in folded
        assert "failed" in folded and "annual subscription" in folded
        assert "billing" in folded or "card update" in folded
        assert "retry" in folded
        # Copy states major units; the ledger keeps minor units. See FM-054.
        assert "$49.00" in fit
    for fit in [card.pile_fit.text for card in plays[2].concept_cards]:
        folded = fit.casefold()
        assert "human review" in folded
        assert "billing" in folded and "retry" in folded


def play_index_for(plays, device: str) -> int:
    for play in plays:
        if any(card.proof_device.text == device for card in play.concept_cards):
            return play.rank
    raise AssertionError("proof device is not on a canonical play")


def test_committed_campaign_artifacts_match_deterministic_fixture_run():
    run = build_canonical_saas_recovery_strategy(
        build_thin_slice_money_map(run_id="run_fm026_fixture")
    )
    assert (FIXTURE / "grounded-evidence.json").read_bytes() == run.packet.to_canonical_json()
    assert (FIXTURE / "recovery-plays.json").read_bytes() == run.recovery_plays.to_canonical_json()
    assert (FIXTURE / "strategy-audit-receipt.json").read_bytes() == run.receipt.to_canonical_json()
    assert (
        FIXTURE / "differentiation-report.json"
    ).read_bytes() == run.differentiation.to_canonical_json()


@pytest.mark.release_evidence
def test_committed_blind_reviewer_packet_binds_locked_recovery_plays():
    run = build_canonical_saas_recovery_strategy(
        build_thin_slice_money_map(run_id="run_fm026_fixture")
    )
    digest = hashlib.sha256(run.recovery_plays.to_canonical_json()).hexdigest()
    packet_path = FIXTURE / "blind-reviewer-packet.json"
    assert packet_path.is_file(), (
        "Independent FM-026 blind reviewer packet is still required at "
        f"{packet_path} bound to locked recovery-plays with blind=true, "
        "producer_context_supplied=false, and a reviewer family different from "
        f"the producer. Required reviewed_artifact_sha256={digest}. "
        "Do not copy, relabel, or preserve a stale pass."
    )
    reviewer = StrategyReviewerPacketV2.model_validate_json(packet_path.read_bytes())
    validate_reviewer_packet(reviewer, run.recovery_plays)
    assert reviewer.passed is True
    assert reviewer.blind is True
    assert reviewer.producer_context_supplied is False
    assert reviewer.rubric_version == 2
    # Two clean runs from distinct contexts, not one lucky sample.
    assert len(reviewer.runs) == 2
    assert len({rev.context_id for rev in reviewer.runs}) == 2
    for review_run in reviewer.runs:
        assert len(review_run.scores) == 81
        assert review_run.reviewer_model_family != reviewer.producer_model_family
