"""Deterministic public-safe demo/newsletter proof (FM-037). Synthetic scenarios only."""

from __future__ import annotations

import io
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from PIL import Image, ImageDraw
from pydantic import ValidationError

from found_money.build import BuildPathError, _safe_source_config, resolve_output_root
from found_money.contracts.public_proof import (
    BANNED_STATE_PHRASES,
    CANONICAL_PUBLIC_PROOF_SCENARIOS,
    PUBLIC_PROOF_AUDIT_EXTRACTIONS_SHA256,
    PUBLIC_PROOF_AUDIT_PACKET_SHA256,
    client_nouns,
    PublicProofAggregateV1,
    PublicProofArtifactV1,
    PublicProofAuditV1,
    PublicProofBinaryExtractionsV1,
    PublicProofConfigV1,
    PublicProofDisplayedNumberV1,
    PublicProofManifestV1,
    PublicProofRecomputeReportV1,
    PublicProofScenarioSummaryV1,
    PublicProofStateV1,
    parse_public_proof_aggregate,
    parse_public_proof_binary_extractions,
    parse_public_proof_manifest,
    parse_public_proof_recompute,
)
from found_money.contracts.run import compute_source_set_hash
from found_money.contracts.scenarios import parse_scenario_aggregate
from found_money.receipts import sha256_bytes
from found_money.redaction import assert_public_safe
from found_money.safety import scan_output_tree, write_artifact_set_atomic
from found_money.scenarios import (
    load_scenario_definition,
    run_scenario_engine,
)
from found_money.scenarios.source_audit import (
    extract_binary_semantics,
    extract_pdf_semantics,
    extract_png_semantics,
    list_public_tree_files,
    public_binary_tree_binding,
    public_text_tree_binding,
)
from found_money.scenarios.transports import load_normalized_scenario_snapshots

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PACKAGE_ROOT / "configs" / "synthetic-public-proof.json"
AUDIT_PACKET = PACKAGE_ROOT / "tests/fixtures/public-proof/fm037/source-proper-noun-audit.json"
AUDIT_EXTRACTIONS = (
    PACKAGE_ROOT / "tests/fixtures/public-proof/fm037/source-audit-kit/binary-extractions.json"
)
MANIFEST_PATH = "public-proof.json"
AGGREGATE_PATH = "aggregate-receipt.json"
RECOMPUTE_PATH = "recompute-report.json"
DEMO_PATH = "demo.html"
NEWSLETTER_PATH = "newsletter.md"
PROVENANCE_PATH = "provenance.md"
LOG_PATH = "logs/public-proof.log"
PDF_PATH = "proof.pdf"
PNG_PATH = "proof.png"
DISPLAY_SURFACES = (DEMO_PATH, NEWSLETTER_PATH, PROVENANCE_PATH, PDF_PATH)
LOCKED_CLOCK = datetime(2026, 7, 29, 18, 0, tzinfo=timezone.utc)
_SENSITIVE_KEY_RE = re.compile(
    r"(?i)^(password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|"
    r"bearer|client[_-]?secret)$"
)
PUBLIC_PROOF_STATE = PublicProofStateV1()


class PublicProofError(ValueError):
    """Sanitized public-proof failure."""


class PublicProofConfigError(PublicProofError):
    """Configuration failed closed before writes."""


class PublicProofPathError(PublicProofError):
    """Output root failed closed before writes."""


@dataclass(frozen=True)
class PublicProofResult:
    output_root: Path
    run_id: str
    manifest: PublicProofManifestV1
    aggregate: PublicProofAggregateV1
    recompute: PublicProofRecomputeReportV1


def _canonical_json_bytes(payload: Any) -> bytes:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return (text + "\n").encode("utf-8")


