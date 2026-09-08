---
name: found-money
description: >
  Operate Found Money with the owner: inspect local CRM and billing exports,
  collect business context, build a Money Map and Recovery Room, and recommend
  the next recovery action. Use for failed payments, expired trials, churned
  accounts, lapsed customers, and stalled deals. Read-only inputs and local
  review outputs; no sending, CRM writes, audience creation, or recovered-revenue
  claims.
---

# Found Money operator

Help the owner find a useful next action in the records they already have. Run
commands yourself, explain the result in plain language, and open the Recovery
Room. Start with the strongest supported opportunity and explain why it deserves
attention. Do not turn the owner into a repository maintainer.

## Start from the owner's situation

1. Determine whether they want to explore an example or analyze their own exports.
   Use files they have already identified; ask only for missing context. Do not
   assume a live account is authorized because credentials happen to exist.
2. Check the local installation with `bash doctor.sh`. If dependencies are missing,
   explain the issue and use the setup instructions in `docs/CLEAN_MACHINE.md`.
3. Keep inputs read-only and local. Do not paste raw records or credentials into
   chat. Store source files and results under ignored `private-runs/` directories,
   with a different folder for each result.

## Explore the example

From the repository root:

```bash
uv run found-money guide --output-root private-runs/example
```

The interactive guide collects a business profile and uses the bundled example
scenario for the selected business model by default. It does not connect to the
owner's accounts or analyze their exports. Tell the owner this before running it.
Confirm the configuration is the intended example if the environment has a guide
configuration override.

The prompts come from `skills/found-money/profile-intake.json`. The guide asks
them interactively, so do not collect the same answers twice. Help interpret a
question when needed; never fabricate margin, tenure, proof, or capacity.

## Analyze actual exports

Read `docs/guides/FILE_MODE_WALKTHROUGH.md` and inspect the export format locally.
Normalize only supported fields into the documented snapshots. Preserve IDs,
currencies, source amounts, and timestamps. Do not join accounts on name alone.

First create a credential-free file configuration pointing at the normalized
snapshots, then run:

```bash
uv run found-money build --config private-runs/inputs/config.json --output-root private-runs/review
```

Snapshot paths are relative to the configuration file. The result can show
opportunities before campaign strategy is available. Do not present withheld
strategy as a failed analysis or insert placeholder campaigns.

For campaign drafts, collect the required business-profile facts from
`skills/found-money/profile-intake.json`, add the validated `business_profile`
object to the configuration, and use `"strategy_provider": "skill"`. Read
`skills/recovery-intelligence/SKILL.md` when interpreting offers and copy. The
business model must agree with the profile. Run again into a fresh output folder.

## Interpret the Recovery Room

Open `index.html` from the result folder. Walk through Find, then Play, then
Evidence and Launch as needed.

- Recommend the highest-priority supported action, with its value basis and the
  practical reason to act. A failed payment calls for a different response from
  a customer who chose to cancel.
- Separate observed source amounts, modeled estimates, and recorded deal values.
  Keep currencies separate. Leave missing amounts unquantified. Identified
  opportunity is not recovered revenue.
- Explain modeled assumptions and missing evidence before relying on a total.
  Do not substitute a convenient default for an unknown owner input.
- Review each offer against margin, capacity, customer status, and the rules in
  `skills/recovery-intelligence/`. Do not bypass a validation failure.
- Before an owner uses a draft, identify what still needs their review: audience,
  consent, proof, offer terms, destination, and timing. Campaign handoffs are
  review material. They do not mean a campaign has run.

## Sharing and action boundaries

Public-facing artifacts must pass the kit's public-output validation. Inspect the
specific files before sharing; a source-code safety scan alone does not certify
an arbitrary folder. Do not share raw snapshots, identity mappings, credentials,
or the entire private run directory.

The kit has no send path. Never send or schedule messages, write CRM/payment/ad
data, create audiences, or mark revenue recovered. The owner executes approved
campaigns in their own tools.

A live read needs explicit owner authorization and scoped environment credentials.
Do not choose credentials, enable a live connection, or persist secrets yourself.
If authorization or supported input is missing, name what is needed and continue
with local work that is already authorized.
