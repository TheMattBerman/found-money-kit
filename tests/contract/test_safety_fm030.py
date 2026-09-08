"""FM-030 package-wide no-mutation capability and network allowlist proof."""

from __future__ import annotations

import hashlib
import io
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from PIL import Image
from pypdf import PdfWriter

from found_money.connectors.hubspot import (
    HubSpotHttpTransport,
    _TrackingTransport,
    build_crm_snapshot_receipt,
    fetch_crm_snapshot,
    write_crm_snapshot,
)
from found_money.connectors.stripe import (
    StripeConnector,
    build_stripe_snapshot_receipt,
    write_stripe_snapshot,
)
from found_money.contracts.safety import (
    NoMutationAssertionV1,
    ReleaseSafetyEvidencePacketV1,
    SafeRequestAuditLogV1,
)
from found_money.receipts import build_run_manifest, build_source_receipt, write_run_manifest
from found_money.safety import (
    ALLOWLIST_VERSION,
    HUBSPOT_HOST,
    MODEL_HOST,
    NEGATIVE_MATRIX,
    NetworkAllowlistError,
    STRIPE_HOST,
    FakeHubSpotTransport,
    FakeModelTransport,
    FakeStripeTransport,
    SafeRequestAuditor,
    allowlist_hash,
    assert_network_allowed,
    build_no_mutation_assertion,
    build_release_safety_evidence_packet,
    empty_fixture_assertion,
    import_graph_modules,
    prove_writer_rejects_escapes,
    run_negative_matrix,
    scan_cli_help,
    scan_forbidden_imports,
    scan_output_tree,
    scan_package_capabilities,
    scan_pdf_bytes,
    scan_png_bytes,
    scan_writers_use_root_validation,
    write_artifact_set_atomic,
    write_no_mutation_assertion,
    write_release_safety_evidence_packet,
)
from found_money.strategy import (
    FixtureGroundedStrategyProvider,
    build_provider_request,
    run_strategy_boundary,
    write_strategy_audit_receipt,
)
from found_money.map import build_thin_slice_money_map
from found_money.contracts.strategy import StrategyBusinessProfileV1

ROOT = Path(__file__).resolve().parents[2]
WHEN = datetime(2026, 8, 10, 18, 0, tzinfo=timezone.utc)
CHECK_EVIDENCE = {
    "quality": "ruff-format=pass\nruff-check=pass\nmypy=pass\n",
    "unit-and-contract": "pytest=577 passed\n",
    "public-safety": "public-safety=pass\n",
    "render-proof": "render-proof=pass\n",
}


def test_allowlist_covers_hubspot_stripe_and_model_only():
    assert_network_allowed("GET", HUBSPOT_HOST, "/crm/v3/objects/contacts")
    assert_network_allowed("GET", STRIPE_HOST, "/v1/customers")
    assert_network_allowed("POST", MODEL_HOST, "/v1/responses")
    with pytest.raises(NetworkAllowlistError):
        assert_network_allowed("POST", HUBSPOT_HOST, "/crm/v3/objects/contacts")
    with pytest.raises(NetworkAllowlistError):
        assert_network_allowed("GET", MODEL_HOST, "/v1/responses")
    digest = allowlist_hash()
    assert len(digest) == 64
    assert ALLOWLIST_VERSION == "found-money-network-allowlist.v1"


def test_fake_transport_negative_matrix_rejects_all_disallowed_cases():
    outcomes = run_negative_matrix()
    assert len(outcomes) == len(NEGATIVE_MATRIX)
    assert {item["result"] for item in outcomes} == {"rejected"}


