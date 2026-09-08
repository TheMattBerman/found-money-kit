"""Tree-closure and semantic cross-binding for scenario outputs."""

from __future__ import annotations

import io
import json
import re
import unicodedata
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import unquote, urlparse

from pypdf import PdfReader

from found_money.activation.intake import build_handoff_actions
from found_money.activation.pack import LaunchPackInputs, build_launch_pack
from found_money.contracts.activation import (
    HandoffIntakeConfigV1,
    LaunchPackManifestV1,
    WithheldAssetV1,
)
from found_money.contracts.build import parse_canonical_json as parse_build_manifest
from found_money.contracts.events import PublicEventProjectionV1
from found_money.contracts.map import MoneyMapV1
from found_money.contracts.run import StageState, compute_source_set_hash
from found_money.contracts.scenarios import (
    ScenarioIdentityAggregateV1,
    ScenarioManifestV1,
    ScenarioValueAggregateV1,
    parse_scenario_aggregate,
    parse_scenario_definition,
    parse_scenario_manifest,
    parse_scenario_situations,
)
from found_money.contracts.source import SourceReceiptV1
from found_money.contracts.strategy import RecoveryPlaySetV1, RecoveryPlayV1
from found_money.receipts import build_run_manifest, sha256_bytes
from found_money.contracts.value import format_major_units
from found_money.rendering import (
    build_recovery_room_manifest,
    pile_display_name,
    recovery_room_static_assets,
    render_print_report_html,
    render_recovery_room_html,
    render_top_play_html,
)
from found_money.rendering.proof import (
    CANONICAL_PRINT_REPORT_PAGE_COUNT,
    DESKTOP_VIEWPORT,
    FOUR_ROOM_PNGS,
    NAMED_VIEWPORT_DIMENSIONS,
    NAMED_VIEWPORT_PNGS,
    PRINT_PAGE_VIEWPORT,
    PRINT_REPORT_CONTACT_SHEET,
    assert_us_letter_pdf,
    build_print_contact_sheet,
    build_print_report_manifest,
    canonical_png_raster_hash,
    canonical_print_report_page_artifacts,
    count_print_report_html_pages,
    png_dimensions,
    rasterize_print_report_page_pngs,
    validate_png_bytes,
)
from found_money.safety.output_scan import (
    contains_recovered_revenue_claim,
    scan_output_tree,
    scan_pdf_bytes,
    scan_png_bytes,
)
from found_money.scenarios.assets import is_ecommerce_fixture, load_scenario_definition
from found_money.scenarios.registry import scenario_arm
from found_money.scenarios.harness import (
    SCENARIO_AGGREGATE_PATH,
    SCENARIO_DEFINITION_PATH,
    SCENARIO_MANIFEST_PATH,
    SCENARIO_SITUATIONS_PATH,
    run_scenario_engine,
)
from found_money.scenarios.transports import (
    derive_raw_source_ids,
    load_normalized_scenario_snapshots,
    packaged_fixture_hashes,
)
from found_money.strategy import (
    canonical_ecommerce_business_profile,
    canonical_saas_business_profile,
    canonical_service_business_profile,
)

