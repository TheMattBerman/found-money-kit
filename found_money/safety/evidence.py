"""Deterministic release safety evidence packet builder."""

from __future__ import annotations

import hashlib
import json
import subprocess
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from found_money.contracts.release import (
    DECLARED_CHECK_NAMES,
    NOT_READY_PACKET_STATUS,
)
from found_money.contracts.safety import (
    NoMutationAssertionV1,
    ReleaseSafetyEvidencePacketV1,
)
from found_money.receipts import _atomic_write_bytes, _validate_relative_under_root
from found_money.safety.allowlist import ALLOWLIST_VERSION, allowlist_hash, summarize_allowlist
from found_money.safety.capability import scan_package_capabilities
from found_money.safety.output_scan import scan_output_tree

ROOT = Path(__file__).resolve().parents[2]


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_text(text: str) -> str:
    return _sha256_bytes(text.encode("utf-8"))


def hash_file_tree(root: Path, *, suffixes: set[str] | None = None) -> str:
    """Deterministic hash of relative path + content for a source/implementation tree."""
    entries: list[tuple[str, str]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(
            part in {".git", ".venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
            for part in path.parts
        ):
            continue
        if suffixes is not None and path.suffix.casefold() not in suffixes:
            continue
        rel = path.relative_to(root).as_posix()
        entries.append((rel, _sha256_bytes(path.read_bytes())))
    payload = json.dumps(entries, separators=(",", ":"), ensure_ascii=False) + "\n"
    return _sha256_text(payload)


def hash_git_file_tree(
    root: Path,
    commit_hash: str,
    *,
    subdir: str | None = None,
    suffixes: set[str] | None = None,
) -> str:
    """Hash tracked bytes at an exact commit using the same path/content envelope."""
    command = ["git", "ls-tree", "-r", "--name-only", commit_hash]
    if subdir:
        command.extend(["--", subdir])
    try:
        listed = subprocess.run(
            command, cwd=root, check=True, capture_output=True, text=True
        ).stdout.splitlines()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError("release safety commit is not available") from exc
    entries: list[tuple[str, str]] = []
    prefix = f"{subdir.rstrip('/')}/" if subdir else ""
    for tracked_path in sorted(listed):
        path = Path(tracked_path)
        if suffixes is not None and path.suffix.casefold() not in suffixes:
            continue
        try:
            payload = subprocess.run(
                ["git", "show", f"{commit_hash}:{tracked_path}"],
                cwd=root,
                check=True,
                capture_output=True,
            ).stdout
        except (OSError, subprocess.CalledProcessError) as exc:
            raise ValueError("release safety commit tree cannot be read") from exc
        relative = tracked_path[len(prefix) :] if prefix else tracked_path
        entries.append((relative, _sha256_bytes(payload)))
    envelope = json.dumps(entries, separators=(",", ":"), ensure_ascii=False) + "\n"
    return _sha256_text(envelope)


def resolve_commit_hash(root: Path = ROOT) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    text = result.stdout.strip().lower()
    return text or None


def declared_check_names(root: Path = ROOT) -> list[str]:
    data = tomllib.loads((root / ".buildloop.toml").read_text(encoding="utf-8"))
    checks = data["checks"]["required"]
    return list(checks)


def build_release_safety_evidence_packet(
    *,
    root: Path = ROOT,
    live_status: str = "fixture-only",
    built_at: datetime | None = None,
    no_mutation_assertion: NoMutationAssertionV1 | None = None,
    linked_live_receipts: Mapping[str, Path] | None = None,
    output_roots: Mapping[str, Path] | None = None,
    commit_hash: str | None = None,
    check_evidence: Mapping[str, str],
) -> ReleaseSafetyEvidencePacketV1:
    """Build an honest fixture-only/live-unverified release safety packet.

    Never marks live-verified and never claims publication or a real-business run.
    """
    if live_status == "live-verified":
        raise ValueError("worker-built packet cannot claim live-verified status")
    linked_live = {
        label: _sha256_bytes(path.read_bytes())
        for label, path in (linked_live_receipts or {}).items()
    }
    if no_mutation_assertion is not None and no_mutation_assertion.live_status != live_status:
        raise ValueError("release live_status must match the attached no-mutation assertion")
    if live_status == "live-unverified" and not linked_live:
        raise ValueError("live-unverified release packets require linked live receipt hashes")
    if live_status == "fixture-only" and linked_live:
        raise ValueError("fixture-only release packets cannot link live receipt hashes")

    capability_violations = scan_package_capabilities(root)
    if capability_violations:
        raise ValueError("capability scan failed:\n" + "\n".join(capability_violations))

    scan_hashes: dict[str, str] = {
        "capability_scan": _sha256_text("\n".join(capability_violations) + "PASS"),
        "allowlist_summary": _sha256_bytes(
            (
                json.dumps(summarize_allowlist(), sort_keys=True, separators=(",", ":")) + "\n"
            ).encode("utf-8")
        ),
    }
    for label, output_root in (output_roots or {}).items():
        violations = scan_output_tree(output_root)
        if violations:
            raise ValueError(f"output scan failed for {label}:\n" + "\n".join(violations))
        scan_hashes[f"output_tree:{label}"] = hash_file_tree(output_root)

    required_checks = set(declared_check_names(root))
    supplied_checks = set(check_evidence)
    if supplied_checks != required_checks:
        missing = sorted(required_checks - supplied_checks)
        extra = sorted(supplied_checks - required_checks)
        raise ValueError(
            f"check evidence must match declared checks; missing={missing}, extra={extra}"
        )
    if any(not isinstance(value, str) or not value.strip() for value in check_evidence.values()):
        raise ValueError("check evidence values must be non-blank exact outputs")
    check_hashes = {name: _sha256_text(check_evidence[name]) for name in sorted(check_evidence)}
    assertion_digest = (
        _sha256_bytes(no_mutation_assertion.to_canonical_json())
        if no_mutation_assertion is not None
        else None
    )
    resolved_commit = commit_hash if commit_hash is not None else resolve_commit_hash(root)
    if resolved_commit is None:
        raise ValueError("an exact commit hash is required for release safety evidence")
    return ReleaseSafetyEvidencePacketV1(
        built_at=built_at or datetime(2026, 8, 10, 18, 0, tzinfo=timezone.utc),
        commit_hash=resolved_commit,
        allowlist_version=ALLOWLIST_VERSION,
        allowlist_hash=allowlist_hash(),
        implementation_hash=hash_file_tree(root / "found_money", suffixes={".py"}),
        source_tree_hash=hash_file_tree(
            root,
            suffixes={".py", ".toml", ".md", ".json", ".yml", ".yaml"},
        ),
        check_evidence={name: check_evidence[name] for name in sorted(check_evidence)},
        check_hashes=check_hashes,
        scan_hashes=scan_hashes,
        live_status=live_status,  # type: ignore[arg-type]
        claims_real_business_run=False,
        claims_publication=False,
        no_mutation_assertion_digest=assertion_digest,
        linked_live_receipt_hashes=linked_live,
    )


def write_release_safety_evidence_packet(
    output_root: Path | str,
    relative_path: str,
    packet: ReleaseSafetyEvidencePacketV1,
) -> Path:
    destination = _validate_relative_under_root(Path(output_root), relative_path)
    _atomic_write_bytes(destination, packet.to_canonical_json())
    return destination


def write_no_mutation_assertion(
    output_root: Path | str,
    relative_path: str,
    assertion: NoMutationAssertionV1,
) -> Path:
    destination = _validate_relative_under_root(Path(output_root), relative_path)
    _atomic_write_bytes(destination, assertion.to_canonical_json())
    return destination


def validate_release_safety_evidence_packet(
    packet: ReleaseSafetyEvidencePacketV1,
    *,
    root: Path = ROOT,
    linked_live_receipts: Mapping[str, Path] | None = None,
    publication_status: str = NOT_READY_PACKET_STATUS,
) -> None:
    """Reject a stale, incomplete, or tree-mismatched release safety packet.

    Strictness follows the publication packet status. A ``not_ready`` packet is a
    historical baseline: its recorded commit must exist, be an ancestor of the
    current head, and validate against what was recorded there; it never needs to
    equal the current head. A ``ready_for_publication_review`` packet keeps the
    exact-head rule and additionally requires observed check evidence for every
    declared check at that exact head.
    """
    required_checks = set(declared_check_names(root))
    if set(packet.check_hashes) != required_checks or set(packet.check_evidence) != required_checks:
        raise ValueError("release safety packet check coverage is incomplete")
    expected_check_hashes = {
        name: _sha256_text(packet.check_evidence[name]) for name in sorted(packet.check_evidence)
    }
    if packet.check_hashes != expected_check_hashes:
        raise ValueError("release safety packet check hashes do not match exact evidence")
    if not {"capability_scan", "allowlist_summary"}.issubset(packet.scan_hashes):
        raise ValueError("release safety packet scan coverage is incomplete")
    if packet.allowlist_version != ALLOWLIST_VERSION or packet.allowlist_hash != allowlist_hash():
        raise ValueError("release safety packet allowlist is stale")
    if publication_status not in {NOT_READY_PACKET_STATUS, "ready_for_publication_review"}:
        raise ValueError("publication packet status must be a known release status")
    observed_live = {
        label: _sha256_bytes(path.read_bytes())
        for label, path in (linked_live_receipts or {}).items()
    }
    if (
        packet.live_status == "live-unverified"
        and observed_live != packet.linked_live_receipt_hashes
    ):
        raise ValueError("linked live receipt hashes do not match retained receipt bytes")
    current_head = resolve_commit_hash(root)
    if current_head is None:
        raise ValueError("current implementation commit is unavailable")
    try:
        subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{packet.commit_hash}^{{commit}}"],
            cwd=root,
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError("release safety packet commit does not exist") from exc
    try:
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", packet.commit_hash, current_head],
            cwd=root,
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError("release safety packet commit is not an ancestor of current HEAD") from exc
    changed = subprocess.run(
        ["git", "diff", "--name-only", f"{packet.commit_hash}..{current_head}"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    if publication_status == "ready_for_publication_review":
        if packet.commit_hash != current_head:
            raise ValueError(
                "ready release safety packet commit must be the current HEAD, not an ancestor"
            )
        if set(changed):
            raise ValueError("ready release safety packet has changes after its exact head")
        if set(packet.check_evidence) != set(DECLARED_CHECK_NAMES):
            raise ValueError("ready release safety packet check coverage is incomplete")
        for name, value in packet.check_evidence.items():
            if not value.strip():
                raise ValueError(f"ready release safety packet evidence for {name} is blank")
        expected_ready_hashes = {
            name: _sha256_text(packet.check_evidence[name])
            for name in sorted(packet.check_evidence)
        }
        if packet.check_hashes != expected_ready_hashes:
            raise ValueError("ready release safety packet check hashes do not match exact evidence")
    expected_implementation = hash_git_file_tree(
        root, packet.commit_hash, subdir="found_money", suffixes={".py"}
    )
    if packet.implementation_hash != expected_implementation:
        raise ValueError("release safety packet implementation hash is stale")
    expected_source = hash_git_file_tree(
        root,
        packet.commit_hash,
        suffixes={".py", ".toml", ".md", ".json", ".yml", ".yaml"},
    )
    if packet.source_tree_hash != expected_source:
        raise ValueError("release safety packet source tree hash is stale")
    if publication_status == "ready_for_publication_review":
        return
    # Historical not_ready baseline: the newest commit that touched this packet is
    # its binding commit. The recorded hashes must reproduce from the git tree at
    # that commit, and that commit must be an ancestor of the current head. This
    # makes the packet a self-consistent historical record instead of a claim
    # about the current working tree.
    try:
        binding_commit = subprocess.run(
            [
                "git",
                "rev-list",
                "-n",
                "1",
                "HEAD",
                "--",
                "tests/fixtures/saas/safety/release-safety-evidence.json",
            ],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError("unable to resolve the release safety packet binding commit") from exc
    if not binding_commit:
        raise ValueError("release safety packet binding commit is unavailable")
    if (
        not subprocess.run(
            ["git", "merge-base", "--is-ancestor", binding_commit, current_head],
            cwd=root,
            check=True,
            capture_output=True,
        ).returncode
        == 0
    ):
        raise ValueError("release safety packet binding commit is not an ancestor of current HEAD")
    expected_binding_implementation = hash_git_file_tree(
        root, binding_commit, subdir="found_money", suffixes={".py"}
    )
    if packet.implementation_hash != expected_binding_implementation:
        raise ValueError("release safety packet implementation hash is stale at binding commit")
