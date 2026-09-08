# Found Money operator guide

Operate the kit for the business owner. Read [AGENTS.md](AGENTS.md), then [docs/OPERATING.md](docs/OPERATING.md) for evidence and safety rules.

Ask for the business model, available exports or authorized sources, the recovery goal, and value context. The guided command uses bundled fixture data:

```bash
uv sync --frozen --group dev
uv run playwright install chromium
uv run found-money guide --output-root private-runs/<business-name>
```

For owner exports, read [the file-mode walkthrough](docs/guides/FILE_MODE_WALKTHROUGH.md), inspect the source declarations, then run `found-money build --config private-runs/inputs/config.json --output-root private-runs/review`. Open the Recovery Room and explain Find, Evidence, Play, and Launch. Separate observed, modeled, recorded, and unquantified value. Ground recommendations in supplied evidence and offer a practical human campaign review.

Keep inputs read-only. Never send, schedule, write CRM/payment/ad data, create audiences, persist credentials, or claim recovered revenue. A live HubSpot or Stripe read requires explicit authorization and environment credentials; do not enable live mode yourself. Inspect each artifact before sharing; a source-code safety scan does not certify an arbitrary private-run folder.
