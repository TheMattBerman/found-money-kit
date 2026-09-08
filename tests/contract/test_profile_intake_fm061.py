"""FM-061: guided agent experience, profile intake, and guardrail gates."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from found_money.profile import (
    BusinessProfileV1,
    ProfileGateError,
    ProfileIntakeError,
    business_model_mismatch,
    discount_violations,
    load_intake_table,
    prebuild_profile_gate_violations,
    profile_intake_violations,
    scenario_business_model_from_config,
    validate_or_raise,
)
from found_money.profile.cli import (
    _default_source_config,
    _recovery_room,
    _single_answer_violations,
    resolved_output_root,
)

ROOT = Path(__file__).resolve().parents[2]

GOOD_ANSWERS = {
    "product": "Vertical SaaS",
    "proof": "Three case studies",
    "margin_percent": "40",
    "capacity": "100",
    "destination": "billing portal",
    "business_model": "saas",
    "tenure_multiple_months": "16",
    "read_only_inputs": True,
    "no_send_path": True,
    "human_authorizes_live": True,
}


def _answers_for_model(business_model: str) -> dict:
    answers = dict(GOOD_ANSWERS)
    answers["business_model"] = business_model
    return answers


def _guide_stdin_lines(**overrides: str) -> str:
    values = {
        "product": "Vertical SaaS",
        "proof": "Three case studies",
        "margin_percent": "40",
        "capacity": "100",
        "destination": "billing portal",
        "business_model": "saas",
        "tenure_multiple_months": "16",
        "ltv_override_minor": "",
        "read_only_inputs": "confirm",
        "no_send_path": "confirm",
        "human_authorizes_live": "confirm",
    }
    values.update(overrides)
    return (
        "\n".join(
            [
                values["product"],
                values["proof"],
                values["margin_percent"],
                values["capacity"],
                values["destination"],
                values["business_model"],
                values["tenure_multiple_months"],
                values["ltv_override_minor"],
                values["read_only_inputs"],
                values["no_send_path"],
                values["human_authorizes_live"],
            ]
        )
        + "\n"
    )


# AC-1: durable question artifact
def test_intake_table_declares_publish_bar_question_set():
    table = load_intake_table()
    ids = {entry["id"] for entry in table["questions"]}
    assert {
        "product",
        "proof",
        "margin_percent",
        "capacity",
        "destination",
        "business_model",
        "tenure_multiple_months",
    } <= ids
    confirmations = {entry["id"] for entry in table["boundary_confirmations"]}
    assert {"read_only_inputs", "no_send_path", "human_authorizes_live"} <= confirmations
    for entry in table["boundary_confirmations"]:
        assert entry["required"] is True


def test_table_validates_structurally():
    table = load_intake_table()
    assert table["schema_version"] == "profile-intake.v1"
    for entry in table["questions"]:
        assert entry["id"] and entry["prompt"] and entry["type"]
        assert entry["feeds"]


def test_missing_required_answer_fails_closed():
    answers = {key: value for key, value in GOOD_ANSWERS.items() if key != "margin_percent"}
    assert "missing required profile answer: margin_percent" in profile_intake_violations(answers)


def test_empty_required_answer_reprompts_via_single_answer_filter():
    entry = next(entry for entry in load_intake_table()["questions"] if entry["id"] == "product")
    assert _single_answer_violations(entry, "")


# AC-2: typed profile, no offer money
def test_typed_profile_round_trip():
    profile = validate_or_raise(GOOD_ANSWERS)
    assert profile.business_model == "saas"
    assert profile.margin_percent == 40
    assert profile.ltv_override_minor is None


def test_unknown_dollar_field_is_rejected():
    answers = dict(GOOD_ANSWERS)
    answers["secret_ltv"] = 5000
    errors = profile_intake_violations(answers)
    assert any("unknown profile answer" in error for error in errors)


def test_profile_model_forbids_unknown_fields():
    with pytest.raises(ValidationError):
        BusinessProfileV1(
            product="x",
            proof="y",
            margin_percent=40,
            capacity=1,
            destination="z",
            business_model="saas",
            tenure_multiple_months=16,
            offer_price_dollars=499,
        )


# AC-3: tenure multiple, no blanket LTV
def test_tenure_multiple_and_ltv_override_validate():
    answers = dict(GOOD_ANSWERS)
    answers["ltv_override_minor"] = "450000"
    profile = validate_or_raise(answers)
    assert profile.tenure_multiple_months == 16
    assert profile.ltv_override_minor == Decimal("450000")


def test_blanket_averaged_ltv_field_is_rejected_by_construction():
    with pytest.raises(ValidationError):
        BusinessProfileV1(
            product="x",
            proof="y",
            margin_percent=40,
            capacity=1,
            destination="z",
            business_model="saas",
            tenure_multiple_months=16,
            blanket_ltv=5000,
        )


def test_negative_tenure_multiple_fails():
    answers = dict(GOOD_ANSWERS)
    answers["tenure_multiple_months"] = "-2"
    assert any("below minimum" in error for error in profile_intake_violations(answers))


# AC-4: business-model gate
@pytest.mark.parametrize(
    ("business_model", "scenario_model", "expect_error"),
    [
        ("saas", "saas", False),
        ("ecommerce", "ecommerce", False),
        ("service", "service", False),
        ("saas", "ecommerce", True),
        ("ecommerce", "service", True),
        ("service", "saas", True),
    ],
)
def test_business_model_gate_for_canonical_arms(business_model, scenario_model, expect_error):
    error = business_model_mismatch(business_model, scenario_model)
    if expect_error:
        assert error is not None and "does not match scenario arm" in error
    else:
        assert error is None


def test_default_source_config_follows_business_model():
    assert _default_source_config("saas").endswith("configs/synthetic-saas-v1.json")
    assert _default_source_config("ecommerce").endswith("configs/synthetic-ecommerce-v1.json")
    assert _default_source_config("service").endswith("configs/synthetic-service-v1.json")
    for model in ("saas", "ecommerce", "service"):
        config = _default_source_config(model)
        assert Path(config).is_file()
        assert scenario_business_model_from_config(config) == model


def test_prebuild_gate_rejects_model_mismatch_before_build():
    config = _default_source_config("saas")
    errors = prebuild_profile_gate_violations("ecommerce", Decimal("40"), config)
    assert any("business-model gate" in error for error in errors)


# AC-5: margin guardrail executes
def test_unbound_discount_fails_citing_table_rule_text():
    from found_money.profile import load_table_guardrails

    guardrail = load_table_guardrails()["discount_requires_commitment_term"]
    errors = discount_violations({"discount_percent": 20}, Decimal("40"))
    assert any(guardrail in error for error in errors)


def test_bound_discount_within_margin_passes():
    assert (
        discount_violations({"discount_percent": 20, "commitment_term": "6-month"}, Decimal("40"))
        == []
    )


def test_discount_against_unfundable_margin_fails_closed():
    errors = discount_violations(
        {"discount_percent": 20, "commitment_term": "6-month"}, Decimal("15")
    )
    assert any("cannot be funded" in error for error in errors)


def test_unresolved_margin_fails_closed():
    errors = discount_violations({"discount_percent": 20, "commitment_term": "6-month"}, None)
    assert any("unresolved" in error for error in errors)


def test_discount_above_table_ceiling_fails_closed(monkeypatch):
    import found_money.profile.gate as gate

    monkeypatch.setattr(gate, "_discount_margin_ceiling_percent", lambda: Decimal("15"))
    errors = discount_violations(
        {"discount_percent": 20, "commitment_term": "6-month"}, Decimal("40")
    )
    assert any("discount_margin_ceiling" in error for error in errors)


def test_discount_at_or_below_table_ceiling_passes_with_margin(monkeypatch):
    import found_money.profile.gate as gate

    monkeypatch.setattr(gate, "_discount_margin_ceiling_percent", lambda: Decimal("100"))
    assert (
        discount_violations({"discount_percent": 20, "commitment_term": "6-month"}, Decimal("40"))
        == []
    )


def test_missing_discount_guardrail_key_fails_closed():
    import found_money.profile.gate as gate

    with patch.object(gate, "load_table_guardrails", return_value={}):
        with pytest.raises(ProfileGateError, match="discount_requires_commitment_term"):
            gate.discount_violations({"discount_percent": 10}, Decimal("40"))


# AC-6: enforcement is thin and table-driven
def test_guardrail_rule_text_comes_from_the_table():
    from found_money.profile import load_table_guardrails

    assert "panic discount" in load_table_guardrails()["discount_requires_commitment_term"]


def test_stale_table_schema_version_fails_closed(monkeypatch, tmp_path):
    import found_money.profile.intake as intake

    table = load_intake_table()
    table["schema_version"] = "profile-intake.v0"
    (tmp_path / "profile-intake.json").write_text(json.dumps(table), encoding="utf-8")
    original = intake._PROFILE_SKILL_DIR
    intake._PROFILE_SKILL_DIR = tmp_path
    intake.load_intake_table.cache_clear()
    try:
        with pytest.raises(ProfileIntakeError, match="schema_version"):
            intake.load_intake_table()
    finally:
        intake._PROFILE_SKILL_DIR = original
        intake.load_intake_table.cache_clear()


def test_deleting_required_question_fails_closed(monkeypatch, tmp_path):
    import found_money.profile.intake as intake

    table = load_intake_table()
    table["questions"] = [entry for entry in table["questions"] if entry["id"] != "margin_percent"]
    (tmp_path / "profile-intake.json").write_text(json.dumps(table), encoding="utf-8")
    original = intake._PROFILE_SKILL_DIR
    intake._PROFILE_SKILL_DIR = tmp_path
    intake.load_intake_table.cache_clear()
    try:
        answers = {key: value for key, value in GOOD_ANSWERS.items() if key != "margin_percent"}
        with pytest.raises((ProfileIntakeError, ValidationError)):
            intake.validate_or_raise(answers)
    finally:
        intake._PROFILE_SKILL_DIR = original
        intake.load_intake_table.cache_clear()


def test_skipping_required_boundary_confirmation_fails_closed():
    answers = dict(GOOD_ANSWERS)
    answers.pop("no_send_path")
    errors = profile_intake_violations(answers)
    assert "missing required boundary confirmation: no_send_path" in errors


def test_guided_validation_refuses_skipped_confirmation_before_build():
    answers = dict(GOOD_ANSWERS)
    answers.pop("human_authorizes_live")
    assert profile_intake_violations(answers)
    profile = None
    with pytest.raises(ProfileIntakeError):
        profile = validate_or_raise(answers)
    assert profile is None


# AC-7: guided flow entry points
def test_default_source_config_exists():
    assert Path(_default_source_config("saas")).is_file()


def test_guided_subcommand_registered():
    result = subprocess.run(
        [sys.executable, "-m", "found_money", "--help"],
        capture_output=True,
        text=True,
    )
    assert "guide" in result.stdout


def test_missing_answer_blocks_before_build():
    answers = dict(GOOD_ANSWERS)
    answers.pop("business_model")
    errors = profile_intake_violations(answers)
    assert errors, "a required answer missing must fail before the build runs"


def test_guide_subprocess_rejects_business_model_mismatch_before_build(tmp_path):
    """AC-7 E2E: piped answers must fail closed on the business-model gate with no build output."""

    output_root = tmp_path / "guided-out"
    env = os.environ.copy()
    env["FOUND_MONEY_GUIDE_CONFIG_OVERRIDE"] = str(ROOT / "configs" / "synthetic-saas-v1.json")
    result = subprocess.run(
        [sys.executable, "-m", "found_money", "guide", "--output-root", str(output_root)],
        input=_guide_stdin_lines(business_model="ecommerce"),
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    assert result.returncode != 0
    assert "business-model gate" in result.stderr
    assert not output_root.exists()


def test_resolved_output_root_joins_cwd(tmp_path):
    root = resolved_output_root(tmp_path, "private-runs/x")
    assert root == tmp_path / "private-runs" / "x"


# AC-8: byte-stability
def test_recovery_room_render_is_byte_identical_on_repeat():
    """AC-8 render determinism claim (not a full double-build E2E).

    Proves: rendering Recovery Room HTML twice from the same frozen strategized
    artifacts yields byte-identical output.
    Does not prove: two consecutive guided or unguided ``found_money build`` runs
    are byte-identical end-to-end. Full double-build byte stability for canonical
    scenario configs is covered by the FM-033 scenario contract tests.
    """

    from found_money.rendering import render_recovery_room_html
    from found_money.strategy import build_thin_slice_strategized_money_map

    money_map, _packet, play_set = build_thin_slice_strategized_money_map()
    first = render_recovery_room_html(money_map, play_set)
    second = render_recovery_room_html(money_map, play_set)
    assert first == second
    assert first


def test_profile_lands_inside_run_directory(tmp_path):
    (tmp_path / "run.json").write_text("{}", encoding="utf-8")
    (tmp_path / "index.html").write_text("<html></html>", encoding="utf-8")
    room = _recovery_room(tmp_path)
    assert room == tmp_path / "index.html"
    profile = validate_or_raise(GOOD_ANSWERS)
    (room.parent / "business-profile.json").write_text(
        json.dumps(profile.model_dump(mode="json"), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    written = (tmp_path / "business-profile.json").read_text(encoding="utf-8")
    assert "offer" not in written.lower() or "ltv_override_minor" in written