_FORBIDDEN_PROPER_NOUNS = (
    "Acme",
    "Globex",
    "Initech",
    "Umbrella Corp",
    "Wonka",
    "Salesforce",
    "Shopify",
)
_PROVIDER_CUSTOMER_ID_RE = re.compile(
    r"\b(?:cus_(?!t_)[A-Za-z0-9_]*[0-9][A-Za-z0-9_]*|"
    r"hs_(?:contact|ct|dl|deal)_[A-Za-z0-9_]+)\b"
)
_UNSAFE_SCHEME_RE = re.compile(r"(?i)(?:^|[\s\"'(<])(?:file|https?|mailto|javascript|data):")
_SAFE_ID_RE = re.compile(r"^(?:cust_|rec_)[0-9a-f]{8,}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


def _withheld_reason(asset_id: str) -> str:
    if asset_id == "high_value_cart_dependent_output":
        return "missing_high_value_cart"
    if asset_id == "no_show_rebook_dependent_output":
        return "missing_appointments"
    if asset_id == "silent_proposal_dependent_output":
        return "missing_proposals"
    if asset_id == "crm_dependent_output":
        return "missing_crm"
    if asset_id in {
        "payment_dependent_output",
        "trial_no_convert_dependent_output",
        "canceled_customer_dependent_output",
        "disappeared_high_value_customer_dependent_output",
    }:
        return "missing_payment"
    return asset_id


_REQUIRED_COMPLETED_STAGES = (
    "credential_declaration",
    "source_read",
    "identity",
    "events",
    "value",
    "money_map",
    "fixture_strategy_one_play",
    "render",
    "three_play_strategy",
    "activation_launch_pack",
    "scenario_harness",
)
_RUN_MANIFEST_EXCLUDED = frozenset(
    {"run.json", "scenario/manifest.json", "provenance/run-manifest.json"}
)


def _locked_public_source_config(fixture_id: str) -> dict[str, str]:
    locked: dict[str, str] = {
        "schema_version": "found-money-build-source.v1",
        "mode": "fixture",
        "run_mode": "public",
        "credential_declaration": "none",
        "credential_runtime": "test",
        "fixture": fixture_id,
    }
    if is_ecommerce_fixture(fixture_id):
        definition = load_scenario_definition(fixture_id)
        locked["high_value_cart_state"] = str(definition.high_value_cart_state)
        locked["high_value_cart_hash"] = str(definition.high_value_cart_hash)
    return locked


_PROVENANCE_SCHEMA_VERSIONS = {
    "found-money-build": "found-money-build.v1",
    "source-receipt": "source-receipt.v1",
    "run-manifest": "run-manifest.v1",
    "money-map": "money-map.v1",
    "recovery-plays": "recovery-plays.v1",
    "strategy-evidence-packet": "grounded-strategy-evidence.v1",
    "strategy-audit-receipt": "strategy-audit-receipt.v1",
    "recovery-play-differentiation": "recovery-play-differentiation.v1",
    "recovery-room-manifest": "recovery-room-manifest.v1",
    "found-money-scenario-definition": "found-money-scenario-definition.v1",
    "found-money-scenario-manifest": "found-money-scenario-manifest.v1",
    "found-money-scenario-aggregate": "found-money-scenario-aggregate.v1",
}


def _tree_files(root: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"scenario output contains a symlink: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if ".." in Path(relative).parts:
            raise ValueError(f"scenario output path contains traversal: {relative}")
        files[relative] = path.read_bytes()
    return files


def _pdf_text(payload: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(payload))
        texts = [(page.extract_text() or "") for page in reader.pages]
        metadata = reader.metadata
        if metadata is not None:
            texts.extend(f"{key}:{value}" for key, value in metadata.items())
        for page in reader.pages:
            annots = page.get("/Annots")
            if not annots:
                continue
            for annot in annots:
                obj = annot.get_object()
                action = obj.get("/A")
                if action is not None:
                    action = action.get_object()
                    uri = action.get("/URI")
                    if uri is not None:
                        texts.append(str(uri))
        return unicodedata.normalize("NFKC", "\n".join(texts))
    except Exception:
        return ""


def _pdf_uris(payload: bytes) -> list[str]:
    uris: list[str] = []
    reader = PdfReader(io.BytesIO(payload))
    for page in reader.pages:
        annots = page.get("/Annots")
        if not annots:
            continue
        for annot in annots:
            obj = annot.get_object()
            action = obj.get("/A")
            if action is not None:
                action = action.get_object()
                for key in ("/URI", "/F"):
                    value = action.get(key)
                    if value is not None:
                        uris.append(str(value))
            dest = obj.get("/Dest")
            if dest is not None:
                uris.append(str(dest))
    return uris


def _json(payload: bytes) -> Any:
    return json.loads(payload.decode("utf-8"))


def _as_int(value: Any) -> int:
    return int(str(value).split(".", 1)[0])


def _require_contains(haystack: str, needle: str, *, label: str) -> None:
    if needle not in haystack:
        raise ValueError(f"{label} missing {needle}")


def _require_exact_bytes(actual: bytes, expected: bytes, *, label: str) -> None:
    if actual != expected:
        raise ValueError(f"{label} does not match independently recomputed output")


def _canonical_json_bytes(payload: Any) -> bytes:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return (text + "\n").encode("utf-8")


def _posix_join_norm(*parts: str) -> str:
    tokens: list[str] = []
    for part in parts:
        for token in str(part).replace("\\", "/").split("/"):
            if token in {"", "."}:
                continue
            if token == "..":
                if not tokens:
                    raise ValueError("relative path traversal escapes the output tree")
                tokens.pop()
                continue
            tokens.append(token)
    return "/".join(tokens)


class _RecoveryRoomDom(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.pile_rows: list[dict[str, str]] = []
        self._in_find_ol = False
        self._find_depth = 0
        self._current_li: dict[str, str] | None = None
        self._li_text: list[str] = []
        self._section_stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {key.casefold(): ("" if value is None else value) for key, value in attrs}
        element_id = attr_map.get("id")
        if element_id:
            self.ids.append(element_id)
        if tag == "section" and (attr_map.get("data-room") == "find" or element_id == "room-find"):
            self._section_stack.append("find")
        if tag == "ol" and "find" in self._section_stack:
            self._in_find_ol = True
            self._find_depth += 1
        if tag == "li" and self._in_find_ol:
            self._current_li = {
                "id": attr_map.get("id", ""),
                "rank": attr_map.get("data-rank", ""),
                "pile_id": attr_map.get("data-pile-id", ""),
            }
            self._li_text = []
        if tag == "span" and self._current_li is not None:
            span_id = attr_map.get("id", "")
            if span_id.startswith("pile-") and span_id[5:].isdigit():
                self._current_li["rank"] = span_id.split("-", 1)[1]
        for attr in ("href", "src"):
            if attr in attr_map:
                self.links.append((attr, attr_map[attr]))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self._current_li is not None:
            self._li_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "li" and self._current_li is not None:
            self._current_li["text"] = " ".join(
                part.strip() for part in self._li_text if part.strip()
            )
            self.pile_rows.append(self._current_li)
            self._current_li = None
            self._li_text = []
        if tag == "ol" and self._in_find_ol:
            self._find_depth = max(0, self._find_depth - 1)
            if self._find_depth == 0:
                self._in_find_ol = False
        if tag == "section" and self._section_stack:
            self._section_stack.pop()


def _parse_dom(html: str) -> _RecoveryRoomDom:
    parser = _RecoveryRoomDom()
    parser.feed(html)
    parser.close()
    return parser


def _id_counts(ids: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in ids:
        counts[item] = counts.get(item, 0) + 1
    return counts


def validate_scenario_output_tree(output_root: Path | str) -> None:
    root = Path(output_root)
    files = _tree_files(root)
    if SCENARIO_MANIFEST_PATH not in files or "run.json" not in files:
        raise ValueError("scenario output is missing manifest or run.json")
    manifest = parse_scenario_manifest(files[SCENARIO_MANIFEST_PATH])
    run = parse_build_manifest(files["run.json"])
    if manifest.run_id != run.run_id:
        raise ValueError("scenario manifest run_id does not bind to run.json")
    hashed = {item.path: item.sha256 for item in manifest.artifacts}
    if SCENARIO_MANIFEST_PATH in hashed or "run.json" in hashed:
        raise ValueError("scenario manifest illegally self-hashes or hashes run.json")
    extra = set(files) - set(hashed) - {SCENARIO_MANIFEST_PATH, "run.json"}
    missing = set(hashed) - set(files)
    if extra:
        raise ValueError("scenario output contains extra files: " + ", ".join(sorted(extra)))
    if missing:
        raise ValueError(
            "scenario manifest references missing files: " + ", ".join(sorted(missing))
        )
    for path, digest in hashed.items():
        if sha256_bytes(files[path]) != digest:
            raise ValueError(f"scenario artifact hash mismatch: {path}")
    run_hashes = {item.path: item.sha256 for item in run.artifacts}
    if SCENARIO_MANIFEST_PATH not in run_hashes:
        raise ValueError("run.json does not bind the scenario manifest")
    if run_hashes[SCENARIO_MANIFEST_PATH] != sha256_bytes(files[SCENARIO_MANIFEST_PATH]):
        raise ValueError("run.json scenario manifest hash is stale")
    if "run.json" in run_hashes:
        raise ValueError("run.json must not self-hash")
    for path, digest in run_hashes.items():
        if path == SCENARIO_MANIFEST_PATH:
            continue
        if path not in files:
            raise ValueError(f"run.json references missing file: {path}")
        if sha256_bytes(files[path]) != digest:
            raise ValueError(f"run.json hash mismatch: {path}")
    _validate_public_safety(root, files)
    _validate_semantics(files, manifest, run)


def _validate_semantics(files: Mapping[str, bytes], manifest: ScenarioManifestV1, run: Any) -> None:
    definition = parse_scenario_definition(files[SCENARIO_DEFINITION_PATH])
    fixture_id = definition.fixture_id
    locked = load_scenario_definition(fixture_id)
    if definition.to_canonical_json() != locked.to_canonical_json():
        raise ValueError("definition hashes do not match the locked scenario definition")
    if definition.scenario_id != locked.scenario_id or manifest.scenario_id != locked.scenario_id:
        raise ValueError("scenario id does not match the locked definition")
    snapshots, raw, receipts = load_normalized_scenario_snapshots(
        fixture_id, retrieved_at=definition.clock
    )
    expected_hashes = {name: sha256_bytes(raw[name]) for name in locked.sources}
    if dict(run.source_hashes) != expected_hashes:
        raise ValueError("run source hashes do not match locked source content")
    expected_set = compute_source_set_hash(
        {
            f"receipts/{name}.source-receipt.json": receipts[name].content_hash
            for name in locked.sources
        }
    )
    if run.source_set_hash != expected_set:
        raise ValueError("run source_set_hash does not match locked source receipts")
    if run.mode != "public":
        raise ValueError("run mode is not the locked public scenario mode")
    for name in _REQUIRED_COMPLETED_STAGES:
        if run.stages.get(name) != "completed":
            raise ValueError("run stages do not match the locked scenario completion set")
    if run.stages.get("native_live_connectors") != "not_started":
        raise ValueError("run stages do not match the locked scenario completion set")
    locked_config = _locked_public_source_config(fixture_id)
    if _canonical_json_bytes(dict(run.source_config)) != _canonical_json_bytes(locked_config):
        raise ValueError(
            "run source_config does not match the locked canonical public source_config"
        )
    expected_run_id = (
        "run_"
        + sha256_bytes(
            _canonical_json_bytes(
                {"source_config": locked_config, "source_hashes": expected_hashes}
            )
        )[:16]
    )
    if run.run_id != expected_run_id:
        raise ValueError("run_id does not match canonical source_config plus source hashes")
    engine = run_scenario_engine(
        run_id=expected_run_id, safe_config=locked_config, fixture_id=fixture_id
    )
    if engine.definition.scenario_id != locked.scenario_id:
        raise ValueError("scenario id does not match the locked definition")
    aggregate = parse_scenario_aggregate(files[SCENARIO_AGGREGATE_PATH])
    identity = ScenarioIdentityAggregateV1.model_validate(_json(files["identity/public.json"]))
    events = PublicEventProjectionV1.model_validate(_json(files["events/public.json"]))
    value = ScenarioValueAggregateV1.model_validate(_json(files["value/public.json"]))
    money_map_data = _json(files["money-map.json"])
    launch_money_data = _json(files["launch-pack/money-map.json"])
    plays = _json(files["recovery-plays.json"])
    html = files["index.html"].decode("utf-8")
    top = files["top-play.html"].decode("utf-8")
    pdf = _pdf_text(files["print-report.pdf"])
    launch = LaunchPackManifestV1.model_validate(_json(files["launch-pack/manifest.json"]))
    if identity.to_canonical_json() != engine.identity_aggregate.to_canonical_json():
        raise ValueError("identity aggregate does not match independently recomputed identity")
    if list(identity.source_systems) != list(locked.sources):
        raise ValueError("identity source_systems do not match locked sources")
    if identity.resolved_customer_count != definition.expected_resolved_accounts:
        raise ValueError("identity aggregate does not match the locked account count")
    if identity.ambiguous_cluster_count != 0:
        raise ValueError("identity aggregate reports an ambiguous cluster")
    if aggregate.to_canonical_json() != engine.aggregate_receipt.to_canonical_json():
        raise ValueError("aggregate receipt does not match independently recomputed aggregate")
    if list(aggregate.sources) != list(locked.sources):
        raise ValueError("aggregate sources do not match locked sources")
    if aggregate.mode != "public":
        raise ValueError("aggregate mode is not the locked public scenario mode")
    expected_counts = dict(engine.aggregate_receipt.candidates_by_family)
    if dict(events.candidates_by_family) != expected_counts:
        raise ValueError("family count mismatch in public events")
    if dict(aggregate.candidates_by_family) != expected_counts:
        raise ValueError("aggregate family counts do not match locked candidates")
    missing_families = set(definition.expected_event_families) - set(expected_counts)
    if missing_families:
        raise ValueError("public events are missing locked families")
    if events.to_canonical_json() != engine.event_public.to_canonical_json():
        raise ValueError("public events do not match independently recomputed events")
    if events.exclusion_count != engine.event_public.exclusion_count:
        raise ValueError("aggregate exclusions do not match the locked scenario")
    if events.data_gap_count != engine.event_public.data_gap_count:
        raise ValueError("aggregate data gaps do not match the locked scenario")
    if list(events.named_data_gaps) != list(engine.event_public.named_data_gaps):
        raise ValueError("public named data gaps do not match independently recomputed gaps")
    if dict(events.exclusions_by_reason) != dict(engine.event_public.exclusions_by_reason):
        raise ValueError(
            "public exclusion reasons do not match independently recomputed exclusions"
        )
    if aggregate.exclusion_count != engine.aggregate_receipt.exclusion_count:
        raise ValueError("aggregate exclusions do not match the locked scenario")
    if aggregate.data_gap_count != engine.aggregate_receipt.data_gap_count:
        raise ValueError("aggregate data gaps do not match the locked scenario")
    if list(aggregate.named_data_gaps or []) != list(
        engine.aggregate_receipt.named_data_gaps or []
    ):
        raise ValueError("aggregate named data gaps do not match independently recomputed gaps")
    if dict(aggregate.exclusions_by_reason or {}) != dict(
        engine.aggregate_receipt.exclusions_by_reason or {}
    ):
        raise ValueError(
            "aggregate exclusion reasons do not match independently recomputed exclusions"
        )
    if locked.business_model == "ecommerce":
        if SCENARIO_SITUATIONS_PATH not in files:
            raise ValueError("ecommerce output is missing durable situation evidence")
        from found_money.scenarios.situations import assert_public_situation_payload

        raw_situations = json.loads(files[SCENARIO_SITUATIONS_PATH].decode("utf-8"))
        assert_public_situation_payload(raw_situations)
        situations = parse_scenario_situations(files[SCENARIO_SITUATIONS_PATH])
        if engine.situation_set is None:
            raise ValueError("ecommerce engine did not recompute situation evidence")
        if situations.to_canonical_json() != engine.situation_set.to_canonical_json():
            raise ValueError("situation evidence does not match independently recomputed evidence")
        expected_rows = {item.situation_id: item for item in engine.situation_set.situations}
        for situation_row in situations.situations:
            expected_situation = expected_rows[situation_row.situation_id]
            if (
                situation_row.qualifying_count != expected_situation.qualifying_count
                or situation_row.totals_minor_by_currency
                != expected_situation.totals_minor_by_currency
                or situation_row.status != expected_situation.status
                or situation_row.evidence_digest != expected_situation.evidence_digest
            ):
                raise ValueError(
                    "situation evidence does not match independently recomputed evidence"
                )
        if situations.high_value_cart_state != locked.high_value_cart_state:
            raise ValueError("situation cart state does not match the locked definition")
        if situations.high_value_cart_hash != locked.high_value_cart_hash:
            raise ValueError("situation cart hash does not match the locked definition")
        if aggregate.high_value_cart_state != locked.high_value_cart_state:
            raise ValueError("aggregate cart state does not match the locked definition")
        if aggregate.high_value_cart_hash != locked.high_value_cart_hash:
            raise ValueError("aggregate cart hash does not match the locked definition")
        if list(aggregate.situation_ids or []) != situations.present_ids():
            raise ValueError("aggregate situation_ids do not match durable situation evidence")
        if "refunded_buyer_next_move" not in engine.typed_situations:
            raise ValueError("ecommerce refunded-buyer situation is missing")
        if locked.high_value_cart_state == "supplied":
            if "high_value_cart" not in engine.typed_situations:
                raise ValueError("ecommerce cart-supplied situation is missing")
            if engine.event_public.data_gap_count != 0:
                raise ValueError("cart-supplied ecommerce run emitted an unexpected data gap")
        else:
            if "high_value_cart" in engine.typed_situations:
                raise ValueError("omitted cart must not invent a high-value cart situation")
            if "missing_high_value_cart" not in engine.data_gap_details:
                raise ValueError("missing cart did not emit a named data gap")
            if "high_value_cart_dependent_output" not in engine.withheld_asset_ids:
                raise ValueError("missing cart did not withhold dependent output")
            blob = files[SCENARIO_SITUATIONS_PATH].decode("utf-8")
            if '"status":"present"' in blob and "high_value_cart" in blob:
                cart_row = next(
                    item for item in situations.situations if item.situation_id == "high_value_cart"
                )
                if cart_row.status == "present":
                    raise ValueError("omitted cart claimed a high-value cart situation")
    if value.to_canonical_json() != engine.value_aggregate.to_canonical_json():
        raise ValueError("public value aggregate does not match independently recomputed value")
    _require_exact_bytes(
        files["recovery-plays.json"],
        engine.strategy_run.recovery_plays.to_canonical_json(),
        label="recovery plays",
    )
    _require_exact_bytes(
        files["provenance/strategy-evidence-packet.json"],
        engine.strategy_run.packet.to_canonical_json(),
        label="grounded evidence packet",
    )
    _require_exact_bytes(
        files["provenance/strategy-audit-receipt.json"],
        engine.strategy_run.receipt.to_canonical_json(),
        label="strategy audit receipt",
    )
    if engine.strategy_run.differentiation is not None:
        _require_exact_bytes(
            files["provenance/content-differentiation-report.json"],
            engine.strategy_run.differentiation.to_canonical_json(),
            label="differentiation report",
        )
    elif "provenance/content-differentiation-report.json" in files:
        raise ValueError("missing payment published a differentiation report for withheld plays")
    play_ids = [play["play_id"] for play in sorted(plays["plays"], key=lambda item: item["rank"])]
    payment_withheld = locked.business_model == "service" and "stripe" in set(
        locked.omitted_service_sources or []
    )
    if payment_withheld:
        if play_ids or aggregate.play_ids:
            raise ValueError("missing payment published payment-grounded recovery plays")
    elif play_ids != definition.expected_play_ids or play_ids != aggregate.play_ids:
        raise ValueError("play ID cross-binding failed")
    card_ids = [
        card["card_id"]
        for play in sorted(plays["plays"], key=lambda item: item["rank"])
        for card in play.get("concept_cards") or []
    ]
    if payment_withheld:
        if card_ids or aggregate.card_ids:
            raise ValueError("missing payment published payment-grounded concept cards")
    elif card_ids != definition.expected_card_ids or card_ids != aggregate.card_ids:
        raise ValueError("card ID cross-binding failed")
    _validate_money_map_agreement(
        money_map_data,
        launch_money_data,
        launch,
        files,
        html,
        pdf,
        play_ids,
        engine.enriched_money_map,
        business_model=locked.business_model,
    )
    value_rows = {row.pile_id: row for row in value.piles}
    for expected in definition.expected_values:
        if expected.amount_minor is None:
            continue
        row = value_rows.get(expected.pile_id)
        if row is None:
            raise ValueError(f"public value aggregate missing pile {expected.pile_id}")
        if _as_int(row.amount_minor) != _as_int(expected.amount_minor):
            raise ValueError(
                f"value amount mismatch for {expected.pile_id}: expected {expected.amount_minor}"
            )
        if row.currency != expected.currency or row.value_basis != expected.value_basis:
            raise ValueError(f"value basis mismatch for {expected.pile_id}")
    ranked = sorted(money_map_data["piles"], key=lambda item: item["rank"])
    map_total = sum((_as_int(item["selected_value_minor"]) for item in ranked), 0)
    if _as_int(aggregate.identified_opportunity_minor["usd"]) != map_total:
        raise ValueError("aggregate opportunity does not match ranked pile totals")
    value_total = sum(_as_int(row.amount_minor) for row in value.piles)
    if value_total != map_total:
        raise ValueError("public value aggregate does not match Money Map totals")
    map_binds_plays = bool(engine.enriched_money_map.recommended_play_ids)
    for play_id in play_ids:
        _require_contains(json.dumps(plays), play_id, label="recovery-plays.json")
        if map_binds_plays:
            _require_contains(json.dumps(money_map_data), play_id, label="money-map.json")
        _require_contains(html, play_id, label="index.html")
        _require_contains(pdf, play_id, label="print-report.pdf")
        if play_id not in launch.play_ids:
            raise ValueError(f"launch pack missing play {play_id}")
    for card_id in card_ids:
        _require_contains(json.dumps(plays), card_id, label="recovery-plays.json")
        _require_contains(html, card_id, label="index.html")
    if payment_withheld:
        published = "\n".join(
            [
                json.dumps(plays),
                json.dumps(money_map_data),
                html,
                top,
                pdf,
                json.dumps(launch.model_dump(mode="json")),
            ]
        )
        for play_id in definition.expected_play_ids:
            if play_id in published:
                raise ValueError("missing payment published a payment-grounded recovery play")
        if "22000" in published:
            raise ValueError("missing payment fabricated disappeared-high-value play value")
        if launch.status == "approval_ready":
            raise ValueError("missing payment marked payment-dependent launch assets ready")
        if launch.available_play_count != 0 or launch.play_ids:
            raise ValueError("missing payment published ready launch-pack plays")
    primary_amount = str(scenario_arm(locked.fixture_id).primary_amount_minor)
    primary_major = f"${format_major_units(int(primary_amount), 'usd')}"
    _require_contains(json.dumps(money_map_data), primary_amount, label="money-map.json")
    _require_contains(html, primary_amount, label="index.html")
    _require_contains(pdf, primary_major, label="print-report.pdf")
    _require_contains(
        value.to_canonical_json().decode("utf-8"), primary_amount, label="value/public.json"
    )
    if locked.business_model == "service":
        from found_money.scenarios.harness import SERVICE_SOURCE_GAP_SPECS

        omitted = set(locked.omitted_service_sources or [])
        named = list(engine.event_public.named_data_gaps)
        if list(engine.enriched_money_map.named_data_gaps) != named:
            raise ValueError("Money Map named data gaps do not match recomputed event gaps")
        if list(money_map_data.get("named_data_gaps") or []) != named:
            raise ValueError("durable Money Map named data gaps do not match recomputed gaps")
        packet_text = files["provenance/strategy-evidence-packet.json"].decode("utf-8")
        for gap_code in named:
            _require_contains(html, gap_code, label="index.html")
            _require_contains(pdf, gap_code, label="print-report.pdf")
            _require_contains(json.dumps(money_map_data), gap_code, label="money-map.json")
            _require_contains(packet_text, gap_code, label="strategy evidence packet")
        reasons = dict(engine.event_public.exclusions_by_reason)
        if dict(engine.enriched_money_map.event_exclusions_by_reason) != reasons:
            raise ValueError("Money Map event exclusions do not match recomputed exclusions")
        if dict(money_map_data.get("event_exclusions_by_reason") or {}) != reasons:
            raise ValueError(
                "durable Money Map event exclusions do not match recomputed exclusions"
            )
        for reason in reasons:
            _require_contains(html, reason, label="index.html")
            _require_contains(pdf, reason, label="print-report.pdf")
            _require_contains(json.dumps(money_map_data), reason, label="money-map.json")
            _require_contains(packet_text, reason, label="strategy evidence packet")
        for source in omitted:
            spec = SERVICE_SOURCE_GAP_SPECS.get(source)
            if spec is None:
                raise ValueError(f"unsupported omitted service source: {source}")
            detail, _reference, assets = spec
            if detail not in engine.data_gap_details or detail not in named:
                raise ValueError(f"missing {source} did not emit a named data gap")
            for asset_id in assets:
                if asset_id not in engine.withheld_asset_ids:
                    raise ValueError(f"missing {source} did not withhold dependent output")
        if "stripe" in omitted and "22000" in json.dumps(money_map_data):
            raise ValueError("missing payment fabricated disappeared-high-value map value")
        if "stripe" in omitted:
            for play_id in definition.expected_play_ids:
                if play_id in json.dumps(plays) or play_id in html or play_id in pdf:
                    raise ValueError("missing payment published a payment-grounded recovery play")
            if "22000" in json.dumps(plays) or "22000" in html or "22000" in pdf:
                raise ValueError("missing payment fabricated disappeared-high-value play value")
        blob = "\n".join(
            [
                json.dumps(plays),
                json.dumps(money_map_data),
                html,
                pdf,
                files["provenance/strategy-evidence-packet.json"].decode("utf-8"),
            ]
        ).casefold()
        for token in scenario_arm(locked.fixture_id).forbidden_copy:
            if token in blob:
                raise ValueError(f"service outputs copied forbidden language: {token}")
    if locked.business_model == "ecommerce":
        blob = "\n".join(
            [
                json.dumps(plays),
                json.dumps(money_map_data),
                html,
                pdf,
                files["provenance/strategy-evidence-packet.json"].decode("utf-8"),
            ]
        ).casefold()
        for token in (
            "payment_rescue",
            "payment rescue",
            "4900",
            "card-retry",
            "card retry",
            "billing portal",
            "billing-portal",
        ):
            if token in blob:
                raise ValueError(f"ecommerce outputs copied forbidden SaaS language: {token}")
    index_dom = _parse_dom(html)
    top_dom = _parse_dom(top)
    _validate_html_document(html, index_dom, files, play_ids, label="index.html")
    _validate_html_document(top, top_dom, files, play_ids, label="top-play.html")
    _validate_pdf_uris(files)
    if set(launch.play_ids) != set(play_ids):
        raise ValueError("launch pack play membership is incomplete")
    if sha256_bytes(files["money-map.json"]) != launch.money_map_sha256:
        raise ValueError("launch money-map hash does not match money-map.json")
    if sha256_bytes(files["recovery-plays.json"]) != launch.recovery_plays_sha256:
        raise ValueError("launch recovery_plays digest does not match recovery-plays.json")
    if (
        sha256_bytes(files["provenance/strategy-evidence-packet.json"])
        != launch.evidence_packet_sha256
    ):
        raise ValueError("launch evidence_packet digest does not match the evidence packet")
    if sha256_bytes(files["launch-pack/activation-policy.json"]) != launch.activation_policy_sha256:
        raise ValueError("launch activation_policy digest does not match the policy artifact")
    if sha256_bytes(engine.ledger.to_canonical_json()) != launch.value_ledger_sha256:
        raise ValueError("launch value_ledger digest does not match the recomputed ledger")
    if not launch.relative_links:
        raise ValueError("launch pack relative links are missing")
    for link in launch.relative_links:
        target = link if link.startswith("launch-pack/") else f"launch-pack/{link}"
        if target not in files and link not in files:
            raise ValueError(f"launch relative link is missing: {link}")
    for entry in launch.files:
        relative = (
            entry.path if entry.path.startswith("launch-pack/") else f"launch-pack/{entry.path}"
        )
        if relative not in files:
            raise ValueError(f"launch file is missing: {entry.path}")
        if sha256_bytes(files[relative]) != entry.sha256:
            raise ValueError(f"launch file hash mismatch: {entry.path}")
    expected_launch = build_launch_pack(
        LaunchPackInputs(
            money_map=engine.enriched_money_map,
            recovery_plays=engine.strategy_run.recovery_plays,
            evidence_packet=engine.strategy_run.packet,
            contribution_ledger=engine.ledger,
            identity_graph=engine.graph,
            candidates=engine.candidates,
            exclusions=engine.exclusions,
            business_profile=(
                canonical_ecommerce_business_profile()
                if locked.business_model == "ecommerce"
                else canonical_service_business_profile()
                if locked.business_model == "service"
                else canonical_saas_business_profile()
            ),
            withheld_decisions=tuple(
                WithheldAssetV1(
                    asset_id=asset_id,
                    asset_class=asset_id,
                    reason=_withheld_reason(asset_id),
                    depends_on=[_withheld_reason(asset_id)],
                )
                for asset_id in engine.withheld_asset_ids
            ),
            source_snapshots=engine.raw_snapshots,
            mode="public",
        )
    )
    for path, payload in expected_launch.payloads.items():
        if path not in files:
            raise ValueError(f"launch pack missing recomputed payload: {path}")
        if files[path] != payload:
            raise ValueError(
                f"launch pack payload does not match independently recomputed launch pack: {path}"
            )
    extra_launch = sorted(
        path
        for path in files
        if path.startswith("launch-pack/") and path not in expected_launch.payloads
    )
    if extra_launch:
        raise ValueError("launch pack contains extra files: " + ", ".join(extra_launch))
    _validate_rerendered_text_artifacts(files, engine, expected_launch)
    expected_print_html = render_print_report_html(
        engine.enriched_money_map,
        engine.strategy_run.recovery_plays,
        contribution_ledger=engine.ledger,
    )
    expected_print_page_count = count_print_report_html_pages(expected_print_html)
    if expected_print_page_count != CANONICAL_PRINT_REPORT_PAGE_COUNT and not payment_withheld:
        raise ValueError(
            "independently rerendered print report page count does not match "
            "the canonical print contract"
        )
    expected_print_manifest = _validate_print_report_manifest(
        files,
        run_id=expected_run_id,
        source_set_hash=expected_set,
        play_ids=play_ids,
        expected_page_count=expected_print_page_count,
    )
    _validate_live_proof_pngs(files)
    _validate_scenario_manifest_fields(manifest, locked, engine, receipts, raw, files, run.run_id)
    expected_receipts: dict[str, SourceReceiptV1] = {}
    for name in locked.sources:
        receipt_path = f"receipts/{name}.source-receipt.json"
        expected_receipts[receipt_path] = receipts[name]
        _require_exact_bytes(
            files[receipt_path],
            receipts[name].to_canonical_json(),
            label=f"{name} source receipt",
        )
    if locked.business_model == "saas" and receipts["stripe"].record_count != 8:
        raise ValueError("Stripe contributing record count must include subscriptions (8)")
    expected_bytes = dict(files)
    expected_bytes["recovery-plays.json"] = engine.strategy_run.recovery_plays.to_canonical_json()
    expected_bytes["provenance/strategy-evidence-packet.json"] = (
        engine.strategy_run.packet.to_canonical_json()
    )
    expected_bytes["provenance/strategy-audit-receipt.json"] = (
        engine.strategy_run.receipt.to_canonical_json()
    )
    if engine.strategy_run.differentiation is not None:
        expected_bytes["provenance/content-differentiation-report.json"] = (
            engine.strategy_run.differentiation.to_canonical_json()
        )
    expected_bytes["money-map.json"] = engine.enriched_money_map.to_canonical_json()
    expected_bytes.update(expected_launch.payloads)
    expected_bytes["render-proof/print-report-manifest.json"] = expected_print_manifest
    for path, receipt in expected_receipts.items():
        expected_bytes[path] = receipt.to_canonical_json()
    expected_stages: dict[str, StageState] = {
        name: "completed" for name in _REQUIRED_COMPLETED_STAGES
    }
    expected_stages["native_live_connectors"] = "not_started"
    referenced = dict(_PROVENANCE_SCHEMA_VERSIONS)
    if locked.business_model == "ecommerce":
        referenced["found-money-scenario-situations"] = "found-money-scenario-situations.v1"
        if engine.situation_set is not None:
            expected_bytes[SCENARIO_SITUATIONS_PATH] = engine.situation_set.to_canonical_json()
    expected_run_manifest = build_run_manifest(
        run_id=run.run_id,
        referenced_schema_versions=referenced,
        started_at=definition.clock,
        completed_at=definition.clock,
        mode="public",
        source_receipts=expected_receipts,
        stages=expected_stages,
        artifact_hashes={
            path: sha256_bytes(data)
            for path, data in expected_bytes.items()
            if path not in _RUN_MANIFEST_EXCLUDED
        },
    )
    _require_exact_bytes(
        files["provenance/run-manifest.json"],
        expected_run_manifest.to_canonical_json(),
        label="provenance run-manifest",
    )
    _ = snapshots


def _money_map_projection(payload: Mapping[str, Any] | MoneyMapV1) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]]
    if isinstance(payload, MoneyMapV1):
        rows = [
            (
                pile.rank,
                pile.pile_id,
                pile.currency,
                _as_int(pile.selected_value_minor),
                pile.value_basis,
            )
            for pile in payload.piles
        ]
    else:
        rows = [
            (
                int(pile["rank"]),
                str(pile["pile_id"]),
                str(pile["currency"]),
                _as_int(pile["selected_value_minor"]),
                str(pile["value_basis"]),
            )
            for pile in payload["piles"]
        ]
    return sorted(rows, key=lambda item: item[0])


def _validate_money_map_agreement(
    money_map: Mapping[str, Any],
    launch_money: Mapping[str, Any],
    launch: LaunchPackManifestV1,
    files: Mapping[str, bytes],
    html: str,
    pdf: str,
    play_ids: list[str],
    expected_map: MoneyMapV1,
    *,
    business_model: str,
) -> None:
    expected = _money_map_projection(expected_map)
    actual = _money_map_projection(money_map)
    launch_proj = _money_map_projection(launch_money)
    if actual != expected:
        expected_amounts = [str(item[3]) for item in expected]
        raise ValueError(
            "Money Map rank/pile/value projection does not match the recomputed map; "
            f"expected amounts {', '.join(expected_amounts)}"
        )
    if launch_proj != expected:
        raise ValueError(
            "Money Map launch-pack copy disagrees with the canonical ranked projection"
        )
    if sha256_bytes(files["money-map.json"]) != launch.money_map_sha256:
        raise ValueError("launch money-map hash does not match money-map.json")
    ranked = sorted(money_map["piles"], key=lambda item: item["rank"])
    expected_by_pile = {item.pile_id: item for item in expected_map.piles}
    for pile in ranked:
        expected_pile = expected_by_pile.get(pile["pile_id"])
        if expected_pile is None:
            raise ValueError(f"unexpected pile {pile['pile_id']}")
        if int(pile["rank"]) != expected_pile.rank:
            raise ValueError("Money Map rank order does not match the canonical projection")
        if pile["currency"] != expected_pile.currency:
            raise ValueError(f"value currency mismatch for {pile['pile_id']}")
        if pile["value_basis"] != expected_pile.value_basis:
            raise ValueError(f"value basis mismatch for {pile['pile_id']}")
        if _as_int(pile["selected_value_minor"]) != _as_int(expected_pile.selected_value_minor):
            raise ValueError(
                f"value amount mismatch for {pile['pile_id']}: expected {expected_pile.selected_value_minor}"
            )
        if _as_int(pile.get("economic_unit_count", 1)) < 1:
            raise ValueError(f"event count missing for {pile['pile_id']}")
    primary = ranked[0]
    if business_model == "ecommerce":
        if (
            primary["pile_id"] != "disappeared_high_value_customer"
            or _as_int(primary["selected_value_minor"]) != 18500
        ):
            raise ValueError("Money Map primary pile is not observed 18500 dormant VIP")
    elif business_model == "service":
        expected_primary = sorted(expected_map.piles, key=lambda item: item.rank)[0]
        if primary["pile_id"] != expected_primary.pile_id or _as_int(
            primary["selected_value_minor"]
        ) != _as_int(expected_primary.selected_value_minor):
            raise ValueError("Money Map primary pile does not match the recomputed service ranking")
    elif primary["pile_id"] != "payment_rescue" or _as_int(primary["selected_value_minor"]) != 4900:
        raise ValueError("Money Map primary pile is not observed 4900 payment_rescue")
    if expected_map.recommended_play_ids:
        if money_map["recommended_play_ids"] != play_ids:
            raise ValueError("Money Map recommended play IDs do not record all three plays")
        nav = primary.get("navigation") or {}
        if nav.get("play_id") != play_ids[0]:
            raise ValueError("primary pile navigation does not point at the primary play")
    elif money_map.get("recommended_play_ids"):
        raise ValueError("Money Map attached plays despite withheld payment-dependent strategy")
    for pile in ranked[1:]:
        other_nav = pile.get("navigation") or {}
        if other_nav.get("play_id"):
            raise ValueError(f"non-payment pile {pile['pile_id']} falsely claims play coverage")
    dom = _parse_dom(html)
    html_proj: list[tuple[int, str, str]] = []
    for row in dom.pile_rows:
        pile_id = row.get("pile_id") or ""
        rank_text = row.get("rank") or ""
        if not rank_text.isdigit():
            continue
        html_proj.append((int(rank_text), pile_id, row.get("text", "")))
    html_proj.sort(key=lambda item: item[0])
    ranked_ids = {str(pile["pile_id"]) for pile in ranked}
    quantified_html = [item for item in html_proj if item[1] in ranked_ids]
    if [item[0] for item in quantified_html] != [int(pile["rank"]) for pile in ranked]:
        raise ValueError("Money Map HTML rank order does not match the canonical projection")
    for pile, html_row in zip(ranked, quantified_html, strict=True):
        if pile["pile_id"] not in html_row[1] and pile["pile_id"] not in html_row[2]:
            raise ValueError("Money Map HTML pile identity does not match the canonical projection")
        amount = str(_as_int(pile["selected_value_minor"]))
        if amount not in html_row[2] and amount not in html:
            raise ValueError("Money Map HTML value text does not match the canonical projection")
        if f"pile-{pile['rank']}" not in dom.ids:
            raise ValueError(f"Money Map HTML missing rank anchor pile-{pile['rank']}")
    present_names = [
        pile_display_name(str(pile["pile_id"]))
        for pile in ranked
        if pile_display_name(str(pile["pile_id"])) in pdf
    ]
    expected_names = [pile_display_name(str(pile["pile_id"])) for pile in ranked]
    if present_names and present_names != expected_names:
        raise ValueError("Money Map rank order is missing from print-report.pdf")
    if present_names:
        positions = [pdf.find(name) for name in present_names]
        if positions != sorted(positions):
            raise ValueError("Money Map rank order is missing from print-report.pdf")


def _validate_rerendered_text_artifacts(
    files: Mapping[str, bytes],
    engine: Any,
    expected_launch: Any,
) -> None:
    if engine.enriched_money_map.recommended_play_ids:
        primary = sorted(engine.strategy_run.complete_plays().plays, key=lambda item: item.rank)[0]
        render_play_set = RecoveryPlaySetV1(
            run_id=engine.run_id,
            built_at=engine.strategy_run.recovery_plays.built_at,
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
        primary_play_id = primary.play_id
    else:
        render_play_set = RecoveryPlaySetV1(
            run_id=engine.run_id,
            built_at=engine.strategy_run.recovery_plays.built_at,
            provider="fixture",
            plays=[],
        )
        primary_play_id = None
    creative_href = None
    brief_href = None
    if primary_play_id is not None:
        for entry in expected_launch.manifest.files:
            if entry.play_id != primary_play_id:
                continue
            if entry.asset_class == "creative_handoff":
                creative_href = f"launch-pack/{entry.path}"
            elif entry.asset_class == "production_brief":
                brief_href = f"launch-pack/{entry.path}"
    expected_index = render_recovery_room_html(
        engine.enriched_money_map,
        engine.strategy_run.recovery_plays,
        launch_status=expected_launch.status,
        synthetic_demo=True,
        withheld_assets=list(expected_launch.withheld_asset_ids),
        handoff_actions=build_handoff_actions(
            intake=HandoffIntakeConfigV1(),
            creative_handoff_href=creative_href,
            production_brief_href=brief_href,
        ),
        contribution_ledger=engine.ledger,
    ).encode("utf-8")
    expected_top = render_top_play_html(engine.enriched_money_map, render_play_set).encode("utf-8")
    _require_exact_bytes(files["index.html"], expected_index, label="index.html")
    _require_exact_bytes(files["top-play.html"], expected_top, label="top-play.html")
    static_assets = recovery_room_static_assets()
    for path, payload in static_assets.items():
        _require_exact_bytes(files[path], payload, label=path)
    expected_render_manifest = build_recovery_room_manifest(
        engine.run_id, expected_index.decode("utf-8"), static_assets
    )
    _require_exact_bytes(
        files["render-manifest.json"],
        expected_render_manifest.to_canonical_json(),
        label="render-manifest",
    )


def _pdf_page_count(payload: bytes) -> int:
    reader = PdfReader(io.BytesIO(payload))
    return len(reader.pages)


def _validate_live_proof_pngs(files: Mapping[str, bytes]) -> None:
    for path, payload in files.items():
        if not path.startswith("render-proof/") or not path.endswith(".png"):
            continue
        validate_png_bytes(payload)
    for name, expected in NAMED_VIEWPORT_DIMENSIONS.items():
        relative = f"render-proof/{name}"
        if relative not in files:
            raise ValueError(f"missing named proof PNG: {relative}")
        dimensions = png_dimensions(files[relative])
        if dimensions != expected:
            raise ValueError(f"{relative} dimensions {dimensions} != expected {expected}")
    if files.get("render-proof/index.png") != files.get(
        "render-proof/money-map-desktop-1440x900.png"
    ):
        raise ValueError("index.png does not match the live money-map desktop raster")
    if files.get("render-proof/top-play.png") != files.get(
        "render-proof/top-play-desktop-1440x900.png"
    ):
        raise ValueError("top-play.png does not match the live top-play desktop raster")
    for relative in ("render-proof/index.png", "render-proof/top-play.png"):
        if png_dimensions(files[relative]) != DESKTOP_VIEWPORT:
            raise ValueError(f"{relative} dimensions are not the live desktop viewport")
    named_payloads = [files[f"render-proof/{name}"] for name in NAMED_VIEWPORT_PNGS]
    if len(set(named_payloads)) != len(named_payloads):
        raise ValueError("named proof PNGs are not distinct live rasters")
    for name in FOUR_ROOM_PNGS:
        relative = f"render-proof/{name}"
        if relative not in files:
            raise ValueError(f"missing four-room proof PNG: {relative}")
        if png_dimensions(files[relative]) != DESKTOP_VIEWPORT:
            raise ValueError(f"{relative} dimensions are not the live desktop viewport")
    for path, payload in files.items():
        name = path.removeprefix("render-proof/")
        if not name.startswith("print-report-page-") or not name.endswith(".png"):
            continue
        dimensions = png_dimensions(payload)
        if dimensions != PRINT_PAGE_VIEWPORT:
            raise ValueError(f"{path} dimensions {dimensions} != {PRINT_PAGE_VIEWPORT}")


def _validate_print_report_manifest(
    files: Mapping[str, bytes],
    *,
    run_id: str,
    source_set_hash: str,
    play_ids: list[str],
    expected_page_count: int,
) -> bytes:
    proof_pdf = files.get("render-proof/print-report.pdf")
    if proof_pdf is None:
        raise ValueError("render-proof/print-report.pdf is missing")
    if files["print-report.pdf"] != proof_pdf:
        raise ValueError("root print-report.pdf does not match render-proof/print-report.pdf")
    root_pages = _pdf_page_count(files["print-report.pdf"])
    proof_pages = _pdf_page_count(proof_pdf)
    if root_pages != proof_pages:
        raise ValueError("print-report.pdf page counts do not match")
    if root_pages != expected_page_count:
        raise ValueError(
            f"print-report.pdf page count {root_pages} does not equal "
            f"canonical print page count {expected_page_count}"
        )
    assert_us_letter_pdf(files["print-report.pdf"])
    assert_us_letter_pdf(proof_pdf)
    stored = _json(files["render-proof/print-report-manifest.json"])
    page_count = int(stored["page_count"])
    page_artifacts = list(stored.get("page_artifacts") or [])
    if page_count != expected_page_count:
        raise ValueError(
            f"print-report-manifest page_count {page_count} does not equal "
            f"canonical print page count {expected_page_count}"
        )
    expected_names = canonical_print_report_page_artifacts(expected_page_count)
    expected_paths = [f"render-proof/{name}" for name in expected_names]
    actual_manifest_paths = [str(row.get("path", "")) for row in page_artifacts]
    actual_file_paths = sorted(
        path
        for path in files
        if path.startswith("render-proof/") and Path(path).name.startswith("print-report-page-")
    )
    if actual_manifest_paths != expected_paths or actual_file_paths != expected_paths:
        raise ValueError("print-report page artifact coverage is not exact")
    expected_page_pngs = rasterize_print_report_page_pngs(files["print-report.pdf"])
    if len(expected_page_pngs) != expected_page_count:
        raise ValueError(
            "canonical PDF page raster count does not match the canonical print page count"
        )
    raster_hashes: list[str] = []
    for path, expected_png in zip(expected_paths, expected_page_pngs, strict=True):
        if path not in files:
            raise ValueError(f"print-report page artifact is missing: {path}")
        actual_hash = canonical_png_raster_hash(files[path])
        if actual_hash != canonical_png_raster_hash(expected_png):
            raise ValueError(f"{path} does not match the canonical PDF page raster")
        raster_hashes.append(actual_hash)
    if len(set(raster_hashes)) != len(raster_hashes):
        raise ValueError("print-report page rasters contain duplicate page-raster content")
    proof_artifacts: dict[str, bytes] = {"print-report.pdf": files["print-report.pdf"]}
    for name in expected_names:
        proof_artifacts[name] = files[f"render-proof/{name}"]
    contact_sheet = f"render-proof/{PRINT_REPORT_CONTACT_SHEET}"
    if contact_sheet not in files:
        raise ValueError("print-report contact sheet is missing")
    expected_contact_sheet = build_print_contact_sheet(expected_page_pngs)
    if canonical_png_raster_hash(files[contact_sheet]) != canonical_png_raster_hash(
        expected_contact_sheet
    ):
        raise ValueError("contact sheet does not match independently recomputed page rasters")
    proof_artifacts[PRINT_REPORT_CONTACT_SHEET] = files[contact_sheet]
    expected = build_print_report_manifest(
        run_id=run_id,
        source_set_hash=source_set_hash,
        play_ids=play_ids,
        proof_artifacts=proof_artifacts,
        print_review=None,
    )
    _require_exact_bytes(
        files["render-proof/print-report-manifest.json"],
        expected,
        label="print-report-manifest",
    )
    return expected


def _validate_scenario_manifest_fields(
    manifest: ScenarioManifestV1,
    locked: Any,
    engine: Any,
    receipts: Mapping[str, SourceReceiptV1],
    raw: Mapping[str, bytes],
    files: Mapping[str, bytes],
    run_id: str,
) -> None:
    if manifest.run_id != run_id:
        raise ValueError("scenario manifest run_id does not bind to run.json")
    if manifest.mode != "public":
        raise ValueError("scenario manifest mode is not the locked public mode")
    if manifest.business_model != locked.business_model:
        raise ValueError("scenario manifest business_model does not match the locked definition")
    if list(manifest.sources) != list(locked.sources):
        raise ValueError("scenario manifest sources do not match locked HubSpot/Stripe sources")
    expected_versions = {name: receipts[name].connector_schema_version for name in locked.sources}
    if dict(manifest.connector_schema_versions) != expected_versions:
        raise ValueError("scenario manifest connector schema map does not match locked receipts")
    if dict(manifest.fixture_schema_versions) != expected_versions:
        raise ValueError("scenario manifest fixture schema map does not match locked receipts")
    if dict(manifest.fixture_hashes) != packaged_fixture_hashes(locked.fixture_id):
        raise ValueError("scenario manifest fixture hashes do not match locked source files")
    expected_content = {name: sha256_bytes(raw[name]) for name in locked.sources}
    if dict(manifest.content_hashes) != expected_content:
        raise ValueError("scenario manifest content hashes are stale")
    if dict(manifest.source_counts) != {
        name: receipts[name].record_count for name in locked.sources
    }:
        raise ValueError("manifest source counts are untruthful")
    if list(manifest.expected_event_families) != list(locked.expected_event_families):
        raise ValueError("scenario manifest expected families do not match the locked definition")
    if dict(manifest.candidates_by_family) != dict(engine.aggregate_receipt.candidates_by_family):
        raise ValueError("scenario manifest candidates_by_family do not match recomputed events")
    if list(manifest.play_ids) != list(engine.aggregate_receipt.play_ids):
        raise ValueError("scenario manifest play IDs do not match the locked definition")
    if list(manifest.card_ids) != list(engine.aggregate_receipt.card_ids):
        raise ValueError("scenario manifest card IDs do not match the locked definition")
    if list(manifest.named_data_gaps or []) != list(engine.aggregate_receipt.named_data_gaps or []):
        raise ValueError("scenario manifest named data gaps do not match recomputed gaps")
    if dict(manifest.exclusions_by_reason or {}) != dict(
        engine.aggregate_receipt.exclusions_by_reason or {}
    ):
        raise ValueError("scenario manifest exclusion reasons do not match recomputed exclusions")
    if manifest.inherited_proof_contract != locked.inherited_proof_contract:
        raise ValueError("scenario manifest proof contract does not match the locked definition")
    if manifest.definition_sha256 != sha256_bytes(files[SCENARIO_DEFINITION_PATH]):
        raise ValueError("manifest definition digest is stale")
    if manifest.aggregate_receipt_sha256 != sha256_bytes(files[SCENARIO_AGGREGATE_PATH]):
        raise ValueError("manifest aggregate digest is stale")
    if locked.business_model == "ecommerce":
        if manifest.high_value_cart_state != locked.high_value_cart_state:
            raise ValueError("scenario manifest cart state does not match the locked definition")
        if manifest.high_value_cart_hash != locked.high_value_cart_hash:
            raise ValueError("scenario manifest cart hash does not match the locked definition")
        if list(manifest.situation_ids or []) != list(engine.aggregate_receipt.situation_ids or []):
            raise ValueError("scenario manifest situation_ids do not match recomputed situations")


def _validate_html_document(
    html: str,
    dom: _RecoveryRoomDom,
    files: Mapping[str, bytes],
    play_ids: list[str],
    *,
    label: str,
) -> None:
    counts = _id_counts(dom.ids)
    duplicates = sorted(name for name, count in counts.items() if count != 1)
    if duplicates:
        raise ValueError(f"{label} has duplicate ids: {', '.join(duplicates)}")
    for attr, href in dom.links:
        _validate_one_link(href, files, counts, label=label, attr=attr)
    for play_id in play_ids:
        if label == "index.html" and counts.get(play_id, 0) != 1:
            raise ValueError(f"broken HTML link: missing unique play id {play_id}")


def _validate_one_link(
    href: str,
    files: Mapping[str, bytes],
    local_ids: Mapping[str, int],
    *,
    label: str,
    attr: str,
) -> None:
    parsed = urlparse(href)
    if parsed.scheme:
        raise ValueError(f"unsafe HTML {attr} scheme: {href}")
    fragment = unquote(parsed.fragment or "")
    path = unquote(parsed.path or "")
    if not path and href.startswith("#"):
        if not fragment:
            return
        if local_ids.get(fragment, 0) != 1:
            raise ValueError(
                f"broken HTML link: {label} {attr} points at a missing unique fragment: {href}"
            )
        return
    if not path:
        return
    if not _is_canonical_relative(path):
        raise ValueError(f"broken relative link: {href}")
    if path not in files:
        raise ValueError(f"broken relative link: {href}")
    if fragment:
        if path.endswith((".html", ".htm")):
            target_dom = _parse_dom(files[path].decode("utf-8"))
            if _id_counts(target_dom.ids).get(fragment, 0) != 1:
                raise ValueError(
                    f"broken HTML link: {label} {attr} points at a missing unique fragment: {href}"
                )
        else:
            raise ValueError(f"broken HTML link: fragment on non-HTML target: {href}")


def _is_canonical_relative(path: str) -> bool:
    if not path or path.startswith("/") or "\\" in path or path.startswith("./"):
        return False
    parts = path.split("/")
    return all(part not in {"", ".", ".."} for part in parts)


def _validate_pdf_uris(files: Mapping[str, bytes]) -> None:
    for path, payload in files.items():
        if not path.endswith(".pdf"):
            continue
        for uri in _pdf_uris(payload):
            parsed = urlparse(str(uri))
            if parsed.scheme:
                raise ValueError(f"unsafe PDF URI scheme in {path}: {uri}")
            raw_path = unquote(parsed.path or str(uri).split("#", 1)[0])
            fragment = unquote(parsed.fragment or "")
            token = raw_path[1:] if raw_path.startswith("/") else raw_path
            if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", token or ""):
                fragment = token
                raw_path = "index.html"
            if not raw_path and fragment:
                if path.startswith("render-proof/"):
                    raise ValueError(
                        f"PDF {path} collapsed a cross-document target to an internal fragment: {uri}"
                    )
                continue
            candidates: list[str] = []
            bases = [str(Path(path).parent), ".", "render-proof"]
            for base in bases:
                try:
                    if raw_path:
                        candidates.append(_posix_join_norm(base, raw_path))
                    else:
                        candidates.append(path)
                except ValueError:
                    continue
            resolved = next((item for item in candidates if item in files), "")
            if not resolved:
                raise ValueError(f"PDF URI does not resolve in the output tree: {uri}")
            if fragment and resolved.endswith((".html", ".htm")):
                target_dom = _parse_dom(files[resolved].decode("utf-8"))
                if _id_counts(target_dom.ids).get(fragment, 0) != 1:
                    raise ValueError(f"PDF URI fragment does not resolve: {uri}")


def _validate_html_links(html: str, files: Mapping[str, bytes], play_ids: list[str]) -> None:
    _validate_html_document(html, _parse_dom(html), files, play_ids, label="index.html")


def _validate_public_safety(root: Path, files: Mapping[str, bytes]) -> None:
    violations = scan_output_tree(root, include_private=True)
    if violations:
        raise ValueError("public scan: " + "; ".join(violations))
    definition = parse_scenario_definition(files[SCENARIO_DEFINITION_PATH])
    snapshots, _raw, _receipts = load_normalized_scenario_snapshots(
        definition.fixture_id, retrieved_at=definition.clock
    )
    source_ids = derive_raw_source_ids(snapshots)
    from found_money.safety.output_scan import verified_texture_asset

    for path, payload in files.items():
        if verified_texture_asset(path, payload):
            continue
        suffix = Path(path).suffix.lower()
        if suffix == ".png":
            png_hits = scan_png_bytes(path, payload)
            if png_hits:
                raise ValueError("public scan: " + "; ".join(png_hits))
            continue
        if suffix == ".pdf":
            pdf_hits = scan_pdf_bytes(path, payload)
            if pdf_hits:
                raise ValueError("public scan: " + "; ".join(pdf_hits))
            text = _pdf_text(payload)
        else:
            text = payload.decode("utf-8", errors="ignore")
        if contains_recovered_revenue_claim(text):
            raise ValueError(f"{path} contains a recovered-revenue guarantee claim")
        # The minified three.js bundle contains 'data:' as JS string literals
        # (DataTexture helpers). It is a vendored third-party asset, not an
        # emitted payload; every other text path keeps the full scheme scan.
        if str(path).replace("\\", "/").endswith("assets/three.min.js"):
            for noun in _FORBIDDEN_PROPER_NOUNS:
                if noun in text:
                    raise ValueError(f"{path} leaked proper noun {noun}")
            continue
        if _UNSAFE_SCHEME_RE.search(text):
            raise ValueError(f"{path} contains an unsafe URI scheme")
        for noun in _FORBIDDEN_PROPER_NOUNS:
            if noun in text:
                raise ValueError(f"{path} leaked proper noun {noun}")
        for match in _PROVIDER_CUSTOMER_ID_RE.finditer(text):
            raise ValueError(f"{path} leaked source identifier {match.group(0)}")
        for source_id in source_ids:
            if _SAFE_ID_RE.fullmatch(source_id) or _HASH_RE.fullmatch(source_id):
                continue
            if source_id and source_id in text:
                raise ValueError(f"{path} leaked source identifier {source_id}")
