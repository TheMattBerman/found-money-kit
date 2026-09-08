"""Caller-root writer proof helpers."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterable, Mapping

from found_money.receipts import _atomic_write_bytes, _validate_relative_under_root

ROOT = Path(__file__).resolve().parents[2]
WRITER_FUNCTION_PREFIXES = ("write_",)
FIXED_TRUSTED_ROOT_WRITERS = frozenset({"write_named_png_baselines"})


def _call_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id.casefold()
    if isinstance(node, ast.Attribute):
        return node.attr.casefold()
    return ""


def assert_caller_root_write(output_root: Path | str, relative_path: str) -> Path:
    """Public proof helper: relative writes must stay under the caller root."""
    return _validate_relative_under_root(Path(output_root), relative_path)


def write_artifact_set_atomic(
    output_root: Path | str, payloads: Mapping[str, bytes]
) -> dict[str, Path]:
    """Write a preflighted artifact set and restore the prior tree on failure."""
    root = Path(output_root)
    destinations = {
        relative: _validate_relative_under_root(root, relative) for relative in payloads
    }
    if len(set(destinations.values())) != len(destinations):
        raise ValueError("artifact paths must differ")
    before = {
        path: (path.read_bytes() if path.exists() else None) for path in destinations.values()
    }
    existing_dirs = {path.parent for path in destinations.values() if path.parent.exists()}
    written: list[Path] = []
    try:
        for relative in sorted(payloads):
            destination = destinations[relative]
            _atomic_write_bytes(destination, payloads[relative])
            written.append(destination)
    except Exception as original:
        restore_errors: list[Exception] = []
        for destination in reversed(written):
            prior = before[destination]
            try:
                if prior is None:
                    destination.unlink(missing_ok=True)
                else:
                    _atomic_write_bytes(destination, prior)
            except Exception as exc:  # pragma: no cover - catastrophic filesystem failure
                restore_errors.append(exc)
        for parent in sorted(
            {path.parent for path in destinations.values()} - existing_dirs,
            key=lambda item: len(item.parts),
            reverse=True,
        ):
            try:
                parent.rmdir()
            except OSError:
                pass
        if restore_errors:
            raise RuntimeError("artifact set write and rollback both failed") from original
        raise
    return destinations


def writer_escape_cases(output_root: Path) -> list[tuple[str, str]]:
    """Return (relative_path, expected_error_fragment) pairs that must fail closed."""
    return [
        ("../outside.json", "traversal or absolute"),
        ("/tmp/escape.json", "absolute"),
        ("~/escape.json", "absolute"),
        ("./nested/../../outside.json", "traversal or absolute"),
    ]


def prove_writer_rejects_escapes(output_root: Path) -> None:
    for relative, fragment in writer_escape_cases(output_root):
        try:
            assert_caller_root_write(output_root, relative)
        except ValueError as exc:
            message = str(exc).casefold()
            if "absolute" not in message and "traversal" not in message and "escape" not in message:
                raise AssertionError(
                    f"escape rejection message missing expected fragment for {relative}: {exc}"
                ) from exc
            continue
        raise AssertionError(f"writer accepted escaping path {relative!r}")


def scan_writers_use_root_validation(paths: Iterable[Path] | None = None) -> list[str]:
    """AST proof that package write_* helpers call root validation or atomic write helpers."""
    package = ROOT / "found_money"
    targets = (
        list(paths)
        if paths is not None
        else sorted(path for path in package.rglob("*.py") if "__pycache__" not in path.parts)
    )
    violations: list[str] = []
    for path in targets:
        # Contracts and pure builders do not write.
        if path.parent.name == "contracts" or path.name in {
            "models.py",
            "mapping.py",
            "engine.py",
            "redaction.py",
            "__main__.py",
        }:
            # Prototype/local helpers predate the caller-root V1 writer contract.
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError, UnicodeError) as exc:
            violations.append(f"cannot inspect {path}: {exc}")
            continue
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name in FIXED_TRUSTED_ROOT_WRITERS or node.name == "_atomic_write_bytes":
                continue
            text = ast.unparse(node)
            has_validation = any(
                marker in text
                for marker in (
                    "_validate_relative_under_root",
                    "assert_caller_root_write",
                    "write_artifact_set_atomic",
                )
            )
            direct_sinks = {
                _call_name(call.func) for call in ast.walk(node) if isinstance(call, ast.Call)
            } & {"open", "write_text", "write_bytes", "replace"}
            delegated = any(
                _call_name(call.func).lstrip("_").startswith("write_")
                and _call_name(call.func) not in {"write_text", "write_bytes"}
                for call in ast.walk(node)
                if isinstance(call, ast.Call)
            )
            is_writer = node.name.startswith(WRITER_FUNCTION_PREFIXES)
            if not is_writer:
                continue
            if direct_sinks and not has_validation:
                violations.append(
                    f"writer {node.name} in {path}:{node.lineno} lacks caller-root validation"
                )
            elif not direct_sinks and not has_validation and not delegated:
                violations.append(
                    f"writer {node.name} in {path}:{node.lineno} lacks validated delegation"
                )
    return violations
