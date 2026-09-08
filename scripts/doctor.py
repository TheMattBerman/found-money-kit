"""Repository doctor for the bootstrap; no network or mutation capability."""

from __future__ import annotations

import sys
import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
REQUIRED_BOOTSTRAP_TESTS = {
    "test_ensure_main_ref_handles_detached_and_checked_out_main",
    "test_buildloop_checks_exactly_match_ci_job_names_and_pr_conditions",
    "test_bootstrap_guard_subjects_exist_and_pass_without_renderer",
}


def _is_pytestmark_target(node: ast.expr) -> bool:
    if isinstance(node, ast.Name):
        return node.id == "pytestmark"
    return (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "globals"
        and isinstance(node.slice, ast.Constant)
        and node.slice.value == "pytestmark"
    )


def _has_meaningful_assert(function: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any(
        isinstance(node, ast.Assert)
        and not (isinstance(node.test, ast.Constant) and bool(node.test.value))
        for node in ast.walk(function)
    )


def main(root: Path = ROOT) -> int:
    expected = (
        root / "found_money",
        root / "tests" / "test_found_money.py",
        root / "tests" / "test_bootstrap_contract.py",
        root / ".buildloop.toml",
        root / "scripts" / "public_safety.py",
        root / "scripts" / "render_guard.py",
    )
    missing = [str(path.relative_to(root)) for path in expected if not path.exists()]
    if sys.version_info < (3, 11):
        missing.append("Python 3.11+")
    if missing:
        print("doctor: FAIL " + ", ".join(missing), file=sys.stderr)
        return 1
    try:
        tree = ast.parse((root / "tests" / "test_bootstrap_contract.py").read_text())
    except (OSError, SyntaxError) as exc:
        print(f"doctor: FAIL bootstrap contract unreadable: {exc}", file=sys.stderr)
        return 1
    pytest_names = {"pytest"}
    skip_names = {"skip"}
    for node in tree.body:
        if isinstance(node, ast.Import):
            pytest_names.update(
                item.asname or item.name for item in node.names if item.name == "pytest"
            )
        elif isinstance(node, ast.ImportFrom) and node.module == "pytest":
            skip_names.update(
                item.asname or item.name for item in node.names if item.name == "skip"
            )
        targets: list[ast.expr] = []
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if any(_is_pytestmark_target(target) for target in targets):
            print("doctor: FAIL bootstrap contract may not set pytestmark", file=sys.stderr)
            return 1
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and (
            (isinstance(node.func, ast.Name) and node.func.id in skip_names)
            or (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in pytest_names
                and node.func.attr == "skip"
            )
        ):
            print("doctor: FAIL bootstrap contract may not call pytest.skip", file=sys.stderr)
            return 1
        if any(
            "skip" in ast.unparse(item).casefold() or "xfail" in ast.unparse(item).casefold()
            for item in getattr(node, "decorator_list", ())
        ):
            print("doctor: FAIL bootstrap contract may not skip or xfail", file=sys.stderr)
            return 1
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    if not REQUIRED_BOOTSTRAP_TESTS.issubset(functions):
        print("doctor: FAIL bootstrap contract required test names missing", file=sys.stderr)
        return 1
    for name in REQUIRED_BOOTSTRAP_TESTS:
        function = functions[name]
        if function.decorator_list or not _has_meaningful_assert(function):
            print(
                "doctor: FAIL required bootstrap tests must be undecorated and assert",
                file=sys.stderr,
            )
            return 1
    print("doctor: PASS (Python 3.11+, prototype regressions, loop configuration present)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
