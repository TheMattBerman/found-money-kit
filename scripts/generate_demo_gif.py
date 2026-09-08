"""Rebuild assets/demo.gif from the synthetic-service Find."""

from __future__ import annotations

import argparse
import io
import tempfile
from pathlib import Path

from PIL import Image

from found_money.build import _fixture_snapshots, _run_id_for, _safe_source_config
from found_money.rendering import recovery_room_static_assets, render_recovery_room_html
from found_money.rendering.proof import capture_four_room_screenshots
from found_money.scenarios import SYNTHETIC_SERVICE_V1, run_scenario_engine

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_GIF = ROOT / "assets" / "demo.gif"
COMMENT = b"We found $387.00"


def _service_index_html() -> str:
    safe = _safe_source_config(
        source_mode="fixture", run_mode="public", fixture=SYNTHETIC_SERVICE_V1
    )
    run_id = _run_id_for(_fixture_snapshots(SYNTHETIC_SERVICE_V1), safe)
    engine = run_scenario_engine(run_id=run_id, safe_config=safe, fixture_id=SYNTHETIC_SERVICE_V1)
    return render_recovery_room_html(
        engine.enriched_money_map,
        engine.strategy_run.recovery_plays,
        contribution_ledger=engine.ledger,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_GIF)
    args = parser.parse_args()
    html = _service_index_html()
    if "We found $387.00" not in html:
        raise SystemExit("service Find HTML is missing We found $387.00")
    with tempfile.TemporaryDirectory(prefix="found-money-demo-gif-") as temporary:
        root = Path(temporary)
        index = root / "index.html"
        index.write_text(html, encoding="utf-8")
        for relative, payload in recovery_room_static_assets().items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
        artifacts = capture_four_room_screenshots(index.resolve())
        png = artifacts["room-find.png"]
    frame = Image.open(io.BytesIO(png)).convert("P", palette=Image.Palette.ADAPTIVE)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    frame.save(
        args.output,
        format="GIF",
        save_all=True,
        append_images=[frame],
        duration=1200,
        loop=0,
        comment=COMMENT,
    )
    if COMMENT not in args.output.read_bytes():
        raise SystemExit("demo GIF is missing We found $387.00")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
