# Operating Found Money

Found Money is a local review loop:

```text
business context -> read-only sources -> exact identity -> event families -> value ledger -> Money Map -> Recovery Room
```

## Prepare the run

Start with the owner’s business model, available sources, useful recovery definition, value basis, and operational constraints. The guided flow is a fixture walkthrough:

```bash
uv sync --frozen --group dev
uv run playwright install chromium
uv run found-money guide --output-root private-runs/<business-name>
```

It does not connect to the owner’s business. For supplied exports or snapshots, read [the file-mode walkthrough](guides/FILE_MODE_WALKTHROUGH.md), inspect the config and snapshots, and run:

```bash
uv run found-money build --config private-runs/inputs/config.json --output-root private-runs/review
```

Snapshot paths are relative to the config. The configuration must match the business model and declare only the sources you supplied. Available event families vary with source coverage and fields.

## Read the Recovery Room

Open the generated `index.html`:

1. **Find** shows ranked opportunity piles.
2. **Evidence** shows event, source, confidence, and value basis.
3. **Play** turns a supported pile into a human-review campaign angle.
4. **Launch** records what a human may review or execute in their own tools.

Launch is preparation. It does not create a send job or authorize an external action.

## Interpret money

- **Observed:** a source supplied the amount and supported currency.
- **Modeled:** an estimate based on qualifying evidence and, where supplied, owner assumptions such as tenure.
- **Recorded:** a source-recorded deal or pipeline value.
- **Unquantified:** the source lacks enough amount, currency, or model evidence.

Currencies remain separate. Missing or conflicting values become data gaps. The Money Map is an opportunity map and does not say that money was collected.

## Understand selection and suppression

Identity uses exact declared keys. Ambiguous matches are withheld. Event families cover failed payments, expired trials, closed-lost deals, canceled customers, lapsed repeat buyers, silent proposals, no-shows, renewal opportunities, and engaged contacts without a booking.

A candidate can be suppressed by disqualification, dispute, later payment, reactivation, rebooking, purchase, active negotiation, an active service issue, or recent owner activity. Contactability safeguards cover unsubscribed, do-not-contact, invalid, conflicting, or unknown status. Open deals, active subscriptions, and recent activity also suppress a candidate. The private run keeps these decisions inspectable.

## Review a campaign angle

For each selected pile, describe the evidence, why it is timely, the value class, and the smallest useful human action. Flag missing consent, contactability, value, and competing activity for human review.

The operator can prepare copy or a campaign brief. Contacting a person, scheduling a task, changing CRM or billing data, creating an audience, or spending money happens separately in the owner’s tools and requires the owner’s decision.

## Live reads and privacy

Synthetic and fixture paths need no credentials. A live HubSpot or Stripe read requires explicit owner authorization, credentials supplied through the environment, and live-run controls. The operator never enables live mode, chooses a credential, or stores a token.

Keep private output local and gitignored. Inspect each specific artifact before sharing it. The source-code safety scan below checks the kit’s code and known public fixtures; it does not certify an arbitrary private-run folder:

```bash
uv run python scripts/public_safety.py
```

Public projections may contain counts, tokens, and safe evidence classes. They must not contain customer identity, source IDs, raw payloads, URLs, credentials, or customer-level values. Share only reviewed files from the run, never the whole private-run directory.

## Evidence language

Say **validated locally** for a fixture replay, dry run, or passing local check. Say **verified live** only after the real business system was inspected in the current session with authorized access. A local Recovery Room or green test suite is not live verification.
