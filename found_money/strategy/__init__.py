"""Deterministic fixture strategy provider and recovery-play helpers."""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, cast

from found_money.contracts.map import MoneyMapNavigationV1, MoneyMapV1
from found_money.contracts.campaign import CompleteRecoveryPlaySetV1
from found_money.contracts.strategy import (
    PublicRecoveryPlayProjectionV1,
    PublicRecoveryPlayRowV1,
    RecoveryPlaySetV1,
    RecoveryPlayV1,
    StrategyEvidencePacketV1,
    StrategyEvidencePileFactV1,
)
from found_money.map import build_thin_slice_money_map

_TRAVERSAL_RE = re.compile(r"(^|/)\.\.(/|$)")
_WINDOWS_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")
_SCHEMA_MAJOR_RE = re.compile(r"^(?P<name>.+)\.v(?P<major>\d+)$")


def build_strategy_evidence_packet(
    money_map: MoneyMapV1,
    *,
    built_at: datetime | None = None,
) -> StrategyEvidencePacketV1:
    """Build a PII-free strategy evidence packet from a Money Map."""
    when = built_at or datetime(2026, 7, 29, 18, 0, 25, tzinfo=timezone.utc)
    facts: list[StrategyEvidencePileFactV1] = []
    for pile in money_map.piles:
        if (
            pile.pile_id != "payment_rescue"
            or pile.value_basis != "observed_face_value"
            or pile.confidence_class != "observed"
        ):
            raise ValueError(
                "fixture strategy evidence only supports observed payment-rescue piles"
            )
        facts.append(
            StrategyEvidencePileFactV1(
                pile_id=cast(Literal["payment_rescue"], pile.pile_id),
                currency=pile.currency,
                selected_value_minor=pile.selected_value_minor,
                customer_count=pile.customer_count,
                economic_unit_count=pile.economic_unit_count,
                value_basis=cast(Literal["observed_face_value"], pile.value_basis),
                confidence_class=cast(Literal["observed"], pile.confidence_class),
            )
        )
    packet = StrategyEvidencePacketV1(
        run_id=money_map.run_id,
        built_at=when,
        pile_facts=facts,
    )
    text = packet.to_canonical_json().decode("utf-8")
    for forbidden in (
        "customer_token",
        "economic_unit_key",
        "lineage",
        "qualifying_evidence",
        "external_ids",
        "inv_failed_001",
        "cus_synth_001",
    ):
        if forbidden in text:
            raise ValueError(f"evidence packet would include forbidden identity key: {forbidden}")
    return packet


class FixtureStrategyProvider:
    """Deterministic fixture-only strategy provider."""

    def propose(
        self,
        packet: StrategyEvidencePacketV1,
        *,
        built_at: datetime | None = None,
    ) -> RecoveryPlaySetV1:
        when = built_at or datetime(2026, 7, 29, 18, 0, 30, tzinfo=timezone.utc)
        plays: list[RecoveryPlayV1] = []
        for index, fact in enumerate(packet.pile_facts):
            if fact.pile_id != "payment_rescue":
                raise ValueError(f"unsupported pile_id for fixture provider: {fact.pile_id}")
            unit_label = "payment" if fact.economic_unit_count == 1 else "payments"
            title = f"Payment rescue dunning for {fact.pile_id}"
            rationale = (
                f"{fact.economic_unit_count} observed {unit_label} totaling "
                f"{fact.selected_value_minor} {fact.currency} under "
                f"{fact.value_basis.replace('_', ' ')}"
            )
            actions = [
                f"Human review of {fact.pile_id} pile before any outreach",
                f"Confirm observed face value of {fact.selected_value_minor} {fact.currency}",
                "Queue recovery review without sending messages",
            ]
            plays.append(
                RecoveryPlayV1(
                    play_id="payment_rescue_dunning",
                    pile_id="payment_rescue",
                    rank=index + 1,
                    title=title,
                    rationale=rationale,
                    recommended_actions=actions,
                )
            )
        return RecoveryPlaySetV1(
            run_id=packet.run_id,
            built_at=when,
            provider="fixture",
            plays=plays,
        )


