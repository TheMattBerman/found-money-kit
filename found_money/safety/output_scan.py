"""Full output-tree scanning for identity, secrets, and unsafe metadata."""

from __future__ import annotations

import io
import json
import re
import unicodedata
from pathlib import Path
from typing import Iterable

from PIL import Image
from pypdf import PdfReader

# Keep detectors at least as strict as scripts/public_safety.py.
_EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_PHONE_RE = re.compile(
    r"(?<![0-9a-fA-F])(?:\+\d{1,3}[\s.-]?)?(?:\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4})(?![0-9a-fA-F])"
)
_URL_RE = re.compile(r"(?i)\bhttps?://[^\s\"']+|\?[A-Za-z0-9_]+=[^\s\"']+")
_UNSAFE_SCHEME_RE = re.compile(r"(?i)(?:^|[\s\"'(<])(?:file|https?|mailto|javascript|data):")
_PROVIDER_CUSTOMER_ID_RE = re.compile(
    r"\b(?:cus_(?!t_)[A-Za-z0-9_]*[0-9][A-Za-z0-9_]*|"
    r"hs_(?:contact|ct|dl|deal)_[A-Za-z0-9_]+)\b"
)
_ABS_PATH_RE = re.compile(r"(?i)(^|[\s\"'])(/Users/|/home/|[A-Za-z]:\\)")
_CREDENTIAL_RE = re.compile(
    r"(?i)(\"(?:password|passwd|secret|api[_-]?key|access[_-]?key|private[_-]?key|"
    r"authorization|credential|credentials|bearer|client[_-]?secret)\"\s*:|"
    r"\b(?:sk_live_|sk_test_|rk_live_|rk_test_|ghp_|xox[baprs]-)[A-Za-z0-9_-]{8,})"
)
_SECRET_VALUE_RE = re.compile(
    r"(?i)\b(?:sk_live_|sk_test_|rk_live_|rk_test_|ghp_|xox[baprs]-)[A-Za-z0-9_-]{8,}"
)
_TOKEN_VALUE_RE = re.compile(r'(?i)"token"\s*:\s*"([^"]*)"')
_PUBLIC_OPAQUE_TOKEN_RE = re.compile(r"^rec_[0-9a-f]{12,64}$")
_IDENTITY_KEY_RE = re.compile(
    r'(?i)"(email|phone|name|company|source_id|record_id|contact_id|crm_url|deal_name|'
    r'external_ids|account_token)"\s*:'
)
_CLAIM_WORDS = frozenset(
    {
        "assurance",
        "assurances",
        "assure",
        "assured",
        "assures",
        "assuring",
        "guarantee",
        "guaranteed",
        "guarantees",
        "guaranteeing",
        "promise",
        "promised",
        "promises",
        "promising",
    }
)
_NEGATION_WORDS = frozenset({"never", "no", "not", "without"})
_NON_TOKEN_RE = re.compile(r"[^\w]+", re.UNICODE)
_MAX_CLAIM_GAP = 6


def contains_recovered_revenue_claim(text: str) -> bool:
    """Return True for recovered-revenue guarantee/promise/assurance claims.

    Punctuation and hyphenation are normalized to spaces so ordinary separators
    cannot hide a claim. A claim lemma immediately after a negation word such as
    ``no`` is ignored so truthful denials remain publishable.
    """

    tokens = _NON_TOKEN_RE.sub(" ", unicodedata.normalize("NFKC", text).casefold()).split()
    recovered_at = [
        index
        for index, token in enumerate(tokens[:-1])
        if token == "recovered" and tokens[index + 1] == "revenue"
    ]
    if not recovered_at:
        return False
    for index, token in enumerate(tokens):
        if token not in _CLAIM_WORDS:
            continue
        if index and tokens[index - 1] in _NEGATION_WORDS:
            continue
        for recovered_index in recovered_at:
            if index < recovered_index:
                gap = recovered_index - index - 1
            elif index > recovered_index + 1:
                gap = index - recovered_index - 2
            else:
                continue
            if 0 <= gap <= _MAX_CLAIM_GAP:
                return True
    return False


TEXT_SUFFIXES = {
    ".json",
    ".html",
    ".htm",
    ".txt",
    ".md",
    ".csv",
    ".log",
    ".yml",
    ".yaml",
    ".toml",
    ".xml",
}
BINARY_SCAN_SUFFIXES = {".pdf", ".png"}
FUTURE_HANDOFF_NAMES = {
    "launch-pack",
    "creative-handoff",
    "production-brief",
    "handoff",
    "segments",
}

