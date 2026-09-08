"""Package-wide AST, import-graph, and CLI capability proof."""

from __future__ import annotations

import ast
import importlib
import importlib.util
import pkgutil
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[2]
PACKAGE_NAME = "found_money"

FORBIDDEN_IMPORT_ROOTS = frozenset(
    {"smtplib", "sendgrid", "twilio", "selenium", "requests", "aiohttp", "socket"}
)
SENSITIVE_IMPORT_ALLOWLIST = {
    "importlib": frozenset({"found_money/safety/capability.py"}),
    "httpx": frozenset({"found_money/connectors/hubspot.py", "found_money/connectors/stripe.py"}),
    "playwright": frozenset({"found_money/rendering/proof.py"}),
    "subprocess": frozenset(
        {
            "found_money/__main__.py",
            "found_money/profile/cli.py",
            "found_money/private_run.py",
            "found_money/release.py",
            "found_money/safety/capability.py",
            "found_money/safety/evidence.py",
            "scripts/ensure_main_ref.py",
        }
    ),
    "urllib.request": frozenset({"found_money/hubspot.py"}),
}
FORBIDDEN_ATTR_CALLS = frozenset(
    {
        "send_message",
        "sendmail",
        "createsegment",
        "create_audience",
        "createaudience",
        "schedule_message",
        "schedulesend",
    }
)
FORBIDDEN_CLI_TOKENS = frozenset(
    {
        "send",
        "schedule",
        "audience",
        "create-audience",
        "write-crm",
        "write-payment",
        "mutate",
        "broadcast",
    }
)
ALLOWED_CLI_COMMANDS = frozenset(
    {
        "doctor",
        "demo",
        "build",
        "source-stage",
        "run",
        "hubspot-analyze",
        "private-run",
        "public-proof",
        "guide",
    }
)


