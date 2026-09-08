"""Skip evidence-binding tests unless a release is actually being prepared.

See `found_money/release_evidence.py`. These tests fail whenever the generated
plays, the public tree, or the rendered pixels change, and clearing them costs
independent model reviews and a human ratification rather than a code fix. They
are the release gate, not the development gate.
"""

from __future__ import annotations

import pytest

from found_money.release_evidence import RELEASE_EVIDENCE_ENV, release_evidence_enabled


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "release_evidence: binds a committed evidence artifact to the current tree; "
        f"skipped unless {RELEASE_EVIDENCE_ENV}=1",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if release_evidence_enabled():
        return
    skip = pytest.mark.skip(
        reason=f"release evidence: set {RELEASE_EVIDENCE_ENV}=1 to bind committed artifacts"
    )
    for item in items:
        if "release_evidence" in item.keywords:
            item.add_marker(skip)
