"""Deterministic activation launch-pack builders (FM-031)."""

from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal, Mapping, Sequence

from found_money.activation.intake import (
    MATT_EMERALD_EXPORT_INSTRUCTIONS,
    STEALADS_MANUAL_INSTRUCTIONS,
)
from found_money.contracts.activation import (
    PAYMENT_RESCUE_PILE_ID,
    PAYMENT_RESCUE_SEGMENT_ID,
    PRIVATE_ONLY_ARTIFACT_CLASSES,
    REQUIRED_ARTIFACT_CLASSES,
    ActivationPolicyV1,
    ActivationSourceIdV1,
    CreativeHandoffCardV1,
    CreativeHandoffV1,
    IdentityOverrideBindingV1,
    LaunchPackFileEntryV1,
    LaunchPackManifestV1,
    LaunchPackMode,
    MemberValueBasisV1,
    PrivateSegmentFileV1,
    PrivateSegmentMemberV1,
    ProductionBriefCardV1,
    ProductionBriefV1,
    PublicSegmentAggregateV1,
    PublicSegmentFileV1,
    WithheldAssetLedgerV1,
    WithheldAssetV1,
    canonical_email_task_activation_policy,
)
from found_money.contracts.campaign import CompleteRecoveryPlaySetV1, CompleteRecoveryPlayV1
from found_money.contracts.events import ExclusionLedgerV1, RecoveryCandidateSetV1
from found_money.contracts.strategy import RecoveryPlaySetV1
from found_money.contracts.identity import IdentityGraphV1, IdentityOverrideEnvelopeV1
from found_money.contracts.map import MoneyMapV1
from found_money.contracts.strategy import (
    GroundedStrategyEvidencePacketV1,
    StrategyBusinessProfileV1,
)
from found_money.contracts.value import ContributionLedgerV1

_PLACEHOLDER_RE = re.compile(r"(?i)\b(?:TODO|TBD|DRAFT|lorem)\b|\[CONFIRM")
_GENERIC_PLAY_RE = re.compile(r"(?i)\b(?:generic|placeholder|fallback)\s+play\b")
_PUBLIC_IDENTITY_RE = re.compile(
    r'(?i)("(?:email|phone|name|company|source_id|record_id|contact_id|crm_url|deal_name|'
    r'external_ids|customer_token|members)"\s*:|https?://|'
    r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b|"
    r"\b(?:cus|hs_contact|inv)_[a-z0-9_-]+\b)"
)


def _assert_activation_public_safe(payload: Any) -> None:
    """Reject customer identity in public activation artifacts."""
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    if _PUBLIC_IDENTITY_RE.search(text):
        raise ValueError("public activation output contains a sensitive identifier or URL")


# Business-profile gaps map to dependent launch-pack asset classes.
_MISSING_INPUT_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "product": ("offer_landing_briefs", "complete_copy"),
    "proof": ("creative_handoff", "production_brief"),
    "margin": ("offer_landing_briefs",),
    "channel": ("complete_copy", "tracking_plan"),
    "capacity": ("calendar",),
    "destination": ("offer_landing_briefs", "tracking_plan"),
}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def segment_id_for(_play_id: str | None = None) -> str:
    """Stable payment-rescue audience segment shared by all three V1 plays."""
    return PAYMENT_RESCUE_SEGMENT_ID


def _segment_id_for_pile(pile_id: str) -> str:
    if pile_id == PAYMENT_RESCUE_PILE_ID:
        return PAYMENT_RESCUE_SEGMENT_ID
    return f"seg_{pile_id}"


def _primary_pile_id(money_map: MoneyMapV1) -> str:
    ranked = sorted(money_map.piles, key=lambda item: item.rank)
    if not ranked:
        raise ValueError("launch pack requires a ranked Money Map pile")
    return ranked[0].pile_id


def _membership_event_family(pile_id: str) -> str:
    if pile_id == PAYMENT_RESCUE_PILE_ID:
        return "failed_payment"
    return pile_id


def _canonical_json_bytes(payload: Any) -> bytes:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return (text + "\n").encode("utf-8")


def _reject_placeholders(text: str, *, label: str) -> None:
    if _PLACEHOLDER_RE.search(text) or _GENERIC_PLAY_RE.search(text):
        raise ValueError(f"{label} contains placeholder or generic play content")


@dataclass(frozen=True)
class LaunchPackInputs:
    """Frozen upstream artifacts consumed by the launch-pack builder."""

    money_map: MoneyMapV1
    recovery_plays: CompleteRecoveryPlaySetV1 | RecoveryPlaySetV1
    evidence_packet: GroundedStrategyEvidencePacketV1
    contribution_ledger: ContributionLedgerV1
    identity_graph: IdentityGraphV1
    candidates: RecoveryCandidateSetV1
    exclusions: ExclusionLedgerV1 | None = None
    business_profile: StrategyBusinessProfileV1 | None = None
    source_snapshots: Mapping[str, Mapping[str, Any]] | None = None
    withheld_decisions: Sequence[WithheldAssetV1] = ()
    activation_policy: ActivationPolicyV1 | None = None
    identity_override: IdentityOverrideEnvelopeV1 | None = None
    mode: LaunchPackMode = "public"


@dataclass(frozen=True)
class LaunchPackBuild:
    payloads: dict[str, bytes]
    manifest: LaunchPackManifestV1
    withheld: WithheldAssetLedgerV1
    private_segments: dict[str, PrivateSegmentFileV1]
    public_segments: PublicSegmentFileV1
    status: str
    withheld_asset_ids: tuple[str, ...]
    mode: LaunchPackMode
    activation_policy: ActivationPolicyV1


