---
name: recovery-intelligence
description: >
  Domain intelligence for writing recovery strategy, offers, cadence, and
  customer-facing copy in Found Money. Use when generating or reviewing a
  Recovery Play, an offer ladder, a win-back email sequence, or the cadence and
  stop conditions for a pile. Covers what to offer, how long the sequence runs,
  what the first message may contain, and which framings are forbidden per pile.
  Do NOT use to send anything, to value a pile, or to decide publication.
---

# Recovery intelligence

The domain brain behind the generator. What to offer someone who went quiet, how
to say it, how many times to say it, and when to stop.

Use `references/` for diagnosis, offer judgment, cadence, and voice. The matching rules in `tables/` are enforced when the kit generates a campaign. Review the result against those rules; do not bypass validation to force a campaign through.

## What this skill guides this skill decides

1. **What kind of offer** a pile gets, and in which direction.
   `references/offers.md`, `tables/offer-direction.json`
2. **How long the sequence is** and when it stops.
   `references/cadence.md`, `tables/pile-cohort.json`
3. **What the first message may contain.**
   `references/message.md`, `tables/first-touch-rules.json`
4. **What is forbidden on a given pile.**
   `references/diagnosis.md`, `tables/pile-prohibitions.json`
5. **Which channels the segment gets**, including whether paid is worth
   recommending, and what the ad concept looks like if it is.
   `references/channels.md`, `tables/channels.json`, `tables/ad-concepts.json`
6. **How it should sound.**
   `references/voice.md`

## Rules that are not negotiable

These rules are enforced by the kit and apply to every campaign review.

- **The first touch asks for a reply.** No booking link, no purchase link, no URL.
  A link in touch one tells the reader it is a broadcast and they stop reading.
- **Do not answer your own question.** One question, then stop. Appending the
  pitch is the single most common mistake in win-back email.
- **Failed payment is not a cancellation.** Never use farewell framing or an
  access-loss ultimatum on an involuntary lapse. It hands an exit to someone who
  never chose one.
- **A discount must buy a commitment.** An unbound percentage off a
  month-to-month price trains the best customers to go quiet and wait.
- **Unpriced piles do not concede money.** If the pile's value is unknown or
  modeled, the offer may not give away cash. Non-monetary rungs only.
- **A paid play cannot claim recovered revenue.** Ad platforms are incentivised to
  take credit for people who would have returned anyway. Tracking must name a real
  incrementality method, not platform-reported ROAS.
- **The kit never creates an audience.** It recommends; the owner builds and runs
  it. Same posture as email.

## What this skill does not do

- It does not value a pile. Use the Money Map and its value basis for amounts.
- It does not send, schedule, or create an audience. Nothing here is an
  execution path.
- It does not approve sending or launch campaigns.
- It does not override a validator. If a table and the generated object disagree,
  the object is wrong.
