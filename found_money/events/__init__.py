"""Deterministic failed-payment detection for fixture Stripe snapshots."""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from found_money.contracts.events import RecoveryCandidateSetV1, RecoveryCandidateV1
from found_money.contracts.identity import IdentityGraphV1
from found_money.identity import (
    build_thin_slice_identity,
    load_thin_slice_snapshots,
    node_id_for,
)

_TRAVERSAL_RE = re.compile(r"(^|/)\.\.(/|$)")
_WINDOWS_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")
_SCHEMA_MAJOR_RE = re.compile(r"^(?P<name>.+)\.v(?P<major>\d+)$")


def economic_unit_key_for_invoice(invoice_id: str) -> str:
    return f"stripe_invoice:{invoice_id.strip()}"


def _customer_token_for_stripe_customer(graph: IdentityGraphV1, customer_id: str) -> str:
    customer_node_id = node_id_for("stripe", "customer", customer_id)
    for cluster in graph.customers:
        if customer_node_id in set(cluster.member_node_ids):
            return cluster.customer_token
    raise ValueError(f"no identity cluster contains Stripe customer {customer_id}")


def _is_failed_payment_invoice(invoice: Mapping[str, Any]) -> bool:
    status = str(invoice.get("status") or "").strip().lower()
    outcome = str(invoice.get("collection_outcome") or "").strip().lower()
    if status == "paid" or outcome == "payment_succeeded":
        return False
    return status == "open" and outcome == "payment_failed"


def detect_failed_payments(
    stripe_snapshot: Mapping[str, Any],
    graph: IdentityGraphV1,
    *,
    run_id: str,
    built_at: datetime | None = None,
) -> RecoveryCandidateSetV1:
    """Emit failed-payment candidates from a Stripe snapshot and identity graph."""
    when = built_at or datetime(2026, 7, 29, 18, 0, 10, tzinfo=timezone.utc)
    invoices = stripe_snapshot.get("invoices") or []
    if not isinstance(invoices, list):
        raise ValueError("stripe snapshot invoices must be a list")

    candidates: list[RecoveryCandidateV1] = []
    for invoice in invoices:
        if not isinstance(invoice, Mapping):
            raise ValueError("invoice records must be objects")
        if not _is_failed_payment_invoice(invoice):
            continue
        invoice_id = str(invoice.get("id") or "").strip()
        customer_id = str(invoice.get("customer_id") or "").strip()
        if not invoice_id or not customer_id:
            raise ValueError("failed-payment invoice requires id and customer_id")
        unit_key = economic_unit_key_for_invoice(invoice_id)
        invoice_node_id = node_id_for("stripe", "invoice", invoice_id)
        customer_token = _customer_token_for_stripe_customer(graph, customer_id)
        candidates.append(
            RecoveryCandidateV1(
                run_id=run_id,
                event_family="failed_payment",
                confidence_class="observed",
                economic_unit_key=unit_key,
                customer_token=customer_token,
                lineage={
                    "invoice_node_id": invoice_node_id,
                    "stripe_customer_id": customer_id,
                    "invoice_id": invoice_id,
                },
                qualifying_evidence={
                    "status": str(invoice.get("status")),
                    "collection_outcome": str(invoice.get("collection_outcome")),
                    "source": "stripe.invoice",
                },
            )
        )

    candidates.sort(key=lambda item: item.economic_unit_key)
    return RecoveryCandidateSetV1(
        run_id=run_id,
        built_at=when,
        candidates=candidates,
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


def write_recovery_candidates(
    output_root: Path | str,
    relative_path: str,
    candidate_set: RecoveryCandidateSetV1,
) -> Path:
    destination = _validate_relative_under_root(Path(output_root), relative_path)
    _atomic_write_bytes(destination, candidate_set.to_canonical_json())
    return destination


def _object_pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate JSON object key: {key}")
        out[key] = value
    return out


def parse_canonical_json(data: bytes) -> RecoveryCandidateSetV1:
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
    if name != "recovery-candidate" or major != 1:
        raise ValueError(f"unknown major schema version: {schema_version}")
    model = RecoveryCandidateSetV1.model_validate(payload)
    if model.to_canonical_json() != bytes(data):
        raise ValueError("serialized bytes are not exactly canonical")
    return model


def build_thin_slice_failed_payments(
    *,
    run_id: str = "run_thin_slice_failed_payments",
) -> tuple[IdentityGraphV1, RecoveryCandidateSetV1]:
    snapshots = load_thin_slice_snapshots()
    graph, _projection = build_thin_slice_identity(run_id=run_id)
    candidates = detect_failed_payments(
        snapshots["stripe"],
        graph,
        run_id=run_id,
    )
    return graph, candidates


from found_money.events.library import (  # noqa: E402
    DEFAULT_EVENT_BUILT_AT,
    EVENT_CANDIDATES_PATH,
    EVENT_DATA_GAPS_PATH,
    EVENT_EXCLUSIONS_PATH,
    EVENT_PUBLIC_PATH,
    EventDetectionConfig,
    EventDetectionResult,
    detect_event_families,
    detect_events,
    parse_data_gap_ledger,
    parse_exclusion_ledger,
    parse_public_event_projection,
    write_event_detection,
)

__all__ = [
    "DEFAULT_EVENT_BUILT_AT",
    "EVENT_CANDIDATES_PATH",
    "EVENT_DATA_GAPS_PATH",
    "EVENT_EXCLUSIONS_PATH",
    "EVENT_PUBLIC_PATH",
    "EventDetectionConfig",
    "EventDetectionResult",
    "build_thin_slice_failed_payments",
    "detect_event_families",
    "detect_events",
    "detect_failed_payments",
    "economic_unit_key_for_invoice",
    "parse_canonical_json",
    "parse_data_gap_ledger",
    "parse_exclusion_ledger",
    "parse_public_event_projection",
    "write_event_detection",
    "write_recovery_candidates",
]