def _reject_sensitive_keys(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if _SENSITIVE_KEY_RE.fullmatch(str(key)):
                raise PublicProofConfigError(
                    "public-proof config must not contain credential fields"
                )
            _reject_sensitive_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_sensitive_keys(nested)


def load_public_proof_config(path: Path | str | None = None) -> PublicProofConfigV1:
    config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    raw = config_path.read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PublicProofConfigError("public-proof config must be valid JSON") from exc
    if not isinstance(payload, Mapping):
        raise PublicProofConfigError("public-proof config must be a JSON object")
    _reject_sensitive_keys(payload)
    try:
        return PublicProofConfigV1.model_validate(payload)
    except Exception as exc:
        if isinstance(exc, ValidationError):
            raise PublicProofConfigError("public-proof config is invalid") from None
        raise PublicProofConfigError("public-proof config is invalid") from None


def scenario_run_binding(fixture_id: str) -> tuple[str, str]:
    """Return the locked scenario run_id and source_set_hash."""

    locked = load_scenario_definition(fixture_id)
    _snapshots, raw, receipts = load_normalized_scenario_snapshots(
        fixture_id, retrieved_at=locked.clock
    )
    expected_hashes = {name: sha256_bytes(raw[name]) for name in locked.sources}
    safe = _safe_source_config(source_mode="fixture", run_mode="public", fixture=fixture_id)
    run_id = (
        "run_"
        + sha256_bytes(
            _canonical_json_bytes({"source_config": safe, "source_hashes": expected_hashes})
        )[:16]
    )
    source_set_hash = compute_source_set_hash(
        {
            f"receipts/{name}.source-receipt.json": receipts[name].content_hash
            for name in locked.sources
        }
    )
    return run_id, source_set_hash


def _run_canonical_scenario(fixture_id: str) -> Any:
    run_id, _source_set_hash = scenario_run_binding(fixture_id)
    safe = _safe_source_config(source_mode="fixture", run_mode="public", fixture=fixture_id)
    return run_scenario_engine(run_id=run_id, safe_config=safe, fixture_id=fixture_id)


def _basis_counts(engine: Any) -> tuple[int, int, int]:
    observed = 0
    modeled = 0
    unquantified = engine.value_aggregate.unquantified_count
    for pile in engine.value_aggregate.piles:
        basis = str(pile.value_basis)
        if "observed" in basis:
            observed += 1
        elif "model" in basis:
            modeled += 1
    return observed, modeled, unquantified


def _scenario_summary(engine: Any) -> PublicProofScenarioSummaryV1:
    run_id, source_set_hash = scenario_run_binding(engine.definition.fixture_id)
    observed, modeled, unquantified = _basis_counts(engine)
    event_count = sum(engine.aggregate_receipt.candidates_by_family.values())
    return PublicProofScenarioSummaryV1(
        scenario_id=engine.definition.scenario_id,
        business_model=engine.definition.business_model,
        run_id=run_id,
        source_set_hash=source_set_hash,
        aggregate_sha256=sha256_bytes(engine.aggregate_receipt.to_canonical_json()),
        resolved_customer_count=engine.aggregate_receipt.resolved_customer_count,
        ambiguous_cluster_count=engine.aggregate_receipt.ambiguous_cluster_count,
        event_count=event_count,
        play_count=len(engine.aggregate_receipt.play_ids),
        card_count=len(engine.aggregate_receipt.card_ids),
        unquantified_count=unquantified,
        identified_opportunity_minor=dict(engine.aggregate_receipt.identified_opportunity_minor),
        candidates_by_family=dict(engine.aggregate_receipt.candidates_by_family),
        observed_count=observed,
        modeled_count=modeled,
    )


def _add_minor(left: dict[str, Decimal], right: Mapping[str, Decimal]) -> dict[str, Decimal]:
    out = dict(left)
    for currency, amount in right.items():
        out[currency] = out.get(currency, Decimal(0)) + amount
    return dict(sorted(out.items()))


def _state_lines() -> list[str]:
    return [
        "Evidence class: synthetic only.",
        "FM-036 real-business aggregate: absent.",
        "Identified opportunity is not recovered revenue.",
        "Observed, modeled, and unquantified values stay separate.",
        "Useful: not started.",
        "Sent: not performed.",
        "Recovered: not claimed.",
        "Publication: not performed.",
        "No send, no publish, no write, no audience, no spend.",
        "Visual baselines are inherited and not re-ratified.",
    ]


def _number(
    number_id: str,
    value: Decimal | int | str,
    unit: str,
    origin: str,
    surfaces: tuple[str, ...] = DISPLAY_SURFACES,
) -> PublicProofDisplayedNumberV1:
    if isinstance(value, Decimal):
        text = format(value, "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
    else:
        text = str(value)
    return PublicProofDisplayedNumberV1(
        number_id=number_id,
        value=text,
        unit=unit,
        origin=origin,
        surfaces=list(surfaces),
    )


def _collect_numbers(
    summaries: list[PublicProofScenarioSummaryV1],
    totals: Mapping[str, Decimal],
    *,
    observed: int,
    modeled: int,
    unquantified: int,
    event_count: int,
    play_count: int,
    card_count: int,
    resolved: int,
) -> list[PublicProofDisplayedNumberV1]:
    numbers = [
        _number("scenario_count", 3, "count", "canonical_synthetic_scenarios"),
        _number("resolved_customer_count", resolved, "count", "sum_of_synthetic_scenarios"),
        _number("event_count", event_count, "count", "sum_of_synthetic_scenarios"),
        _number("play_count", play_count, "count", "sum_of_synthetic_scenarios"),
        _number("card_count", card_count, "count", "sum_of_synthetic_scenarios"),
        _number("observed_count", observed, "count", "sum_of_synthetic_scenarios"),
        _number("modeled_count", modeled, "count", "sum_of_synthetic_scenarios"),
        _number("unquantified_count", unquantified, "count", "sum_of_synthetic_scenarios"),
    ]
    for currency, amount in totals.items():
        numbers.append(
            _number(
                f"identified_opportunity.{currency}",
                amount,
                f"{currency}_minor",
                "sum_of_synthetic_scenario_identified_opportunity",
            )
        )
    for summary in summaries:
        prefix = summary.scenario_id
        numbers.extend(
            [
                _number(
                    f"{prefix}.resolved_customer_count",
                    summary.resolved_customer_count,
                    "count",
                    prefix,
                ),
                _number(f"{prefix}.event_count", summary.event_count, "count", prefix),
                _number(f"{prefix}.play_count", summary.play_count, "count", prefix),
                _number(f"{prefix}.card_count", summary.card_count, "count", prefix),
                _number(
                    f"{prefix}.unquantified_count",
                    summary.unquantified_count,
                    "count",
                    prefix,
                ),
                _number(f"{prefix}.observed_count", summary.observed_count, "count", prefix),
                _number(f"{prefix}.modeled_count", summary.modeled_count, "count", prefix),
            ]
        )
        for currency, amount in summary.identified_opportunity_minor.items():
            numbers.append(
                _number(
                    f"{prefix}.identified_opportunity.{currency}",
                    amount,
                    f"{currency}_minor",
                    prefix,
                )
            )
        for family, count in summary.candidates_by_family.items():
            numbers.append(_number(f"{prefix}.family.{family}", count, "count", prefix))
    return numbers


def _escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def _number_span(item: PublicProofDisplayedNumberV1) -> str:
    return (
        f'<span data-fm-number="{_escape_html(item.number_id)}">{_escape_html(item.value)}</span>'
    )


def _numbers_by_id(
    numbers: list[PublicProofDisplayedNumberV1],
) -> dict[str, PublicProofDisplayedNumberV1]:
    return {item.number_id: item for item in numbers}


def _render_demo(numbers: list[PublicProofDisplayedNumberV1]) -> str:
    by_id = _numbers_by_id(numbers)
    rows = []
    for fixture in CANONICAL_PUBLIC_PROOF_SCENARIOS:
        rows.append(
            "<tr>"
            f"<td>{_escape_html(fixture)}</td>"
            f"<td>{_number_span(by_id[f'{fixture}.resolved_customer_count'])}</td>"
            f"<td>{_number_span(by_id[f'{fixture}.event_count'])}</td>"
            f"<td>{_number_span(by_id[f'{fixture}.play_count'])}</td>"
            f"<td>{_number_span(by_id[f'{fixture}.identified_opportunity.usd'])}</td>"
            f"<td>{_number_span(by_id[f'{fixture}.unquantified_count'])}</td>"
            "</tr>"
        )
    state = "".join(f"<li>{_escape_html(line)}</li>" for line in _state_lines())
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        "<title>Found Money public-safe demo proof</title></head><body>"
        "<main><h1>Found Money public-safe demo proof</h1>"
        "<p>Shareable example using illustrative records. Not a newsletter send and not a publication.</p>"
        f"<p>Scenario count: {_number_span(by_id['scenario_count'])}</p>"
        "<table><thead><tr><th>Scenario</th><th>Resolved</th><th>Events</th>"
        "<th>Plays</th><th>Identified opportunity USD minor</th>"
        "<th>Unquantified</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
        f"<p>Headline identified opportunity USD minor: {_number_span(by_id['identified_opportunity.usd'])}</p>"
        f"<p>Observed piles: {_number_span(by_id['observed_count'])}; "
        f"modeled piles: {_number_span(by_id['modeled_count'])}; "
        f"unquantified rows: {_number_span(by_id['unquantified_count'])}.</p>"
        f"<p>Event count: {_number_span(by_id['event_count'])}; "
        f"play count: {_number_span(by_id['play_count'])}; "
        f"card count: {_number_span(by_id['card_count'])}.</p>"
        "<h2>Independently recomputed numbers</h2><dl>"
        + "".join(
            f"<dt>{_escape_html(item.number_id)}</dt><dd>{_number_span(item)}</dd>"
            for item in numbers
        )
        + f"</dl><ul>{state}</ul></main></body></html>\n"
    )


def _render_newsletter(numbers: list[PublicProofDisplayedNumberV1]) -> str:
    by_id = _numbers_by_id(numbers)
    lines = [
        "# Found Money public-safe newsletter proof",
        "",
        "This is a local export for human review. It is not a send and not a publication.",
        "",
        f"The proof covers {_number_span_md(by_id['scenario_count'])} synthetic scenarios.",
        f"Headline identified opportunity is {_number_span_md(by_id['identified_opportunity.usd'])} USD minor.",
        "That figure is identified opportunity, not recovered revenue.",
        "",
    ]
    for fixture in CANONICAL_PUBLIC_PROOF_SCENARIOS:
        lines.extend(
            [
                f"## {fixture}",
                "",
                f"- Resolved customers: {_number_span_md(by_id[f'{fixture}.resolved_customer_count'])}",
                f"- Events: {_number_span_md(by_id[f'{fixture}.event_count'])}",
                f"- Plays: {_number_span_md(by_id[f'{fixture}.play_count'])}",
                f"- Identified opportunity USD minor: {_number_span_md(by_id[f'{fixture}.identified_opportunity.usd'])}",
                f"- Unquantified rows: {_number_span_md(by_id[f'{fixture}.unquantified_count'])}",
                f"- Observed piles: {_number_span_md(by_id[f'{fixture}.observed_count'])}",
                f"- Modeled piles: {_number_span_md(by_id[f'{fixture}.modeled_count'])}",
                f"- Cards: {_number_span_md(by_id[f'{fixture}.card_count'])}",
                "",
            ]
        )
        for item in numbers:
            if item.number_id.startswith(f"{fixture}.family."):
                lines.append(f"- {item.number_id}: {item.value}")
        lines.append("")
    lines.extend(["## Independently recomputed numbers", ""])
    lines.extend(f"- {item.number_id}: {item.value}" for item in numbers)
    lines.extend(["", "## State"] + [f"- {line}" for line in _state_lines()] + [""])
    return "\n".join(lines)


def _number_span_md(item: PublicProofDisplayedNumberV1) -> str:
    return f"{item.value}"


def _render_provenance(
    *,
    _run_id: str,
    summaries: list[PublicProofScenarioSummaryV1],
    numbers: list[PublicProofDisplayedNumberV1],
) -> str:
    by_id = _numbers_by_id(numbers)
    lines = [
        "# Public-proof provenance",
        "",
        "Public proof run identifier is recorded in the canonical manifest.",
        "Evidence class: synthetic only.",
        "Authorized live aggregate from the private-run harness: absent. No live read was performed.",
        "Inputs are the packaged canonical SaaS, ecommerce, and service scenario outputs.",
        "Every displayed number is independently recomputed from those scenario aggregates.",
        "",
        "## Scenario bindings",
        "",
    ]
    for summary in summaries:
        lines.extend(
            [
                f"- {summary.scenario_id}: synthetic aggregate bound by the manifest "
                f"and the scenario aggregate receipt.",
            ]
        )
    lines.extend(
        [
            "",
            f"Headline identified opportunity USD minor: {by_id['identified_opportunity.usd'].value}",
            "",
            "## Independently recomputed numbers",
            "",
        ]
    )
    lines.extend(f"- {item.number_id}: {item.value}" for item in numbers)
    lines.extend(
        [
            "",
            "## State",
            "",
        ]
    )
    lines.extend(f"- {line}" for line in _state_lines())
    lines.append("")
    return "\n".join(lines)


def _render_log(_run_id: str) -> str:
    lines = [
        "public-proof status=completed",
        "mode=synthetic",
        "fm036_aggregate=absent",
        "live_read=not_performed",
        "send=not_performed",
        "publish=not_performed",
        "visual_baseline=not_ratified",
        "public_safety=required",
    ]
    return "\n".join(lines) + "\n"


def _escape_pdf(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _render_pdf(lines: list[str]) -> bytes:
    content = ["BT", "/F1 10 Tf", "72 720 Td", "14 TL"]
    for index, line in enumerate(lines):
        if index:
            content.append("T*")
        content.append(f"({_escape_pdf(line[:110])}) Tj")
    content.append("ET")
    stream = "\n".join(content).encode("latin-1", errors="replace")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length "
        + str(len(stream)).encode("ascii")
        + b" >>\nstream\n"
        + stream
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, payload in enumerate(objects, start=1):
        offsets.append(len(out))
        out.extend(f"{index} 0 obj\n".encode("ascii"))
        out.extend(payload)
        out.extend(b"\nendobj\n")
    xref = len(out)
    out.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    out.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        out.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    out.extend(
        (f"trailer << /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n").encode(
            "ascii"
        )
    )
    return bytes(out)


def _pdf_lines(
    numbers: list[PublicProofDisplayedNumberV1],
    summaries: list[PublicProofScenarioSummaryV1],
) -> list[str]:
    by_id = _numbers_by_id(numbers)
    lines = [
        "Found Money public-safe proof",
        "Illustrative example and newsletter export. Not a send. Not a publication.",
        f"Scenarios: {by_id['scenario_count'].value}",
        f"Identified opportunity USD minor: {by_id['identified_opportunity.usd'].value}",
        "Identified opportunity is not recovered revenue.",
        f"Events {by_id['event_count'].value} plays {by_id['play_count'].value} cards {by_id['card_count'].value}",
        f"Observed {by_id['observed_count'].value} modeled {by_id['modeled_count'].value} unquantified {by_id['unquantified_count'].value}",
    ]
    for summary in summaries:
        lines.append(
            f"{summary.scenario_id} resolved {by_id[f'{summary.scenario_id}.resolved_customer_count'].value} "
            f"events {by_id[f'{summary.scenario_id}.event_count'].value} "
            f"plays {by_id[f'{summary.scenario_id}.play_count'].value} "
            f"usd {by_id[f'{summary.scenario_id}.identified_opportunity.usd'].value}"
        )
    lines.extend(f"{item.number_id} {item.value}" for item in numbers)
    lines.extend(_state_lines())
    return lines


def _render_png(lines: list[str]) -> bytes:
    image = Image.new("RGB", (900, 500), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    y = 16
    for line in lines[:18]:
        draw.text((16, y), line[:90], fill=(0, 0, 0))
        y += 22
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", compress_level=9, optimize=False)
    return buffer.getvalue()


def _assert_state_language(*texts: str) -> None:
    blob = "\n".join(texts).casefold()
    for phrase in BANNED_STATE_PHRASES:
        if phrase in blob:
            raise PublicProofConfigError(
                "public proof claimed send, publication, or recovered revenue"
            )
    for noun in client_nouns():
        if noun.casefold() in blob:
            raise PublicProofConfigError("public proof contains a Vault-only client name")
    required = (
        "synthetic",
        "absent",
        "identified opportunity is not recovered",
        "useful: not started",
        "sent: not performed",
        "recovered: not claimed",
        "publication: not performed",
    )
    for token in required:
        if token not in blob:
            raise PublicProofConfigError("public proof is missing required state language")


_HTML_NUMBER_SPAN_RE = re.compile(r'<span data-fm-number="([^"]+)">([^<]*)</span>')
_UNREGISTERED_NUMBER_RE = re.compile(r"(?<![0-9])\d{4,}(?![0-9])")


def _assert_registered_numeric_tokens(surface: str, text: str, registered: set[str]) -> None:
    extra = sorted(
        {token for token in _UNREGISTERED_NUMBER_RE.findall(text) if token not in registered}
    )
    if extra:
        raise PublicProofConfigError(f"unregistered displayed number on {surface}")


def _assert_numbers_on_surfaces(
    numbers: list[PublicProofDisplayedNumberV1],
    files: Mapping[str, bytes],
) -> None:
    expected = {item.number_id: item for item in numbers}
    registered_values = {item.value for item in numbers}
    html = files[DEMO_PATH].decode("utf-8")
    spans = _HTML_NUMBER_SPAN_RE.findall(html)
    seen_html: set[str] = set()
    for number_id, value in spans:
        item = expected.get(number_id)
        if item is None or DEMO_PATH not in item.surfaces:
            raise PublicProofConfigError("demo HTML contains an unregistered displayed number")
        if value != item.value:
            raise PublicProofConfigError(
                f"displayed number {item.number_id}={item.value} missing from {DEMO_PATH}"
            )
        seen_html.add(number_id)
    expected_html = {item.number_id for item in numbers if DEMO_PATH in item.surfaces}
    if seen_html != expected_html:
        raise PublicProofConfigError("demo HTML number marks do not match the recompute report")
    _assert_registered_numeric_tokens(DEMO_PATH, html, registered_values)
    pdf_text = extract_pdf_semantics(PDF_PATH, files[PDF_PATH]).text
    png = extract_png_semantics(PNG_PATH, files[PNG_PATH])
    png_text = " ".join(png.metadata.values())
    for item in numbers:
        for surface in item.surfaces:
            if surface == PDF_PATH:
                marker = f"{item.number_id} {item.value}"
                if marker not in pdf_text:
                    raise PublicProofConfigError(
                        f"displayed number {item.number_id}={item.value} missing from {surface}"
                    )
                continue
            text = files[surface].decode("utf-8")
            if surface == DEMO_PATH:
                continue
            if f"{item.number_id}: {item.value}" not in text and item.value not in text:
                raise PublicProofConfigError(
                    f"displayed number {item.number_id}={item.value} missing from {surface}"
                )
            if item.number_id not in text:
                raise PublicProofConfigError(
                    f"displayed number id {item.number_id} missing from {surface}"
                )
            _assert_registered_numeric_tokens(surface, text, registered_values)
    _assert_registered_numeric_tokens(PDF_PATH, pdf_text, registered_values)
    _assert_registered_numeric_tokens(PNG_PATH, png_text, registered_values)
    if png.size[0] < 1 or png.size[1] < 1:
        raise PublicProofConfigError("public-proof PNG is invalid")


def _build_payloads(
    config: PublicProofConfigV1,
) -> tuple[
    str,
    dict[str, bytes],
    PublicProofAggregateV1,
    PublicProofRecomputeReportV1,
    PublicProofManifestV1,
]:
    engines = [_run_canonical_scenario(fixture_id) for fixture_id in config.scenarios]
    summaries = [_scenario_summary(engine) for engine in engines]
    totals: dict[str, Decimal] = {}
    observed = modeled = unquantified = event_count = play_count = card_count = resolved = 0
    for summary in summaries:
        totals = _add_minor(totals, summary.identified_opportunity_minor)
        observed += summary.observed_count
        modeled += summary.modeled_count
        unquantified += summary.unquantified_count
        event_count += summary.event_count
        play_count += summary.play_count
        card_count += summary.card_count
        resolved += summary.resolved_customer_count
    scenario_hashes = {item.scenario_id: item.source_set_hash for item in summaries}
    scenario_run_ids = {item.scenario_id: item.run_id for item in summaries}
    source_set_hash = compute_source_set_hash(
        {f"scenarios/{name}/source-set": digest for name, digest in scenario_hashes.items()}
    )
    config_sha256 = sha256_bytes(config.to_canonical_json())
    run_id = (
        "run_"
        + sha256_bytes(
            _canonical_json_bytes(
                {
                    "config_sha256": config_sha256,
                    "scenario_source_set_hashes": scenario_hashes,
                    "scenario_run_ids": scenario_run_ids,
                }
            )
        )[:16]
    )
    aggregate = PublicProofAggregateV1(
        run_id=run_id,
        built_at=LOCKED_CLOCK,
        scenario_count=3,
        scenarios=summaries,
        identified_opportunity_minor=totals,
        observed_count=observed,
        modeled_count=modeled,
        unquantified_count=unquantified,
        event_count=event_count,
        play_count=play_count,
        card_count=card_count,
        resolved_customer_count=resolved,
        state=PUBLIC_PROOF_STATE,
    )
    numbers = _collect_numbers(
        summaries,
        totals,
        observed=observed,
        modeled=modeled,
        unquantified=unquantified,
        event_count=event_count,
        play_count=play_count,
        card_count=card_count,
        resolved=resolved,
    )
    recompute = PublicProofRecomputeReportV1(
        run_id=run_id,
        aggregate_sha256=sha256_bytes(aggregate.to_canonical_json()),
        displayed_numbers=numbers,
        state=PUBLIC_PROOF_STATE,
    )
    demo = _render_demo(numbers)
    newsletter = _render_newsletter(numbers)
    provenance = _render_provenance(_run_id=run_id, summaries=summaries, numbers=numbers)
    log = _render_log(run_id)
    pdf_lines = _pdf_lines(numbers, summaries)
    pdf = _render_pdf(pdf_lines)
    png = _render_png(pdf_lines)
    _assert_state_language(demo, newsletter, provenance, log, "\n".join(pdf_lines))
    payloads: dict[str, bytes] = {
        AGGREGATE_PATH: aggregate.to_canonical_json(),
        RECOMPUTE_PATH: recompute.to_canonical_json(),
        DEMO_PATH: demo.encode("utf-8"),
        NEWSLETTER_PATH: newsletter.encode("utf-8"),
        PROVENANCE_PATH: provenance.encode("utf-8"),
        LOG_PATH: log.encode("utf-8"),
        PDF_PATH: pdf,
        PNG_PATH: png,
    }
    for engine, summary in zip(engines, summaries, strict=True):
        payloads[f"scenarios/{summary.scenario_id}/aggregate-receipt.json"] = (
            engine.aggregate_receipt.to_canonical_json()
        )
    _assert_numbers_on_surfaces(numbers, payloads)
    try:
        for relative in (DEMO_PATH, NEWSLETTER_PATH, PROVENANCE_PATH, LOG_PATH):
            assert_public_safe({"artifact": relative, "text": payloads[relative].decode("utf-8")})
        pdf_meta = extract_pdf_semantics(PDF_PATH, payloads[PDF_PATH])
        png_meta = extract_png_semantics(PNG_PATH, payloads[PNG_PATH])
        assert_public_safe(
            {"artifact": PDF_PATH, "text": pdf_meta.text, "metadata": pdf_meta.metadata}
        )
        assert_public_safe({"artifact": PNG_PATH, "metadata": png_meta.metadata})
    except ValueError as exc:
        raise PublicProofConfigError("public proof failed public-safe validation") from exc
    artifacts = [
        PublicProofArtifactV1(path=path, sha256=sha256_bytes(data))
        for path, data in sorted(payloads.items())
    ]
    manifest = PublicProofManifestV1(
        run_id=run_id,
        built_at=LOCKED_CLOCK,
        scenarios=list(config.scenarios),
        scenario_run_ids=scenario_run_ids,
        scenario_source_set_hashes=scenario_hashes,
        source_set_hash=source_set_hash,
        config_sha256=config_sha256,
        aggregate_sha256=sha256_bytes(payloads[AGGREGATE_PATH]),
        recompute_sha256=sha256_bytes(payloads[RECOMPUTE_PATH]),
        artifacts=artifacts,
        state=PUBLIC_PROOF_STATE,
    )
    payloads[MANIFEST_PATH] = manifest.to_canonical_json()
    return run_id, payloads, aggregate, recompute, manifest


def _resolve_root(output_root: Path | str) -> Path:
    try:
        return resolve_output_root(output_root)
    except BuildPathError as exc:
        raise PublicProofPathError(str(exc)) from None


def build_public_proof(
    *,
    output_root: Path | str,
    config_path: Path | str | None = None,
    fm036_aggregate_path: Path | str | None = None,
) -> PublicProofResult:
    if fm036_aggregate_path is not None:
        raise PublicProofConfigError(
            "FM-036 live aggregate is absent; refusing a live or stored real-business file"
        )
    config = load_public_proof_config(config_path)
    root = _resolve_root(output_root)
    run_id, payloads, aggregate, recompute, manifest = _build_payloads(config)
    write_artifact_set_atomic(root, payloads)
    violations = scan_output_tree(root)
    if violations:
        raise PublicProofConfigError("public-proof output failed public-safety scanning")
    validate_public_proof_tree(root)
    return PublicProofResult(
        output_root=root,
        run_id=run_id,
        manifest=manifest,
        aggregate=aggregate,
        recompute=recompute,
    )


def recompute_public_proof(output_root: Path | str) -> PublicProofAggregateV1:
    """Independently rebuild aggregates, recompute report, and generated proof surfaces."""

    root = Path(output_root)
    config = load_public_proof_config()
    _run_id, payloads, expected, expected_recompute, _manifest = _build_payloads(config)
    for relative, expected_bytes in payloads.items():
        if relative == MANIFEST_PATH:
            continue
        actual = root / relative
        if not actual.is_file() or actual.read_bytes() != expected_bytes:
            raise PublicProofConfigError(
                "public-proof artifacts do not match independently recomputed output"
            )
    written_recompute = (root / RECOMPUTE_PATH).read_bytes()
    if expected_recompute.to_canonical_json() != written_recompute:
        raise PublicProofConfigError(
            "public-proof recompute report does not match independently recomputed output"
        )
    _assert_numbers_on_surfaces(
        list(expected_recompute.displayed_numbers),
        list_public_tree_files(root),
    )
    return expected


def validate_public_proof_tree(output_root: Path | str) -> PublicProofManifestV1:
    root = Path(output_root)
    files = list_public_tree_files(root)
    if MANIFEST_PATH not in files:
        raise PublicProofConfigError("public-proof manifest is missing")
    manifest = parse_public_proof_manifest(files[MANIFEST_PATH])
    hashed = {item.path: item.sha256 for item in manifest.artifacts}
    if set(hashed) != set(files) - {MANIFEST_PATH}:
        raise PublicProofConfigError("public-proof output tree does not match the artifact closure")
    for relative, digest in hashed.items():
        if sha256_bytes(files[relative]) != digest:
            raise PublicProofConfigError("public-proof manifest hash does not match artifact bytes")
    aggregate = parse_public_proof_aggregate(files[AGGREGATE_PATH])
    recompute = parse_public_proof_recompute(files[RECOMPUTE_PATH])
    if sha256_bytes(files[AGGREGATE_PATH]) != manifest.aggregate_sha256:
        raise PublicProofConfigError("public-proof aggregate hash does not match the manifest")
    if sha256_bytes(files[RECOMPUTE_PATH]) != manifest.recompute_sha256:
        raise PublicProofConfigError("public-proof recompute hash does not match the manifest")
    if aggregate.run_id != manifest.run_id or recompute.run_id != manifest.run_id:
        raise PublicProofConfigError("public-proof run_id does not bind the artifact set")
    if recompute.aggregate_sha256 != manifest.aggregate_sha256:
        raise PublicProofConfigError("recompute report does not bind the aggregate bytes")
    expected = recompute_public_proof(root)
    if expected.to_canonical_json() != files[AGGREGATE_PATH]:
        raise PublicProofConfigError(
            "public-proof aggregate does not match independently recomputed output"
        )
    config = load_public_proof_config()
    _run_id, expected_payloads, _expected_aggregate, expected_recompute, _manifest = (
        _build_payloads(config)
    )
    if expected_recompute.to_canonical_json() != files[RECOMPUTE_PATH]:
        raise PublicProofConfigError(
            "public-proof recompute report does not match independently recomputed output"
        )
    for relative in (DEMO_PATH, NEWSLETTER_PATH, PROVENANCE_PATH, PDF_PATH, PNG_PATH):
        if files[relative] != expected_payloads[relative]:
            raise PublicProofConfigError(
                "public-proof generated surfaces do not match independently recomputed output"
            )
    for fixture_id in CANONICAL_PUBLIC_PROOF_SCENARIOS:
        relative = f"scenarios/{fixture_id}/aggregate-receipt.json"
        try:
            stored = parse_scenario_aggregate(files[relative])
        except (ValueError, TypeError) as exc:
            raise PublicProofConfigError(
                "scenario aggregate does not match independently recomputed output"
            ) from exc
        engine = _run_canonical_scenario(fixture_id)
        if stored.to_canonical_json() != engine.aggregate_receipt.to_canonical_json():
            raise PublicProofConfigError(
                "scenario aggregate does not match independently recomputed output"
            )
    _assert_numbers_on_surfaces(list(expected_recompute.displayed_numbers), files)
    _assert_state_language(
        files[DEMO_PATH].decode("utf-8"),
        files[NEWSLETTER_PATH].decode("utf-8"),
        files[PROVENANCE_PATH].decode("utf-8"),
        files[LOG_PATH].decode("utf-8"),
        extract_pdf_semantics(PDF_PATH, files[PDF_PATH]).text,
    )
    png = extract_png_semantics(PNG_PATH, files[PNG_PATH])
    if png.size[0] < 1 or png.size[1] < 1:
        raise PublicProofConfigError("public-proof PNG is invalid")
    violations = scan_output_tree(root)
    if violations:
        raise PublicProofConfigError("public-proof output failed public-safety scanning")
    return manifest


def current_public_proof_binding() -> tuple[str, str]:
    config = load_public_proof_config()
    run_id, _payloads, _aggregate, _recompute, manifest = _build_payloads(config)
    return run_id, manifest.source_set_hash


def validate_public_proof_audit(
    packet: PublicProofAuditV1,
    *,
    binary_extractions: bytes,
    output_root: Path | str,
) -> PublicProofBinaryExtractionsV1:
    # Bind every generated PDF/PNG byte for this runtime first. Do not call back
    # into this audit validator from the tree path (no circular validation).
    validate_public_proof_tree(output_root)
    packet_digest = sha256_bytes(packet.to_canonical_json())
    if packet_digest != PUBLIC_PROOF_AUDIT_PACKET_SHA256:
        raise ValueError("public-proof audit packet hash is stale")
    run_id, source_set_hash = current_public_proof_binding()
    if packet.run_id != run_id:
        raise ValueError("public-proof audit run_id does not bind to the current proof")
    if packet.source_set_hash != source_set_hash:
        raise ValueError("public-proof audit source_set_hash is stale")
    digest = sha256_bytes(binary_extractions)
    if (
        packet.binary_extractions_sha256 != digest
        or digest != PUBLIC_PROOF_AUDIT_EXTRACTIONS_SHA256
    ):
        raise ValueError("public-proof audit binary extractions hash is stale")
    extractions = parse_public_proof_binary_extractions(binary_extractions)
    if packet.audited_pdf_count != len(extractions.pdfs):
        raise ValueError("public-proof audit PDF count does not match extractions")
    if packet.audited_png_count != len(extractions.pngs):
        raise ValueError("public-proof audit PNG count does not match extractions")
    files = list_public_tree_files(output_root)
    text_count, text_digest = public_text_tree_binding(files)
    if text_count != packet.audited_text_file_count:
        raise ValueError("public-proof audit text file count does not match current output")
    current_platform = (
        "macos"
        if sys.platform == "darwin"
        else "linux"
        if sys.platform.startswith("linux")
        else None
    )
    if current_platform is None:
        raise ValueError("public-proof audit does not support this runtime platform")
    if packet.audited_text_tree_sha256 != packet.audited_text_tree_sha256_by_platform.get("macos"):
        raise ValueError("public-proof audit legacy text tree hash is stale")
    if current_platform == "macos":
        expected_text = packet.audited_text_tree_sha256_by_platform.get("macos")
        if expected_text != text_digest:
            raise ValueError(
                "public-proof audit text tree hash is stale: "
                f"{current_platform} expected={expected_text} observed={text_digest}"
            )
    live_pdf_paths = sorted(path for path in files if Path(path).suffix.casefold() == ".pdf")
    live_png_paths = sorted(path for path in files if Path(path).suffix.casefold() == ".png")
    if live_pdf_paths != sorted(item.path for item in extractions.pdfs):
        raise ValueError("public-proof audit PDF path closure does not match current output")
    if live_png_paths != sorted(item.path for item in extractions.pngs):
        raise ValueError("public-proof audit PNG path closure does not match current output")
    binary_count, binary_digest = public_binary_tree_binding(files)
    if binary_count != packet.audited_pdf_count + packet.audited_png_count:
        raise ValueError("public-proof audit binary file count is stale")
    if current_platform == "macos":
        expected_binary = packet.audited_binary_tree_sha256_by_platform.get("macos")
        if expected_binary != binary_digest:
            raise ValueError(
                "public-proof audit binary tree hash is stale: "
                f"{current_platform} expected={expected_binary} observed={binary_digest}"
            )
    live_pdfs, live_pngs = extract_binary_semantics(files)
    live_pdf_by_path = {item.path: item for item in live_pdfs}
    live_png_by_path = {item.path: item for item in live_pngs}
    # Extraction-row sha256 values remain the macOS byte lock. Linux may diverge
    # in encoder output; validate_public_proof_tree already exact-compared the
    # current-runtime PDF/PNG bytes, and the immutable audit packet SHA prevents
    # model_copy from rewriting platform digests. Keep separate observed Linux
    # and macOS tree digests — do not pretend encoded bytes match across OS.
    # When a packet claims identical platform digests, raw sha stays mandatory so
    # a hypothetically unlocked packet cannot launder substituted PDF/PNG bytes.
    platform_digests = packet.audited_binary_tree_sha256_by_platform
    require_raw_sha = current_platform == "macos" or platform_digests.get(
        "linux"
    ) == platform_digests.get("macos")
    for committed_pdf in extractions.pdfs:
        live_pdf = live_pdf_by_path[committed_pdf.path]
        live_digest = sha256_bytes(files[committed_pdf.path])
        if committed_pdf.sha256 != live_digest or live_pdf.sha256 != committed_pdf.sha256:
            if require_raw_sha:
                raise ValueError(
                    "public-proof audit PDF sha256 does not match committed extraction"
                )
        if committed_pdf.semantic_dict() != live_pdf.semantic_dict():
            raise ValueError("public-proof audit PDF extraction does not match current output")
    for committed_png in extractions.pngs:
        live_png = live_png_by_path[committed_png.path]
        live_digest = sha256_bytes(files[committed_png.path])
        if committed_png.sha256 != live_digest or live_png.sha256 != committed_png.sha256:
            if require_raw_sha:
                raise ValueError(
                    "public-proof audit PNG sha256 does not match committed extraction"
                )
        if committed_png.semantic_dict() != live_png.semantic_dict():
            raise ValueError("public-proof audit PNG extraction does not match current output")
    return extractions


def assert_no_send_or_publish(root: Path | str | None = None) -> None:
    if root is not None:
        text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in Path(root).rglob("*")
            if path.is_file() and path.suffix.casefold() in {".md", ".html", ".log", ".json"}
        )
        _assert_state_language(text)


__all__ = [
    "AUDIT_EXTRACTIONS",
    "AUDIT_PACKET",
    "PublicProofConfigError",
    "PublicProofError",
    "PublicProofPathError",
    "PublicProofResult",
    "assert_no_send_or_publish",
    "build_public_proof",
    "current_public_proof_binding",
    "load_public_proof_config",
    "recompute_public_proof",
    "scenario_run_binding",
    "validate_public_proof_audit",
    "validate_public_proof_tree",
]
