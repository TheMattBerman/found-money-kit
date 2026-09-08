"""Bounded private two-system run harness (FM-036). Fixture proof only by default."""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from pydantic import ValidationError

from found_money.activation import LaunchPackInputs, build_launch_pack
from found_money.connectors.hubspot import (
    HubSpotConnectorError,
    HubSpotHttpTransport,
    build_crm_snapshot_receipt,
    canonical_snapshot_bytes as hubspot_canonical_bytes,
    fetch_crm_snapshot,
)
from found_money.connectors.stripe import (
    RESOURCE_NAMES,
    StripeConnector,
    StripeConnectorError,
    StripeHttpTransport,
    build_stripe_snapshot_receipt,
    canonical_snapshot_bytes as stripe_canonical_bytes,
)
from found_money.contracts.private_run import (
    ALLOWED_SYSTEMS as ALLOWED_SYSTEMS,
    BINDABLE_PROOF_CLASSES,
    DEFAULT_PAGE_SIZE,
    PRIVATE_INPUT_ARTIFACTS,
    PRIVATE_INPUT_PREFIX,
    REQUIRED_CHECKS,
    REQUIRED_ENV_BINDINGS,
    UNSUPPORTED_DATE_QUERY_KEYS,
    OperatorReviewPacketV1,
    PassingCheckReceiptV1,
    PrivateRunAggregateProofV1,
    PrivateRunArtifactV1,
    PrivateRunBoundsV1,
    PrivateRunConfigV1,
    PrivateRunDiagnosticsV1,
    PrivateRunManifestV1,
    PrivateRunSourceStageEvidenceV1,
    ProofClassRecordV1,
    RequiredCheckResultV1,
    parse_operator_packet,
    parse_private_run_config,
    parse_private_run_manifest,
)
from found_money.contracts.run import compute_source_set_hash
from found_money.contracts.safety import (
    NoMutationAssertionV1,
    ReleaseSafetyEvidencePacketV1,
    SafeRequestAuditLogV1,
)
from found_money.contracts.scenarios import ScenarioIdentityAggregateV1
from found_money.contracts.source import SourceReceiptV1
from found_money.events import detect_event_families
from found_money.identity import build_identity_graph, normalize_source_records
from found_money.map import build_money_map
from found_money.ranking import rank_money_map
from found_money.receipts import _validate_relative_under_root, sha256_bytes
from found_money.rendering import render_recovery_room_html
from found_money.safety import empty_fixture_assertion, scan_output_tree, write_artifact_set_atomic
from found_money.safety.allowlist import HUBSPOT_HOST, STRIPE_HOST, assert_network_allowed
from found_money.safety.audit import SafeRequestAuditor, audit_digest, build_no_mutation_assertion
from found_money.safety.evidence import validate_release_safety_evidence_packet
from found_money.scenarios import SYNTHETIC_SAAS_V1, run_scenario_engine
from found_money.scenarios.harness import payment_rescue_ledger
from found_money.scenarios.transports import load_normalized_scenario_snapshots
from found_money.strategy import (
    apply_complete_plays_to_money_map,
    build_canonical_saas_recovery_strategy,
    build_grounded_strategy_packet,
    canonical_saas_business_profile,
)
from found_money.strategy.campaign import CompleteStrategyGroundingError
from found_money.value import build_value_ledger

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
RELEASE_SAFETY_EVIDENCE_PACKET = (
    PACKAGE_ROOT / "tests/fixtures/saas/safety/release-safety-evidence.json"
)
IGNORED_PRIVATE_ROOT_PREFIXES = ("private-runs", "runs", "artifacts/private")
SOURCE_STAGE_PATH = "provenance/source-stage-evidence.json"
PRIVATE_RUN_ARTIFACTS = (
    "private-run.json",
    "aggregate-proof.json",
    "operator-packet.json",
    "diagnostics.json",
    "receipts/hubspot.source-receipt.json",
    "receipts/stripe.source-receipt.json",
    "provenance/no-mutation-assertion.json",
    "provenance/request-audit.json",
    SOURCE_STAGE_PATH,
)
_TRAVERSAL_RE = re.compile(r"(^|/)\.\.(/|$)")
_WINDOWS_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")
_SENSITIVE_KEY_RE = re.compile(
    r"(?i)^(password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|"
    r"bearer|client[_-]?secret)$"
)
LIVE_ENABLE_ENV = "FOUND_MONEY_PRIVATE_RUN_LIVE"


class PrivateRunError(ValueError):
    """Sanitized private-run failure."""


class PrivateRunConfigError(PrivateRunError):
    """Configuration failed closed before source reads or writes."""


class PrivateRunPathError(PrivateRunError):
    """Private output root failed closed before source reads or writes."""


class PrivateRunCredentialError(PrivateRunError):
    """Credential declaration or environment match failed closed."""


@dataclass(frozen=True)
class PrivateRunResult:
    private_root: Path
    run_id: str
    manifest: PrivateRunManifestV1
    aggregate: PrivateRunAggregateProofV1
    packet: OperatorReviewPacketV1


@dataclass
class _DiagnosticBundle:
    identity_aggregate: ScenarioIdentityAggregateV1
    event_public: Any
    ledger: Any
    ranked: Any
    html: str
    launch: Any
    snapshots: Mapping[str, Mapping[str, Any]]


def _is_record_list_path(path: str) -> bool:
    if path.startswith("/crm/v3/objects/"):
        return True
    if path.startswith("/v1/"):
        return True
    return False


def _response_record_count(path: str, payload: Mapping[str, Any]) -> int:
    if not _is_record_list_path(path):
        return 0
    if path.startswith("/crm/v3/objects/"):
        results = payload.get("results")
        return len(results) if isinstance(results, list) else 0
    data = payload.get("data")
    return len(data) if isinstance(data, list) else 0


def _sanitize_query(query: Mapping[str, str]) -> dict[str, str]:
    cleaned: dict[str, str] = {}
    for key, value in query.items():
        if key in UNSUPPORTED_DATE_QUERY_KEYS or key.startswith("created["):
            continue
        cleaned[str(key)] = str(value)
    return cleaned


class _GetOnlyBoundedTransport:
    """GET-only wrapper that enforces page/record budgets on the outgoing wire."""

    def __init__(
        self,
        inner: Any,
        *,
        host: str,
        auditor: SafeRequestAuditor,
        page_limit: int,
        record_limit: int,
    ) -> None:
        self._inner = inner
        self._host = host
        self._auditor = auditor
        self._page_limit = page_limit
        self._record_limit = record_limit
        self.pages = 0
        self.records = 0
        self.queries: list[dict[str, str]] = []

    def request(
        self,
        *,
        method: str,
        path: str,
        query: Mapping[str, str],
        headers: Mapping[str, str],
    ) -> Mapping[str, Any]:
        _ = headers
        if method != "GET":
            raise PrivateRunCredentialError("private-run transport permits GET requests only")
        try:
            assert_network_allowed("GET", self._host, path)
        except Exception as exc:
            raise PrivateRunCredentialError("private-run request is not allowlisted") from exc
        outgoing = _sanitize_query(query)
        if _is_record_list_path(path):
            remaining = self._record_limit - self.records
            if remaining <= 0:
                raise PrivateRunConfigError("private-run exceeded configured record limit")
            if "limit" in outgoing:
                try:
                    requested = int(outgoing["limit"])
                except ValueError:
                    requested = DEFAULT_PAGE_SIZE
                outgoing["limit"] = str(max(1, min(requested, remaining, DEFAULT_PAGE_SIZE)))
        self.pages += 1
        if self.pages > self._page_limit:
            raise PrivateRunConfigError("private-run exceeded configured page limit")
        self.queries.append(dict(outgoing))
        self._auditor.record(method="GET", host=self._host, path=path, provenance=self._inner)
        payload = self._inner.request(method="GET", path=path, query=outgoing, headers=headers)
        self.records += _response_record_count(path, payload)
        if self.records > self._record_limit:
            raise PrivateRunConfigError("private-run exceeded configured record limit")
        return payload


def _canonical_json_bytes(payload: Any) -> bytes:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return (text + "\n").encode("utf-8")


def _pending_check_results() -> list[RequiredCheckResultV1]:
    return [RequiredCheckResultV1(check=name, status="pending") for name in REQUIRED_CHECKS]


def _completed_check_results(hashes: Mapping[str, str]) -> list[RequiredCheckResultV1]:
    return [
        RequiredCheckResultV1(check=name, status="completed", evidence_hash=hashes[name])
        for name in REQUIRED_CHECKS
    ]


def _proof_record(
    *,
    status: str,
    commit_hash: str | None,
    check_results: list[RequiredCheckResultV1] | None = None,
) -> ProofClassRecordV1:
    return ProofClassRecordV1(
        status=status,  # type: ignore[arg-type]
        required_checks=list(REQUIRED_CHECKS),
        required_check_results=check_results or _pending_check_results(),
        commit_hash=commit_hash,
    )


