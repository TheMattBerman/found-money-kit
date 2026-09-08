"""Fail-closed public-artifact and mutation-capability guard."""

from __future__ import annotations

import ast
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Iterable

from found_money.redaction import assert_public_safe
from found_money.safety.capability import scan_cli_help, scan_forbidden_imports
from found_money.safety.output_scan import scan_output_tree
from found_money.safety.writers import scan_writers_use_root_validation

ROOT = Path(__file__).resolve().parent.parent
MUTATING_METHODS = {"post", "put", "patch", "delete"}
EXCLUDED_PARTS = {
    ".git",
    ".venv",
    ".factory",
    "node_modules",
    "__pycache__",
    "build",
    "dist",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}

# Generated contract artifacts must stay free of these shapes.
# Phone detection requires separators or a leading '+'; bare digit runs inside
# SHA-256 hex digests are not phone numbers.
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
    r"(?i)(\"(?:password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|"
    r"authorization|credential|credentials|bearer|client[_-]?secret)\"\s*:)"
)
_IDENTITY_KEY_RE = re.compile(
    r'(?i)"(email|phone|name|company|source_id|record_id|contact_id|crm_url|deal_name|'
    r'external_ids|account_token)"\s*:'
)


def _call_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id.casefold()
    if isinstance(node, ast.Attribute):
        return node.attr.casefold()
    return ""


def _dotted_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted_name(node.value)
        return f"{parent}.{node.attr}" if parent else ""
    return ""


