"""Guided owner walkthrough (FM-061): setup, questions, build, Recovery Room.

The flow is orchestration only. It reads the durable question table, collects
answers, validates them, runs the one-command build, writes
``business-profile.json`` into the run directory, prints the next-step statement,
and opens the Recovery Room HTML. It never sends, schedules, writes CRM/payment
data, creates an audience, sets the live-enable flag, or selects credentials.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from found_money.profile.gate import prebuild_profile_gate_violations
from found_money.profile.intake import (
    load_intake_table,
    profile_intake_violations,
    validate_or_raise,
)

_CONFIG_BY_BUSINESS_MODEL = {
    "saas": "synthetic-saas-v1.json",
    "ecommerce": "synthetic-ecommerce-v1.json",
    "service": "synthetic-service-v1.json",
}


def _single_answer_violations(entry: dict[str, Any], raw: str) -> list[str]:
    qid = entry["id"]
    return [
        error
        for error in profile_intake_violations({qid: raw})
        if error.endswith(f": {qid}") or f": {qid} " in error
    ]


def _ask(entry: dict[str, Any]) -> Any:
    options = entry.get("options")
    suffix = f" ({' / '.join(options)})" if options else ""
    required_note = "" if entry["required"] else " [optional, blank to skip]"
    while True:
        raw = input(f"{entry['prompt']}{required_note}{suffix}\n> ").strip()
        if raw == "" and not entry["required"]:
            return None
        if not _single_answer_violations(entry, raw):
            return raw
        print("  that answer is not valid; try again.")


def _source_config_for_profile(business_model: str) -> str:
    override = os.environ.get("FOUND_MONEY_GUIDE_CONFIG_OVERRIDE")
    if override:
        return override
    return _default_source_config(business_model)


def guided_session(output_root: str) -> int:
    table = load_intake_table()
    print("Found Money setup. Nothing sends; you hit send yourself.")
    print("Inputs stay read-only. A live read needs your explicit authorization.\n")
    answers: dict[str, Any] = {}
    for entry in table["questions"]:
        value = _ask(entry)
        if value is not None:
            answers[entry["id"]] = value
    for entry in table["boundary_confirmations"]:
        while True:
            raw = input(f"{entry['prompt']} [confirm/skip]\n> ").strip().lower()
            if raw in ("confirm", "skip"):
                break
            print("  answer 'confirm' or 'skip'.")
        if raw == "confirm":
            answers[entry["id"]] = True
    errors = profile_intake_violations(answers)
    if errors:
        for error in errors:
            print("error: " + error, file=sys.stderr)
        return 2
    profile = validate_or_raise(answers)

    config_path = _source_config_for_profile(profile.business_model)
    gate_errors = prebuild_profile_gate_violations(
        profile.business_model,
        profile.margin_percent,
        config_path,
    )
    if gate_errors:
        for error in gate_errors:
            print("error: " + error, file=sys.stderr)
        return 2

    from found_money.build import build, BuildConfigError, BuildPathError

    try:
        build(output_root=output_root, source_config=config_path, business_profile=profile)
    except (BuildConfigError, BuildPathError, ValueError) as exc:
        print("error: " + str(exc), file=sys.stderr)
        return 2

    resolved_root = resolved_output_root(Path.cwd(), output_root)
    room = _recovery_room(resolved_root)
    if room is not None:
        (room.parent / "business-profile.json").write_text(_profile_json(profile), encoding="utf-8")
    print("\nWhat happens next:")
    print("1. Review the Recovery Room in your browser. Nothing was sent.")
    print("2. You hit send yourself, from your own tools, if you choose to.")
    print("3. A live HubSpot or Stripe read happens only if you authorize it.")
    if room is not None:
        print(f"Opening: {room}")
        _open_html(room)
    return 0


def _default_source_config(business_model: str) -> str:
    repo_root = Path(__file__).resolve().parents[2]
    filename = _CONFIG_BY_BUSINESS_MODEL.get(business_model)
    if filename is None:
        raise ValueError(f"unknown business_model for guided config: {business_model!r}")
    return str(repo_root / "configs" / filename)


def resolved_output_root(base: Path, output_root: str) -> Path:
    root = Path(output_root)
    if not root.is_absolute():
        root = base / root
    return root


def _run_dir(output_root: Path):
    if (output_root / "run.json").exists():
        return output_root
    return None


def _recovery_room(output_root: Path):
    run_dir = _run_dir(output_root)
    if run_dir is None:
        return None
    candidates = sorted(run_dir.glob("index.html"))
    return candidates[0] if candidates else None


def _open_html(path) -> None:
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    try:
        subprocess.run([opener, str(path)], check=False)
    except OSError:
        pass


def _serialize_profile(profile) -> str:
    import json

    payload = profile.model_dump(mode="json")
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _profile_json(profile) -> str:
    return _serialize_profile(profile)
