"""FM-022 twelve-family candidate, suppression, and identity-boundary proofs."""

from __future__ import annotations

import json
import socket
from datetime import datetime, timezone

import pytest

from found_money.contracts.events import (
    EVENT_FAMILIES,
    SUPPRESSION_PRIORITY,
    ExclusionLedgerV1,
    RecoveryCandidateSetV1,
)
from found_money.contracts.identity import IdentityNodeV1
from found_money.events import (
    EventDetectionConfig,
    detect_event_families,
    parse_data_gap_ledger,
    parse_exclusion_ledger,
    parse_public_event_projection,
    write_event_detection,
)
from found_money.identity import build_identity_graph

WHEN = datetime(2026, 7, 29, 18, 0, tzinfo=timezone.utc)
CONFIG = EventDetectionConfig(
    stale_deal_days=30,
    proposal_silence_days=14,
    lapse_days=90,
    high_value_minor=10_000,
    reorder_days=60,
    renewal_window_days=30,
)


def _sources() -> dict[str, dict[str, list[dict[str, object]]]]:
    return {
        "stripe": {
            "invoices": [
                {
                    "id": "inv_failed",
                    "customer_id": "cus_failed",
                    "customer_token": "cust_failed",
                    "status": "open",
                    "collection_outcome": "payment_failed",
                    "attempted_at": "2026-05-01T00:00:00Z",
                }
            ],
            "subscriptions": [
                {
                    "id": "sub_expired",
                    "customer_id": "cus_expired",
                    "customer_token": "cust_expired",
                    "status": "trialing",
                    "trial_end": "2026-05-01T00:00:00Z",
                },
                {
                    "id": "sub_no_convert",
                    "customer_id": "cus_no_convert",
                    "customer_token": "cust_no_convert",
                    "status": "canceled",
                    "trial_end": "2026-05-01T00:00:00Z",
                },
                {
                    "id": "sub_renewal",
                    "customer_id": "cus_renewal",
                    "customer_token": "cust_renewal",
                    "status": "active",
                    "renewal_at": "2026-08-01T00:00:00Z",
                },
            ],
            "customers": [
                {
                    "id": "cus_canceled",
                    "customer_token": "cust_canceled",
                    "status": "canceled",
                    "canceled_at": "2026-05-01T00:00:00Z",
                }
            ],
        },
        "hubspot": {
            "deals": [
                {
                    "id": "deal_lost",
                    "customer_token": "cust_lost",
                    "deal_stage": "closed_lost",
                    "closed_at": "2026-05-01T00:00:00Z",
                }
            ],
            "contacts": [
                {
                    "id": "contact_engaged",
                    "customer_token": "cust_engaged",
                    "engaged": True,
                    "engagement_at": "2026-07-01T00:00:00Z",
                }
            ],
        },
        "orders": {
            "orders": [
                {
                    "order_id": "order_lapsed_1",
                    "customer_token": "cust_lapsed",
                    "ordered_at": "2025-12-01T00:00:00Z",
                    "total_minor": 1000,
                    "currency": "usd",
                },
                {
                    "order_id": "order_lapsed_2",
                    "customer_token": "cust_lapsed",
                    "ordered_at": "2026-01-01T00:00:00Z",
                    "total_minor": 1000,
                    "currency": "usd",
                },
                {
                    "order_id": "order_high_1",
                    "customer_token": "cust_high",
                    "ordered_at": "2025-12-01T00:00:00Z",
                    "total_minor": 25000,
                    "currency": "usd",
                },
                {
                    "order_id": "order_high_2",
                    "customer_token": "cust_high",
                    "ordered_at": "2026-01-01T00:00:00Z",
                    "total_minor": 25000,
                    "currency": "usd",
                },
                {
                    "order_id": "order_reorder_1",
                    "customer_token": "cust_reorder",
                    "ordered_at": "2026-01-01T00:00:00Z",
                    "total_minor": 5000,
                    "currency": "usd",
                },
                {
                    "order_id": "order_reorder_2",
                    "customer_token": "cust_reorder",
                    "ordered_at": "2026-04-01T00:00:00Z",
                    "total_minor": 5000,
                    "currency": "usd",
                },
            ]
        },
        "proposals": {
            "proposals": [
                {
                    "proposal_id": "proposal_silent",
                    "customer_token": "cust_proposal",
                    "proposed_at": "2026-06-01T00:00:00Z",
                    "status": "sent",
                    "amount_minor": 7000,
                    "currency": "usd",
                }
            ]
        },
        "appointments": {
            "appointments": [
                {
                    "appointment_id": "appointment_no_show",
                    "customer_token": "cust_no_show",
                    "scheduled_at": "2026-06-01T00:00:00Z",
                    "status": "no_show",
                }
            ]
        },
    }