def _sanitized_config_error(exc: Exception) -> PrivateRunConfigError:
    if isinstance(exc, ValidationError):
        for err in exc.errors(include_url=False, include_context=False, include_input=False):
            msg = str(err.get("msg", ""))
            if "at least two" in msg:
                return PrivateRunConfigError("scope must name at least two matching systems")
    return PrivateRunConfigError("private-run config is invalid")


def load_private_run_config(path: Path | str) -> PrivateRunConfigV1:
    raw = Path(path).read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PrivateRunConfigError("private-run config must be valid JSON") from exc
    if not isinstance(payload, Mapping):
        raise PrivateRunConfigError("private-run config must be a JSON object")
    _reject_sensitive_keys(payload)
    try:
        config = PrivateRunConfigV1.model_validate(payload)
    except Exception as exc:
        raise _sanitized_config_error(exc) from None
    return config


def _reject_sensitive_keys(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if _SENSITIVE_KEY_RE.fullmatch(str(key)):
                raise PrivateRunConfigError("private-run config must not contain credential fields")
            _reject_sensitive_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_sensitive_keys(nested)


def _contains_control(text: str) -> bool:
    return any(ord(char) < 32 or ord(char) == 127 for char in text)


def _git_toplevel(start: Path) -> Path | None:
    probe = start
    if not probe.exists() or not probe.is_dir():
        probe = probe.parent
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=probe,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    text = result.stdout.strip()
    return Path(text) if text else None


def _resolve_private_root(raw_root: Path | str, *, cwd: Path | None = None) -> tuple[str, Path]:
    raw = str(raw_root).strip().replace("\\", "/")
    if not raw:
        raise PrivateRunPathError("private output root is required")
    if _contains_control(raw) or "\x00" in raw or raw.startswith("~/") or raw == "~":
        raise PrivateRunPathError("private output root is malformed")
    if raw == ".." or _TRAVERSAL_RE.search(raw):
        raise PrivateRunPathError("private output root must not contain traversal")
    if raw.startswith("/") or _WINDOWS_ABS_RE.match(raw):
        raise PrivateRunPathError("private output root must not be absolute")
    relative = raw.lstrip("./")
    allowed = False
    for prefix in IGNORED_PRIVATE_ROOT_PREFIXES:
        if relative == prefix or relative.startswith(prefix + "/"):
            allowed = True
    if not allowed:
        raise PrivateRunPathError("private output root must be an ignored unpublished prefix")
    base = cwd or Path.cwd()
    probe = base
    for part in Path(relative).parts:
        probe = probe / part
        if probe.is_symlink():
            raise PrivateRunPathError("private output root must not be a symlink")
    root = (base / relative).resolve(strict=False)
    if root.exists() and root.is_symlink():
        raise PrivateRunPathError("private output root must not be a symlink")
    if root.exists() and not root.is_dir():
        raise PrivateRunPathError("private output root must be a directory")
    return relative, root


def _assert_destination_unpublished(relative: str, root: Path) -> None:
    """Prove ignore rules against the actual destination checkout, never a caller override."""
    toplevel = _git_toplevel(root) or _git_toplevel(Path.cwd())
    if toplevel is None:
        return
    try:
        destination = root.resolve(strict=False).relative_to(toplevel.resolve())
    except ValueError:
        return
    gitignore = toplevel / ".gitignore"
    if not gitignore.is_file():
        raise PrivateRunPathError("repository gitignore is missing a required private prefix")
    text = gitignore.read_text(encoding="utf-8")
    for prefix in IGNORED_PRIVATE_ROOT_PREFIXES:
        if f"{prefix}/" not in text and prefix not in text.splitlines():
            raise PrivateRunPathError("repository gitignore is missing a required private prefix")
    try:
        checked = subprocess.run(
            ["git", "check-ignore", "-q", "--", destination.as_posix()],
            cwd=toplevel,
            check=False,
            capture_output=True,
        )
    except OSError as exc:
        raise PrivateRunPathError("unable to prove private root is unpublished") from exc
    if checked.returncode != 0:
        raise PrivateRunPathError("private output root is not gitignored")
    _ = relative


def implementation_head_state(repo: Path = PACKAGE_ROOT) -> tuple[str, bool]:
    """Return the implementation HEAD digest and whether the worktree is dirty."""
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )
        porcelain = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise PrivateRunConfigError("private-run commit hash is unavailable") from exc
    digest = head.stdout.strip().lower()
    if len(digest) != 40:
        raise PrivateRunConfigError("private-run commit hash is unavailable")
    return digest, bool(porcelain.stdout.strip())


def _require_clean_head() -> str:
    commit, dirty = implementation_head_state()
    if dirty:
        raise PrivateRunConfigError("private-run requires a clean implementation commit")
    return commit


def classify_transport_provenance(transport: Any) -> str:
    inner = transport
    while isinstance(inner, _GetOnlyBoundedTransport):
        inner = inner._inner
    if type(inner) is HubSpotHttpTransport and bool(getattr(inner, "uses_live_network", False)):
        return "native"
    if type(inner) is StripeHttpTransport and bool(getattr(inner, "uses_live_network", False)):
        return "native"
    return "simulated"


def open_native_live_transports(
    *, runtime_mode: str, environ: Mapping[str, str]
) -> tuple[HubSpotHttpTransport, StripeHttpTransport]:
    """Construct native GET-only transports after credential gates. Does not request."""
    hubspot = HubSpotHttpTransport(token=environ[REQUIRED_ENV_BINDINGS["hubspot"]])
    stripe_env = REQUIRED_ENV_BINDINGS["stripe"]
    previous = os.environ.get(stripe_env)
    os.environ[stripe_env] = environ[stripe_env]
    try:
        stripe = StripeHttpTransport(mode=runtime_mode)
    except Exception:
        hubspot.close()
        raise PrivateRunCredentialError("native source read failed") from None
    finally:
        if previous is None:
            os.environ.pop(stripe_env, None)
        else:
            os.environ[stripe_env] = previous
    return hubspot, stripe


def _parse_provider_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    if isinstance(value, str) and value.strip():
        text = value.strip()
        if text.isdigit():
            return datetime.fromtimestamp(int(text), tz=timezone.utc)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return None


def _record_timestamp(record: Mapping[str, Any], *, system: str) -> datetime | None:
    candidates: list[Any] = []
    if system == "hubspot":
        properties = record.get("properties")
        props = properties if isinstance(properties, Mapping) else {}
        candidates = [
            record.get("createdAt"),
            props.get("createdate"),
            record.get("updatedAt"),
            props.get("lastmodifieddate"),
            props.get("last_activity_at"),
            props.get("closedate"),
            props.get("closed_at"),
        ]
    else:
        candidates = [
            record.get("created"),
            record.get("attempted_at"),
            record.get("paid_at"),
            record.get("canceled_at"),
            record.get("renewal_at"),
            record.get("trial_end"),
        ]
    for candidate in candidates:
        parsed = _parse_provider_timestamp(candidate)
        if parsed is not None:
            return parsed
    return None


def _hubspot_record_count(snapshot: Mapping[str, Any]) -> int:
    contacts = snapshot.get("contacts")
    deals = snapshot.get("deals")
    return (len(contacts) if isinstance(contacts, list) else 0) + (
        len(deals) if isinstance(deals, list) else 0
    )


def _stripe_record_count(snapshot: Mapping[str, Any]) -> int:
    total = 0
    for name in RESOURCE_NAMES:
        records = snapshot.get(name)
        if isinstance(records, list):
            total += len(records)
    lines = snapshot.get("invoice_lines")
    if isinstance(lines, Mapping):
        for records in lines.values():
            if isinstance(records, list):
                total += len(records)
    return total


def _filter_record_list(
    records: list[Any], *, system: str, bounds: PrivateRunBoundsV1
) -> tuple[list[Any], int, int]:
    kept: list[Any] = []
    excluded = 0
    unknown = 0
    for record in records:
        if not isinstance(record, Mapping):
            excluded += 1
            continue
        stamp = _record_timestamp(record, system=system)
        if stamp is None:
            unknown += 1
            continue
        if stamp < bounds.window_start or stamp > bounds.window_end:
            excluded += 1
            continue
        kept.append(record)
    return kept, excluded, unknown


