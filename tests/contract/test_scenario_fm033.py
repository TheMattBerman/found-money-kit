"""FM-033 reusable scenario harness and canonical SaaS-v1 proof."""

from __future__ import annotations

import io
import json
import socket
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from found_money.build import (
    SYNTHETIC_SAAS_V1_FIXTURE,
    BuildPathError,
    _fixture_snapshots,
    _run_id_for,
    _safe_source_config,
    build,
)
from found_money.contracts.campaign import StrategyReviewerPacketV2
from found_money.contracts.scenarios import ScenarioDefinitionV1, parse_scenario_definition
from found_money.events import detect_event_families
from found_money.identity import build_identity_graph, normalize_source_records
from found_money.receipts import sha256_bytes
from found_money.rendering import render_print_report_html
from found_money.rendering.proof import (
    CANONICAL_PRINT_REPORT_PAGE_COUNT,
    NAMED_VIEWPORT_PNGS,
    PDF_SIGNATURE,
    PNG_SIGNATURE,
    PRINT_PAGE_VIEWPORT,
    assert_us_letter_pdf,
    canonical_print_report_page_artifacts,
    count_print_report_html_pages,
    png_dimensions,
)
from found_money.safety import scan_output_tree
from found_money.safety.output_scan import TEXT_SUFFIXES, scan_text_artifact
from found_money.scenarios import (
    RAW_SCENARIO_SOURCE_IDS,
    apply_complete_plays_to_money_map,
    load_scenario_definition,
    load_scenario_snapshot_bytes,
    load_scenario_snapshots,
    load_scenario_source_config_bytes,
    run_scenario_engine,
    validate_scenario_output_tree,
    validate_scenario_reviewer_packet,
)
from found_money.strategy import apply_complete_plays_to_money_map as strategy_helper

from _reviewer_packets import synthetic_reviewer_packet

ROOT = Path(__file__).resolve().parents[2]
THIN_CONFIG = ROOT / "configs" / "synthetic-saas-thin-slice.json"
THIN_HUBSPOT = ROOT / "tests" / "fixtures" / "saas" / "thin-slice" / "hubspot" / "snapshot.json"
REVIEWER_PACKET = (
    ROOT / "tests" / "fixtures" / "saas" / "strategy" / "fm033" / "blind-reviewer-packet.json"
)
REQUIRED_FAMILIES = {
    "failed_payment",
    "expired_trial",
    "canceled_customer",
    "closed_lost_stale_deal",
    "renewal_upsell",
}


def _write_packaged_config(directory: Path) -> Path:
    path = directory / "synthetic-saas-v1.json"
    path.write_bytes(load_scenario_source_config_bytes())
    return path


def _public_safe_config() -> dict:
    return _safe_source_config(
        source_mode="fixture",
        run_mode="public",
        fixture=SYNTHETIC_SAAS_V1_FIXTURE,
    )


def _locked_run_id() -> str:
    return _run_id_for(_fixture_snapshots(SYNTHETIC_SAAS_V1_FIXTURE), _public_safe_config())


def _engine(run_id: str | None = None):
    safe = _public_safe_config()
    return run_scenario_engine(
        run_id=run_id or _locked_run_id(),
        safe_config=safe,
        definition=load_scenario_definition(),
    )


def test_saas_v1_fixture_is_separate_from_thin_slice():
    packaged = load_scenario_snapshot_bytes()
    thin = (
        ROOT / "tests" / "fixtures" / "saas" / "thin-slice" / "stripe" / "snapshot.json"
    ).read_bytes()
    assert packaged["stripe"] != thin
    assert "cus_synth_001" not in packaged["stripe"].decode("utf-8")
    assert "in_saas_fp_001" in packaged["stripe"].decode("utf-8")
    assert THIN_HUBSPOT.read_bytes() != packaged["hubspot"]


