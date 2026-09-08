"""Atomic identity-stage runner for FM-039 source-set integration."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from found_money.contracts.identity import (
    IdentityGraphV1,
    IdentityOverrideEnvelopeV1,
    PublicIdentityProjectionV1,
)
from found_money.contracts.identity_stage import (
    ALLOWED_MATCH_RULES,
    IdentitySourceReferenceV1,
    IdentityStageEvidenceV1,
    parse_identity_stage_evidence,
)
from found_money.contracts.run import compute_source_set_hash
from found_money.contracts.source import SourceReceiptV1
from found_money.contracts.source_stage import SourceSetManifestV1
from found_money.identity import (
    _object_pairs_hook,
    _parse_source_set,
    _nodes_from_verified_payloads,
    apply_identity_override,
    build_identity_graph,
    canonical_identity_payload_bytes,
    parse_canonical_json,
    public_identity_projection,
    sha256_bytes,
    verify_source_set_payloads,
)
from found_money.receipts import _atomic_write_bytes, _validate_relative_under_root

IDENTITY_GRAPH_PATH = "identity/identity-graph.json"
IDENTITY_PUBLIC_PATH = "identity/identity-public.json"
IDENTITY_STAGE_PATH = "provenance/identity-stage.json"
DEFAULT_BUILT_AT = datetime(2026, 7, 29, 18, 0, 5, tzinfo=timezone.utc)
_TRAVERSAL_RE = re.compile(r"(^|/)\.\.(/|$)")


class IdentityStageError(ValueError):
    """A sanitized identity-stage validation or atomic-output failure."""


@dataclass(frozen=True)
class IdentityStageResult:
    """Committed identity-stage outputs and their public-safe evidence."""

    output_root: Path
    graph: IdentityGraphV1
    projection: PublicIdentityProjectionV1
    evidence: IdentityStageEvidenceV1

    @property
    def source_set_hash(self) -> str:
        return self.evidence.source_set_hash


def _resolve_output_root(root: Path | str) -> Path:
    raw = str(root).replace("\\", "/")
    if _TRAVERSAL_RE.search(raw) or ".." in Path(root).parts:
        raise IdentityStageError("identity-stage output root must not contain traversal")
    candidate = Path(root)
    if candidate.exists() or candidate.is_symlink():
        raise IdentityStageError("identity-stage output root must not already exist")
    parent = candidate.parent if candidate.parent != Path("") else Path.cwd()
    if not parent.exists() or not parent.is_dir() or parent.is_symlink():
        raise IdentityStageError("identity-stage output parent must be an existing directory")
    return candidate.resolve(strict=False)


def _match_rule_counts(graph: IdentityGraphV1) -> dict[str, int]:
    counts: dict[str, int] = {rule: 0 for rule in ALLOWED_MATCH_RULES}
    for edge in graph.edges:
        counts[edge.match_rule] = counts.get(edge.match_rule, 0) + 1
    return counts


def _source_references(
    graph: IdentityGraphV1,
    source_set: SourceSetManifestV1,
    verified_hashes: Mapping[str, str],
) -> list[IdentitySourceReferenceV1]:
    node_counts: dict[str, int] = {}
    for node in graph.nodes:
        node_counts[node.source_system] = node_counts.get(node.source_system, 0) + 1
    references = []
    for entry in source_set.sources:
        digest = verified_hashes.get(entry.source_id)
        if not digest:
            raise IdentityStageError(f"missing verified content hash for source {entry.source_id}")
        references.append(
            IdentitySourceReferenceV1(
                declared_source=entry.source_id,
                source_type=entry.source_type,
                receipt_path=entry.receipt_path,
                content_hash=digest,
                identity_node_count=node_counts.get(entry.source_id, 0),
            )
        )
    return sorted(references, key=lambda item: item.declared_source)


def _coerce_graph(value: IdentityGraphV1 | bytes | bytearray) -> IdentityGraphV1:
    if isinstance(value, IdentityGraphV1):
        return value
    parsed = parse_canonical_json(bytes(value))
    if not isinstance(parsed, IdentityGraphV1):
        raise IdentityStageError("identity-stage bundle graph must be identity-graph.v1")
    return parsed


def _coerce_projection(
    value: PublicIdentityProjectionV1 | bytes | bytearray,
) -> PublicIdentityProjectionV1:
    if isinstance(value, PublicIdentityProjectionV1):
        return value
    parsed = parse_canonical_json(bytes(value))
    if not isinstance(parsed, PublicIdentityProjectionV1):
        raise IdentityStageError("identity-stage bundle projection must be identity-public.v1")
    return parsed


def _references_from_receipts(
    graph: IdentityGraphV1,
    source_receipts: Mapping[str, SourceReceiptV1],
    *,
    payloads: Mapping[str, Mapping[str, Any] | bytes | bytearray] | None = None,
) -> tuple[str, list[IdentitySourceReferenceV1]]:
    verified_by_receipt: dict[str, str] = {}
    node_counts: dict[str, int] = {}
    for node in graph.nodes:
        node_counts[node.source_system] = node_counts.get(node.source_system, 0) + 1
    references: list[IdentitySourceReferenceV1] = []
    for path, receipt in source_receipts.items():
        digest = receipt.content_hash
        if payloads is not None:
            raw = payloads.get(receipt.source_type)
            if raw is None:
                raise IdentityStageError(
                    f"missing verified payload for receipt source {receipt.source_type}"
                )
            observed = sha256_bytes(canonical_identity_payload_bytes(raw))
            if observed != digest:
                raise IdentityStageError(
                    f"receipt content_hash mismatch for source {receipt.source_type}"
                )
            digest = observed
        verified_by_receipt[str(path)] = digest
        references.append(
            IdentitySourceReferenceV1(
                declared_source=receipt.source_type,
                source_type=receipt.source_type,
                receipt_path=str(path),
                content_hash=digest,
                identity_node_count=node_counts.get(receipt.source_type, 0),
            )
        )
    references.sort(key=lambda item: item.declared_source)
    return compute_source_set_hash(verified_by_receipt), references


def validate_identity_stage_bundle(
    *,
    graph: IdentityGraphV1 | bytes | bytearray,
    projection: PublicIdentityProjectionV1 | bytes | bytearray,
    evidence: IdentityStageEvidenceV1 | bytes | bytearray | Mapping[str, Any],
    source_set: SourceSetManifestV1 | Mapping[str, Any] | bytes | bytearray | None = None,
    payloads: Mapping[str, Mapping[str, Any] | bytes | bytearray] | None = None,
    source_receipts: Mapping[str, SourceReceiptV1] | None = None,
) -> IdentityStageEvidenceV1:
    """Independently rebuild identity-stage evidence and exact-compare the bundle."""
    parsed_graph = _coerce_graph(graph)
    parsed_projection = _coerce_projection(projection)
    parsed_evidence = (
        evidence
        if isinstance(evidence, IdentityStageEvidenceV1)
        else parse_identity_stage_evidence(evidence)
    )
    assert_identity_count_consistency(parsed_graph, parsed_projection)
    if source_set is not None:
        if payloads is None:
            raise IdentityStageError("identity-stage bundle requires verified payloads")
        parsed_set = _parse_source_set(source_set)
        _decoded, verified = verify_source_set_payloads(parsed_set, payloads)
        expected_hash = parsed_set.source_set_hash
        references = _source_references(parsed_graph, parsed_set, verified)
    elif source_receipts is not None:
        expected_hash, references = _references_from_receipts(
            parsed_graph, source_receipts, payloads=payloads
        )
    else:
        raise IdentityStageError("identity-stage bundle requires source-set payloads or receipts")

    rebuilt = build_identity_stage_evidence(
        parsed_graph,
        parsed_projection,
        source_set_hash=expected_hash,
        source_references=references,
        private_graph_path=parsed_evidence.private_graph_path,
        public_projection_path=parsed_evidence.public_projection_path,
    )
    if rebuilt.resolved_customer_count != parsed_evidence.resolved_customer_count:
        raise IdentityStageError("identity-stage resolved_customer_count mismatch")
    if rebuilt.ambiguous_cluster_count != parsed_evidence.ambiguous_cluster_count:
        raise IdentityStageError("identity-stage ambiguous_cluster_count mismatch")
    if rebuilt.quarantined_member_count != parsed_evidence.quarantined_member_count:
        raise IdentityStageError("identity-stage quarantined_member_count mismatch")
    if dict(rebuilt.match_rule_counts) != dict(parsed_evidence.match_rule_counts):
        raise IdentityStageError("identity-stage match_rule_counts mismatch")
    if rebuilt.private_graph_sha256 != parsed_evidence.private_graph_sha256:
        raise IdentityStageError("identity-stage private_graph_sha256 mismatch")
    if rebuilt.public_projection_sha256 != parsed_evidence.public_projection_sha256:
        raise IdentityStageError("identity-stage public_projection_sha256 mismatch")
    if rebuilt.source_set_hash != parsed_evidence.source_set_hash:
        raise IdentityStageError("identity-stage source_set_hash mismatch")
    rebuilt_refs = [
        (
            item.declared_source,
            item.source_type,
            item.receipt_path,
            item.content_hash,
            item.identity_node_count,
        )
        for item in rebuilt.source_references
    ]
    evidence_refs = [
        (
            item.declared_source,
            item.source_type,
            item.receipt_path,
            item.content_hash,
            item.identity_node_count,
        )
        for item in parsed_evidence.source_references
    ]
    if rebuilt_refs != evidence_refs:
        raise IdentityStageError("identity-stage source_references mismatch")
    if rebuilt.to_canonical_json() != parsed_evidence.to_canonical_json():
        raise IdentityStageError("identity-stage evidence canonical bytes mismatch")
    return parsed_evidence


def assert_identity_count_consistency(
    graph: IdentityGraphV1,
    projection: PublicIdentityProjectionV1,
    *,
    candidate_tokens: set[str] | None = None,
    contribution_tokens: set[str] | None = None,
    resolved_customer_count: int | None = None,
    ambiguous_cluster_count: int | None = None,
) -> None:
    """Fail closed when public/downstream counts diverge from the private graph."""
    resolved = {cluster.customer_token for cluster in graph.customers}
    if projection.customer_count != len(graph.customers):
        raise IdentityStageError(
            "identity public projection count does not match resolved customers"
        )
    projected = {row.customer_token for row in projection.customers}
    if projected != resolved:
        raise IdentityStageError("identity public tokens do not match resolved customers")
    by_token = {row.customer_token: row for row in projection.customers}
    for cluster in graph.customers:
        if by_token[cluster.customer_token].member_count != len(cluster.member_node_ids):
            raise IdentityStageError("identity public member_count does not match private cluster")
    quarantined = {
        node_id for cluster in graph.ambiguous_identities for node_id in cluster.member_node_ids
    }
    if any(
        node_id in quarantined for cluster in graph.customers for node_id in cluster.member_node_ids
    ):
        raise IdentityStageError("quarantined members leaked into resolved customers")
    extra_candidates = (candidate_tokens or set()) - resolved
    if extra_candidates:
        raise IdentityStageError("candidate tokens are not in the resolved identity graph")
    extra_contrib = (contribution_tokens or set()) - resolved
    if extra_contrib:
        raise IdentityStageError("contribution tokens are not in the resolved identity graph")
    if resolved_customer_count is not None and resolved_customer_count != len(graph.customers):
        raise IdentityStageError("downstream resolved customer count does not match identity graph")
    if ambiguous_cluster_count is not None and ambiguous_cluster_count != len(
        graph.ambiguous_identities
    ):
        raise IdentityStageError("downstream ambiguous cluster count does not match identity graph")


def build_identity_stage_evidence(
    graph: IdentityGraphV1,
    projection: PublicIdentityProjectionV1,
    *,
    source_set_hash: str,
    source_references: Sequence[IdentitySourceReferenceV1 | Mapping[str, Any]],
    private_graph_path: str = IDENTITY_GRAPH_PATH,
    public_projection_path: str = IDENTITY_PUBLIC_PATH,
) -> IdentityStageEvidenceV1:
    """Build canonical identity-stage evidence without writing files."""
    assert_identity_count_consistency(graph, projection)
    references = [
        item
        if isinstance(item, IdentitySourceReferenceV1)
        else IdentitySourceReferenceV1.model_validate(item)
        for item in source_references
    ]
    return IdentityStageEvidenceV1(
        run_id=graph.run_id,
        built_at=graph.built_at,
        source_set_hash=source_set_hash,
        private_graph_path=private_graph_path,
        private_graph_sha256=sha256_bytes(graph.to_canonical_json()),
        public_projection_path=public_projection_path,
        public_projection_sha256=sha256_bytes(projection.to_canonical_json()),
        resolved_customer_count=len(graph.customers),
        ambiguous_cluster_count=len(graph.ambiguous_identities),
        quarantined_member_count=sum(
            len(cluster.member_node_ids) for cluster in graph.ambiguous_identities
        ),
        match_rule_counts=_match_rule_counts(graph),
        source_references=references,
    )


def run_identity_stage(
    source_set: SourceSetManifestV1 | Mapping[str, Any] | bytes | bytearray,
    payloads: Mapping[str, Mapping[str, Any] | bytes | bytearray],
    *,
    output_root: Path | str,
    run_id: str,
    built_at: datetime | None = None,
    write_private_graph: bool = True,
    override: IdentityOverrideEnvelopeV1 | None = None,
) -> IdentityStageResult:
    """Normalize, join, optionally override, and atomically commit identity artifacts."""
    if isinstance(source_set, SourceSetManifestV1):
        parsed = source_set
    elif isinstance(source_set, Mapping):
        try:
            parsed = SourceSetManifestV1.model_validate(dict(source_set))
        except (TypeError, ValueError) as exc:
            raise IdentityStageError(str(exc)) from exc
    elif isinstance(source_set, (bytes, bytearray)):
        try:
            loaded = json.loads(
                bytes(source_set).decode("utf-8"), object_pairs_hook=_object_pairs_hook
            )
            parsed = SourceSetManifestV1.model_validate(loaded)
        except (TypeError, ValueError, UnicodeDecodeError) as exc:
            raise IdentityStageError(str(exc)) from exc
    else:
        raise IdentityStageError("source-set must be a manifest, object, or JSON bytes")

    final_root = _resolve_output_root(output_root)
    stage_parent = final_root.parent
    when = built_at or DEFAULT_BUILT_AT
    try:
        decoded, verified = verify_source_set_payloads(parsed, payloads)
        nodes = _nodes_from_verified_payloads(parsed, decoded)
        graph = build_identity_graph(nodes, run_id=run_id, built_at=when)
        if override is not None:
            graph = apply_identity_override(graph, override)
        projection = public_identity_projection(graph)
        evidence = build_identity_stage_evidence(
            graph,
            projection,
            source_set_hash=parsed.source_set_hash,
            source_references=_source_references(graph, parsed, verified),
        )
        validate_identity_stage_bundle(
            graph=graph,
            projection=projection,
            evidence=evidence,
            source_set=parsed,
            payloads=payloads,
        )
    except IdentityStageError:
        raise
    except (TypeError, ValueError) as exc:
        raise IdentityStageError(str(exc)) from exc

    stage_root = Path(tempfile.mkdtemp(prefix=".found-money-identity-stage-", dir=stage_parent))
    try:
        if write_private_graph:
            _atomic_write_bytes(
                _validate_relative_under_root(stage_root, IDENTITY_GRAPH_PATH),
                graph.to_canonical_json(),
            )
        _atomic_write_bytes(
            _validate_relative_under_root(stage_root, IDENTITY_PUBLIC_PATH),
            projection.to_canonical_json(),
        )
        _atomic_write_bytes(
            _validate_relative_under_root(stage_root, IDENTITY_STAGE_PATH),
            evidence.to_canonical_json(),
        )
        if final_root.exists():
            raise IdentityStageError("identity-stage output root appeared before commit")
        os.replace(stage_root, final_root)
        return IdentityStageResult(
            output_root=final_root,
            graph=graph,
            projection=projection,
            evidence=evidence,
        )
    except IdentityStageError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise IdentityStageError("identity stage failed before commit") from exc
    finally:
        if stage_root.exists():
            shutil.rmtree(stage_root, ignore_errors=True)


__all__ = [
    "DEFAULT_BUILT_AT",
    "IDENTITY_GRAPH_PATH",
    "IDENTITY_PUBLIC_PATH",
    "IDENTITY_STAGE_PATH",
    "IdentityStageError",
    "IdentityStageResult",
    "assert_identity_count_consistency",
    "build_identity_stage_evidence",
    "run_identity_stage",
    "validate_identity_stage_bundle",
]
