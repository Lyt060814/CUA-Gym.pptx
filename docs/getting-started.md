# Getting Started

To have a coding agent perform discovery, ask the setup questions, and prepare
this configuration safely, use [Agent Quick Start](agent-quickstart.md).

## 1. Install

Use Linux with Python 3.10 or newer. The pipeline requires `soffice` and
`pdftoppm`; a model harness requires either `claude` or `codex`.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv tool install 'pptxgym[corpus]'
pptxgym --help
```

The `corpus` extra supports the default Zenodo10K source. A virtual environment
with `pip install 'pptxgym[corpus]'` is also supported. For development
from a source checkout, use `uv sync --extra dev --extra corpus` and prefix
commands with `uv run`.

For a bare Ubuntu machine, clone the repository, inspect
`image/bootstrap.sh`, and run it as root first. It installs the pinned office
runtime used by the container executor.

## 2. Authenticate

Native harnesses use their normal credential stores:

```bash
claude auth login
# or
codex login
```

For a relay, configure the endpoint and refer to a credential supplied through
the environment, a protected file, or `credentials.toml`. A reference keeps
the secret out of shell history and process listings.

```bash
export RELAY_API_KEY=...  # enter this privately in your terminal
pptxgym setup --harness codex \
  --base-url https://relay.example/v1 \
  --api-key-ref env:RELAY_API_KEY
```

Interactive setup can instead prompt with hidden input and save the value in
the mode-`0600` `credentials.toml`. The legacy literal `--api-key` option is
supported for automation compatibility but is not recommended.

## 3. Configure

```bash
pptxgym setup --harness claude
pptxgym doctor
pptxgym doctor --smoke   # one real, billable model call
```

Edit `~/.config/pptxgym/config.toml` for stage-specific models, endpoints,
worker limits, storage, and publishing. Confirm the effective routing with:

```bash
pptxgym harness list
pptxgym harness test
```

## 4. Run a Pilot

Start with 3-10 decks before scaling a new harness or relay.

```bash
pptxgym run --mode fast --count 5 --name pilot --detach
pptxgym run-status runs/pilot
pptxgym logs runs/pilot --follow
```

Inspect `runs/pilot/run.toml`, `events.jsonl`, `runner.log`, and
`work/deck*/state.json`. The run snapshot is the source of truth for resume.

## 5. Scale

After the pilot has acceptable yield and no sustained 429s:

```bash
pptxgym run --mode fast --count 50 --workers 10 --detach
```

The default worker count is not the deck count. `auto` respects the harness's
configured capacity; `--workers all` is an explicit stress-test setting.

## 6. Publish

Configure the rollout and assets targets before enabling publish. First use a
small run and verify both sides:

```bash
pptxgym publish runs/pilot
pptxgym verify runs/pilot
```

Publishing does not call the model. It can run while another generation batch
uses the harness, but two publishers must not share one ID registry at once.