def apply_selection_window(
    snapshots: Mapping[str, Mapping[str, Any]],
    config: PrivateRunConfigV1,
) -> tuple[
    dict[str, dict[str, Any]],
    dict[str, int],
    dict[str, int],
    dict[str, int],
    dict[str, int],
]:
    fetched_counts = {
        "hubspot": _hubspot_record_count(snapshots["hubspot"]),
        "stripe": _stripe_record_count(snapshots["stripe"]),
    }
    bounds = config.scope.bounds
    if bounds is None:
        windowed = {
            "hubspot": json.loads(hubspot_canonical_bytes(snapshots["hubspot"]).decode("utf-8")),
            "stripe": json.loads(stripe_canonical_bytes(snapshots["stripe"]).decode("utf-8")),
        }
        excluded = {name: 0 for name in fetched_counts}
        unknown_counts = {name: 0 for name in fetched_counts}
        return windowed, fetched_counts, dict(fetched_counts), excluded, unknown_counts

    hubspot = json.loads(hubspot_canonical_bytes(snapshots["hubspot"]).decode("utf-8"))
    contacts, contact_excluded, contact_unknown = _filter_record_list(
        list(hubspot.get("contacts") or []), system="hubspot", bounds=bounds
    )
    deals, deal_excluded, deal_unknown = _filter_record_list(
        list(hubspot.get("deals") or []), system="hubspot", bounds=bounds
    )
    kept_contacts = {str(item["id"]) for item in contacts}
    kept_deals = {str(item["id"]) for item in deals}
    links = [
        link
        for link in list(hubspot.get("deal_to_contact_links") or [])
        if isinstance(link, Mapping)
        and str(link.get("deal_id")) in kept_deals
        and str(link.get("contact_id")) in kept_contacts
    ]
    hubspot["contacts"] = contacts
    hubspot["deals"] = deals
    hubspot["deal_to_contact_links"] = links

    stripe = json.loads(stripe_canonical_bytes(snapshots["stripe"]).decode("utf-8"))
    stripe_excluded = 0
    stripe_unknown = 0
    kept_ids: set[str] = set()
    for name in RESOURCE_NAMES:
        kept, dropped, unknown_stamps = _filter_record_list(
            list(stripe.get(name) or []), system="stripe", bounds=bounds
        )
        stripe[name] = kept
        stripe_excluded += dropped
        stripe_unknown += unknown_stamps
        kept_ids.update(str(item["id"]) for item in kept if isinstance(item, Mapping))
    filtered_lines: dict[str, list[Any]] = {}
    for invoice_id, records in dict(stripe.get("invoice_lines") or {}).items():
        if invoice_id not in kept_ids:
            stripe_excluded += len(records) if isinstance(records, list) else 0
            continue
        kept, dropped, unknown_stamps = _filter_record_list(
            list(records or []), system="stripe", bounds=bounds
        )
        stripe_excluded += dropped
        stripe_unknown += unknown_stamps
        filtered_lines[invoice_id] = kept
        kept_ids.update(str(item["id"]) for item in kept if isinstance(item, Mapping))
    stripe["invoice_lines"] = filtered_lines
    stripe["relationships"] = [
        row
        for row in list(stripe.get("relationships") or [])
        if isinstance(row, Mapping)
        and str(row.get("child_id")) in kept_ids
        and str(row.get("parent_id")) in kept_ids
    ]
    stripe["economic_units"] = [
        unit
        for unit in list(stripe.get("economic_units") or [])
        if isinstance(unit, Mapping) and str(unit.get("invoice_id")) in kept_ids
    ]
    stripe["record_count"] = _stripe_record_count(stripe)
    windowed = {"hubspot": hubspot, "stripe": stripe}
    windowed_counts = {
        "hubspot": _hubspot_record_count(hubspot),
        "stripe": _stripe_record_count(stripe),
    }
    excluded_counts = {
        "hubspot": contact_excluded + deal_excluded,
        "stripe": stripe_excluded,
    }
    unknown_counts = {
        "hubspot": contact_unknown + deal_unknown,
        "stripe": stripe_unknown,
    }
    return windowed, fetched_counts, windowed_counts, excluded_counts, unknown_counts


def _run_id_from_private_bytes(config_bytes: bytes, snapshot_bytes: Mapping[str, bytes]) -> str:
    digest = sha256_bytes(
        _canonical_json_bytes(
            {
                "config_sha256": sha256_bytes(config_bytes),
                "snapshots": {
                    name: sha256_bytes(payload) for name, payload in sorted(snapshot_bytes.items())
                },
            }
        )
    )
    return f"prv_{digest[:16]}"


def _parse_typed_check_evidence(
    check_evidence: Mapping[str, Any], *, expected_commit: str
) -> dict[str, PassingCheckReceiptV1]:
    if set(check_evidence) != set(REQUIRED_CHECKS):
        raise PrivateRunConfigError("check evidence is incomplete")
    parsed: dict[str, PassingCheckReceiptV1] = {}
    for name in REQUIRED_CHECKS:
        raw = check_evidence[name]
        if not isinstance(raw, Mapping):
            raise PrivateRunConfigError("check evidence must be typed passing receipts")
        try:
            receipt = PassingCheckReceiptV1.model_validate(raw)
        except Exception:
            raise PrivateRunConfigError("check evidence must be typed passing receipts") from None
        if receipt.check != name:
            raise PrivateRunConfigError("check evidence must be typed passing receipts")
        if receipt.commit != expected_commit:
            raise PrivateRunConfigError("check evidence commit does not match the private run")
        parsed[name] = receipt
    return parsed


def _parse_canonical_safety(data: bytes, model: type[Any]) -> Any:
    try:
        parsed = model.model_validate_json(data)
    except Exception:
        raise PrivateRunConfigError("private-run provenance evidence is invalid") from None
    if parsed.to_canonical_json() != data:
        raise PrivateRunConfigError("private-run provenance evidence is invalid")
    return parsed


def _derive_transport_provenance(
    *,
    audit: SafeRequestAuditLogV1,
    assertion: NoMutationAssertionV1,
    source_hashes: Mapping[str, str],
    systems: list[str],
) -> dict[str, str]:
    if audit_digest(audit) != assertion.audit_digest:
        raise PrivateRunConfigError("request audit digest does not match no-mutation assertion")
    methods = Counter(record.method for record in audit.records)
    if {key: methods[key] for key in sorted(methods)} != assertion.allowed_methods_summary:
        raise PrivateRunConfigError(
            "request audit method counts do not match no-mutation assertion"
        )
    if assertion.request_count != len(audit.records):
        raise PrivateRunConfigError(
            "request audit request count does not match no-mutation assertion"
        )
    if dict(assertion.attached_receipt_hashes) != dict(source_hashes):
        raise PrivateRunConfigError("no-mutation attached receipts do not match source receipts")
    families = Counter(str(record.family) for record in audit.records)
    for family, count in families.items():
        if assertion.scope_counts.get(family) != count:
            raise PrivateRunConfigError("request audit host/path counts do not match assertion")
    hubspot_reads = sum(
        1
        for record in audit.records
        if record.method == "GET"
        and record.host == HUBSPOT_HOST
        and record.family == "hubspot_read"
    )
    stripe_reads = sum(
        1
        for record in audit.records
        if record.method == "GET" and record.host == STRIPE_HOST and record.family == "stripe_read"
    )
    if assertion.live_status == "live-unverified":
        if hubspot_reads < 1 or stripe_reads < 1:
            raise PrivateRunConfigError(
                "native provenance requires HubSpot and Stripe allowlisted reads"
            )
        return {name: "native" for name in systems}
    if assertion.live_status != "fixture-only":
        raise PrivateRunConfigError("private-run live status is invalid")
    return {name: "simulated" for name in systems}


def _load_bound_release_safety_packet(
    linked_live_receipts: Mapping[str, Path] | None = None,
) -> ReleaseSafetyEvidencePacketV1:
    raw = RELEASE_SAFETY_EVIDENCE_PACKET.read_bytes()
    try:
        packet = ReleaseSafetyEvidencePacketV1.model_validate_json(raw)
    except Exception:
        raise PrivateRunConfigError("release safety packet is invalid") from None
    if packet.to_canonical_json() != raw:
        raise PrivateRunConfigError("release safety packet is invalid")
    try:
        validate_release_safety_evidence_packet(
            packet, root=PACKAGE_ROOT, linked_live_receipts=linked_live_receipts
        )
    except ValueError as exc:
        raise PrivateRunConfigError(
            "release safety packet is not bound to this implementation"
        ) from exc
    if set(packet.check_hashes) != set(REQUIRED_CHECKS) or set(packet.check_evidence) != set(
        REQUIRED_CHECKS
    ):
        raise PrivateRunConfigError("release safety packet check coverage is incomplete")
    return packet


def _linked_live_receipt_paths(root: Path, systems: list[str]) -> dict[str, Path]:
    paths = {
        "provenance/no-mutation-assertion.json": root / "provenance/no-mutation-assertion.json",
    }
    for name in systems:
        relative = f"receipts/{name}.source-receipt.json"
        paths[relative] = root / relative
    return paths


def _linked_live_receipt_hashes(root: Path, systems: list[str]) -> dict[str, str]:
    return {
        label: sha256_bytes(path.read_bytes())
        for label, path in _linked_live_receipt_paths(root, systems).items()
    }