@dataclass
class _MembershipBuild:
    private: PrivateSegmentFileV1 | None
    eligible_count: int
    excluded_count: int
    ambiguous_count: int
    excluded_reasons: list[str] = field(default_factory=list)
    ambiguous_reasons: list[str] = field(default_factory=list)
    withhold_segment: bool = False
    withhold_reason: str | None = None


def _profile_gaps(profile: StrategyBusinessProfileV1 | None) -> list[str]:
    if profile is None:
        return sorted(_MISSING_INPUT_DEPENDENCIES)
    gaps: list[str] = []
    for field_name in _MISSING_INPUT_DEPENDENCIES:
        if getattr(profile, field_name) is None:
            gaps.append(field_name)
    return gaps


def _ambiguous_member_ids(graph: IdentityGraphV1) -> set[str]:
    return {
        node_id for cluster in graph.ambiguous_identities for node_id in cluster.member_node_ids
    }


def _ambiguous_reason_lookup(graph: IdentityGraphV1) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for cluster in graph.ambiguous_identities:
        for node_id in cluster.member_node_ids:
            mapping[node_id] = cluster.reason
    return mapping


def _policy_source_ids(
    graph: IdentityGraphV1,
    customer_token: str,
    policy: ActivationPolicyV1,
) -> list[ActivationSourceIdV1] | None:
    """Return allowed source IDs, or None when required platform IDs are missing."""
    cluster = next(
        (item for item in graph.customers if item.customer_token == customer_token), None
    )
    if cluster is None:
        return None
    by_id = {node.node_id: node for node in graph.nodes}
    allowed = {(item.source_system, item.object_type) for item in policy.allowed_source_objects}
    required = {(item.source_system, item.object_type) for item in policy.required_source_objects}
    rows: list[ActivationSourceIdV1] = []
    present_types: set[tuple[str, str]] = set()
    for node_id in cluster.member_node_ids:
        node = by_id.get(node_id)
        if node is None:
            continue
        key = (node.source_system, node.object_type)
        if key not in allowed:
            continue
        present_types.add(key)
        rows.append(
            ActivationSourceIdV1(
                source_system=node.source_system,
                object_type=node.object_type,
                source_id=node.source_id,
            )
        )
    if not required.issubset(present_types):
        return None
    rows.sort(key=lambda item: (item.source_system, item.object_type, item.source_id))
    return rows


def _exclusion_lookup(exclusions: ExclusionLedgerV1 | None) -> dict[str, str]:
    if exclusions is None:
        return {}
    return {record.candidate_key: record.reason_code for record in exclusions.exclusions}


def _value_basis_for(contribution) -> MemberValueBasisV1:
    basis = contribution.value_basis
    amount = contribution.amount_minor
    if amount is None:
        return MemberValueBasisV1(basis="unquantified")
    if Decimal(amount) == 0:
        # Missing/zero source value stays unquantified — never synthesize zero.
        return MemberValueBasisV1(basis="unquantified")
    return MemberValueBasisV1(
        basis=basis,
        currency=contribution.currency,
        amount_minor=amount,
    )


def _override_binding(
    override: IdentityOverrideEnvelopeV1 | None,
    graph: IdentityGraphV1,
) -> IdentityOverrideBindingV1 | None:
    if override is None:
        return None
    target_members = set(override.member_node_ids)
    resolved = [
        customer for customer in graph.customers if set(customer.member_node_ids) == target_members
    ]
    if len(resolved) != 1:
        raise ValueError("identity override binding requires exactly one resolved target customer")
    if any(target_members & set(cluster.member_node_ids) for cluster in graph.ambiguous_identities):
        raise ValueError("identity override target remains ambiguous")
    return IdentityOverrideBindingV1(
        target_cluster_hash=override.target_cluster_hash,
        integrity=override.integrity,
        member_node_ids=list(override.member_node_ids),
    )


