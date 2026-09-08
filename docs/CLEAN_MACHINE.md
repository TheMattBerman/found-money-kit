# Setup and troubleshooting

Use Python 3.11 or newer and [uv](https://docs.astral.sh/uv/). Start in the directory where you want to keep the kit.

```bash
git clone https://github.com/TheMattBerman/found-money-kit.git
cd found-money-kit
uv sync --frozen --group dev
uv run playwright install chromium
bash doctor.sh
```

Chromium is used locally to render and check the Recovery Room and printable report. On Linux, install its system dependencies with `uv run playwright install --with-deps chromium` if the browser reports missing libraries. Dependency installation downloads software; analyzing local files does not require a live business connection.

## Use an agent

Run `./install.sh claude` or `./install.sh codex` to install the Found Money skill for that agent. Then ask the agent to help you run the kit. The [operator guide](OPERATING.md) explains the workflow. The interactive `found-money guide` command uses bundled example records by default; use the [file-mode walkthrough](guides/FILE_MODE_WALKTHROUGH.md) for your own exports.

## Common problems

- **Python is too old:** select an existing Python 3.11+ interpreter and repeat the dependency installation.
- **Browser executable is missing:** run `uv run playwright install chromium` in this repository.
- **Doctor fails:** read the first failing check. Doctor diagnoses the environment; it does not install or repair it.
- **Output directory is rejected:** choose a new directory under `private-runs/`. Keep source files and results separate.
- **Snapshot schema is rejected:** a raw platform export may need normalization. Check the schemas in the file-mode walkthrough; changing a filename does not normalize data.
- **No campaign is available:** inspect the named missing evidence or profile input. The kit can report an opportunity while withholding strategy that lacks support.
- **An opportunity has no amount:** the source lacks a supported value. Keep it unquantified until the owner supplies usable evidence.

Keep credentials in the environment and customer files under ignored local directories. Do not include either in support issues. Share the error message with identifying details removed, the command shape, and your Python version.