def test_definition_is_extra_forbid_and_hash_locked():
    definition = load_scenario_definition()
    raw = (
        ROOT / "found_money" / "scenarios" / "fixtures" / "saas-v1" / "definition.json"
    ).read_bytes()
    assert parse_scenario_definition(raw).to_canonical_json() == raw
    payload = json.loads(raw.decode("utf-8"))
    payload["unexpected"] = True
    with pytest.raises(ValidationError):
        ScenarioDefinitionV1.model_validate(payload)
    from found_money.scenarios.transports import packaged_fixture_hashes

    assert packaged_fixture_hashes() == definition.fixture_hashes
    assert sha256_bytes(load_scenario_source_config_bytes()) == definition.source_config_hash
    assert definition.expected_event_families == [
        "failed_payment",
        "expired_trial",
        "canceled_customer",
        "closed_lost_stale_deal",
        "renewal_upsell",
    ]
    assert len(definition.expected_play_ids) == 3
    assert len(definition.expected_card_ids) == 9
    assert definition.inherited_proof_contract.claims_new_visual_baseline is False
    assert definition.inherited_proof_contract.claims_print_ratification is False


def test_locked_scenario_print_html_matches_canonical_page_count_contract():
    engine = _engine()
    html = render_print_report_html(engine.enriched_money_map, engine.strategy_run.recovery_plays)
    assert count_print_report_html_pages(html) == CANONICAL_PRINT_REPORT_PAGE_COUNT
    assert canonical_print_report_page_artifacts() == tuple(
        f"print-report-page-{index:02d}.png"
        for index in range(1, CANONICAL_PRINT_REPORT_PAGE_COUNT + 1)
    )


def test_detector_emits_five_headline_families_without_overlap_or_hardcoded_labels():
    snapshots = load_scenario_snapshots()
    graph = build_identity_graph(
        normalize_source_records(snapshots, default_observed_at=load_scenario_definition().clock),
        run_id="run_detector_proof",
        built_at=load_scenario_definition().clock,
    )
    assert len(graph.customers) == 5
    assert graph.ambiguous_identities == []
    detection = detect_event_families(
        snapshots,
        graph,
        run_id="run_detector_proof",
        built_at=load_scenario_definition().clock,
    )
    families = {item.event_family for item in detection.candidates.candidates}
    assert families == REQUIRED_FAMILIES
    assert detection.public_projection.exclusion_count == 0
    by_customer = {}
    for candidate in detection.candidates.candidates:
        by_customer.setdefault(candidate.customer_token, set()).add(candidate.event_family)
    assert all(len(items) == 1 for items in by_customer.values())
    assert len(by_customer) == 5


def test_engine_ranks_observed_4900_payment_rescue_first_and_plays_do_not_claim_other_piles():
    engine = _engine()
    ranked = sorted(engine.enriched_money_map.piles, key=lambda item: item.rank)
    assert ranked[0].pile_id == "payment_rescue"
    assert str(ranked[0].selected_value_minor) == "4900"
    assert ranked[0].currency == "usd"
    assert engine.enriched_money_map.recommended_play_ids == list(
        load_scenario_definition().expected_play_ids
    )
    assert ranked[0].navigation is not None
    assert ranked[0].navigation.play_id == engine.enriched_money_map.recommended_play_ids[0]
    for pile in ranked[1:]:
        assert pile.navigation is None or pile.navigation.play_id is None
        assert pile.selected_value_minor != ranked[0].selected_value_minor
    blob = engine.strategy_run.recovery_plays.to_canonical_json().decode("utf-8")
    for family in REQUIRED_FAMILIES - {"failed_payment"}:
        assert family not in blob
        assert family.replace("_", " ") not in blob.casefold()


