"""Bootstrap-only contracts for CI and guards before FM-001 exists."""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


public_safety = load_script("public_safety")
render_guard = load_script("render_guard")
doctor = load_script("doctor")


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def make_remote_clone(tmp_path: Path, name: str) -> Path:
    remote = tmp_path / f"{name}.git"
    seed = tmp_path / f"{name}-seed"
    clone = tmp_path / f"{name}-clone"
    git(tmp_path, "init", "--bare", str(remote))
    git(tmp_path, "init", str(seed))
    git(seed, "config", "user.email", "test@example.invalid")
    git(seed, "config", "user.name", "Test")
    (seed / "file").write_text("base\n")
    git(seed, "add", "file")
    git(seed, "commit", "-m", "base")
    git(seed, "branch", "-M", "main")
    git(seed, "remote", "add", "origin", str(remote))
    git(seed, "push", "-u", "origin", "main")
    git(tmp_path, "clone", "--branch", "main", str(remote), str(clone))
    return clone


def test_ensure_main_ref_handles_detached_and_checked_out_main(tmp_path):
    script = ROOT / "scripts" / "ensure_main_ref.py"
    detached = make_remote_clone(tmp_path, "detached")
    git(detached, "checkout", "--detach")
    git(detached, "branch", "-D", "main")
    subprocess.run(["python3", str(script)], cwd=detached, check=True)
    git(detached, "show-ref", "--verify", "refs/heads/main")
    assert (detached / ".git").exists()
    checked = make_remote_clone(tmp_path, "checked")
    subprocess.run(["python3", str(script)], cwd=checked, check=True)
    git(checked, "show-ref", "--verify", "refs/heads/main")
    assert (checked / ".git").exists()


def test_buildloop_checks_exactly_match_ci_job_names_and_pr_conditions():
    checks = tomllib.loads((ROOT / ".buildloop.toml").read_text())["checks"]["required"]
    assert checks == [
        "quality",
        "unit-and-contract",
        "public-safety",
        "render-proof",
    ]
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    for name in checks:
        assert f"  {name}:" in workflow
        job = re.search(
            rf"^  {re.escape(name)}:\n    name: (?P<display>.+)$", workflow, re.MULTILINE
        )
        assert job and job.group("display") == name
    for name in checks[:-1]:
        job = re.search(
            rf"^  {re.escape(name)}:\n(?P<body>(?:^    .*\n?)*)", workflow, re.MULTILINE
        )
        assert job
        assert "if: github.event_name == 'pull_request'" in job.group("body")
    unit = workflow.split("  unit-and-contract:", 1)[1].split("\n  public-safety:", 1)[0]
    assert "runs-on: ubuntu-24.04" in unit
    assert "uv run python scripts/doctor.py" in unit
    assert "uv run playwright install --with-deps chromium" in unit
    render = workflow.split("  render-proof:", 1)[1]
    assert "uv run playwright install --with-deps chromium" in render
    assert "uv run python scripts/render_guard.py" in render
    assert "fresh-clone" not in workflow
    assert "verify-build-packet" not in workflow


def test_bootstrap_guard_subjects_exist_and_pass_without_renderer():
    assert (ROOT / "scripts" / "doctor.py").is_file()
    assert (ROOT / "scripts" / "public_safety.py").is_file()
    assert (ROOT / "scripts" / "render_guard.py").is_file()
    assert doctor.main(ROOT) == 0
    assert public_safety.main(ROOT) == 0
    assert render_guard.main(ROOT) == 0


def test_public_safety_rejects_nested_and_top_level_mutating_calls(tmp_path):
    nested = tmp_path / "nested.py"
    nested.write_text("def f(client):\n    return client.post('/x')\n")
    top_level = tmp_path / "top.py"
    top_level.write_text("httpx.request('POST', 'https://example.invalid')\n")
    safe = tmp_path / "safe.py"
    safe.write_text("client.request('GET', '/x')\nhttpx.get('/x')\n")
    violations = public_safety.scan_python_paths([nested, top_level, safe])
    assert len(violations) == 2
    assert all("forbidden mutating HTTP call" in item for item in violations)


