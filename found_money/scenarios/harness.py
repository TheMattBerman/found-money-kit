"""Reusable scenario engine for FM-033/034/035. Not a SaaS-only second runner."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping

from found_money.contracts.events import (
    EVENT_FAMILIES,
    EventDataGapLedgerV1,
    EventDataGapV1,
    ExclusionLedgerV1,
    PublicEventProjectionV1,
    public_named_data_gaps,
)
from found_money.contracts.identity import IdentityGraphV1
from found_money.contracts.map import MoneyMapV1
from found_money.contracts.scenarios import (
    ScenarioAggregateReceiptV1,
    ScenarioDefinitionV1,
    ScenarioIdentityAggregateV1,
    ScenarioManifestArtifactV1,
    ScenarioManifestV1,
    ScenarioSituationSetV1,
    ScenarioValueAggregateV1,
    ScenarioValuePileAggregateV1,
)
from found_money.contracts.source import SourceReceiptV1
from found_money.contracts.value import ContributionLedgerV1, ContributionV1
from found_money.events import detect_event_families
from found_money.identity import build_identity_graph, normalize_source_records
from found_money.map import build_money_map
from found_money.receipts import sha256_bytes
from found_money.scenarios.assets import (
    SYNTHETIC_SAAS_V1,
    load_scenario_definition,
    load_scenario_snapshot_bytes,
    load_scenario_snapshots,
)
from found_money.scenarios.registry import scenario_arm
from found_money.strategy import (
    CanonicalSaasStrategyRun,
    apply_complete_plays_to_money_map,
    build_canonical_ecommerce_recovery_strategy,
    build_canonical_saas_recovery_strategy,
    build_canonical_service_recovery_strategy,
    build_withheld_payment_dependent_service_strategy,
)
from found_money.value import build_value_ledger

SCENARIO_DEFINITION_PATH = "scenario/definition.json"
SCENARIO_MANIFEST_PATH = "scenario/manifest.json"
SCENARIO_AGGREGATE_PATH = "scenario/aggregate-receipt.json"
SCENARIO_EVENTS_PUBLIC_PATH = "events/public.json"
SCENARIO_IDENTITY_PUBLIC_PATH = "identity/public.json"
SCENARIO_VALUE_PUBLIC_PATH = "value/public.json"
SCENARIO_SITUATIONS_PATH = "scenario/situations.json"
SCENARIO_PUBLIC_PATHS = (
    SCENARIO_DEFINITION_PATH,
    SCENARIO_AGGREGATE_PATH,
    SCENARIO_SITUATIONS_PATH,
    SCENARIO_EVENTS_PUBLIC_PATH,
    SCENARIO_IDENTITY_PUBLIC_PATH,
    SCENARIO_VALUE_PUBLIC_PATH,
    SCENARIO_MANIFEST_PATH,
    "assets/three.min.js",
    "assets/OrbitControls.js",
    "assets/tween.umd.js",
    "assets/continent-texture.jpg",
)
RAW_SCENARIO_SOURCE_IDS = (
    "hs_ct_saas_fp_001",
    "hs_ct_saas_et_001",
    "hs_ct_saas_cc_001",
    "hs_ct_saas_cl_001",
    "hs_ct_saas_ru_001",
    "hs_dl_saas_cl_001",
    "cus_saas_fp_001",
    "cus_saas_et_001",
    "cus_saas_cc_001",
    "cus_saas_cl_001",
    "cus_saas_ru_001",
    "in_saas_fp_001",
    "sub_saas_et_001",
    "sub_saas_ru_001",
    "syn_saas_fp",
    "syn_saas_et",
    "syn_saas_cc",
    "syn_saas_cl",
    "syn_saas_ru",
)
RAW_ECOMMERCE_SOURCE_IDS = (
    "cus_ecom_lapse_001",
    "cus_ecom_reorder_001",
    "cus_ecom_vip_001",
    "cus_ecom_refund_001",
    "cus_ecom_mix_001",
    "ord_cust_lapse_001",
    "ord_cust_reorder_001",
    "ord_cust_vip_001",
    "ord_cust_refund_001",
    "ord_cust_mix_001",
    "ord_ecom_lapse_1",
    "ord_ecom_lapse_2",
    "ord_ecom_re_1",
    "ord_ecom_re_2",
    "ord_ecom_vip_1",
    "ord_ecom_vip_2",
    "ord_ecom_refund_1",
    "ord_ecom_mix_1",
    "ord_ecom_mix_2",
    "ch_ecom_refund_001",
    "re_ecom_refund_001",
    "cart_ecom_hv_001",
)
RAW_SERVICE_SOURCE_IDS = (
    "hs_ct_svc_ns_001",
    "hs_ct_svc_tn_001",
    "hs_ct_svc_cm_001",
    "hs_ct_svc_sp_001",
    "hs_ct_svc_hv_001",
    "cus_svc_ns_001",
    "cus_svc_tn_001",
    "cus_svc_cm_001",
    "cus_svc_sp_001",
    "cus_svc_hv_001",
    "sub_svc_tn_001",
    "in_svc_hv_001",
    "apt_cust_ns_001",
    "apt_svc_ns_001",
    "prp_cust_sp_001",
    "prp_svc_sp_001",
    "syn_svc_ns",
    "syn_svc_tn",
    "syn_svc_cm",
    "syn_svc_sp",
    "syn_svc_hv",
    "apt_cust_tn_001",
    "apt_svc_tn_ns_001",
    "apt_svc_tn_rb_001",
)

SERVICE_SOURCE_GAP_SPECS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "appointments": (
        "missing_appointments",
        "appointments.v1",
        ("no_show_rebook_dependent_output",),
    ),
    "proposals": ("missing_proposals", "proposals.v1", ("silent_proposal_dependent_output",)),
    "hubspot": ("missing_crm", "hubspot-crm.2026-03.v1", ("crm_dependent_output",)),
    "stripe": (
        "missing_payment",
        "stripe.2026-02-25.clover.v1",
        (
            "payment_dependent_output",
            "trial_no_convert_dependent_output",
            "canceled_customer_dependent_output",
            "disappeared_high_value_customer_dependent_output",
        ),
    ),
}


@dataclass(frozen=True)
class ScenarioEngineResult:
    definition: ScenarioDefinitionV1
    run_id: str
    raw_snapshots: dict[str, Mapping[str, Any]]
    snapshot_bytes: dict[str, bytes]
    graph: IdentityGraphV1
    candidates: Any
    exclusions: ExclusionLedgerV1
    event_public: PublicEventProjectionV1
    ledger: ContributionLedgerV1
    money_map: MoneyMapV1
    strategy_run: CanonicalSaasStrategyRun
    enriched_money_map: MoneyMapV1
    identity_aggregate: ScenarioIdentityAggregateV1
    value_aggregate: ScenarioValueAggregateV1
    aggregate_receipt: ScenarioAggregateReceiptV1
    typed_situations: tuple[str, ...] = ()
    withheld_asset_ids: tuple[str, ...] = ()
    data_gap_details: tuple[str, ...] = ()
    situation_set: ScenarioSituationSetV1 | None = None


def payment_rescue_ledger(ledger: ContributionLedgerV1) -> ContributionLedgerV1:
    """Map failed_payment contributions onto the canonical payment_rescue pile."""
    rows: list[ContributionV1] = []
    for row in ledger.contributions:
        if row.pile_id == "failed_payment":
            rows.append(row.model_copy(update={"pile_id": "payment_rescue"}))
        else:
            rows.append(row)
    return ledger.model_copy(update={"contributions": rows})


def _assert_detector_families(definition: ScenarioDefinitionV1, families: set[str]) -> None:
    expected = set(definition.expected_event_families)
    extra = families - expected
    missing = expected - families
    if missing:
        raise ValueError(
            "scenario detector did not emit required families: " + ", ".join(sorted(missing))
        )
    unexpected = extra.intersection(EVENT_FAMILIES) - expected
    if unexpected:
        raise ValueError(
            "scenario detector emitted overlapping extra families: " + ", ".join(sorted(unexpected))
        )


def _assert_expected_values(definition: ScenarioDefinitionV1, ledger: ContributionLedgerV1) -> None:
    if definition.business_model == "ecommerce":
        totals: dict[tuple[str, str, str], Decimal] = {}
        for contribution in ledger.contributions:
            pile_key = (
                str(contribution.pile_id),
                contribution.currency,
                str(contribution.value_basis),
            )
            totals[pile_key] = totals.get(pile_key, Decimal(0)) + contribution.amount_minor
        for expected in definition.expected_values:
            pile_key = (str(expected.pile_id), expected.currency, str(expected.value_basis))
            if expected.value_basis == "unquantified":
                if any(item[0] == expected.pile_id for item in totals):
                    raise ValueError(
                        f"{expected.event_family} was quantified despite unquantified lock"
                    )
                continue
            amount = totals.get(pile_key)
            if amount is None:
                raise ValueError(f"missing quantified value for {expected.event_family}")
            if amount != expected.amount_minor:
                raise ValueError(f"value mismatch for {expected.event_family}")
        return
    by_family: dict[str, ContributionV1] = {}
    for contribution in ledger.contributions:
        family = (
            "failed_payment" if contribution.pile_id == "payment_rescue" else contribution.pile_id
        )
        if family in by_family:
            raise ValueError(f"scenario value overlap erased family {family}")
        by_family[family] = contribution
    for expected in definition.expected_values:
        if expected.value_basis == "unquantified":
            if expected.event_family in by_family:
                raise ValueError(
                    f"{expected.event_family} was quantified despite unquantified lock"
                )
            continue
        matched = by_family.get(expected.event_family)
        if matched is None:
            raise ValueError(f"missing quantified value for {expected.event_family}")
        if matched.currency != expected.currency or matched.amount_minor != expected.amount_minor:
            raise ValueError(f"value mismatch for {expected.event_family}")
        if matched.value_basis != expected.value_basis:
            raise ValueError(f"value basis mismatch for {expected.event_family}")
        if matched.pile_id != expected.pile_id:
            raise ValueError(f"pile mismatch for {expected.event_family}")


def _identity_aggregate(graph: IdentityGraphV1, sources: list[str]) -> ScenarioIdentityAggregateV1:
    return ScenarioIdentityAggregateV1(
        run_id=graph.run_id,
        resolved_customer_count=len(graph.customers),
        ambiguous_cluster_count=len(graph.ambiguous_identities),
        source_systems=list(sources),
    )


def _value_aggregate(ledger: ContributionLedgerV1) -> ScenarioValueAggregateV1:
    totals: dict[str, Decimal] = {}
    merged: dict[tuple[str, str, str], ScenarioValuePileAggregateV1] = {}
    for row in ledger.contributions:
        totals[row.currency] = totals.get(row.currency, Decimal(0)) + row.amount_minor
        key = (row.pile_id, row.currency, row.value_basis)
        existing = merged.get(key)
        if existing is None:
            merged[key] = ScenarioValuePileAggregateV1(
                pile_id=row.pile_id,
                currency=row.currency,
                amount_minor=row.amount_minor,
                value_basis=row.value_basis,
                event_count=1,
            )
            continue
        merged[key] = existing.model_copy(
            update={
                "amount_minor": existing.amount_minor + row.amount_minor,
                "event_count": existing.event_count + 1,
            }
        )
    piles = sorted(merged.values(), key=lambda item: (item.pile_id, item.currency))
    return ScenarioValueAggregateV1(
        run_id=ledger.run_id,
        totals_minor_by_currency=dict(sorted(totals.items())),
        piles=piles,
        unquantified_count=len(ledger.data_gaps),
    )


def _ecommerce_situations(
    locked: ScenarioDefinitionV1,
    detection: Any,
    snapshots: Mapping[str, Mapping[str, Any]],
    graph: IdentityGraphV1,
    *,
    run_id: str,
    clock: datetime,
) -> tuple[tuple[str, ...], Any, tuple[str, ...], ScenarioSituationSetV1 | None]:
    if locked.business_model != "ecommerce":
        return (), detection, (), None
    from found_money.events.library import _public_projection
    from found_money.scenarios.assets import load_optional_cart_binding
    from found_money.scenarios.situations import build_ecommerce_situation_set, situation_data_gaps

    cart = load_optional_cart_binding(locked.fixture_id)
    if cart.state != locked.high_value_cart_state:
        raise ValueError("optional cart state does not match the locked definition")
    if cart.sha256() != locked.high_value_cart_hash:
        raise ValueError("optional cart hash does not match the locked definition")
    situation_set = build_ecommerce_situation_set(
        run_id=run_id,
        cart=cart,
        snapshots=snapshots,
        graph=graph,
        clock=clock,
    )
    if situation_set.high_value_cart_hash != locked.high_value_cart_hash:
        raise ValueError("situation cart hash does not match the locked definition")
    present = tuple(situation_set.present_ids())
    gaps = list(detection.data_gaps.gaps)
    gaps.extend(situation_data_gaps(situation_set, run_id=run_id))
    withheld: list[str] = []
    cart_row = next(
        item for item in situation_set.situations if item.situation_id == "high_value_cart"
    )
    if cart_row.status != "present":
        withheld.append("high_value_cart_dependent_output")
    data_gaps = EventDataGapLedgerV1(run_id=run_id, built_at=clock, gaps=gaps)
    public = _public_projection(detection.candidates, detection.exclusions, data_gaps)
    updated = detection.__class__(detection.candidates, detection.exclusions, data_gaps, public)
    return present, updated, tuple(withheld), situation_set


def _service_source_gaps(
    locked: ScenarioDefinitionV1,
    detection: Any,
    *,
    run_id: str,
    clock: datetime,
) -> tuple[Any, tuple[str, ...], tuple[str, ...]]:
    if locked.business_model != "service":
        return detection, (), ()
    omitted = tuple(locked.omitted_service_sources or ())
    if not omitted:
        return detection, (), ()
    from found_money.events.library import _public_projection

    gaps = list(detection.data_gaps.gaps)
    withheld: list[str] = []
    for source in omitted:
        spec = SERVICE_SOURCE_GAP_SPECS.get(source)
        if spec is None:
            raise ValueError(f"unsupported omitted service source: {source}")
        detail, reference, assets = spec
        gaps.append(
            EventDataGapV1(
                run_id=run_id,
                reason_code="missing_evidence",
                source_reference=reference,
                detail=detail,
            )
        )
        withheld.extend(assets)
    data_gaps = EventDataGapLedgerV1(run_id=run_id, built_at=clock, gaps=gaps)
    public = _public_projection(detection.candidates, detection.exclusions, data_gaps)
    updated = detection.__class__(detection.candidates, detection.exclusions, data_gaps, public)
    return updated, tuple(withheld), tuple(gap.detail for gap in data_gaps.gaps)


def run_scenario_engine(
    *,
    run_id: str,
    safe_config: Mapping[str, Any],
    fixture_id: str = SYNTHETIC_SAAS_V1,
    definition: ScenarioDefinitionV1 | None = None,
    snapshots: Mapping[str, Mapping[str, Any]] | None = None,
    snapshot_bytes: Mapping[str, bytes] | None = None,
) -> ScenarioEngineResult:
    """Run identity/event/value/ranking/strategy for one locked scenario."""
    locked = definition or load_scenario_definition(fixture_id)
    effective_fixture = locked.fixture_id
    raw_snapshots = dict(snapshots or load_scenario_snapshots(effective_fixture))
    raw_bytes = dict(snapshot_bytes or load_scenario_snapshot_bytes(effective_fixture))
    clock: datetime = locked.clock
    nodes = normalize_source_records(raw_snapshots, default_observed_at=clock)
    graph = build_identity_graph(nodes, run_id=run_id, built_at=clock)
    if len(graph.customers) != locked.expected_resolved_accounts:
        raise ValueError("scenario resolved account count does not match the definition")
    if graph.ambiguous_identities:
        raise ValueError("scenario resolved totals include an ambiguous identity")
    detection = detect_event_families(raw_snapshots, graph, run_id=run_id, built_at=clock)
    families = {str(item.event_family) for item in detection.candidates.candidates}
    _assert_detector_families(locked, families)
    customer_families: dict[str, set[str]] = {}
    for candidate in detection.candidates.candidates:
        customer_families.setdefault(candidate.customer_token, set()).add(candidate.event_family)
    arm = scenario_arm(locked.fixture_id)
    if arm.one_family_per_account:
        if any(len(items) != 1 for items in customer_families.values()):
            raise ValueError("scenario families overlap on a resolved account")
        if len(customer_families) != locked.expected_resolved_accounts:
            raise ValueError("scenario headline families do not cover every resolved account")
    typed_situations, detection, withheld, situation_set = _ecommerce_situations(
        locked, detection, raw_snapshots, graph, run_id=run_id, clock=clock
    )
    detection, service_withheld, _service_gap_details = _service_source_gaps(
        locked, detection, run_id=run_id, clock=clock
    )
    withheld = tuple(withheld) + service_withheld
    ledger = payment_rescue_ledger(
        build_value_ledger(detection.candidates, raw_snapshots, built_at=clock)
    )
    _assert_expected_values(locked, ledger)
    money_map = build_money_map(ledger, detection.candidates, built_at=clock)
    named_gaps = public_named_data_gaps([gap.detail for gap in detection.data_gaps.gaps])
    exclusions_by_reason = dict(detection.public_projection.exclusions_by_reason)
    money_map = money_map.model_copy(
        update={
            "named_data_gaps": named_gaps,
            "event_exclusions_by_reason": exclusions_by_reason,
        }
    )
    ranked = sorted(money_map.piles, key=lambda item: item.rank)
    if not ranked or ranked[0].pile_id != arm.primary_pile_id:
        raise ValueError(f"{arm.primary_pile_id} pile must rank first")
    if ranked[0].selected_value_minor != arm.primary_amount_minor or ranked[0].currency != "usd":
        raise ValueError(
            f"{arm.primary_pile_id} value must be exactly {arm.primary_amount_minor} usd"
        )
    omitted = set(locked.omitted_service_sources or [])
    payment_omitted = locked.business_model == "service" and "stripe" in omitted
    if arm.strategy_kind == "saas":
        strategy_run = build_canonical_saas_recovery_strategy(money_map, built_at=clock)
        covered_family = "failed_payment"
    elif arm.strategy_kind == "ecommerce":
        strategy_run = build_canonical_ecommerce_recovery_strategy(money_map, built_at=clock)
        covered_family = "disappeared_high_value_customer"
    elif payment_omitted:
        strategy_run = build_withheld_payment_dependent_service_strategy(money_map, built_at=clock)
        covered_family = "disappeared_high_value_customer"
    else:
        strategy_run = build_canonical_service_recovery_strategy(money_map, built_at=clock)
        covered_family = "disappeared_high_value_customer"
    if payment_omitted:
        if strategy_run.recovery_plays.plays:
            raise ValueError("missing payment published payment-grounded recovery plays")
        play_ids: list[str] = []
        card_ids: list[str] = []
        enriched = money_map.model_copy(update={"strategy_stage": "needs_strategy_review"})
    else:
        complete_plays = strategy_run.complete_plays()
        plays = sorted(complete_plays.plays, key=lambda item: item.rank)
        play_ids = [play.play_id for play in plays]
        card_ids = [card.card_id for play in plays for card in play.concept_cards]
        if play_ids != locked.expected_play_ids:
            raise ValueError("scenario play IDs do not match the locked definition")
        if card_ids != locked.expected_card_ids:
            raise ValueError("scenario card IDs do not match the locked definition")
        for play in plays:
            blob = play.to_canonical_json().decode("utf-8").casefold()
            for token in arm.forbidden_copy:
                if token in blob:
                    raise ValueError(
                        f"{arm.business_model} play copied forbidden language: {token}"
                    )
            for family in locked.expected_event_families:
                if family == covered_family:
                    continue
                if family.replace("_", " ") in blob or family in blob:
                    raise ValueError(f"play {play.play_id} falsely claims to cover {family}")
        enriched = apply_complete_plays_to_money_map(money_map, complete_plays)
    identity_aggregate = _identity_aggregate(graph, list(locked.sources))
    value_aggregate = _value_aggregate(ledger)
    aggregate = ScenarioAggregateReceiptV1(
        run_id=run_id,
        scenario_id=locked.scenario_id,
        built_at=clock,
        mode=safe_config["run_mode"],
        sources=list(locked.sources),
        resolved_customer_count=identity_aggregate.resolved_customer_count,
        ambiguous_cluster_count=identity_aggregate.ambiguous_cluster_count,
        candidates_by_family=dict(detection.public_projection.candidates_by_family),
        exclusion_count=detection.public_projection.exclusion_count,
        data_gap_count=detection.public_projection.data_gap_count,
        named_data_gaps=named_gaps or None,
        exclusions_by_reason=exclusions_by_reason or None,
        identified_opportunity_minor=dict(enriched.identified_opportunity_minor),
        play_ids=play_ids,
        card_ids=card_ids,
        primary_play_id=None if payment_omitted else play_ids[0],
        inherited_proof_contract=locked.inherited_proof_contract,
        high_value_cart_state=locked.high_value_cart_state,
        high_value_cart_hash=locked.high_value_cart_hash,
        situation_ids=None if situation_set is None else situation_set.present_ids(),
    )
    return ScenarioEngineResult(
        definition=locked,
        run_id=run_id,
        raw_snapshots=raw_snapshots,
        snapshot_bytes=raw_bytes,
        graph=graph,
        candidates=detection.candidates,
        exclusions=detection.exclusions,
        event_public=detection.public_projection,
        ledger=ledger,
        money_map=money_map,
        strategy_run=strategy_run,
        enriched_money_map=enriched,
        identity_aggregate=identity_aggregate,
        value_aggregate=value_aggregate,
        aggregate_receipt=aggregate,
        typed_situations=typed_situations,
        withheld_asset_ids=withheld,
        data_gap_details=tuple(gap.detail for gap in detection.data_gaps.gaps),
        situation_set=situation_set,
    )


def scenario_public_payloads(result: ScenarioEngineResult) -> dict[str, bytes]:
    payloads = {
        SCENARIO_DEFINITION_PATH: result.definition.to_canonical_json(),
        SCENARIO_AGGREGATE_PATH: result.aggregate_receipt.to_canonical_json(),
        SCENARIO_EVENTS_PUBLIC_PATH: result.event_public.to_canonical_json(),
        SCENARIO_IDENTITY_PUBLIC_PATH: result.identity_aggregate.to_canonical_json(),
        SCENARIO_VALUE_PUBLIC_PATH: result.value_aggregate.to_canonical_json(),
    }
    if result.situation_set is not None:
        payloads[SCENARIO_SITUATIONS_PATH] = result.situation_set.to_canonical_json()
    return payloads


def build_scenario_manifest(
    result: ScenarioEngineResult,
    payloads: Mapping[str, bytes],
    *,
    receipts: Mapping[str, SourceReceiptV1],
) -> ScenarioManifestV1:
    from found_money.scenarios.transports import packaged_fixture_hashes

    artifacts = [
        ScenarioManifestArtifactV1(path=path, sha256=sha256_bytes(data))
        for path, data in sorted(payloads.items())
        if path not in {SCENARIO_MANIFEST_PATH, "run.json"}
    ]
    locked = result.definition
    return ScenarioManifestV1(
        run_id=result.run_id,
        scenario_id=locked.scenario_id,
        mode=result.aggregate_receipt.mode,
        business_model=locked.business_model,
        sources=list(locked.sources),
        connector_schema_versions={
            name: receipts[name].connector_schema_version for name in locked.sources
        },
        fixture_schema_versions={
            name: receipts[name].connector_schema_version for name in locked.sources
        },
        fixture_hashes=packaged_fixture_hashes(locked.fixture_id),
        content_hashes={name: sha256_bytes(result.snapshot_bytes[name]) for name in locked.sources},
        source_counts={name: receipts[name].record_count for name in locked.sources},
        expected_event_families=list(locked.expected_event_families),
        candidates_by_family=dict(result.aggregate_receipt.candidates_by_family),
        play_ids=list(result.aggregate_receipt.play_ids),
        card_ids=list(result.aggregate_receipt.card_ids),
        definition_sha256=sha256_bytes(payloads[SCENARIO_DEFINITION_PATH]),
        aggregate_receipt_sha256=sha256_bytes(payloads[SCENARIO_AGGREGATE_PATH]),
        inherited_proof_contract=locked.inherited_proof_contract,
        artifacts=artifacts,
        high_value_cart_state=locked.high_value_cart_state,
        high_value_cart_hash=locked.high_value_cart_hash,
        situation_ids=None if result.situation_set is None else result.situation_set.present_ids(),
        named_data_gaps=list(result.aggregate_receipt.named_data_gaps or []) or None,
        exclusions_by_reason=dict(result.aggregate_receipt.exclusions_by_reason or {}) or None,
    )
