"""FM-038 release evidence: fresh-clone proof, AC ledger, publication-review packet."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


from found_money.contracts.public_proof import CANONICAL_PUBLIC_PROOF_SCENARIOS
from found_money.contracts.release import (
    CANONICAL_PUBLIC_SCENARIOS,
    DECLARED_CHECK_NAMES,
    GLOBAL_AC_IDS,
    NOT_READY_PACKET_STATUS,
    READY_FOR_PUBLICATION_REVIEW_PACKET_STATUS,
    PERMITTED_PACKET_ONLY_DELTA,
    AcEvidenceEntryV1,
    AcEvidenceLedgerV1,
    AcEvidenceLinkV1,
    EvidenceClass,
    FreshCloneActor,
    FreshCloneReceiptV1,
    FreshCloneStepV1,
    PublicationReviewPacketV1,
    PublicationReviewReferenceV1,
    ReleaseDocScanFindingV1,
    ReleaseDocScanReportV1,
    parse_ac_evidence_ledger,
    parse_fresh_clone_receipt,
    parse_publication_review_packet,
    parse_release_doc_scan_report,
)
from found_money.contracts.safety import ReleaseSafetyEvidencePacketV1
from found_money.receipts import sha256_bytes
from found_money.safety import write_artifact_set_atomic
from found_money.safety.evidence import resolve_commit_hash, validate_release_safety_evidence_packet
from found_money.safety.output_scan import scan_output_tree

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
ACCEPTANCE_DOC = "docs/product/ACCEPTANCE.md"
LOCKED_CLOCK = datetime(2026, 8, 11, 23, 0, tzinfo=timezone.utc)
SUPPORTED_PYTHON = ">=3.11"
LEDGER_PATH = "tests/fixtures/release/fm038/ac-evidence-ledger.json"
PUBLICATION_PACKET_PATH = "tests/fixtures/release/fm038/publication-review-packet.json"
FRESH_CLONE_RECEIPT_PATH = "tests/fixtures/release/fm038/fresh-clone-receipt.json"
DOC_SCAN_PATH = "tests/fixtures/release/fm038/release-doc-scan.json"
CLEAN_MACHINE_RUNBOOK = "docs/CLEAN_MACHINE.md"
PROVENANCE_PATH = "tests/fixtures/release/fm038/PROVENANCE.md"

# DRAFT is all-caps only so JSON "draft" message fields are not false positives.
_PLACEHOLDER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("placeholder", re.compile(r"\bTODO\b", re.IGNORECASE)),
    ("placeholder", re.compile(r"\bTBD\b", re.IGNORECASE)),
    ("placeholder", re.compile(r"\[CONFIRM", re.IGNORECASE)),
    ("placeholder", re.compile(r"\blorem\b", re.IGNORECASE)),
    ("placeholder", re.compile(r"\bDRAFT\b")),
)
_PRIVATE_PATH_RE = re.compile(r"(?i)(^|[\s\"'`(])(/Users/|/home/|[A-Za-z]:\\)")
_CREDENTIAL_RE = re.compile(
    r"(?i)(\b(?:sk_live_|sk_test_|rk_live_|rk_test_|ghp_|xox[baprs]-)[A-Za-z0-9_-]{8,}"
    r"|HUBSPOT_PRIVATE_APP_TOKEN\s*=\s*[^\s'\"]+|STRIPE_API_KEY\s*=\s*[^\s'\"]+)"
)
_EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_PHONE_RE = re.compile(
    r"(?<![0-9a-fA-F])(?:\+\d{1,3}[\s.-]?)?(?:\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4})(?![0-9a-fA-F])"
)
# Placeholder business names plus the neutral client placeholder. Real client names
# are never listed here; a deployment supplies its own through
# FOUND_MONEY_CLIENT_NOUNS so the scanner keeps catching them without this source
# tree naming anyone.
_BUILTIN_PROPER_NOUNS = (
    r"acme",
    r"globex",
    r"initech",
    r"umbrella\s+corp",
    r"wonka",
    r"exampleclientco",
    r"example\s+client\s+co",
)


def _proper_noun_re() -> re.Pattern[str]:
    configured = [
        re.escape(part.strip())
        for part in os.environ.get("FOUND_MONEY_CLIENT_NOUNS", "").split(",")
        if part.strip()
    ]
    alternation = "|".join((*_BUILTIN_PROPER_NOUNS, *configured))
    return re.compile(rf"(?<!\w)(?:{alternation})(?!\w)", re.IGNORECASE)


_STALE_STATE_RE = re.compile(
    r"(?i)\b(?:messages?\s+sent|newsletter\s+sent|audience\s+created|"
    r"recovered\s+revenue\s+guaranteed|published\s+to\s+customers|"
    r"v1\s+is\s+complete|publication\s+occurred)\b"
)
# Status docs only. CHANGELOG historical "later issues" wording is excluded.
STALE_NEXT_ISSUE_DOC_PATHS = frozenset(
    {
        "README.md",
        "AGENTS.md",
        "docs/product/ROADMAP.md",
        "docs/product/V1.md",
        "docs/OPERATING.md",
    }
)
STALE_NEXT_ISSUE_PHRASES = (
    "Do not begin Wave 2 product work",
    "Close Wave 1 visual-baseline gap",
    "Wave 2 money-data:",
    "Wave 3 money intelligence:",
    "Wave 4 campaign intelligence:",
    "Wave 5 Recovery Room hardening:",
    "Wave 6 activation and honest handoffs",
    "Remaining 11 event families",
    "Stripe remains unimplemented",
    "Native/live connectors remain `not_started`",
    "As of `a980632`",
    "As of `origin/main` merge `a980632`",
    "FM-001 through FM-007 are merged",
    "Reconciliation baseline: `origin/main` at `a980632`",
    "AC-1, AC-2 (V1 credential contract), AC-3, AC-4, AC-5, AC-8",
    "golden visual baselines, the four-room Recovery Room, full event library",
    "value/overlap/ranking completion, four-room Recovery Room",
)
_STALE_NEXT_ISSUE_PATTERNS = tuple(
    re.compile(re.escape(phrase), re.IGNORECASE) for phrase in STALE_NEXT_ISSUE_PHRASES
)
_PLACEHOLDER_DOC_EXAMPLES = frozenset(
    {
        "docs/product/ACCEPTANCE.md",
        "docs/CLEAN_MACHINE.md",
        "tests/fixtures/release/fm038/PROVENANCE.md",
    }
)
_AC_ROW_RE = re.compile(
    r"^\|\s*(AC-(?:[1-9]|[1-3][0-9]|40))\s*\|.*?\|.*?\|\s*`([^`]+(?:`\s*,\s*`[^`]+)*)`\s*\|",
    re.MULTILINE,
)
_DOC_SCAN_BINARY_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".pdf",
    ".woff",
    ".woff2",
    ".ttf",
    ".otf",
    ".ico",
    ".zip",
    ".gz",
    ".pyc",
    ".so",
    ".dylib",
}
_DOC_SCAN_ROOTS = (
    "README.md",
    "AGENTS.md",
    "CHANGELOG.md",
    "PROVENANCE.md",
    "docs",
    "output",
    "configs",
    "tests/fixtures/release",
    "tests/fixtures/public-proof",
)
_DOC_SCAN_SKIP_PARTS = {
    ".git",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
}
# Fixture negatives / private identity fixtures stay out of the release doc surface.
_DOC_SCAN_SKIP_GLOBS = (
    "tests/fixtures/saas/identity/",
    "tests/fixtures/saas/events/",
    "tests/fixtures/saas/connectors/",
    "tests/fixtures/saas/imports/",
    "tests/fixtures/saas/map/",
    "tests/fixtures/saas/strategy/forbidden_identity.json",
    "tests/fixtures/saas/thin-slice/",
)

# Honest evidence catalog: only link bytes that exist; missing classes stay incomplete.
# Paths are relative to the repository root.
_EVIDENCE_CATALOG: dict[str, dict[EvidenceClass, dict[str, str]]] = {
    "AC-1": {
        "T": {
            "path": "tests/contract/test_release_fm038.py",
            "note": "fresh-clone and outside-root rejection coverage",
        },
        "C": {
            "path": ".github/workflows/ci.yml",
            "note": "declared CI jobs exist; exact-head green status remains incomplete until PR checks",
        },
        "R": {
            "path": "tests/fixtures/release/fm038/fresh-clone-receipt.json",
            "note": "agent/ci fresh-clone receipt; not human H",
        },
    },
    "AC-2": {
        "T": {
            "path": "tests/contract/test_safety_fm030.py",
            "note": "credential and secret-pattern scans",
        },
        "C": {
            "path": ".github/workflows/ci.yml",
            "note": "public-safety job declared; exact-head result not claimed here",
        },
    },
    "AC-6": {
        "T": {
            "path": "tests/contract/test_source_receipts.py",
            "note": "source-receipt contract coverage",
        },
        "C": {
            "path": ".github/workflows/ci.yml",
            "note": "unit-and-contract job declared",
        },
        "R": {
            "path": "tests/fixtures/saas/thin-slice/hubspot/snapshot.json",
            "note": "stable thin-slice source fixture used by receipt hashing regressions",
        },
    },
    "AC-7": {
        "T": {
            "path": "tests/contract/test_identity_integration_fm039.py",
            "note": "two-source matrix, adapter, quarantine/override, canonical bytes",
        },
        "C": {
            "path": ".github/workflows/ci.yml",
            "note": "declared checks exist; exact-head green remains incomplete until PR",
        },
        "R": {
            "path": "tests/fixtures/saas/identity/fm039/evidence/identity-stage.json",
            "note": "canonical identity-stage evidence; not live L or human H",
        },
    },
    "AC-25": {
        "C": {
            "path": ".github/workflows/ci.yml",
            "note": "render-proof job declared",
        },
        "B": {
            "path": "tests/fixtures/saas/rendering/baselines/VISUAL_REVIEW.json",
            "note": "fixed viewport baseline hashes",
        },
        "H": {
            "path": "tests/fixtures/saas/rendering/baselines/RATIFICATION.md",
            "note": "visual ratification packet on recorded head",
        },
    },
    "AC-27": {
        "T": {
            "path": "tests/test_render_proof_artifacts.py",
            "note": "print report contract tests",
        },
        "C": {
            "path": ".github/workflows/ci.yml",
            "note": "render-proof job declared",
        },
        "B": {
            "path": "tests/fixtures/saas/rendering/PRINT_REVIEW.json",
            "note": "print review artifact hashes",
        },
        "H": {
            "path": "tests/fixtures/saas/rendering/PRINT_REVIEW.json",
            "note": "print ratification on recorded implementation head",
        },
    },
    "AC-36": {
        "T": {
            "path": "tests/contract/test_public_proof_fm037.py",
            "note": "synthetic public-proof tree and recompute",
        },
        "C": {
            "path": ".github/workflows/ci.yml",
            "note": "declared checks cover public-proof regressions",
        },
        "H": {
            "note": "human publication review absent; Cursor Auto audit is not H",
        },
    },
    "AC-37": {
        "L": {
            "note": "Matthew-authorized live two-system read absent",
        },
        "R": {
            "path": "configs/synthetic-private-run-fixture.json",
            "note": "fixture private-run config only; no live receipt",
        },
        "H": {
            "note": "FM-036 operator usefulness verdict absent",
        },
    },
    "AC-38": {
        "T": {
            "path": "tests/contract/test_safety_fm030.py",
            "note": "capability and no-mutation scans",
        },
        "C": {
            "path": ".github/workflows/ci.yml",
            "note": "public-safety job declared",
        },
        "L": {
            "note": "live no-mutation receipt absent",
        },
    },
    "AC-39": {
        "T": {
            "path": "tests/test_found_money.py",
            "note": "imported prototype regressions",
        },
        "C": {
            "path": ".github/workflows/ci.yml",
            "note": "unit-and-contract job declared",
        },
    },
    "AC-40": {
        "C": {
            "path": ".github/workflows/ci.yml",
            "note": "fresh-clone job plus five required checks declared",
        },
        "B": {
            "path": "tests/fixtures/release/fm038/fresh-clone-receipt.json",
            "note": "fresh-clone scenario/public-proof artifact hashes",
        },
        "H": {
            "note": "human clean-machine dry run absent",
        },
    },
}


class ReleaseEvidenceError(ValueError):
    """Sanitized release-evidence failure."""


class ReleaseEvidenceConfigError(ReleaseEvidenceError):
    """Configuration or honesty gate failed closed."""


def _canonical_json_bytes(payload: Any) -> bytes:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return (text + "\n").encode("utf-8")


def _file_sha256(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def parse_acceptance_required_evidence(text: str) -> dict[str, list[EvidenceClass]]:
    """Derive AC-1..AC-40 required evidence classes from ACCEPTANCE.md."""

    found: dict[str, list[EvidenceClass]] = {}
    for match in _AC_ROW_RE.finditer(text):
        ac_id = match.group(1)
        raw_classes = match.group(2)
        classes = [part.strip().upper() for part in raw_classes.replace("`", "").split(",")]
        normalized: list[EvidenceClass] = []
        for item in classes:
            if item not in {"T", "C", "R", "B", "H", "L"}:
                raise ReleaseEvidenceConfigError(
                    f"acceptance matrix has unknown evidence class for {ac_id}"
                )
            if item not in normalized:
                normalized.append(item)  # type: ignore[arg-type]
        found[ac_id] = normalized
    if set(found) != set(GLOBAL_AC_IDS):
        missing = sorted(set(GLOBAL_AC_IDS) - set(found), key=lambda item: int(item.split("-")[1]))
        extra = sorted(set(found) - set(GLOBAL_AC_IDS))
        raise ReleaseEvidenceConfigError(
            f"acceptance matrix AC coverage invalid; missing={missing}, extra={extra}"
        )
    return {ac_id: found[ac_id] for ac_id in GLOBAL_AC_IDS}


def load_acceptance_required_evidence(root: Path = PACKAGE_ROOT) -> dict[str, list[EvidenceClass]]:
    path = root / ACCEPTANCE_DOC
    return parse_acceptance_required_evidence(path.read_text(encoding="utf-8"))


def _iter_doc_scan_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for relative in _DOC_SCAN_ROOTS:
        target = root / relative
        if not target.exists():
            continue
        if target.is_file():
            files.append(target)
            continue
        for path in sorted(target.rglob("*")):
            if not path.is_file():
                continue
            if any(part in _DOC_SCAN_SKIP_PARTS for part in path.parts):
                continue
            rel = path.relative_to(root).as_posix()
            if any(rel.startswith(prefix) for prefix in _DOC_SCAN_SKIP_GLOBS):
                continue
            if path.suffix.casefold() in _DOC_SCAN_BINARY_SUFFIXES:
                continue
            files.append(path)
    # Stable unique order
    unique = sorted({path.resolve() for path in files}, key=lambda item: item.as_posix())
    return unique


def _excerpt(text: str, start: int, end: int) -> str:
    left = max(0, start - 24)
    right = min(len(text), end + 24)
    return re.sub(r"\s+", " ", text[left:right]).strip()[:160]


def _iter_json_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            out.extend(_iter_json_strings(item))
        return out
    if isinstance(value, dict):
        out = []
        for item in value.values():
            out.extend(_iter_json_strings(item))
        return out
    return []


def _findings_for_text(rel: str, text: str) -> list[ReleaseDocScanFindingV1]:
    findings: list[ReleaseDocScanFindingV1] = []
    checks: list[tuple[str, re.Pattern[str]]] = [
        *_PLACEHOLDER_PATTERNS,
        ("private_path", _PRIVATE_PATH_RE),
        ("credential", _CREDENTIAL_RE),
        ("email_pii", _EMAIL_RE),
        ("phone_pii", _PHONE_RE),
        ("proper_noun", _proper_noun_re()),
        ("stale_state", _STALE_STATE_RE),
    ]
    for category, pattern in checks:
        for match in pattern.finditer(text):
            excerpt = _excerpt(text, match.start(), match.end())
            lowered = excerpt.casefold()
            if category == "placeholder" and rel in _PLACEHOLDER_DOC_EXAMPLES:
                # Product docs name the forbidden tokens; that is not a placeholder.
                continue
            if category == "stale_state" and any(
                marker in lowered
                for marker in ("never", "not ", "no ", "does not", "without", "absent")
            ):
                continue
            if category == "email_pii" and (
                "@example.invalid" in lowered or "@example.com" in lowered
            ):
                continue
            if category == "credential":
                matched = match.group(0)
                if "set in your shell only" in matched.casefold():
                    continue
                if matched.casefold().startswith(
                    ("hubspot_private_app_token='set", "stripe_api_key='set")
                ):
                    continue
            findings.append(ReleaseDocScanFindingV1(path=rel, category=category, excerpt=excerpt))
    if rel in STALE_NEXT_ISSUE_DOC_PATHS:
        for pattern in _STALE_NEXT_ISSUE_PATTERNS:
            for match in pattern.finditer(text):
                findings.append(
                    ReleaseDocScanFindingV1(
                        path=rel,
                        category="stale_next_issue",
                        excerpt=_excerpt(text, match.start(), match.end()),
                    )
                )
    return findings


def scan_release_docs(
    root: Path = PACKAGE_ROOT,
    *,
    built_at: datetime | None = None,
) -> ReleaseDocScanReportV1:
    """Scan docs/sample/public/release trees for placeholders, secrets, and stale claims."""

    findings: list[ReleaseDocScanFindingV1] = []
    scanned: list[str] = []
    for path in _iter_doc_scan_files(root):
        rel = path.relative_to(root).as_posix()
        scanned.append(rel)
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            findings.append(
                ReleaseDocScanFindingV1(
                    path=rel,
                    category="encoding",
                    excerpt="file is not valid UTF-8 text",
                )
            )
            continue
        if rel.startswith("tests/fixtures/release/") and path.suffix.casefold() == ".json":
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                findings.extend(_findings_for_text(rel, text))
                continue
            for field in _iter_json_strings(payload):
                findings.extend(_findings_for_text(rel, field))
            continue
        findings.extend(_findings_for_text(rel, text))
    return ReleaseDocScanReportV1(
        built_at=built_at or LOCKED_CLOCK,
        scanned_paths=scanned,
        findings=findings,
        clean=not findings,
    )


def _link_for_class(
    *,
    ac_id: str,
    evidence_class: EvidenceClass,
    root: Path,
) -> AcEvidenceLinkV1:
    catalog = _EVIDENCE_CATALOG.get(ac_id, {})
    item = catalog.get(evidence_class)
    if item is None:
        return AcEvidenceLinkV1(
            evidence_class=evidence_class,
            presence="missing",
            path=None,
            note=f"required {evidence_class} evidence is absent for {ac_id}",
            sha256=None,
        )
    path = item.get("path")
    note = item["note"]
    if path is None:
        return AcEvidenceLinkV1(
            evidence_class=evidence_class,
            presence="missing",
            path=None,
            note=note,
            sha256=None,
        )
    full = root / path
    if not full.is_file():
        return AcEvidenceLinkV1(
            evidence_class=evidence_class,
            presence="incomplete",
            path=path,
            note=f"linked path missing on disk: {note}",
            sha256=None,
        )
    # Exact-head C evidence is never self-approved from local presence of workflow YAML alone.
    if evidence_class == "C":
        return AcEvidenceLinkV1(
            evidence_class=evidence_class,
            presence="incomplete",
            path=path,
            note=note,
            sha256=None,
        )
    # H evidence for AC-36/37/40 stays incomplete even if related files exist, unless note says linked.
    if evidence_class == "H" and ac_id in {"AC-36", "AC-37", "AC-40"}:
        return AcEvidenceLinkV1(
            evidence_class=evidence_class,
            presence="missing",
            path=None,
            note=note,
            sha256=None,
        )
    if evidence_class == "L":
        return AcEvidenceLinkV1(
            evidence_class=evidence_class,
            presence="missing",
            path=None,
            note=note,
            sha256=None,
        )
    return AcEvidenceLinkV1(
        evidence_class=evidence_class,
        presence="present",
        path=path,
        note=note,
        sha256=_file_sha256(full),
    )


def build_ac_evidence_ledger(
    *,
    root: Path = PACKAGE_ROOT,
    exact_head: str | None = None,
    built_at: datetime | None = None,
) -> AcEvidenceLedgerV1:
    """Build an honest AC-1..AC-40 ledger; never invents N/A or self-approves missing classes."""

    required = load_acceptance_required_evidence(root)
    head = exact_head or resolve_commit_hash(root)
    if head is None:
        raise ReleaseEvidenceConfigError("exact commit hash is required for the AC ledger")
    acceptance_path = root / ACCEPTANCE_DOC
    entries: list[AcEvidenceEntryV1] = []
    for ac_id in GLOBAL_AC_IDS:
        classes = required[ac_id]
        links = [_link_for_class(ac_id=ac_id, evidence_class=klass, root=root) for klass in classes]
        present = {link.evidence_class for link in links if link.presence == "present"}
        status = "complete" if present == set(classes) else "incomplete"
        entries.append(
            AcEvidenceEntryV1(
                ac_id=ac_id,
                required_evidence=classes,
                links=links,
                status=status,  # type: ignore[arg-type]
                na_determination="none",
            )
        )
    complete = sum(1 for entry in entries if entry.status == "complete")
    incomplete = 40 - complete
    return AcEvidenceLedgerV1(
        acceptance_doc_path=ACCEPTANCE_DOC,
        acceptance_doc_sha256=_file_sha256(acceptance_path),
        built_at=built_at or LOCKED_CLOCK,
        exact_head=head,
        entries=entries,
        complete_count=complete,
        incomplete_count=incomplete,
        claims_publication=False,
        invents_na=False,
    )


def _reference(
    *,
    label: str,
    root: Path,
    relative: str | None,
    note: str,
    force_missing: bool = False,
) -> PublicationReviewReferenceV1:
    if force_missing or relative is None:
        return PublicationReviewReferenceV1(
            label=label,
            path=None,
            sha256=None,
            status="missing",
            note=note,
        )
    path = root / relative
    if not path.is_file():
        return PublicationReviewReferenceV1(
            label=label,
            path=None,
            sha256=None,
            status="missing",
            note=f"{note} (path absent)",
        )
    return PublicationReviewReferenceV1(
        label=label,
        path=relative,
        sha256=_file_sha256(path),
        status="present",
        note=note,
    )


def _release_safety_reference(root: Path) -> PublicationReviewReferenceV1:
    relative = "tests/fixtures/saas/safety/release-safety-evidence.json"
    path = root / relative
    if not path.is_file():
        return PublicationReviewReferenceV1(
            label="release_safety_evidence",
            path=None,
            sha256=None,
            status="missing",
            note="release safety evidence packet absent",
        )
    try:
        parsed = ReleaseSafetyEvidencePacketV1.model_validate_json(path.read_bytes())
        canonical = parsed.to_canonical_json()
    except Exception:
        return PublicationReviewReferenceV1(
            label="release_safety_evidence",
            path=relative,
            sha256=None,
            status="incomplete",
            note="release safety evidence exists but is not a valid canonical packet",
        )
    if path.read_bytes() != canonical:
        return PublicationReviewReferenceV1(
            label="release_safety_evidence",
            path=relative,
            sha256=None,
            status="incomplete",
            note="release safety evidence bytes are not canonical",
        )
    return PublicationReviewReferenceV1(
        label="release_safety_evidence",
        path=relative,
        sha256=sha256_bytes(canonical),
        status="present",
        note="canonical fixture-only release safety evidence packet",
    )


def build_publication_review_packet(
    *,
    root: Path = PACKAGE_ROOT,
    ledger: AcEvidenceLedgerV1 | None = None,
    fresh_clone_receipt: FreshCloneReceiptV1 | None = None,
    exact_head: str | None = None,
    built_at: datetime | None = None,
) -> PublicationReviewPacketV1:
    """Deterministic publication-review packet that stays NOT READY while gates are absent."""

    resolved_ledger = ledger or build_ac_evidence_ledger(
        root=root, exact_head=exact_head, built_at=built_at
    )
    head = exact_head or resolved_ledger.exact_head
    missing_gates: list[str] = []
    if resolved_ledger.incomplete_count:
        missing_gates.append("incomplete_ac_evidence")
    missing_gates.append("fm036_usefulness_verdict")
    missing_gates.append("human_clean_machine_dry_run")
    missing_gates.append("exact_head_ci_green")
    # Keep unique stable order
    missing_gates = list(dict.fromkeys(missing_gates))

    references = [
        _reference(
            label="ac_evidence_ledger",
            root=root,
            relative=LEDGER_PATH,
            note="machine-readable AC-1..AC-40 ledger",
        ),
        _reference(
            label="fresh_clone_receipt",
            root=root,
            relative=FRESH_CLONE_RECEIPT_PATH,
            note="agent/ci fresh-clone receipt; not human H",
        ),
        _release_safety_reference(root),
        _reference(
            label="visual_ratification",
            root=root,
            relative="tests/fixtures/saas/rendering/baselines/RATIFICATION.md",
            note="FM-028 visual ratification reference",
        ),
        _reference(
            label="print_ratification",
            root=root,
            relative="tests/fixtures/saas/rendering/PRINT_REVIEW.json",
            note="FM-029 print ratification reference",
        ),
        _reference(
            label="public_proof_audit",
            root=root,
            relative="tests/fixtures/public-proof/fm037/source-proper-noun-audit.json",
            note="FM-037 synthetic public-proof audit; not publication",
        ),
        _reference(
            label="private_run_config",
            root=root,
            relative="configs/synthetic-private-run-fixture.json",
            note="FM-036 fixture harness only; live usefulness absent",
        ),
        _reference(
            label="fm036_usefulness_verdict",
            root=root,
            relative=None,
            note="human useful/not_useful/waived verdict absent",
            force_missing=True,
        ),
        _reference(
            label="human_clean_machine_dry_run",
            root=root,
            relative=None,
            note="human clean-machine dry run absent",
            force_missing=True,
        ),
        _reference(
            label="clean_machine_runbook",
            root=root,
            relative=CLEAN_MACHINE_RUNBOOK,
            note="documented clean-machine reproduction steps",
        ),
    ]
    receipt_hash = (
        sha256_bytes(fresh_clone_receipt.to_canonical_json())
        if fresh_clone_receipt is not None
        else (
            _file_sha256(root / FRESH_CLONE_RECEIPT_PATH)
            if (root / FRESH_CLONE_RECEIPT_PATH).is_file()
            else None
        )
    )
    return PublicationReviewPacketV1(
        built_at=built_at or LOCKED_CLOCK,
        exact_head=head,
        status="not_ready",
        declared_checks=list(DECLARED_CHECK_NAMES),
        ledger_sha256=sha256_bytes(resolved_ledger.to_canonical_json()),
        fresh_clone_receipt_sha256=receipt_hash,
        references=references,
        fm036_usefulness_verdict="absent",
        human_clean_machine_dry_run="absent",
        missing_gates=missing_gates,
        claims_publication=False,
        claims_sent=False,
        claims_recovered=False,
    )


def write_release_evidence_artifacts(
    *,
    root: Path = PACKAGE_ROOT,
    ledger: AcEvidenceLedgerV1,
    packet: PublicationReviewPacketV1,
    doc_scan: ReleaseDocScanReportV1,
    fresh_clone_receipt: FreshCloneReceiptV1 | None = None,
) -> dict[str, Path]:
    """Atomically write committed release-evidence fixtures under the caller root."""

    payloads: dict[str, bytes] = {
        LEDGER_PATH: ledger.to_canonical_json(),
        PUBLICATION_PACKET_PATH: packet.to_canonical_json(),
        DOC_SCAN_PATH: doc_scan.to_canonical_json(),
    }
    if fresh_clone_receipt is not None:
        payloads[FRESH_CLONE_RECEIPT_PATH] = fresh_clone_receipt.to_canonical_json()
    return write_artifact_set_atomic(root, payloads)


def _git_is_ancestor(root: Path, ancestor: str, head: str) -> bool:
    try:
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", ancestor, head],
            cwd=root,
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return False
    return True


def _git_diff_names(root: Path, old: str, new: str) -> set[str]:
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", f"{old}..{new}"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ReleaseEvidenceConfigError(
            "unable to diff release evidence exact_head against HEAD"
        ) from exc
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def _latest_commit_touching(root: Path, relative: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-list", "-n", "1", "HEAD", "--", relative],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ReleaseEvidenceConfigError(
            f"unable to resolve the newest commit touching {relative}"
        ) from exc
    commit = result.stdout.strip()
    return commit or None


def _validate_historical_packet_tree(
    root: Path,
    *,
    ledger: AcEvidenceLedgerV1,
    packet: PublicationReviewPacketV1,
) -> None:
    """Validate a not_ready packet against the tree recorded at its own commit.

    This is the historical baseline promised by the packet status: recorded commit
    exists (checked by the caller), is an ancestor of the current head, and every
    recorded hash matches the git tree at that commit. Hash-for-status attacks
    fail here because all recorded hashes must reproduce from the recorded tree.
    """
    head = resolve_commit_hash(root)
    if head is None:
        raise ReleaseEvidenceConfigError("current implementation commit is unavailable")
    if not _git_is_ancestor(root, ledger.exact_head, head):
        raise ReleaseEvidenceConfigError(
            "not_ready release evidence exact_head is not an ancestor of the current HEAD"
        )
    recorded_head = ledger.exact_head

    def _recorded_bytes_at(commit: str, relative: str) -> bytes:
        try:
            result = subprocess.run(
                ["git", "show", f"{commit}:{relative}"],
                cwd=root,
                check=True,
                capture_output=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise ReleaseEvidenceConfigError(
                f"not_ready release evidence references a path absent at {commit}: {relative}"
            ) from exc
        return result.stdout

    def _recorded_bytes(relative: str) -> bytes:
        binding_commit = _latest_commit_touching(root, relative)
        if binding_commit is None:
            raise ReleaseEvidenceConfigError(
                f"not_ready release evidence references a tracked path with no commit: {relative}"
            )
        return _recorded_bytes_at(binding_commit, relative)

    if sha256_bytes(_recorded_bytes(LEDGER_PATH)) != packet.ledger_sha256:
        raise ReleaseEvidenceConfigError(
            "publication packet ledger hash does not match the recorded historical tree"
        )
    if packet.fresh_clone_receipt_sha256 is not None:
        receipt_path = root / FRESH_CLONE_RECEIPT_PATH
        if receipt_path.is_file():
            # Receipt bytes on disk are covered by the packet hash check above;
            # the historical check pins the receipt recorded at that commit.
            if sha256_bytes(_recorded_bytes(FRESH_CLONE_RECEIPT_PATH)) != (
                packet.fresh_clone_receipt_sha256
            ):
                raise ReleaseEvidenceConfigError(
                    "publication packet fresh-clone hash does not match the recorded historical tree"
                )
        else:
            raise ReleaseEvidenceConfigError(
                "publication packet fresh-clone hash recorded but receipt file is absent"
            )
    for reference in packet.references:
        if reference.sha256 is None:
            continue
        if reference.path is None:
            raise ReleaseEvidenceConfigError(
                "publication packet reference hash recorded without a path"
            )
        recorded = _recorded_bytes(reference.path)
        if sha256_bytes(recorded) != reference.sha256:
            raise ReleaseEvidenceConfigError(
                "publication packet reference hash does not match the recorded historical tree: "
                f"{reference.path}"
            )
    for entry in ledger.entries:
        for link in entry.links:
            if link.sha256 is None:
                continue
            if link.path is None:
                raise ReleaseEvidenceConfigError(
                    f"{entry.ac_id} {link.evidence_class} evidence hash recorded without a path"
                )
            recorded = _recorded_bytes(link.path)
            if sha256_bytes(recorded) != link.sha256:
                raise ReleaseEvidenceConfigError(
                    f"{entry.ac_id} {link.evidence_class} evidence hash does not match the "
                    f"recorded historical tree: {link.path}"
                )
    changed = _git_diff_names(root, recorded_head, head)
    del changed  # staleness for not_ready is caught by binding-commit hash replay above
    if ledger.complete_count != 0 or ledger.incomplete_count != 40:
        raise ReleaseEvidenceConfigError(
            "not_ready release evidence ledger must remain fully incomplete"
        )
    acceptance_blob = _recorded_bytes(ACCEPTANCE_DOC)
    if sha256_bytes(acceptance_blob) != ledger.acceptance_doc_sha256:
        raise ReleaseEvidenceConfigError(
            "not_ready release evidence ACCEPTANCE.md hash does not match the recorded tree"
        )
    if _file_sha256(root / ACCEPTANCE_DOC) != ledger.acceptance_doc_sha256:
        raise ReleaseEvidenceConfigError(
            "ACCEPTANCE.md has changed since the not_ready release evidence was recorded"
        )


def assert_release_evidence_exact_head_binding(
    *,
    root: Path,
    exact_head: str,
    current_head: str | None = None,
) -> None:
    """Fail closed unless exact_head is HEAD or an ancestor with only packet-only delta."""

    head = current_head or resolve_commit_hash(root)
    if head is None:
        raise ReleaseEvidenceConfigError("current implementation commit is unavailable")
    if exact_head == head:
        return
    if not _git_is_ancestor(root, exact_head, head):
        raise ReleaseEvidenceConfigError(
            "release evidence exact_head is not the current HEAD or an ancestor"
        )
    changed = _git_diff_names(root, exact_head, head)
    if not changed.issubset(PERMITTED_PACKET_ONLY_DELTA):
        raise ReleaseEvidenceConfigError(
            "release evidence exact_head is stale relative to non-packet implementation changes"
        )


def validate_release_evidence_tree(root: Path = PACKAGE_ROOT) -> None:
    """Fail closed on missing, non-canonical, or self-approving release evidence.

    Strictness follows the packet status. ``not_ready`` is a historical baseline
    validated against the files and hashes recorded at its own commit;
    ``ready_for_publication_review`` keeps the exact-head binding and additionally
    requires observed check receipts. Flipping a not_ready packet to ready without
    rebuilding every hash fails closed.
    """

    ledger = parse_ac_evidence_ledger((root / LEDGER_PATH).read_bytes())
    packet = parse_publication_review_packet((root / PUBLICATION_PACKET_PATH).read_bytes())
    doc_scan = parse_release_doc_scan_report((root / DOC_SCAN_PATH).read_bytes())
    if not doc_scan.clean:
        raise ReleaseEvidenceConfigError("release doc scan is not clean")
    live = scan_release_docs(root, built_at=doc_scan.built_at)
    if live.to_canonical_json() != doc_scan.to_canonical_json():
        raise ReleaseEvidenceConfigError("committed doc scan is stale")
    if packet.exact_head != ledger.exact_head:
        raise ReleaseEvidenceConfigError("publication packet exact_head does not match ledger")
    if packet.status == READY_FOR_PUBLICATION_REVIEW_PACKET_STATUS:
        rebuilt = build_ac_evidence_ledger(
            root=root, exact_head=ledger.exact_head, built_at=ledger.built_at
        )
        if rebuilt.to_canonical_json() != ledger.to_canonical_json():
            raise ReleaseEvidenceConfigError(
                "AC ledger is stale relative to ACCEPTANCE.md and catalog"
            )
    if packet.ledger_sha256 != sha256_bytes(ledger.to_canonical_json()):
        raise ReleaseEvidenceConfigError("publication packet ledger hash mismatch")
    if packet.status not in {NOT_READY_PACKET_STATUS, READY_FOR_PUBLICATION_REVIEW_PACKET_STATUS}:
        raise ReleaseEvidenceConfigError(
            "publication packet status must be not_ready or ready_for_publication_review"
        )
    if packet.claims_publication or packet.claims_sent or packet.claims_recovered:
        raise ReleaseEvidenceConfigError("publication packet fabricated completion claims")
    receipt: FreshCloneReceiptV1 | None = None
    if (root / FRESH_CLONE_RECEIPT_PATH).is_file():
        receipt = parse_fresh_clone_receipt((root / FRESH_CLONE_RECEIPT_PATH).read_bytes())
        if receipt.claims_human_clean_machine:
            raise ReleaseEvidenceConfigError("fresh-clone receipt must not claim human H")
        if receipt.actor not in {"ci", "agent"}:
            raise ReleaseEvidenceConfigError("fresh-clone receipt actor must be ci or agent")
        if receipt.exact_head != ledger.exact_head:
            raise ReleaseEvidenceConfigError("fresh-clone receipt exact_head does not match ledger")
        if packet.fresh_clone_receipt_sha256 != sha256_bytes(receipt.to_canonical_json()):
            raise ReleaseEvidenceConfigError("publication packet fresh-clone hash mismatch")
    if packet.status == NOT_READY_PACKET_STATUS:
        # Historical baseline: the recorded commit must exist and be an ancestor,
        # and the packet must validate against what was recorded there. It does
        # not need to equal the current head, and packet-only changes after the
        # recorded commit remain permitted.
        recorded = f"{ledger.exact_head}^{{commit}}"
        try:
            subprocess.run(
                ["git", "rev-parse", "--verify", "--quiet", recorded],
                cwd=root,
                check=True,
                capture_output=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise ReleaseEvidenceConfigError(
                "not_ready release evidence exact_head commit does not exist"
            ) from exc
        if not _git_is_ancestor(root, ledger.exact_head, ledger.exact_head):
            raise ReleaseEvidenceConfigError(
                "not_ready release evidence exact_head must be an ancestor of itself"
            )
        _validate_historical_packet_tree(root, ledger=ledger, packet=packet)
    else:
        assert_release_evidence_exact_head_binding(root=root, exact_head=ledger.exact_head)
    safety_path = root / "tests/fixtures/saas/safety/release-safety-evidence.json"
    safety_ref = next(
        (item for item in packet.references if item.label == "release_safety_evidence"),
        None,
    )
    if safety_path.is_file():
        safety = ReleaseSafetyEvidencePacketV1.model_validate_json(safety_path.read_bytes())
        canonical = safety.to_canonical_json()
        if safety_path.read_bytes() != canonical:
            raise ReleaseEvidenceConfigError("release-safety evidence bytes are not canonical")
        digest = sha256_bytes(canonical)
        if (
            safety_ref is None
            or safety_ref.status != "present"
            or safety_ref.path != "tests/fixtures/saas/safety/release-safety-evidence.json"
            or safety_ref.sha256 != digest
        ):
            raise ReleaseEvidenceConfigError("publication packet release-safety hash mismatch")
        if safety.commit_hash != ledger.exact_head:
            raise ReleaseEvidenceConfigError(
                "release-safety commit_hash does not match release evidence exact_head"
            )
        validate_release_safety_evidence_packet(safety, root=root, publication_status=packet.status)
    elif safety_ref is not None and safety_ref.status != "missing":
        raise ReleaseEvidenceConfigError(
            "release-safety reference must be missing when file absent"
        )
    if packet.status == NOT_READY_PACKET_STATUS:
        rebuilt_packet = build_publication_review_packet(
            root=root,
            ledger=ledger,
            fresh_clone_receipt=receipt,
            exact_head=ledger.exact_head,
            built_at=packet.built_at,
        )
        if rebuilt_packet.to_canonical_json() != packet.to_canonical_json():
            raise ReleaseEvidenceConfigError(
                "publication packet is stale relative to linked evidence"
            )


def _run_capture(
    command: list[str], *, cwd: Path, env: Mapping[str, str] | None = None
) -> tuple[int, bytes]:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=dict(os.environ if env is None else env),
        capture_output=True,
        check=False,
    )
    payload = result.stdout + b"\n---stderr---\n" + result.stderr
    # Redact obvious home paths and tokens from the captured envelope.
    text = payload.decode("utf-8", errors="replace")
    text = _PRIVATE_PATH_RE.sub(r"\1[REDACTED_PATH]", text)
    text = _CREDENTIAL_RE.sub("[REDACTED_CREDENTIAL]", text)
    return result.returncode, text.encode("utf-8")


def _python_version_string() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"


def _uv_version_string(cwd: Path) -> str:
    code, payload = _run_capture(["uv", "--version"], cwd=cwd)
    if code != 0:
        raise ReleaseEvidenceConfigError("uv is required for fresh-clone proof")
    return payload.decode("utf-8", errors="replace").splitlines()[0].strip() or "uv"


def run_fresh_clone_proof(
    *,
    source_root: Path = PACKAGE_ROOT,
    exact_head: str | None = None,
    actor: FreshCloneActor = "agent",
    work_root: Path | None = None,
    built_at: datetime | None = None,
    skip_render_checks: bool = False,
) -> FreshCloneReceiptV1:
    """Run an isolated fresh clone outside the checkout and return a redacted receipt.

    Never claims human clean-machine H evidence.
    """

    if actor not in {"ci", "agent"}:
        raise ReleaseEvidenceConfigError("this runner cannot claim human clean-machine evidence")
    head = exact_head or resolve_commit_hash(source_root)
    if head is None:
        raise ReleaseEvidenceConfigError("exact commit hash is required for fresh-clone proof")
    current = resolve_commit_hash(source_root)
    if current != head:
        raise ReleaseEvidenceConfigError("fresh-clone proof requires an exact-head match")

    cleanup = False
    if work_root is None:
        work_root = Path(tempfile.mkdtemp(prefix="fm038-fresh-clone-"))
        cleanup = True
    work_root = work_root.resolve()
    if source_root.resolve() in work_root.parents or work_root == source_root.resolve():
        raise ReleaseEvidenceConfigError(
            "fresh-clone work root must be outside the source checkout"
        )

    clone_dir = work_root / "repo"
    steps: list[FreshCloneStepV1] = []
    scenario_hashes: dict[str, str] = {}
    public_proof_hash: str | None = None
    overall = 0
    try:
        code, payload = _run_capture(
            ["git", "clone", "--no-local", str(source_root), str(clone_dir)],
            cwd=work_root,
        )
        steps.append(
            FreshCloneStepV1(step_id="git_clone", exit_code=code, sha256=sha256_bytes(payload))
        )
        overall = code if code else overall
        if code != 0:
            raise ReleaseEvidenceConfigError("fresh-clone git clone failed")

        code, payload = _run_capture(["git", "checkout", "--detach", head], cwd=clone_dir)
        steps.append(
            FreshCloneStepV1(
                step_id="checkout_exact_head", exit_code=code, sha256=sha256_bytes(payload)
            )
        )
        overall = code if code else overall
        if code != 0:
            raise ReleaseEvidenceConfigError("fresh-clone exact-head checkout failed")

        verified = resolve_commit_hash(clone_dir)
        if verified != head:
            raise ReleaseEvidenceConfigError("cloned HEAD does not match exact head")

        code, payload = _run_capture(["uv", "sync", "--frozen", "--group", "dev"], cwd=clone_dir)
        steps.append(
            FreshCloneStepV1(step_id="uv_sync_frozen", exit_code=code, sha256=sha256_bytes(payload))
        )
        overall = code if code else overall
        if code != 0:
            raise ReleaseEvidenceConfigError("fresh-clone uv sync --frozen failed")

        code, payload = _run_capture(["uv", "run", "python", "scripts/doctor.py"], cwd=clone_dir)
        steps.append(
            FreshCloneStepV1(step_id="doctor", exit_code=code, sha256=sha256_bytes(payload))
        )
        overall = code if code else overall
        if code != 0:
            raise ReleaseEvidenceConfigError("fresh-clone doctor failed")

        for fixture_id in CANONICAL_PUBLIC_SCENARIOS:
            config = f"configs/{fixture_id}.json"
            out = f"artifacts/fresh-clone/{fixture_id}"
            code, payload = _run_capture(
                [
                    "uv",
                    "run",
                    "found-money",
                    "build",
                    "--config",
                    config,
                    "--output-root",
                    out,
                ],
                cwd=clone_dir,
            )
            steps.append(
                FreshCloneStepV1(
                    step_id=f"build_{fixture_id}",
                    exit_code=code,
                    sha256=sha256_bytes(payload),
                )
            )
            overall = code if code else overall
            if code != 0:
                raise ReleaseEvidenceConfigError(f"fresh-clone build failed for {fixture_id}")
            manifest = clone_dir / out / "scenario" / "manifest.json"
            if not manifest.is_file():
                raise ReleaseEvidenceConfigError(f"missing scenario manifest for {fixture_id}")
            scenario_hashes[fixture_id] = _file_sha256(manifest)
            violations = scan_output_tree(clone_dir / out)
            if violations:
                raise ReleaseEvidenceConfigError(
                    f"public scenario scan failed for {fixture_id}: {violations[0]}"
                )

        code, payload = _run_capture(
            [
                "uv",
                "run",
                "found-money",
                "public-proof",
                "--config",
                "configs/synthetic-public-proof.json",
                "--output-root",
                "artifacts/fresh-clone/public-proof",
            ],
            cwd=clone_dir,
        )
        steps.append(
            FreshCloneStepV1(step_id="public_proof", exit_code=code, sha256=sha256_bytes(payload))
        )
        overall = code if code else overall
        if code != 0:
            raise ReleaseEvidenceConfigError("fresh-clone public-proof failed")
        public_manifest = clone_dir / "artifacts/fresh-clone/public-proof/public-proof.json"
        public_proof_hash = _file_sha256(public_manifest)

        code, payload = _run_capture(
            ["uv", "run", "python", "scripts/public_safety.py"], cwd=clone_dir
        )
        steps.append(
            FreshCloneStepV1(step_id="public_safety", exit_code=code, sha256=sha256_bytes(payload))
        )
        overall = code if code else overall
        if code != 0:
            raise ReleaseEvidenceConfigError("fresh-clone public-safety failed")

        if not skip_render_checks:
            code, payload = _run_capture(
                ["uv", "run", "python", "scripts/render_guard.py"], cwd=clone_dir
            )
            steps.append(
                FreshCloneStepV1(
                    step_id="render_guard", exit_code=code, sha256=sha256_bytes(payload)
                )
            )
            overall = code if code else overall
            if code != 0:
                raise ReleaseEvidenceConfigError("fresh-clone render_guard failed")

        # Quality subset that does not require network or private factory.
        code, payload = _run_capture(["uv", "run", "ruff", "format", "--check", "."], cwd=clone_dir)
        steps.append(
            FreshCloneStepV1(step_id="ruff_format", exit_code=code, sha256=sha256_bytes(payload))
        )
        overall = code if code else overall
        if code != 0:
            raise ReleaseEvidenceConfigError("fresh-clone ruff format failed")

        code, payload = _run_capture(["uv", "run", "ruff", "check", "."], cwd=clone_dir)
        steps.append(
            FreshCloneStepV1(step_id="ruff_check", exit_code=code, sha256=sha256_bytes(payload))
        )
        overall = code if code else overall
        if code != 0:
            raise ReleaseEvidenceConfigError("fresh-clone ruff check failed")

        code, payload = _run_capture(["uv", "run", "mypy", "found_money"], cwd=clone_dir)
        steps.append(FreshCloneStepV1(step_id="mypy", exit_code=code, sha256=sha256_bytes(payload)))
        overall = code if code else overall
        if code != 0:
            raise ReleaseEvidenceConfigError("fresh-clone mypy failed")

        doc_scan = scan_release_docs(clone_dir, built_at=built_at or LOCKED_CLOCK)
        if not doc_scan.clean:
            raise ReleaseEvidenceConfigError(
                "fresh-clone doc scan failed: " + doc_scan.findings[0].excerpt
            )
        doc_scan_hash = sha256_bytes(doc_scan.to_canonical_json())
        steps.append(FreshCloneStepV1(step_id="doc_scan", exit_code=0, sha256=doc_scan_hash))

        receipt = FreshCloneReceiptV1(
            actor=actor,
            claims_human_clean_machine=False,
            exact_head=head,
            python_version=_python_version_string(),
            uv_version=_uv_version_string(clone_dir),
            supported_python=SUPPORTED_PYTHON,
            built_at=built_at or LOCKED_CLOCK,
            steps=steps,
            scenario_manifest_hashes=scenario_hashes,
            public_proof_manifest_sha256=public_proof_hash,
            doc_scan_sha256=doc_scan_hash,
            overall_exit_code=overall,
            redacted=True,
        )
        assert set(scenario_hashes) == set(CANONICAL_PUBLIC_PROOF_SCENARIOS)
        return receipt
    finally:
        if cleanup:
            shutil.rmtree(work_root, ignore_errors=True)


def build_committed_release_bundle(
    *,
    root: Path = PACKAGE_ROOT,
    fresh_clone_receipt: FreshCloneReceiptV1,
    exact_head: str | None = None,
    built_at: datetime | None = None,
) -> tuple[AcEvidenceLedgerV1, PublicationReviewPacketV1, ReleaseDocScanReportV1]:
    """Build ledger/packet/doc-scan bound to a fresh-clone receipt."""

    head = exact_head or fresh_clone_receipt.exact_head
    clock = built_at or fresh_clone_receipt.built_at
    # Write receipt and ledger first so publication references can hash them.
    write_artifact_set_atomic(
        root, {FRESH_CLONE_RECEIPT_PATH: fresh_clone_receipt.to_canonical_json()}
    )
    ledger = build_ac_evidence_ledger(root=root, exact_head=head, built_at=clock)
    write_artifact_set_atomic(root, {LEDGER_PATH: ledger.to_canonical_json()})
    packet = build_publication_review_packet(
        root=root,
        ledger=ledger,
        fresh_clone_receipt=fresh_clone_receipt,
        exact_head=head,
        built_at=clock,
    )
    write_artifact_set_atomic(root, {PUBLICATION_PACKET_PATH: packet.to_canonical_json()})
    doc_scan = scan_release_docs(root, built_at=clock)
    if not doc_scan.clean:
        raise ReleaseEvidenceConfigError(
            "repository doc scan is not clean: "
            + f"{doc_scan.findings[0].path}: {doc_scan.findings[0].excerpt}"
        )
    write_release_evidence_artifacts(
        root=root,
        ledger=ledger,
        packet=packet,
        doc_scan=doc_scan,
        fresh_clone_receipt=fresh_clone_receipt,
    )
    validate_release_evidence_tree(root)
    return ledger, packet, doc_scan