def apply_recovery_plays_to_money_map(
    money_map: MoneyMapV1,
    play_set: RecoveryPlaySetV1,
) -> MoneyMapV1:
    """Enrich a Money Map with recommended play ids from a recovery-play set."""
    if money_map.run_id != play_set.run_id:
        raise ValueError("money map run_id must match recovery play set run_id")
    if not play_set.plays:
        piles = [
            pile.model_copy(
                update={
                    "navigation": MoneyMapNavigationV1(
                        target_id=(
                            pile.navigation.target_id
                            if pile.navigation is not None
                            else f"pile/{pile.pile_id}/{pile.currency}"
                        ),
                        state="deferred",
                        next_action="strategy_review_pending",
                    )
                }
            )
            for pile in money_map.piles
        ]
        return money_map.model_copy(
            update={
                "piles": piles,
                "recommended_play_ids": [],
                "strategy_stage": "not_started",
                "next_action": "review_top_ranked_pile" if piles else "no_recoverable_opportunity",
            }
        )

    ordered = sorted(play_set.plays, key=lambda item: item.rank)
    play_ids: list[str] = []
    linked_by_pile: dict[str, str] = {}
    for play in ordered:
        matching_piles = [pile for pile in money_map.piles if pile.pile_id == play.pile_id]
        if not matching_piles:
            raise ValueError(f"play references unknown pile_id: {play.pile_id}")
        if len(matching_piles) > 1:
            raise ValueError(f"play references ambiguous pile_id across currencies: {play.pile_id}")
        if play.pile_id in linked_by_pile:
            raise ValueError(f"multiple plays reference pile_id: {play.pile_id}")
        linked_by_pile[play.pile_id] = play.play_id
        play_ids.append(play.play_id)

    piles = []
    for pile in money_map.piles:
        play_id = linked_by_pile.get(pile.pile_id)
        target_id = (
            pile.navigation.target_id
            if pile.navigation is not None
            else f"pile/{pile.pile_id}/{pile.currency}"
        )
        navigation = MoneyMapNavigationV1(
            target_id=target_id,
            state="available" if play_id is not None else "deferred",
            play_id=play_id,
            next_action=(
                "review_linked_play" if play_id is not None else "strategy_review_pending"
            ),
        )
        piles.append(pile.model_copy(update={"navigation": navigation}))
    return money_map.model_copy(
        update={
            "piles": piles,
            "recommended_play_ids": play_ids,
            "strategy_stage": "completed",
            "next_action": "review_recommended_play",
        }
    )


def apply_complete_plays_to_money_map(
    money_map: MoneyMapV1,
    play_set: CompleteRecoveryPlaySetV1,
) -> MoneyMapV1:
    """Record all recommended complete-play IDs; pile navigation points at primary.

    Unknown pile IDs, duplicate play IDs, duplicate ranks, and rank order errors
    fail closed. Non-primary piles stay deferred and never inherit a play ID.
    """
    if money_map.run_id != play_set.run_id:
        raise ValueError("money map run_id must match recovery play set run_id")
    if not play_set.plays:
        raise ValueError("complete play set is empty")
    ordered = sorted(play_set.plays, key=lambda item: (item.rank, item.play_id))
    ranks = [play.rank for play in ordered]
    if ranks != list(range(1, len(ordered) + 1)):
        raise ValueError("complete plays must use contiguous ranks starting at 1")
    play_ids = [play.play_id for play in ordered]
    if len(play_ids) != len(set(play_ids)):
        raise ValueError("complete plays contain duplicate play ids")
    supplied_ids = [play.play_id for play in play_set.plays]
    if supplied_ids != play_ids and sorted(supplied_ids) == sorted(play_ids):
        raise ValueError("complete plays must be supplied in rank order")
    pile_ids = {pile.pile_id for pile in money_map.piles}
    for play in ordered:
        if play.pile_id not in pile_ids:
            raise ValueError(f"play references unknown pile_id: {play.pile_id}")
    for family in {play.pile_id for play in ordered}:
        if len([pile for pile in money_map.piles if pile.pile_id == family]) != 1:
            raise ValueError(f"play pile_id is missing or ambiguous: {family}")
    piles = []
    for pile in money_map.piles:
        target_id = (
            pile.navigation.target_id
            if pile.navigation is not None
            else f"pile/{pile.pile_id}/{pile.currency}"
        )
        linked = [play for play in ordered if play.pile_id == pile.pile_id]
        if linked:
            navigation = MoneyMapNavigationV1(
                target_id=target_id,
                state="available",
                play_id=linked[0].play_id,
                alternative_play_ids=[play.play_id for play in linked[1:]] or None,
                next_action="review_linked_play",
            )
        else:
            navigation = MoneyMapNavigationV1(
                target_id=target_id,
                state="deferred",
                play_id=None,
                next_action="strategy_review_pending",
            )
        piles.append(pile.model_copy(update={"navigation": navigation}))
    data = money_map.model_dump()
    data.update(
        {
            "piles": piles,
            "recommended_play_ids": play_ids,
            "strategy_stage": "completed",
            "next_action": "review_recommended_play",
        }
    )
    return MoneyMapV1.model_validate(data)