def test_approved_reads_record_method_host_path_without_headers_or_payloads():
    auditor = SafeRequestAuditor()
    hubspot = FakeHubSpotTransport(auditor=auditor)
    stripe = FakeStripeTransport(auditor=auditor)
    model = FakeModelTransport(auditor=auditor)
    get_method = "GET"
    post_method = "POST"
    hubspot.request(
        method=get_method,
        path="/crm/v3/objects/contacts",
        query={"limit": "100"},
        headers={"Authorization": "Bearer secret-token"},
    )
    stripe.request(
        method=get_method,
        path="/v1/customers",
        query={"limit": "100"},
        headers={"Authorization": "Bearer rk_test_secret"},
    )
    model.invoke(
        method=post_method,
        url=f"https://{MODEL_HOST}/v1/responses",
    )
    log = auditor.as_log()
    payload = log.to_canonical_json().decode("utf-8")
    assert "secret" not in payload.casefold()
    assert "bearer" not in payload.casefold()
    assert "authorization" not in payload.casefold()
    assert [record.method for record in log.records] == ["GET", "GET", "POST"]
    assert [record.host for record in log.records] == [HUBSPOT_HOST, STRIPE_HOST, MODEL_HOST]
    assertion = build_no_mutation_assertion(auditor, built_at=WHEN)
    assert assertion.no_mutation is True
    assert assertion.allowed_methods_summary == {"GET": 2, "POST": 1}
    assert assertion.scope_counts["model_request"] == 1


def test_hubspot_and_stripe_writes_attach_no_mutation_assertions(tmp_path):
    import importlib.util

    def _load(name: str, relative: str):
        path = ROOT / relative
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    hubspot_tests = _load("hubspot_contract_fm030", "tests/contract/test_hubspot_connector.py")
    stripe_tests = _load("stripe_contract_fm030", "tests/contract/test_stripe_connector.py")

    hubspot_snapshot = fetch_crm_snapshot(hubspot_tests._transport(), token="fixture-token")
    hubspot_receipt = build_crm_snapshot_receipt(hubspot_snapshot, retrieved_at=WHEN)
    write_crm_snapshot(tmp_path, hubspot_snapshot, hubspot_receipt)
    assertion = NoMutationAssertionV1.model_validate_json(
        (tmp_path / "hubspot" / "no-mutation-assertion.json").read_bytes()
    )
    assert assertion.no_mutation is True
    assert assertion.live_status == "fixture-only"
    assert assertion.attached_receipt_hashes["source-receipt"] == hubspot_receipt.content_hash

    stripe_snapshot = StripeConnector(stripe_tests._transport()).fetch_snapshot(
        api_key="sk_fixture_key"
    )
    stripe_receipt = build_stripe_snapshot_receipt(stripe_snapshot, retrieved_at=WHEN)
    write_stripe_snapshot(tmp_path, stripe_snapshot, stripe_receipt)
    stripe_assertion = NoMutationAssertionV1.model_validate_json(
        (tmp_path / "stripe" / "no-mutation-assertion.json").read_bytes()
    )
    assert stripe_assertion.no_mutation is True
    assert stripe_assertion.live_status == "fixture-only"

    with pytest.raises(Exception, match="paths must differ"):
        write_crm_snapshot(
            tmp_path / "collision-hubspot",
            hubspot_snapshot,
            hubspot_receipt,
            assertion_path="hubspot/source-receipt.json",
        )
    assert not (tmp_path / "collision-hubspot").exists()
    with pytest.raises(Exception, match="paths must differ"):
        write_stripe_snapshot(
            tmp_path / "collision-stripe",
            stripe_snapshot,
            stripe_receipt,
            assertion_path="stripe/source-receipt.json",
        )
    assert not (tmp_path / "collision-stripe").exists()


