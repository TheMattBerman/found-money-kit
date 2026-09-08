"""FM-018 unified build runner contract tests."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from found_money.contracts.build import parse_canonical_json
import found_money.build as build_module
from found_money.build import BuildConfigError, BuildPathError, build
from found_money.rendering.proof import (
    FOUR_ROOM_PNGS,
    PDF_SIGNATURE,
    PNG_SIGNATURE,
    PRINT_REPORT_CONTACT_SHEET,
    REQUIRED_ARTIFACTS,
    resolve_render_baselines_dir,
)

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs" / "synthetic-saas-thin-slice.json"
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "saas" / "thin-slice"

REQUIRED_BUILD_PATHS = {
    "run.json",
    "money-map.json",
    "recovery-plays.json",
    "index.html",
    "render-manifest.json",
    "assets/recovery-room.css",
    "assets/recovery-room.js",
    "print-report.pdf",
    "render-proof/print-report-manifest.json",
    "launch-pack",
}


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


@pytest.fixture(autouse=True)
def run_from_tmp_path(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)


def _tree(root: Path) -> list[str]:
    if not root.exists():
        return []
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))


def _write_config(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return path


def _file_config(
    tmp_path: Path, *, hubspot: Path | None = None, stripe: Path | None = None
) -> Path:
    return _write_config(
        tmp_path / "source.json",
        {
            "schema_version": "found-money-build-source.v1",
            "mode": "file",
            "credentials": {"credential_mode": "none", "runtime_mode": "test", "scopes": []},
            "sources": {
                "hubspot_snapshot": str(hubspot or FIXTURE_ROOT / "hubspot" / "snapshot.json"),
                "stripe_snapshot": str(stripe or FIXTURE_ROOT / "stripe" / "snapshot.json"),
            },
        },
    )


def test_fixture_build_writes_required_public_tree(fake_proof, tmp_path):
    result = build(output_root="out", source_config=CONFIG)
    paths = set(_tree(result.output_root))
    assert REQUIRED_BUILD_PATHS.issubset(paths)
    assert result.artifact_paths["run.json"].read_bytes() == result.manifest.to_canonical_json()
    assert (result.output_root / "print-report.pdf").read_bytes().startswith(PDF_SIGNATURE)
    print_manifest = json.loads(
        (result.output_root / "render-proof" / "print-report-manifest.json").read_text()
    )
    assert print_manifest["run_id"] == result.run_id
    assert print_manifest["status"] == "ready_for_human_review"
    assert print_manifest["available_play_count"] == 3
    assert print_manifest["page_count"] == 19
    assert print_manifest["contact_sheet"]["path"].endswith(PRINT_REPORT_CONTACT_SHEET)
    assert print_manifest["human_review"]["status"] == "approved"
    assert print_manifest["human_review"]["reviewed_head"]
    assert print_manifest["human_review"]["review_packet_sha256"]

    run = json.loads((result.output_root / "run.json").read_text(encoding="utf-8"))
    parsed_manifest = parse_canonical_json((result.output_root / "run.json").read_bytes())
    assert run["schema_version"] == "found-money-build.v1"
    assert parsed_manifest.run_id == result.run_id
    for artifact in parsed_manifest.artifacts:
        artifact_path = result.output_root / artifact.path
        assert artifact_path.is_file()
        assert build_module.sha256_bytes(artifact_path.read_bytes()) == artifact.sha256
    assert run["stages"]["three_play_strategy"] == "completed"
    assert "three_play_strategy" not in run["deferred_stages"]
    assert run["stages"]["activation_launch_pack"] == "completed"
    assert "activation_launch_pack" not in run["deferred_stages"]
    assert len(json.loads((result.output_root / "recovery-plays.json").read_text())["plays"]) == 3
    launch = json.loads((result.output_root / "launch-pack" / "manifest.json").read_text())
    assert launch["status"] == "approval_ready"
    assert launch["mode"] == "public"
    assert launch["available_play_count"] == 3
    assert launch["required_v1_play_count"] == 3
    assert launch["approval_only"] is True
    assert launch["export_only"] is True
    assert launch["not_activated"] is True
    assert launch["send_performed"] is False
    assert launch["audience_created"] is False
    assert launch["provider_write_performed"] is False
    assert launch["withheld_assets"] == []
    assert not (result.output_root / "launch-pack" / "private").exists()
    assert (result.output_root / "launch-pack" / "public" / "segments.json").is_file()
    launch_hash = next(
        item["sha256"] for item in run["artifacts"] if item["path"] == "launch-pack/manifest.json"
    )
    assert (
        build_module.sha256_bytes(
            (result.output_root / "launch-pack" / "manifest.json").read_bytes()
        )
        == launch_hash
    )

    text_artifacts = [
        path
        for path in result.output_root.rglob("*")
        if path.is_file()
        and path.suffix in {".json", ".html", ".md", ".csv"}
        and "private" not in path.relative_to(result.output_root).parts
    ]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in text_artifacts)
    import re as _re

    assert "cus_synth_001" not in combined
    assert "inv_failed_001" not in combined
    # "@" is forbidden only in PII shape (emails); CSS @-rules are legitimate.
    assert not _re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", combined)
    assert "/Users/" not in combined
    assert "api_key" not in combined
    assert "secret" not in combined


def test_canonical_build_reaches_approved_print_review_control(fake_proof, tmp_path, monkeypatch):
    original = build_module.validate_print_review_packet
    calls: list[str] = []

    def recording_review():
        calls.append("print-review")
        return original()

    monkeypatch.setattr(build_module, "validate_print_review_packet", recording_review)
    result = build(output_root="out", source_config=CONFIG)
    manifest = json.loads(
        (result.output_root / "render-proof" / "print-report-manifest.json").read_text()
    )
    assert calls == ["print-review"]
    assert manifest["human_review"]["status"] == "approved"


def test_absolute_output_root_fails_before_source_read_or_write(tmp_path, monkeypatch):
    absolute_root = tmp_path / "absolute-out"
    source_reads: list[Path] = []

    def fail_source_read(path: Path):
        source_reads.append(path)
        raise AssertionError("source config read before output-root rejection")

    monkeypatch.setattr(build_module, "load_source_config", fail_source_read)
    with pytest.raises(BuildPathError, match="must not be absolute"):
        build(output_root=absolute_root, source_config=CONFIG)
    assert not absolute_root.exists()
    assert source_reads == []


def test_output_root_and_artifact_paths_fail_before_writes(fake_proof, tmp_path, monkeypatch):
    with pytest.raises(BuildPathError):
        build(output_root="../outside", source_config=CONFIG)
    with pytest.raises(BuildPathError):
        build(output_root="out/../absolute-sibling", source_config=CONFIG)

    root = tmp_path / "out"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "launch-pack").symlink_to(outside, target_is_directory=True)
    before_root = _tree(root)
    before_outside = _tree(outside)
    with pytest.raises(BuildPathError):
        build(output_root="out", source_config=CONFIG)
    assert _tree(root) == before_root
    assert _tree(outside) == before_outside

    original_artifact_paths = build_module.FINAL_ARTIFACT_PATHS
    for escaped in (
        "../sibling.json",
        "/tmp/absolute.json",
        "C:\\outside\\absolute.json",
        "sibling/../../escape.json",
    ):
        clean = tmp_path / escaped.replace("/", "_").replace("\\", "_")
        monkeypatch.setattr(
            build_module,
            "FINAL_ARTIFACT_PATHS",
            (*build_module.FINAL_ARTIFACT_PATHS, escaped),
        )
        with pytest.raises(BuildPathError):
            build(output_root=clean.name, source_config=CONFIG)
        assert not clean.exists()
        monkeypatch.setattr(build_module, "FINAL_ARTIFACT_PATHS", original_artifact_paths)

    root_link = tmp_path / "root-link"
    root_link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(BuildPathError):
        build(output_root=root_link.name, source_config=CONFIG)
    assert _tree(outside) == before_outside


def test_credentials_fail_closed_before_source_reads(fake_proof, tmp_path, monkeypatch):
    def fail_read(*_args, **_kwargs):
        raise AssertionError("source read happened before credential rejection")

    monkeypatch.setattr(build_module, "_read_snapshot", fail_read)
    base = {
        "schema_version": "found-money-build-source.v1",
        "mode": "file",
        "sources": {
            "hubspot_snapshot": str(FIXTURE_ROOT / "hubspot" / "snapshot.json"),
            "stripe_snapshot": str(FIXTURE_ROOT / "stripe" / "snapshot.json"),
        },
    }
    invalid_credentials = [
        ("missing", None, "credential declaration is required"),
        (
            "blank",
            {"credential_mode": "", "runtime_mode": "test", "scopes": []},
            "credential declaration is unavailable",
        ),
        (
            "malformed",
            {"credential_mode": "none", "runtime_mode": "test", "scopes": [], "token": "x"},
            "credential declaration is malformed",
        ),
        (
            "over-permissioned",
            {"credential_mode": "none", "runtime_mode": "test", "scopes": ["read"]},
            "over-permissioned",
        ),
        (
            "live-mismatch",
            {"credential_mode": "none", "runtime_mode": "live", "scopes": []},
            "test mode",
        ),
    ]
    for label, credentials, message in invalid_credentials:
        payload = dict(base)
        if credentials is not None:
            payload["credentials"] = credentials
        config = _write_config(tmp_path / f"{label}.json", payload)
        root = tmp_path / f"{label}-out"
        with pytest.raises(BuildConfigError, match=message):
            build(output_root=root.name, source_config=config)
        assert not root.exists()

    secret = "sk_live_DO_NOT_LEAK_123"
    bad_secret = _write_config(
        tmp_path / "secret.json",
        {
            "schema_version": "found-money-build-source.v1",
            "mode": "fixture",
            "fixture": "synthetic-saas-thin-slice",
            "credentials": {"credential_mode": secret, "runtime_mode": "test", "scopes": []},
        },
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "found_money",
            "build",
            "--config",
            str(bad_secret),
            "--output-root",
            str(tmp_path / "secret-out"),
        ],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 2
    assert secret not in completed.stdout
    assert secret not in completed.stderr
    assert not (tmp_path / "secret-out").exists()


def test_public_private_and_fixture_file_modes_are_explicit(fake_proof, tmp_path):
    private_config = _write_config(
        tmp_path / "private.json",
        {
            "schema_version": "found-money-build-source.v1",
            "mode": "private",
            "source_mode": "fixture",
            "credentials": {"credential_mode": "none", "runtime_mode": "test", "scopes": []},
            "fixture": "synthetic-saas-thin-slice",
        },
    )
    private = build(output_root="private-out", source_config=private_config)
    manifest = json.loads((private.output_root / "run.json").read_text())
    assert manifest["mode"] == "private"
    assert manifest["source_config"]["mode"] == "fixture"
    assert manifest["source_config"]["run_mode"] == "private"
    launch = json.loads((private.output_root / "launch-pack" / "manifest.json").read_text())
    assert launch["mode"] == "private"
    assert (private.output_root / "launch-pack" / "private" / "segments").is_dir()

    inconsistent = _write_config(
        tmp_path / "inconsistent.json",
        {
            "schema_version": "found-money-build-source.v1",
            "mode": "private",
            "source_mode": "fixture",
            "run_mode": "public",
            "credentials": {"credential_mode": "none", "runtime_mode": "test", "scopes": []},
            "fixture": "synthetic-saas-thin-slice",
        },
    )
    with pytest.raises(BuildConfigError, match="inconsistent"):
        build(output_root="inconsistent-out", source_config=inconsistent)
    assert not (tmp_path / "inconsistent-out").exists()


def test_public_build_rejects_stale_private_launch_tree_without_writes(fake_proof, tmp_path):
    private_config = _write_config(
        tmp_path / "private-stale.json",
        {
            "schema_version": "found-money-build-source.v1",
            "mode": "private",
            "source_mode": "fixture",
            "credentials": {"credential_mode": "none", "runtime_mode": "test", "scopes": []},
            "fixture": "synthetic-saas-thin-slice",
        },
    )
    private = build(output_root="shared-out", source_config=private_config)
    before = {
        path.relative_to(private.output_root).as_posix(): path.read_bytes()
        for path in private.output_root.rglob("*")
        if path.is_file()
    }
    with pytest.raises(BuildPathError, match="stale"):
        build(output_root="shared-out", source_config=CONFIG)
    after = {
        path.relative_to(private.output_root).as_posix(): path.read_bytes()
        for path in private.output_root.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_repeated_file_builds_are_byte_stable_and_hash_isolated(fake_proof, tmp_path):
    config = _file_config(tmp_path)
    first = build(output_root="first", source_config=config)
    second = build(output_root="second", source_config=config)
    first_files = {
        path.relative_to(first.output_root).as_posix(): path.read_bytes()
        for path in first.output_root.rglob("*")
        if path.is_file()
    }
    second_files = {
        path.relative_to(second.output_root).as_posix(): path.read_bytes()
        for path in second.output_root.rglob("*")
        if path.is_file()
    }
    assert first_files == second_files

    mutated_hubspot = tmp_path / "hubspot-mutated.json"
    mutated_hubspot.write_bytes((FIXTURE_ROOT / "hubspot" / "snapshot.json").read_bytes() + b"\n")
    mutated_config = _file_config(tmp_path / "mutated", hubspot=mutated_hubspot)
    mutated = build(output_root="mutated-out", source_config=mutated_config)

    assert (mutated.output_root / "receipts" / "hubspot.source-receipt.json").read_bytes() != (
        first.output_root / "receipts" / "hubspot.source-receipt.json"
    ).read_bytes()
    assert (mutated.output_root / "receipts" / "stripe.source-receipt.json").read_bytes() == (
        first.output_root / "receipts" / "stripe.source-receipt.json"
    ).read_bytes()
    first_run = json.loads((first.output_root / "run.json").read_text())
    mutated_run = json.loads((mutated.output_root / "run.json").read_text())
    assert mutated_run["run_id"] != first_run["run_id"]
    assert mutated_run["source_set_hash"] != first_run["source_set_hash"]
    assert (mutated.output_root / "money-map.json").read_bytes() != first_files["money-map.json"]
    assert (
        json.loads((mutated.output_root / "money-map.json").read_text())[
            "identified_opportunity_minor"
        ]
        == json.loads((first.output_root / "money-map.json").read_text())[
            "identified_opportunity_minor"
        ]
    )


def test_no_socket_or_mutating_http_capability(fake_proof, tmp_path, monkeypatch):
    def boom(*_args, **_kwargs):
        raise AssertionError("socket connection attempted")

    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket.socket, "connect", boom)
    result = build(output_root="out", source_config=CONFIG)
    assert result.run_id

    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "public_safety", ROOT / "scripts/public_safety.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.scan_python_paths([ROOT / "found_money" / "build.py"]) == []


def test_build_public_safety_rejects_secret_and_identity_payloads():
    with pytest.raises(BuildConfigError):
        build_module.validate_public_artifact_payloads(
            {"run.json": b'{"credential_declaration":"sk_live_DO_NOT_LEAK"}\n'}
        )
    with pytest.raises(BuildConfigError):
        build_module.validate_public_artifact_payloads({"run.json": b'{"secret":"x"}\n'})
    with pytest.raises(BuildConfigError):
        build_module.validate_public_artifact_payloads({"index.html": b"<p>user@example.com</p>"})


def test_cli_runs_from_outside_checkout(fake_proof, tmp_path, monkeypatch):
    from found_money.__main__ import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "found-money",
            "build",
            "--config",
            str(CONFIG),
            "--output-root",
            "out",
        ],
    )
    assert main() == 0
    out = tmp_path / "out"
    for relative in REQUIRED_BUILD_PATHS:
        assert (out / relative).exists(), relative
    run = json.loads((out / "run.json").read_text(encoding="utf-8"))
    assert run["source_config"]["mode"] == "fixture"
    assert "/Users/" not in (out / "run.json").read_text(encoding="utf-8")