def _build_membership(
    *,
    play_ids: Sequence[str],
    inputs: LaunchPackInputs,
    policy: ActivationPolicyV1,
    money_map_sha: str,
    plays_sha: str,
    evidence_sha: str,
    value_sha: str,
    policy_sha: str,
    pile_id: str | None = None,
) -> _MembershipBuild:
    if not inputs.money_map.piles:
        return _MembershipBuild(private=None, eligible_count=0, excluded_count=0, ambiguous_count=0)
    pile_id = pile_id or _primary_pile_id(inputs.money_map)
    segment_id = _segment_id_for_pile(pile_id)
    event_family = _membership_event_family(pile_id)
    ambiguous_nodes = _ambiguous_member_ids(inputs.identity_graph)
    ambiguous_reasons = _ambiguous_reason_lookup(inputs.identity_graph)
    excluded_by_candidate = _exclusion_lookup(inputs.exclusions)
    candidates = sorted(
        (
            candidate
            for candidate in inputs.candidates.candidates
            if candidate.event_family == event_family
        ),
        key=lambda candidate: candidate.candidate_key,
    )
    candidates_by_token: dict[str, list[Any]] = {}
    for candidate in candidates:
        candidates_by_token.setdefault(candidate.customer_token, []).append(candidate)
    contributions_by_candidate = {
        row.candidate_key or f"{event_family}:{row.candidate_economic_unit_key}": row
        for row in inputs.contribution_ledger.contributions
        if row.pile_id == pile_id
    }

    members: list[PrivateSegmentMemberV1] = []
    seen_tokens: set[str] = set()
    excluded_count = 0
    ambiguous_count = sum(
        len(cluster.member_node_ids) for cluster in inputs.identity_graph.ambiguous_identities
    )
    excluded_reasons: list[str] = []
    amb_reasons: list[str] = sorted(
        {cluster.reason for cluster in inputs.identity_graph.ambiguous_identities}
    )
    missing_required = False

    for token, token_candidates in sorted(candidates_by_token.items()):
        if token in seen_tokens:
            continue
        eligible_candidates = [
            candidate
            for candidate in token_candidates
            if candidate.candidate_key not in excluded_by_candidate
        ]
        if not eligible_candidates:
            excluded_count += 1
            excluded_reasons.extend(
                excluded_by_candidate[candidate.candidate_key]
                for candidate in token_candidates
                if candidate.candidate_key in excluded_by_candidate
            )
            continue
        cluster = next(
            (item for item in inputs.identity_graph.customers if item.customer_token == token),
            None,
        )
        if cluster is None:
            continue
        overlap = set(cluster.member_node_ids) & ambiguous_nodes
        if overlap:
            for node_id in sorted(overlap):
                amb_reasons.append(ambiguous_reasons.get(node_id, "ambiguous_identity"))
            continue
        source_ids = _policy_source_ids(inputs.identity_graph, token, policy)
        if source_ids is None:
            missing_required = True
            continue
        contributions = sorted(
            (
                contributions_by_candidate[candidate.candidate_key]
                for candidate in eligible_candidates
                if candidate.candidate_key in contributions_by_candidate
            ),
            key=lambda row: row.economic_unit_key,
        )
        seen_tokens.add(token)
        if not play_ids:
            continue
        members.append(
            PrivateSegmentMemberV1(
                segment_id=segment_id,
                play_ids=list(play_ids),
                customer_token=token,
                source_ids=source_ids,
                value_basis=(
                    _value_basis_for(contributions[0])
                    if contributions
                    else MemberValueBasisV1(basis="unquantified")
                ),
                personalization_allowlist=list(policy.personalization_field_allowlist),
                exclusion_status="included",
            )
        )

    if missing_required:
        return _MembershipBuild(
            private=None,
            eligible_count=0,
            excluded_count=excluded_count,
            ambiguous_count=ambiguous_count,
            excluded_reasons=sorted(set(excluded_reasons)),
            ambiguous_reasons=sorted(set(amb_reasons)),
            withhold_segment=True,
            withhold_reason="missing_required_platform_id",
        )

    members.sort(key=lambda item: item.customer_token)
    if not play_ids:
        return _MembershipBuild(
            private=None,
            eligible_count=len(seen_tokens),
            excluded_count=excluded_count,
            ambiguous_count=ambiguous_count,
            excluded_reasons=sorted(set(excluded_reasons)),
            ambiguous_reasons=sorted(set(amb_reasons)),
        )
    private = PrivateSegmentFileV1(
        run_id=inputs.money_map.run_id,
        segment_id=segment_id,
        play_ids=list(play_ids),
        money_map_sha256=money_map_sha,
        recovery_plays_sha256=plays_sha,
        evidence_packet_sha256=evidence_sha,
        value_ledger_sha256=value_sha,
        activation_policy_sha256=policy_sha,
        identity_override=_override_binding(inputs.identity_override, inputs.identity_graph),
        members=members,
    )
    return _MembershipBuild(
        private=private,
        eligible_count=len(members),
        excluded_count=excluded_count,
        ambiguous_count=ambiguous_count,
        excluded_reasons=sorted(set(excluded_reasons)),
        ambiguous_reasons=sorted(set(amb_reasons)),
    )


def _public_segments(
    *,
    run_id: str,
    play_ids: Sequence[str],
    membership: _MembershipBuild,
    money_map_sha: str,
    plays_sha: str,
    evidence_sha: str,
    value_sha: str,
    segment_id: str,
) -> PublicSegmentFileV1:
    aggregates = [
        PublicSegmentAggregateV1(
            segment_id=segment_id,
            play_ids=list(play_ids),
            eligible_member_count=membership.eligible_count,
            withheld_exclusion_count=membership.excluded_count,
            withheld_ambiguous_count=membership.ambiguous_count,
        )
    ]
    public = PublicSegmentFileV1(
        run_id=run_id,
        money_map_sha256=money_map_sha,
        recovery_plays_sha256=plays_sha,
        evidence_packet_sha256=evidence_sha,
        value_ledger_sha256=value_sha,
        segments=aggregates,
    )
    _assert_activation_public_safe(public.canonical_dict())
    return public


def _markdown_play_page(
    play: CompleteRecoveryPlayV1,
    *,
    segment_id: str,
    play_ids: Sequence[str],
    hashes: Mapping[str, str],
    present_links: Mapping[str, str],
) -> str:
    lines = [
        f"# {play.campaign_name}",
        "",
        f"Play ID: `{play.play_id}`",
        "",
        f"Segment ID: `{segment_id}`",
        "",
        "Audience play alternatives:",
    ]
    for play_id in play_ids:
        lines.append(f"- Alternative Play ID: `{play_id}`")
    lines.extend(
        [
            "",
            f"Rank: {play.rank}",
            "",
            f"Diagnosis: {play.diagnosis.text}",
            "",
            f"Offer: {next(rung.mechanism.text for rung in play.offer_ladder.rungs if rung.role == 'core')}",
            "",
            f"Evidence packet: `{hashes['evidence']}`",
            "",
            f"Value ledger: `{hashes['value']}`",
            "",
            f"Money map: `{hashes['money_map']}`",
            "",
            f"Recovery plays: `{hashes['plays']}`",
            "",
            "This page is an approval-only export. It does not activate, send, schedule, "
            "or create an audience.",
            "",
        ]
    )
    if present_links:
        lines.append("## Related assets")
        for label, href in present_links.items():
            lines.append(f"- [{label}]({href})")
        lines.append("")
    text = "\n".join(lines)
    _reject_placeholders(text, label=f"play page {play.play_id}")
    return text