# Private contract artifacts may retain source IDs; public/scan trees must not.
_PRIVATE_BASENAME_ALLOWLIST = {
    "identity-graph.json",
    "recovery-candidates.json",
    "contribution-ledger.json",
    "money-map.json",
    "recovery-plays.json",
    "exclusion-ledger.json",
    "data-gaps.json",
    "snapshot.json",
}


# Vendored minified third-party bundles contain scheme-shaped JS string literals
# ("data:" in DataTexture helpers). They are integrity-checked assets, not emitted
# payloads, so the scheme scan does not apply to them.
_VENDOR_ASSET_PATHS = frozenset({"assets/three.min.js"})


# Only the exact bundled texture carrier is opaque binary data. A modified script
# at this path is rejected, even if an outer artifact manifest is rehashed.
_TEXTURE_ASSET_SHA256 = "5705df735d744a9c5773fd370deb18c9b6e50495eda269c941f0effb6454c928"


def verified_texture_asset(label: str, payload: bytes) -> bool:
    from hashlib import sha256

    if label.replace("\\", "/") != "assets/atlas-texture.js":
        return False
    if sha256(payload).hexdigest() != _TEXTURE_ASSET_SHA256:
        raise ValueError("texture asset differs from the bundled image carrier")
    return True


def scan_text_artifact(label: str, text: str, *, suffix: str) -> list[str]:
    try:
        if verified_texture_asset(label, text.encode("utf-8")):
            return []
    except ValueError as exc:
        return [f"{label}: {exc}"]
    if label.replace("\\", "/") in _VENDOR_ASSET_PATHS:
        # Vendored bundle: run only the checks that cannot false-positive on
        # minified third-party code (credential shapes and absolute paths).
        violations: list[str] = []
        if _CREDENTIAL_RE.search(text):
            violations.append(f"{label}: contains credential-shaped value")
        if _ABS_PATH_RE.search(text):
            violations.append(f"{label}: contains absolute local path")
        return violations
    if suffix == ".json":
        try:
            json.loads(text)
        except json.JSONDecodeError as exc:
            return [f"{label}: malformed JSON ({exc})"]
        violations = _text_checks(label, text, json_mode=True)
        for match in _TOKEN_VALUE_RE.finditer(text):
            if not _PUBLIC_OPAQUE_TOKEN_RE.fullmatch(match.group(1)):
                violations.append(f"{label}: contains credential-shaped token value")
        return violations
    return _text_checks(label, text, json_mode=False, html_mode=suffix in {".html", ".htm"})


def _text_checks(
    label: str,
    text: str,
    *,
    json_mode: bool,
    html_mode: bool = False,
) -> list[str]:
    violations: list[str] = []
    scanned = text
    if html_mode:
        from found_money.activation.intake import html_for_public_scan

        try:
            scanned = html_for_public_scan(text)
        except ValueError as exc:
            return [f"{label}: unsafe handoff intake markup ({exc})"]
    checks = [
        ("email", _EMAIL_RE),
        ("phone", _PHONE_RE),
        ("URL/query string", _URL_RE),
        ("unsafe URI scheme", _UNSAFE_SCHEME_RE),
        ("absolute local path", _ABS_PATH_RE),
        ("credential-shaped value", _CREDENTIAL_RE),
        ("provider-shaped customer/source id", _PROVIDER_CUSTOMER_ID_RE),
    ]
    if json_mode:
        checks.append(("raw customer identity key", _IDENTITY_KEY_RE))
    for name, pattern in checks:
        if pattern.search(scanned):
            violations.append(f"{label}: contains {name}")
    if contains_recovered_revenue_claim(scanned):
        violations.append(f"{label}: contains recovered-revenue guarantee claim")
    return violations


def scan_pdf_bytes(label: str, payload: bytes) -> list[str]:
    violations: list[str] = []
    try:
        reader = PdfReader(io.BytesIO(payload))
    except Exception as exc:  # fail closed
        return [f"{label}: unreadable PDF ({exc})"]
    texts: list[str] = []
    for page in reader.pages:
        try:
            texts.append(page.extract_text() or "")
        except Exception as exc:
            violations.append(f"{label}: PDF text extraction failed ({exc})")
    metadata = reader.metadata
    if metadata is not None:
        for key, value in metadata.items():
            texts.append(f"{key}:{value}")
    for page in reader.pages:
        annots = page.get("/Annots")
        if not annots:
            continue
        for annot in annots:
            obj = annot.get_object()
            action = obj.get("/A")
            if action is not None:
                action = action.get_object()
                for key in ("/URI", "/F", "/JS"):
                    value = action.get(key)
                    if value is not None:
                        texts.append(str(value))
            contents = obj.get("/Contents")
            if contents is not None:
                texts.append(str(contents))
    joined = "\n".join(texts)
    violations.extend(_text_checks(label, joined, json_mode=False))
    return violations


