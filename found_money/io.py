import csv
import json
from pathlib import Path
from typing import Any
from .mapping import map_row
from .engine import evaluate, segment_cards
from .redaction import assert_public_safe
from .receipts import _atomic_write_bytes, _validate_relative_under_root


def load_records(path):
    p = Path(path)
    rows: Any
    if p.suffix.lower() == ".csv":
        with p.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    else:
        data = json.loads(p.read_text(encoding="utf-8"))
        rows = (
            data.get("records", data.get("contacts", data.get("data", data)))
            if isinstance(data, dict)
            else data
        )
    if not isinstance(rows, list):
        raise ValueError("input must contain an array of records")
    return [map_row(r) for r in rows]


def public_report(records, approved=None):
    approved = set(approved or [])
    ds = evaluate(records)
    queue = []
    for d in ds:
        basis = (
            "observed source value × 25–75%"
            if d.value_high
            else "unquantified: no observed source value"
        )
        queue.append(
            {
                "token": d.token,
                "classification": d.classification,
                "status": d.status,
                "reasons": d.reasons,
                "opportunity_value": {"basis": basis, "low": d.value_low, "high": d.value_high},
                "priority": d.score,
                "channel": d.channel,
                "draft": d.draft,
                "approved": d.token in approved,
            }
        )
    payload = {
        "format": "found-money-public-v1",
        "no_send_enforced": True,
        "proof_tier": "built",
        "queue": queue,
        "segment_cards": segment_cards(ds),
    }
    assert_public_safe(payload)
    return payload


def write_report(records, output_root, relative_path, approved=None):
    payload = public_report(records, approved)
    destination = _validate_relative_under_root(Path(output_root), relative_path)
    _atomic_write_bytes(
        destination, (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    )
    return payload
