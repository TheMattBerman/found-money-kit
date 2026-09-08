"""FM-032 honest StealAds / Matt-Emerald handoff contracts and intake boundary."""

from __future__ import annotations

import ast
import hashlib
import http.server
import json
import socket
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import sync_playwright
from pydantic import ValidationError

import found_money.build as build_module
import found_money.rendering as rendering_module
from found_money.activation import (
    IntakeConfigError,
    LaunchPackInputs,
    LaunchPackValidationError,
    build_handoff_actions,
    build_launch_pack,
    html_for_public_scan,
    local_intake_fixture_html,
    parse_handoff_intake_config,
    validate_intake_url,
    validate_launch_pack_payloads,
    write_launch_pack,
)
from found_money.build import BuildConfigError, build, load_source_config
from found_money.contracts.activation import (
    CreativeHandoffCardV1,
    CreativeHandoffV1,
    HandoffIntakeConfigV1,
    LaunchPackManifestV1,
    ProductionBriefCardV1,
    ProductionBriefV1,
)
from found_money.contracts.campaign import CompleteRecoveryPlaySetV1
from found_money.rendering.proof import (
    FOUR_ROOM_PNGS,
    PDF_SIGNATURE,
    PNG_SIGNATURE,
    PRINT_REPORT_CONTACT_SHEET,
    REQUIRED_ARTIFACTS,
    resolve_render_baselines_dir,
)
from found_money.safety.output_scan import scan_text_artifact
from found_money.contracts.events import ExclusionLedgerV1
from found_money.contracts.strategy import RecoveryPlaySetV1, RecoveryPlayV1
from found_money.events import detect_failed_payments
from found_money.identity import (
    build_identity_graph,
    load_thin_slice_snapshots,
    normalize_source_records,
)
from found_money.map import build_thin_slice_money_map
from found_money.redaction import assert_public_safe
from found_money.rendering import recovery_room_static_assets, render_recovery_room_html
from found_money.safety import (
    prove_writer_rejects_escapes,
    scan_output_tree,
    scan_package_capabilities,
)
from found_money.strategy import (
    apply_recovery_plays_to_money_map,
    build_canonical_saas_recovery_strategy,
    canonical_saas_business_profile,
)
from found_money.value import build_contribution_ledger

ROOT = Path(__file__).resolve().parents[2]
WHEN = datetime(2026, 7, 29, 18, 0, tzinfo=timezone.utc)
HASH = "a" * 64


def _enriched_inputs(*, mode: str = "public"):
    money_map = build_thin_slice_money_map(run_id="run_fm032_handoff")
    strategy = build_canonical_saas_recovery_strategy(money_map)
    primary = sorted(strategy.recovery_plays.plays, key=lambda item: item.rank)[0]
    render_play_set = RecoveryPlaySetV1(
        run_id=money_map.run_id,
        built_at=strategy.recovery_plays.built_at,
        provider="fixture",
        plays=[
            RecoveryPlayV1(
                play_id=primary.play_id,
                pile_id=primary.pile_id,
                rank=1,
                title=primary.title,
                rationale=primary.rationale,
                recommended_actions=primary.recommended_actions,
            )
        ],
    )
    enriched = apply_recovery_plays_to_money_map(money_map, render_play_set)
    snaps = load_thin_slice_snapshots()
    identity = build_identity_graph(normalize_source_records(snaps), run_id=money_map.run_id)
    candidates = detect_failed_payments(snaps["stripe"], identity, run_id=money_map.run_id)
    ledger = build_contribution_ledger(candidates, snaps["stripe"])
    return (
        LaunchPackInputs(
            money_map=enriched,
            recovery_plays=strategy.recovery_plays,
            evidence_packet=strategy.packet,
            contribution_ledger=ledger,
            identity_graph=identity,
            candidates=candidates,
            exclusions=ExclusionLedgerV1(run_id=money_map.run_id, built_at=WHEN, exclusions=[]),
            business_profile=canonical_saas_business_profile(),
            source_snapshots=snaps,
            mode=mode,
        ),
        strategy.recovery_plays,
        primary,
    )


def _sample_handoff(play_id: str = "play_payment_rescue_a") -> CreativeHandoffV1:
    return CreativeHandoffV1(
        play_id=play_id,
        segment_id="seg_payment_rescue",
        campaign_name="Payment Rescue Campaign",
        money_map_sha256=HASH,
        recovery_plays_sha256=HASH,
        evidence_packet_sha256=HASH,
        value_ledger_sha256=HASH,
        concept_cards=[
            CreativeHandoffCardV1(
                card_id="card_payment_rescue_a1",
                card_name="Failed payment tension",
                format_style="Static social proof card",
            )
        ],
        import_instructions=(
            "Manually import this approval-only creative handoff into StealAds. "
            "No automatic integration or audience creation is performed."
        ),
    )