def _completed_proof_hashes(record: ProofClassRecordV1) -> dict[str, str]:
    return {
        item.check: item.evidence_hash
        for item in record.required_check_results
        if item.evidence_hash is not None
    }


def _assert_live_release_safety_packet(
    root: Path, manifest: PrivateRunManifestV1, packet: ReleaseSafetyEvidencePacketV1
) -> None:
    if packet.live_status not in {"live-unverified", "live-verified"}:
        raise PrivateRunConfigError("completed live proof requires a live release safety packet")
    expected = _linked_live_receipt_hashes(root, list(manifest.systems))
    if dict(packet.linked_live_receipt_hashes) != expected:
        raise PrivateRunConfigError("completed live proof requires linked live receipt hashes")


def _assert_completed_proofs_match_release_safety(
    *,
    root: Path,
    manifest: PrivateRunManifestV1,
    operator: OperatorReviewPacketV1,
    provenance: Mapping[str, str],
) -> None:
    fixture = manifest.proof_classes["fixture_proof"]
    live = manifest.proof_classes["live_proof"]
    human = manifest.proof_classes["human_verdict"]
    if fixture.status != "completed" and live.status != "completed" and human.status != "completed":
        return
    packet = _load_bound_release_safety_packet(
        linked_live_receipts=_linked_live_receipt_paths(root, list(manifest.systems))
    )
    expected = {name: packet.check_hashes[name] for name in REQUIRED_CHECKS}
    for name in BINDABLE_PROOF_CLASSES:
        record = manifest.proof_classes[name]
        if record.status != "completed":
            continue
        if _completed_proof_hashes(record) != expected:
            raise PrivateRunConfigError(
                "completed proof hashes must match the bound release safety packet"
            )
        if name == "live_proof":
            _assert_live_release_safety_packet(root, manifest, packet)
    if human.status != "completed":
        return
    if manifest.mode != "live":
        raise PrivateRunConfigError("human verdict requires live mode")
    if any(kind != "native" for kind in provenance.values()):
        raise PrivateRunConfigError("human verdict requires native source provenance")
    if live.status != "completed":
        raise PrivateRunConfigError("human verdict requires completed live proof")
    if _completed_proof_hashes(human) != expected or operator.check_evidence_hashes != expected:
        raise PrivateRunConfigError(
            "completed proof hashes must match the bound release safety packet"
        )
    if operator.run_id != manifest.run_id or operator.bound_commit_hash != manifest.commit_hash:
        raise PrivateRunConfigError("human verdict packet does not agree with the manifest")


def _reject_self_authored_check_evidence(check_evidence: Mapping[str, Any]) -> None:
    for value in check_evidence.values():
        if isinstance(value, Mapping):
            digest = str(value.get("evidence_sha256", "")).strip().lower()
            if digest == "a" * 64 or "status" in value or "command" in value:
                raise PrivateRunConfigError("check evidence must not be self-authored")
        elif isinstance(value, str) and value.strip().lower() == "a" * 64:
            raise PrivateRunConfigError("check evidence must not be self-authored")


def _validate_credentials(config: PrivateRunConfigV1, environ: Mapping[str, str]) -> None:
    for system in config.scope.systems:
        expected = REQUIRED_ENV_BINDINGS[system]
        if config.credentials.bindings.get(system) != expected:
            raise PrivateRunCredentialError(
                "credential binding does not match the required environment name"
            )
    if config.scope.mode != "live":
        return
    if config.scope.authorization != "matthew-authorized":
        raise PrivateRunCredentialError("live private-run is not authorized")
    for system in config.scope.systems:
        env_name = REQUIRED_ENV_BINDINGS[system]
        value = environ.get(env_name)
        if not isinstance(value, str) or not value.strip():
            raise PrivateRunCredentialError("environment credential is missing")
        if system == "stripe":
            token = value.strip()
            expected_mode = config.scope.runtime_mode
            matched = token.startswith(f"rk_{expected_mode}_") or token.startswith(
                f"sk_{expected_mode}_"
            )
            if not matched:
                raise PrivateRunCredentialError("environment credential is mode-mismatched")
    if environ.get(LIVE_ENABLE_ENV) != "1":
        raise PrivateRunCredentialError("live native read is not enabled")


def _read_live_systems(
    config: PrivateRunConfigV1,
    *,
    environ: Mapping[str, str],
    transports: Mapping[str, Any] | None = None,
) -> tuple[
    dict[str, dict[str, Any]],
    dict[str, str],
    datetime,
    SafeRequestAuditor,
    list[Any],
]:
    bounds = config.scope.bounds
    if bounds is None:
        raise PrivateRunConfigError("live mode requires explicit window and limit bounds")
    owned: list[Any] = []
    supplied = transports
    if supplied is None:
        native = open_native_live_transports(
            runtime_mode=config.scope.runtime_mode, environ=environ
        )
        owned.extend(native)
        supplied = {"hubspot": native[0], "stripe": native[1]}
    if any(name not in supplied for name in config.scope.systems):
        for item in owned:
            item.close()
        raise PrivateRunCredentialError("live private-run requires native GET-only transports")
    hubspot_inner = supplied["hubspot"]
    stripe_inner = supplied["stripe"]
    hubspot_native = classify_transport_provenance(hubspot_inner) == "native"
    stripe_native = classify_transport_provenance(stripe_inner) == "native"
    if hubspot_native and stripe_native:
        auditor = SafeRequestAuditor._for_native_transports(hubspot_inner, stripe_inner)
    elif hubspot_native or stripe_native:
        for item in owned:
            item.close()
        raise PrivateRunCredentialError("live private-run requires native GET-only transports")
    else:
        auditor = SafeRequestAuditor()
    clock = bounds.window_end
    hubspot_token = environ[REQUIRED_ENV_BINDINGS["hubspot"]]
    stripe_key = environ[REQUIRED_ENV_BINDINGS["stripe"]]
    hubspot_transport = _GetOnlyBoundedTransport(
        supplied["hubspot"],
        host=HUBSPOT_HOST,
        auditor=auditor,
        page_limit=bounds.page_limits["hubspot"],
        record_limit=bounds.record_limits["hubspot"],
    )
    stripe_transport = _GetOnlyBoundedTransport(
        supplied["stripe"],
        host=STRIPE_HOST,
        auditor=auditor,
        page_limit=bounds.page_limits["stripe"],
        record_limit=bounds.record_limits["stripe"],
    )
    try:
        hubspot_snapshot = fetch_crm_snapshot(
            hubspot_transport, token=hubspot_token, retrieved_at=clock
        )
        stripe_snapshot = StripeConnector(stripe_transport).fetch_snapshot(
            api_key=stripe_key, retrieved_at=clock
        )
    except PrivateRunError:
        raise
    except (HubSpotConnectorError, StripeConnectorError, OSError, RuntimeError):
        raise PrivateRunCredentialError("native source read failed") from None
    snapshots = {"hubspot": hubspot_snapshot, "stripe": stripe_snapshot}
    provenance = {
        "hubspot": classify_transport_provenance(supplied["hubspot"]),
        "stripe": classify_transport_provenance(supplied["stripe"]),
    }
    return snapshots, provenance, clock, auditor, owned


def _fixture_systems(
    config: PrivateRunConfigV1,
) -> tuple[dict[str, dict[str, Any]], dict[str, bytes], dict[str, SourceReceiptV1], datetime]:
    fixture = config.scope.fixture or SYNTHETIC_SAAS_V1
    if fixture != SYNTHETIC_SAAS_V1:
        raise PrivateRunConfigError("fixture replay supports only synthetic-saas-v1")
    from found_money.scenarios.assets import load_scenario_definition

    definition = load_scenario_definition(fixture)
    snapshots, raw, receipts = load_normalized_scenario_snapshots(
        fixture, retrieved_at=definition.clock
    )
    missing = [name for name in config.scope.systems if name not in snapshots]
    if missing:
        raise PrivateRunConfigError("fixture does not provide the declared matching systems")
    return snapshots, raw, receipts, definition.clock


def _safe_config(config: PrivateRunConfigV1) -> dict[str, Any]:
    return {
        "schema_version": "found-money-build-source.v1",
        "mode": "fixture",
        "run_mode": "private",
        "fixture": config.scope.fixture,
        "credential_declaration": "none",
        "credential_runtime": "test",
    }


