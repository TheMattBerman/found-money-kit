"""Filter only reviewed, byte-identical fixture lines from hygiene matches.

Unknown or changed lines always remain findings. No file or directory is exempt.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

ALLOWLIST = Path(__file__).with_suffix(".json")


def is_reviewed(path: str, label: str, line: str, entries: list[dict[str, str]]) -> bool:
    digest = hashlib.sha256(line.encode()).hexdigest()
    return any(e["path"] == path and e["label"] == label and e["sha256"] == digest for e in entries)


def filter_matches(
    lines: list[str], label: str, history: bool, entries: list[dict[str, str]]
) -> list[str]:
    findings = []
    for line in lines:
        parts = line.split(":", 3 if history else 2)
        if len(parts) != (4 if history else 3):
            findings.append("unparseable history match (value withheld)" if history else line)
            continue
        if history:
            commit, path, _number, content = parts
        else:
            path, _number, content = parts
        if not is_reviewed(path, label, content, entries):
            findings.append(f"{commit}:{path}" if history else line)
    return findings


def main() -> None:
    try:
        entries = json.loads(ALLOWLIST.read_text())
    except (OSError, ValueError):
        entries = []
    lines = sys.stdin.read().splitlines()
    print("\n".join(filter_matches(lines, sys.argv[1], sys.argv[2] == "history", entries)))


if __name__ == "__main__":
    main()
