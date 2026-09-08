"""Typed business-profile intake behind the guided skill (FM-061).

The question set lives in ``skills/found-money/profile-intake.json`` as a durable
artifact. This module is thin typed enforcement: it loads the table, validates the
table shape, validates owner answers against it, and fails closed on anything the
table does not declare. It holds no domain knowledge of its own. Validation applies the question table directly.
"""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator

_PROFILE_SKILL_DIR = Path(__file__).resolve().parents[2] / "skills" / "found-money"
_TABLE_NAME = "profile-intake.json"
_KNOWN_TYPES = frozenset({"text", "percent", "integer", "number", "enum", "money_minor"})
BUSINESS_MODELS = frozenset({"saas", "ecommerce", "service"})


class ProfileIntakeError(ValueError):
    """Raised when the intake table or a profile answer fails validation."""


@lru_cache(maxsize=None)
def load_intake_table() -> dict[str, Any]:
    """Load and structurally validate the durable question table."""

    path = _PROFILE_SKILL_DIR / _TABLE_NAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ProfileIntakeError(f"missing profile-intake table: {_TABLE_NAME}") from exc
    except json.JSONDecodeError as exc:
        raise ProfileIntakeError(f"malformed profile-intake table ({exc})") from exc
    if not isinstance(payload, dict):
        raise ProfileIntakeError("profile-intake table is not an object")
    if payload.get("schema_version") != "profile-intake.v1":
        raise ProfileIntakeError("profile-intake table schema_version must be 'profile-intake.v1'")
    questions = payload.get("questions")
    if not isinstance(questions, list) or not questions:
        raise ProfileIntakeError("profile-intake table has no questions list")
    ids: set[str] = set()
    for entry in questions:
        if not isinstance(entry, dict):
            raise ProfileIntakeError("profile-intake question entry is not an object")
        for key in ("id", "prompt", "type", "required", "feeds"):
            if key not in entry:
                raise ProfileIntakeError(f"profile-intake question is missing '{key}'")
        qid = entry["id"]
        if not isinstance(qid, str) or not qid:
            raise ProfileIntakeError("profile-intake question id must be a non-empty string")
        if qid in ids:
            raise ProfileIntakeError(f"profile-intake question id is duplicated: {qid}")
        ids.add(qid)
        if entry["type"] not in _KNOWN_TYPES:
            raise ProfileIntakeError(f"profile-intake question {qid} has unknown type")
        if entry["required"] is True and entry["type"] == "enum" and not entry.get("options"):
            raise ProfileIntakeError(f"required enum question {qid} must declare options")
    confirmations = payload.get("boundary_confirmations")
    if not isinstance(confirmations, list) or not confirmations:
        raise ProfileIntakeError("profile-intake table has no boundary_confirmations list")
    for entry in confirmations:
        if not isinstance(entry, dict) or "id" not in entry or "prompt" not in entry:
            raise ProfileIntakeError("boundary confirmation is malformed")
        if "required" not in entry:
            raise ProfileIntakeError("boundary confirmation is missing 'required'")
    return payload


def required_question_ids() -> tuple[str, ...]:
    """Ids of the required questions, in table order."""

    return tuple(
        entry["id"] for entry in load_intake_table()["questions"] if entry["required"] is True
    )


def _as_number(value: Any) -> Decimal:
    try:
        number = Decimal(str(value))
        if not number.is_finite():
            raise ProfileIntakeError("numeric answer must be finite")
        return number
    except (InvalidOperation, ValueError) as exc:
        raise ProfileIntakeError(f"numeric answer is not a number: {value!r}") from exc


class BusinessProfileV1(BaseModel):
    """Validated owner answers. Money is unquantified except an explicit LTV override."""

    model_config = ConfigDict(extra="forbid")

    product: str
    proof: str
    margin_percent: Decimal
    capacity: int
    destination: str
    business_model: Literal["saas", "ecommerce", "service"]
    tenure_multiple_months: Decimal
    ltv_override_minor: Decimal | None = None

    @field_validator("product", "proof", "destination")
    @classmethod
    def _text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("profile text answer must be non-empty")
        return text

    @field_validator(
        "margin_percent", "tenure_multiple_months", "ltv_override_minor", mode="before"
    )
    @classmethod
    def _decimal(cls, value: Any) -> Any:
        return str(value) if not isinstance(value, (Decimal, int)) else value

    @field_validator("capacity", mode="before")
    @classmethod
    def _int(cls, value: Any) -> Any:
        return int(value) if isinstance(value, str) and value.isdigit() else value


def profile_intake_violations(answers: dict[str, Any]) -> list[str]:
    """Validate owner answers against the durable table. Returns named errors."""

    table = load_intake_table()
    questions = table["questions"]
    errors: list[str] = []
    allowed = {entry["id"] for entry in questions} | {
        entry["id"] for entry in table["boundary_confirmations"]
    }
    for key in answers:
        if key not in allowed:
            errors.append(f"unknown profile answer: {key}")
    for entry in questions:
        qid, qtype = entry["id"], entry["type"]
        value = answers.get(qid)
        if value in (None, ""):
            if entry["required"]:
                errors.append(f"missing required profile answer: {qid}")
            continue
        if qtype == "percent":
            number = _as_number(value)
            if number < 0 or number > 100:
                errors.append(f"profile answer out of range: {qid} must be 0-100 percent")
        elif qtype == "integer":
            if _as_number(value) != _as_number(value).to_integral_value():
                errors.append(f"profile answer must be a whole number: {qid}")
            elif _as_number(value) < entry.get("minimum", float("-inf")):
                errors.append(f"profile answer below minimum: {qid}")
        elif qtype == "number":
            number = _as_number(value)
            if number < entry.get("minimum", float("-inf")):
                errors.append(f"profile answer below minimum: {qid}")
        elif qtype == "enum":
            if value not in entry.get("options", []):
                errors.append(f"profile answer not in allowed options: {qid}")
        elif qtype == "money_minor":
            if _as_number(value) < 0:
                errors.append(f"profile answer may not be negative: {qid}")
    for entry in table["boundary_confirmations"]:
        cid = entry["id"]
        if entry.get("required") and not answers.get(cid):
            errors.append(f"missing required boundary confirmation: {cid}")
    return errors


def validate_or_raise(answers: dict[str, Any]) -> BusinessProfileV1:
    """Validate answers against the table, then type them. Fails closed."""

    errors = profile_intake_violations(answers)
    if errors:
        raise ProfileIntakeError("; ".join(errors))
    # The table is the source of truth for which answers are required; the model
    # constructor is the typed landing spot. Missing required answers already
    # failed above, so construction here can only fail on type coercion, which
    # the table validation has already excluded.
    return BusinessProfileV1(
        **{
            key: value
            for key, value in answers.items()
            if key in {entry["feeds"] for entry in load_intake_table()["questions"]}
        }
    )
