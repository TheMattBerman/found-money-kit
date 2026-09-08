"""FM-037 public-safe demo/newsletter proof contract."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from found_money.contracts.public_proof import (
    CANONICAL_PUBLIC_PROOF_SCENARIOS,
    PublicProofConfigV1,
    parse_public_proof_aggregate,
    parse_public_proof_manifest,
    parse_public_proof_recompute,
)
from found_money.public_proof import (
    AGGREGATE_PATH,
    DEMO_PATH,
    LOG_PATH,
    MANIFEST_PATH,
    NEWSLETTER_PATH,
    PDF_PATH,
    PNG_PATH,
    PROVENANCE_PATH,
    RECOMPUTE_PATH,
    PublicProofConfigError,
    PublicProofPathError,
    build_public_proof,
    load_public_proof_config,
    recompute_public_proof,
    validate_public_proof_tree,
)
from found_money.redaction import assert_public_safe
from found_money.safety import scan_cli_help, scan_output_tree
from found_money.scenarios.source_audit import extract_pdf_semantics, extract_png_semantics

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "configs" / "synthetic-public-proof.json"


def _tree(root: Path) -> list[str]:
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())


def test_documented_config_is_synthetic_and_credential_free():
    parsed = load_public_proof_config(CONFIG)
    assert list(parsed.scenarios) == list(CANONICAL_PUBLIC_PROOF_SCENARIOS)
    assert parsed.evidence_class == "synthetic"
    assert parsed.real_business_aggregate == "absent"
    assert parsed.fm036_aggregate_status == "absent"
    text = CONFIG.read_text(encoding="utf-8").casefold()
    for banned in ("sk_", "rk_", "pat-", "bearer ", "token", "secret"):
        assert banned not in text


def test_config_rejects_live_aggregate_and_extra_fields():
    with pytest.raises(ValidationError):
        PublicProofConfigV1.model_validate(
            {
                "schema_version": "found-money-public-proof-config.v1",
                "scenarios": list(CANONICAL_PUBLIC_PROOF_SCENARIOS),
                "real_business_aggregate": "present",
            }
        )
    with pytest.raises(ValidationError):
        PublicProofConfigV1.model_validate(
            {
                "schema_version": "found-money-public-proof-config.v1",
                "scenarios": list(CANONICAL_PUBLIC_PROOF_SCENARIOS),
                "extra": True,
            }
        )


def test_public_proof_writes_synthetic_tree_and_explicit_absent_fm036(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = build_public_proof(
        output_root="artifacts/public-proof",
        config_path=CONFIG,
    )
    root = result.output_root
    assert (root / MANIFEST_PATH).is_file()
    manifest = parse_public_proof_manifest((root / MANIFEST_PATH).read_bytes())
    aggregate = parse_public_proof_aggregate((root / AGGREGATE_PATH).read_bytes())
    recompute = parse_public_proof_recompute((root / RECOMPUTE_PATH).read_bytes())
    assert manifest.state.real_business_aggregate == "absent"
    assert aggregate.fm036_aggregate_status == "absent"
    assert aggregate.evidence_class == "synthetic"
    assert [item.scenario_id for item in aggregate.scenarios] == list(
        CANONICAL_PUBLIC_PROOF_SCENARIOS
    )
    assert recompute.matched is True
    assert manifest.state.claims_new_visual_baseline is False
    assert manifest.state.sent == "not_performed"
    assert manifest.state.recovered == "not_claimed"
    assert manifest.state.publication == "not_performed"
    for relative in (
        DEMO_PATH,
        NEWSLETTER_PATH,
        PROVENANCE_PATH,
        LOG_PATH,
        PDF_PATH,
        PNG_PATH,
        RECOMPUTE_PATH,
    ):
        assert (root / relative).is_file(), relative
    for fixture in CANONICAL_PUBLIC_PROOF_SCENARIOS:
        assert (root / "scenarios" / fixture / "aggregate-receipt.json").is_file()


def test_independent_recompute_matches_written_aggregate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = build_public_proof(output_root="out", config_path=CONFIG)
    recomputed = recompute_public_proof(result.output_root)
    written = parse_public_proof_aggregate((result.output_root / AGGREGATE_PATH).read_bytes())
    assert recomputed.to_canonical_json() == written.to_canonical_json()
    validate_public_proof_tree(result.output_root)


def test_build_is_byte_deterministic(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    first = build_public_proof(output_root="a", config_path=CONFIG)
    second = build_public_proof(output_root="b", config_path=CONFIG)
    left = {
        path.relative_to(first.output_root).as_posix(): path.read_bytes()
        for path in first.output_root.rglob("*")
        if path.is_file()
    }
    right = {
        path.relative_to(second.output_root).as_posix(): path.read_bytes()
        for path in second.output_root.rglob("*")
        if path.is_file()
    }
    assert left == right


def test_public_surfaces_pass_public_safe_and_tree_scan(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = build_public_proof(output_root="out", config_path=CONFIG)
    root = result.output_root
    assert scan_output_tree(root) == []
    for relative in (DEMO_PATH, NEWSLETTER_PATH, PROVENANCE_PATH, LOG_PATH):
        assert_public_safe(
            {"artifact": relative, "text": (root / relative).read_text(encoding="utf-8")}
        )
    pdf = extract_pdf_semantics(PDF_PATH, (root / PDF_PATH).read_bytes())
    png = extract_png_semantics(PNG_PATH, (root / PNG_PATH).read_bytes())
    assert_public_safe({"artifact": PDF_PATH, "text": pdf.text, "metadata": pdf.metadata})
    assert_public_safe({"artifact": PNG_PATH, "metadata": png.metadata})
    blob = "\n".join(
        (root / name).read_text(encoding="utf-8")
        for name in (DEMO_PATH, NEWSLETTER_PATH, PROVENANCE_PATH, LOG_PATH)
    )
    lowered = blob.casefold()
    assert "identified opportunity is not recovered" in lowered
    assert (
        "fm-036 real-business aggregate: absent" in lowered
        or "live aggregate from the private-run harness: absent" in lowered
    )
    assert "sent: not performed" in lowered
    assert "publication: not performed" in lowered
    for banned in (
        "message sent",
        "newsletter sent",
        "recovered revenue guaranteed",
        "exampleclientco",
    ):
        assert banned not in lowered


def test_refuses_stored_fm036_aggregate_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    fake = tmp_path / "aggregate-proof.json"
    fake.write_text("{}", encoding="utf-8")
    with pytest.raises(PublicProofConfigError, match="absent"):
        build_public_proof(
            output_root="out",
            config_path=CONFIG,
            fm036_aggregate_path=fake,
        )


def test_rejects_traversal_output_root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(PublicProofPathError):
        build_public_proof(output_root="../outside", config_path=CONFIG)


def test_cli_help_includes_public_proof_and_no_send_tokens():
    assert scan_cli_help() == []
    probed = subprocess.run(
        [sys.executable, "-m", "found_money", "public-proof", "--help"],
        check=True,
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    assert "public-proof" in probed.stdout
    assert "send" not in probed.stdout.split()


def test_cli_build_completes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "found_money",
            "public-proof",
            "--config",
            str(CONFIG),
            "--output-root",
            "artifacts/public-proof",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
    )
    assert completed.returncode == 0, completed.stderr
    assert "found-money public-proof: completed" in completed.stdout