def test_complete_play_helper_records_all_ids_and_rejects_unknown_duplicate_order():
    engine = _engine("run_complete_helper")
    enriched = strategy_helper(engine.money_map, engine.strategy_run.recovery_plays)
    assert enriched.recommended_play_ids == load_scenario_definition().expected_play_ids
    primary = sorted(engine.money_map.piles, key=lambda item: item.rank)[0]
    matched = next(pile for pile in enriched.piles if pile.pile_id == primary.pile_id)
    assert matched.navigation is not None
    assert matched.navigation.play_id == enriched.recommended_play_ids[0]
    plays = sorted(engine.strategy_run.recovery_plays.plays, key=lambda item: item.rank)
    unknown = SimpleNamespace(
        run_id=engine.money_map.run_id,
        plays=[
            SimpleNamespace(play_id=play.play_id, pile_id="missing_pile", rank=play.rank)
            for play in plays
        ],
    )
    with pytest.raises(ValueError, match="unknown pile"):
        apply_complete_plays_to_money_map(engine.money_map, unknown)  # type: ignore[arg-type]
    duplicate = SimpleNamespace(
        run_id=engine.money_map.run_id,
        plays=[
            SimpleNamespace(play_id="same-play-identifier", pile_id="payment_rescue", rank=1),
            SimpleNamespace(play_id="same-play-identifier", pile_id="payment_rescue", rank=2),
            SimpleNamespace(play_id="other-play-identifier", pile_id="payment_rescue", rank=3),
        ],
    )
    with pytest.raises(ValueError, match="duplicate play"):
        apply_complete_plays_to_money_map(engine.money_map, duplicate)  # type: ignore[arg-type]
    reversed_set = engine.strategy_run.recovery_plays.model_copy(
        update={"plays": list(reversed(plays))}
    )
    with pytest.raises(ValueError, match="rank order"):
        apply_complete_plays_to_money_map(engine.money_map, reversed_set)