def _markdown_copy(
    play: CompleteRecoveryPlayV1, *, segment_id: str, hashes: Mapping[str, str]
) -> str:
    lines = [
        f"# Complete copy — {play.play_id}",
        "",
        f"Segment: `{segment_id}`",
        f"Evidence: `{hashes['evidence']}`",
        f"Value: `{hashes['value']}`",
        "",
        "## Email sequence",
    ]
    for step in play.email_sequence:
        lines.extend(
            [
                f"### Step {step.order}",
                f"Subject: {step.subject.text}",
                f"Body: {step.body.text}",
                f"CTA: {step.cta.text}",
                "",
            ]
        )
    if play.sms.available:
        lines.append("## SMS")
        for message in play.sms.messages:
            lines.append(f"- {message.text}")
        lines.append("")
    else:
        lines.append("## SMS")
        lines.append(f"Unavailable: {play.sms.unavailable_reason}")
        lines.append("")
    lines.extend(
        [
            "## Task talk track",
            play.task_talk_track.text,
            "",
            "Approval-only export. No send or schedule action is performed.",
            "",
        ]
    )
    text = "\n".join(lines)
    _reject_placeholders(text, label=f"copy {play.play_id}")
    return text


def _markdown_calendar(
    play: CompleteRecoveryPlayV1, *, segment_id: str, hashes: Mapping[str, str]
) -> str:
    lines = [
        f"# Calendar — {play.play_id}",
        "",
        f"Segment: `{segment_id}`",
        f"Evidence: `{hashes['evidence']}`",
        f"Value: `{hashes['value']}`",
        "",
    ]
    for step in play.calendar:
        lines.append(f"- Day {step.day}: {step.action.text} (stop: {step.stop_condition.text})")
    lines.extend(["", "## Stop conditions"])
    for condition in play.stop_conditions:
        lines.append(f"- {condition.text}")
    lines.append("")
    lines.append("Approval-only export. Scheduling is not performed.")
    lines.append("")
    text = "\n".join(lines)
    _reject_placeholders(text, label=f"calendar {play.play_id}")
    return text


def _markdown_offer(
    play: CompleteRecoveryPlayV1, *, segment_id: str, hashes: Mapping[str, str]
) -> str:
    core_rung = next(rung for rung in play.offer_ladder.rungs if rung.role == "core")
    lines = [
        f"# Offer and landing brief — {play.play_id}",
        "",
        f"Segment: `{segment_id}`",
        f"Evidence: `{hashes['evidence']}`",
        f"Value: `{hashes['value']}`",
        "",
        f"Mechanism: {core_rung.mechanism.text}",
        f"Rationale: {core_rung.rationale.text}",
        "",
        "## Constraints",
    ]
    for constraint in core_rung.constraints:
        lines.append(f"- {constraint.text}")
    lines.extend(
        [
            "",
            f"Primary CTA: {play.primary_cta.text}",
            "",
            "This is an approval brief, not a live landing-page deployment.",
            "",
        ]
    )
    text = "\n".join(lines)
    _reject_placeholders(text, label=f"offer {play.play_id}")
    return text


def _markdown_tracking(
    play: CompleteRecoveryPlayV1, *, segment_id: str, hashes: Mapping[str, str]
) -> str:
    lines = [
        f"# Tracking plan — {play.play_id}",
        "",
        f"Segment: `{segment_id}`",
        f"Evidence: `{hashes['evidence']}`",
        f"Value: `{hashes['value']}`",
        "",
        f"Success event: {play.tracking.success_event.text}",
        "",
        "## Signals",
    ]
    for signal in play.tracking.tracked_signals:
        lines.append(f"- {signal.text}")
    lines.append("")
    lines.append("Approval-only export. No tracking pixels or audiences are created.")
    lines.append("")
    text = "\n".join(lines)
    _reject_placeholders(text, label=f"tracking {play.play_id}")
    return text


def _markdown_checklist(
    plays: Sequence[CompleteRecoveryPlayV1],
    *,
    segment_id: str,
    hashes: Mapping[str, str],
    withheld: Sequence[str],
) -> str:
    lines = [
        "# Launch checklist",
        "",
        "Status: approval_only_export",
        "",
        f"Money map: `{hashes['money_map']}`",
        f"Recovery plays: `{hashes['plays']}`",
        f"Evidence: `{hashes['evidence']}`",
        f"Value ledger: `{hashes['value']}`",
        "",
        f"Segment: `{segment_id}`",
        "",
        f"## Plays (one {segment_id.removeprefix('seg_')} audience)",
    ]
    for play in plays:
        lines.append(f"- Play `{play.play_id}` → segment `{segment_id}`")
    lines.extend(
        [
            "",
            "## Human gates",
            "- [ ] Review private segment membership source IDs",
            "- [ ] Confirm withheld assets are acceptable",
            "- [ ] Approve copy, calendar, offer, tracking, creative handoff, and production brief",
            "- [ ] Do not send, schedule, create audiences, or mutate CRM/payment/ad data from this pack",
            "",
        ]
    )
    if withheld:
        lines.append("## Withheld assets")
        for asset_id in withheld:
            lines.append(f"- {asset_id}")
        lines.append("")
    text = "\n".join(lines)
    _reject_placeholders(text, label="launch checklist")
    return text


