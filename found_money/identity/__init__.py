"""Deterministic exact-join identity graph for fixture sources."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from found_money.contracts.identity import (
    AmbiguousIdentityClusterV1,
    AmbiguousReason,
    CustomerClusterV1,
    IdentityEdgeV1,
    IdentityGraphV1,
    IdentityNodeV1,
    IdentityOverrideEnvelopeV1,
    MatchRule,
    PublicCustomerProjectionV1,
    PublicIdentityProjectionV1,
    ambiguous_cluster_hash,
    normalize_e164,
    normalize_email,
    override_envelope_integrity,
)
from found_money.contracts.run import compute_source_set_hash
from found_money.contracts.source_stage import SourceSetManifestV1

SYNTHETIC_CUSTOMER_NAMESPACE = "found_money.synthetic_customer"
IDENTITY_OBJECT_TYPES = frozenset({"contact", "customer"})
_NATIVE_IDENTITY_KINDS = frozenset({"hubspot", "stripe"})
_FILE_IDENTITY_KINDS = frozenset({"orders", "appointments", "proposals"})
_CSV_JSON_ALIASES = frozenset(
    {"csv", "json", "universal-csv", "universal-json", "universal_csv", "universal_json"}
)
_UNSUPPORTED_IDENTITY_FIELDS = frozenset(
    {
        "fuzzy_score",
        "name_similarity",
        "company_similarity",
        "domain_match",
        "address_match",
        "match_confidence",
        "probabilistic_match",
        "phonetic_name",
        "fuzzy_name",
    }
)
FM039_MATRIX_ROOT = (
    Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "saas" / "identity" / "fm039"
)

_TRAVERSAL_RE = re.compile(r"(^|/)\.\.(/|$)")
_WINDOWS_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")
_SCHEMA_MAJOR_RE = re.compile(r"^(?P<name>.+)\.v(?P<major>\d+)$")

# Stronger keys block weaker auto-merges when they disagree.
_KEY_STRENGTH = {
    "same_source_object_id": 4,
    "declared_external_id": 3,
    "unique_exact_email": 2,
    "unique_exact_e164_phone": 1,
}
_AMBIGUOUS_REASON_ORDER: tuple[AmbiguousReason, ...] = (
    "household_email",
    "recycled_phone",
    "conflicting_stronger_identifier",
)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def node_id_for(source_system: str, object_type: str, source_id: str) -> str:
    return f"{source_system}:{object_type}:{source_id}"


def customer_token_for(member_node_ids: Sequence[str]) -> str:
    digest = sha256_bytes("\n".join(sorted(member_node_ids)).encode("utf-8"))
    return f"cust_{digest[:12]}"


def _observed_at(raw: Any, default: datetime) -> datetime:
    if raw is None:
        return default
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    text = str(raw).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)


def _map_external_ids(raw: Mapping[str, Any]) -> dict[str, str]:
    external: dict[str, str] = {}
    if "synthetic_customer_key" in raw and raw["synthetic_customer_key"] is not None:
        external[SYNTHETIC_CUSTOMER_NAMESPACE] = str(raw["synthetic_customer_key"]).strip()
    nested = raw.get("external_ids")
    if isinstance(nested, Mapping):
        for key, value in nested.items():
            if value is None:
                continue
            external[str(key).strip()] = str(value).strip()
    return {k: v for k, v in external.items() if k and v}


def _optional_email(raw: Mapping[str, Any]) -> str | None:
    value = raw.get("email")
    if value is None and isinstance(raw.get("properties"), Mapping):
        value = raw["properties"].get("email")
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return normalize_email(text)


def _optional_phone(raw: Mapping[str, Any]) -> str | None:
    value = raw.get("phone")
    if value is None and isinstance(raw.get("properties"), Mapping):
        value = raw["properties"].get("phone")
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return normalize_e164(text)


def _optional_name(raw: Mapping[str, Any]) -> str | None:
    for key in ("display_name", "name"):
        if raw.get(key):
            return str(raw[key]).strip() or None
    props = raw.get("properties")
    if isinstance(props, Mapping):
        for key in ("display_name", "name", "firstname"):
            if props.get(key):
                return str(props[key]).strip() or None
    return None


def _reject_unsupported_identity_fields(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if isinstance(key, str) and key.strip().lower() in _UNSUPPORTED_IDENTITY_FIELDS:
                raise ValueError(f"unsupported identity field: {key}")
            _reject_unsupported_identity_fields(nested)
    elif isinstance(value, list):
        for item in value:
            _reject_unsupported_identity_fields(item)


def _require_mapping(value: Any, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return value


def _decode_identity_payload(raw: Mapping[str, Any] | bytes | bytearray) -> Mapping[str, Any]:
    if isinstance(raw, Mapping):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        try:
            payload = json.loads(bytes(raw).decode("utf-8"), object_pairs_hook=_object_pairs_hook)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"identity source payload is malformed JSON: {exc}") from exc
        if not isinstance(payload, Mapping):
            raise ValueError("identity source payload root must be an object")
        return payload
    raise TypeError("identity source payload must be an object or JSON bytes")


def _infer_kind_from_payload(payload: Mapping[str, Any]) -> str | None:
    schema = str(payload.get("schema_version") or payload.get("schema") or "").strip().lower()
    schema_name = schema.split(".", 1)[0] if schema else ""
    flags = {
        "orders": "orders" in payload or schema_name == "orders",
        "appointments": "appointments" in payload or schema_name == "appointments",
        "proposals": "proposals" in payload or schema_name == "proposals",
        "hubspot": "contacts" in payload or schema_name.startswith("hubspot"),
        "stripe": "customers" in payload or schema_name.startswith("stripe"),
    }
    hits = [name for name, present in flags.items() if present]
    if len(hits) > 1:
        raise ValueError("identity source payload kind is ambiguous")
    if len(hits) == 1:
        return hits[0]
    return None


def _payload_kind(payload: Mapping[str, Any], *, declared: str | None) -> str:
    inferred = _infer_kind_from_payload(payload)
    if declared in _NATIVE_IDENTITY_KINDS or declared in _FILE_IDENTITY_KINDS:
        if inferred is not None and inferred != declared:
            raise ValueError(
                f"identity payload kind {inferred} does not match declared source type {declared}"
            )
        return declared
    if declared in _CSV_JSON_ALIASES or declared is None:
        if inferred in _FILE_IDENTITY_KINDS:
            return inferred
        raise ValueError("csv/json identity payload must be orders, appointments, or proposals")
    raise ValueError(f"unsupported identity source type: {declared}")


def _node_from_record(
    *,
    source_system: str,
    object_type: str,
    record: Mapping[str, Any],
    default_observed: datetime,
) -> IdentityNodeV1:
    _reject_unsupported_identity_fields(record)
    raw_id = record.get("id")
    if raw_id is None or not str(raw_id).strip():
        raise ValueError("identity source record is missing a source id")
    source_id = str(raw_id).strip()
    payload = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    raw_props = record.get("properties")
    props: Mapping[str, Any] = raw_props if isinstance(raw_props, Mapping) else {}
    merged: dict[str, Any] = {**dict(record), **dict(props)}
    observed_candidates = (
        merged.get("observed_at"),
        merged.get("last_activity_at"),
        merged.get("attempted_at"),
        merged.get("paid_at"),
        merged.get("closed_at"),
        merged.get("created"),
    )
    observed = default_observed
    for candidate in observed_candidates:
        if candidate:
            observed = _observed_at(candidate, default_observed)
            break
    return IdentityNodeV1(
        node_id=node_id_for(source_system, object_type, source_id),
        source_system=source_system,
        object_type=object_type,
        source_id=source_id,
        observed_at=observed,
        payload_hash=sha256_bytes(payload.encode("utf-8")),
        email=_optional_email(merged),
        phone=_optional_phone(merged),
        external_ids=_map_external_ids(merged),
        display_name=_optional_name(merged),
    )


def _customer_nodes_from_rows(
    rows: Sequence[Any],
    *,
    source_system: str,
    namespace: str,
    observed_key: str,
    default_observed: datetime,
) -> list[IdentityNodeV1]:
    nodes: list[IdentityNodeV1] = []
    seen_customers: set[str] = set()
    for index, row in enumerate(rows, start=1):
        record = _require_mapping(row, label=f"{namespace} row {index}")
        _reject_unsupported_identity_fields(record)
        customer_id = str(record.get("customer_id") or "").strip()
        if not customer_id:
            raise ValueError(f"{namespace} row {index} is missing a customer_id")
        if customer_id in seen_customers:
            continue
        seen_customers.add(customer_id)
        node = _node_from_record(
            source_system=source_system,
            object_type="customer",
            record={
                "id": customer_id,
                "external_ids": {namespace: customer_id},
                "observed_at": record.get(observed_key),
            },
            default_observed=default_observed,
        )
        payload = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        nodes.append(
            node.model_copy(update={"payload_hash": sha256_bytes(payload.encode("utf-8"))})
        )
    return nodes


def _nodes_from_kind(
    kind: str,
    *,
    source_system: str,
    payload: Mapping[str, Any],
    default_observed: datetime,
) -> list[IdentityNodeV1]:
    _reject_unsupported_identity_fields(payload)
    if kind == "hubspot":
        nodes: list[IdentityNodeV1] = []
        for index, contact in enumerate(payload.get("contacts") or [], start=1):
            nodes.append(
                _node_from_record(
                    source_system=source_system,
                    object_type="contact",
                    record=_require_mapping(contact, label=f"hubspot contact {index}"),
                    default_observed=default_observed,
                )
            )
        for index, deal in enumerate(payload.get("deals") or [], start=1):
            nodes.append(
                _node_from_record(
                    source_system=source_system,
                    object_type="deal",
                    record=_require_mapping(deal, label=f"hubspot deal {index}"),
                    default_observed=default_observed,
                )
            )
        return nodes
    if kind == "stripe":
        nodes = []
        for index, customer in enumerate(payload.get("customers") or [], start=1):
            nodes.append(
                _node_from_record(
                    source_system=source_system,
                    object_type="customer",
                    record=_require_mapping(customer, label=f"stripe customer {index}"),
                    default_observed=default_observed,
                )
            )
        for index, invoice in enumerate(payload.get("invoices") or [], start=1):
            nodes.append(
                _node_from_record(
                    source_system=source_system,
                    object_type="invoice",
                    record=_require_mapping(invoice, label=f"stripe invoice {index}"),
                    default_observed=default_observed,
                )
            )
        return nodes
    if kind == "orders":
        return _customer_nodes_from_rows(
            payload.get("orders") or [],
            source_system=source_system,
            namespace="orders.customer_id",
            observed_key="ordered_at",
            default_observed=default_observed,
        )
    if kind == "appointments":
        return _customer_nodes_from_rows(
            payload.get("appointments") or [],
            source_system=source_system,
            namespace="appointments.customer_id",
            observed_key="scheduled_at",
            default_observed=default_observed,
        )
    if kind == "proposals":
        return _customer_nodes_from_rows(
            payload.get("proposals") or [],
            source_system=source_system,
            namespace="proposals.customer_id",
            observed_key="proposed_at",
            default_observed=default_observed,
        )
    raise ValueError(f"unsupported identity source type: {kind}")


def _finalize_nodes(nodes: Sequence[IdentityNodeV1]) -> list[IdentityNodeV1]:
    counts: dict[str, int] = {}
    seen_payloads: set[tuple[str, str, str, str]] = set()
    out: list[IdentityNodeV1] = []
    for node in nodes:
        fingerprint = (node.source_system, node.object_type, node.source_id, node.payload_hash)
        if fingerprint in seen_payloads:
            raise ValueError("duplicate identity source records")
        seen_payloads.add(fingerprint)
        base = node_id_for(node.source_system, node.object_type, node.source_id)
        counts[base] = counts.get(base, 0) + 1
        node_id = base if counts[base] == 1 else f"{base}:{counts[base]}"
        out.append(
            node if node.node_id == node_id else node.model_copy(update={"node_id": node_id})
        )
    ids = [item.node_id for item in out]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate identity node_id values")
    out.sort(key=lambda item: item.node_id)
    return out


def normalize_source_records(
    snapshots: Mapping[str, Mapping[str, Any] | bytes | bytearray],
    *,
    default_observed_at: datetime | None = None,
) -> list[IdentityNodeV1]:
    """Map HubSpot/Stripe/file snapshots into typed identity nodes."""
    when = default_observed_at or datetime(2026, 7, 29, 18, 0, 0, tzinfo=timezone.utc)
    nodes: list[IdentityNodeV1] = []
    for declared, raw in snapshots.items():
        if raw is None:
            continue
        payload = _decode_identity_payload(raw)
        kind = _payload_kind(payload, declared=str(declared).strip().lower() or None)
        nodes.extend(
            _nodes_from_kind(
                kind,
                source_system=str(declared).strip(),
                payload=payload,
                default_observed=when,
            )
        )
    return _finalize_nodes(nodes)


def canonical_identity_payload_bytes(raw: Mapping[str, Any] | bytes | bytearray) -> bytes:
    """Return the exact consumed bytes, or the documented canonical encoding for mappings."""
    if isinstance(raw, (bytes, bytearray)):
        return bytes(raw)
    if isinstance(raw, Mapping):
        return (
            json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
    raise TypeError("identity source payload must be an object or JSON bytes")


def _parse_source_set(
    source_set: SourceSetManifestV1 | Mapping[str, Any] | bytes | bytearray,
) -> SourceSetManifestV1:
    if isinstance(source_set, SourceSetManifestV1):
        return source_set
    if isinstance(source_set, (bytes, bytearray)):
        try:
            loaded = json.loads(
                bytes(source_set).decode("utf-8"), object_pairs_hook=_object_pairs_hook
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"source-set is malformed JSON: {exc}") from exc
        return SourceSetManifestV1.model_validate(loaded)
    if isinstance(source_set, Mapping):
        return SourceSetManifestV1.model_validate(dict(source_set))
    raise TypeError("source_set must be a manifest, object, or JSON bytes")


def verify_source_set_payloads(
    source_set: SourceSetManifestV1 | Mapping[str, Any] | bytes | bytearray,
    payloads: Mapping[str, Mapping[str, Any] | bytes | bytearray],
) -> tuple[dict[str, Mapping[str, Any]], dict[str, str]]:
    """Verify consumed payload bytes against source-set content and set hashes."""
    parsed = _parse_source_set(source_set)
    expected = {entry.source_id for entry in parsed.sources}
    incoming = {str(source_id).strip() for source_id in payloads}
    if incoming != expected:
        raise ValueError("identity payloads must match the source-set exactly")
    decoded: dict[str, Mapping[str, Any]] = {}
    verified_by_id: dict[str, str] = {}
    verified_by_receipt: dict[str, str] = {}
    for entry in parsed.sources:
        raw = payloads[entry.source_id]
        consumed = canonical_identity_payload_bytes(raw)
        digest = sha256_bytes(consumed)
        if digest != entry.content_hash:
            raise ValueError(f"identity payload content_hash mismatch for source {entry.source_id}")
        decoded[entry.source_id] = _decode_identity_payload(
            raw if isinstance(raw, Mapping) else consumed
        )
        verified_by_id[entry.source_id] = digest
        verified_by_receipt[entry.receipt_path] = digest
    recomputed = compute_source_set_hash(verified_by_receipt)
    if recomputed != parsed.source_set_hash:
        raise ValueError("source_set_hash does not match verified payload content hashes")
    return decoded, verified_by_id


def _nodes_from_verified_payloads(
    parsed: SourceSetManifestV1,
    decoded: Mapping[str, Mapping[str, Any]],
    *,
    default_observed_at: datetime | None = None,
) -> list[IdentityNodeV1]:
    when = default_observed_at or datetime(2026, 7, 29, 18, 0, 0, tzinfo=timezone.utc)
    nodes: list[IdentityNodeV1] = []
    for entry in sorted(parsed.sources, key=lambda item: item.source_id):
        payload = decoded[entry.source_id]
        kind = _payload_kind(payload, declared=entry.source_type)
        nodes.extend(
            _nodes_from_kind(
                kind,
                source_system=entry.source_id,
                payload=payload,
                default_observed=when,
            )
        )
    return _finalize_nodes(nodes)


def normalize_source_set(
    source_set: SourceSetManifestV1 | Mapping[str, Any] | bytes | bytearray,
    payloads: Mapping[str, Mapping[str, Any] | bytes | bytearray],
    *,
    default_observed_at: datetime | None = None,
) -> list[IdentityNodeV1]:
    """Map a FM-021 source-set and its verified normalized payloads into identity nodes."""
    parsed = _parse_source_set(source_set)
    decoded, _verified = verify_source_set_payloads(parsed, payloads)
    return _nodes_from_verified_payloads(parsed, decoded, default_observed_at=default_observed_at)


def load_thin_slice_snapshots(root: Path | None = None) -> dict[str, Any]:
    base = root or (
        Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "saas" / "thin-slice"
    )
    return {
        "hubspot": json.loads((base / "hubspot" / "snapshot.json").read_text(encoding="utf-8")),
        "stripe": json.loads((base / "stripe" / "snapshot.json").read_text(encoding="utf-8")),
    }


def load_fm039_matrix_snapshots(root: Path | None = None) -> dict[str, Any]:
    base = root or (FM039_MATRIX_ROOT / "matrix")
    return {
        "hubspot": json.loads((base / "hubspot.json").read_text(encoding="utf-8")),
        "stripe": json.loads((base / "stripe.json").read_text(encoding="utf-8")),
    }


class _UnionFind:
    def __init__(self, ids: Iterable[str]) -> None:
        self.parent = {item: item for item in ids}

    def find(self, item: str) -> str:
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, left: str, right: str) -> None:
        a, b = self.find(left), self.find(right)
        if a == b:
            return
        if a < b:
            self.parent[b] = a
        else:
            self.parent[a] = b


def _merged_external_ids(nodes: Sequence[IdentityNodeV1]) -> dict[str, str]:
    merged: dict[str, str] = {}
    for node in nodes:
        for namespace, value in node.external_ids.items():
            prior = merged.get(namespace)
            if prior is not None and prior != value:
                # Internally inconsistent component; treat as the first value and
                # let pairwise conflict checks block further merges.
                continue
            merged[namespace] = value
    return merged


def _external_id_values(nodes: Sequence[IdentityNodeV1]) -> dict[str, set[str]]:
    values: dict[str, set[str]] = {}
    for node in nodes:
        for namespace, value in node.external_ids.items():
            values.setdefault(namespace, set()).add(value)
    return values


def _has_conflicting_external_ids(nodes: Sequence[IdentityNodeV1]) -> bool:
    return any(len(values) > 1 for values in _external_id_values(nodes).values())


def _conflicts_stronger(
    left_nodes: Sequence[IdentityNodeV1],
    right_nodes: Sequence[IdentityNodeV1],
    rule: MatchRule,
) -> bool:
    """Block a weaker join when both sides already disagree on a stronger key.

    Distinct same-source object IDs are expected across systems and do not count
    as a conflict. Conflicting declared external IDs (same namespace, different
    value), emails, or phones do.
    """
    strength = _KEY_STRENGTH[rule]

    if strength < _KEY_STRENGTH["declared_external_id"]:
        if _declared_external_id_conflict(left_nodes, right_nodes):
            return True

    if strength < _KEY_STRENGTH["unique_exact_email"]:
        left_emails = {node.email for node in left_nodes if node.email}
        right_emails = {node.email for node in right_nodes if node.email}
        if left_emails and right_emails and left_emails.isdisjoint(right_emails):
            return True

    if strength < _KEY_STRENGTH["unique_exact_e164_phone"]:
        left_phones = {node.phone for node in left_nodes if node.phone}
        right_phones = {node.phone for node in right_nodes if node.phone}
        if left_phones and right_phones and left_phones.isdisjoint(right_phones):
            return True

    return False


def _candidate_groups(
    nodes: Sequence[IdentityNodeV1],
) -> list[tuple[MatchRule, str | None, str, list[IdentityNodeV1]]]:
    groups: list[tuple[MatchRule, str | None, str, list[IdentityNodeV1]]] = []

    by_source: dict[str, list[IdentityNodeV1]] = {}
    for node in nodes:
        key = f"{node.source_system}:{node.object_type}:{node.source_id}"
        by_source.setdefault(key, []).append(node)
    for key, members in by_source.items():
        if len(members) >= 2:
            groups.append(("same_source_object_id", None, key, members))

    by_external: dict[tuple[str, str], list[IdentityNodeV1]] = {}
    for node in nodes:
        for namespace, value in node.external_ids.items():
            by_external.setdefault((namespace, value), []).append(node)
    for (namespace, value), members in by_external.items():
        # Declared external IDs are intentional shared keys across systems; any
        # cardinality >= 2 is an auto-merge candidate (subject to stronger conflicts).
        if len(members) >= 2:
            groups.append(("declared_external_id", namespace, value, members))

    by_email: dict[str, list[IdentityNodeV1]] = {}
    for node in nodes:
        if node.email:
            by_email.setdefault(node.email, []).append(node)
    for email, members in by_email.items():
        # unique_exact_email: auto-merge only when exactly one matching pair shares
        # the address. Three or more eligible parties are quarantined separately.
        if len(members) == 2:
            groups.append(("unique_exact_email", None, email, members))

    by_phone: dict[str, list[IdentityNodeV1]] = {}
    for node in nodes:
        if node.phone:
            by_phone.setdefault(node.phone, []).append(node)
    for phone, members in by_phone.items():
        # unique_exact_e164_phone: same exact-pair cardinality guard as email.
        if len(members) == 2:
            groups.append(("unique_exact_e164_phone", None, phone, members))

    return groups


def _declared_external_id_conflict(
    left_nodes: Sequence[IdentityNodeV1],
    right_nodes: Sequence[IdentityNodeV1],
) -> bool:
    if _has_conflicting_external_ids(left_nodes) or _has_conflicting_external_ids(right_nodes):
        return True
    left_ext = _merged_external_ids(left_nodes)
    right_ext = _merged_external_ids(right_nodes)
    for namespace, left_value in left_ext.items():
        right_value = right_ext.get(namespace)
        if right_value is not None and right_value != left_value:
            return True
    return False


def _coalesce_ambiguous_candidates(
    candidates: Iterable[tuple[Sequence[str], Iterable[AmbiguousReason]]],
) -> list[AmbiguousIdentityClusterV1]:
    """Union overlapping quarantine keys while retaining every reason."""
    normalized: list[tuple[tuple[str, ...], tuple[AmbiguousReason, ...]]] = []
    for member_node_ids, reasons in candidates:
        members = tuple(sorted({str(item) for item in member_node_ids}))
        reason_set = set(reasons)
        if not members or not reason_set:
            continue
        ordered_reason_tuple = tuple(
            reason for reason in _AMBIGUOUS_REASON_ORDER if reason in reason_set
        )
        normalized.append((members, ordered_reason_tuple))

    components: list[tuple[set[str], set[AmbiguousReason]]] = []
    for members, reasons in sorted(normalized):
        member_set = set(members)
        overlapping = [
            component for component in components if component[0].intersection(member_set)
        ]
        if not overlapping:
            components.append((member_set, set(reasons)))
            continue

        merged_members = member_set
        merged_reasons = set(reasons)
        for component in overlapping:
            merged_members.update(component[0])
            merged_reasons.update(component[1])
            components.remove(component)
        components.append((merged_members, merged_reasons))

    clusters: list[AmbiguousIdentityClusterV1] = []
    for component_members, component_reasons in components:
        component_ordered_reasons = [
            reason for reason in _AMBIGUOUS_REASON_ORDER if reason in component_reasons
        ]
        clusters.append(
            _make_ambiguous_cluster(
                sorted(component_members),
                component_ordered_reasons[0],
            )
        )
    clusters.sort(key=lambda cluster: (cluster.reason, cluster.cluster_hash))
    return clusters


def _make_ambiguous_cluster(
    member_node_ids: Sequence[str],
    reason: AmbiguousReason,
) -> AmbiguousIdentityClusterV1:
    members = sorted({str(item) for item in member_node_ids})
    return AmbiguousIdentityClusterV1(
        member_node_ids=members,
        reason=reason,
        cluster_hash=ambiguous_cluster_hash(members, reason),
    )


def _collect_quarantine_seeds(
    eligible: Sequence[IdentityNodeV1],
    union_find: _UnionFind,
) -> list[tuple[Sequence[str], Iterable[AmbiguousReason]]]:
    """Collect ambiguous channel keys after stronger components are formed."""
    candidates: list[tuple[Sequence[str], Iterable[AmbiguousReason]]] = []

    by_component: dict[str, list[IdentityNodeV1]] = {}
    for node in eligible:
        by_component.setdefault(union_find.find(node.node_id), []).append(node)

    def collect_channel_seeds(
        groups: Mapping[str, list[IdentityNodeV1]],
        reason: AmbiguousReason,
    ) -> None:
        for value in sorted(groups):
            members = groups[value]
            component_roots = sorted({union_find.find(node.node_id) for node in members})
            if len(component_roots) >= 3:
                candidates.append((sorted(node.node_id for node in members), (reason,)))
                continue

            if len(members) < 3 or len(component_roots) != 2:
                continue
            left_component = by_component[component_roots[0]]
            right_component = by_component[component_roots[1]]
            if _declared_external_id_conflict(left_component, right_component):
                candidates.append(
                    (
                        sorted(node.node_id for node in members),
                        ("conflicting_stronger_identifier",),
                    )
                )
            else:
                candidates.append((sorted(node.node_id for node in members), (reason,)))

    by_email: dict[str, list[IdentityNodeV1]] = {}
    by_phone: dict[str, list[IdentityNodeV1]] = {}
    for node in eligible:
        if node.email:
            by_email.setdefault(node.email, []).append(node)
        if node.phone:
            by_phone.setdefault(node.phone, []).append(node)

    collect_channel_seeds(by_email, "household_email")
    collect_channel_seeds(by_phone, "recycled_phone")

    return candidates


def build_identity_graph(
    nodes: Sequence[IdentityNodeV1],
    *,
    run_id: str,
    built_at: datetime | None = None,
) -> IdentityGraphV1:
    """Build an exact-join identity graph from normalized nodes."""
    when = built_at or datetime(2026, 7, 29, 18, 0, 5, tzinfo=timezone.utc)
    all_nodes = sorted(nodes, key=lambda node: node.node_id)
    eligible = [node for node in all_nodes if node.object_type in IDENTITY_OBJECT_TYPES]
    uf = _UnionFind(node.node_id for node in eligible)
    by_id = {node.node_id: node for node in eligible}
    edges: list[IdentityEdgeV1] = []
    quarantine_seeds: list[tuple[Sequence[str], Iterable[AmbiguousReason]]] = []

    # Process stronger rules first so conflicts block weaker joins.
    ordered = sorted(
        _candidate_groups(eligible),
        key=lambda item: (-_KEY_STRENGTH[item[0]], item[0], item[2], item[1] or ""),
    )
    for rule, namespace, value, members in ordered:
        member_ids = sorted({node.node_id for node in members})
        for index, left_id in enumerate(member_ids):
            for right_id in member_ids[index + 1 :]:
                left_root, right_root = uf.find(left_id), uf.find(right_id)
                if left_root == right_root:
                    continue
                left_component = [by_id[i] for i in by_id if uf.find(i) == left_root]
                right_component = [by_id[i] for i in by_id if uf.find(i) == right_root]
                if _conflicts_stronger(left_component, right_component, rule):
                    if rule in {"unique_exact_email", "unique_exact_e164_phone"}:
                        quarantine_seeds.append(
                            (
                                sorted(node.node_id for node in left_component + right_component),
                                ("conflicting_stronger_identifier",),
                            )
                        )
                    continue
                uf.union(left_id, right_id)
                edges.append(
                    IdentityEdgeV1(
                        left_node_id=left_id,
                        right_node_id=right_id,
                        match_rule=rule,
                        match_namespace=namespace,
                        match_value=value,
                        lineage={
                            "left_source": f"{by_id[left_id].source_system}:{by_id[left_id].source_id}",
                            "right_source": (
                                f"{by_id[right_id].source_system}:{by_id[right_id].source_id}"
                            ),
                        },
                    )
                )

    quarantine_seeds.extend(_collect_quarantine_seeds(eligible, uf))

    components: dict[str, list[str]] = {}
    for node in eligible:
        components.setdefault(uf.find(node.node_id), []).append(node.node_id)
    for member_ids in components.values():
        member_ids.sort()

    for member_ids in components.values():
        component_nodes = [by_id[node_id] for node_id in member_ids]
        if _has_conflicting_external_ids(component_nodes):
            quarantine_seeds.append((member_ids, ("conflicting_stronger_identifier",)))

    def expand_to_components(member_node_ids: Sequence[str]) -> list[str]:
        expanded: set[str] = set()
        for node_id in member_node_ids:
            expanded.update(components[uf.find(node_id)])
        return sorted(expanded)

    quarantine_candidates = [
        (expand_to_components(member_node_ids), reasons)
        for member_node_ids, reasons in quarantine_seeds
    ]
    ambiguous_identities = _coalesce_ambiguous_candidates(quarantine_candidates)
    quarantined_ids = {
        node_id for cluster in ambiguous_identities for node_id in cluster.member_node_ids
    }
    customers = [
        CustomerClusterV1(
            customer_token=customer_token_for(member_ids),
            member_node_ids=member_ids,
        )
        for member_ids in components.values()
        if not set(member_ids).intersection(quarantined_ids)
    ]
    customers.sort(key=lambda cluster: cluster.customer_token)
    edges.sort(key=lambda edge: (edge.left_node_id, edge.right_node_id, edge.match_rule))
    return IdentityGraphV1(
        run_id=run_id,
        built_at=when,
        nodes=list(all_nodes),
        edges=edges,
        customers=customers,
        ambiguous_identities=ambiguous_identities,
    )


def make_identity_override(
    *,
    target_cluster_hash: str,
    member_node_ids: Sequence[str],
    decided_by: str,
    decided_at: datetime | str,
    resolution: str = "merge_members",
) -> IdentityOverrideEnvelopeV1:
    """Build a canonical integrity-bound identity override envelope."""
    from found_money.contracts.identity import _format_utc, _parse_utc

    members = sorted({str(item).strip() for item in member_node_ids if str(item).strip()})
    when = decided_at if isinstance(decided_at, datetime) else _parse_utc(decided_at)
    body = {
        "schema_version": "identity-override.v1",
        "target_cluster_hash": target_cluster_hash.strip().lower(),
        "member_node_ids": members,
        "resolution": resolution,
        "decided_by": str(decided_by).strip(),
        "decided_at": _format_utc(when),
    }
    integrity = override_envelope_integrity(body)
    return IdentityOverrideEnvelopeV1.model_validate({**body, "integrity": integrity})


def parse_identity_override(data: bytes) -> IdentityOverrideEnvelopeV1:
    """Parse canonical identity-override.v1 bytes (fail closed on non-canonical)."""
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("identity override must be bytes")
    try:
        payload = json.loads(bytes(data).decode("utf-8"), object_pairs_hook=_object_pairs_hook)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"malformed JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("identity override root must be an object")
    model = IdentityOverrideEnvelopeV1.model_validate(payload)
    if model.to_canonical_json() != bytes(data):
        raise ValueError("serialized bytes are not exactly canonical")
    expected = override_envelope_integrity(model.canonical_dict())
    if model.integrity != expected:
        raise ValueError("integrity does not match canonical envelope body")
    return model


def apply_identity_override(
    graph: IdentityGraphV1,
    override: IdentityOverrideEnvelopeV1,
) -> IdentityGraphV1:
    """Resolve one ambiguous cluster via merge_members; leave others untouched."""
    if override.resolution != "merge_members":
        raise ValueError('resolution must be exactly "merge_members"')
    expected_integrity = override_envelope_integrity(override.canonical_dict())
    if override.integrity != expected_integrity:
        raise ValueError("integrity does not match canonical envelope body")

    target: AmbiguousIdentityClusterV1 | None = None
    for cluster in graph.ambiguous_identities:
        if cluster.cluster_hash == override.target_cluster_hash:
            target = cluster
            break
    if target is None:
        raise ValueError("target ambiguous cluster is absent")
    if list(target.member_node_ids) != list(override.member_node_ids):
        raise ValueError("member_node_ids do not match target cluster")
    if set(override.member_node_ids) != set(target.member_node_ids):
        raise ValueError("member_node_ids do not match target cluster")

    remaining = [
        cluster
        for cluster in graph.ambiguous_identities
        if cluster.cluster_hash != target.cluster_hash
    ]
    resolved = CustomerClusterV1(
        customer_token=customer_token_for(target.member_node_ids),
        member_node_ids=list(target.member_node_ids),
    )
    customers = sorted(
        [*graph.customers, resolved],
        key=lambda cluster: cluster.customer_token,
    )
    return IdentityGraphV1(
        schema_version=graph.schema_version,
        run_id=graph.run_id,
        built_at=graph.built_at,
        nodes=list(graph.nodes),
        edges=list(graph.edges),
        customers=customers,
        ambiguous_identities=remaining,
    )


def public_identity_projection(graph: IdentityGraphV1) -> PublicIdentityProjectionV1:
    customers = [
        PublicCustomerProjectionV1(
            customer_token=cluster.customer_token,
            member_count=len(cluster.member_node_ids),
        )
        for cluster in graph.customers
    ]
    customers.sort(key=lambda item: item.customer_token)
    return PublicIdentityProjectionV1(
        run_id=graph.run_id,
        built_at=graph.built_at,
        customer_count=len(customers),
        customers=customers,
    )


def _validate_relative_under_root(output_root: Path, relative_path: str) -> Path:
    if not isinstance(relative_path, str):
        raise ValueError("relative path must be a string")
    raw = relative_path.strip().replace("\\", "/")
    if not raw:
        raise ValueError("relative path must be non-empty")
    if raw.startswith("/") or _WINDOWS_ABS_RE.match(raw) or raw.startswith("~/"):
        raise ValueError("absolute paths are rejected")
    if raw.startswith("./"):
        raw = raw[2:]
    if not raw or _TRAVERSAL_RE.search(raw) or raw == ".." or Path(raw).is_absolute():
        raise ValueError("traversal or absolute paths are rejected")
    root = output_root.expanduser().resolve(strict=False)
    candidate = root.joinpath(*Path(raw).parts)
    try:
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise ValueError("path escapes the caller-owned output root") from exc
    probe = root
    for part in Path(raw).parts[:-1]:
        probe = probe / part
        if probe.is_symlink():
            try:
                probe.resolve(strict=True).relative_to(root)
            except (OSError, ValueError) as exc:
                raise ValueError("symlink escapes the caller-owned output root") from exc
        if probe.exists() and not probe.is_dir():
            raise ValueError("parent path component is not a directory")
    if candidate.exists() and candidate.is_symlink():
        try:
            candidate.resolve(strict=True).relative_to(root)
        except (OSError, ValueError) as exc:
            raise ValueError("symlink escapes the caller-owned output root") from exc
    return candidate


def _atomic_write_bytes(destination: Path, payload: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=str(destination.parent),
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, destination)
    except Exception:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)
        raise


def write_identity_graph(
    output_root: Path | str,
    relative_path: str,
    graph: IdentityGraphV1,
) -> Path:
    destination = _validate_relative_under_root(Path(output_root), relative_path)
    _atomic_write_bytes(destination, graph.to_canonical_json())
    return destination


def write_public_identity_projection(
    output_root: Path | str,
    relative_path: str,
    projection: PublicIdentityProjectionV1,
) -> Path:
    destination = _validate_relative_under_root(Path(output_root), relative_path)
    _atomic_write_bytes(destination, projection.to_canonical_json())
    return destination


def _object_pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate JSON object key: {key}")
        out[key] = value
    return out


def parse_canonical_json(data: bytes) -> IdentityGraphV1 | PublicIdentityProjectionV1:
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("canonical JSON must be bytes")
    try:
        payload = json.loads(data.decode("utf-8"), object_pairs_hook=_object_pairs_hook)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"malformed JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("canonical JSON root must be an object")
    schema_version = payload.get("schema_version")
    if not isinstance(schema_version, str):
        raise ValueError("schema_version is required")
    match = _SCHEMA_MAJOR_RE.fullmatch(schema_version.strip())
    if match is None:
        raise ValueError(f"unrecognized schema_version: {schema_version!r}")
    name, major = match.group("name"), int(match.group("major"))
    if name == "identity-graph":
        if major != 1:
            raise ValueError(f"unknown major schema version: {schema_version}")
        model: IdentityGraphV1 | PublicIdentityProjectionV1 = IdentityGraphV1.model_validate(
            payload
        )
    elif name == "identity-public":
        if major != 1:
            raise ValueError(f"unknown major schema version: {schema_version}")
        model = PublicIdentityProjectionV1.model_validate(payload)
    else:
        raise ValueError(f"unknown major schema version: {schema_version}")
    if model.to_canonical_json() != bytes(data):
        raise ValueError("serialized bytes are not exactly canonical")
    return model


def build_thin_slice_identity(
    *,
    run_id: str = "run_thin_slice_identity",
) -> tuple[IdentityGraphV1, PublicIdentityProjectionV1]:
    nodes = normalize_source_records(load_thin_slice_snapshots())
    graph = build_identity_graph(nodes, run_id=run_id)
    return graph, public_identity_projection(graph)