def test_ac1_creative_handoff_v1_validates_required_fields_and_manual_import():
    handoff = _sample_handoff()
    assert handoff.schema_version == "creative-handoff.v1"
    assert handoff.live_integration is False
    assert "manually import" in handoff.import_instructions.casefold()
    assert_public_safe(
        {
            "schema_version": handoff.schema_version,
            "status": handoff.status,
            "live_integration": handoff.live_integration,
            "import_instructions": handoff.import_instructions,
        }
    )
    with pytest.raises(ValidationError, match="manual import"):
        CreativeHandoffV1(
            play_id="play_payment_rescue_a",
            segment_id="seg_payment_rescue",
            campaign_name="Payment Rescue Campaign",
            money_map_sha256=HASH,
            recovery_plays_sha256=HASH,
            evidence_packet_sha256=HASH,
            value_ledger_sha256=HASH,
            concept_cards=handoff.concept_cards,
            import_instructions="Drop this file somewhere",
        )
    with pytest.raises(ValidationError, match="automatic integration"):
        CreativeHandoffV1(
            play_id="play_payment_rescue_a",
            segment_id="seg_payment_rescue",
            campaign_name="Payment Rescue Campaign",
            money_map_sha256=HASH,
            recovery_plays_sha256=HASH,
            evidence_packet_sha256=HASH,
            value_ledger_sha256=HASH,
            concept_cards=handoff.concept_cards,
            import_instructions="Manually import then enjoy live integration with StealAds",
        )


def test_ac1_production_brief_and_stable_concept_card_refs():
    inputs, plays, primary = _enriched_inputs()
    pack = build_launch_pack(inputs)
    validate_launch_pack_payloads(pack.payloads)
    handoff_path = f"launch-pack/creative-handoff/{primary.play_id}-creative-handoff.json"
    brief_path = f"launch-pack/production-brief/{primary.play_id}-production-brief.json"
    handoff = CreativeHandoffV1.model_validate_json(pack.payloads[handoff_path])
    brief = ProductionBriefV1.model_validate_json(pack.payloads[brief_path])
    play = next(item for item in plays.plays if item.play_id == primary.play_id)
    assert {card.card_id for card in handoff.concept_cards} == {
        card.card_id for card in play.concept_cards
    }
    assert {card.card_id for card in brief.concept_cards} == {
        card.card_id for card in play.concept_cards
    }
    assert handoff.evidence_packet_sha256 == pack.manifest.evidence_packet_sha256
    assert brief.value_ledger_sha256 == pack.manifest.value_ledger_sha256
    # Hashes are opaque digests; scan the written tree instead of the hash-bearing dict.
    from found_money.activation.pack import _assert_activation_public_safe

    _assert_activation_public_safe(brief.canonical_dict())
    _assert_activation_public_safe(handoff.canonical_dict())


def test_ac5_handoffs_are_deterministic_hashed_linked_and_caller_root_bounded(tmp_path):
    inputs, _plays, primary = _enriched_inputs(mode="public")
    first = build_launch_pack(inputs)
    second = build_launch_pack(inputs)
    handoff_rel = f"creative-handoff/{primary.play_id}-creative-handoff.json"
    brief_rel = f"production-brief/{primary.play_id}-production-brief.json"
    assert (
        first.payloads[f"launch-pack/{handoff_rel}"]
        == second.payloads[f"launch-pack/{handoff_rel}"]
    )
    assert first.payloads[f"launch-pack/{brief_rel}"] == second.payloads[f"launch-pack/{brief_rel}"]
    assert handoff_rel in first.manifest.relative_links
    assert brief_rel in first.manifest.relative_links
    written = write_launch_pack(tmp_path, first)
    assert (tmp_path / "launch-pack" / handoff_rel) in written.values()
    assert (tmp_path / "launch-pack" / brief_rel) in written.values()
    assert scan_output_tree(tmp_path, include_private=True) == []
    prove_writer_rejects_escapes(tmp_path)


def test_ac4_intake_url_rejects_unsafe_schemes_payloads_and_identity():
    assert (
        validate_intake_url("https://intake.example/stealads") == "https://intake.example/stealads"
    )
    assert validate_intake_url("intake/stealads.html") == "intake/stealads.html"
    for bad in (
        "javascript:alert(1)",
        "data:text/html,hi",
        "file:///tmp/x",
        "http://intake.example/x",
        "https://intake.example/x?email=a@b.com",
        "https://intake.example/x#cus_synth_001",
        "https://user:pass@intake.example/x",
        "intake/../secret.json",
        "intake/stealads.html?customer=1",
        "",
    ):
        with pytest.raises(IntakeConfigError):
            validate_intake_url(bad)
    with pytest.raises(IntakeConfigError):
        parse_handoff_intake_config({"stealads_intake_url": "javascript:alert(1)"})