def _private_csv(private: PrivateSegmentFileV1) -> bytes:
    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer,
        fieldnames=[
            "segment_id",
            "play_ids",
            "customer_token",
            "source_system",
            "object_type",
            "source_id",
            "value_basis",
            "currency",
            "amount_minor",
            "personalization_allowlist",
            "exclusion_status",
        ],
    )
    writer.writeheader()
    for member in private.members:
        for source in member.source_ids:
            writer.writerow(
                {
                    "segment_id": member.segment_id,
                    "play_ids": "|".join(member.play_ids),
                    "customer_token": member.customer_token,
                    "source_system": source.source_system,
                    "object_type": source.object_type,
                    "source_id": source.source_id,
                    "value_basis": member.value_basis.basis,
                    "currency": member.value_basis.currency or "",
                    "amount_minor": member.value_basis.amount_minor or "",
                    "personalization_allowlist": "|".join(member.personalization_allowlist),
                    "exclusion_status": member.exclusion_status,
                }
            )
    return buffer.getvalue().encode("utf-8")


def _public_csv(public: PublicSegmentFileV1) -> bytes:
    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer,
        fieldnames=[
            "segment_id",
            "play_ids",
            "eligible_member_count",
            "withheld_exclusion_count",
            "withheld_ambiguous_count",
        ],
    )
    writer.writeheader()
    for row in public.segments:
        writer.writerow(
            {
                "segment_id": row.segment_id,
                "play_ids": "|".join(row.play_ids),
                "eligible_member_count": row.eligible_member_count,
                "withheld_exclusion_count": row.withheld_exclusion_count,
                "withheld_ambiguous_count": row.withheld_ambiguous_count,
            }
        )
    return buffer.getvalue().encode("utf-8")


def _play_html(
    play: CompleteRecoveryPlayV1,
    *,
    segment_id: str,
    play_ids: Sequence[str],
    hashes: Mapping[str, str],
    present_links: Mapping[str, str],
) -> str:
    link_html = "".join(
        f'<p><a href="{html.escape(href, quote=True)}">{html.escape(label)}</a></p>\n'
        for label, href in present_links.items()
    )
    alternatives = "".join(
        f"<li>Alternative Play ID: {html.escape(play_id)}</li>\n" for play_id in play_ids
    )
    body = (
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n'
        f'<meta charset="utf-8">\n<title>{html.escape(play.campaign_name)}</title>\n'
        "</head>\n<body>\n"
        f"<h1>{html.escape(play.campaign_name)}</h1>\n"
        f"<p>Play ID: {html.escape(play.play_id)}</p>\n"
        f"<p>Segment ID: {html.escape(segment_id)}</p>\n"
        "<p>Audience play alternatives:</p>\n"
        f"<ul>\n{alternatives}</ul>\n"
        f"<p>Diagnosis: {html.escape(play.diagnosis.text)}</p>\n"
        f"<p>Evidence: {html.escape(hashes['evidence'])}</p>\n"
        f"<p>Value: {html.escape(hashes['value'])}</p>\n"
        "<p>Approval-only export. No activation is performed.</p>\n"
        f"{link_html}"
        '<p><a href="../manifest.json">Launch pack manifest</a></p>\n'
        "</body>\n</html>\n"
    )
    _reject_placeholders(body, label=f"play html {play.play_id}")
    return body


def _creative_handoff(
    play: CompleteRecoveryPlayV1,
    *,
    segment_id: str,
    hashes: Mapping[str, str],
) -> CreativeHandoffV1:
    handoff = CreativeHandoffV1(
        play_id=play.play_id,
        segment_id=segment_id,
        campaign_name=play.campaign_name,
        money_map_sha256=hashes["money_map"],
        recovery_plays_sha256=hashes["plays"],
        evidence_packet_sha256=hashes["evidence"],
        value_ledger_sha256=hashes["value"],
        concept_cards=[
            CreativeHandoffCardV1(
                card_id=card.card_id,
                card_name=card.card_name,
                format_style=card.format_style,
            )
            for card in play.concept_cards
        ],
        import_instructions=STEALADS_MANUAL_INSTRUCTIONS,
    )
    _assert_activation_public_safe(handoff.canonical_dict())
    return handoff


def _production_brief(
    play: CompleteRecoveryPlayV1,
    *,
    segment_id: str,
    hashes: Mapping[str, str],
) -> ProductionBriefV1:
    brief = ProductionBriefV1(
        play_id=play.play_id,
        segment_id=segment_id,
        campaign_name=play.campaign_name,
        money_map_sha256=hashes["money_map"],
        recovery_plays_sha256=hashes["plays"],
        evidence_packet_sha256=hashes["evidence"],
        value_ledger_sha256=hashes["value"],
        creative_big_idea=play.creative_big_idea.text,
        concept_cards=[
            ProductionBriefCardV1(
                card_id=card.card_id,
                card_name=card.card_name,
                format_style=card.format_style,
                production_requirements=list(card.production_requirements),
            )
            for card in play.concept_cards
        ],
        export_instructions=MATT_EMERALD_EXPORT_INSTRUCTIONS,
    )
    _assert_activation_public_safe(brief.canonical_dict())
    return brief


