"""Loader for the recovery-intelligence skill tables.

The domain rules live in ``skills/recovery-intelligence/tables/`` so they can be
read and edited as expertise rather than as Python literals. This module is the
thin enforcement seam: it loads those tables and exposes them as typed values. It
holds no domain knowledge of its own.

Adding a rule means editing a table. Changing behaviour here should be rare.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, get_args

from found_money.contracts.map import PileId

Cohort = Literal["customer", "prospect"]
Warmth = Literal["involuntary", "warm", "cold", "product_aware"]

_TABLE_DIRNAME = Path("skills") / "recovery-intelligence" / "tables"


class IntelligenceTableError(RuntimeError):
    """A skill table is missing, unreadable, or does not agree with the contract.

    Raised rather than falling back to a default. A silently absent rule is worse
    than a hard failure: it disables a check without telling anyone.
    """


def _repo_root() -> Path:
    """Walk up from this file until the skill tables are found.

    The tables deliberately live outside the ``found_money`` package so the skill
    owns them. That means they are resolved relative to the working tree rather
    than to package data, which is fine because the kit is operated from a clone.
    If the kit is ever installed as a standalone wheel this is the seam that has
    to change.
    """

    for candidate in Path(__file__).resolve().parents:
        if (candidate / _TABLE_DIRNAME).is_dir():
            return candidate
    raise IntelligenceTableError(
        f"recovery-intelligence tables not found; expected {_TABLE_DIRNAME.as_posix()} "
        "in a parent of found_money/strategy/intelligence.py"
    )


@lru_cache(maxsize=None)
def load_table(name: str) -> dict[str, Any]:
    """Load one skill table by stem, e.g. ``pile-cohort``."""

    path = _repo_root() / _TABLE_DIRNAME / f"{name}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise IntelligenceTableError(f"missing skill table: {name}.json") from exc
    except json.JSONDecodeError as exc:
        raise IntelligenceTableError(f"malformed skill table: {name}.json ({exc})") from exc
    if not isinstance(payload, dict):
        raise IntelligenceTableError(f"skill table is not an object: {name}.json")
    return payload


@lru_cache(maxsize=None)
def pile_plan() -> dict[str, dict[str, Any]]:
    """Per-pile cohort, warmth, touch ceiling, and re-engagement interval.

    Fails closed if the table does not cover exactly the contract's ``PileId``
    set, so adding a pile without classifying it is caught at load time rather
    than producing an unplanned sequence.
    """

    piles = load_table("pile-cohort").get("piles")
    if not isinstance(piles, dict):
        raise IntelligenceTableError("pile-cohort.json has no 'piles' object")

    declared = set(get_args(PileId))
    present = set(piles)
    if present != declared:
        missing = sorted(declared - present)
        extra = sorted(present - declared)
        raise IntelligenceTableError(
            f"pile-cohort.json must cover every PileId exactly once; "
            f"missing={missing}, extra={extra}"
        )

    for pile, entry in piles.items():
        if not isinstance(entry, dict):
            raise IntelligenceTableError(f"pile-cohort.json entry is not an object: {pile}")
        for key in ("cohort", "warmth", "touches", "reengage_days"):
            if key not in entry:
                raise IntelligenceTableError(f"pile-cohort.json {pile} is missing '{key}'")
        if entry["cohort"] not in get_args(Cohort):
            raise IntelligenceTableError(
                f"pile-cohort.json {pile} has unknown cohort {entry['cohort']!r}"
            )
        if entry["warmth"] not in get_args(Warmth):
            raise IntelligenceTableError(
                f"pile-cohort.json {pile} has unknown warmth {entry['warmth']!r}"
            )
        touches = entry["touches"]
        if not isinstance(touches, int) or touches < 1:
            raise IntelligenceTableError(f"pile-cohort.json {pile} touches must be an integer >= 1")
        reengage = entry["reengage_days"]
        if reengage is not None and (not isinstance(reengage, int) or reengage < 1):
            raise IntelligenceTableError(
                f"pile-cohort.json {pile} reengage_days must be null or an integer >= 1"
            )
    return piles


def cohort_for(pile_id: str) -> Cohort:
    """Which side of the prospect/customer split this pile sits on."""

    cohort: Cohort = _entry(pile_id)["cohort"]
    return cohort


def touch_ceiling_for(pile_id: str) -> int:
    """Maximum email steps this pile warrants. A ceiling, not a quota."""

    touches: int = _entry(pile_id)["touches"]
    return touches


def reengage_days_for(pile_id: str) -> int | None:
    """Days until the separate re-engagement cycle, or None if the pile has none."""

    days: int | None = _entry(pile_id)["reengage_days"]
    return days


def no_email_play_for(pile_id: str) -> bool:
    """Whether this pile permits email plays or is review action only."""

    return bool(_entry(pile_id).get("no_email_play", False))


def warmth_for(pile_id: str) -> Warmth:
    """Warmth classification of the pile: involuntary, warm, cold, product_aware."""

    warmth: Warmth = _entry(pile_id)["warmth"]
    return warmth


def _entry(pile_id: str) -> dict[str, Any]:
    try:
        return pile_plan()[pile_id]
    except KeyError as exc:
        raise IntelligenceTableError(f"pile is not classified: {pile_id}") from exc
