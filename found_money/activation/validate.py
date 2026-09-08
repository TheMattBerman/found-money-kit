"""Launch-pack hash, link, and cross-ID validation (FM-031)."""

from __future__ import annotations

import json
import csv
import io
import re
from pathlib import Path
from typing import Mapping
from urllib.parse import unquote, urlsplit

from found_money.activation.pack import sha256_bytes, _segment_id_for_pile
from found_money.contracts.activation import (
    ActivationPolicyV1,
    CreativeHandoffV1,
    LaunchPackManifestV1,
    PrivateSegmentFileV1,
    ProductionBriefV1,
    PublicSegmentFileV1,
)
from pydantic import ValidationError

from found_money.contracts.campaign import CompleteRecoveryPlaySetV1, CompleteRecoveryPlayV1
from found_money.contracts.strategy import RecoveryPlaySetV1

_HREF_RE = re.compile(r"""(?:href|src)\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_MD_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
_HTML_ID_RE = re.compile(r"""\bid\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
_MARKED_PLAY_RE = re.compile(
    r"(?:(?:Alternative\s+)?Play ID\s*:\s*`?|Play\s+`)"
    r"([a-z0-9][a-z0-9_-]*)(?:`)?",
    re.IGNORECASE,
)
_MARKED_SEGMENT_RE = re.compile(
    r"(?:Segment ID\s*:\s*`?|Segment\s*:\s*`?|segment\s+`)"
    r"(seg_[a-z0-9_-]+)(?:`)?",
    re.IGNORECASE,
)
_ABS_SCHEME_RE = re.compile(r"^(?:[a-z][a-z0-9+.-]*:|/|~)", re.IGNORECASE)


class LaunchPackValidationError(ValueError):
    """Launch-pack structure or cross-reference failure."""


def parse_launch_pack_manifest(data: bytes) -> LaunchPackManifestV1:
    payload = json.loads(data.decode("utf-8"))
    manifest = LaunchPackManifestV1.model_validate(payload)
    if manifest.to_canonical_json() != bytes(data):
        raise LaunchPackValidationError("manifest bytes are not canonical")
    return manifest


def validate_launch_pack_payloads(payloads: Mapping[str, bytes]) -> LaunchPackManifestV1:
    """Validate hashes, relative links, and play/segment cross-IDs for a payload map."""
    manifest_bytes = payloads.get("launch-pack/manifest.json")
    if manifest_bytes is None:
        raise LaunchPackValidationError("launch-pack/manifest.json is required")
    manifest = parse_launch_pack_manifest(manifest_bytes)

    indexed = {entry.path: entry for entry in manifest.files}
    if len(indexed) != len(manifest.files):
        raise LaunchPackValidationError("manifest contains duplicate file paths")

    for relative, entry in indexed.items():
        if relative == "manifest.json":
            raise LaunchPackValidationError("manifest must not hash itself")
        full = f"launch-pack/{relative}"
        payload = payloads.get(full)
        if payload is None:
            raise LaunchPackValidationError(f"manifest references missing file: {relative}")
        digest = sha256_bytes(payload)
        if digest != entry.sha256:
            raise LaunchPackValidationError(f"hash mismatch for {relative}")

    for full in payloads:
        if not full.startswith("launch-pack/"):
            continue
        if full == "launch-pack/manifest.json":
            continue
        relative = full[len("launch-pack/") :]
        if relative != Path(relative).as_posix() or relative.startswith("./") or "//" in relative:
            raise LaunchPackValidationError(f"noncanonical launch-pack path: {relative}")
        if relative not in indexed:
            raise LaunchPackValidationError(f"unindexed launch-pack file: {relative}")

    if manifest.mode == "public":
        for full in payloads:
            if full.startswith("launch-pack/private/") or "/private/" in full:
                raise LaunchPackValidationError(
                    "public launch pack must not contain a private subtree"
                )

    play_ids = set(manifest.play_ids)
    segment_ids = set(manifest.segment_ids)
    plays_payload = payloads.get("launch-pack/recovery-plays.json")
    if plays_payload is None:
        raise LaunchPackValidationError("canonical recovery-plays artifact is required")
    if sha256_bytes(plays_payload) != manifest.recovery_plays_sha256:
        raise LaunchPackValidationError("recovery-plays hash mismatch")
    try:
        complete_play_set = CompleteRecoveryPlaySetV1.model_validate_json(plays_payload)
    except ValidationError:
        empty_play_set = RecoveryPlaySetV1.model_validate_json(plays_payload)
        if empty_play_set.plays:
            raise LaunchPackValidationError("recovery-plays.json is not a complete play set")
        if (
            manifest.status == "approval_ready"
            or manifest.play_ids
            or manifest.available_play_count != 0
        ):
            raise LaunchPackValidationError(
                "empty recovery plays cannot mark payment-dependent launch assets ready"
            )
        plays_by_id: dict[str, CompleteRecoveryPlayV1] = {}
    else:
        plays_by_id = {play.play_id: play for play in complete_play_set.plays}
    audience_plays: dict[str, set[str]] = {}
    for canonical_play in plays_by_id.values():
        audience_plays.setdefault(_segment_id_for_pile(canonical_play.pile_id), set()).add(
            canonical_play.play_id
        )
    if set(plays_by_id) != play_ids:
        raise LaunchPackValidationError("manifest play ids must match canonical plays")
    private_files: list[PrivateSegmentFileV1] = []
    public_file: PublicSegmentFileV1 | None = None

    for relative, payload in sorted(payloads.items()):
        if not relative.startswith("launch-pack/"):
            continue
        suffix = Path(relative).suffix.lower()
        text: str | None = None
        if suffix in {".html", ".md", ".json", ".csv"}:
            text = payload.decode("utf-8")
        if relative.endswith("public/segments.json"):
            public_file = PublicSegmentFileV1.model_validate(json.loads(payload.decode("utf-8")))
        if (
            "/private/segments/" in relative
            and relative.endswith(".json")
            and not relative.endswith(".withheld.json")
        ):
            private_files.append(
                PrivateSegmentFileV1.model_validate(json.loads(payload.decode("utf-8")))
            )
        if text is None:
            continue
        pack_relative = relative[len("launch-pack/") :]
        file_entry = indexed.get(pack_relative)
        if file_entry is not None and file_entry.asset_class in {
            "play_pages",
            "complete_copy",
            "calendar",
            "offer_landing_briefs",
            "tracking_plan",
            "launch_checklist",
        }:
            if manifest.evidence_packet_sha256 not in text:
                raise LaunchPackValidationError(
                    f"{pack_relative} does not bind the canonical evidence hash"
                )
            if manifest.value_ledger_sha256 not in text:
                raise LaunchPackValidationError(
                    f"{pack_relative} does not bind the canonical value hash"
                )
        if file_entry is not None and file_entry.asset_class == "creative_handoff":
            handoff = CreativeHandoffV1.model_validate_json(payload)
            play = plays_by_id.get(handoff.play_id)
            if (
                play is None
                or handoff.play_id not in play_ids
                or handoff.segment_id not in segment_ids
            ):
                raise LaunchPackValidationError("creative handoff has unknown play/segment ID")
            expected_cards = [card.card_id for card in play.concept_cards]
            actual_cards = [card.card_id for card in handoff.concept_cards]
            if actual_cards != expected_cards:
                raise LaunchPackValidationError(
                    "creative handoff concept card ids must match the referenced play"
                )
            if handoff.money_map_sha256 != manifest.money_map_sha256:
                raise LaunchPackValidationError("creative handoff money-map hash mismatch")
            if handoff.recovery_plays_sha256 != manifest.recovery_plays_sha256:
                raise LaunchPackValidationError("creative handoff recovery-plays hash mismatch")
            if handoff.evidence_packet_sha256 != manifest.evidence_packet_sha256:
                raise LaunchPackValidationError("creative handoff evidence hash mismatch")
            if handoff.value_ledger_sha256 != manifest.value_ledger_sha256:
                raise LaunchPackValidationError("creative handoff value hash mismatch")
        if file_entry is not None and file_entry.asset_class == "production_brief":
            brief = ProductionBriefV1.model_validate_json(payload)
            play = plays_by_id.get(brief.play_id)
            if play is None or brief.play_id not in play_ids or brief.segment_id not in segment_ids:
                raise LaunchPackValidationError("production brief has unknown play/segment ID")
            expected_cards = [card.card_id for card in play.concept_cards]
            actual_cards = [card.card_id for card in brief.concept_cards]
            if actual_cards != expected_cards:
                raise LaunchPackValidationError(
                    "production brief concept card ids must match the referenced play"
                )
            if brief.money_map_sha256 != manifest.money_map_sha256:
                raise LaunchPackValidationError("production brief money-map hash mismatch")
            if brief.recovery_plays_sha256 != manifest.recovery_plays_sha256:
                raise LaunchPackValidationError("production brief recovery-plays hash mismatch")
            if brief.evidence_packet_sha256 != manifest.evidence_packet_sha256:
                raise LaunchPackValidationError("production brief evidence hash mismatch")
            if brief.value_ledger_sha256 != manifest.value_ledger_sha256:
                raise LaunchPackValidationError("production brief value hash mismatch")
        if file_entry is not None and file_entry.asset_class == "activation_policy":
            policy = ActivationPolicyV1.model_validate_json(payload)
            if sha256_bytes(policy.to_canonical_json()) != manifest.activation_policy_sha256:
                raise LaunchPackValidationError("activation policy hash mismatch")
        if file_entry is not None and file_entry.asset_class == "money_map":
            if sha256_bytes(payload) != manifest.money_map_sha256:
                raise LaunchPackValidationError("money-map hash mismatch")
        if suffix in {".html", ".md"}:
            _validate_relative_links(pack_relative, text, indexed, payloads)
        found_plays, found_segments = _validate_cross_ids(
            pack_relative, text, play_ids, segment_ids
        )
        if file_entry is not None and file_entry.play_id is not None:
            if file_entry.play_id not in text:
                raise LaunchPackValidationError(
                    f"{pack_relative} does not contain its indexed play_id"
                )
        if file_entry is not None and file_entry.asset_class == "play_pages":
            if found_plays != audience_plays.get(file_entry.segment_id or "", set()):
                raise LaunchPackValidationError(
                    f"{pack_relative} does not contain the complete play alternative set"
                )
        if file_entry is not None and file_entry.segment_id is not None:
            if file_entry.segment_id not in text:
                raise LaunchPackValidationError(
                    f"{pack_relative} does not contain its indexed segment_id"
                )

    if public_file is None:
        raise LaunchPackValidationError("public segments file is required")
    if {r.segment_id for r in public_file.segments} != segment_ids or len(
        public_file.segments
    ) != len(segment_ids):
        raise LaunchPackValidationError("public segments must match manifest audiences exactly")
    if audience_plays and set(audience_plays) != segment_ids:
        raise LaunchPackValidationError("manifest audiences must match canonical play piles")
    for row in public_file.segments:
        if set(row.play_ids) != audience_plays.get(row.segment_id, set()):
            raise LaunchPackValidationError(
                "public segment play_ids must match its canonical audience"
            )
        if row.segment_id not in segment_ids:
            raise LaunchPackValidationError("public segment_ids must match the manifest")
        # Public aggregates must not smuggle member rows.
        raw = public_file.canonical_dict()
        dumped = json.dumps(raw)
        for forbidden in ('"customer_token"', '"source_id"', '"members"', "@"):
            if forbidden in dumped:
                raise LaunchPackValidationError("public segments contain private identity material")

    if manifest.mode == "private":
        validate_private_launch_pack_payloads(payloads, manifest=manifest)
    elif private_files:
        raise LaunchPackValidationError("public mode must not include private segment files")

    if public_file.money_map_sha256 != manifest.money_map_sha256:
        raise LaunchPackValidationError("public segment money_map hash mismatch")
    if public_file.recovery_plays_sha256 != manifest.recovery_plays_sha256:
        raise LaunchPackValidationError("public segment recovery_plays hash mismatch")
    if public_file.evidence_packet_sha256 != manifest.evidence_packet_sha256:
        raise LaunchPackValidationError("public segment evidence hash mismatch")
    if public_file.value_ledger_sha256 != manifest.value_ledger_sha256:
        raise LaunchPackValidationError("public segment value hash mismatch")

    return manifest


def validate_private_launch_pack_payloads(
    payloads: Mapping[str, bytes],
    *,
    manifest: LaunchPackManifestV1 | None = None,
) -> list[PrivateSegmentFileV1]:
    """Validate private segment files separately from package-wide public safety."""
    if manifest is None:
        manifest_bytes = payloads.get("launch-pack/manifest.json")
        if manifest_bytes is None:
            raise LaunchPackValidationError("launch-pack/manifest.json is required")
        manifest = parse_launch_pack_manifest(manifest_bytes)
    if manifest.mode != "private":
        raise LaunchPackValidationError("private validation requires private mode")

    audience_plays: dict[str, set[str]] = {}
    if manifest.play_ids:
        plays = CompleteRecoveryPlaySetV1.model_validate_json(
            payloads["launch-pack/recovery-plays.json"]
        )
        for play in plays.plays:
            audience_plays.setdefault(_segment_id_for_pile(play.pile_id), set()).add(play.play_id)
    segment_ids = set(manifest.segment_ids)
    private_files: list[PrivateSegmentFileV1] = []
    for relative, payload in sorted(payloads.items()):
        if not relative.startswith("launch-pack/private/segments/"):
            continue
        if not relative.endswith(".json"):
            continue
        private = PrivateSegmentFileV1.model_validate(json.loads(payload.decode("utf-8")))
        private_files.append(private)
        if set(private.play_ids) != audience_plays.get(private.segment_id, set()):
            raise LaunchPackValidationError("private segment play_ids must match the manifest")
        if private.segment_id not in segment_ids:
            raise LaunchPackValidationError(f"private segment_id unknown: {private.segment_id}")
        if private.money_map_sha256 != manifest.money_map_sha256:
            raise LaunchPackValidationError("private segment money_map hash mismatch")
        if private.recovery_plays_sha256 != manifest.recovery_plays_sha256:
            raise LaunchPackValidationError("private segment recovery_plays hash mismatch")
        if private.evidence_packet_sha256 != manifest.evidence_packet_sha256:
            raise LaunchPackValidationError("private segment evidence hash mismatch")
        if private.value_ledger_sha256 != manifest.value_ledger_sha256:
            raise LaunchPackValidationError("private segment value hash mismatch")
        if private.activation_policy_sha256 != manifest.activation_policy_sha256:
            raise LaunchPackValidationError("private segment activation policy hash mismatch")
        tokens = [member.customer_token for member in private.members]
        if len(tokens) != len(set(tokens)):
            raise LaunchPackValidationError("private segment duplicates the same customer")
        for member in private.members:
            if member.exclusion_status != "included":
                raise LaunchPackValidationError("private members must be eligible only")
            if member.segment_id != private.segment_id:
                raise LaunchPackValidationError("private member segment_id mismatch")
            if set(member.play_ids) != set(private.play_ids):
                raise LaunchPackValidationError("private member play_ids mismatch")
            for source in member.source_ids:
                if source.source_system == "stripe":
                    raise LaunchPackValidationError(
                        "private members must omit Stripe customer/invoice/payment IDs"
                    )
    return private_files


def _heading_slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9\s-]", "", text.strip().lower())
    slug = re.sub(r"\s+", "-", slug)
    return slug.strip("-")