def test_ac2_ac3_configured_and_unconfigured_action_labels():
    unconfigured = build_handoff_actions(
        intake=None,
        creative_handoff_href="launch-pack/creative-handoff/x.json",
        production_brief_href="launch-pack/production-brief/x.json",
    )
    assert unconfigured.stealads.configured is False
    assert unconfigured.stealads.label == "Build in StealAds"
    assert unconfigured.stealads.intake_url is None
    assert unconfigured.matt_emerald.configured is False
    assert unconfigured.matt_emerald.label == "Export production brief"
    assert unconfigured.matt_emerald.intake_url is None

    configured = build_handoff_actions(
        intake={
            "stealads_intake_url": "intake/stealads.html",
            "matt_emerald_intake_url": "intake/matt-emerald.html",
        },
        creative_handoff_href="launch-pack/creative-handoff/x.json",
        production_brief_href="launch-pack/production-brief/x.json",
    )
    assert configured.stealads.configured is True
    assert configured.stealads.intake_url == "intake/stealads.html"
    assert configured.matt_emerald.configured is True
    assert configured.matt_emerald.label == "Have Matt/Emerald Build This"
    assert configured.matt_emerald.intake_url == "intake/matt-emerald.html"


def _materialize_room(tmp_path: Path, *, configured: bool):
    inputs, plays, primary = _enriched_inputs(mode="public")
    pack = build_launch_pack(inputs)
    write_launch_pack(tmp_path, pack)
    creative_href = f"launch-pack/creative-handoff/{primary.play_id}-creative-handoff.json"
    brief_href = f"launch-pack/production-brief/{primary.play_id}-production-brief.json"
    intake = None
    if configured:
        (tmp_path / "intake").mkdir(parents=True, exist_ok=True)
        (tmp_path / "intake" / "stealads.html").write_text(
            local_intake_fixture_html(title="StealAds intake fixture"),
            encoding="utf-8",
        )
        (tmp_path / "intake" / "matt-emerald.html").write_text(
            local_intake_fixture_html(title="Matt Emerald intake fixture"),
            encoding="utf-8",
        )
        intake = {
            "stealads_intake_url": "intake/stealads.html",
            "matt_emerald_intake_url": "intake/matt-emerald.html",
        }
    actions = build_handoff_actions(
        intake=intake,
        creative_handoff_href=creative_href,
        production_brief_href=brief_href,
    )
    html = render_recovery_room_html(
        inputs.money_map,
        plays,
        launch_status=pack.status,
        withheld_assets=list(pack.withheld_asset_ids),
        handoff_actions=actions,
    )
    index = tmp_path / "index.html"
    index.write_text(html, encoding="utf-8")
    for relative, payload in recovery_room_static_assets().items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    return index, pack, primary


def test_ac2_ac3_browser_configured_and_unconfigured_states(tmp_path):
    configured_root = tmp_path / "configured"
    unconfigured_root = tmp_path / "unconfigured"
    configured_root.mkdir()
    unconfigured_root.mkdir()
    configured_index, configured_pack, primary = _materialize_room(configured_root, configured=True)
    unconfigured_index, _unconfigured_pack, _ = _materialize_room(
        unconfigured_root, configured=False
    )

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)

        page = browser.new_page()
        page.goto(configured_index.resolve().as_uri())
        stealads = page.locator("#stealads-intake")
        assert stealads.inner_text() == "Build in StealAds"
        href = stealads.get_attribute("href")
        assert href == "intake/stealads.html"
        parsed = urlsplit(href or "")
        assert parsed.query == ""
        assert parsed.fragment == ""
        with page.expect_navigation():
            stealads.click()
        assert page.url.endswith("/intake/stealads.html")
        assert urlsplit(page.url).query == ""
        assert urlsplit(page.url).fragment == ""

        page.goto(configured_index.resolve().as_uri())
        matt = page.locator("#matt-emerald-intake")
        assert matt.inner_text() == "Have Matt/Emerald Build This"
        with page.expect_navigation():
            matt.click()
        assert page.url.endswith("/intake/matt-emerald.html")

        page.goto(configured_index.resolve().as_uri())
        stealads_export = page.locator("#stealads-export")
        assert "creative-handoff" in (stealads_export.get_attribute("href") or "")
        brief_export = page.locator("#matt-emerald-export")
        assert "production-brief" in (brief_export.get_attribute("href") or "")
        handoff_bytes = (
            configured_root
            / "launch-pack"
            / "creative-handoff"
            / f"{primary.play_id}-creative-handoff.json"
        ).read_bytes()
        brief_bytes = (
            configured_root
            / "launch-pack"
            / "production-brief"
            / f"{primary.play_id}-production-brief.json"
        ).read_bytes()
        CreativeHandoffV1.model_validate_json(handoff_bytes)
        ProductionBriefV1.model_validate_json(brief_bytes)
        assert scan_output_tree(configured_root, include_private=True) == []

        page2 = browser.new_page()
        page2.goto(unconfigured_index.resolve().as_uri())
        assert page2.locator("#stealads-intake").count() == 0
        assert "manually import" in page2.locator("#stealads-instructions").inner_text().casefold()
        assert page2.locator("#stealads-export").count() == 1
        matt_label = page2.locator("#matt-emerald-export-label")
        assert matt_label.inner_text() == "Export production brief"
        assert page2.locator("#matt-emerald-intake").count() == 0
        browser.close()

    assert configured_pack.manifest.approval_only is True


