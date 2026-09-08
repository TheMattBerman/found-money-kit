# Cadence, stopping, and re-engagement

Governs `tables/pile-cohort.json`.

## Length is a property of the pile

Not fixed at three. Not fixed at one. A cold prospect who ghosted a proposal and a
warm customer whose card just failed are not the same conversation and should not
get the same number of emails.

The table sets a ceiling per pile, not a quota. Fewer is fine.

## The stop is the design, not a failure

The corpus is unambiguous and three separate answers converge on it:

> "If you send a low-friction re-engagement email and the customer does not reply,
> or if they ask you to stop, you stop immediately. You do not chase."

> "It is not a sin to not get the deal, but it is a sin to take a long time to not
> get the deal."

> "Do not put them through a long, nagging email gauntlet."

For a cold contact, dead silence after the breakup message is a definitive answer.
It means yes, they have given up. Suppress and move on.

A one-touch play that stops is a correct play. It must be representable as such,
not as a truncated three-touch play.

## Re-engagement is a different cycle

Roughly 90 days, low stakes, and separate from the sequence. The reasoning:
most businesses give up on an inquiry inside 30 to 90 days, but the bulk of buying
value lands after the 90-day mark across an 18-month window. So the quarterly loop
is where the value is, and the four-day follow-up is not.

Do not encode this as a fourth `email_sequence` step with `wait_days: 90`. It is
a separate cycle with a different purpose, and flattening it says the wrong thing.

**Involuntary piles do not re-engage.** A failed card resolves or it does not.
There is no quarterly win-back loop for a billing fault.

## The final message

One line, sent word for word:

> Have you given up on [specific project, goal, or outcome]?

The corpus is explicit that this is sent with nothing added. No greeting padding,
no "I sent a proposal last week", no context. Everything else added acts as a
commercial that gives them permission to ignore it.

Because it must not vary, it is a fixed string, not something the generator
writes.

Then go silent.
