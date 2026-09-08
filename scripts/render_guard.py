"""Require an explicit render-test contract before renderer files land."""

from __future__ import annotations

import sys
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RENDER_TEST_CONTRACT = "found-money-render-v1"
RENDER_DIRS = ("found_money/rendering", "web", "assets")
RENDER_SUFFIXES = {".html", ".css", ".js", ".ts", ".tsx"}
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


def render_scoped_files(root: Path) -> list[Path]:
    scoped: list[Path] = []
    for directory in RENDER_DIRS:
        path = root / directory
        if path.exists():
            scoped.extend(candidate for candidate in path.rglob("*") if candidate.is_file())
    scoped.extend(
        candidate
        for candidate in root.rglob("*")
        if candidate.is_file() and candidate.suffix in RENDER_SUFFIXES
    )
    return [path for path in scoped if not EXCLUDED_PARTS.intersection(path.parts)]


def has_render_test_contract(root: Path) -> bool:
    candidates = list((root / "tests").glob("test_render*.py"))
    visual = root / "tests" / "visual"
    if visual.exists():
        candidates.extend(visual.glob("test_*.py"))
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        try:
            tree = ast.parse(text, filename=str(path))
        except (OSError, SyntaxError, UnicodeError):
            continue
        if RENDER_TEST_CONTRACT not in text or "pytestmark" in text:
            continue
        for function in (
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test_")
        ):
            if any(
                "skip" in ast.unparse(item).casefold() or "xfail" in ast.unparse(item).casefold()
                for item in function.decorator_list
            ):
                continue
            if any(
                isinstance(node, ast.Assert)
                and not (isinstance(node.test, ast.Constant) and bool(node.test.value))
                for node in ast.walk(function)
            ):
                return True
    return False


def main(root: Path = ROOT) -> int:
    scoped = render_scoped_files(root)
    if scoped and not has_render_test_contract(root):
        print("render-scoped files require an explicit render-test contract:", file=sys.stderr)
        print("\n".join(str(path.relative_to(root)) for path in scoped), file=sys.stderr)
        return 1
    print("render-proof: PASS (no renderer yet, or explicit render tests are present)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
