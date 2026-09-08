#!/usr/bin/env python3
"""Build and optionally open the deterministic synthetic vertical-SaaS demo. No keys or live reads."""

from __future__ import annotations

import argparse
import json
import os
import sys
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from found_money.build import build  # noqa: E402
from generate_demo_dataset import (  # noqa: E402
    NOW,
    build_dataset,
    hubspot_snapshot,
    stripe_snapshot,
    write_json,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--open", action="store_true", help="open the finished offline Recovery Room"
    )
    args = parser.parse_args()
    os.chdir(ROOT)
    source_root = ROOT / "private-runs" / "demo-verticalsaas"
    dataset = build_dataset()
    write_json(source_root / "hubspot/snapshot.json", hubspot_snapshot(dataset))
    write_json(source_root / "stripe/snapshot.json", stripe_snapshot(dataset))
    config = {
        "schema_version": "found-money-build-source.v1",
        "mode": "file",
        "data_origin": "synthetic",
        "run_mode": "public",
        "clock": NOW.isoformat(),
        "credentials": {"credential_mode": "none", "runtime_mode": "test", "scopes": []},
        "sources": {
            "hubspot_snapshot": "hubspot/snapshot.json",
            "stripe_snapshot": "stripe/snapshot.json",
        },
        "business_model": "saas",
        "strategy_provider": "skill",
        "business_profile": {
            "product": "scheduling software for plumbers",
            "proof": "product walkthrough",
            "margin_percent": "75",
            "capacity": 20,
            "destination": "account billing portal",
            "business_model": "saas",
            "tenure_multiple_months": "16",
        },
    }
    config_path = source_root / "build-config.json"
    write_json(config_path, config)
    result = build(output_root="private-runs/launch-demo", source_config=config_path)
    receipt = json.loads((result.output_root / "provenance/valuation-summary.json").read_text())
    print(
        "Example business with illustrative data. Profile assumptions: 75% margin, 20 review slots, 16-month owner tenure multiple."
    )
    print(json.dumps(receipt, indent=2))
    print(f"Recovery Room: {result.output_root / 'index.html'}")
    if args.open:
        webbrowser.open((result.output_root / "index.html").as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