def _document_anchors(text: str, *, suffix: str) -> set[str]:
    anchors = set(_HTML_ID_RE.findall(text))
    if suffix == ".md":
        for match in _MD_HEADING_RE.finditer(text):
            anchors.add(_heading_slug(match.group(2)))
    return anchors


def _validate_relative_links(
    relative: str,
    text: str,
    indexed: Mapping[str, object],
    payloads: Mapping[str, bytes],
) -> None:
    base = Path(relative).parent
    refs = [match.group(1).strip() for match in _HREF_RE.finditer(text)]
    refs.extend(match.group(1).strip() for match in _MD_LINK_RE.finditer(text))
    anchors = _document_anchors(text, suffix=Path(relative).suffix.lower())

    for ref in refs:
        if not ref:
            raise LaunchPackValidationError(f"{relative} has an empty link")
        lowered = ref.casefold()
        if lowered.startswith(("http://", "https://", "mailto:", "file:")):
            raise LaunchPackValidationError(f"{relative} has disallowed absolute/scheme link {ref}")
        if ref.startswith(("/", "~")) or _ABS_SCHEME_RE.match(ref):
            raise LaunchPackValidationError(f"{relative} has absolute link {ref}")
        if "?" in ref:
            raise LaunchPackValidationError(f"{relative} has query-bearing link {ref}")

        parsed = urlsplit(ref)
        if parsed.scheme or parsed.netloc:
            raise LaunchPackValidationError(f"{relative} has absolute link {ref}")

        path_part = parsed.path or ""
        fragment = parsed.fragment
        if path_part == "" and fragment:
            if not re.fullmatch(r"[A-Za-z][\w:.-]*", fragment) or fragment not in anchors:
                raise LaunchPackValidationError(f"{relative} has bad fragment link {ref}")
            continue

        decoded = unquote(path_part)
        if "%" in path_part:
            # Fail closed on percent-encoding, including encoded traversal.
            raise LaunchPackValidationError(f"{relative} has percent-encoded/traversal link {ref}")

        target = (base / decoded).as_posix()
        parts: list[str] = []
        for part in Path(target).parts:
            if part in {"", "."}:
                continue
            if part == "..":
                if not parts:
                    raise LaunchPackValidationError(f"{relative} link escapes pack root: {ref}")
                parts.pop()
                continue
            parts.append(part)
        normalized = "/".join(parts)
        if normalized == "manifest.json":
            if fragment:
                raise LaunchPackValidationError(f"{relative} has bad fragment on manifest link")
            continue
        if normalized not in indexed:
            raise LaunchPackValidationError(f"{relative} broken relative link: {ref}")
        # Directory targets are rejected: indexed entries are files only.
        full = f"launch-pack/{normalized}"
        if full not in payloads:
            raise LaunchPackValidationError(f"{relative} missing link target: {ref}")
        if fragment:
            target_text = payloads[full].decode("utf-8")
            target_anchors = _document_anchors(target_text, suffix=Path(normalized).suffix.lower())
            if not re.fullmatch(r"[A-Za-z][\w:.-]*", fragment) or fragment not in target_anchors:
                raise LaunchPackValidationError(f"{relative} has bad fragment link {ref}")