def test_live_label_is_derived_from_native_transport_provenance(tmp_path):
    import importlib.util

    path = ROOT / "tests/contract/test_hubspot_connector.py"
    spec = importlib.util.spec_from_file_location("hubspot_live_fm030", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fetch_crm_snapshot(
        module._transport(),
        token="fixture-token",
        retrieved_at=WHEN,
        output_root=tmp_path,
    )
    assertion = NoMutationAssertionV1.model_validate_json(
        (tmp_path / "hubspot/no-mutation-assertion.json").read_bytes()
    )
    assert assertion.live_status == "fixture-only"

    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"results": []}, request=request)
        )
    )
    native = HubSpotHttpTransport(client=client, token="fixture-token")
    tracked = _TrackingTransport(native)
    tracked.request(
        method="GET",
        path="/crm/v3/objects/contacts",
        query={"limit": "100"},
        headers={"Authorization": "Bearer fixture-token"},
    )
    assertion = build_no_mutation_assertion(tracked.auditor, built_at=WHEN)
    assert assertion.live_status == "fixture-only"
    assert assertion.request_count == 1
    assert assertion.allowed_methods_summary == {"GET": 1}
    assert assertion.scope_counts["hubspot_read"] == 1
    native.close()

    native_live = HubSpotHttpTransport(token="fixture-token")
    tracked_live = _TrackingTransport(native_live)
    tracked_live.auditor.record(
        method="GET",
        host=HUBSPOT_HOST,
        path="/crm/v3/objects/contacts",
        provenance=native_live,
    )
    live_assertion = build_no_mutation_assertion(tracked_live.auditor, built_at=WHEN)
    assert live_assertion.live_status == "live-unverified"
    native_live.close()


def test_run_and_model_receipts_get_compatible_no_mutation_assertions(tmp_path):
    receipt = build_source_receipt(
        source_type="orders",
        connector_schema_version="orders.v1",
        retrieved_at=WHEN,
        locator_kind="input_path",
        locator="orders.json",
        content=b'{"orders":[]}\n',
        page_or_row_count=0,
        record_count=0,
    )
    manifest = build_run_manifest(
        run_id="run_fm030",
        referenced_schema_versions={"source-receipt": "source-receipt.v1"},
        started_at=WHEN,
        completed_at=WHEN,
        mode="public",
        source_receipts={"receipts/orders.json": receipt},
        stages={"sources": "completed"},
    )
    write_run_manifest(tmp_path, "run.json", manifest)
    assertion = empty_fixture_assertion(
        attached_receipt_hashes={
            "run-manifest": hashlib.sha256(manifest.to_canonical_json()).hexdigest(),
            "source-receipt": receipt.content_hash,
        },
        built_at=WHEN,
    )
    write_no_mutation_assertion(tmp_path, "safety/no-mutation-assertion.json", assertion)
    stored = NoMutationAssertionV1.model_validate_json(
        (tmp_path / "safety" / "no-mutation-assertion.json").read_bytes()
    )
    assert stored.no_mutation is True
    assert stored.live_status == "fixture-only"

    provider = FixtureGroundedStrategyProvider()
    execution = run_strategy_boundary(
        build_thin_slice_money_map(run_id="run_fm030_model"),
        StrategyBusinessProfileV1(
            product="Annual subscription",
            proof="Three approved case studies",
            margin="40 percent contribution margin",
            channel="email",
            capacity="100 recovery reviews per week",
            destination="billing portal",
        ),
        provider,
        configured_model_id=provider.configured_model_id,
    )
    model_auditor = SafeRequestAuditor()
    FakeModelTransport(auditor=model_auditor).invoke(
        method="POST", url=f"https://{MODEL_HOST}/v1/responses"
    )
    write_strategy_audit_receipt(
        str(tmp_path),
        "strategy/receipt.json",
        execution.boundary.receipt,
        auditor=model_auditor,
    )
    model_assertion = NoMutationAssertionV1.model_validate_json(
        (tmp_path / "strategy" / "no-mutation-assertion.json").read_bytes()
    )
    assert model_assertion.no_mutation is True
    assert model_assertion.allowed_methods_summary == {"POST": 1}
    assert model_assertion.scope_counts == {"model_request": 1}
    assert model_assertion.attached_receipt_hashes["evidence-packet"] == (
        execution.boundary.receipt.evidence_packet_hash
    )


