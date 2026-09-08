# Channels

Governs `tables/channels.json` and `tables/ad-concepts.json`.

## Paid is a fourth channel, and it is not an alternative

An owner may not want to email or call a segment at all. They may want to run
paid creative to it as a matched custom audience. That is not cold acquisition —
these are people the business already knows.

The corpus is clear that this is **not a choice between channels**:

> Isolate that limited list of contacts and run a coordinated campaign where your
> email list, paid ads, direct mail, and outbound calls all target them
> simultaneously. The synergistic frequency yields significantly higher conversion
> rates than running any of those channels in isolation.

So `channel_emphasis` should express a coordinated set, not a winner. The current
field is a single grounded string, which encodes the wrong idea.

## Reconciling with the stop rule

Round 1 says a cold contact gets one message and then a hard stop. Round 2 says
run paid alongside email rather than after it. These look contradictory and are
not.

**The stop rule governs how many times you put a message in their inbox.** It does
not govern whether the segment stays in a coordinated campaign. The email stops;
the ad does not have to. If direct outreach goes cold, the contact moves out of
active sequencing and into passive nurture, which is where paid keeps working.

## The kit's boundary, which does not move

The kit **never creates an audience**. Capability scanning blocks
`create_audience`, the transport allowlist blocks the audience endpoint, and
"audience created" is a forbidden claim.

That is not in tension with recommending paid. It is the same shape as email: the
kit writes the message and the human sends it. Here the kit produces the concept
and the recommendation, and the human exports the segment, builds the audience,
produces the creative, and runs it. Identified opportunity is not recovered
revenue, and the human executes.

## Attribution theft, and why paid plays cannot claim recovery

The single most important thing in round 2:

> Meta's system is optimized to find the lowest cost per purchase, so it is
> incentivised to serve ads to high-intent warm users who were already likely to
> convert on their own. It will aggressively capture these easy sales to inflate
> in-platform ROAS without yielding incremental reach.

A paid reactivation play is the easiest place in this product to overclaim,
because the platform will happily report conversions that would have happened
anyway. So a paid play must not carry a recovered-revenue claim, and its tracking
must name a real incrementality method — a geo holdout, the incremental
attribution setting, or blended MER across the ecosystem — rather than
platform-reported ROAS.

This is the existing posture with teeth, not a new rule.

## What we do not know

The corpus explicitly does not hold platform audience minimums or expected match
rates for an uploaded list. Those stay unquantified rather than invented. If an
owner needs a size floor, it is owner-supplied.

## Ad concepts are not email copy

The five formats in `ad-concepts.json` are for the paid play. They are concepts,
not creative: the kit describes the idea and what it would take to produce, and
stops there.

One guard worth repeating: the apology/restock format carries a discount, and it
is only legitimate because it is bound to a real stock event. Without one it is
fabricated scarcity and a panic discount at the same time, which breaks two round-1
rules at once. Do not emit it without the event.