def test_thin_slice_build_preserved(fake_proof, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = build(output_root="thin-out", source_config=THIN_CONFIG)
    assert not (result.output_root / "scenario").exists()
    assert (result.output_root / "run.json").is_file()
    money_map = json.loads((result.output_root / "money-map.json").read_text(encoding="utf-8"))
    assert len(money_map["piles"]) == 1
    assert money_map["piles"][0]["pile_id"] == "payment_rescue"


def test_scenario_build_tree_manifest_public_scan_and_tamper(fake_proof, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = _write_packaged_config(tmp_path)
    result = build(output_root="saas-out", source_config=config)
    validate_scenario_output_tree(result.output_root)
    text_violations: list[str] = []
    for path in result.output_root.rglob("*"):
        if path.is_file() and path.suffix.lower() in TEXT_SUFFIXES:
            text_violations.extend(
                scan_text_artifact(
                    path.relative_to(result.output_root).as_posix(),
                    path.read_text(encoding="utf-8"),
                    suffix=path.suffix.lower(),
                )
            )
    assert text_violations == []
    run = json.loads((result.output_root / "run.json").read_text(encoding="utf-8"))
    manifest = json.loads(
        (result.output_root / "scenario" / "manifest.json").read_text(encoding="utf-8")
    )
    hashed = {item["path"] for item in manifest["artifacts"]}
    assert "scenario/manifest.json" not in hashed
    assert "run.json" not in hashed
    assert "scenario/manifest.json" in {item["path"] for item in run["artifacts"]}
    assert "run.json" not in {item["path"] for item in run["artifacts"]}
    print_review = json.loads(
        (result.output_root / "render-proof" / "print-report-manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert print_review["human_review"]["status"] == "pending"
    extra = result.output_root / "unexpected.json"
    extra.write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="extra files"):
        validate_scenario_output_tree(result.output_root)
    extra.unlink()
    target = result.output_root / "money-map.json"
    original = target.read_bytes()
    mutated = json.loads(original.decode("utf-8"))
    mutated["piles"][0]["selected_value_minor"] = "1"
    target.write_bytes((json.dumps(mutated, sort_keys=True, separators=(",", ":")) + "\n").encode())
    with pytest.raises(ValueError, match="hash mismatch"):
        validate_scenario_output_tree(result.output_root)
    _rehash_manifests(result.output_root)
    with pytest.raises(ValueError, match="4900"):
        validate_scenario_output_tree(result.output_root)
    target.write_bytes(original)
    _rehash_manifests(result.output_root)
    validate_scenario_output_tree(result.output_root)
    link = result.output_root / "link-out.json"
    link.symlink_to(result.output_root / "run.json")
    with pytest.raises(ValueError, match="symlink"):
        validate_scenario_output_tree(result.output_root)
    link.unlink()
    audit = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in result.output_root.rglob("*")
        if path.is_file() and path.suffix in {".json", ".html", ".md", ".csv"}
    )
    for noun in ("Acme", "Globex", "Salesforce", "Shopify"):
        assert noun not in audit
    for source_id in RAW_SCENARIO_SOURCE_IDS:
        assert source_id not in audit
    assert "api.hubapi.com" not in audit
    assert "api.stripe.com" not in audit


def test_two_mktemp_cli_runs_are_byte_identical_and_stay_in_caller_root(fake_proof, monkeypatch):
    from found_money.__main__ import main

    trees: list[dict[str, bytes]] = []
    roots: list[Path] = []
    for _ in range(2):
        cwd = Path(tempfile.mkdtemp(prefix="fm033-cli-"))
        assert ROOT not in cwd.parents and cwd != ROOT
        config = _write_packaged_config(cwd)
        monkeypatch.chdir(cwd)
        monkeypatch.setattr(
            sys,
            "argv",
            ["found-money", "build", "--config", str(config), "--output-root", "out"],
        )
        assert main() == 0
        out = cwd / "out"
        roots.append(out)
        trees.append(
            {
                path.relative_to(out).as_posix(): path.read_bytes()
                for path in sorted(out.rglob("*"))
                if path.is_file()
            }
        )
        assert all(ROOT not in path.parents for path in out.rglob("*") if path.is_file())
    assert trees[0] == trees[1]
    assert set(trees[0]) == set(trees[1])
    with pytest.raises(BuildPathError):
        build(output_root="../escaped", source_config=_write_packaged_config(roots[0].parent))


def test_scenario_build_has_no_network_or_provider_mutations(fake_proof, tmp_path, monkeypatch):
    def blocked(*_args, **_kwargs):
        raise AssertionError("network access is forbidden")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(socket, "socket", blocked)
    build(output_root="out", source_config=_write_packaged_config(tmp_path))


def test_reviewer_packet_validator_rejects_unbound_and_wrong_producer_packets():
    engine = _engine()
    unbound = synthetic_reviewer_packet("0" * 64, producer="codex")
    with pytest.raises(ValueError, match="does not bind"):
        validate_scenario_reviewer_packet(
            unbound,
            engine.strategy_run.recovery_plays,
            producer_model_family="fixture-strategy-v1",
        )
    rebound = unbound.model_copy(
        update={
            "reviewed_artifact_sha256": sha256_bytes(
                engine.strategy_run.recovery_plays.to_canonical_json()
            )
        }
    )
    with pytest.raises(ValueError, match="producer family"):
        validate_scenario_reviewer_packet(
            rebound,
            engine.strategy_run.recovery_plays,
            producer_model_family="fixture-strategy-v1",
        )


@pytest.mark.release_evidence
def test_ac4_committed_blind_reviewer_packet_binds_locked_scenario_plays():
    engine = _engine()
    digest = sha256_bytes(engine.strategy_run.recovery_plays.to_canonical_json())
    assert REVIEWER_PACKET.is_file(), (
        "AC-4 unresolved: a genuine isolated blind reviewer packet is still required at "
        f"{REVIEWER_PACKET.relative_to(ROOT)} bound to locked synthetic-saas-v1 "
        "recovery-plays with blind=true, producer_context_supplied=false, "
        "producer_model_family=fixture-strategy-v1, and reviewer_model_family "
        f"different from that producer. Required reviewed_artifact_sha256={digest}. "
        "Do not copy or relabel the FM-026 packet."
    )
    packet = StrategyReviewerPacketV2.model_validate_json(REVIEWER_PACKET.read_bytes())
    validate_scenario_reviewer_packet(
        packet,
        engine.strategy_run.recovery_plays,
        producer_model_family="fixture-strategy-v1",
    )
    assert packet.reviewed_artifact_sha256 == digest
    assert packet.blind is True
    assert packet.producer_context_supplied is False


def test_real_chromium_mechanical_proof_no_console_clip_responsive_a11y_us_letter(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    result = build(output_root="proof-out", source_config=_write_packaged_config(tmp_path))
    validate_scenario_output_tree(result.output_root)
    pdf = (result.output_root / "print-report.pdf").read_bytes()
    text = assert_us_letter_pdf(pdf)
    # Copy states major units since FM-054; the ledger keeps minor units.
    assert "49.00" in text
    for play_id in load_scenario_definition().expected_play_ids:
        assert play_id in text or play_id in (result.output_root / "index.html").read_text(
            encoding="utf-8"
        )
    proof = result.output_root / "render-proof"
    assert (proof / "index.png").read_bytes() == (
        proof / "money-map-desktop-1440x900.png"
    ).read_bytes()
    assert (proof / "top-play.png").read_bytes() == (
        proof / "top-play-desktop-1440x900.png"
    ).read_bytes()
    named = [(proof / name).read_bytes() for name in NAMED_VIEWPORT_PNGS]
    assert len(set(named)) == len(named)
    for name in ("index.png", "top-play.png"):
        assert (proof / name).read_bytes().startswith(PNG_SIGNATURE)
    page_pngs = sorted(proof.glob("print-report-page-*.png"))
    assert [path.name for path in page_pngs] == list(canonical_print_report_page_artifacts())
    for path in page_pngs:
        payload = path.read_bytes()
        assert png_dimensions(payload) == PRINT_PAGE_VIEWPORT
    assert pdf.startswith(PDF_SIGNATURE)
    assert (proof / "print-report.pdf").read_bytes() == pdf
    print_review = json.loads((proof / "print-report-manifest.json").read_text(encoding="utf-8"))
    assert print_review["page_count"] == CANONICAL_PRINT_REPORT_PAGE_COUNT
    assert len(print_review["page_artifacts"]) == CANONICAL_PRINT_REPORT_PAGE_COUNT
    assert print_review["human_review"]["status"] == "pending"
    assert scan_output_tree(result.output_root, include_private=True) == []


def _rehash_manifests(root: Path) -> None:
    files = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
        and path.relative_to(root).as_posix() not in {"run.json", "scenario/manifest.json"}
    }
    scenario = json.loads((root / "scenario" / "manifest.json").read_text(encoding="utf-8"))
    scenario["artifacts"] = [
        {"path": path, "sha256": sha256_bytes(data)} for path, data in sorted(files.items())
    ]
    scenario["definition_sha256"] = sha256_bytes(files["scenario/definition.json"])
    scenario["aggregate_receipt_sha256"] = sha256_bytes(files["scenario/aggregate-receipt.json"])
    manifest_bytes = (json.dumps(scenario, sort_keys=True, separators=(",", ":")) + "\n").encode()
    (root / "scenario" / "manifest.json").write_bytes(manifest_bytes)
    run = json.loads((root / "run.json").read_text(encoding="utf-8"))
    hashed = dict(files)
    hashed["scenario/manifest.json"] = manifest_bytes
    run["artifacts"] = [
        {"path": path, "sha256": sha256_bytes(data)} for path, data in sorted(hashed.items())
    ]
    (root / "run.json").write_bytes(
        (json.dumps(run, sort_keys=True, separators=(",", ":")) + "\n").encode()
    )


@pytest.fixture
def fake_proof(monkeypatch):
    import found_money.build as build_module
    from PIL import Image
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    from found_money.rendering.proof import (
        FOUR_ROOM_PNGS,
        PRINT_REPORT_CONTACT_SHEET,
        build_print_contact_sheet,
        rasterize_print_report_page_pngs,
        resolve_render_baselines_dir,
    )

    def _png(width: int, height: int, color: tuple[int, int, int]) -> bytes:
        image = Image.new("RGB", (width, height), color)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()

    def _minimal_pdf() -> bytes:
        writer = PdfWriter()
        body = (
            "Money Map Recovery Plays Human control "
            "payment-rescue-friction-fix payment-rescue-proof-reset "
            "payment-rescue-capacity-window $49.00"
        )
        for index in range(1, CANONICAL_PRINT_REPORT_PAGE_COUNT + 1):
            page = writer.add_blank_page(width=612, height=792)
            font = DictionaryObject(
                {
                    NameObject("/Type"): NameObject("/Font"),
                    NameObject("/Subtype"): NameObject("/Type1"),
                    NameObject("/BaseFont"): NameObject("/Helvetica"),
                }
            )
            resources = DictionaryObject()
            resources[NameObject("/Font")] = DictionaryObject(
                {NameObject("/F1"): writer._add_object(font)}
            )
            page[NameObject("/Resources")] = resources
            stream = DecodedStreamObject()
            stream.set_data(
                f"BT /F1 18 Tf 72 720 Td (page {index:02d}) Tj 0 -24 Td ({body}) Tj ET".encode()
            )
            page[NameObject("/Contents")] = writer._add_object(stream)
        buffer = io.BytesIO()
        writer.write(buffer)
        return buffer.getvalue()

    def _fake_capture(*_args, **_kwargs):
        money_desktop = _png(1440, 900, (12, 34, 56))
        top_desktop = _png(1440, 900, (70, 80, 90))
        print_pdf = _minimal_pdf()
        page_pngs = rasterize_print_report_page_pngs(print_pdf)
        artifacts = {
            "index.png": money_desktop,
            "index.pdf": print_pdf,
            "top-play.png": top_desktop,
            "top-play.pdf": print_pdf,
            "money-map-desktop-1440x900.png": money_desktop,
            "money-map-mobile-390x844.png": _png(390, 844, (22, 44, 66)),
            "top-play-desktop-1440x900.png": top_desktop,
            "top-play-mobile-390x844.png": _png(390, 844, (32, 54, 76)),
            "print-report.pdf": print_pdf,
            PRINT_REPORT_CONTACT_SHEET: build_print_contact_sheet(page_pngs),
        }
        artifacts.update(
            {
                f"print-report-page-{index:02d}.png": payload
                for index, payload in enumerate(page_pngs, start=1)
            }
        )
        return artifacts

    def _fake_print_review():
        artifacts = _fake_capture()
        pages = sorted(name for name in artifacts if name.startswith("print-report-page-"))
        return {
            "platform": resolve_render_baselines_dir().name,
            "run_id": "run_f7e27bfd5aeb154d",
            "reviewer": "test reviewer",
            "reviewed_implementation_head": "0" * 40,
            "pdf_sha256": build_module.sha256_bytes(artifacts["print-report.pdf"]),
            "contact_sheet_sha256": build_module.sha256_bytes(
                artifacts[PRINT_REPORT_CONTACT_SHEET]
            ),
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

    monkeypatch.setattr(build_module, "capture_recovery_room_artifacts", _fake_capture)
    monkeypatch.setattr(build_module, "validate_print_review_packet", _fake_print_review)
    monkeypatch.setattr(
        build_module,
        "capture_four_room_screenshots",
        lambda *_args, **_kwargs: {
            name: _png(1440, 900, (100 + index * 10, 20, 30))
            for index, name in enumerate(FOUR_ROOM_PNGS)
        },
    )