def _load_public_safety() -> Any:
    path = ROOT / "scripts" / "public_safety.py"
    spec = importlib.util.spec_from_file_location("found_money_public_safety", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load scripts/public_safety.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _call_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id.casefold()
    if isinstance(node, ast.Attribute):
        return node.attr.casefold()
    return ""


def iter_package_python(root: Path = ROOT) -> list[Path]:
    package = root / "found_money"
    return sorted(
        path
        for path in package.rglob("*.py")
        if "__pycache__" not in path.parts and ".venv" not in path.parts
    )


def scan_forbidden_imports(paths: Iterable[Path] | None = None) -> list[str]:
    violations: list[str] = []
    for path in paths or iter_package_python():
        try:
            relative_label = path.relative_to(ROOT).as_posix()
        except ValueError:
            relative_label = path.as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError, UnicodeError) as exc:
            violations.append(f"cannot inspect {path}: {exc}")
            continue
        import_module_aliases = {
            item.asname or item.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == "importlib"
            for item in node.names
            if item.name == "import_module"
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for item in node.names:
                    root_name = item.name.split(".", 1)[0]
                    if root_name in FORBIDDEN_IMPORT_ROOTS:
                        violations.append(f"forbidden import {item.name} in {path}:{node.lineno}")
                    allowed = SENSITIVE_IMPORT_ALLOWLIST.get(item.name)
                    if allowed is not None:
                        if relative_label not in allowed:
                            violations.append(
                                f"sensitive import {item.name} outside allowlist in {path}:{node.lineno}"
                            )
            elif isinstance(node, ast.ImportFrom) and node.module:
                root_name = node.module.split(".", 1)[0]
                if root_name in FORBIDDEN_IMPORT_ROOTS:
                    violations.append(
                        f"forbidden import from {node.module} in {path}:{node.lineno}"
                    )
                allowed = SENSITIVE_IMPORT_ALLOWLIST.get(node.module)
                if allowed is None:
                    allowed = SENSITIVE_IMPORT_ALLOWLIST.get(root_name)
                if allowed is not None:
                    try:
                        rel = path.relative_to(ROOT).as_posix()
                    except ValueError:
                        rel = path.as_posix()
                    if rel not in allowed:
                        violations.append(
                            f"sensitive import from {node.module} outside allowlist in "
                            f"{path}:{node.lineno}"
                        )
            elif isinstance(node, ast.Call):
                name = _call_name(node.func)
                dynamic_module: str | None = None
                is_dynamic_import = False
                if (
                    isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "importlib"
                    and node.func.attr == "import_module"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                ):
                    is_dynamic_import = True
                    dynamic_module = node.args[0].value
                elif (
                    isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "importlib"
                    and node.func.attr == "import_module"
                ):
                    is_dynamic_import = True
                elif (
                    isinstance(node.func, ast.Name)
                    and node.func.id == "__import__"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                ):
                    is_dynamic_import = True
                    dynamic_module = node.args[0].value
                elif isinstance(node.func, ast.Name) and node.func.id == "__import__":
                    is_dynamic_import = True
                elif isinstance(node.func, ast.Name) and node.func.id in import_module_aliases:
                    is_dynamic_import = True
                    if (
                        node.args
                        and isinstance(node.args[0], ast.Constant)
                        and isinstance(node.args[0].value, str)
                    ):
                        dynamic_module = node.args[0].value
                if is_dynamic_import and dynamic_module is None:
                    try:
                        rel = path.relative_to(ROOT).as_posix()
                    except ValueError:
                        rel = path.as_posix()
                    if rel != "found_money/safety/capability.py":
                        violations.append(f"non-literal dynamic import in {path}:{node.lineno}")
                if dynamic_module is not None:
                    root_name = dynamic_module.split(".", 1)[0]
                    allowed = SENSITIVE_IMPORT_ALLOWLIST.get(dynamic_module)
                    if allowed is None:
                        allowed = SENSITIVE_IMPORT_ALLOWLIST.get(root_name)
                    try:
                        rel = path.relative_to(ROOT).as_posix()
                    except ValueError:
                        rel = path.as_posix()
                    if root_name in FORBIDDEN_IMPORT_ROOTS:
                        violations.append(
                            f"forbidden dynamic import {dynamic_module} in {path}:{node.lineno}"
                        )
                    elif allowed is not None and rel not in allowed:
                        violations.append(
                            f"sensitive dynamic import {dynamic_module} outside allowlist in "
                            f"{path}:{node.lineno}"
                        )
                if name in FORBIDDEN_ATTR_CALLS:
                    violations.append(f"forbidden capability call {name} in {path}:{node.lineno}")
                if (
                    isinstance(node.func, ast.Name)
                    and node.func.id == "getattr"
                    and len(node.args) >= 2
                    and isinstance(node.args[0], ast.Name)
                    and node.args[0].id == "importlib"
                ):
                    violations.append(f"dynamic import alias via getattr in {path}:{node.lineno}")
                if name in {"exec_module", "spec_from_file_location", "module_from_spec"}:
                    try:
                        rel = path.relative_to(ROOT).as_posix()
                    except ValueError:
                        rel = path.as_posix()
                    if rel != "found_money/safety/capability.py":
                        violations.append(f"dynamic module loader {name} in {path}:{node.lineno}")
    return violations


def scan_mutating_http(root: Path = ROOT, paths: Iterable[Path] | None = None) -> list[str]:
    public_safety = _load_public_safety()
    targets = list(paths) if paths is not None else public_safety.source_paths(root)
    return public_safety.scan_python_paths(targets)


def scan_disallowed_network_literals(paths: Iterable[Path] | None = None) -> list[str]:
    """Flag non-allowlisted hosts in package modules that construct outbound HTTP."""
    from found_money.safety.allowlist import HUBSPOT_HOST, MODEL_HOST, STRIPE_HOST

    allowed_hosts = {HUBSPOT_HOST, MODEL_HOST, STRIPE_HOST, "example.invalid"}
    host_re = re.compile(r"https?://([A-Za-z0-9.-]+)")
    http_markers = ("httpx.", "urlopen(", "requests.", "urllib.request")
    violations: list[str] = []
    for path in paths or iter_package_python():
        if path.name == "transports.py" and path.parent.name == "safety":
            continue
        text = path.read_text(encoding="utf-8")
        if not any(marker in text for marker in http_markers):
            continue
        for match in host_re.finditer(text):
            host = match.group(1).casefold()
            if host in allowed_hosts or host.endswith(".invalid"):
                continue
            if host in {"localhost", "127.0.0.1", "example.com"}:
                continue
            violations.append(f"disallowed network host literal {host} in {path}")
    return violations


def import_graph_modules() -> list[str]:
    """Import every found_money module; proves import-time has no CLI send side effects."""
    import found_money

    names = [PACKAGE_NAME]
    for module in pkgutil.walk_packages(found_money.__path__, prefix=f"{PACKAGE_NAME}."):
        names.append(module.name)
    for name in names:
        importlib.import_module(name)
    return sorted(names)


def cli_help_text() -> str:
    result = subprocess.run(
        [sys.executable, "-m", "found_money", "--help"],
        check=True,
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    chunks = [result.stdout]
    for command in sorted(ALLOWED_CLI_COMMANDS):
        probed = subprocess.run(
            [sys.executable, "-m", "found_money", command, "--help"],
            check=False,
            capture_output=True,
            text=True,
            cwd=str(ROOT),
        )
        if probed.returncode != 0:
            raise RuntimeError(f"CLI help failed for {command}: {probed.stderr}")
        chunks.append(probed.stdout)
    return "\n".join(chunks)


def scan_cli_help(text: str | None = None) -> list[str]:
    help_text = text if text is not None else cli_help_text()
    violations: list[str] = []
    lowered = help_text.casefold()
    for token in FORBIDDEN_CLI_TOKENS:
        pattern = re.compile(rf"(?m)^\s+{re.escape(token)}(?:\s|:|$)")
        if pattern.search(lowered):
            violations.append(f"forbidden CLI capability token {token!r}")
    command_block = re.search(r"\{([^}]+)\}", help_text)
    if command_block:
        listed = {item.strip() for item in command_block.group(1).split(",")}
        unexpected = listed - ALLOWED_CLI_COMMANDS
        missing = ALLOWED_CLI_COMMANDS - listed
        for item in sorted(unexpected):
            violations.append(f"unexpected CLI command {item!r}")
        for item in sorted(missing):
            violations.append(f"missing expected CLI command {item!r}")
    return violations


def scan_package_capabilities(root: Path = ROOT) -> list[str]:
    """Full package capability proof used by public_safety and FM-030 tests."""
    package_paths = iter_package_python(root)
    cli_paths = sorted((root / "scripts").glob("*.py"))
    violations: list[str] = []
    violations.extend(scan_forbidden_imports([*package_paths, *cli_paths]))
    violations.extend(scan_mutating_http(root, [*package_paths, *cli_paths]))
    violations.extend(scan_disallowed_network_literals(package_paths))
    violations.extend(scan_cli_help())
    return violations