def build_thin_slice_strategized_money_map(
    *,
    run_id: str = "run_thin_slice_strategized_money_map",
) -> tuple[MoneyMapV1, StrategyEvidencePacketV1, RecoveryPlaySetV1]:
    """Compose FM-004 thin-slice Money Map with fixture Recovery Plays."""
    money_map = build_thin_slice_money_map(run_id=run_id)
    packet = build_strategy_evidence_packet(money_map)
    play_set = FixtureStrategyProvider().propose(packet)
    enriched = apply_recovery_plays_to_money_map(money_map, play_set)
    return enriched, packet, play_set


def public_recovery_play_projection(
    play_set: RecoveryPlaySetV1,
) -> PublicRecoveryPlayProjectionV1:
    rows = [
        PublicRecoveryPlayRowV1(
            play_id=play.play_id,
            pile_id=play.pile_id,
            rank=play.rank,
            title=play.title,
            action_labels=list(play.recommended_actions),
        )
        for play in play_set.plays
    ]
    rows.sort(key=lambda item: item.rank)
    return PublicRecoveryPlayProjectionV1(
        run_id=play_set.run_id,
        built_at=play_set.built_at,
        provider=play_set.provider,
        plays=rows,
    )


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


def write_strategy_evidence_packet(
    output_root: Path | str,
    relative_path: str,
    packet: StrategyEvidencePacketV1,
) -> Path:
    destination = _validate_relative_under_root(Path(output_root), relative_path)
    _atomic_write_bytes(destination, packet.to_canonical_json())
    return destination


def write_recovery_plays(
    output_root: Path | str,
    relative_path: str,
    play_set: RecoveryPlaySetV1,
) -> Path:
    destination = _validate_relative_under_root(Path(output_root), relative_path)
    _atomic_write_bytes(destination, play_set.to_canonical_json())
    return destination


def write_public_recovery_play_projection(
    output_root: Path | str,
    relative_path: str,
    projection: PublicRecoveryPlayProjectionV1,
) -> Path:
    destination = _validate_relative_under_root(Path(output_root), relative_path)
    _atomic_write_bytes(destination, projection.to_canonical_json())
    return destination


def _object_pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate JSON object key: {key}")
        out[key] = value
    return out


def parse_canonical_json(
    data: bytes,
) -> (
    StrategyEvidencePacketV1
    | RecoveryPlaySetV1
    | PublicRecoveryPlayProjectionV1
    | CompleteRecoveryPlaySetV1
):
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("canonical JSON must be bytes")
    try:
        payload = json.loads(data.decode("utf-8"), object_pairs_hook=_object_pairs_hook)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"malformed JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("canonical JSON root must be an object")
    schema_version = payload.get("schema_version")
    if not isinstance(schema_version, str):
        raise ValueError("schema_version is required")
    match = _SCHEMA_MAJOR_RE.fullmatch(schema_version.strip())
    if match is None:
        raise ValueError(f"unrecognized schema_version: {schema_version!r}")
    name, major = match.group("name"), int(match.group("major"))
    if name == "strategy-evidence-packet":
        if major != 1:
            raise ValueError(f"unknown major schema version: {schema_version}")
        model: (
            StrategyEvidencePacketV1
            | RecoveryPlaySetV1
            | PublicRecoveryPlayProjectionV1
            | CompleteRecoveryPlaySetV1
        ) = StrategyEvidencePacketV1.model_validate(payload)
    elif name == "recovery-plays":
        if major != 1:
            raise ValueError(f"unknown major schema version: {schema_version}")
        if (
            payload.get("plays")
            and isinstance(payload["plays"], list)
            and isinstance(payload["plays"][0], dict)
            and "campaign_name" in payload["plays"][0]
        ):
            model = CompleteRecoveryPlaySetV1.model_validate(payload)
        else:
            model = RecoveryPlaySetV1.model_validate(payload)
    elif name == "recovery-plays-public":
        if major != 1:
            raise ValueError(f"unknown major schema version: {schema_version}")
        model = PublicRecoveryPlayProjectionV1.model_validate(payload)
    else:
        raise ValueError(f"unknown major schema version: {schema_version}")
    if model.to_canonical_json() != bytes(data):
        raise ValueError("serialized bytes are not exactly canonical")
    return model