def _withheld_ledger(
    *,
    run_id: str,
    gaps: Sequence[str],
    plays: Sequence[CompleteRecoveryPlayV1],
    membership: _MembershipBuild,
    extra: Sequence[WithheldAssetV1],
    segment_id: str,
) -> WithheldAssetLedgerV1:
    assets: list[WithheldAssetV1] = list(extra)
    seen = {item.asset_id for item in assets}

    for gap in gaps:
        for asset_class in _MISSING_INPUT_DEPENDENCIES[gap]:
            for play in plays:
                asset_id = f"{asset_class}:{play.play_id}:{gap}"
                if asset_id in seen:
                    continue
                seen.add(asset_id)
                assets.append(
                    WithheldAssetV1(
                        asset_id=asset_id,
                        asset_class=asset_class,
                        reason=f"missing_business_input:{gap}",
                        depends_on=[gap],
                        play_id=play.play_id,
                        segment_id=segment_id,
                    )
                )

    if membership.excluded_count or membership.ambiguous_count:
        asset_id = f"exclusion_aggregate:{segment_id}"
        if asset_id not in seen:
            seen.add(asset_id)
            assets.append(
                WithheldAssetV1(
                    asset_id=asset_id,
                    asset_class="exclusion_aggregate",
                    reason="ineligible_members_omitted_from_private_segment",
                    depends_on=[],
                    segment_id=segment_id,
                    count=membership.excluded_count + membership.ambiguous_count,
                    reason_codes=sorted(
                        set(membership.excluded_reasons + membership.ambiguous_reasons)
                    ),
                )
            )

    if membership.withhold_segment:
        asset_id = f"private_segments:{segment_id}"
        if asset_id not in seen:
            assets.append(
                WithheldAssetV1(
                    asset_id=asset_id,
                    asset_class="private_segments",
                    reason=membership.withhold_reason or "missing_required_platform_id",
                    depends_on=["activation_policy"],
                    segment_id=segment_id,
                )
            )

    if not plays and any(
        item.reason in {"missing_payment", "unconfigured_strategy"}
        or item.asset_id in {"payment_dependent_output", "strategy_dependent_output"}
        for item in extra
    ):
        hold_reason = next(
            item.reason
            for item in extra
            if item.reason in {"missing_payment", "unconfigured_strategy"}
            or item.asset_id in {"payment_dependent_output", "strategy_dependent_output"}
        )
        for asset_class in (
            "play_pages",
            "complete_copy",
            "calendar",
            "offer_landing_briefs",
            "tracking_plan",
            "launch_checklist",
            "creative_handoff",
            "production_brief",
            "private_segments",
        ):
            if asset_class in seen:
                continue
            seen.add(asset_class)
            assets.append(
                WithheldAssetV1(
                    asset_id=asset_class,
                    asset_class=asset_class,
                    reason=hold_reason,
                    depends_on=[hold_reason],
                )
            )

    assets.sort(key=lambda item: item.asset_id)
    return WithheldAssetLedgerV1(run_id=run_id, assets=assets)