def test_output_tree_scan_covers_json_html_logs_pdf_png_and_handoffs(tmp_path):
    tree = tmp_path / "run"
    (tree / "launch-pack").mkdir(parents=True)
    (tree / "handoff").mkdir(parents=True)
    (tree / "public.json").write_text(
        '{"schema_version":"public.v1","count":1}\n', encoding="utf-8"
    )
    (tree / "notes.txt").write_text("aggregate only\n", encoding="utf-8")
    (tree / "build.log").write_text("completed fixture-only\n", encoding="utf-8")
    (tree / "index.html").write_text("<html><body>Recovery Room</body></html>\n", encoding="utf-8")
    (tree / "launch-pack" / "manifest.json").write_text(
        '{"schema_version":"launch-pack-manifest.v1","files":[]}\n', encoding="utf-8"
    )
    (tree / "handoff" / "creative-handoff.json").write_text(
        '{"schema_version":"creative-handoff.v1","status":"fixture-only"}\n',
        encoding="utf-8",
    )

    pdf = PdfWriter()
    pdf.add_blank_page(width=72, height=72)
    pdf_buf = io.BytesIO()
    pdf.write(pdf_buf)
    (tree / "print-report.pdf").write_bytes(pdf_buf.getvalue())

    image = Image.new("RGB", (8, 8), color=(20, 40, 60))
    png_buf = io.BytesIO()
    image.save(png_buf, format="PNG")
    (tree / "money-map.png").write_bytes(png_buf.getvalue())

    assert scan_output_tree(tree) == []

    dirty = tmp_path / "dirty"
    dirty.mkdir()
    (dirty / "leak.json").write_text(
        '{"email":"person@example.com","token":"secret"}\n', encoding="utf-8"
    )
    violations = scan_output_tree(dirty)
    assert any("email" in item or "credential" in item for item in violations)

    dirty_pdf = PdfWriter()
    dirty_pdf.add_blank_page(width=72, height=72)
    dirty_pdf.add_metadata({"/Author": "person@example.com"})
    buf = io.BytesIO()
    dirty_pdf.write(buf)
    assert scan_pdf_bytes("dirty.pdf", buf.getvalue())

    dirty_png = Image.new("RGB", (4, 4), color=(1, 2, 3))
    png = io.BytesIO()
    from PIL.PngImagePlugin import PngInfo

    meta = PngInfo()
    meta.add_text("Comment", "secret-token-value")
    dirty_png.save(png, format="PNG", pnginfo=meta)
    # Credential regex looks for JSON keys or provider prefixes; comment alone may not trip.
    # Plant an email in metadata to ensure PNG metadata scanning works.
    png2 = io.BytesIO()
    meta2 = PngInfo()
    meta2.add_text("Author", "person@example.com")
    dirty_png.save(png2, format="PNG", pnginfo=meta2)
    assert scan_png_bytes("dirty.png", png2.getvalue())


def test_cli_import_graph_and_writer_proofs():
    modules = import_graph_modules()
    assert "found_money.safety" in modules
    assert "found_money.connectors.hubspot" in modules
    assert scan_cli_help() == []
    prove_writer_rejects_escapes(Path("/tmp/fm030-writer-root"))
    assert scan_writers_use_root_validation() == []


def test_capability_scan_rejects_dynamic_imports_and_unvalidated_writers(tmp_path):
    dynamic = tmp_path / "dynamic.py"
    dynamic.write_text(
        "import importlib\n"
        'name = "smtplib"\n'
        "importlib.import_module(name)\n"
        '__import__("socket")\n'
        'getattr(importlib, "import_module")("requests")\n'
        'from importlib import import_module\nimport_module("smtplib")\n',
        encoding="utf-8",
    )
    violations = scan_forbidden_imports([dynamic])
    assert any("non-literal" in item for item in violations)
    assert any("socket" in item for item in violations)
    assert any("getattr" in item for item in violations)

    writer = tmp_path / "writer.py"
    writer.write_text(
        'from pathlib import Path\ndef write_bad(path):\n    Path(path).write_text("bad")\n',
        encoding="utf-8",
    )
    assert any("write_bad" in item for item in scan_writers_use_root_validation([writer]))


