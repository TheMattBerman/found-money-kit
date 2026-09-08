"""Deterministic Money Map builder for contribution ledgers."""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from found_money.contracts.events import RecoveryCandidateSetV1, RecoveryCandidateV1
from found_money.contracts.map import (
    ConfidenceClass,
    CurrencyBasisCountsV1,
    MoneyMapPileV1,
    MoneyMapV1,
    PileId,
    PublicMoneyMapPileV1,
    PublicMoneyMapProjectionV1,
)
from found_money.contracts.value import ContributionLedgerV1, ContributionV1
from found_money.events import build_thin_slice_failed_payments
from found_money.identity import load_thin_slice_snapshots
from found_money.ranking import rank_money_map
from found_money.value import build_contribution_ledger

_TRAVERSAL_RE = re.compile(r"(^|/)\.\.(/|$)")
_WINDOWS_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")
_SCHEMA_MAJOR_RE = re.compile(r"^(?P<name>.+)\.v(?P<major>\d+)$")


def _why_recoverable_for_pile(
    *,
    economic_unit_count: int,
    value_basis: str,
    event_family: str,
) -> str:
    unit_label = "event" if economic_unit_count == 1 else "events"
    return (
        f"{economic_unit_count} {value_basis.replace('_', ' ')} "
        f"{event_family.replace('_', ' ')} {unit_label}; not recovered revenue"
    )


def build_money_map(
    ledger: ContributionLedgerV1,
    candidate_set: RecoveryCandidateSetV1,
    *,
    built_at: datetime | None = None,
) -> MoneyMapV1:
    """Build a Money Map from an accepted contribution ledger and candidate set."""
    when = built_at or datetime(2026, 7, 29, 18, 0, 20, tzinfo=timezone.utc)
    if ledger.run_id != candidate_set.run_id:
        raise ValueError("ledger run_id must match candidate set run_id")

    contributions = list(ledger.contributions)
    candidates_by_candidate_key = {item.candidate_key: item for item in candidate_set.candidates}
    candidates_by_economic_key: dict[str, list] = defaultdict(list)
    for candidate_item in candidate_set.candidates:
        candidates_by_economic_key[candidate_item.economic_unit_key].append(candidate_item)
    # Separate pile values by currency. Never blend currencies or apply FX.
    by_pile_currency: dict[tuple[str, str], list[ContributionV1]] = defaultdict(list)
    for contribution in contributions:
        by_pile_currency[(contribution.pile_id, contribution.currency)].append(contribution)

    headline: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
    basis_by_currency: dict[str, CurrencyBasisCountsV1] = {}
    piles: list[MoneyMapPileV1] = []

    def add_basis(
        currency: str, *, observed: int = 0, modeled: int = 0, unquantified: int = 0
    ) -> None:
        prior = basis_by_currency.get(currency)
        basis_by_currency[currency] = CurrencyBasisCountsV1(
            observed_event_count=(prior.observed_event_count if prior else 0) + observed,
            modeled_event_count=(prior.modeled_event_count if prior else 0) + modeled,
            unquantified_event_count=(prior.unquantified_event_count if prior else 0)
            + unquantified,
        )

    contribution_candidates: dict[str, Any] = {}

    for pile_id, pile_currency in sorted(by_pile_currency):
        rows = by_pile_currency[(pile_id, pile_currency)]
        selected_total = sum((row.amount_minor for row in rows), Decimal(0))
        customer_tokens = {row.customer_token for row in rows}
        economic_unit_count = len(rows)
        for row in rows:
            resolved_candidate: RecoveryCandidateV1 | None = None
            if row.candidate_key is not None:
                resolved_candidate = candidates_by_candidate_key.get(row.candidate_key)
            if resolved_candidate is None:
                matches = candidates_by_economic_key.get(row.economic_unit_key, [])
                if len(matches) == 1:
                    resolved_candidate = matches[0]
            if resolved_candidate is None:
                raise ValueError(f"missing candidate for {row.economic_unit_key}")
            contribution_candidates[row.economic_unit_key] = resolved_candidate
            expected_pile = (
                "payment_rescue"
                if resolved_candidate.event_family == "failed_payment"
                else resolved_candidate.event_family
            )
            if not (
                row.pile_id == expected_pile
                or (
                    resolved_candidate.event_family == "failed_payment"
                    and row.pile_id == "failed_payment"
                )
            ):
                raise ValueError(
                    f"pile {row.pile_id} does not match candidate event family "
                    f"{resolved_candidate.event_family}"
                )
        headline[pile_currency] += selected_total
        observed_count = sum(row.value_basis == "observed_face_value" for row in rows)
        modeled_count = sum(row.value_basis == "modeled_opportunity" for row in rows)
        add_basis(pile_currency, observed=observed_count, modeled=modeled_count)
        basis_values = {row.value_basis for row in rows}
        value_basis = next(iter(basis_values)) if len(basis_values) == 1 else "mixed"
        confidence_class = (
            "observed"
            if value_basis == "observed_face_value"
            else ("modeled" if value_basis == "modeled_opportunity" else "mixed")
        )
        family_labels = sorted(
            {contribution_candidates[row.economic_unit_key].event_family for row in rows}
        )
        family_label = "/".join(family_labels)
        piles.append(
            MoneyMapPileV1(
                pile_id=cast(PileId, pile_id),
                rank=len(piles) + 1,
                value_basis=value_basis,
                currency=pile_currency,
                selected_value_minor=selected_total,
                customer_count=len(customer_tokens),
                economic_unit_count=economic_unit_count,
                confidence_class=cast(ConfidenceClass, confidence_class),
                why_recoverable=_why_recoverable_for_pile(
                    economic_unit_count=economic_unit_count,
                    value_basis=value_basis,
                    event_family=family_label,
                ),
            )
        )

    excluded_candidate_keys = {
        record.excluded_candidate_key
        for record in (ledger.overlap_ledger.records if ledger.overlap_ledger else [])
    }
    for gap in ledger.data_gaps:
        if gap.candidate_key in excluded_candidate_keys or gap.reason_code == "overlap_excluded":
            continue
        if gap.currency is not None:
            add_basis(gap.currency, unquantified=1)

    # Deterministic order: pile_id, then currency. Never compare minor units across currencies.
    piles.sort(key=lambda item: (item.pile_id, item.currency, -item.selected_value_minor))
    ranked: list[MoneyMapPileV1] = []
    for index, item in enumerate(piles):
        ranked.append(item.model_copy(update={"rank": index + 1}))
    base_map = MoneyMapV1(
        run_id=ledger.run_id,
        built_at=when,
        identified_opportunity_minor=dict(sorted(headline.items())),
        basis_counts_by_currency=dict(sorted(basis_by_currency.items())),
        piles=ranked,
        recommended_play_ids=[],
        strategy_stage="not_started",
        data_gap_count=len(ledger.data_gaps),
        overlap_exclusion_count=(
            len(ledger.overlap_ledger.records) if ledger.overlap_ledger else 0
        ),
    )
    return rank_money_map(base_map, ledger, candidate_set)


