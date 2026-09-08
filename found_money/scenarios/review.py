"""Scenario strategic-review packet binding. Does not synthesize an unperformed pass."""

from __future__ import annotations

from found_money.contracts.campaign import CompleteRecoveryPlaySetV1, StrategyReviewerPacketV2
from found_money.receipts import sha256_bytes
from found_money.strategy.campaign import validate_reviewer_packet


def validate_scenario_reviewer_packet(
    packet: StrategyReviewerPacketV2,
    play_set: CompleteRecoveryPlaySetV1,
    *,
    producer_model_family: str,
) -> None:
    """Bind a fresh blind packet to locked scenario plays. Never relabel FM-026."""
    validate_reviewer_packet(packet, play_set)
    if packet.blind is not True:
        raise ValueError("scenario reviewer packet must be blind")
    if packet.producer_context_supplied is not False:
        raise ValueError("scenario reviewer packet must exclude producer context")
    if packet.producer_model_family != producer_model_family:
        raise ValueError("scenario reviewer packet producer family does not match the fixture")
    for run in packet.runs:
        if run.reviewer_model_family.casefold() == producer_model_family.casefold():
            raise ValueError("scenario reviewer family must differ from the fixture producer")
    digest = sha256_bytes(play_set.to_canonical_json())
    if packet.reviewed_artifact_sha256 != digest:
        raise ValueError("scenario reviewer packet does not bind to locked recovery-plays")