def test_public_safety_detects_keyword_post_and_allows_cache_delete(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text("client.request(method='POST', url='/x')\n")
    safe = tmp_path / "safe.py"
    safe.write_text("cache.delete('x')\n")
    assert len(public_safety.scan_python_paths([bad, safe])) == 1


def test_public_safety_limits_direct_verbs_to_http_receivers(tmp_path):
    probes = tmp_path / "probes.py"
    probes.write_text(
        "import httpx as hx\nfrom requests import Session as RequestsSession\n"
        "api = RequestsSession()\ncache.delete('x')\ndelete('/x')\nself.records.delete('x')\n"
        "hx.post('/x')\napi.delete('/x')\ntransport.patch('/x')\nhx.Client().put('/x')\n"
    )
    violations = public_safety.scan_python_paths([probes])
    assert len(violations) == 4


def test_public_safety_rejects_urlopen_payload(tmp_path):
    probe = tmp_path / "probe.py"
    probe.write_text(
        "from urllib.request import urlopen\nurlopen('https://example.invalid', data=b'x')\n"
    )
    assert public_safety.scan_python_paths([probe])


def test_public_safety_recognizes_prototype_urllib_request_method_shape(tmp_path):
    source = (ROOT / "found_money" / "hubspot.py").read_text()
    get_copy = tmp_path / "hubspot_get.py"
    get_copy.write_text(source)
    assert not public_safety.scan_python_paths([get_copy])
    for method in ("POST", "DELETE"):
        changed = tmp_path / f"hubspot_{method}.py"
        changed.write_text(source.replace('method="GET"', f'method="{method}"'))
        assert public_safety.scan_python_paths([changed])


def test_public_safety_rejects_urllib_request_payload_shapes(tmp_path):
    safe = tmp_path / "safe.py"
    safe.write_text(
        "from urllib.request import Request\nRequest('https://example.invalid', method='GET')\n"
    )
    assert not public_safety.scan_python_paths([safe])
    for index, body in enumerate(
        (
            "Request('https://example.invalid', data=b'x')",
            "Request('https://example.invalid', b'x')",
            "urlopen(Request('https://example.invalid', json={'x': 1}))",
        )
    ):
        probe = tmp_path / f"payload_{index}.py"
        probe.write_text("from urllib.request import Request, urlopen\n" + body + "\n")
        assert public_safety.scan_python_paths([probe])
    variants = (
        "import urllib.request\nurllib.request.Request('https://example.invalid', data=b'x')",
        "import urllib.request as ur\nur.Request('https://example.invalid', b'x')",
        "from urllib.request import Request as Req, urlopen\nurlopen(Req('https://example.invalid', json={'x': 1}))",
        "from urllib import request\nrequest.Request('https://example.invalid', data=b'x')",
        "from urllib import request as rq\nrq.urlopen(rq.Request('https://example.invalid', data=b'x'))",
    )
    for index, source in enumerate(variants):
        probe = tmp_path / f"constructor_{index}.py"
        probe.write_text(source + "\n")
        assert public_safety.scan_python_paths([probe])


def test_public_safety_fails_closed_for_invalid_python(tmp_path):
    broken = tmp_path / "broken.py"
    broken.write_text("def nope(:\n")
    assert public_safety.scan_python_paths([broken])


def test_render_guard_rejects_placeholder_visual_test(tmp_path):
    (tmp_path / "found_money" / "rendering").mkdir(parents=True)
    (tmp_path / "found_money" / "rendering" / "page.py").write_text("pass\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_render_placeholder.py").write_text(
        "def test_render():\n    assert True\n"
    )
    assert render_guard.main(tmp_path) == 1


def test_render_guard_accepts_explicit_non_placeholder_contract(tmp_path):
    (tmp_path / "found_money" / "rendering").mkdir(parents=True)
    (tmp_path / "found_money" / "rendering" / "page.py").write_text("pass\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_render_contract.py").write_text(
        "RENDER_TEST_CONTRACT = 'found-money-render-v1'\n"
        "def test_render_contract():\n    assert 'Money Map' in 'Money Map'\n"
    )
    assert render_guard.main(tmp_path) == 0


def test_render_guard_rejects_constant_one_and_skip(tmp_path):
    (tmp_path / "found_money" / "rendering").mkdir(parents=True)
    (tmp_path / "found_money" / "rendering" / "page.py").write_text("pass\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_render_contract.py").write_text(
        "RENDER_TEST_CONTRACT = 'found-money-render-v1'\n"
        "@pytest.mark.skip\ndef test_render_contract():\n    assert 1\n"
    )
    assert render_guard.main(tmp_path) == 1


def test_render_guard_ignores_complete_dependency_exclusions(tmp_path):
    for directory in (".factory/ignored", "node_modules/ignored", "build/ignored"):
        path = tmp_path / directory
        path.mkdir(parents=True)
        (path / "page.js").write_text("export default 1\n")
    assert render_guard.main(tmp_path) == 0


def _doctor_subprocess_root(tmp_path: Path, bootstrap: str) -> Path:
    for relative in (
        "scripts/doctor.py",
        "scripts/public_safety.py",
        "scripts/render_guard.py",
        ".buildloop.toml",
    ):
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / relative, destination)
    (tmp_path / "found_money").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_found_money.py").write_text("def test_smoke():\n    assert True\n")
    (tmp_path / "tests" / "test_bootstrap_contract.py").write_text(bootstrap)
    return tmp_path


