# Use your own HubSpot and Stripe exports

File mode reads local snapshots and creates an offline Recovery Room. It does not contact HubSpot or Stripe. A platform CSV export must first be normalized into the JSON shapes below; renaming a CSV is not enough.

Keep source files under `private-runs/inputs/`, which Git ignores. Do not paste raw customer records into chat or commit them. Preserve consistent IDs and matching fields across both sources during normalization so account identity can be reconciled. Keep raw identity data local and share only reviewed, redacted outputs.

## 1. Normalize the snapshots

The examples below contain illustrative records. Replace them with normalized exports you are authorized to analyze. Preserve currencies and source amounts. Normalize timestamps into timezone-aware ISO 8601 strings, such as `2026-01-15T12:00:00Z`. Stripe amounts use minor units: `120000` USD means $1,200.00.

Save `private-runs/inputs/hubspot.snapshot.json`:

```json
{
  "schema": "hubspot-snapshot.v1",
  "source": "hubspot",
  "retrieved_at": "2026-09-01T00:00:00Z",
  "contacts": [
    {
      "id": "contact_001",
      "email": "customer1@example.com",
      "createdate": "2026-01-15T12:00:00Z"
    }
  ],
  "deals": [
    {
      "id": "deal_001",
      "dealname": "Subscription Renewal",
      "amount": "1200.00",
      "dealstage": "closedwon",
      "closedate": "2026-01-15T12:00:00Z",
      "associated_contact_ids": ["contact_001"]
    }
  ]
}
```

Save `private-runs/inputs/stripe.snapshot.json`:

```json
{
  "schema": "stripe-snapshot.v1",
  "source": "stripe",
  "retrieved_at": "2026-09-01T00:00:00Z",
  "customers": [
    {
      "id": "cus_001",
      "email": "customer1@example.com",
      "created": "2026-01-15T12:00:00Z"
    }
  ],
  "invoices": [
    {
      "id": "in_001",
      "customer": "cus_001",
      "amount_due": 120000,
      "currency": "usd",
      "status": "open",
      "attempt_count": 2,
      "created": "2026-01-15T12:00:00Z"
    }
  ]
}
```

The full snapshot can also include subscriptions and other supported fields. Ask your agent to inspect the source schema before converting an unfamiliar export. Do not invent values for missing fields or merge accounts merely because their names match.

## 2. Point the kit at those files

Save `private-runs/inputs/config.json`:

```json
{
  "schema_version": "found-money-build-source.v1",
  "mode": "file",
  "run_mode": "public",
  "credentials": {
    "credential_mode": "none",
    "runtime_mode": "test",
    "scopes": []
  },
  "sources": {
    "hubspot_snapshot": "hubspot.snapshot.json",
    "stripe_snapshot": "stripe.snapshot.json"
  }
}
```

Snapshot paths are relative to this configuration file. `runtime_mode: test` is the required declaration for this credential-free file loader; it does not mean your input records are examples. This configuration does not call a live API.

## 3. Create the Recovery Room

From the repository root:

```bash
uv run found-money build --config private-runs/inputs/config.json --output-root private-runs/review
```

Open `private-runs/review/index.html` in a browser. This first pass reports opportunities and evidence. Without a strategy provider, it withholds campaign drafts with a `needs_strategy_review` status.

## 4. Add business context for campaign drafts

Have your agent collect the required answers in `skills/found-money/profile-intake.json`: product, proof, contribution margin, weekly review capacity, destination, business model, and customer tenure. Use owner-supplied facts. Never use another business's assumptions to fill gaps.

Put the validated answers in a `business_profile` object in the configuration and add `"strategy_provider": "skill"`. Set `business_model` consistently with the profile. The skill provider uses the kit's local strategy rules; it does not authorize a message, campaign, or live connection. Do not use the `stub` provider for an operator run.

Run the same command with a fresh output directory, such as `private-runs/review-with-profile`. The kit may still withhold a campaign when evidence is insufficient. Review the named reason rather than forcing a draft.

## 5. Read and review the result

- `index.html`: the Recovery Room, including Find, Play, Evidence, and Launch.
- `money-map.json`: ranked opportunities and their value basis.
- `recovery-plays.json`: supported campaign drafts or the deferred strategy state.
- `print-report.pdf`: printable review packet.
- `launch-pack/`: campaign handoff material for owner review.
- `run.json`: the run manifest and artifact integrity information.

Observed amounts, modeled estimates, and recorded deals are different kinds of evidence. Keep currencies separate and unpriced entries unquantified. Identified opportunity is not recovered revenue.

Confirm the audience, customer status, consent, offer economics, proof, and destination before using any draft. The kit has no send path. Carry out an approved campaign in your own tools.