def _http_symbols(tree: ast.AST) -> tuple[set[str], set[str], set[str]]:
    """Return imported HTTP module names and names assigned HTTP client instances."""
    modules = {"httpx", "requests"}
    classes = {"client", "session", "transport"}
    instances = {"client", "session", "transport"}
    request_constructors = {"Request"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                if item.name.split(".", 1)[0] in {"httpx", "requests"}:
                    modules.add(item.asname or item.name.split(".", 1)[0])
                if item.name == "urllib.request":
                    request_constructors.add(item.asname or "urllib.request")
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.split(".", 1)[0] in {"httpx", "requests"}:
                for item in node.names:
                    if item.name.casefold() in {"client", "session", "asyncclient"}:
                        classes.add((item.asname or item.name).casefold())
            if node.module == "urllib.request":
                request_constructors.update(
                    item.asname or item.name for item in node.names if item.name == "Request"
                )
            if node.module == "urllib":
                request_constructors.update(
                    item.asname or item.name for item in node.names if item.name == "request"
                )
        elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            func = node.value.func
            class_name = _call_name(func)
            is_known_class = class_name in classes or (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id.casefold() in modules
                and class_name in {"client", "session", "asyncclient"}
            )
            if is_known_class:
                instances.update(
                    target.id.casefold() for target in node.targets if isinstance(target, ast.Name)
                )
    return modules, instances, request_constructors


def _is_request_constructor(node: ast.expr, constructors: set[str]) -> bool:
    if isinstance(node, ast.Name):
        return node.id in constructors
    if isinstance(node, ast.Attribute) and node.attr == "Request":
        return _dotted_name(node.value) in constructors
    return False


def _http_receiver(node: ast.expr, modules: set[str], instances: set[str]) -> bool:
    if isinstance(node, ast.Name):
        return node.id.casefold() in modules | instances
    if isinstance(node, ast.Call):
        return _call_name(node.func) in {"client", "session", "asyncclient"} or (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id.casefold() in modules
        )
    return False


def _mutating_call(
    node: ast.Call, modules: set[str], instances: set[str], constructors: set[str]
) -> str | None:
    name = _call_name(node.func)
    is_request_constructor = _is_request_constructor(node.func, constructors)
    receiver = node.func.value if isinstance(node.func, ast.Attribute) else None
    if name in MUTATING_METHODS and receiver and _http_receiver(receiver, modules, instances):
        return name.upper()
    if name == "urlopen":
        payload = next((item.value for item in node.keywords if item.arg == "data"), None)
        if payload is None and len(node.args) > 1:
            payload = node.args[1]
        if (
            not (isinstance(payload, ast.Constant) and payload.value is None)
            and payload is not None
        ):
            return "POST"
    if name == "request" or is_request_constructor:
        method = next((item.value for item in node.keywords if item.arg == "method"), None)
        if (
            method is None
            and receiver
            and _http_receiver(receiver, modules, instances)
            and node.args
        ):
            method = node.args[0]
        if isinstance(method, ast.Constant) and isinstance(method.value, str):
            if method.value.casefold() in MUTATING_METHODS:
                return method.value.upper()
            return None
        if is_request_constructor:
            payload = next(
                (
                    item.value
                    for item in node.keywords
                    if item.arg in {"data", "body", "json", "content"}
                ),
                None,
            )
            if payload is None and len(node.args) > 1:
                payload = node.args[1]
            if payload is not None and not (
                isinstance(payload, ast.Constant) and payload.value is None
            ):
                return "POST"
    return None


def scan_python_paths(paths: Iterable[Path]) -> list[str]:
    """Return violations; unreadable or invalid Python is itself a violation."""
    violations: list[str] = []
    for path in paths:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError, UnicodeError) as exc:
            violations.append(f"cannot safely inspect {path}: {exc}")
            continue
        modules, instances, constructors = _http_symbols(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and (
                method := _mutating_call(node, modules, instances, constructors)
            ):
                violations.append(f"forbidden mutating HTTP call {method} in {path}:{node.lineno}")
    return violations


def source_paths(root: Path) -> list[Path]:
    return [path for path in root.rglob("*.py") if not EXCLUDED_PARTS.intersection(path.parts)]


def scan_contract_artifact_text(label: str, text: str) -> list[str]:
    """Return public-safety violations found in a generated contract artifact.

    Uses contract-specific detectors rather than ``assert_public_safe``, because
    that helper treats long digit runs inside SHA-256 digests as phone numbers.
    """
    violations: list[str] = []
    try:
        json.loads(text)
    except json.JSONDecodeError as exc:
        return [f"{label}: malformed JSON ({exc})"]
    checks = (
        ("email", _EMAIL_RE),
        ("phone", _PHONE_RE),
        ("URL/query string", _URL_RE),
        ("unsafe URI scheme", _UNSAFE_SCHEME_RE),
        ("absolute local path", _ABS_PATH_RE),
        ("credential-shaped value", _CREDENTIAL_RE),
        ("provider-shaped customer/source id", _PROVIDER_CUSTOMER_ID_RE),
        ("raw customer identity key", _IDENTITY_KEY_RE),
    )
    for name, pattern in checks:
        if pattern.search(text):
            violations.append(f"{label}: contains {name}")
    return violations


def scan_html_artifact_text(label: str, text: str) -> list[str]:
    """Return public-safety violations found in generated Recovery Room HTML."""
    violations: list[str] = []
    checks = (
        ("email", _EMAIL_RE),
        ("phone", _PHONE_RE),
        ("URL/query string", _URL_RE),
        ("unsafe URI scheme", _UNSAFE_SCHEME_RE),
        ("absolute local path", _ABS_PATH_RE),
        ("credential-shaped value", _CREDENTIAL_RE),
        ("provider-shaped customer/source id", _PROVIDER_CUSTOMER_ID_RE),
    )
    for name, pattern in checks:
        if pattern.search(text):
            violations.append(f"{label}: contains {name}")
    for token in (
        "customer_token",
        "economic_unit_key",
        "external_ids",
        "inv_failed_001",
        "cus_synth_001",
        "source_id",
    ):
        if token in text:
            violations.append(f"{label}: contains raw customer identity key ({token})")
    return violations


def scan_generated_contract_artifacts(root: Path) -> list[str]:
    """Build synthetic thin-slice receipts/manifest/public identity/value and scan them."""
    from found_money.events import (
        build_thin_slice_failed_payments,
        write_recovery_candidates,
    )
    from found_money.identity import (
        build_thin_slice_identity,
        load_thin_slice_snapshots,
        write_public_identity_projection,
    )
    from found_money.map import (
        build_thin_slice_money_map,
        public_money_map_projection,
        write_money_map,
        write_public_money_map_projection,
    )
    from found_money.receipts import build_thin_slice_artifacts, write_thin_slice_artifacts
    from found_money.rendering import (
        build_thin_slice_recovery_room,
        write_recovery_room,
    )
    from found_money.strategy import (
        build_thin_slice_strategized_money_map,
        public_recovery_play_projection,
        write_public_recovery_play_projection,
        write_recovery_plays,
        write_strategy_evidence_packet,
    )
    from found_money.value import (
        build_contribution_ledger,
        public_contribution_projection,
        write_contribution_ledger,
        write_public_contribution_projection,
    )

    violations: list[str] = []
    _receipts, _manifest, encoded = build_thin_slice_artifacts()
    for relative, payload in encoded.items():
        text = payload.decode("utf-8")
        violations.extend(scan_contract_artifact_text(f"generated:{relative}", text))

    _graph, identity_projection = build_thin_slice_identity()
    public_bytes = identity_projection.to_canonical_json()
    violations.extend(
        scan_contract_artifact_text(
            "generated:identity/identity-public.json",
            public_bytes.decode("utf-8"),
        )
    )

    _graph2, candidates = build_thin_slice_failed_payments()
    snapshots = load_thin_slice_snapshots()
    ledger = build_contribution_ledger(candidates, snapshots["stripe"])
    contribution_projection = public_contribution_projection(ledger)
    violations.extend(
        scan_contract_artifact_text(
            "generated:value/contribution-public.json",
            contribution_projection.to_canonical_json().decode("utf-8"),
        )
    )

    money_map = build_thin_slice_money_map()
    map_projection = public_money_map_projection(money_map)
    violations.extend(
        scan_contract_artifact_text(
            "generated:map/money-map-public.json",
            map_projection.to_canonical_json().decode("utf-8"),
        )
    )

    _enriched, evidence_packet, play_set = build_thin_slice_strategized_money_map()
    play_projection = public_recovery_play_projection(play_set)
    violations.extend(
        scan_contract_artifact_text(
            "generated:strategy/recovery-plays-public.json",
            play_projection.to_canonical_json().decode("utf-8"),
        )
    )
    violations.extend(
        scan_contract_artifact_text(
            "generated:strategy/strategy-evidence-packet.json",
            evidence_packet.to_canonical_json().decode("utf-8"),
        )
    )

    _room_map, _room_plays, index_html, top_play_html = build_thin_slice_recovery_room()
    violations.extend(scan_html_artifact_text("generated:recovery-room/index.html", index_html))
    violations.extend(
        scan_html_artifact_text("generated:recovery-room/top-play.html", top_play_html)
    )

    with tempfile.TemporaryDirectory(prefix="fm-public-safety-") as tmp:
        out = Path(tmp)
        write_thin_slice_artifacts(out)
        write_public_identity_projection(out, "identity/identity-public.json", identity_projection)
        write_recovery_candidates(out, "events/recovery-candidates.json", candidates)
        write_contribution_ledger(out, "value/contribution-ledger.json", ledger)
        write_public_contribution_projection(
            out, "value/contribution-public.json", contribution_projection
        )
        write_money_map(out, "map/money-map.json", money_map)
        write_public_money_map_projection(out, "map/money-map-public.json", map_projection)
        write_strategy_evidence_packet(
            out, "strategy/strategy-evidence-packet.json", evidence_packet
        )
        write_recovery_plays(out, "strategy/recovery-plays.json", play_set)
        write_public_recovery_play_projection(
            out, "strategy/recovery-plays-public.json", play_projection
        )
        write_recovery_room(out, index_html, top_play_html)
        for path in sorted(out.rglob("*")):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            rel = path.relative_to(out).as_posix()
            # Private identity graphs and candidate/ledger lineage retain source
            # IDs; only scan public contract artifacts for identity leakage.
            if rel.endswith(
                (
                    "identity-graph.json",
                    "recovery-candidates.json",
                    "contribution-ledger.json",
                    "money-map.json",
                    "recovery-plays.json",
                )
            ):
                continue
            if rel.endswith(".html"):
                violations.extend(scan_html_artifact_text(f"written:{rel}", text))
            elif rel.endswith(".json"):
                violations.extend(scan_contract_artifact_text(f"written:{rel}", text))
        violations.extend(scan_output_tree(out))

    from found_money.public_proof import build_public_proof

    previous = Path.cwd()
    with tempfile.TemporaryDirectory(prefix="fm-public-proof-") as tmp:
        proof_cwd = Path(tmp)
        os.chdir(proof_cwd)
        try:
            result = build_public_proof(
                output_root="artifacts/public-proof",
                config_path=root / "configs" / "synthetic-public-proof.json",
            )
            violations.extend(scan_output_tree(result.output_root))
            demo = (result.output_root / "demo.html").read_text(encoding="utf-8")
            violations.extend(scan_html_artifact_text("generated:public-proof/demo.html", demo))
            for relative in (
                "aggregate-receipt.json",
                "recompute-report.json",
                "public-proof.json",
            ):
                violations.extend(
                    scan_contract_artifact_text(
                        f"generated:public-proof/{relative}",
                        (result.output_root / relative).read_text(encoding="utf-8"),
                    )
                )
        finally:
            os.chdir(previous)
    return violations


def main(root: Path = ROOT) -> int:
    sample = root / "output" / "sample-public.json"
    if not sample.is_file():
        print(f"missing committed public sample: {sample}", file=sys.stderr)
        return 1
    try:
        assert_public_safe(json.loads(sample.read_text(encoding="utf-8")))
    except (ValueError, json.JSONDecodeError, OSError, UnicodeError) as exc:
        print(f"public sample failed safety scan: {exc}", file=sys.stderr)
        return 1
    violations = scan_python_paths(source_paths(root))
    violations.extend(scan_forbidden_imports())
    try:
        violations.extend(scan_cli_help())
    except Exception as exc:
        print(f"CLI help capability scan failed: {exc}", file=sys.stderr)
        return 1
    violations.extend(scan_writers_use_root_validation())
    try:
        violations.extend(scan_generated_contract_artifacts(root))
    except Exception as exc:  # fail closed if contracts cannot be generated
        print(f"generated contract artifacts failed safety scan: {exc}", file=sys.stderr)
        return 1
    if violations:
        print("\n".join(violations), file=sys.stderr)
        return 1
    print(
        "public-safety: PASS (public sample redacted; contract artifacts clean; "
        "capability/CLI/writer scans clean; no mutating HTTP calls)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
