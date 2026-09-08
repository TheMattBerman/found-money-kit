# Diagnosis

Governs `tables/pile-prohibitions.json`.

## Quiet customers are not one group

Reading the digital exhaust in a CRM and billing system, four states are
distinguishable from the outside:

| Observable | State | What it means |
| --- | --- | --- |
| high opens, zero clicks | back-burner observer | still trusts you, no current trigger |
| intent spikes, no purchase | frictional buyer | high intent, blocked by one thing |
| failed billing + stopped opening | embarrassed ghost | card declined, went dark from embarrassment |
| zero opens, zero clicks 90+ days | true churn | moved on or solved it elsewhere |

## What a CRM cannot tell you

The drifter-versus-unhappy distinction is a **response** signal, not a CRM signal.
A customer who fell out of routine and one who left angry look identical in an
export. You only learn which you have after they reply: the drifter answers warmly,
the unhappy one lashes out.

The generator must not claim to know which it has. This is a real limit on what
can be asserted from a cold import, and overstating it is how the kit would lose
trust on the first run.

## Failed payment is the important one

It is the kit's default pile and it is the one most likely to be handled wrongly.

The person whose card failed is usually in **quiet embarrassment**. They did not
choose to leave. They may be having a genuinely tight month and they are ghosting
because they feel cornered, not because they rejected the product.

Two failure modes, both currently easy to produce:

**Cancellation framing.** "Sorry to see you go, let us know why you cancelled."
This is a self-inflicted wound. You have handed a frictionless exit ramp to
someone who never planned to leave, and forced a conscious decision to quit.

**Robotic dunning.** "Your payment failed. Update your card in 24 hours or lose
access." A threat triggers a defensive reaction from someone already embarrassed.

The move instead is to assume a technical fault, disarm, and pre-emptively remove
the embarrassment:

> Subject: billing glitch?
>
> It looks like the card didn't go through on the renewal.
>
> Would it be a ridiculous idea to take a quick look so we don't accidentally
> pause your access?
>
> If things are tight right now, just say so and we can adjust the terms.

"Would it be a ridiculous idea" is no-oriented; saying no is safe. Offering to
adjust the terms unprompted is what removes the embarrassment.

## Ghosted prospects

Four causes, and only some are recoverable. Roughly a fifth of the time in B2B the
counterpart never intended to buy — you were the stalking horse used to move
another vendor's price, or the source of free consulting. Others lost budget or
internal influence and went dark from embarrassment rather than disinterest.

That is why the first move on a ghosted prospect is a diagnostic, not a pitch. You
are bouncing sonar off them to read what comes back.