def _bundle_from_engine(engine: Any) -> _DiagnosticBundle:
    ranked = rank_money_map(engine.enriched_money_map, engine.ledger, engine.candidates)
    html = render_recovery_room_html(
        ranked,
        engine.strategy_run.complete_plays(),
        contribution_ledger=engine.ledger,
    )
    launch = build_launch_pack(
        LaunchPackInputs(
            money_map=ranked,
            recovery_plays=engine.strategy_run.recovery_plays,
            evidence_packet=engine.strategy_run.packet,
            contribution_ledger=engine.ledger,
            identity_graph=engine.graph,
            candidates=engine.candidates,
            exclusions=engine.exclusions,
            business_profile=canonical_saas_business_profile(),
            source_snapshots=engine.raw_snapshots,
            mode="public",
        )
    )
    return _DiagnosticBundle(
        identity_aggregate=engine.identity_aggregate,
        event_public=engine.event_public,
        ledger=engine.ledger,
        ranked=ranked,
        html=html,
        launch=launch,
        snapshots=engine.raw_snapshots,
    )


def _ungrounded_canonical_strategy(ranked: Any, *, built_at: datetime) -> Any:
    """Keep launch-pack complete plays when window quarantine changes fixture grounding."""
    from found_money.contracts.strategy import StrategyAuditReceiptV1, StrategyTokenUsageV1
    from found_money.strategy.campaign import (
        CanonicalSaasStrategyRun,
        _canonical_plays,
        build_differentiation_report,
    )

    packet = build_grounded_strategy_packet(
        ranked, canonical_saas_business_profile(), built_at=built_at
    )
    plays = _canonical_plays(ranked.run_id, built_at)
    report = build_differentiation_report(plays)
    receipt = StrategyAuditReceiptV1(
        run_id=ranked.run_id,
        status="completed",
        prompt_version="found-money-three-play.v1",
        prompt_hash=sha256_bytes(b"deterministic three-play SaaS fixture from frozen evidence"),
        output_schema_version="recovery-plays.v1",
        output_schema_hash=sha256_bytes(b"complete recovery play and concept card contract v1"),
        configured_model_id="fixture-strategy-v1",
        returned_model_id="fixture-strategy-v1",
        response_id="fixture-three-play-response-v1",
        token_usage=StrategyTokenUsageV1(input_tokens=0, output_tokens=0),
        attempt_count=1,
        evidence_packet_hash=sha256_bytes(packet.to_canonical_json()),
        output_hash=sha256_bytes(plays.to_canonical_json()),
    )
    return CanonicalSaasStrategyRun(packet, plays, receipt, report)


def _unlocked_diagnostics(
    *,
    run_id: str,
    snapshots: Mapping[str, Mapping[str, Any]],
    clock: datetime,
    systems: list[str],
) -> _DiagnosticBundle:
    nodes = normalize_source_records(snapshots, default_observed_at=clock)
    graph = build_identity_graph(nodes, run_id=run_id, built_at=clock)
    detection = detect_event_families(snapshots, graph, run_id=run_id, built_at=clock)
    ledger = payment_rescue_ledger(
        build_value_ledger(detection.candidates, snapshots, built_at=clock)
    )
    money_map = build_money_map(ledger, detection.candidates, built_at=clock)
    ranked = rank_money_map(money_map, ledger, detection.candidates)
    try:
        strategy_run = build_canonical_saas_recovery_strategy(ranked, built_at=clock)
        enriched = apply_complete_plays_to_money_map(ranked, strategy_run.complete_plays())
        ranked = rank_money_map(enriched, ledger, detection.candidates)
        html = render_recovery_room_html(
            ranked, strategy_run.complete_plays(), contribution_ledger=ledger
        )
    except (CompleteStrategyGroundingError, ValueError):
        strategy_run = _ungrounded_canonical_strategy(ranked, built_at=clock)
        html = render_recovery_room_html(
            ranked, strategy_run.complete_plays(), contribution_ledger=ledger
        )
    launch = build_launch_pack(
        LaunchPackInputs(
            money_map=ranked,
            recovery_plays=strategy_run.recovery_plays,
            evidence_packet=strategy_run.packet,
            contribution_ledger=ledger,
            identity_graph=graph,
            candidates=detection.candidates,
            exclusions=detection.exclusions,
            business_profile=canonical_saas_business_profile(),
            source_snapshots=snapshots,
            mode="public",
        )
    )
    identity_aggregate = ScenarioIdentityAggregateV1(
        run_id=run_id,
        resolved_customer_count=len(graph.customers),
        ambiguous_cluster_count=len(graph.ambiguous_identities),
        source_systems=list(systems),
    )
    return _DiagnosticBundle(
        identity_aggregate=identity_aggregate,
        event_public=detection.public_projection,
        ledger=ledger,
        ranked=ranked,
        html=html,
        launch=launch,
        snapshots=snapshots,
    )


def _aggregate_from_stage(stage: PrivateRunSourceStageEvidenceV1) -> PrivateRunAggregateProofV1:
    return PrivateRunAggregateProofV1(
        run_id=stage.run_id,
        built_at=stage.built_at,
        systems=list(stage.systems),
        source_record_counts=stage.source_record_counts,
        fetched_record_counts=stage.fetched_record_counts,
        windowed_record_counts=stage.windowed_record_counts,
        window_excluded_record_counts=stage.window_excluded_record_counts,
        window_unknown_timestamp_counts=stage.window_unknown_timestamp_counts,
        resolved_customer_count=stage.resolved_customer_count,
        ambiguous_cluster_count=stage.ambiguous_cluster_count,
        candidates_by_family=stage.candidates_by_family,
        exclusion_count=stage.exclusion_count,
        data_gap_count=stage.data_gap_count,
        suppression_count=stage.suppression_count,
        identified_opportunity_minor=stage.identified_opportunity_minor,
        basis_counts_by_currency=stage.basis_counts_by_currency,
        run_scope=stage.run_scope,
    )


def _expected_public_tree() -> set[str]:
    return set(PRIVATE_RUN_ARTIFACTS)


def _list_files(root: Path) -> set[str]:
    return {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}


def _reject_committed_private_inputs(root: Path) -> None:
    toplevel = _git_toplevel(root)
    if toplevel is None:
        return
    for relative in PRIVATE_INPUT_ARTIFACTS:
        path = (root / relative).resolve(strict=False)
        try:
            tracked = path.relative_to(toplevel.resolve())
        except ValueError:
            continue
        listed = subprocess.run(
            ["git", "ls-files", "--", tracked.as_posix()],
            cwd=toplevel,
            check=False,
            capture_output=True,
            text=True,
        )
        if listed.stdout.strip():
            raise PrivateRunConfigError("private source inputs must not be committed")


def _assert_human_state_agreement(
    manifest: PrivateRunManifestV1, packet: OperatorReviewPacketV1
) -> None:
    human = manifest.proof_classes["human_verdict"]
    if human.status == "completed":
        if (
            packet.proof_class != "human"
            or packet.human_issued is not True
            or packet.issuer_kind != "human"
            or packet.run_id != manifest.run_id
            or packet.bound_commit_hash != manifest.commit_hash
            or packet.usefulness_verdict not in {"useful", "not_useful", "waived"}
        ):
            raise PrivateRunConfigError("human verdict packet does not agree with the manifest")
        expected = {
            item.check: item.evidence_hash
            for item in human.required_check_results
            if item.evidence_hash is not None
        }
        if packet.check_evidence_hashes != expected:
            raise PrivateRunConfigError("human verdict packet does not agree with the manifest")
        return
    if (
        packet.human_issued
        or packet.proof_class == "human"
        or packet.usefulness_verdict != "not_started"
        or human.status != "not_started"
    ):
        raise PrivateRunConfigError("pending operator packet does not agree with the manifest")


def _pipeline_bundle(
    *,
    config: PrivateRunConfigV1,
    windowed: Mapping[str, Mapping[str, Any]],
    windowed_bytes: Mapping[str, bytes],
    run_id: str,
    clock: datetime,
) -> _DiagnosticBundle:
    if config.scope.mode == "fixture":
        engine = run_scenario_engine(
            run_id=run_id,
            safe_config=_safe_config(config),
            fixture_id=str(config.scope.fixture),
            snapshots=windowed,
            snapshot_bytes=windowed_bytes,
        )
        return _bundle_from_engine(engine)
    return _unlocked_diagnostics(
        run_id=run_id,
        snapshots=windowed,
        clock=clock,
        systems=list(config.scope.systems),
    )


