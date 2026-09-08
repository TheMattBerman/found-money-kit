# Found Money Kit

**Find the money already sitting in your business.**

Found Money reads CRM, billing, order, appointment, proposal, or CSV evidence locally. It groups failed payments, expired trials, closed-lost deals, and lapsed customers into a ranked Money Map, then opens an offline Recovery Room with practical next steps.

It is an operator’s review tool. It does not send messages, write back to business systems, create audiences, or claim that an opportunity has been recovered.

<p align="center">
  <img src="assets/launch-room.png" alt="Recovery Room showing observed, modeled, and recorded values separately" width="900" />
</p>

## Start here

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/TheMattBerman/found-money-kit.git
cd found-money-kit
uv sync --frozen --group dev
uv run playwright install chromium
uv run found-money guide --output-root private-runs/example
```

The guided flow uses bundled fixture data for a walkthrough. It asks for the context needed to explain that fixture, builds locally, and opens `private-runs/example/index.html`. For your own exports, use `found-money build` as described below. Review Find, Evidence, Play, then Launch. Launch is an approval checklist for a human, not a send queue.

To install the agent skill for Claude Code or Codex:

```bash
./install.sh claude   # or: ./install.sh codex
bash doctor.sh
```

## Analyze your own exports

```bash
uv run found-money build \
  --config private-runs/inputs/config.json \
  --output-root private-runs/review
```

Prepare the credential-free config and normalized snapshots as described in [the file-mode walkthrough](docs/guides/FILE_MODE_WALKTHROUGH.md). Snapshot paths are relative to the config. A live HubSpot or Stripe read requires explicit authorization, environment credentials, and live-run controls. The kit never selects credentials or enables live mode for you.

## What the result means

The Money Map keeps value classes separate:

- **Observed:** a source supplied an amount and currency.
- **Modeled:** an estimate that may use qualifying history and owner-supplied assumptions such as tenure.
- **Recorded:** a source-recorded deal or pipeline value.
- **Unquantified:** the source lacks enough evidence for a defensible amount.

Totals are identified opportunity, not recovered revenue. Identity joins use exact, declared keys; ambiguous records are withheld instead of guessed. Available event families depend on the sources and fields supplied. Private run output belongs in the local, gitignored output tree. Inspect specific output files before sharing; a source-code safety scan does not certify an arbitrary private-run folder. Public projections contain aggregates and tokens only.

## Safety boundary

Found Money is read-only by design. It does not send email or SMS, schedule work, write CRM, billing, advertising, or payment data, create audiences, or persist credentials. Agents may prepare a review and campaign recommendation; a human decides whether to act in the business’s own tools.

See [the operating guide](docs/OPERATING.md) and [the Found Money skill](skills/found-money/SKILL.md).

## License

MIT. Built by [Matt Berman](https://twitter.com/themattberman).