def test_doctor_rejects_skipped_or_renamed_bootstrap_contracts(tmp_path):
    required = "\n".join(
        f"def {name}():\n    assert True" for name in doctor.REQUIRED_BOOTSTRAP_TESTS
    )
    cases = (
        "pytestmark = pytest.mark.skip\n" + required,
        "pytest.skip('nope', allow_module_level=True)\n" + required,
        required.replace(
            "test_bootstrap_guard_subjects_exist_and_pass_without_renderer", "renamed"
        ),
    )
    for index, bootstrap in enumerate(cases):
        root = _doctor_subprocess_root(tmp_path / str(index), bootstrap)
        result = subprocess.run(
            ["python3", "scripts/doctor.py"], cwd=root, text=True, capture_output=True
        )
        assert result.returncode == 1, result.stdout + result.stderr


def test_doctor_rejects_contract_neutralization_variants(tmp_path):
    required = "\n".join(
        f"def {name}():\n    assert 1 == 1" for name in doctor.REQUIRED_BOOTSTRAP_TESTS
    )
    cases = (
        "pytestmark: object = pytest.mark.skip\n" + required,
        "globals()['pytestmark'] = pytest.mark.skip\n" + required,
        "import pytest as pt\npt.skip('nope')\n" + required,
        "from pytest import skip as no\nno('nope')\n" + required,
        required + "\ndef test_extra():\n    skip('nope')",
        required.replace("assert 1 == 1", "pass", 1),
    )
    for index, bootstrap in enumerate(cases):
        root = _doctor_subprocess_root(tmp_path / str(index), bootstrap)
        result = subprocess.run(
            ["python3", "scripts/doctor.py"], cwd=root, text=True, capture_output=True
        )
        assert result.returncode == 1, result.stdout + result.stderr


def test_version_contract_agrees_across_public_metadata():
    from found_money import VERSION, __version__

    project_version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    changelog = (ROOT / "CHANGELOG.md").read_text()
    assert __version__ == VERSION == project_version == "0.1.0"
    assert f"## {VERSION} " in changelog


def test_unit_and_contract_reports_breakage_on_main_pushes():
    """main can report its own breakage; the suite cannot run pull-request-only.

    #108: a merge that landed red on main went unnoticed because every test
    job was gated to pull_request. The push trigger for main exists so the
    binding tests execute on the one branch whose state is otherwise invisible.
    The gate is narrow: only pushes to main run the suite, keeping cost bounded
    while restoring the signal.
    """
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "on:\n  push:\n    branches: [main]" in workflow
    unit = workflow.split("  unit-and-contract:", 1)[1].split("\n  public-safety:", 1)[0]
    assert (
        "if: github.event_name == 'pull_request' || "
        "(github.event_name == 'push' && github.ref == 'refs/heads/main')" in unit
    )