def _diagnostics_from_bundle(
    bundle: _DiagnosticBundle,
    *,
    run_id: str,
    windowed_counts: Mapping[str, int],
    fetched_counts: Mapping[str, int],
    excluded_counts: Mapping[str, int],
    unknown_counts: Mapping[str, int],
) -> PrivateRunDiagnosticsV1:
    overlap_count = 0
    if bundle.ledger.overlap_ledger is not None:
        overlap_count = len(bundle.ledger.overlap_ledger.records)
    return PrivateRunDiagnosticsV1(
        run_id=run_id,
        stages={
            "identity": "completed",
            "ambiguity": "completed",
            "event": "completed",
            "exclusion": "completed",
            "value": "completed",
            "overlap": "completed",
            "ranking": "completed",
            "strategy": "completed",
            "rendering": "completed",
            "activation": "completed",
        },
        resolved_customer_count=bundle.identity_aggregate.resolved_customer_count,
        ambiguous_cluster_count=bundle.identity_aggregate.ambiguous_cluster_count,
        candidates_by_family=dict(bundle.event_public.candidates_by_family),
        exclusion_count=bundle.event_public.exclusion_count,
        data_gap_count=bundle.event_public.data_gap_count + sum(unknown_counts.values()),
        overlap_count=overlap_count,
        source_record_counts=dict(sorted(windowed_counts.items())),
        fetched_record_counts=dict(sorted(fetched_counts.items())),
        windowed_record_counts=dict(sorted(windowed_counts.items())),
        window_excluded_record_counts=dict(sorted(excluded_counts.items())),
        window_unknown_timestamp_counts=dict(sorted(unknown_counts.items())),
        identified_opportunity_minor=dict(bundle.ranked.identified_opportunity_minor),
        basis_counts_by_currency=dict(bundle.ranked.basis_counts_by_currency),
        rendering_html_sha256=sha256_bytes(bundle.html.encode("utf-8")),
        activation_manifest_sha256=sha256_bytes(bundle.launch.manifest.to_canonical_json()),
    )


def _run_scope(
    config: PrivateRunConfigV1, unknown_counts: Mapping[str, int] | None = None
) -> dict[str, str]:
    scope = {
        "mode": config.scope.mode,
        "systems": ",".join(config.scope.systems),
        "fixture": str(config.scope.fixture or "none"),
        "window_filter": "none" if config.scope.bounds is None else "local-provider-timestamp",
        "window_unknown_timestamp_count": str(sum((unknown_counts or {}).values())),
    }
    bounds = config.scope.bounds
    if bounds is not None:
        scope["window_start"] = (
            bounds.window_start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        )
        scope["window_end"] = (
            bounds.window_end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        )
    return scope


def _stage_from_parts(
    *,
    run_id: str,
    clock: datetime,
    config: PrivateRunConfigV1,
    source_hashes: Mapping[str, str],
    source_set_hash: str,
    config_sha256: str,
    fetched_snapshot_sha256: Mapping[str, str],
    source_snapshot_sha256: Mapping[str, str],
    provenance: Mapping[str, str],
    diagnostics: PrivateRunDiagnosticsV1,
    bundle: _DiagnosticBundle,
    diagnostics_bytes: bytes,
) -> PrivateRunSourceStageEvidenceV1:
    return PrivateRunSourceStageEvidenceV1(
        run_id=run_id,
        built_at=clock,
        systems=list(config.scope.systems),
        source_receipt_hashes=dict(source_hashes),
        source_set_hash=source_set_hash,
        config_sha256=config_sha256,
        fetched_snapshot_sha256=dict(fetched_snapshot_sha256),
        source_snapshot_sha256=dict(source_snapshot_sha256),
        source_transport_provenance=dict(provenance),
        source_record_counts=diagnostics.source_record_counts,
        fetched_record_counts=diagnostics.fetched_record_counts,
        windowed_record_counts=diagnostics.windowed_record_counts,
        window_excluded_record_counts=diagnostics.window_excluded_record_counts,
        window_unknown_timestamp_counts=diagnostics.window_unknown_timestamp_counts,
        resolved_customer_count=diagnostics.resolved_customer_count,
        ambiguous_cluster_count=diagnostics.ambiguous_cluster_count,
        candidates_by_family=diagnostics.candidates_by_family,
        exclusion_count=diagnostics.exclusion_count,
        data_gap_count=diagnostics.data_gap_count,
        suppression_count=diagnostics.exclusion_count,
        identified_opportunity_minor=diagnostics.identified_opportunity_minor,
        basis_counts_by_currency=diagnostics.basis_counts_by_currency,
        run_scope=_run_scope(config, diagnostics.window_unknown_timestamp_counts),
        identity_aggregate_sha256=sha256_bytes(bundle.identity_aggregate.to_canonical_json()),
        event_public_sha256=sha256_bytes(bundle.event_public.to_canonical_json()),
        diagnostics_sha256=sha256_bytes(diagnostics_bytes),
    )


def validate_private_run_tree(private_root: Path | str) -> PrivateRunSourceStageEvidenceV1:
    """Independently recompute from private snapshots; do not trust self-authored summaries."""
    root = Path(private_root)
    present = _list_files(root)
    private_files = {path for path in present if path.startswith(PRIVATE_INPUT_PREFIX)}
    public_files = present - private_files
    if private_files != set(PRIVATE_INPUT_ARTIFACTS):
        raise PrivateRunConfigError("private-run output tree does not match the artifact closure")
    if public_files != _expected_public_tree():
        raise PrivateRunConfigError("private-run output tree does not match the artifact closure")
    if any(
        Path(path).name.endswith(".snapshot.json") or Path(path).name.endswith(".fetched.json")
        for path in public_files
    ):
        raise PrivateRunConfigError("private source inputs must not be public artifacts")
    _reject_committed_private_inputs(root)
    manifest = parse_private_run_manifest((root / "private-run.json").read_bytes())
    hashed = {item.path: item.sha256 for item in manifest.artifacts}
    if set(hashed) != public_files - {"private-run.json"}:
        raise PrivateRunConfigError("private-run output tree does not match the artifact closure")
    for relative, digest in hashed.items():
        if sha256_bytes((root / relative).read_bytes()) != digest:
            raise PrivateRunConfigError("private-run manifest hash does not match artifact bytes")
    packet = parse_operator_packet((root / "operator-packet.json").read_bytes())
    if set(packet.evidence_refs) != set(hashed) - {"operator-packet.json"}:
        raise PrivateRunConfigError("operator evidence refs do not match private artifacts")
    for relative, digest in packet.evidence_refs.items():
        if sha256_bytes((root / relative).read_bytes()) != digest:
            raise PrivateRunConfigError("operator evidence refs do not match private artifacts")
    _assert_human_state_agreement(manifest, packet)
    config_bytes = (root / "private/config.json").read_bytes()
    try:
        config = parse_private_run_config(config_bytes)
    except Exception:
        raise PrivateRunConfigError("private-run config is invalid") from None
    fetched = {
        "hubspot": json.loads((root / "private/sources/hubspot.fetched.json").read_text("utf-8")),
        "stripe": json.loads((root / "private/sources/stripe.fetched.json").read_text("utf-8")),
    }
    stored_windowed_bytes = {
        "hubspot": (root / "private/sources/hubspot.snapshot.json").read_bytes(),
        "stripe": (root / "private/sources/stripe.snapshot.json").read_bytes(),
    }
    windowed, fetched_counts, windowed_counts, excluded_counts, unknown_counts = (
        apply_selection_window(fetched, config)
    )
    rebuilt_windowed_bytes = {
        "hubspot": hubspot_canonical_bytes(windowed["hubspot"]),
        "stripe": stripe_canonical_bytes(windowed["stripe"]),
    }
    if rebuilt_windowed_bytes != stored_windowed_bytes:
        raise PrivateRunConfigError("private-run windowed snapshots do not match fetched inputs")
    clock = manifest.built_at
    receipts = {
        "hubspot": build_crm_snapshot_receipt(windowed["hubspot"], retrieved_at=clock),
        "stripe": build_stripe_snapshot_receipt(windowed["stripe"], retrieved_at=clock),
    }
    run_id = _run_id_from_private_bytes(config_bytes, rebuilt_windowed_bytes)
    if run_id != manifest.run_id:
        raise PrivateRunConfigError("private-run run_id does not bind private source bytes")
    bundle = _pipeline_bundle(
        config=config,
        windowed=windowed,
        windowed_bytes=rebuilt_windowed_bytes,
        run_id=run_id,
        clock=clock,
    )
    diagnostics = _diagnostics_from_bundle(
        bundle,
        run_id=run_id,
        windowed_counts=windowed_counts,
        fetched_counts=fetched_counts,
        excluded_counts=excluded_counts,
        unknown_counts=unknown_counts,
    )
    source_hashes = {name: receipts[name].content_hash for name in config.scope.systems}
    receipt_paths = {name: f"receipts/{name}.source-receipt.json" for name in config.scope.systems}
    source_set_hash = compute_source_set_hash(
        {receipt_paths[name]: source_hashes[name] for name in config.scope.systems}
    )
    diagnostics_bytes = diagnostics.to_canonical_json()
    fetched_snapshot_sha256 = {
        name: sha256_bytes((root / f"private/sources/{name}.fetched.json").read_bytes())
        for name in config.scope.systems
    }
    source_snapshot_sha256 = {
        name: sha256_bytes(rebuilt_windowed_bytes[name]) for name in config.scope.systems
    }
    if manifest.fetched_snapshot_sha256 != fetched_snapshot_sha256:
        raise PrivateRunConfigError(
            "private-run fetched snapshot hash does not match private bytes"
        )
    if manifest.source_snapshot_sha256 != source_snapshot_sha256:
        raise PrivateRunConfigError("private-run source snapshot hash does not match private bytes")
    audit = _parse_canonical_safety(
        (root / "provenance/request-audit.json").read_bytes(), SafeRequestAuditLogV1
    )
    assertion = _parse_canonical_safety(
        (root / "provenance/no-mutation-assertion.json").read_bytes(), NoMutationAssertionV1
    )
    provenance = _derive_transport_provenance(
        audit=audit,
        assertion=assertion,
        source_hashes=source_hashes,
        systems=list(config.scope.systems),
    )
    if manifest.mode != config.scope.mode or list(manifest.systems) != list(config.scope.systems):
        raise PrivateRunConfigError("private-run manifest does not match private config")
    if manifest.fixture != config.scope.fixture:
        raise PrivateRunConfigError("private-run manifest does not match private config")
    if dict(manifest.source_transport_provenance) != provenance:
        raise PrivateRunConfigError("private-run provenance does not match request audit evidence")
    _assert_completed_proofs_match_release_safety(
        root=root,
        manifest=manifest,
        operator=packet,
        provenance=provenance,
    )
    stage = _stage_from_parts(
        run_id=run_id,
        clock=clock,
        config=config,
        source_hashes=source_hashes,
        source_set_hash=source_set_hash,
        config_sha256=sha256_bytes(config_bytes),
        fetched_snapshot_sha256=fetched_snapshot_sha256,
        source_snapshot_sha256=source_snapshot_sha256,
        provenance=provenance,
        diagnostics=diagnostics,
        bundle=bundle,
        diagnostics_bytes=diagnostics_bytes,
    )
    written_diag = (root / "diagnostics.json").read_bytes()
    written_stage = (root / SOURCE_STAGE_PATH).read_bytes()
    written_agg = (root / "aggregate-proof.json").read_bytes()
    if written_diag != diagnostics_bytes:
        raise PrivateRunConfigError("private-run diagnostics do not match private source bytes")
    if written_stage != stage.to_canonical_json():
        raise PrivateRunConfigError("private-run source-stage does not match private source bytes")
    rebuilt_agg = _aggregate_from_stage(stage)
    if written_agg != rebuilt_agg.to_canonical_json():
        raise PrivateRunConfigError("private-run aggregate does not match private source bytes")
    if sha256_bytes(config_bytes) != manifest.config_sha256:
        raise PrivateRunConfigError("private-run config hash does not match private config bytes")
    for name, relative in zip(manifest.systems, manifest.source_receipt_paths, strict=True):
        receipt = SourceReceiptV1.model_validate_json((root / relative).read_bytes())
        if (
            receipt.content_hash != source_hashes[name]
            or receipt.content_hash != manifest.source_hashes[name]
        ):
            raise PrivateRunConfigError(
                "private-run source receipt hash does not match the snapshot bytes"
            )
    if source_set_hash != manifest.source_set_hash:
        raise PrivateRunConfigError("private-run source set hash does not match receipts")
    if scan_output_tree(root):
        raise PrivateRunConfigError("private-run output failed public-safety scanning")
    return stage