def test_artifact_set_writer_rolls_back_on_mid_set_failure(tmp_path, monkeypatch):
    import found_money.safety.writers as writers

    original = writers._atomic_write_bytes
    calls = 0

    def fail_second(path, payload):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected write failure")
        original(path, payload)

    monkeypatch.setattr(writers, "_atomic_write_bytes", fail_second)
    with pytest.raises(OSError, match="injected"):
        write_artifact_set_atomic(tmp_path, {"a.json": b"{}\n", "b.json": b"{}\n"})
    assert not (tmp_path / "a.json").exists()
    assert not (tmp_path / "b.json").exists()


def test_native_auditor_cannot_be_injected_into_fixture_transport():
    native = HubSpotHttpTransport(token="fixture-token")
    auditor = SafeRequestAuditor._for_native_transport(native)
    with pytest.raises(ValueError, match="bound transport provenance"):
        FakeHubSpotTransport(auditor=auditor).request(method="GET", path="/crm/v3/objects/contacts")
    native.close()


def test_native_provenance_rejects_forged_type_and_replaced_client():
    class HubSpotHttpTransportLookalike:
        uses_live_network = True

    HubSpotHttpTransportLookalike.__name__ = "HubSpotHttpTransport"
    HubSpotHttpTransportLookalike.__module__ = "found_money.connectors.hubspot"
    with pytest.raises(ValueError, match="live connector transport"):
        SafeRequestAuditor._for_native_transport(HubSpotHttpTransportLookalike())

    native = HubSpotHttpTransport(token="fixture-token")
    replacement = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"results": []}, request=request)
        )
    )
    original = native._client
    native._client = replacement
    assert native.uses_live_network is False
    with pytest.raises(ValueError, match="live connector transport"):
        SafeRequestAuditor._for_native_transport(native)
    replacement.close()
    original.close()


def test_native_auditor_requires_exact_inner_provenance_for_each_bound_transport(monkeypatch):
    from found_money.connectors.stripe import StripeHttpTransport

    monkeypatch.setenv("STRIPE_API_KEY", "rk_test_fixture_not_used")
    hubspot = HubSpotHttpTransport(token="fixture-token")
    stripe = StripeHttpTransport(mode="test")
    auditor = SafeRequestAuditor._for_native_transports(hubspot, stripe)
    auditor.record(
        method="GET",
        host=HUBSPOT_HOST,
        path="/crm/v3/objects/contacts",
        provenance=hubspot,
    )
    auditor.record(
        method="GET",
        host=STRIPE_HOST,
        path="/v1/customers",
        provenance=stripe,
    )
    assertion = build_no_mutation_assertion(auditor, built_at=WHEN)
    assert assertion.live_status == "live-unverified"
    with pytest.raises(ValueError, match="bound transport provenance"):
        auditor.record(
            method="GET",
            host=HUBSPOT_HOST,
            path="/crm/v3/objects/deals",
            provenance=stripe,
        )
    hubspot.close()
    stripe.close()


