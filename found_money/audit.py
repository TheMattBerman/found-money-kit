"""Append-only local approval evidence; this module never dispatches a message."""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


def write_approval_record(output_root, relative_path, approved_tokens, approved_by):
    event = {
        "event": "approval_recorded_no_send",
        "at": datetime.now(timezone.utc).isoformat(),
        "approved_by": str(approved_by),
        "tokens": sorted(set(approved_tokens)),
    }
    event["integrity"] = hashlib.sha256(json.dumps(event, sort_keys=True).encode()).hexdigest()
    from found_money.receipts import _validate_relative_under_root

    p = _validate_relative_under_root(Path(output_root), relative_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, sort_keys=True) + "\n")
    return event