def recompute_aggregate_proof(private_root: Path | str) -> PrivateRunAggregateProofV1:
    stage = validate_private_run_tree(private_root)
    return _aggregate_from_stage(stage)


def finalize_human_verdict(
    *,
    private_root: Path | str,
    run_id: str,
    operator_id: str,
    issuer_kind: str,
    issued_at: datetime,
    usefulness_verdict: str,
    rationale: str,
    limitations: list[str],
    unresolved_data_gaps: list[str],
) -> OperatorReviewPacketV1:
    """Record a human usefulness verdict for a native live run with completed live proof."""
    if issuer_kind != "human":
        raise PrivateRunConfigError("human verdicts must be issued by a human")
    root = Path(private_root)
    stage = validate_private_run_tree(root)
    if stage.run_id != run_id:
        raise PrivateRunConfigError("human verdict run_id does not match the private run")
    manifest = parse_private_run_manifest((root / "private-run.json").read_bytes())
    if manifest.mode != "live":
        raise PrivateRunConfigError("fixture runs cannot issue a usefulness verdict")
    if any(kind != "native" for kind in manifest.source_transport_provenance.values()):
        raise PrivateRunConfigError("human verdict requires native source provenance")
    live = manifest.proof_classes["live_proof"]
    if live.status != "completed":
        raise PrivateRunConfigError("human verdict requires completed live proof")
    commit = _require_clean_head()
    if commit != manifest.commit_hash:
        raise PrivateRunConfigError("human verdict commit does not match the private run")
    hashes = {
        item.check: item.evidence_hash
        for item in live.required_check_results
        if item.evidence_hash is not None
    }
    if set(hashes) != set(REQUIRED_CHECKS):
        raise PrivateRunConfigError("human verdict requires completed live proof")
    packet_path = root / "operator-packet.json"
    current = parse_operator_packet(packet_path.read_bytes())
    packet = OperatorReviewPacketV1(
        run_id=run_id,
        usefulness_verdict=usefulness_verdict,  # type: ignore[arg-type]
        rationale=rationale,
        limitations=limitations,
        unresolved_data_gaps=unresolved_data_gaps,
        evidence_refs=current.evidence_refs,
        human_issued=True,
        proof_class="human",
        issuer_kind="human",
        operator_id=operator_id,
        issued_at=issued_at,
        check_evidence_hashes=hashes,
        bound_commit_hash=commit,
    )
    human_record = _proof_record(
        status="completed",
        commit_hash=commit,
        check_results=_completed_check_results(hashes),
    )
    proof_classes = dict(manifest.proof_classes)
    proof_classes["human_verdict"] = human_record
    packet_bytes = packet.to_canonical_json()
    artifacts = [
        item
        if item.path != "operator-packet.json"
        else PrivateRunArtifactV1(path=item.path, sha256=sha256_bytes(packet_bytes))
        for item in manifest.artifacts
    ]
    updated = manifest.model_copy(update={"proof_classes": proof_classes, "artifacts": artifacts})
    write_artifact_set_atomic(
        root,
        {
            "operator-packet.json": packet_bytes,
            "private-run.json": updated.to_canonical_json(),
        },
    )
    validate_private_run_tree(root)
    return packet


def bind_release_safety_check_evidence(
    *,
    private_root: Path | str,
    proof_class: str,
) -> PrivateRunManifestV1:
    """Bind the committed release-safety packet check hashes. No caller check strings."""
    return bind_required_check_evidence(private_root=private_root, proof_class=proof_class)


def bind_required_check_evidence(
    *,
    private_root: Path | str,
    proof_class: str,
    check_evidence: Mapping[str, Any] | None = None,
) -> PrivateRunManifestV1:
    """Bind only the repository release-safety packet to fixture_proof or live_proof."""
    root = Path(private_root)
    validate_private_run_tree(root)
    manifest = parse_private_run_manifest((root / "private-run.json").read_bytes())
    if proof_class not in BINDABLE_PROOF_CLASSES:
        raise PrivateRunConfigError("check evidence cannot bind a human verdict")
    commit = _require_clean_head()
    if commit != manifest.commit_hash:
        raise PrivateRunConfigError("check binding commit does not match the private run")
    if proof_class == "live_proof":
        if manifest.mode != "live":
            raise PrivateRunConfigError("live proof requires native source provenance")
        if any(kind != "native" for kind in manifest.source_transport_provenance.values()):
            raise PrivateRunConfigError("live proof requires native source provenance")
    if check_evidence is not None:
        _reject_self_authored_check_evidence(check_evidence)
    packet = _load_bound_release_safety_packet(
        linked_live_receipts=_linked_live_receipt_paths(root, list(manifest.systems))
    )
    if proof_class == "live_proof":
        _assert_live_release_safety_packet(root, manifest, packet)
    if check_evidence is not None and dict(check_evidence) not in (
        dict(packet.check_hashes),
        dict(packet.check_evidence),
    ):
        raise PrivateRunConfigError("check evidence must match the bound release safety packet")
    hashes = {name: packet.check_hashes[name] for name in REQUIRED_CHECKS}
    record = _proof_record(
        status="completed",
        commit_hash=commit,
        check_results=_completed_check_results(hashes),
    )
    proof_classes = dict(manifest.proof_classes)
    proof_classes[proof_class] = record
    updated = manifest.model_copy(update={"proof_classes": proof_classes})
    write_artifact_set_atomic(root, {"private-run.json": updated.to_canonical_json()})
    validate_private_run_tree(root)
    return updated