# FM-025 provider boundary exports live here to preserve the original thin-slice API.
from found_money.strategy.boundary import (  # noqa: E402
    ConfiguredStrategyProvider,
    FixtureGroundedStrategyProvider,
    GroundingError,
    ProviderRefusalError,
    ProviderTimeoutError,
    StrategyExecution,
    StrategyProvider,
    build_grounded_strategy_packet,
    build_provider_request,
    decision_flags,
    parse_strategy_boundary_json,
    public_strategy_boundary_projection,
    render_strategy_boundary_mechanical_html,
    run_strategy_boundary,
    validate_grounded_draft,
    write_strategy_boundary_artifact,
    write_strategy_audit_receipt,
    write_strategy_provider_request,
    write_strategy_provider_response,
)

__all__ = [
    "ConfiguredStrategyProvider",
    "FixtureGroundedStrategyProvider",
    "FixtureStrategyProvider",
    "GroundingError",
    "ProviderRefusalError",
    "ProviderTimeoutError",
    "StrategyExecution",
    "StrategyProvider",
    "apply_complete_plays_to_money_map",
    "apply_recovery_plays_to_money_map",
    "build_grounded_strategy_packet",
    "build_provider_request",
    "build_strategy_evidence_packet",
    "build_thin_slice_strategized_money_map",
    "decision_flags",
    "parse_canonical_json",
    "parse_strategy_boundary_json",
    "public_recovery_play_projection",
    "public_strategy_boundary_projection",
    "run_strategy_boundary",
    "render_strategy_boundary_mechanical_html",
    "validate_grounded_draft",
    "write_public_recovery_play_projection",
    "write_recovery_plays",
    "write_strategy_boundary_artifact",
    "write_strategy_audit_receipt",
    "write_strategy_evidence_packet",
    "write_strategy_provider_request",
    "write_strategy_provider_response",
]

from found_money.strategy.campaign import (  # noqa: E402
    CanonicalSaasStrategyRun as CanonicalSaasStrategyRun,
    CompleteStrategyGroundingError as CompleteStrategyGroundingError,
    build_canonical_ecommerce_recovery_strategy as build_canonical_ecommerce_recovery_strategy,
    build_canonical_saas_recovery_strategy as build_canonical_saas_recovery_strategy,
    build_canonical_service_recovery_strategy as build_canonical_service_recovery_strategy,
    build_withheld_payment_dependent_service_strategy as build_withheld_payment_dependent_service_strategy,
    build_differentiation_report as build_differentiation_report,
    canonical_ecommerce_business_profile as canonical_ecommerce_business_profile,
    canonical_saas_business_profile as canonical_saas_business_profile,
    canonical_service_business_profile as canonical_service_business_profile,
    parse_complete_recovery_plays as parse_complete_recovery_plays,
    public_complete_recovery_play_projection as public_complete_recovery_play_projection,
    render_recovery_plays_field_presence_html as render_recovery_plays_field_presence_html,
    validate_complete_recovery_play_set as validate_complete_recovery_play_set,
    validate_reviewer_packet as validate_reviewer_packet,
)
from found_money.strategy.author import (  # noqa: E402
    generate_recovery_plays as generate_recovery_plays,
    generate_stub_recovery_plays as generate_stub_recovery_plays,
)

__all__.extend(
    [
        "CanonicalSaasStrategyRun",
        "CompleteStrategyGroundingError",
        "build_canonical_ecommerce_recovery_strategy",
        "build_canonical_saas_recovery_strategy",
        "build_canonical_service_recovery_strategy",
        "build_withheld_payment_dependent_service_strategy",
        "build_differentiation_report",
        "canonical_ecommerce_business_profile",
        "canonical_saas_business_profile",
        "canonical_service_business_profile",
        "generate_recovery_plays",
        "generate_stub_recovery_plays",
        "parse_complete_recovery_plays",
        "public_complete_recovery_play_projection",
        "render_recovery_plays_field_presence_html",
        "validate_complete_recovery_play_set",
        "validate_reviewer_packet",
    ]
)
