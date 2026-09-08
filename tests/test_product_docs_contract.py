"""Public operator documentation stays useful, self-contained, and safe to share."""

from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def test_public_operator_entrypoints_exist_and_describe_the_kit():
    for name in ("README.md", "AGENTS.md", "CLAUDE.md", "docs/OPERATING.md"):
        text = (ROOT / name).read_text()
        assert len(text) > 200, name
        assert "Recovery Room" in text, name
    readme = (ROOT / "README.md").read_text()
    assert "scripts/build_launch_demo.py" not in readme
    assert "https://github.com/TheMattBerman/found-money-kit" in readme


def test_public_docs_have_no_internal_plans_or_local_developer_paths():
    assert not (ROOT / "docs/product").exists()
    assert not (ROOT / "docs/launch").exists()
    paths = [ROOT / name for name in ("README.md", "AGENTS.md", "CLAUDE.md", "CHANGELOG.md")]
    paths += list((ROOT / "docs").rglob("*.md"))
    paths += list((ROOT / "skills").rglob("*.md"))
    banned = re.compile(
        r"/Users/|/home/|V2_RECUT|agent-ready|build-spec|issue-local|NotebookLM|harvest-raw|proposed-sort|publication-review packet",
        re.I,
    )
    for path in paths:
        assert not banned.search(path.read_text()), path.relative_to(ROOT)


def test_public_docs_relative_markdown_links_resolve():
    paths = [ROOT / name for name in ("README.md", "AGENTS.md", "CLAUDE.md", "CHANGELOG.md")]
    paths += list((ROOT / "docs").rglob("*.md"))
    for path in paths:
        for target in re.findall(r"\[[^\]]*\]\(([^)]+)\)", path.read_text()):
            if "://" in target or target.startswith("#"):
                continue
            target = target.split("#", 1)[0]
            assert (path.parent / target).exists(), (str(path.relative_to(ROOT)), target)


def test_operator_instructions_keep_actions_read_only():
    for name in ("AGENTS.md", "CLAUDE.md", "skills/found-money/SKILL.md"):
        text = (ROOT / name).read_text().lower()
        assert "read-only" in text, name
        assert "credential" in text, name
        assert "recovered revenue" in text, name