def _family_for_token(result, family: str, token: str):
    return [
        candidate
        for candidate in result.candidates.candidates
        if candidate.event_family == family and candidate.customer_token == token
    ]


def test_all_twelve_families_emit_versioned_candidates_from_fixture_inputs():
    result = detect_event_families(_sources(), run_id="run_families", built_at=WHEN, config=CONFIG)
    assert {candidate.event_family for candidate in result.candidates.candidates} == set(
        EVENT_FAMILIES
    )
    assert all(candidate.event_at is not None for candidate in result.candidates.candidates)
    assert all(candidate.recency_days is not None for candidate in result.candidates.candidates)
    assert all(candidate.evidence_references for candidate in result.candidates.candidates)
    assert all(candidate.lineage for candidate in result.candidates.candidates)
    assert all(candidate.economic_unit_key for candidate in result.candidates.candidates)


@pytest.mark.parametrize(
    ("family", "token", "source_key", "record"),
    [
        (
            "failed_payment",
            "cust_failed",
            "stripe",
            {
                "id": "inv_failed",
                "customer_id": "cus_failed",
                "customer_token": "cust_failed",
                "status": "open",
                "collection_outcome": "payment_failed",
                "attempted_at": "2026-05-01T00:00:00Z",
            },
        ),
        (
            "expired_trial",
            "cust_expired",
            "stripe",
            {
                "id": "sub_expired",
                "customer_id": "cus_expired",
                "customer_token": "cust_expired",
                "status": "trialing",
                "trial_end": "2026-05-01T00:00:00Z",
            },
        ),
        (
            "trial_no_convert",
            "cust_no_convert",
            "stripe",
            {
                "id": "sub_no_convert",
                "customer_id": "cus_no_convert",
                "customer_token": "cust_no_convert",
                "status": "canceled",
                "trial_end": "2026-05-01T00:00:00Z",
            },
        ),
        (
            "closed_lost_stale_deal",
            "cust_lost",
            "hubspot",
            {
                "id": "deal_lost",
                "customer_token": "cust_lost",
                "deal_stage": "closed_lost",
                "closed_at": "2026-05-01T00:00:00Z",
            },
        ),
        (
            "canceled_customer",
            "cust_canceled",
            "stripe",
            {
                "id": "cus_canceled",
                "customer_token": "cust_canceled",
                "status": "canceled",
                "canceled_at": "2026-05-01T00:00:00Z",
            },
        ),
        (
            "lapsed_repeat_buyer",
            "cust_lapsed",
            "orders",
            {"order_id": "order_lapsed_2", "customer_token": "cust_lapsed"},
        ),
        (
            "silent_proposal",
            "cust_proposal",
            "proposals",
            {"proposal_id": "proposal_silent", "customer_token": "cust_proposal"},
        ),
        (
            "no_show_rebook",
            "cust_no_show",
            "appointments",
            {"appointment_id": "appointment_no_show", "customer_token": "cust_no_show"},
        ),
        (
            "disappeared_high_value_customer",
            "cust_high",
            "orders",
            {"order_id": "order_high_2", "customer_token": "cust_high"},
        ),
        (
            "overdue_reorder",
            "cust_reorder",
            "orders",
            {"order_id": "order_reorder_2", "customer_token": "cust_reorder"},
        ),
        (
            "renewal_upsell",
            "cust_renewal",
            "stripe",
            {"id": "sub_renewal", "customer_token": "cust_renewal"},
        ),
        (
            "engaged_unbooked",
            "cust_engaged",
            "hubspot",
            {"id": "contact_engaged", "customer_token": "cust_engaged"},
        ),
    ],
)
def test_each_family_has_evidence_bound_to_an_immutable_unit(
    family: str, token: str, source_key: str, record: dict[str, object]
):
    result = detect_event_families(
        _sources(), run_id="run_family_case", built_at=WHEN, config=CONFIG
    )
    candidates = _family_for_token(result, family, token)
    assert len(candidates) == 1, (family, source_key, record)
    candidate = candidates[0]
    assert candidate.candidate_key == f"{family}:{candidate.economic_unit_key}"
    assert candidate.qualifying_evidence["event_family"] == family
    assert candidate.evidence_references == sorted(candidate.evidence_references)