def public_money_map_projection(money_map: MoneyMapV1) -> PublicMoneyMapProjectionV1:
    piles = [
        PublicMoneyMapPileV1(
            pile_id=item.pile_id,
            rank=item.rank,
            currency=item.currency,
            selected_value_minor=item.selected_value_minor,
            value_basis=item.value_basis,
            confidence_class=item.confidence_class,
            customer_count=item.customer_count,
            source_count=item.source_count,
            readiness=item.readiness,
            navigation=item.navigation,
        )
        for item in money_map.piles
    ]
    piles.sort(key=lambda item: item.rank)
    return PublicMoneyMapProjectionV1(
        run_id=money_map.run_id,
        built_at=money_map.built_at,
        identified_opportunity_minor=dict(money_map.identified_opportunity_minor),
        basis_counts_by_currency=dict(money_map.basis_counts_by_currency),
        piles=piles,
        data_gap_count=money_map.data_gap_count,
        overlap_exclusion_count=money_map.overlap_exclusion_count,
        named_data_gaps=list(money_map.named_data_gaps),
        event_exclusions_by_reason=dict(money_map.event_exclusions_by_reason),
        strategy_stage=money_map.strategy_stage,
        customer_count=money_map.customer_count,
        source_count=money_map.source_count,
        next_action=money_map.next_action,
    )


def build_thin_slice_money_map(
    *,
    run_id: str = "run_thin_slice_money_map",
) -> MoneyMapV1:
    """Compose FM-003 thin-slice helpers into one Money Map."""
    _graph, candidates = build_thin_slice_failed_payments(run_id=run_id)
    snapshots = load_thin_slice_snapshots()
    ledger = build_contribution_ledger(candidates, snapshots["stripe"])
    return build_money_map(ledger, candidates)


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


def write_money_map(
    output_root: Path | str,
    relative_path: str,
    money_map: MoneyMapV1,
) -> Path:
    destination = _validate_relative_under_root(Path(output_root), relative_path)
    _atomic_write_bytes(destination, money_map.to_canonical_json())
    return destination


def write_public_money_map_projection(
    output_root: Path | str,
    relative_path: str,
    projection: PublicMoneyMapProjectionV1,
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


def parse_canonical_json(data: bytes) -> MoneyMapV1 | PublicMoneyMapProjectionV1:
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
    if name == "money-map":
        if major != 1:
            raise ValueError(f"unknown major schema version: {schema_version}")
        model: MoneyMapV1 | PublicMoneyMapProjectionV1 = MoneyMapV1.model_validate(payload)
    elif name == "money-map-public":
        if major != 1:
            raise ValueError(f"unknown major schema version: {schema_version}")
        model = PublicMoneyMapProjectionV1.model_validate(payload)
    else:
        raise ValueError(f"unknown major schema version: {schema_version}")
    if model.to_canonical_json() != bytes(data):
        raise ValueError("serialized bytes are not exactly canonical")
    return model
