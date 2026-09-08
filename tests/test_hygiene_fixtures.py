"""An added credential in a reviewed fixture must still block publication."""

import hashlib
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "hygiene_fixtures", ROOT / "scripts/hygiene_fixtures.py"
)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_exact_line_only_and_history_are_bound_to_path_and_pattern():
    label = "test category"
    line = "fabricated canary"
    entries = [
        {
            "path": "tests/fake.py",
            "label": label,
            "sha256": hashlib.sha256(line.encode()).hexdigest(),
        }
    ]
    good = "tests/fake.py:2:" + line
    changed = good + " with a new value"
    other = "product.py:2:" + line
    assert module.filter_matches([good, changed, other], label, False, entries) == [changed, other]
    assert module.filter_matches([good], "different category", False, entries) == [good]
    assert module.filter_matches(["a:tests/fake.py:2:" + line], label, True, entries) == []
    assert module.filter_matches(["a:tests/fake.py:2:new value"], label, True, entries) == [
        "a:tests/fake.py"
    ]
    assert module.filter_matches([good], label, False, []) == [good]
    assert module.filter_matches(["malformed private canary"], label, True, []) == [
        "unparseable history match (value withheld)"
    ]


def test_shell_scan_does_not_exempt_new_matching_lines(tmp_path):
    import json
    import os
    import shutil
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "scripts").mkdir()
    for name in ("pre-public-scan.sh", "hygiene_fixtures.py"):
        shutil.copyfile(ROOT / "scripts" / name, tmp_path / "scripts" / name)
    line = 'password = "' + "fake_" * 6 + '"'
    (tmp_path / "fixture.py").write_text(line + "\n")
    entries = [
        {
            "path": "fixture.py",
            "label": "Generic secret literal",
            "sha256": hashlib.sha256(line.encode()).hexdigest(),
        }
    ]
    (tmp_path / "scripts/hygiene_fixtures.json").write_text(json.dumps(entries))
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    env = {**os.environ, "SKIP_HISTORY": "1"}
    command = ["bash", "scripts/pre-public-scan.sh"]
    assert subprocess.run(command, cwd=tmp_path, env=env, capture_output=True).returncode == 0
    # A staged snapshot has no history yet; do not reparse current matches as commits.
    full_env = dict(env)
    full_env.pop("SKIP_HISTORY")
    assert subprocess.run(command, cwd=tmp_path, env=full_env, capture_output=True).returncode == 0
    (tmp_path / "fixture.py").write_text(line + "\n" + line.replace("fake", "changed") + "\n")
    assert subprocess.run(command, cwd=tmp_path, env=env, capture_output=True).returncode == 1
    # A failed filter cannot turn the finding into an empty result.
    (tmp_path / "scripts/hygiene_fixtures.py").write_text("raise SystemExit(1)\n")
    assert subprocess.run(command, cwd=tmp_path, env=env, capture_output=True).returncode == 1