def scan_png_bytes(label: str, payload: bytes) -> list[str]:
    violations: list[str] = []
    try:
        with Image.open(io.BytesIO(payload)) as image:
            texts: list[str] = []
            info = getattr(image, "info", {}) or {}
            for key, value in info.items():
                texts.append(f"{key}:{value}")
            # PNG textual chunks often land in info; also probe common keys.
            for key in ("exif", "xmp", "xml", "icc_profile", "comment", "description"):
                if key in info and info[key]:
                    raw = info[key]
                    if isinstance(raw, bytes):
                        texts.append(raw.decode("utf-8", errors="replace"))
                    else:
                        texts.append(str(raw))
            joined = "\n".join(texts)
            if joined.strip():
                violations.extend(_text_checks(label, joined, json_mode=False))
    except Exception as exc:
        return [f"{label}: unreadable PNG ({exc})"]
    return violations


def _should_skip_private(path: Path, root: Path) -> bool:
    rel = path.relative_to(root).as_posix()
    if path.name in _PRIVATE_BASENAME_ALLOWLIST:
        return True
    # Future private segment trees under launch-pack/private remain private.
    parts = Path(rel).parts
    if parts and parts[0] == "private":
        return True
    if "private" in parts and "launch-pack" in parts:
        return True
    return False


def scan_output_tree(root: Path | str, *, include_private: bool = False) -> list[str]:
    """Scan an output/run tree including JSON/HTML/text/logs/PDF/PNG and handoff files."""
    base = Path(root)
    if not base.exists():
        return [f"missing output root: {base}"]
    violations: list[str] = []
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        if not include_private and _should_skip_private(path, base):
            # Still scan private trees for credential/secret shapes only.
            try:
                raw = path.read_bytes()
            except OSError as exc:
                violations.append(f"{path}: unreadable ({exc})")
                continue
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                continue
            secret_shaped = (
                _SECRET_VALUE_RE.search(text)
                if Path(path.relative_to(base).as_posix()).parts[:1] == ("private",)
                else _CREDENTIAL_RE.search(text)
            )
            if secret_shaped or _ABS_PATH_RE.search(text):
                rel = path.relative_to(base).as_posix()
                violations.append(f"{rel}: contains credential-shaped or absolute-path value")
            continue
        rel = path.relative_to(base).as_posix()
        suffix = path.suffix.casefold()
        try:
            payload = path.read_bytes()
        except OSError as exc:
            violations.append(f"{rel}: unreadable ({exc})")
            continue
        if suffix in TEXT_SUFFIXES or suffix == "":
            # Treat extensionless logs/handoffs as text when UTF-8 decodable.
            try:
                text = payload.decode("utf-8")
            except UnicodeDecodeError:
                continue
            effective = suffix if suffix in TEXT_SUFFIXES else ".txt"
            violations.extend(scan_text_artifact(rel, text, suffix=effective))
        elif suffix == ".pdf":
            violations.extend(scan_pdf_bytes(rel, payload))
        elif suffix == ".png":
            violations.extend(scan_png_bytes(rel, payload))
        # Future launch/handoff directories: ensure named trees are covered when present.
        if any(name in Path(rel).parts for name in FUTURE_HANDOFF_NAMES):
            if suffix not in TEXT_SUFFIXES | BINARY_SCAN_SUFFIXES and suffix != "":
                # Unknown binary under handoff/launch trees fails closed unless empty.
                if payload and suffix not in {".pdf", ".png"}:
                    # Allow only already-scanned types; other binaries are reported.
                    if suffix not in {".svg", ".css", ".js"}:
                        violations.append(
                            f"{rel}: unsupported handoff/launch artifact type {suffix}"
                        )
    return violations


def scan_paths(paths: Iterable[Path]) -> list[str]:
    violations: list[str] = []
    for path in paths:
        if not path.is_file():
            continue
        suffix = path.suffix.casefold()
        payload = path.read_bytes()
        label = str(path)
        if suffix == ".pdf":
            violations.extend(scan_pdf_bytes(label, payload))
        elif suffix == ".png":
            violations.extend(scan_png_bytes(label, payload))
        else:
            text = payload.decode("utf-8")
            violations.extend(scan_text_artifact(label, text, suffix=suffix or ".txt"))
    return violations