@pytest.mark.parametrize(
    ("family", "token", "extra_source", "reason"),
    [
        (
            "failed_payment",
            "cust_failed",
            {
                "invoices": [
                    {
                        "id": "inv_paid_later",
                        "customer_id": "cus_failed",
                        "customer_token": "cust_failed",
                        "status": "paid",
                        "collection_outcome": "payment_succeeded",
                        "paid_at": "2026-06-01T00:00:00Z",
                    }
                ]
            },
            "later_payment",
        ),
        (
            "expired_trial",
            "cust_expired",
            {
                "subscriptions": [
                    {
                        "id": "sub_expired_active",
                        "customer_id": "cus_expired",
                        "customer_token": "cust_expired",
                        "status": "active",
                        "event_at": "2026-06-02T00:00:00Z",
                    }
                ]
            },
            "reactivation",
        ),
        (
            "trial_no_convert",
            "cust_no_convert",
            {
                "subscriptions": [
                    {
                        "id": "sub_no_convert_active",
                        "customer_id": "cus_no_convert",
                        "customer_token": "cust_no_convert",
                        "status": "active",
                        "event_at": "2026-06-02T00:00:00Z",
                    }
                ]
            },
            "reactivation",
        ),
        (
            "closed_lost_stale_deal",
            "cust_lost",
            {
                "deals": [
                    {
                        "id": "deal_active",
                        "customer_token": "cust_lost",
                        "deal_stage": "qualifiedtobuy",
                        "event_at": "2026-06-02T00:00:00Z",
                    }
                ]
            },
            "active_negotiation",
        ),
        (
            "canceled_customer",
            "cust_canceled",
            {
                "subscriptions": [
                    {
                        "id": "sub_reactivated",
                        "customer_id": "cus_canceled",
                        "customer_token": "cust_canceled",
                        "status": "active",
                        "event_at": "2026-06-02T00:00:00Z",
                    }
                ]
            },
            "reactivation",
        ),
        (
            "lapsed_repeat_buyer",
            "cust_lapsed",
            {
                "orders": [
                    {
                        "order_id": "order_lapsed_recent",
                        "customer_token": "cust_lapsed",
                        "ordered_at": "2026-07-01T00:00:00Z",
                        "total_minor": 1000,
                    }
                ]
            },
            "purchase",
        ),
        (
            "silent_proposal",
            "cust_proposal",
            {
                "proposals": [
                    {
                        "proposal_id": "proposal_accepted",
                        "customer_token": "cust_proposal",
                        "proposed_at": "2026-06-01T00:00:00Z",
                        "status": "accepted",
                        "amount_minor": 7000,
                        "currency": "usd",
                    }
                ]
            },
            "purchase",
        ),
        (
            "no_show_rebook",
            "cust_no_show",
            {
                "appointments": [
                    {
                        "appointment_id": "appointment_rebooked",
                        "customer_token": "cust_no_show",
                        "scheduled_at": "2026-06-02T00:00:00Z",
                        "status": "scheduled",
                    }
                ]
            },
            "rebooking",
        ),
        (
            "disappeared_high_value_customer",
            "cust_high",
            {
                "orders": [
                    {
                        "order_id": "order_high_recent",
                        "customer_token": "cust_high",
                        "ordered_at": "2026-07-01T00:00:00Z",
                        "total_minor": 25000,
                    }
                ]
            },
            "purchase",
        ),
        (
            "overdue_reorder",
            "cust_reorder",
            {
                "orders": [
                    {
                        "order_id": "order_reorder_recent",
                        "customer_token": "cust_reorder",
                        "ordered_at": "2026-07-01T00:00:00Z",
                        "total_minor": 5000,
                    }
                ]
            },
            "purchase",
        ),
        (
            "renewal_upsell",
            "cust_renewal",
            {
                "customers": [
                    {
                        "id": "cus_renewal_optout",
                        "customer_token": "cust_renewal",
                        "status": "active",
                        "do_not_contact": True,
                    }
                ]
            },
            "explicit_disqualification",
        ),
        (
            "engaged_unbooked",
            "cust_engaged",
            {
                "appointments": [
                    {
                        "appointment_id": "appointment_engaged",
                        "customer_token": "cust_engaged",
                        "scheduled_at": "2026-07-02T00:00:00Z",
                        "status": "scheduled",
                    }
                ]
            },
            "rebooking",
        ),
    ],
)
def test_each_family_has_a_suppression_case(
    family: str,
    token: str,
    extra_source: dict[str, list[dict[str, object]]],
    reason: str,
):
    sources = _sources()
    source_name = (
        "stripe" if "invoices" in extra_source or "subscriptions" in extra_source else "orders"
    )
    if "deals" in extra_source or "customers" in extra_source:
        source_name = "hubspot" if "deals" in extra_source else "stripe"
    if "proposals" in extra_source:
        source_name = "proposals"
    if "appointments" in extra_source:
        source_name = "appointments"
    for collection, records in extra_source.items():
        sources.setdefault(source_name, {}).setdefault(collection, []).extend(records)
    result = detect_event_families(sources, run_id="run_suppression", built_at=WHEN, config=CONFIG)
    assert _family_for_token(result, family, token) == []
    matching = [
        item
        for item in result.exclusions.exclusions
        if item.event_family == family and item.customer_token == token
    ]
    assert matching, family
    assert matching[0].reason_code == reason
    assert matching[0].priority == SUPPRESSION_PRIORITY[reason]


