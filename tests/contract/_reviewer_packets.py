"""Shared synthetic reviewer packets for the scenario suites.

Three suites carried byte-identical copies of this builder, each spelling out the
rubric's item set by hand. That is the same duplication that let
`found_money/scenarios/REVIEW_RUBRIC.md` drift out of step with the real rubric,
so the item set comes from the contract and lives in one place.
"""

from __future__ import annotations

from found_money.contracts.campaign import (
    ReviewerRunV1,
    ReviewerScoreV2,
    StrategyReviewerPacketV2,
    expected_review_items,
)


def clean_scores() -> list[ReviewerScoreV2]:
    return [
        ReviewerScoreV2(target_id=target, rubric=rubric, passed=True)
        for target, rubric in sorted(expected_review_items())
    ]


def clean_run(context_id: str, *, reviewer: str = "gpt-5.6-luna") -> ReviewerRunV1:
    return ReviewerRunV1(
        context_id=context_id,
        reviewer_runtime="codex-cli",
        reviewer_model_family=reviewer,
        scores=clean_scores(),
    )


def synthetic_reviewer_packet(
    digest: str,
    *,
    producer: str,
    reviewer: str = "gpt-5.6-luna",
) -> StrategyReviewerPacketV2:
    """A packet that passes every structural rule, so a test can break one on purpose."""

    return StrategyReviewerPacketV2(
        reviewed_artifact_sha256=digest,
        blind=True,
        producer_context_supplied=False,
        producer_model_family=producer,
        runs=[clean_run("round-1", reviewer=reviewer), clean_run("round-2", reviewer=reviewer)],
        passed=True,
    )