def test_release_safety_evidence_packet_is_honest_and_deterministic(tmp_path):
    first = build_release_safety_evidence_packet(
        root=ROOT,
        live_status="fixture-only",
        built_at=WHEN,
        commit_hash="a" * 40,
        no_mutation_assertion=empty_fixture_assertion(built_at=WHEN),
        check_evidence=CHECK_EVIDENCE,
    )
    second = build_release_safety_evidence_packet(
        root=ROOT,
        live_status="fixture-only",
        built_at=WHEN,
        commit_hash="a" * 40,
        no_mutation_assertion=empty_fixture_assertion(built_at=WHEN),
        check_evidence=CHECK_EVIDENCE,
    )
    assert first.to_canonical_json() == second.to_canonical_json()
    assert first.claims_real_business_run is False
    assert first.claims_publication is False
    assert first.live_status == "fixture-only"
    assert first.allowlist_hash == allowlist_hash()
    assert set(first.check_hashes) >= {
        "quality",
        "unit-and-contract",
        "public-safety",
        "render-proof",
    }
    with pytest.raises(ValueError, match="live-verified"):
        build_release_safety_evidence_packet(
            root=ROOT,
            live_status="live-verified",
            built_at=WHEN,
            check_evidence=CHECK_EVIDENCE,
        )
    with pytest.raises(ValueError, match="live-verified"):
        ReleaseSafetyEvidencePacketV1(
            built_at=WHEN,
            commit_hash="b" * 40,
            allowlist_version=ALLOWLIST_VERSION,
            allowlist_hash=allowlist_hash(),
            implementation_hash="c" * 64,
            source_tree_hash="d" * 64,
            check_evidence={"quality": "pass\n"},
            check_hashes={"quality": hashlib.sha256(b"pass\n").hexdigest()},
            scan_hashes={"capability_scan": "f" * 64},
            live_status="live-verified",
        )
    write_release_safety_evidence_packet(tmp_path, "safety/release-safety-evidence.json", first)
    stored = ReleaseSafetyEvidencePacketV1.model_validate_json(
        (tmp_path / "safety" / "release-safety-evidence.json").read_bytes()
    )
    assert stored.to_canonical_json() == first.to_canonical_json()


def test_package_capability_scan_is_clean():
    # Full package scan including CLI help; keep this as the AC-1/AC-5 oracle.
    assert scan_package_capabilities(ROOT) == []


def test_safe_request_audit_rejects_credential_shaped_fields():
    with pytest.raises(ValueError):
        SafeRequestAuditLogV1(
            allowlist_version=ALLOWLIST_VERSION,
            allowlist_hash=allowlist_hash(),
            evidence_mode="native",  # type: ignore[call-arg]
            records=[],
        )
    with pytest.raises(ValueError):
        SafeRequestAuditLogV1(
            allowlist_version=ALLOWLIST_VERSION,
            allowlist_hash=allowlist_hash(),
            records=[
                {
                    "method": "GET",
                    "host": HUBSPOT_HOST,
                    "path": "/crm/v3/objects/contacts/Bearer secret",
                    "family": "hubspot_read",
                }
            ],
        )
    with pytest.raises(ValueError, match="outside the explicit allowlist"):
        SafeRequestAuditLogV1(
            allowlist_version=ALLOWLIST_VERSION,
            allowlist_hash=allowlist_hash(),
            records=[
                {
                    "method": "GET",
                    "host": "evil.example",
                    "path": "/crm/v3/objects/contacts",
                    "family": "hubspot_read",
                }
            ],
        )
    with pytest.raises(ValueError, match="live assertions require"):
        NoMutationAssertionV1.model_validate(
            empty_fixture_assertion(built_at=WHEN).model_dump(mode="python")
            | {"live_status": "live-unverified"}
        )


def test_provider_request_path_stays_fixture_only_without_network(monkeypatch):
    import socket

    def fail(*_args, **_kwargs):
        raise AssertionError("network opened")

    monkeypatch.setattr(socket, "socket", fail)
    provider = FixtureGroundedStrategyProvider()
    request = build_provider_request(
        run_strategy_boundary(
            build_thin_slice_money_map(run_id="run_fm030_net"),
            StrategyBusinessProfileV1(product="Annual subscription"),
            provider,
            configured_model_id=provider.configured_model_id,
        ).boundary.packet,
        configured_model_id=provider.configured_model_id,
    )
    assert request.configured_model_id == provider.configured_model_id
