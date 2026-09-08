"""Fail-closed path, state-language, and coordinated rehash attacks for FM-037."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from found_money.contracts.public_proof import (
    parse_public_proof_aggregate,
    parse_public_proof_manifest,
    parse_public_proof_recompute,
)
from found_money.contracts.scenarios import parse_scenario_aggregate
from found_money.public_proof import (
    AGGREGATE_PATH,
    DEMO_PATH,
    MANIFEST_PATH,
    NEWSLETTER_PATH,
    PDF_PATH,
    PROVENANCE_PATH,
    RECOMPUTE_PATH,
    PublicProofConfigError,
    build_public_proof,
    recompute_public_proof,
    validate_public_proof_tree,
)
from found_money.receipts import sha256_bytes

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "configs" / "synthetic-public-proof.json"
_SPAN_VALUE_RE = re.compile(r'(<span data-fm-number="[^"]+">)80300(</span>)')


def _rehash_manifest(root: Path) -> None:
    manifest = parse_public_proof_manifest((root / MANIFEST_PATH).read_bytes())
    files = {item.path: (root / item.path).read_bytes() for item in manifest.artifacts}
    refreshed = [
        item.model_copy(update={"sha256": sha256_bytes(files[item.path])})
        for item in manifest.artifacts
    ]
    (root / MANIFEST_PATH).write_bytes(
        manifest.model_copy(
            update={
                "artifacts": refreshed,
                "aggregate_sha256": sha256_bytes(files[AGGREGATE_PATH]),
                "recompute_sha256": sha256_bytes(files[RECOMPUTE_PATH]),
            }
        ).to_canonical_json()
    )


def test_coordinated_rehash_still_fails_independent_recompute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = build_public_proof(output_root="out", config_path=CONFIG)
    root = result.output_root
    aggregate = parse_public_proof_aggregate((root / AGGREGATE_PATH).read_bytes())
    bumped = aggregate.resolved_customer_count + 9
    agg_bytes = aggregate.model_copy(update={"resolved_customer_count": bumped}).to_canonical_json()
    (root / AGGREGATE_PATH).write_bytes(agg_bytes)
    recompute = parse_public_proof_recompute((root / RECOMPUTE_PATH).read_bytes())
    recompute_bytes = recompute.model_copy(
        update={"aggregate_sha256": sha256_bytes(agg_bytes)}
    ).to_canonical_json()
    (root / RECOMPUTE_PATH).write_bytes(recompute_bytes)
    manifest = parse_public_proof_manifest((root / MANIFEST_PATH).read_bytes())
    refreshed = []
    replacements = {
        AGGREGATE_PATH: sha256_bytes(agg_bytes),
        RECOMPUTE_PATH: sha256_bytes(recompute_bytes),
    }
    for item in manifest.artifacts:
        if item.path in replacements:
            refreshed.append(item.model_copy(update={"sha256": replacements[item.path]}))
        else:
            refreshed.append(item)
    (root / MANIFEST_PATH).write_bytes(
        manifest.model_copy(
            update={
                "artifacts": refreshed,
                "aggregate_sha256": sha256_bytes(agg_bytes),
                "recompute_sha256": sha256_bytes(recompute_bytes),
            }
        ).to_canonical_json()
    )
    with pytest.raises(PublicProofConfigError):
        recompute_public_proof(root)
    with pytest.raises(PublicProofConfigError):
        validate_public_proof_tree(root)


def test_tampered_scenario_aggregate_fails_tree_validation(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = build_public_proof(output_root="out", config_path=CONFIG)
    root = result.output_root
    relative = "scenarios/synthetic-saas-v1/aggregate-receipt.json"
    stored = parse_scenario_aggregate((root / relative).read_bytes())
    tampered = stored.model_copy(
        update={"resolved_customer_count": stored.resolved_customer_count + 4}
    ).to_canonical_json()
    (root / relative).write_bytes(tampered)
    manifest = parse_public_proof_manifest((root / MANIFEST_PATH).read_bytes())
    refreshed = []
    for item in manifest.artifacts:
        if item.path == relative:
            refreshed.append(item.model_copy(update={"sha256": sha256_bytes(tampered)}))
        else:
            refreshed.append(item)
    (root / MANIFEST_PATH).write_bytes(
        manifest.model_copy(update={"artifacts": refreshed}).to_canonical_json()
    )
    with pytest.raises(PublicProofConfigError):
        validate_public_proof_tree(root)


def test_injected_recovered_claim_fails_state_language(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = build_public_proof(output_root="out", config_path=CONFIG)
    root = result.output_root
    newsletter = (root / NEWSLETTER_PATH).read_text(encoding="utf-8")
    poisoned = newsletter + "\nRecovered revenue guaranteed.\n"
    (root / NEWSLETTER_PATH).write_bytes(poisoned.encode("utf-8"))
    manifest = parse_public_proof_manifest((root / MANIFEST_PATH).read_bytes())
    refreshed = []
    for item in manifest.artifacts:
        if item.path == NEWSLETTER_PATH:
            refreshed.append(
                item.model_copy(update={"sha256": sha256_bytes(poisoned.encode("utf-8"))})
            )
        else:
            refreshed.append(item)
    (root / MANIFEST_PATH).write_bytes(
        manifest.model_copy(update={"artifacts": refreshed}).to_canonical_json()
    )
    with pytest.raises(PublicProofConfigError):
        validate_public_proof_tree(root)


def _fm051_customer_text_fragments() -> list[str]:
    """Every CustomerCopyV1 text and offer_recommendation across the canonical plays."""

    from found_money.contracts.campaign import CustomerCopyV1
    from found_money.scenarios import (
        SYNTHETIC_ECOMMERCE_V1,
        SYNTHETIC_SAAS_V1,
        SYNTHETIC_SERVICE_V1,
    )
    from found_money.scenarios.harness import run_scenario_engine

    runs = [
        run_scenario_engine(
            run_id="run_fm051_proof",
            safe_config={"source_mode": "fixture", "run_mode": "public", "fixture": fixture_id},
            fixture_id=fixture_id,
        ).strategy_run.recovery_plays
        for fixture_id in (SYNTHETIC_SAAS_V1, SYNTHETIC_ECOMMERCE_V1, SYNTHETIC_SERVICE_V1)
    ]
    fragments: list[str] = []
    for play_set in runs:
        for play in play_set.plays:
            fragments.append(play.offer_recommendation)
            core = next(rung for rung in play.offer_ladder.rungs if rung.role == "core")
            fragments.append(core.mechanism.text)
            for step in play.email_sequence:
                for copy in (step.subject, step.body, step.cta):
                    assert isinstance(copy, CustomerCopyV1)
                    fragments.append(copy.text)
            for message in play.sms.messages:
                fragments.append(message.text)
    return [fragment for fragment in fragments if len(fragment) >= 16]


def test_play_copy_never_reaches_public_proof_artifacts(tmp_path, monkeypatch):
    """AC-6 (FM-051): the loosening cannot reach a published artifact.

    Public proof renders counts and aggregates today, so this passes on arrival.
    It exists to fail the later issue that starts printing play copy on the
    surface that publishes under Matthew's name.
    """

    monkeypatch.chdir(tmp_path)
    build_public_proof(output_root="out", config_path=CONFIG)
    root = Path("out")
    fragments = _fm051_customer_text_fragments()
    assert fragments, "expected at least one guarded fragment"
    for artifact in sorted(path for path in root.rglob("*") if path.is_file()):
        content = artifact.read_text(encoding="utf-8", errors="ignore")
        for fragment in fragments:
            assert fragment not in content, f"play copy leaked into {artifact}: {fragment[:60]}"


def test_injected_proper_noun_fails_privacy_audit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = build_public_proof(output_root="out", config_path=CONFIG)
    root = result.output_root
    demo = (root / DEMO_PATH).read_text(encoding="utf-8")
    poisoned = demo.replace("</main>", "<p>ExampleClientCo customer Jane Doe</p></main>")
    (root / DEMO_PATH).write_bytes(poisoned.encode("utf-8"))
    manifest = parse_public_proof_manifest((root / MANIFEST_PATH).read_bytes())
    refreshed = []
    for item in manifest.artifacts:
        if item.path == DEMO_PATH:
            refreshed.append(
                item.model_copy(update={"sha256": sha256_bytes(poisoned.encode("utf-8"))})
            )
        else:
            refreshed.append(item)
    (root / MANIFEST_PATH).write_bytes(
        manifest.model_copy(update={"artifacts": refreshed}).to_canonical_json()
    )
    with pytest.raises(PublicProofConfigError):
        validate_public_proof_tree(root)


def test_partial_displayed_number_span_forgery_fails_after_manifest_rehash(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = build_public_proof(output_root="out", config_path=CONFIG)
    root = result.output_root
    demo = (root / DEMO_PATH).read_text(encoding="utf-8")
    poisoned, count = _SPAN_VALUE_RE.subn(r"\g<1>99999\g<2>", demo, count=1)
    assert count == 1
    assert "99999" in poisoned
    assert "80300" in poisoned
    (root / DEMO_PATH).write_bytes(poisoned.encode("utf-8"))
    _rehash_manifest(root)
    with pytest.raises(PublicProofConfigError):
        recompute_public_proof(root)
    with pytest.raises(PublicProofConfigError):
        validate_public_proof_tree(root)


def test_coordinated_displayed_number_recompute_forgery_fails_independent_rebuild(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    result = build_public_proof(output_root="out", config_path=CONFIG)
    root = result.output_root
    honest_aggregate = (root / AGGREGATE_PATH).read_bytes()
    for relative in (DEMO_PATH, NEWSLETTER_PATH, PROVENANCE_PATH):
        text = (root / relative).read_text(encoding="utf-8")
        (root / relative).write_bytes(text.replace("80300", "99999").encode("utf-8"))
    pdf = (root / PDF_PATH).read_bytes()
    (root / PDF_PATH).write_bytes(pdf.replace(b"80300", b"99999"))
    recompute = parse_public_proof_recompute((root / RECOMPUTE_PATH).read_bytes())
    forged_numbers = [
        item.model_copy(update={"value": "99999"}) if item.value == "80300" else item
        for item in recompute.displayed_numbers
    ]
    (root / RECOMPUTE_PATH).write_bytes(
        recompute.model_copy(update={"displayed_numbers": forged_numbers}).to_canonical_json()
    )
    _rehash_manifest(root)
    assert (root / AGGREGATE_PATH).read_bytes() == honest_aggregate
    with pytest.raises(PublicProofConfigError):
        recompute_public_proof(root)
    with pytest.raises(PublicProofConfigError):
        validate_public_proof_tree(root)