def _validate_cross_ids(
    relative: str,
    text: str,
    play_ids: set[str],
    segment_ids: set[str],
) -> tuple[set[str], set[str]]:
    found_plays: set[str] = set()
    found_segments: set[str] = set()
    if relative.endswith(".json"):
        payload = json.loads(text)

        def collect(value: object) -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    if key == "play_id" and isinstance(child, str):
                        found_plays.add(child)
                    elif key == "play_ids" and isinstance(child, list):
                        found_plays.update(item for item in child if isinstance(item, str))
                    elif key == "segment_id" and isinstance(child, str):
                        found_segments.add(child)
                    elif key == "segment_ids" and isinstance(child, list):
                        found_segments.update(item for item in child if isinstance(item, str))
                    collect(child)
            elif isinstance(value, list):
                for child in value:
                    collect(child)

        collect(payload)
    elif relative.endswith(".csv"):
        for row in csv.DictReader(io.StringIO(text)):
            if row.get("play_id"):
                found_plays.add(row["play_id"])
            if row.get("play_ids"):
                found_plays.update(item for item in row["play_ids"].split("|") if item)
            if row.get("segment_id"):
                found_segments.add(row["segment_id"])
            if row.get("segment_ids"):
                found_segments.update(item for item in row["segment_ids"].split("|") if item)
    else:
        found_plays.update(_MARKED_PLAY_RE.findall(text))
        found_segments.update(_MARKED_SEGMENT_RE.findall(text))
    for play_id in found_plays:
        if play_id not in play_ids:
            raise LaunchPackValidationError(f"{relative} references unknown play_id {play_id}")
    for segment_id in found_segments:
        if segment_id not in segment_ids:
            raise LaunchPackValidationError(
                f"{relative} references unknown segment_id {segment_id}"
            )
    return found_plays, found_segments


def validate_launch_pack_tree(root: Path) -> LaunchPackManifestV1:
    """Validate an on-disk launch-pack directory under a caller output root."""
    pack_root = Path(root)
    if pack_root.name != "launch-pack":
        pack_root = pack_root / "launch-pack"
    if not pack_root.is_dir():
        raise LaunchPackValidationError("launch-pack directory is missing")
    pack_root_resolved = pack_root.resolve()
    payloads: dict[str, bytes] = {}
    for path in sorted(pack_root.rglob("*")):
        if path.is_symlink() or path.is_file():
            resolved = path.resolve()
            try:
                resolved.relative_to(pack_root_resolved)
            except ValueError as exc:
                raise LaunchPackValidationError(
                    f"symlink escapes launch-pack root: {path}"
                ) from exc
        if not path.is_file():
            continue
        relative = path.relative_to(pack_root.parent).as_posix()
        payloads[relative] = path.read_bytes()
    return validate_launch_pack_payloads(payloads)