def test_suppression_priority_is_stable_and_competing_reasons_remain_visible():
    sources = _sources()
    sources["stripe"]["invoices"].extend(
        [
            {
                "id": "inv_paid_priority",
                "customer_id": "cus_failed",
                "customer_token": "cust_failed",
                "status": "paid",
                "collection_outcome": "payment_succeeded",
                "paid_at": "2026-06-01T00:00:00Z",
                "dispute": True,
            }
        ]
    )
    sources["stripe"]["customers"].append(
        {
            "id": "cus_failed_optout",
            "customer_token": "cust_failed",
            "status": "active",
            "do_not_contact": True,
        }
    )
    result = detect_event_families(sources, run_id="run_priority", built_at=WHEN, config=CONFIG)
    exclusion = next(
        item for item in result.exclusions.exclusions if item.event_family == "failed_payment"
    )
    assert exclusion.reason_code == "explicit_disqualification"
    assert exclusion.competing_reason_codes == ["fraud_dispute", "later_payment"]


def test_ambiguous_identity_is_withheld_and_surfaces_as_data_gap():
    nodes = [
        IdentityNodeV1.model_validate(
            {
                "node_id": f"stripe:customer:{value}",
                "source_system": "stripe",
                "object_type": "customer",
                "source_id": value,
                "observed_at": "2026-07-29T18:00:00Z",
                "payload_hash": "a" * 64,
                "email": "household@example.invalid",
            }
        )
        for value in ("cus_a", "cus_b", "cus_c")
    ]
    graph = build_identity_graph(nodes, run_id="run_ambiguous")
    sources = {
        "stripe": {
            "invoices": [
                {
                    "id": "inv_ambiguous",
                    "customer_id": "cus_a",
                    "status": "open",
                    "collection_outcome": "payment_failed",
                    "attempted_at": "2026-05-01T00:00:00Z",
                }
            ]
        }
    }
    result = detect_event_families(sources, graph, run_id="run_ambiguous", built_at=WHEN)
    assert result.candidates.candidates == []
    assert result.exclusions.exclusions == []
    assert [gap.reason_code for gap in result.data_gaps.gaps] == ["ambiguous_identity"]


