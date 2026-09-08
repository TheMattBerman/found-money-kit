"""Complete Recovery Play and Concept Card contracts for FM-026."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from found_money.contracts.strategy import StrategyProviderName

CopyKind = Literal[
    "general",
    "number",
    "product",
    "proof",
    "objection",
    "urgency",
    "capacity",
    "destination",
]

_PLACEHOLDER_RE = re.compile(
    r"(?:\bDRAFT\b|\bTBD\b|\bTODO\b|\[TODO\]|\[CONFIRM|\{\{.+?\}\}|\blorem\b)",
    re.IGNORECASE,
)
_HEADING_RE = re.compile(r"^(?:#{1,6}\s+.+|[^.!?]+:)$")
_PII_RE = re.compile(
    r"(?:https?://|\b[^\s@]+@[^\s@]+\.[^\s@]+\b|"
    r"(?<!\w)\+?\d[\d .()/-]{7,}\d(?!\w)|"
    r"\b(?:cus|src|inv|contact|deal|rec)_[a-z0-9_-]+\b)",
    re.IGNORECASE,
)
_SCRIPT_RE = re.compile(
    r"(?:\bscene\s+\d+\b|\bvoiceover\s*:|\bshot\s+list\b|\b\d+:\d{2}\b)", re.IGNORECASE
)


def _clean_text(value: Any, *, allow_none: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError("content must be a string")
    text = " ".join(value.split())
    if allow_none and text.casefold() == "none":
        return "none"
    if len(text) < 8:
        raise ValueError("content must be finished prose")
    if _PLACEHOLDER_RE.search(text) or _HEADING_RE.fullmatch(text):
        raise ValueError("headings and placeholders are not finished content")
    if _PII_RE.search(text):
        raise ValueError("content contains raw identity, source id, phone, email, or URL")
    return text


class CanonicalModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    def to_canonical_json(self) -> bytes:
        text = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return (text + "\n").encode("utf-8")


class GroundedCopyV1(CanonicalModel):
    text: str
    kind: CopyKind = "general"
    evidence_ids: list[str]

    @field_validator("text", mode="before")
    @classmethod
    def _text(cls, value: Any) -> str:
        return _clean_text(value)

    @field_validator("evidence_ids", mode="before")
    @classmethod
    def _refs(cls, value: Any) -> list[str]:
        if not isinstance(value, list) or not value:
            raise ValueError("grounded copy requires evidence ids")
        refs = [str(item).strip() for item in value]
        if any(not ref.startswith("ev_") for ref in refs) or len(refs) != len(set(refs)):
            raise ValueError("evidence ids must be unique ev_ identifiers")
        return refs


class CustomerCopyV1(CanonicalModel):
    """Customer-facing copy: ungrounded, bounded only by cheap negative checks.

    GroundedCopyV1 proves operator claims against the evidence packet. Customer
    copy is the owner's voice to their customer and cites nothing; per-field
    evidence grounding is what made every sentence hedge.
    The negative rules still hold: no placeholders, no PII, and
    ``validate_complete_recovery_play_set`` rejects ledger money figures
    (``category="map_value"``) resolved to minor units.
    """

    text: str
    kind: CopyKind = "general"

    @field_validator("text", mode="before")
    @classmethod
    def _text(cls, value: Any) -> str:
        return _clean_text(value)


class UrgencyV1(CanonicalModel):
    text: str
    evidence_ids: list[str] = Field(default_factory=list)

    @field_validator("text", mode="before")
    @classmethod
    def _text(cls, value: Any) -> str:
        return _clean_text(value, allow_none=True)

    @model_validator(mode="after")
    def _refs(self) -> Self:
        if self.text == "none" and self.evidence_ids:
            raise ValueError("urgency none cannot cite urgency evidence")
        if self.text != "none" and not self.evidence_ids:
            raise ValueError("real urgency requires evidence")
        return self


class EmailStepV1(CanonicalModel):
    order: int = Field(ge=1)
    lifecycle_stage: GroundedCopyV1
    subject: CustomerCopyV1
    body: CustomerCopyV1
    cta: CustomerCopyV1
    wait_days: int = Field(ge=0)


class SmsPlanV1(CanonicalModel):
    available: bool
    messages: list[CustomerCopyV1] = Field(default_factory=list)
    unavailable_reason: str | None = None

    @model_validator(mode="after")
    def _availability(self) -> Self:
        if self.available:
            if not self.messages or self.unavailable_reason is not None:
                raise ValueError("available SMS requires messages and no unavailable reason")
        elif self.messages or self.unavailable_reason is None:
            raise ValueError("unavailable SMS requires an explicit reason and no messages")
        if self.unavailable_reason is not None:
            self.unavailable_reason = _clean_text(self.unavailable_reason)
        return self


class OfferV1(CanonicalModel):
    mechanism: CustomerCopyV1
    rationale: GroundedCopyV1
    constraints: list[GroundedCopyV1] = Field(min_length=1)


OfferRungRole = Literal["high_anchor", "core", "downsell"]
OfferConditionKind = Literal["milestone", "results_share", "implementation_step", "clawback"]
PileValueBasis = Literal["observed_face_value", "modeled_opportunity", "mixed"]
PileConfidenceClass = Literal["observed", "modeled", "mixed"]
_OFFER_RUNG_ORDER: tuple[OfferRungRole, ...] = ("high_anchor", "core", "downsell")


class UnresolvedOfferPricingV1(CanonicalModel):
    """Pricing the frozen evidence packet cannot support; surfaces for human review."""

    field_name: Literal["price_terms"] = "price_terms"
    review_reason: GroundedCopyV1


class OfferRungV1(CanonicalModel):
    id: str
    role: OfferRungRole
    mechanism: CustomerCopyV1
    terms: CustomerCopyV1
    scope: GroundedCopyV1
    support: GroundedCopyV1
    commitment: GroundedCopyV1
    constraints: list[GroundedCopyV1] = Field(min_length=1)
    rationale: GroundedCopyV1
    condition_kind: OfferConditionKind
    condition: GroundedCopyV1
    discount_percent: float | None = Field(default=None, ge=0, le=100)
    commitment_term: str | None = None
    concedes_money: bool = False
    scope_reduction: GroundedCopyV1 | None = None
    price_terms: GroundedCopyV1 | None = None
    price_terms_unresolved: UnresolvedOfferPricingV1 | None = None

    @field_validator("id", mode="before")
    @classmethod
    def _id(cls, value: Any) -> str:
        return _clean_text(value)

    @field_validator("commitment_term", mode="before")
    @classmethod
    def _commitment_term(cls, value: Any) -> str | None:
        if value is None:
            return None
        return _clean_text(value)

    @model_validator(mode="after")
    def _pricing_exclusive(self) -> Self:
        if self.price_terms is not None and self.price_terms_unresolved is not None:
            raise ValueError("price_terms and price_terms_unresolved are mutually exclusive")
        return self


class OfferLadderV1(CanonicalModel):
    offer_ladder_id: str
    diagnosis: GroundedCopyV1
    outcome: GroundedCopyV1
    ordering_basis: GroundedCopyV1
    evidence_references: list[str]
    rungs: list[OfferRungV1] = Field(min_length=3, max_length=3)
    pile_value_basis: PileValueBasis = "observed_face_value"
    pile_confidence_class: PileConfidenceClass = "observed"
    selected_offer_rung_id: str | None = None

    @field_validator("offer_ladder_id", mode="before")
    @classmethod
    def _ladder_id(cls, value: Any) -> str:
        return _clean_text(value)

    @field_validator("evidence_references", mode="before")
    @classmethod
    def _refs(cls, value: Any) -> list[str]:
        if not isinstance(value, list) or not value:
            raise ValueError("ladder evidence_references must be a non-empty list")
        refs = [str(item).strip() for item in value]
        if any(not ref.startswith("ev_") for ref in refs) or len(refs) != len(set(refs)):
            raise ValueError("ladder evidence ids must be unique ev_ identifiers")
        return refs

    @model_validator(mode="after")
    def _shape(self) -> Self:
        roles = [rung.role for rung in self.rungs]
        if roles != list(_OFFER_RUNG_ORDER):
            raise ValueError("offer ladder rungs must be serialized high_anchor, core, downsell")
        if len({rung.role for rung in self.rungs}) != 3:
            raise ValueError("offer ladder requires exactly one rung per role")
        if len({rung.id for rung in self.rungs}) != 3:
            raise ValueError("offer rung ids must be unique within a ladder")
        if self.selected_offer_rung_id is not None:
            rung_ids = {rung.id for rung in self.rungs}
            if self.selected_offer_rung_id not in rung_ids:
                raise ValueError("selected_offer_rung_id must name a ladder rung")
        return self


class OfferRungCopyPackageV1(CanonicalModel):
    """Customer-facing copy for one ladder rung (FM-059 AC-8)."""

    offer_ladder_id: str
    offer_rung_id: str
    subject: CustomerCopyV1
    body: CustomerCopyV1
    cta: CustomerCopyV1

    @field_validator("offer_ladder_id", "offer_rung_id", mode="before")
    @classmethod
    def _ids(cls, value: Any) -> str:
        return _clean_text(value)


class ObjectionV1(CanonicalModel):
    objection: GroundedCopyV1
    response: GroundedCopyV1


class CalendarStepV1(CanonicalModel):
    day: int = Field(ge=0)
    action: GroundedCopyV1
    stop_condition: GroundedCopyV1


class TrackingPlanV1(CanonicalModel):
    success_event: GroundedCopyV1
    tracked_signals: list[GroundedCopyV1] = Field(min_length=1)


_COMPLETE_PLAY_PILE_IDS = {
    "payment_rescue",
    "lapsed_repeat_buyer",
    "overdue_reorder",
    "disappeared_high_value_customer",
    "canceled_customer",
}


class ConceptCardV1(CanonicalModel):
    card_id: str
    card_name: str
    pile_id: str = "payment_rescue"
    audience_tension: GroundedCopyV1
    big_idea: GroundedCopyV1
    hook: GroundedCopyV1
    opening_visual: GroundedCopyV1
    proof_device: GroundedCopyV1
    format_style: str
    cta: GroundedCopyV1
    pile_fit: GroundedCopyV1
    production_requirements: list[str] = Field(min_length=1)

    @field_validator("pile_id", mode="before")
    @classmethod
    def _pile(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("pile_id must be a string")
        text = value.strip()
        if text not in _COMPLETE_PLAY_PILE_IDS:
            raise ValueError("pile_id is not supported for complete plays")
        return text

    @field_validator("card_id", "card_name", "format_style", mode="before")
    @classmethod
    def _identity_text(cls, value: Any) -> str:
        return _clean_text(value)

    @field_validator("production_requirements", mode="before")
    @classmethod
    def _requirements(cls, value: Any) -> list[str]:
        if not isinstance(value, list) or not value:
            raise ValueError("production requirements must be a non-empty list")
        requirements = [_clean_text(item) for item in value]
        if any(_SCRIPT_RE.search(item) for item in requirements):
            raise ValueError("production requirements cannot contain a finished script")
        return requirements


class SequencePlanV1(CanonicalModel):
    """How many touches this play intends, and what happens when they run out.

    A one-touch play that stops on purpose is a correct play. Without this, a
    single-step sequence is indistinguishable from a truncated multi-step one,
    which is why length alone cannot carry the intent.
    """

    intent: Literal["single_touch", "multi_touch"]
    stops_after_last: bool
    reengage_days: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if not self.stops_after_last and self.reengage_days is None:
            raise ValueError("a sequence that does not stop must declare a re-engagement cycle")
        return self


class CompleteRecoveryPlayV1(CanonicalModel):
    play_id: str
    pile_id: str = "payment_rescue"
    rank: int = Field(ge=1, le=3)
    title: str
    rationale: str
    recommended_actions: list[str] = Field(min_length=1)
    offer_recommendation: str
    campaign_name: str
    campaign_strategy: GroundedCopyV1
    value_basis: GroundedCopyV1
    audience: GroundedCopyV1
    recoverability: GroundedCopyV1
    diagnosis: GroundedCopyV1
    offer_ladder: OfferLadderV1
    rung_copy_packages: list[OfferRungCopyPackageV1] = Field(min_length=3, max_length=3)
    lifecycle_sequence: GroundedCopyV1
    sequence_plan: SequencePlanV1
    email_sequence: list[EmailStepV1] = Field(min_length=1)
    sms: SmsPlanV1
    task_talk_track: GroundedCopyV1
    primary_cta: GroundedCopyV1
    objections: list[ObjectionV1] = Field(min_length=2)
    urgency: UrgencyV1
    calendar: list[CalendarStepV1] = Field(min_length=1)
    stop_conditions: list[GroundedCopyV1] = Field(min_length=2)
    tracking: TrackingPlanV1
    channel_emphasis: GroundedCopyV1
    creative_big_idea: GroundedCopyV1
    concept_cards: list[ConceptCardV1] = Field(min_length=3, max_length=3)
    evidence_references: list[str]

    @field_validator("pile_id", mode="before")
    @classmethod
    def _pile(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("pile_id must be a string")
        text = value.strip()
        if text not in _COMPLETE_PLAY_PILE_IDS:
            raise ValueError("pile_id is not supported for complete plays")
        return text

    @model_validator(mode="after")
    def _sequence_coherent(self) -> Self:
        """Length, calendar, and declared intent must agree.

        Length alone cannot say whether a one-step play stopped on purpose, so
        the declared intent has to match the shape that was actually emitted.
        """

        steps = len(self.email_sequence)
        if len(self.calendar) != steps:
            raise ValueError("calendar must have one step per email step")
        if self.sequence_plan.intent == "single_touch" and steps != 1:
            raise ValueError("single_touch sequences carry exactly one email step")
        if self.sequence_plan.intent == "multi_touch" and steps < 2:
            raise ValueError("multi_touch sequences carry at least two email steps")
        orders = [step.order for step in self.email_sequence]
        if orders != list(range(1, steps + 1)):
            raise ValueError("email steps must be ordered 1..n with no gaps")
        waits = [step.wait_days for step in self.email_sequence]
        if waits != sorted(waits):
            raise ValueError("cumulative wait_days must not decrease")
        days = [item.day for item in self.calendar]
        if days != waits:
            raise ValueError("calendar days must match cumulative email wait_days")
        return self

    @field_validator(
        "play_id", "title", "rationale", "offer_recommendation", "campaign_name", mode="before"
    )
    @classmethod
    def _text(cls, value: Any) -> str:
        return _clean_text(value)

    @field_validator("recommended_actions", mode="before")
    @classmethod
    def _actions(cls, value: Any) -> list[str]:
        if not isinstance(value, list) or not value:
            raise ValueError("recommended actions must be a non-empty list")
        return [_clean_text(item) for item in value]

    @model_validator(mode="after")
    def _rung_copy_coherent(self) -> Self:
        ladder_id = self.offer_ladder.offer_ladder_id
        rung_ids = {rung.id for rung in self.offer_ladder.rungs}
        if len(self.rung_copy_packages) != 3:
            raise ValueError("rung_copy_packages must contain exactly three packages")
        package_rung_ids: set[str] = set()
        for package in self.rung_copy_packages:
            if package.offer_ladder_id != ladder_id:
                raise ValueError("rung_copy_packages must reference the play offer_ladder_id")
            if package.offer_rung_id not in rung_ids:
                raise ValueError("rung_copy_packages reference an unknown offer_rung_id")
            if package.offer_rung_id in package_rung_ids:
                raise ValueError("rung_copy_packages must cover each ladder rung once")
            package_rung_ids.add(package.offer_rung_id)
        if package_rung_ids != rung_ids:
            raise ValueError("rung_copy_packages must cover each ladder rung exactly once")
        return self

    @model_validator(mode="after")
    def _coverage(self) -> Self:
        card_ids = [card.card_id for card in self.concept_cards]
        if len(card_ids) != len(set(card_ids)):
            raise ValueError("concept card ids must be unique within a play")
        refs: list[str] = []

        def collect(value: Any) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    if key == "evidence_ids" and isinstance(item, list):
                        refs.extend(item)
                    else:
                        collect(item)
            elif isinstance(value, list):
                for item in value:
                    collect(item)

        collect(self.model_dump(mode="python", exclude={"evidence_references"}))
        exact = sorted(set(refs))
        if self.evidence_references != exact:
            raise ValueError("evidence_references must exactly enumerate nested copy evidence")
        return self


class CompleteRecoveryPlaySetV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["recovery-plays.v1"] = "recovery-plays.v1"
    run_id: str
    built_at: datetime
    provider: StrategyProviderName = "fixture"
    plays: list[CompleteRecoveryPlayV1] = Field(min_length=3, max_length=3)

    @field_validator("built_at", mode="before")
    @classmethod
    def _built_at(cls, value: Any) -> datetime:
        if isinstance(value, str) and value.endswith("Z"):
            value = value[:-1] + "+00:00"
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            raise ValueError("built_at must be timezone aware")
        return parsed.astimezone(timezone.utc).replace(
            microsecond=(parsed.microsecond // 1000) * 1000
        )

    @model_validator(mode="after")
    def _set(self) -> Self:
        ordered = sorted(self.plays, key=lambda play: play.rank)
        if [play.rank for play in ordered] != [1, 2, 3]:
            raise ValueError("complete recovery plays require ranks 1, 2, and 3")
        if len({play.play_id for play in self.plays}) != 3:
            raise ValueError("complete recovery play ids must be unique")
        if len({play.campaign_name for play in self.plays}) != 3:
            raise ValueError("complete campaign names must be unique")
        if len({card.card_id for play in self.plays for card in play.concept_cards}) != 9:
            raise ValueError("complete recovery plays require nine unique concept cards")
        return self

    def canonical_dict(self) -> dict[str, Any]:
        payload = self.model_dump(mode="json")
        payload["built_at"] = self.built_at.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        return payload

    def to_canonical_json(self) -> bytes:
        text = json.dumps(self.canonical_dict(), sort_keys=True, separators=(",", ":"))
        return (text + "\n").encode("utf-8")


class DifferentiationCheckV1(CanonicalModel):
    left_play_id: str
    right_play_id: str
    axis: Literal[
        "diagnosis",
        "offer_mechanism",
        "lifecycle_sequence",
        "primary_cta",
        "channel_emphasis",
        "creative_big_idea",
    ]
    passed: bool
    finding: str | None = None


class DifferentiationReportV1(CanonicalModel):
    schema_version: Literal["recovery-play-differentiation.v1"] = "recovery-play-differentiation.v1"
    recovery_plays_sha256: str
    checks: list[DifferentiationCheckV1]
    passed: bool
    actionable_findings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _derived(self) -> Self:
        if len(self.checks) != 18:
            raise ValueError("differentiation report requires 18 pairwise checks")
        derived = all(check.passed for check in self.checks)
        if self.passed != derived:
            raise ValueError("differentiation pass must be derived from checks")
        if not derived and not self.actionable_findings:
            raise ValueError("failed differentiation requires actionable findings")
        return self


PAIR_AXES: tuple[str, ...] = (
    "diagnosis",
    "offer_mechanism",
    "lifecycle_sequence",
    "primary_cta",
    "channel_emphasis",
    "creative_big_idea",
)

# Rubric v2, Rubric B. `format_style`, `cta` and
# `production_requirements_and_no_finished_script` are absent on purpose: they are
# schema facts and `found_money.strategy.card_checks` decides them. Asking a model
# to rediscover them is what produced whole-field reversals between rounds.
CARD_JUDGMENT_FIELDS: tuple[str, ...] = (
    "card_name",
    "audience_tension",
    "big_idea",
    "hook",
    "opening_visual",
    "proof_device",
    "pile_fit",
)

# A field failing across a block of cards with one rationale is a disagreement
# about where the bar sits, not N defects. Mirrors `scripts/blind_review.py`.
WHOLE_FIELD_MIN = 3


def expected_review_items() -> frozenset[tuple[str, str]]:
    """The exact (target_id, rubric) set a rubric v2 review must cover: 18 + 63."""

    pairs = {
        (f"pair:{left}-{right}", axis)
        for left, right in ((1, 2), (1, 3), (2, 3))
        for axis in PAIR_AXES
    }
    cards = {
        (f"card:{play}-{card}", field)
        for play in range(1, 4)
        for card in range(1, 4)
        for field in CARD_JUDGMENT_FIELDS
    }
    return frozenset(pairs | cards)


class ReviewerScoreV2(CanonicalModel):
    target_id: str
    rubric: str
    passed: bool
    finding: str | None = None
    evidence: str | None = None

    @model_validator(mode="after")
    def _cite_or_drop(self) -> Self:
        """A failure carries a finding and the artifact text it refers to.

        The rubric says a failure with no citation is not a finding. Enforcing it
        here rather than in the runner means a packet cannot record an uncited
        failure at all, instead of one being dropped at print time.
        """
        if self.passed:
            return self
        if not (self.finding or "").strip():
            raise ValueError("a failing score requires an actionable finding")
        if not (self.evidence or "").strip():
            raise ValueError("a failing score requires cited artifact text")
        return self


class CriterionDisputeV1(CanonicalModel):
    """One whole-field disagreement, folded and then adjudicated by a human.

    `upheld` means the objection stands and the artifact has a defect. `rejected`
    means the reviewer misread the bar. Either way the disagreement is recorded
    rather than voted away, which is the whole point of routing it here.
    """

    rubric: str
    target_ids: list[str] = Field(min_length=WHOLE_FIELD_MIN)
    shared_rationale: str = Field(min_length=1)
    adjudication: Literal["upheld", "rejected"]
    adjudication_rationale: str = Field(min_length=1)
    adjudicated_by: str = Field(min_length=1)

    @model_validator(mode="after")
    def _card_targets(self) -> Self:
        if len(set(self.target_ids)) != len(self.target_ids):
            raise ValueError("criterion dispute lists a target twice")
        if not all(target.startswith("card:") for target in self.target_ids):
            raise ValueError("criterion disputes fold card fields only")
        if self.rubric not in CARD_JUDGMENT_FIELDS:
            raise ValueError(f"criterion dispute names an unscored rubric: {self.rubric}")
        return self


class ReviewerRunV1(CanonicalModel):
    """One complete pass of the rubric over one artifact, in one fresh context.

    `context_id` records the operator's claim that this run was independent of the
    other. Nothing in-band can prove that; naming it makes the claim auditable
    instead of implicit.
    """

    context_id: str = Field(min_length=1)
    reviewer_runtime: str = Field(min_length=1)
    reviewer_model_family: str = Field(min_length=1)
    scores: list[ReviewerScoreV2]

    @model_validator(mode="after")
    def _covers_the_rubric(self) -> Self:
        actual = {(score.target_id, score.rubric) for score in self.scores}
        if len(actual) != len(self.scores):
            raise ValueError("review run scores an item twice")
        if actual != expected_review_items():
            raise ValueError("review run must exactly cover all pair axes and card fields")
        return self


class StrategyReviewerPacketV2(CanonicalModel):
    """A certified blind review under rubric v2.

    A pass means two consecutive clean runs on the same frozen artifact hash from
    a reviewer family that is not the producer's, with every whole-field
    disagreement adjudicated. One clean run is a sample, not a certification.
    """

    schema_version: Literal["strategy-reviewer-packet.v2"] = "strategy-reviewer-packet.v2"
    rubric_version: Literal[2] = 2
    reviewed_artifact_sha256: str
    blind: Literal[True]
    producer_context_supplied: Literal[False]
    producer_model_family: str = Field(min_length=1)
    runs: list[ReviewerRunV1]
    disputes: list[CriterionDisputeV1] = Field(default_factory=list)
    passed: bool
    actionable_findings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _review(self) -> Self:
        if len(self.runs) != 2:
            raise ValueError("certification requires two consecutive runs on the same artifact")
        if len({run.context_id for run in self.runs}) != 2:
            raise ValueError("the two runs must come from independent contexts")
        for run in self.runs:
            if run.reviewer_model_family.casefold() == self.producer_model_family.casefold():
                raise ValueError("reviewer model family must differ from producer")

        folded = {
            (target, dispute.rubric) for dispute in self.disputes for target in dispute.target_ids
        }
        if len({dispute.rubric for dispute in self.disputes}) != len(self.disputes):
            raise ValueError("a rubric may be disputed at most once per packet")

        failures = {
            (score.target_id, score.rubric)
            for run in self.runs
            for score in run.scores
            if not score.passed
        }
        unsupported = folded - failures
        if unsupported:
            raise ValueError("a criterion dispute folds items no run actually failed")

        item_failures = sorted(failures - folded)
        upheld = sorted(
            dispute.rubric for dispute in self.disputes if dispute.adjudication == "upheld"
        )
        derived = not item_failures and not upheld
        if self.passed != derived:
            raise ValueError("review pass must be derived from scores and adjudicated disputes")
        if not derived and not self.actionable_findings:
            raise ValueError("failed review requires actionable findings")
        if derived and self.actionable_findings:
            raise ValueError("a passing review carries no actionable findings")
        return self
