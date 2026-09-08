"""Documentation scanner catches unsafe and misleading publication claims."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from found_money.release import (
    scan_release_docs,
)

ROOT = Path(__file__).resolve().parents[2]
LOCKED = datetime(2026, 8, 11, 23, 0, tzinfo=timezone.utc)


def test_release_doc_scan_flags_stale_next_issue_language(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "docs" / "product").mkdir(parents=True)
    (root / "docs" / "product" / "ROADMAP.md").write_text(
        "Do not begin Wave 2 product work until the Wave 1 visual-baseline gap.\n"
        "Close Wave 1 visual-baseline gap: fixed screenshots.\n",
        encoding="utf-8",
    )
    (root / "docs" / "product" / "V1.md").write_text(
        "As of `origin/main` merge `a980632`, Stripe remains unimplemented.\n",
        encoding="utf-8",
    )
    (root / "README.md").write_text(
        "Native/live connectors remain `not_started`.\n",
        encoding="utf-8",
    )
    (root / "docs" / "OPERATING.md").write_text("operator notes\n", encoding="utf-8")
    (root / "AGENTS.md").write_text(
        "As of `a980632`, FM-001 through FM-007 are merged.\n",
        encoding="utf-8",
    )
    report = scan_release_docs(root, built_at=LOCKED)
    assert report.clean is False
    categories = {item.category for item in report.findings}
    assert "stale_next_issue" in categories
    paths = {item.path for item in report.findings if item.category == "stale_next_issue"}
    assert "docs/product/ROADMAP.md" in paths
    assert "docs/product/V1.md" in paths
    assert "README.md" in paths
    assert "AGENTS.md" in paths


def test_changelog_historical_later_issue_wording_is_not_a_scan_finding(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    (root / "README.md").write_text("Found Money shipped through FM-039.\n", encoding="utf-8")
    (root / "CHANGELOG.md").write_text(
        "- FM-031: live activation and StealAds intake wiring remain later issues.\n",
        encoding="utf-8",
    )
    report = scan_release_docs(root, built_at=LOCKED)
    assert report.clean is True


def test_live_release_doc_scan_stays_clean_after_reconciliation() -> None:
    report = scan_release_docs(ROOT, built_at=LOCKED)
    assert report.clean is True
    assert not any(item.category == "stale_next_issue" for item in report.findings)
