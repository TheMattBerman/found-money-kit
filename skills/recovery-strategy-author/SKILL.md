---
name: recovery-strategy-author
description: >
  Author grounded recovery play sets from frozen GroundedStrategyEvidencePacketV1
  and StrategyBusinessProfileV1 in Found Money. Reads domain references from
  skills/recovery-intelligence/references/ and enforces table constraints from
  skills/recovery-intelligence/tables/. Emits complete play sets passing all
  campaign and copy validators.
---

# Recovery Strategy Author

The production strategy generator for Found Money. Authors complete, grounded
recovery play sets tailored to a business's money map and profile intake.

## The Strategy Authoring Contract

The generator converts a frozen, PII-free evidence packet and an intake business
profile into a validated recovery strategy. It holds judgment and voice, while
table rules and Python validators hold enforcement.

### 1. Inputs

- **`GroundedStrategyEvidencePacketV1`**: Frozen aggregate pile facts, value basis,
  evidence IDs (`ev_pile_1_value`, `ev_business_margin`, etc.). Never invent an
  evidence ID not declared in `allowed_evidence_ids`.
- **`StrategyBusinessProfileV1`**: Owner intake answers: product, proof, margin,
  capacity, destination, business model, tenure multiple, and optional LTV override.

### 2. Domain Intelligence Sources

The generator reads from `skills/recovery-intelligence/`:

- **Voice (`references/voice.md`)**: SPEAR framework, Starbucks Test. Lowercase,
  utilitarian subject line (e.g. `billing glitch?`, `quick note`). Plain text,
  single-question body (~9 words), bare first-name sign-off. No links in touch one.
- **Cadence (`references/cadence.md`, `tables/pile-cohort.json`)**: Sequence length
  is capped by the cohort table ceiling (e.g., max 3 for involuntary payment
  recovery, max 2 for churned customers, max 1 for cold prospects). Re-engagement
  is a separate 90-day cycle, not an extra email step.
- **Diagnosis (`references/diagnosis.md`, `tables/pile-prohibitions.json`)**: Involuntary
  lapses (`failed_payment`, `payment_rescue`) never receive farewell or cancellation
  framing. Churned customers (`canceled_customer`) never receive billing glitch framing.
  Closed-lost deals (`closed_lost_stale_deal`) get review-only actions, never email plays.
- **Offers (`references/offers.md`, `tables/offer-direction.json`)**: Every play
  generates a 3-rung ladder: `high_anchor`, `core`, `downsell`. Step-up rungs for
  customers (bonuses, waived continuity, non-monetary perks), step-down for prospects
  (payment plans, lower tier, trial with a catch). Discounts must bind to commitment.
- **Channels (`references/channels.md`, `tables/channels.json`)**: Email, SMS when
  appropriate, and human review task.

### 3. Required Output

The author produces a `CompleteRecoveryPlaySetV1`:

1. **Plays**: One play per qualified pile in `_COMPLETE_PLAY_PILE_IDS` present in the
   money map. Closed-lost deals and unclassified piles do NOT receive complete email plays.
2. **Offer Ladder**: Exactly 3 rungs with roles `high_anchor`, `core`, and `downsell`.
3. **Copy Packages**: Exactly 3 copy packages, matching the 3 rungs.
4. **Email Sequence**: 1 to 3 steps respecting `touch_ceiling_for(pile_id)`.
   Touch 1 must pass all first-touch rules (no URLs, 1 question, no throat-clearing).
5. **Concept Cards**: Exactly 3 cards per play (`concept_cards`), with audience tension,
   big idea, hook, opening visual, proof device, format, CTA, pile fit, and production
   requirements.
6. **Grounding**: Operator-facing copy and numbers cite exact evidence IDs. Customer-facing
   copy contains NO ledger money and NO PII.
7. **Validation Gate**: Must pass `validate_complete_recovery_play_set` without errors.