def run_private_run(
    *,
    config_path: Path | str,
    private_root: Path | str,
    repo_root: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
    transports: Mapping[str, Any] | None = None,
    commit_hash: str | None = None,
    cwd: Path | None = None,
) -> PrivateRunResult:
    _ = repo_root
    if commit_hash is not None:
        raise PrivateRunConfigError("private-run commit hash must be derived from HEAD")
    env = environ if environ is not None else os.environ
    config = load_private_run_config(config_path)
    relative, root = _resolve_private_root(private_root, cwd=cwd)
    _assert_destination_unpublished(relative, root)
    _validate_credentials(config, env)
    head = _require_clean_head()
    owned: list[Any] = []
    try:
        auditor: SafeRequestAuditor
        if config.scope.mode == "live":
            fetched, provenance, clock, auditor, owned = _read_live_systems(
                config, environ=env, transports=transports
            )
        else:
            fetched, _ignored_raw, _ignored_receipts, clock = _fixture_systems(config)
            provenance = {name: "simulated" for name in config.scope.systems}
            auditor = SafeRequestAuditor()
        windowed, fetched_counts, windowed_counts, excluded_counts, unknown_counts = (
            apply_selection_window(fetched, config)
        )
        fetched_bytes = {
            "hubspot": hubspot_canonical_bytes(fetched["hubspot"]),
            "stripe": stripe_canonical_bytes(fetched["stripe"]),
        }
        windowed_bytes = {
            "hubspot": hubspot_canonical_bytes(windowed["hubspot"]),
            "stripe": stripe_canonical_bytes(windowed["stripe"]),
        }
        receipts = {
            "hubspot": build_crm_snapshot_receipt(windowed["hubspot"], retrieved_at=clock),
            "stripe": build_stripe_snapshot_receipt(windowed["stripe"], retrieved_at=clock),
        }
        config_bytes = config.to_canonical_json()
        run_id = _run_id_from_private_bytes(config_bytes, windowed_bytes)
        bundle = _pipeline_bundle(
            config=config,
            windowed=windowed,
            windowed_bytes=windowed_bytes,
            run_id=run_id,
            clock=clock,
        )
        diagnostics = _diagnostics_from_bundle(
            bundle,
            run_id=run_id,
            windowed_counts=windowed_counts,
            fetched_counts=fetched_counts,
            excluded_counts=excluded_counts,
            unknown_counts=unknown_counts,
        )
        diagnostics_bytes = diagnostics.to_canonical_json()
        receipt_paths = {
            name: f"receipts/{name}.source-receipt.json" for name in config.scope.systems
        }
        source_hashes = {name: receipts[name].content_hash for name in config.scope.systems}
        source_set_hash = compute_source_set_hash(
            {receipt_paths[name]: source_hashes[name] for name in config.scope.systems}
        )
        config_sha256 = sha256_bytes(config_bytes)
        fetched_snapshot_sha256 = {
            name: sha256_bytes(fetched_bytes[name]) for name in config.scope.systems
        }
        source_snapshot_sha256 = {
            name: sha256_bytes(windowed_bytes[name]) for name in config.scope.systems
        }
        stage = _stage_from_parts(
            run_id=run_id,
            clock=clock,
            config=config,
            source_hashes=source_hashes,
            source_set_hash=source_set_hash,
            config_sha256=config_sha256,
            fetched_snapshot_sha256=fetched_snapshot_sha256,
            source_snapshot_sha256=source_snapshot_sha256,
            provenance=provenance,
            diagnostics=diagnostics,
            bundle=bundle,
            diagnostics_bytes=diagnostics_bytes,
        )
        aggregate = _aggregate_from_stage(stage)
        if auditor.records:
            assertion = build_no_mutation_assertion(
                auditor,
                attached_receipt_hashes=dict(source_hashes),
                built_at=clock,
            )
        else:
            assertion = empty_fixture_assertion(
                attached_receipt_hashes=dict(source_hashes),
                built_at=clock,
            )
        audit_log = auditor.as_log()
        pending_results = _pending_check_results()
        if config.scope.mode == "fixture":
            fixture_status = "pending"
            live_proof_status = "not_started"
            fixture_commit: str | None = head
            live_commit = None
        else:
            fixture_status = "not_started"
            live_proof_status = "pending"
            fixture_commit = None
            live_commit = head
        proof_classes = {
            "fixture_proof": _proof_record(
                status=fixture_status,
                commit_hash=fixture_commit,
                check_results=pending_results,
            ),
            "live_proof": _proof_record(
                status=live_proof_status,
                commit_hash=live_commit,
                check_results=pending_results,
            ),
            "human_verdict": _proof_record(status="not_started", commit_hash=None),
        }
        public_payloads: dict[str, bytes] = {
            receipt_paths[name]: receipts[name].to_canonical_json() for name in config.scope.systems
        }
        public_payloads["diagnostics.json"] = diagnostics_bytes
        public_payloads[SOURCE_STAGE_PATH] = stage.to_canonical_json()
        public_payloads["aggregate-proof.json"] = aggregate.to_canonical_json()
        public_payloads["provenance/no-mutation-assertion.json"] = assertion.to_canonical_json()
        public_payloads["provenance/request-audit.json"] = audit_log.to_canonical_json()
        evidence_refs = {
            path: sha256_bytes(payload) for path, payload in sorted(public_payloads.items())
        }
        native = config.scope.mode == "live" and all(
            kind == "native" for kind in provenance.values()
        )
        if config.scope.mode == "fixture":
            rationale = (
                "Fixture replay of the synthetic two-system harness. "
                "This packet is not a live-business usefulness verdict."
            )
            limitations = [
                "Fixture replay is not a live-business read.",
                "Human usefulness verdict remains not_started.",
            ]
        elif native:
            rationale = (
                "Native GET-only two-system read completed diagnostics. "
                "A human usefulness verdict has not been issued."
            )
            limitations = [
                "Native read is not a completed real-business usefulness verdict.",
                "Human usefulness verdict remains not_started.",
            ]
        else:
            rationale = (
                "Simulated GET-only transport completed diagnostics. "
                "This is not a native live read and not a live-business proof."
            )
            limitations = [
                "Simulated transport proof is not a native live business read.",
                "Human usefulness verdict remains not_started.",
            ]
        packet = OperatorReviewPacketV1(
            run_id=run_id,
            usefulness_verdict="not_started",
            rationale=rationale,
            limitations=limitations,
            unresolved_data_gaps=["live_two_system_read", "human_usefulness_verdict"],
            evidence_refs=evidence_refs,
            human_issued=False,
            proof_class="fixture" if config.scope.mode == "fixture" else "live",
            issuer_kind="fixture",
        )
        public_payloads["operator-packet.json"] = packet.to_canonical_json()
        artifacts = [
            PrivateRunArtifactV1(path=path, sha256=sha256_bytes(payload))
            for path, payload in sorted(public_payloads.items())
        ]
        manifest = PrivateRunManifestV1(
            run_id=run_id,
            built_at=clock,
            mode=config.scope.mode,
            systems=list(config.scope.systems),
            fixture=config.scope.fixture,
            commit_hash=head,
            required_checks=list(REQUIRED_CHECKS),
            proof_classes=proof_classes,
            source_receipt_paths=[receipt_paths[name] for name in config.scope.systems],
            source_hashes=source_hashes,
            source_set_hash=source_set_hash,
            source_transport_provenance=dict(provenance),
            config_sha256=config_sha256,
            fetched_snapshot_sha256=fetched_snapshot_sha256,
            source_snapshot_sha256=source_snapshot_sha256,
            artifacts=artifacts,
        )
        public_payloads["private-run.json"] = manifest.to_canonical_json()
        private_payloads = {
            "private/config.json": config_bytes,
            "private/sources/hubspot.fetched.json": fetched_bytes["hubspot"],
            "private/sources/hubspot.snapshot.json": windowed_bytes["hubspot"],
            "private/sources/stripe.fetched.json": fetched_bytes["stripe"],
            "private/sources/stripe.snapshot.json": windowed_bytes["stripe"],
        }
        payloads = {**public_payloads, **private_payloads}
        head_now = _require_clean_head()
        if head_now != head:
            raise PrivateRunConfigError("private-run HEAD drifted before write")
        write_artifact_set_atomic(root, payloads)
        head_after_write = _require_clean_head()
        if head_after_write != head:
            raise PrivateRunConfigError("private-run requires a clean implementation commit")
        for relative_path in payloads:
            _validate_relative_under_root(root, relative_path)
        violations = scan_output_tree(root)
        if violations:
            raise PrivateRunConfigError("private-run output failed public-safety scanning")
        validate_private_run_tree(root)
        head_after_validate = _require_clean_head()
        if head_after_validate != head:
            raise PrivateRunConfigError("private-run requires a clean implementation commit")
        return PrivateRunResult(
            private_root=root,
            run_id=run_id,
            manifest=manifest,
            aggregate=aggregate,
            packet=packet,
        )
    finally:
        for item in owned:
            closer = getattr(item, "close", None)
            if callable(closer):
                closer()
