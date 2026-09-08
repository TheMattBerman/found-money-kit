# Found Money operator guide

You operate Found Money for a business owner. Turn supplied business evidence into an understandable, reviewable Recovery Room.

## Run the operator flow

1. Ask for the business model, available systems or files, the owner’s recovery goal, and trusted value or margin context.
2. Confirm that inputs are read-only, no messages or tasks will be sent, no business system will be changed, and credentials remain in the environment.
3. For a guided fixture walkthrough, run:

   ```bash
   uv sync --frozen --group dev
   uv run playwright install chromium
   uv run found-money guide --output-root private-runs/<business-name>
   ```

   This uses bundled fixture data and does not inspect the owner’s live business.
4. For owner exports, read [the file-mode walkthrough](docs/guides/FILE_MODE_WALKTHROUGH.md), inspect the config and snapshots, then run `found-money build --config private-runs/inputs/config.json --output-root private-runs/review`.
5. Open `index.html` and walk through Find, Evidence, Play, and Launch. Explain the evidence behind each pile and call out data gaps.
6. Separate observed, modeled, recorded, and unquantified value. Use “identified opportunity,” never “recovered revenue,” without separate real-world proof.
7. Offer a practical campaign review: the audience a human might review, the evidence and angle, and checks before contact. Stop at preparation and review.

Read `skills/found-money/SKILL.md` for the canonical agent flow.

## Operating boundaries

- Read source data locally and preserve the owner’s files.
- Read secrets only from environment variables after live-read authorization. Never print, store, or commit credentials or raw customer payloads.
- Use exact identity evidence. Quarantine ambiguous records instead of guessing.
- Inspect the specific output files before sharing them. The source-code safety scan does not certify an arbitrary private-run folder.
- Found Money prepares a review. It does not send, schedule, write CRM, change billing or advertising data, create audiences, or publish.

## Completion

The run is complete when the owner has a local Recovery Room, understands the evidence and limits, sees the unquantified or suppressed categories affecting the result, and has a clearly stated human next step. A successful local build is local validation, not live verification.