def test_hubspot_snapshot_association_map_resolves_deal_to_contact_graph_member():
    contact = IdentityNodeV1.model_validate(
        {
            "node_id": "hubspot:contact:contact_assoc",
            "source_system": "hubspot",
            "object_type": "contact",
            "source_id": "contact_assoc",
            "observed_at": "2026-07-29T18:00:00Z",
            "payload_hash": "b" * 64,
            "email": "assoc@example.invalid",
        }
    )
    graph = build_identity_graph([contact], run_id="run_association")
    result = detect_event_families(
        {
            "hubspot": {
                "contacts": [{"id": "contact_assoc"}],
                "deals": [
                    {
                        "id": "deal_assoc",
                        "deal_stage": "closed_lost",
                        "closed_at": "2026-05-01T00:00:00Z",
                    }
                ],
                "associations": {"deal_assoc": ["contact_assoc"]},
            }
        },
        graph,
        run_id="run_association",
        built_at=WHEN,
        config=CONFIG,
    )
    candidate = next(
        item
        for item in result.candidates.candidates
        if item.event_family == "closed_lost_stale_deal"
    )
    assert candidate.customer_token == graph.customers[0].customer_token
    assert candidate.evidence_references == ["hubspot/deals/deal_assoc"]


def test_repeated_inputs_are_byte_stable_and_public_projection_has_no_source_ids(
    tmp_path, monkeypatch
):
    sources = _sources()
    first = detect_event_families(sources, run_id="run_stable", built_at=WHEN, config=CONFIG)
    shuffled = {key: value for key, value in reversed(list(sources.items()))}
    for payload in shuffled.values():
        for records in payload.values():
            if isinstance(records, list):
                records.reverse()
    second = detect_event_families(shuffled, run_id="run_stable", built_at=WHEN, config=CONFIG)
    assert first.candidates.to_canonical_json() == second.candidates.to_canonical_json()
    assert first.exclusions.to_canonical_json() == second.exclusions.to_canonical_json()
    assert (
        first.public_projection.to_canonical_json() == second.public_projection.to_canonical_json()
    )

    def blocked(*_args, **_kwargs):
        raise AssertionError("network called")

    monkeypatch.setattr(socket, "create_connection", blocked)
    output = write_event_detection(tmp_path / "events", first)
    assert (
        parse_exclusion_ledger(output["events/exclusion-ledger.json"].read_bytes())
        == first.exclusions
    )
    assert parse_data_gap_ledger(output["events/data-gaps.json"].read_bytes()) == first.data_gaps
    assert (
        parse_public_event_projection(output["events/public.json"].read_bytes())
        == first.public_projection
    )
    public_text = output["events/public.json"].read_text(encoding="utf-8")
    assert "cust_" not in public_text
    assert "stripe/" not in public_text
    assert not any(path.name.startswith(".found-money-events-") for path in tmp_path.iterdir())


def test_canonical_candidate_and_exclusion_files_round_trip():
    result = detect_event_families(_sources(), run_id="run_roundtrip", built_at=WHEN, config=CONFIG)
    parsed_candidates = RecoveryCandidateSetV1.model_validate(
        json.loads(result.candidates.to_canonical_json())
    )
    parsed_exclusions = ExclusionLedgerV1.model_validate(
        json.loads(result.exclusions.to_canonical_json())
    )
    assert parsed_candidates.to_canonical_json() == result.candidates.to_canonical_json()
    assert parsed_exclusions.to_canonical_json() == result.exclusions.to_canonical_json()