def test_ac4_no_network_connector_or_browser_relay_in_handoff_modules():
    intake_path = ROOT / "found_money" / "activation" / "intake.py"
    tree = ast.parse(intake_path.read_text(encoding="utf-8"))
    forbidden = {"httpx", "requests", "selenium", "webbrowser", "socket"}
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".", 1)[0])
    assert forbidden.isdisjoint(imported)
    # urllib.parse is parsing-only; reject urllib.request specifically.
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "urllib.request":
            raise AssertionError("urllib.request is not allowed in intake module")
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "urllib.request" or alias.name.startswith("urllib.request."):
                    raise AssertionError("urllib.request is not allowed in intake module")
    assert scan_package_capabilities(ROOT) == []


def test_ng1_handoff_path_never_opens_sockets(monkeypatch):
    def _blocked(*_args, **_kwargs):
        raise AssertionError("handoff path must not open sockets")

    monkeypatch.setattr(socket, "socket", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    _ = build_handoff_actions(
        intake={"stealads_intake_url": "https://intake.example/stealads"},
        creative_handoff_href="launch-pack/creative-handoff/x.json",
        production_brief_href="launch-pack/production-brief/x.json",
    )
    inputs, plays, primary = _enriched_inputs()
    pack = build_launch_pack(inputs)
    actions = build_handoff_actions(
        intake={"stealads_intake_url": "intake/stealads.html"},
        creative_handoff_href=(
            f"launch-pack/creative-handoff/{primary.play_id}-creative-handoff.json"
        ),
        production_brief_href=(
            f"launch-pack/production-brief/{primary.play_id}-production-brief.json"
        ),
    )
    html = render_recovery_room_html(
        inputs.money_map,
        plays,
        launch_status=pack.status,
        handoff_actions=actions,
    )
    assert 'data-integration="manual"' in html
    assert "live integration" not in html.casefold()


def test_ac4_rejects_automatic_integration_wording_in_rendered_copy(tmp_path):
    index, _pack, _primary = _materialize_room(tmp_path, configured=False)
    text = index.read_text(encoding="utf-8").casefold()
    assert "manually import" in text
    assert "no automatic integration" in text or "not automatic integrations" in text
    assert "api sync" not in text


def test_production_brief_rejects_finished_asset_claims():
    with pytest.raises(ValidationError, match="finished production assets"):
        ProductionBriefV1(
            play_id="play_payment_rescue_a",
            segment_id="seg_payment_rescue",
            campaign_name="Payment Rescue Campaign",
            money_map_sha256=HASH,
            recovery_plays_sha256=HASH,
            evidence_packet_sha256=HASH,
            value_ledger_sha256=HASH,
            creative_big_idea="Show the missed renewal as recoverable with one clear next step.",
            concept_cards=[
                ProductionBriefCardV1(
                    card_id="card_payment_rescue_a1",
                    card_name="Failed payment tension",
                    format_style="Static social proof card",
                    production_requirements=["Use anonymized invoice timing only"],
                )
            ],
            export_instructions="Export this finished video script and media-buying plan now.",
        )


def _fake_proof_capture(*_args, **_kwargs):
    artifacts = {
        name: (PNG_SIGNATURE + b"fake-png") if name.endswith(".png") else (PDF_SIGNATURE + b"1.4\n")
        for name in REQUIRED_ARTIFACTS
    }
    artifacts[PRINT_REPORT_CONTACT_SHEET] = PNG_SIGNATURE + b"fake-contact-sheet"
    artifacts.update(
        {
            f"print-report-page-{page:02d}.png": PNG_SIGNATURE + f"fake-page-{page}".encode()
            for page in range(1, 20)
        }
    )
    return artifacts


def _fake_print_review_packet():
    artifacts = _fake_proof_capture()
    pages = sorted(name for name in artifacts if name.startswith("print-report-page-"))
    return {
        "platform": resolve_render_baselines_dir().name,
        "run_id": "run_f7e27bfd5aeb154d",
        "reviewer": "test reviewer",
        "reviewed_implementation_head": "0" * 40,
        "pdf_sha256": build_module.sha256_bytes(artifacts["print-report.pdf"]),
        "contact_sheet_sha256": build_module.sha256_bytes(artifacts[PRINT_REPORT_CONTACT_SHEET]),
        "page_reviews": [
            {
                "page": index,
                "artifact": name,
                "sha256": build_module.sha256_bytes(artifacts[name]),
                "status": "pass",
            }
            for index, name in enumerate(pages, start=1)
        ],
    }


@pytest.fixture
def fake_proof(monkeypatch):
    monkeypatch.setattr(build_module, "capture_recovery_room_artifacts", _fake_proof_capture)
    monkeypatch.setattr(build_module, "validate_print_review_packet", _fake_print_review_packet)
    monkeypatch.setattr(
        build_module,
        "capture_four_room_screenshots",
        lambda *_args, **_kwargs: {name: PNG_SIGNATURE + b"fake-png" for name in FOUR_ROOM_PNGS},
    )


def _write_source_config(path: Path, extra: dict | None = None) -> Path:
    payload = json.loads((ROOT / "configs" / "synthetic-saas-thin-slice.json").read_text())
    if extra:
        payload.update(extra)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def _rehash_manifest(payloads: dict[str, bytes]) -> dict[str, bytes]:
    manifest = json.loads(payloads["launch-pack/manifest.json"])
    for entry in manifest["files"]:
        full = f"launch-pack/{entry['path']}"
        entry["sha256"] = hashlib.sha256(payloads[full]).hexdigest()
    payloads["launch-pack/manifest.json"] = LaunchPackManifestV1.model_validate(
        manifest
    ).to_canonical_json()
    return payloads


def test_mustfix_load_source_config_wires_intake_and_rejects_malformed(
    fake_proof, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    configured = _write_source_config(
        tmp_path / "configured.json",
        {
            "handoff_intake": {
                "stealads_intake_url": "https://intake.example/stealads",
                "matt_emerald_intake_url": "intake/matt-emerald.html",
            }
        },
    )
    snapshots, safe_config, runtime_intake = load_source_config(configured)
    assert snapshots
    projection = safe_config["handoff_intake"]
    assert projection["stealads"]["configured"] is True
    assert projection["stealads"]["status"] == "configured"
    assert projection["matt_emerald"]["configured"] is True
    assert "://" not in json.dumps(safe_config)
    assert "intake.example" not in json.dumps(safe_config)
    assert runtime_intake.stealads_intake_url == "https://intake.example/stealads"
    assert runtime_intake.matt_emerald_intake_url == "intake/matt-emerald.html"

    result = build(output_root="configured-out", source_config=configured)
    html = (result.output_root / "index.html").read_text(encoding="utf-8")
    assert 'id="stealads-intake"' in html
    assert 'href="https://intake.example/stealads"' in html
    assert 'id="matt-emerald-intake"' in html
    assert 'data-handoff-intake="true"' in html
    run = json.loads((result.output_root / "run.json").read_text(encoding="utf-8"))
    assert "://" not in json.dumps(run["source_config"])
    assert run["source_config"]["handoff_intake"]["stealads"]["url_sha256"] == (
        hashlib.sha256(b"https://intake.example/stealads").hexdigest()
    )

    unconfigured = build(
        output_root="unconfigured-out",
        source_config=ROOT / "configs" / "synthetic-saas-thin-slice.json",
    )
    unconfigured_html = (unconfigured.output_root / "index.html").read_text(encoding="utf-8")
    assert 'id="stealads-intake"' not in unconfigured_html
    assert 'id="matt-emerald-intake"' not in unconfigured_html
    assert unconfigured.run_id != result.run_id
    unconfigured_run = json.loads(
        (unconfigured.output_root / "run.json").read_text(encoding="utf-8")
    )
    assert "handoff_intake" not in unconfigured_run["source_config"]
    assert "://" not in json.dumps(unconfigured_run["source_config"])

    file_config = {
        "schema_version": "found-money-build-source.v1",
        "mode": "file",
        "credentials": {"credential_mode": "none", "runtime_mode": "test", "scopes": []},
        "sources": {
            "hubspot_snapshot": str(ROOT / "tests/fixtures/saas/thin-slice/hubspot/snapshot.json"),
            "stripe_snapshot": str(ROOT / "tests/fixtures/saas/thin-slice/stripe/snapshot.json"),
        },
        "handoff_intake": {"stealads_intake_url": "intake/stealads.html"},
    }
    file_path = tmp_path / "file.json"
    file_path.write_text(json.dumps(file_config), encoding="utf-8")
    file_result = build(output_root="file-out", source_config=file_path)
    file_html = (file_result.output_root / "index.html").read_text(encoding="utf-8")
    assert 'href="intake/stealads.html"' in file_html
    assert 'id="matt-emerald-intake"' not in file_html

    bad_root = tmp_path / "bad-out"
    bad_config = _write_source_config(
        tmp_path / "bad.json", {"handoff_intake": "javascript:alert(1)"}
    )
    with pytest.raises(BuildConfigError):
        build(output_root=bad_root.name, source_config=bad_config)
    assert not bad_root.exists()
    unsafe = _write_source_config(
        tmp_path / "unsafe.json",
        {"handoff_intake": {"stealads_intake_url": "javascript:alert(1)"}},
    )
    unsafe_root = tmp_path / "unsafe-out"
    with pytest.raises(BuildConfigError):
        build(output_root=unsafe_root.name, source_config=unsafe)
    assert not unsafe_root.exists()


def test_mustfix_typed_config_and_export_hrefs_are_revalidated():
    evil = HandoffIntakeConfigV1(stealads_intake_url="javascript:alert(1)")
    with pytest.raises(IntakeConfigError):
        build_handoff_actions(
            intake=evil,
            creative_handoff_href="launch-pack/creative-handoff/play_x-creative-handoff.json",
            production_brief_href="launch-pack/production-brief/play_x-production-brief.json",
        )
    safe_hrefs = (
        "launch-pack/creative-handoff/play_x-creative-handoff.json",
        "launch-pack/production-brief/play_x-production-brief.json",
    )
    for bad in (
        "javascript:alert(1)",
        "data:text/html,hi",
        "http://evil.example/x.json",
        "file:///tmp/x.json",
        "/tmp/x.json",
        "../launch-pack/creative-handoff/x.json",
        "launch-pack/creative-handoff/x.json?email=a@b.com",
        "launch-pack/creative-handoff/x.json#frag",
        "launch-pack/creative-handoff/%2e%2e/secret.json",
        "launch-pack/other/x.json",
        "C:/launch-pack/creative-handoff/x.json",
    ):
        with pytest.raises(IntakeConfigError):
            build_handoff_actions(
                intake=None,
                creative_handoff_href=bad,
                production_brief_href=safe_hrefs[1],
            )
        with pytest.raises(IntakeConfigError):
            build_handoff_actions(
                intake=None,
                creative_handoff_href=safe_hrefs[0],
                production_brief_href=bad,
            )


def test_mustfix_renderer_rejects_arbitrary_action_objects():
    inputs, plays, _primary = _enriched_inputs()
    fake = SimpleNamespace(
        stealads=SimpleNamespace(
            configured=True,
            label="Build in StealAds",
            intake_url="javascript:alert(1)",
            export_href="javascript:alert(1)",
            instructions="Manually import this approval-only creative handoff into StealAds.",
        ),
        matt_emerald=SimpleNamespace(
            configured=False,
            label="Export production brief",
            intake_url=None,
            export_href="../secret.json",
            instructions="Export this approval-only production brief for Matt/Emerald.",
        ),
    )
    with pytest.raises((IntakeConfigError, ValueError)):
        rendering_module.render_recovery_room_html(inputs.money_map, plays, handoff_actions=fake)


def test_mustfix_marked_anchor_scan_is_fail_closed():
    valid = (
        '<html><body><a id="stealads-intake" data-handoff-intake="true" '
        'data-integration="manual" href="https://intake.example/stealads">'
        "Build in StealAds</a></body></html>"
    )
    rendering_module._public_safe_scan(valid)
    assert scan_text_artifact("index.html", valid, suffix=".html") == []

    malicious = [
        (
            '<a data-handoff-intake="true" href="javascript:alert(1)" '
            'id="stealads-intake" data-integration="manual">x</a>'
        ),
        (
            '<a id="stealads-intake" data-handoff-intake="true" '
            'data-integration="manual" href="https://intake.example/x" '
            'title="user@evil.com">x</a>'
        ),
        ('<div data-handoff-intake="true" href="https://intake.example/x">spoof</div>'),
        (
            '<a id="stealads-intake" data-handoff-intake="true" '
            'data-integration="manual" href="https://intake.example/x?email=a@b.com">x</a>'
        ),
        (
            '<a id="stealads-intake" data-handoff-intake="true" '
            'data-integration="manual" href="https://intake.example/x#cus_synth_001">x</a>'
        ),
        (
            '<a id="stealads-intake" data-handoff-intake="true" '
            'data-integration="manual" href="https://intake.example/x">x</a>'
            '<a id="stealads-intake" data-handoff-intake="true" '
            'data-integration="manual" href="https://intake.example/y">y</a>'
        ),
        (
            '<a id="stealads-intake" data-handoff-intake="true" '
            'data-integration="manual" href="https://intake.example/x">user@evil.com</a>'
        ),
        '<a data-handoff-intake="true" href="https://intake.example/x">missing attrs</a>',
    ]
    for html in malicious:
        wrapped = f"<html><body>{html}</body></html>"
        with pytest.raises(ValueError):
            rendering_module._public_safe_scan(wrapped)
        assert scan_text_artifact("index.html", wrapped, suffix=".html")


def _marked_intake_html(href: str, extra: str = "") -> str:
    suffix = f" {extra}" if extra else ""
    return (
        '<html><body><a id="stealads-intake" data-handoff-intake="true" '
        f'data-integration="manual" href="{href}"{suffix}>Build in StealAds</a></body></html>'
    )


def test_mustfix_validate_intake_url_rejects_phone_credential_and_identity_payloads():
    safe = (
        "https://intake.example/stealads",
        "intake/stealads.html",
        "intake/matt-emerald.html",
    )
    for url in safe:
        assert validate_intake_url(url) == url
        wrapped = _marked_intake_html(url)
        rendering_module._public_safe_scan(wrapped)
        assert scan_text_artifact("index.html", wrapped, suffix=".html") == []
        assert "validated-intake" in html_for_public_scan(wrapped)

    bad = (
        "https://intake.example/stealads/312-555-1212",
        "https://intake.example/+13125551212",
        "https://intake.example/sk_live_abcdefghijk",
        "https://intake.example/cus_synth_001",
        "intake/312-555-1212",
        "intake/+13125551212",
        "intake/sk_live_abcdefghijk",
        "https://intake.example/stealads/%33%31%32-555-1212",
        "https://intake.example/%2B13125551212",
        "https://intake.example/%73%6b%5f%6c%69%76%65%5fabcdefghijk",
        "https://intake.example/%63%75%73%5fsynth_001",
    )
    for url in bad:
        with pytest.raises(IntakeConfigError):
            validate_intake_url(url)
        wrapped = _marked_intake_html(url)
        with pytest.raises(ValueError):
            rendering_module._public_safe_scan(wrapped)
        assert scan_text_artifact("index.html", wrapped, suffix=".html")
        with pytest.raises(IntakeConfigError):
            html_for_public_scan(wrapped)


def test_mustfix_marked_intake_anchor_rejects_extra_attributes():
    valid = _marked_intake_html("https://intake.example/stealads")
    rendering_module._public_safe_scan(valid)
    assert scan_text_artifact("index.html", valid, suffix=".html") == []

    extras = (
        'onclick="alert(1)"',
        "onclick=\"fetch('/exfil', {method: 'POST'})\"",
        'ping="/beacon"',
        "download",
        'formaction="/submit"',
        'style="display:none"',
        'target="_blank"',
        'onmouseover="alert(1)"',
        'href="https://intake.example/stealads" href="https://intake.example/other"',
    )
    for extra in extras:
        wrapped = _marked_intake_html("https://intake.example/stealads", extra)
        with pytest.raises(ValueError):
            rendering_module._public_safe_scan(wrapped)
        assert scan_text_artifact("index.html", wrapped, suffix=".html")
        with pytest.raises(IntakeConfigError):
            html_for_public_scan(wrapped)


def test_mustfix_validate_intake_url_rejects_encoded_traversal_and_bad_hosts():
    for bad in (
        "https://intake.example/%2e%2e/secret",
        "https://intake.example/%2e%2e%2fsecret",
        "https://intake.example/a/%252e%252e/secret",
        "https://intake.example/%2f%2fother",
        "https://intake.example/a%40b",
        "https://intake.example/%3Femail=x",
        "https://intake.example/%23frag",
        "https://intake_example.com/x",
        "https://-intake.example/x",
        "https://intake..example/x",
        "https://intake.example:70000/x",
        "https://intake.example:0/x",
        "https://intake.example:abc/x",
        "intake/%2e%2e/secret.html",
        "https://intake.example/cus_synth_001",
    ):
        with pytest.raises(IntakeConfigError):
            validate_intake_url(bad)


def test_mustfix_handoff_contracts_reject_nested_pii_with_paths():
    cards = [
        CreativeHandoffCardV1(
            card_id="card_payment_rescue_a1",
            card_name="Failed payment tension",
            format_style="Static social proof card",
        )
    ]
    with pytest.raises(ValidationError, match="campaign_name"):
        CreativeHandoffV1(
            play_id="play_payment_rescue_a",
            segment_id="seg_payment_rescue",
            campaign_name="Email user@evil.com now",
            money_map_sha256=HASH,
            recovery_plays_sha256=HASH,
            evidence_packet_sha256=HASH,
            value_ledger_sha256=HASH,
            concept_cards=cards,
            import_instructions=(
                "Manually import this approval-only creative handoff into StealAds. "
                "No automatic integration or audience creation is performed."
            ),
        )
    with pytest.raises(ValidationError, match="concept_cards.0.card_name"):
        CreativeHandoffV1(
            play_id="play_payment_rescue_a",
            segment_id="seg_payment_rescue",
            campaign_name="Payment Rescue Campaign",
            money_map_sha256=HASH,
            recovery_plays_sha256=HASH,
            evidence_packet_sha256=HASH,
            value_ledger_sha256=HASH,
            concept_cards=[
                {
                    "card_id": "card_payment_rescue_a1",
                    "card_name": "Call +1 415 555 0100",
                    "format_style": "Static social proof card",
                }
            ],
            import_instructions=(
                "Manually import this approval-only creative handoff into StealAds. "
                "No automatic integration or audience creation is performed."
            ),
        )
    with pytest.raises(ValidationError, match="play_id"):
        CreativeHandoffV1(
            play_id="cus_synth_001",
            segment_id="seg_payment_rescue",
            campaign_name="Payment Rescue Campaign",
            money_map_sha256=HASH,
            recovery_plays_sha256=HASH,
            evidence_packet_sha256=HASH,
            value_ledger_sha256=HASH,
            concept_cards=cards,
            import_instructions=(
                "Manually import this approval-only creative handoff into StealAds. "
                "No automatic integration or audience creation is performed."
            ),
        )
    with pytest.raises(ValidationError, match="creative_big_idea"):
        ProductionBriefV1(
            play_id="play_payment_rescue_a",
            segment_id="seg_payment_rescue",
            campaign_name="Payment Rescue Campaign",
            money_map_sha256=HASH,
            recovery_plays_sha256=HASH,
            evidence_packet_sha256=HASH,
            value_ledger_sha256=HASH,
            creative_big_idea="Open https://evil.example/now with the secret sk_live_abc1234567",
            concept_cards=[
                ProductionBriefCardV1(
                    card_id="card_payment_rescue_a1",
                    card_name="Failed payment tension",
                    format_style="Static social proof card",
                    production_requirements=["Use anonymized invoice timing only"],
                )
            ],
            export_instructions=(
                "Export this approval-only production brief for Matt/Emerald. "
                "No automatic integration, finished video script, or media-buying plan is included."
            ),
        )
    ok = _sample_handoff()
    assert ok.money_map_sha256 == HASH
    assert ok.play_id == "play_payment_rescue_a"


def test_mustfix_validate_launch_pack_binds_ordered_card_ids():
    inputs, _plays, primary = _enriched_inputs()
    pack = build_launch_pack(inputs)
    plays_path = "launch-pack/recovery-plays.json"
    assert plays_path in pack.payloads
    canonical = CompleteRecoveryPlaySetV1.model_validate_json(pack.payloads[plays_path])
    play = next(item for item in canonical.plays if item.play_id == primary.play_id)
    expected = [card.card_id for card in play.concept_cards]
    handoff_path = f"launch-pack/creative-handoff/{primary.play_id}-creative-handoff.json"
    brief_path = f"launch-pack/production-brief/{primary.play_id}-production-brief.json"
    handoff = json.loads(pack.payloads[handoff_path])
    brief = json.loads(pack.payloads[brief_path])
    assert [card["card_id"] for card in handoff["concept_cards"]] == expected
    assert [card["card_id"] for card in brief["concept_cards"]] == expected

    reordered = dict(pack.payloads)
    handoff["concept_cards"] = list(reversed(handoff["concept_cards"]))
    reordered[handoff_path] = (
        json.dumps(handoff, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    _rehash_manifest(reordered)
    with pytest.raises(LaunchPackValidationError, match="card"):
        validate_launch_pack_payloads(reordered)

    missing = dict(pack.payloads)
    brief["concept_cards"] = brief["concept_cards"][:-1]
    missing[brief_path] = (json.dumps(brief, sort_keys=True, separators=(",", ":")) + "\n").encode()
    _rehash_manifest(missing)
    with pytest.raises((LaunchPackValidationError, ValidationError)):
        validate_launch_pack_payloads(missing)


def _loopback_server(root: Path):
    class _Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(root), **kwargs)

        def log_message(self, *_args, **_kwargs):
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def test_mustfix_browser_downloads_match_canonical_pack_bytes(tmp_path):
    configured_root = tmp_path / "configured"
    unconfigured_root = tmp_path / "unconfigured"
    configured_root.mkdir()
    unconfigured_root.mkdir()
    configured_index, configured_pack, primary = _materialize_room(configured_root, configured=True)
    unconfigured_index, unconfigured_pack, _ = _materialize_room(
        unconfigured_root, configured=False
    )
    handoff_rel = f"launch-pack/creative-handoff/{primary.play_id}-creative-handoff.json"
    brief_rel = f"launch-pack/production-brief/{primary.play_id}-production-brief.json"
    configured_handoff = configured_pack.payloads[handoff_rel]
    configured_brief = configured_pack.payloads[brief_rel]
    unconfigured_handoff = unconfigured_pack.payloads[handoff_rel]
    unconfigured_brief = unconfigured_pack.payloads[brief_rel]

    configured_server = _loopback_server(configured_root)
    unconfigured_server = _loopback_server(unconfigured_root)
    try:
        configured_origin = f"http://127.0.0.1:{configured_server.server_address[1]}/index.html"
        unconfigured_origin = f"http://127.0.0.1:{unconfigured_server.server_address[1]}/index.html"
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(configured_origin)
            with page.expect_download() as download_info:
                page.locator("#stealads-export").click()
            downloaded = Path(download_info.value.path())
            assert downloaded.read_bytes() == configured_handoff
            page.goto(configured_origin)
            with page.expect_download() as download_info:
                page.locator("#matt-emerald-export").click()
            assert Path(download_info.value.path()).read_bytes() == configured_brief

            page.goto(unconfigured_origin)
            with page.expect_download() as download_info:
                page.locator("#stealads-export").click()
            assert Path(download_info.value.path()).read_bytes() == unconfigured_handoff
            page.goto(unconfigured_origin)
            with page.expect_download() as download_info:
                page.locator("#matt-emerald-export-label").click()
            assert Path(download_info.value.path()).read_bytes() == unconfigured_brief
            browser.close()
    finally:
        configured_server.shutdown()
        unconfigured_server.shutdown()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(configured_index.resolve().as_uri())
        assert page.locator("#stealads-intake").count() == 1
        page.goto(unconfigured_index.resolve().as_uri())
        assert page.locator("#stealads-intake").count() == 0
        browser.close()