def build_launch_pack(inputs: LaunchPackInputs) -> LaunchPackBuild:
    """Build deterministic private and/or public-safe launch-pack payloads."""
    mode: LaunchPackMode = inputs.mode
    if mode not in {"public", "private"}:
        raise ValueError("launch pack mode must be public or private")

    strategy_hold = any(
        item.reason in {"missing_payment", "unconfigured_strategy"}
        or item.asset_id in {"payment_dependent_output", "strategy_dependent_output"}
        for item in inputs.withheld_decisions
    )
    strategy_withheld = (
        not inputs.recovery_plays.plays
        and strategy_hold
        and not inputs.money_map.recommended_play_ids
        and inputs.money_map.strategy_stage == "needs_strategy_review"
    )
    if isinstance(inputs.recovery_plays, CompleteRecoveryPlaySetV1):
        plays = sorted(inputs.recovery_plays.plays, key=lambda item: item.rank)
    elif strategy_withheld:
        plays = []
    else:
        raise ValueError("launch pack requires exactly three complete recovery plays")
    if len(plays) != 3 and not strategy_withheld:
        raise ValueError("launch pack requires exactly three complete recovery plays")
    for play in plays:
        _reject_placeholders(play.play_id, label="play_id")
        _reject_placeholders(play.campaign_name, label="campaign_name")

    policy = inputs.activation_policy or canonical_email_task_activation_policy()
    money_map_bytes = inputs.money_map.to_canonical_json()
    plays_bytes = inputs.recovery_plays.to_canonical_json()
    evidence_bytes = inputs.evidence_packet.to_canonical_json()
    value_bytes = inputs.contribution_ledger.to_canonical_json()
    policy_bytes = policy.to_canonical_json()
    hashes = {
        "money_map": sha256_bytes(money_map_bytes),
        "plays": sha256_bytes(plays_bytes),
        "evidence": sha256_bytes(evidence_bytes),
        "value": sha256_bytes(value_bytes),
        "policy": sha256_bytes(policy_bytes),
    }

    play_ids = [play.play_id for play in plays]
    grouped_plays: dict[str, list[CompleteRecoveryPlayV1]] = {}
    for play in plays:
        grouped_plays.setdefault(play.pile_id, []).append(play)
    if not grouped_plays:
        fallback = (
            _primary_pile_id(inputs.money_map)
            if inputs.money_map.piles
            else "no_eligible_opportunities"
        )
        grouped_plays[fallback] = []
    memberships = {}
    all_withheld = {}
    gaps = _profile_gaps(inputs.business_profile)
    for pile_id, group in grouped_plays.items():
        segment_id = _segment_id_for_pile(pile_id)
        membership = _build_membership(
            play_ids=[p.play_id for p in group],
            inputs=inputs,
            policy=policy,
            pile_id=pile_id,
            money_map_sha=hashes["money_map"],
            plays_sha=hashes["plays"],
            evidence_sha=hashes["evidence"],
            value_sha=hashes["value"],
            policy_sha=hashes["policy"],
        )
        memberships[segment_id] = membership
        withheld = _withheld_ledger(
            run_id=inputs.money_map.run_id,
            gaps=gaps,
            plays=group,
            membership=membership,
            extra=inputs.withheld_decisions,
            segment_id=segment_id,
        )
        all_withheld.update({item.asset_id: item for item in withheld.assets})
    withheld_ledger = WithheldAssetLedgerV1(
        run_id=inputs.money_map.run_id,
        assets=sorted(all_withheld.values(), key=lambda item: item.asset_id),
    )
    withheld_classes = {
        asset.asset_class
        for asset in withheld_ledger.assets
        if asset.asset_class != "exclusion_aggregate"
    }
    withheld_ids = tuple(sorted(asset.asset_id for asset in withheld_ledger.assets))

    public_rows = []
    private_segments: dict[str, PrivateSegmentFileV1] = {}
    for pile_id, group in grouped_plays.items():
        segment_id = _segment_id_for_pile(pile_id)
        membership = memberships[segment_id]
        public = _public_segments(
            run_id=inputs.money_map.run_id,
            play_ids=[p.play_id for p in group],
            membership=membership,
            money_map_sha=hashes["money_map"],
            plays_sha=hashes["plays"],
            evidence_sha=hashes["evidence"],
            value_sha=hashes["value"],
            segment_id=segment_id,
        )
        public_rows.extend(public.segments)
        if (
            mode == "private"
            and membership.private is not None
            and "private_segments" not in withheld_classes
            and not membership.withhold_segment
        ):
            private_segments[segment_id] = membership.private
    public_segments = public.model_copy(update={"segments": public_rows})

    payloads: dict[str, bytes] = {}
    file_entries: list[LaunchPackFileEntryV1] = []
    relative_links: list[str] = []
    written_relatives: set[str] = set()

    def add_file(
        relative: str,
        data: bytes,
        *,
        asset_class: str,
        status: Literal["ready", "withheld"] = "ready",
        play_id: str | None = None,
        segment_id_value: str | None = None,
    ) -> None:
        if relative in written_relatives:
            raise ValueError(f"duplicate launch-pack path: {relative}")
        written_relatives.add(relative)
        path = f"launch-pack/{relative}"
        payloads[path] = data
        file_entries.append(
            LaunchPackFileEntryV1(
                path=relative,
                sha256=sha256_bytes(data),
                asset_class=asset_class,
                status=status,
                play_id=play_id,
                segment_id=segment_id_value,
            )
        )
        if relative.endswith((".html", ".md", ".json", ".csv")):
            relative_links.append(relative)

    add_file("money-map.json", money_map_bytes, asset_class="money_map")
    add_file("recovery-plays.json", plays_bytes, asset_class="recovery_plays")
    add_file(
        "public/segments.json",
        public_segments.to_canonical_json(),
        asset_class="public_segments",
    )
    add_file(
        "public/segments.csv",
        _public_csv(public_segments),
        asset_class="public_segments",
    )
    add_file(
        "withheld-assets.json",
        withheld_ledger.to_canonical_json(),
        asset_class="withheld_ledger",
    )
    add_file(
        "activation-policy.json",
        policy_bytes,
        asset_class="activation_policy",
    )

    for segment_id, private in private_segments.items():
        add_file(
            f"private/segments/{segment_id}.json",
            private.to_canonical_json(),
            asset_class="private_segments",
            segment_id_value=segment_id,
        )
        add_file(
            f"private/segments/{segment_id}.csv",
            _private_csv(private),
            asset_class="private_segments",
            segment_id_value=segment_id,
        )

    # Materialize optional per-play assets only when not withheld.
    ready_by_play: dict[str, dict[str, str]] = {play.play_id: {} for play in plays}
    for play in plays:
        segment_id = _segment_id_for_pile(play.pile_id)
        if "complete_copy" not in withheld_classes:
            relative = f"copy/{play.play_id}-copy.md"
            add_file(
                relative,
                _markdown_copy(play, segment_id=segment_id, hashes=hashes).encode("utf-8"),
                asset_class="complete_copy",
                play_id=play.play_id,
                segment_id_value=segment_id,
            )
            ready_by_play[play.play_id]["Complete copy"] = f"../copy/{play.play_id}-copy.md"
        if "calendar" not in withheld_classes:
            relative = f"calendar/{play.play_id}-calendar.md"
            add_file(
                relative,
                _markdown_calendar(play, segment_id=segment_id, hashes=hashes).encode("utf-8"),
                asset_class="calendar",
                play_id=play.play_id,
                segment_id_value=segment_id,
            )
            ready_by_play[play.play_id]["Calendar"] = f"../calendar/{play.play_id}-calendar.md"
        if "offer_landing_briefs" not in withheld_classes:
            relative = f"offers/{play.play_id}-offer.md"
            add_file(
                relative,
                _markdown_offer(play, segment_id=segment_id, hashes=hashes).encode("utf-8"),
                asset_class="offer_landing_briefs",
                play_id=play.play_id,
                segment_id_value=segment_id,
            )
            ready_by_play[play.play_id]["Offer brief"] = f"../offers/{play.play_id}-offer.md"
        if "tracking_plan" not in withheld_classes:
            relative = f"tracking/{play.play_id}-tracking.md"
            add_file(
                relative,
                _markdown_tracking(play, segment_id=segment_id, hashes=hashes).encode("utf-8"),
                asset_class="tracking_plan",
                play_id=play.play_id,
                segment_id_value=segment_id,
            )
            ready_by_play[play.play_id]["Tracking plan"] = f"../tracking/{play.play_id}-tracking.md"
        if "creative_handoff" not in withheld_classes:
            handoff = _creative_handoff(play, segment_id=segment_id, hashes=hashes)
            relative = f"creative-handoff/{play.play_id}-creative-handoff.json"
            add_file(
                relative,
                handoff.to_canonical_json(),
                asset_class="creative_handoff",
                play_id=play.play_id,
                segment_id_value=segment_id,
            )
            ready_by_play[play.play_id]["Creative handoff"] = (
                f"../creative-handoff/{play.play_id}-creative-handoff.json"
            )
        if "production_brief" not in withheld_classes:
            brief = _production_brief(play, segment_id=segment_id, hashes=hashes)
            relative = f"production-brief/{play.play_id}-production-brief.json"
            add_file(
                relative,
                brief.to_canonical_json(),
                asset_class="production_brief",
                play_id=play.play_id,
                segment_id_value=segment_id,
            )
            ready_by_play[play.play_id]["Production brief"] = (
                f"../production-brief/{play.play_id}-production-brief.json"
            )

    for play in plays:
        segment_id = _segment_id_for_pile(play.pile_id)
        audience_play_ids = [p.play_id for p in grouped_plays[play.pile_id]]
        links = ready_by_play[play.play_id]
        add_file(
            f"plays/{play.play_id}.md",
            _markdown_play_page(
                play,
                segment_id=segment_id,
                play_ids=audience_play_ids,
                hashes=hashes,
                present_links=links,
            ).encode("utf-8"),
            asset_class="play_pages",
            play_id=play.play_id,
            segment_id_value=segment_id,
        )
        add_file(
            f"plays/{play.play_id}.html",
            _play_html(
                play,
                segment_id=segment_id,
                play_ids=audience_play_ids,
                hashes=hashes,
                present_links=links,
            ).encode("utf-8"),
            asset_class="play_pages",
            play_id=play.play_id,
            segment_id_value=segment_id,
        )

    for pile_id, group in grouped_plays.items():
        if "launch_checklist" in withheld_classes:
            continue
        segment_id = _segment_id_for_pile(pile_id)
        add_file(
            "checklist/launch-checklist.md"
            if len(grouped_plays) == 1
            else f"checklist/{segment_id}-launch-checklist.md",
            _markdown_checklist(
                group,
                segment_id=segment_id,
                hashes=hashes,
                withheld=withheld_ids,
            ).encode("utf-8"),
            asset_class="launch_checklist",
        )

    status: Literal["approval_ready", "partial", "withheld"]
    ready_classes = {entry.asset_class for entry in file_entries}
    if not withheld_ids:
        status = "approval_ready"
    elif ready_classes & {
        "complete_copy",
        "calendar",
        "offer_landing_briefs",
        "tracking_plan",
        "creative_handoff",
        "production_brief",
        "private_segments",
        "play_pages",
    }:
        status = "partial"
    else:
        status = "withheld"

    readme = (
        f"# Launch Pack\n\n"
        f"Mode: `{mode}`\n\n"
        f"Status: {status}\n\n"
        "This directory is an approval-only activation export. Generating it does not "
        "send messages, schedule outreach, create audiences, or write CRM, payment, or "
        "ad data."
        + (
            " Private source IDs appear only under `private/`.\n\n"
            if mode == "private"
            else " Public mode contains no private segment subtree.\n\n"
        )
        + f"Money map: `{hashes['money_map']}`\n\n"
        f"Recovery plays: `{hashes['plays']}`\n\n"
        f"Evidence packet: `{hashes['evidence']}`\n\n"
        f"Value ledger: `{hashes['value']}`\n"
    ).encode("utf-8")
    add_file("README.md", readme, asset_class="manifest")

    present_classes = {entry.asset_class for entry in file_entries}
    required = list(REQUIRED_ARTIFACT_CLASSES)
    if mode == "private" and any(not m.withhold_segment for m in memberships.values()):
        required.extend(PRIVATE_ONLY_ARTIFACT_CLASSES)
    missing_required = [
        name
        for name in required
        if name not in present_classes and name not in withheld_classes and name != "manifest"
    ]
    if missing_required:
        raise ValueError(f"launch pack missing required artifact classes: {missing_required}")

    if mode == "public" and any(path.startswith("launch-pack/private/") for path in payloads):
        raise ValueError("public launch pack must not contain a private subtree")

    manifest = LaunchPackManifestV1(
        run_id=inputs.money_map.run_id,
        mode=mode,
        status=status,
        reason=(
            "Canonical three-play launch pack is ready for human approval."
            if status == "approval_ready"
            else "One or more launch-pack assets are withheld pending human inputs."
        ),
        available_play_count=len(plays),
        required_v1_play_count=3,
        money_map_sha256=hashes["money_map"],
        recovery_plays_sha256=hashes["plays"],
        evidence_packet_sha256=hashes["evidence"],
        value_ledger_sha256=hashes["value"],
        activation_policy_sha256=hashes["policy"],
        play_ids=play_ids,
        segment_ids=list(memberships),
        files=file_entries,
        withheld_assets=list(withheld_ids),
        relative_links=sorted(set(relative_links)),
    )
    # Manifest hashes every other file and never itself.
    payloads["launch-pack/manifest.json"] = manifest.to_canonical_json()
    return LaunchPackBuild(
        payloads=payloads,
        manifest=manifest,
        withheld=withheld_ledger,
        private_segments=private_segments,
        public_segments=public_segments,
        status=status,
        withheld_asset_ids=withheld_ids,
        mode=mode,
        activation_policy=policy,
    )


def public_safe_launch_pack_projection(build: LaunchPackBuild) -> dict[str, Any]:
    """Public-safe summary without private segment membership."""
    projection = {
        "schema_version": "launch-pack-public.v1",
        "run_id": build.manifest.run_id,
        "mode": build.manifest.mode,
        "status": build.manifest.status,
        "approval_only": True,
        "export_only": True,
        "not_activated": True,
        "send_performed": False,
        "schedule_performed": False,
        "audience_created": False,
        "provider_write_performed": False,
        "play_ids": list(build.manifest.play_ids),
        "segment_ids": list(build.manifest.segment_ids),
        "withheld_assets": list(build.manifest.withheld_assets),
        "public_segments": build.public_segments.canonical_dict(),
        "file_count": len(build.manifest.files),
    }
    _assert_activation_public_safe(projection)
    return projection
