# Getting Started

To have a coding agent perform discovery, ask the setup questions, and prepare
this configuration safely, use [Agent Quick Start](agent-quickstart.md).

## 1. Install

Use Linux with Python 3.10 or newer. The pipeline requires `soffice` and
`pdftoppm`; a model harness requires either `claude` or `codex`.

```bash
uv sync --extra dev --extra corpus
uv run pptxgym --help
```

For a bare Ubuntu machine, inspect and run `image/bootstrap.sh` as root first.
It installs the pinned office runtime used by the container executor.

## 2. Authenticate

Native harnesses use their normal credential stores:

```bash
claude auth login
# or
codex login
```

For a relay, create the config with `--base-url` and `--api-key`. The key is
written only to `credentials.toml`.

```bash
uv run pptxgym setup --harness codex \
  --base-url https://relay.example/v1 --api-key "$RELAY_KEY"
```

## 3. Configure

```bash
uv run pptxgym setup --harness claude
uv run pptxgym doctor
uv run pptxgym doctor --smoke   # one real, billable model call
```

Edit `~/.config/pptxgym/config.toml` for stage-specific models, endpoints,
worker limits, storage, and publishing. Confirm the effective routing with:

```bash
uv run pptxgym harness list
uv run pptxgym harness test
```

## 4. Run a Pilot

Start with 3-10 decks before scaling a new harness or relay.

```bash
uv run pptxgym run --mode fast --count 5 --name pilot --detach
uv run pptxgym run-status runs/pilot
uv run pptxgym logs runs/pilot --follow
```

Inspect `runs/pilot/run.toml`, `events.jsonl`, `runner.log`, and
`work/deck*/state.json`. The run snapshot is the source of truth for resume.

## 5. Scale

After the pilot has acceptable yield and no sustained 429s:

```bash
uv run pptxgym run --mode fast --count 50 --workers 10 --detach
```

The default worker count is not the deck count. `auto` respects the harness's
configured capacity; `--workers all` is an explicit stress-test setting.

## 6. Publish

Configure the rollout and assets targets before enabling publish. First use a
small run and verify both sides:

```bash
uv run pptxgym publish runs/pilot
uv run pptxgym verify runs/pilot
```

Publishing does not call the model. It can run while another generation batch
uses the harness, but two publishers must not share one ID registry at once.
