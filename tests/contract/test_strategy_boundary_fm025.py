from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from found_money.contracts.strategy import (
    StrategyAssetV1,
    StrategyBusinessProfileV1,
    StrategyClaimV1,
    StrategyDraftV1,
    StrategyProviderResponseV1,
    StrategyTokenUsageV1,
)
from found_money.map import build_thin_slice_money_map
from found_money.redaction import assert_public_safe
from found_money.strategy import (
    ConfiguredStrategyProvider,
    FixtureGroundedStrategyProvider,
    GroundingError,
    ProviderRefusalError,
    ProviderTimeoutError,
    build_grounded_strategy_packet,
    build_provider_request,
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


def _profile(**changes: str | None) -> StrategyBusinessProfileV1:
    values: dict[str, str | None] = {
        "product": "Annual subscription",
        "proof": "Three approved case studies",
        "margin": "40 percent contribution margin",
        "channel": "email",
        "capacity": "100 recovery reviews per week",
        "destination": "billing portal",
    }
    values.update(changes)
    return StrategyBusinessProfileV1(**values)


def _response(request, *, claim: StrategyClaimV1 | None = None):
    evidence = request.packet.evidence[0]
    return StrategyProviderResponseV1(
        returned_model_id=request.configured_model_id,
        response_id="response-001",
        token_usage=StrategyTokenUsageV1(input_tokens=12, output_tokens=8),
        draft=StrategyDraftV1(
            summary="Grounded recovery review",
            claims=[
                claim
                or StrategyClaimV1(
                    path="$.summary",
                    kind="general",
                    value=str(evidence.value),
                    evidence_ids=[evidence.evidence_id],
                )
            ],
            assets=[
                StrategyAssetV1(
                    asset_id="diagnosis",
                    status="renderable",
                    content={"body": "Grounded recovery review"},
                )
            ],
        ),
    )


def test_packet_freezes_completed_map_exclusions_gaps_and_exact_allowed_ids():
    money_map = build_thin_slice_money_map(run_id="run_fm025")
    packet = build_grounded_strategy_packet(money_map, _profile())

    assert packet.map_hash
    assert packet.generated_after == "deterministic_map_and_exclusions"
    assert packet.allowed_evidence_ids == [item.evidence_id for item in packet.evidence]
    assert {item.evidence_id for item in packet.evidence} >= {
        "ev_pile_1_value",
        "ev_pile_1_basis",
        "ev_overlap_exclusion_count",
        "ev_data_gap_count",
        "ev_business_product",
    }
    assert "customer_token" not in packet.to_canonical_json().decode()


@pytest.mark.parametrize(
    "field,bad_value",
    [
        ("product", "person@example.com"),
        ("destination", "https://example.com/pay"),
        ("proof", "+1 (312) 555-0199"),
        ("product", "cus_customer_123"),
    ],
)
def test_packet_rejects_raw_identity_and_urls(field: str, bad_value: str):
    with pytest.raises(ValueError, match="raw identity or URL"):
        build_grounded_strategy_packet(build_thin_slice_money_map(), _profile(**{field: bad_value}))


def test_packet_requires_completed_deterministic_ranking_before_provider():
    money_map = build_thin_slice_money_map()
    money_map = money_map.model_copy(
        update={"piles": [money_map.piles[0].model_copy(update={"rank_explanation": None})]}
    )
    with pytest.raises(ValueError, match="completed deterministic ranking"):
        build_grounded_strategy_packet(money_map, _profile())


@pytest.mark.parametrize(
    "kind,value,evidence_ids,expected_path",
    [
        ("number", "9999 usd", ["ev_pile_1_value"], "$.draft.claims[0].value"),
        ("price", "$49", ["missing"], "$.draft.claims[0]"),
        ("proof", "Ten case studies", [], "$.draft.claims[0]"),
        ("objection", "No implementation work", ["ev_pile_1_basis"], "evidence_ids"),
        ("urgency", "Only today", ["ev_data_gap_count"], "evidence_ids"),
        ("url", "https://example.com", ["ev_pile_1_value"], "value"),
        ("destination", "checkout", ["ev_pile_1_basis"], "evidence_ids"),
        ("capacity", "500 per day", ["ev_data_gap_count"], "evidence_ids"),
        ("product", "Enterprise plan", ["ev_pile_1_basis"], "evidence_ids"),
        ("identity", "Customer A", ["ev_pile_1_basis"], "$.draft.claims[0]"),
    ],
)
def test_grounding_rejects_unsupported_claim_classes_by_json_path(
    kind: str, value: str, evidence_ids: list[str], expected_path: str
):
    packet = build_grounded_strategy_packet(build_thin_slice_money_map(), _profile())
    claim = StrategyClaimV1(path="$.asset.body", kind=kind, value=value, evidence_ids=evidence_ids)
    with pytest.raises(GroundingError) as caught:
        validate_grounded_draft(
            _response(build_provider_request(packet, configured_model_id="m"), claim=claim).draft,
            packet,
        )
    assert expected_path in str(caught.value)


def test_grounding_accepts_evidence_backed_claim_and_shared_adapter_contract():
    packet = build_grounded_strategy_packet(build_thin_slice_money_map(), _profile())
    request = build_provider_request(packet, configured_model_id="configured-model")
    provider = ConfiguredStrategyProvider("configured-model", _response)
    response = provider.invoke(request)

    validate_grounded_draft(response.draft, packet)
    assert response.returned_model_id == "configured-model"
    assert request.schema_version == "strategy-provider-request.v1"
    assert response.schema_version == "strategy-provider-response.v1"

    product = next(item for item in packet.evidence if item.evidence_id == "ev_business_product")
    backed_product_claim = StrategyClaimV1(
        path="$.offer.product",
        kind="product",
        value=f"Offer the {product.value}",
        evidence_ids=[product.evidence_id],
    )
    validate_grounded_draft(_response(request, claim=backed_product_claim).draft, packet)


def test_missing_business_inputs_withhold_only_dependent_assets():
    result = run_strategy_boundary(
        build_thin_slice_money_map(),
        _profile(product=None, destination=None),
        FixtureGroundedStrategyProvider(),
        configured_model_id="fixture-strategy-v1",
    ).boundary

    assert [decision.flag for decision in result.decisions] == [
        "confirm_product",
        "confirm_destination",
    ]
    assets = {asset.asset_id: asset for asset in result.draft.assets}  # type: ignore[union-attr]
    assert assets["diagnosis"].status == "renderable"
    assert assets["offer"].status == "withheld"
    assert assets["copy"].status == "withheld"
    assert assets["cta"].status == "withheld"
    assert "proof_asset" not in assets
    rendered = render_strategy_boundary_mechanical_html(result)
    assert "Observed opportunity: 4900 usd." in rendered
    assert 'data-asset="cta" data-status="withheld"' in rendered
    assert "proof_asset" not in rendered


class _AlwaysFails:
    def __init__(self, error: Exception):
        self.error = error
        self.calls = 0

    def invoke(self, request):
        self.calls += 1
        raise self.error


class _Ungrounded:
    def __init__(self):
        self.calls = 0

    def invoke(self, request):
        self.calls += 1
        return _response(
            request,
            claim=StrategyClaimV1(
                path="$.summary",
                kind="price",
                value="$999",
                evidence_ids=["ev_pile_1_value"],
            ),
        )


@pytest.mark.parametrize(
    "error,code",
    [
        (ProviderRefusalError("no"), "provider_refusal"),
        (ProviderTimeoutError("late"), "provider_timeout"),
        (ValueError("bad payload"), "malformed_or_ungrounded_output"),
    ],
)
def test_provider_failure_stops_after_two_attempts_preserves_map_and_has_no_fallback(
    error: Exception, code: str
):
    money_map = build_thin_slice_money_map()
    before = money_map.to_canonical_json()
    provider = _AlwaysFails(error)

    execution = run_strategy_boundary(
        money_map, _profile(), provider, configured_model_id="configured-model"
    )

    assert provider.calls == 2
    assert execution.money_map.to_canonical_json() == before
    assert execution.boundary.status == "needs_strategy_review"
    assert execution.boundary.draft is None
    assert execution.boundary.receipt.failure_code == code
    assert execution.boundary.receipt.output_hash is None


def test_grounding_failure_is_terminal_after_second_attempt_without_fallback():
    provider = _Ungrounded()
    execution = run_strategy_boundary(
        build_thin_slice_money_map(),
        _profile(),
        provider,
        configured_model_id="configured-model",
    )
    assert provider.calls == 2
    assert execution.boundary.status == "needs_strategy_review"
    assert execution.boundary.draft is None
    assert execution.boundary.receipt.failure_code == "malformed_or_ungrounded_output"


def test_fixture_receipt_is_byte_stable_and_records_full_audit_envelope(tmp_path):
    money_map = build_thin_slice_money_map(run_id="run_replay")
    provider = FixtureGroundedStrategyProvider()
    first = run_strategy_boundary(
        money_map, _profile(), provider, configured_model_id=provider.configured_model_id
    ).boundary
    second = run_strategy_boundary(
        deepcopy(money_map), _profile(), provider, configured_model_id=provider.configured_model_id
    ).boundary

    assert first.to_canonical_json() == second.to_canonical_json()
    receipt = first.receipt
    assert receipt.prompt_version and len(receipt.prompt_hash) == 64
    assert receipt.output_schema_version == "strategy-draft.v1"
    assert len(receipt.output_schema_hash) == 64
    assert receipt.configured_model_id == receipt.returned_model_id
    assert receipt.response_id == "fixture-response-v1"
    assert receipt.attempt_count == 1
    assert receipt.output_hash and len(receipt.output_hash) == 64

    destination = write_strategy_boundary_artifact(str(tmp_path), "strategy/boundary.json", first)
    payload = (tmp_path / "strategy/boundary.json").read_bytes()
    assert destination.endswith("strategy/boundary.json")
    assert parse_strategy_boundary_json(payload).to_canonical_json() == payload
    assert_public_safe(public_strategy_boundary_projection(first))

    request = build_provider_request(first.packet, configured_model_id="fixture-strategy-v1")
    response = provider.invoke(request)
    write_strategy_provider_request(str(tmp_path), "strategy/request.json", request)
    write_strategy_provider_response(str(tmp_path), "strategy/response.json", response)
    from found_money.safety import SafeRequestAuditor

    write_strategy_audit_receipt(
        str(tmp_path), "strategy/receipt.json", receipt, auditor=SafeRequestAuditor()
    )
    assert (tmp_path / "strategy/request.json").read_bytes() == request.to_canonical_json()
    assert (tmp_path / "strategy/response.json").read_bytes() == response.to_canonical_json()
    assert (tmp_path / "strategy/receipt.json").read_bytes() == receipt.to_canonical_json()


def test_committed_fixture_receipt_replays_byte_for_byte():
    provider = FixtureGroundedStrategyProvider()
    result = run_strategy_boundary(
        build_thin_slice_money_map(run_id="run_fm025_fixture"),
        _profile(),
        provider,
        configured_model_id=provider.configured_model_id,
    ).boundary
    stored = Path("tests/fixtures/saas/strategy/grounded_receipt.json").read_bytes()
    assert result.receipt.to_canonical_json() == stored


def test_fixture_provider_never_opens_network_or_reads_credentials(monkeypatch):
    import socket

    def blocked(*args, **kwargs):
        raise AssertionError("network access is forbidden")

    monkeypatch.setattr(socket, "socket", blocked)
    result = run_strategy_boundary(
        build_thin_slice_money_map(),
        _profile(),
        FixtureGroundedStrategyProvider(),
        configured_model_id="fixture-strategy-v1",
    ).boundary
    text = result.to_canonical_json().decode().lower()
    assert result.status == "completed"
    assert "api_key" not in text
    assert "password" not in text
    assert "secret" not in text


def test_strategy_writer_rejects_path_escape(tmp_path):
    result = run_strategy_boundary(
        build_thin_slice_money_map(),
        _profile(),
        FixtureGroundedStrategyProvider(),
        configured_model_id="fixture-strategy-v1",
    ).boundary
    with pytest.raises(ValueError, match="traversal"):
        write_strategy_boundary_artifact(str(tmp_path), "../boundary.json", result)


@pytest.mark.parametrize(
    "summary,assets",
    [
        (
            "TBD",
            [StrategyAssetV1(asset_id="diagnosis", status="renderable", content={"body": "ok"})],
        ),
        ("Valid", []),
        (
            "Valid",
            [StrategyAssetV1(asset_id="diagnosis", status="renderable", content={})],
        ),
        (
            "Valid",
            [
                StrategyAssetV1(
                    asset_id="diagnosis",
                    status="renderable",
                    content={"body": "Recovery plan:"},
                )
            ],
        ),
        (
            "Valid",
            [
                StrategyAssetV1(
                    asset_id="diagnosis",
                    status="renderable",
                    content={"body": "{{ placeholder }}"},
                )
            ],
        ),
    ],
)
def test_finished_play_structure_rejects_placeholders_and_missing_content(summary, assets):
    packet = build_grounded_strategy_packet(build_thin_slice_money_map(), _profile())
    evidence = packet.evidence[0]
    draft = StrategyDraftV1(
        summary=summary,
        claims=[
            StrategyClaimV1(
                path="$.summary",
                kind="general",
                value=str(evidence.value),
                evidence_ids=[evidence.evidence_id],
            )
        ],
        assets=assets,
    )
    with pytest.raises(GroundingError):
        validate_grounded_draft(draft, packet)
