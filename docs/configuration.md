# Configuration

`pptxgym` merges built-in defaults with one TOML file. CLI flags override the
loaded file for a run, and the resolved result is frozen into the run's
`run.toml`. Resume always uses that snapshot.

## Modes

- `fast`: owner-authored proposal, recipe, and reconciliation through the same
  deterministic checkers; sealed solvability probe remains independent.
- `full`: independent proposal, recipe, reconcile, and probe specialists.
- `focused`: fast execution plus balanced advanced-feature selection and hard
  recipe constraints for animation, equation, chart, or effects.

## Harnesses, Connections, and Routes

A harness is the CLI contract (`claude` or `codex`). A connection defines how
that CLI reaches a model. A route assigns a harness, model, effort, and optional
connection override to a stage.

```toml
[harnesses.main]
type = "codex"
connection = "relay"
max_concurrency = 10
probe_concurrency = 2

[connections.relay]
kind = "relay"
base_url = "https://relay.example/v1"
wire_api = "responses"
auth = "secret:codex_relay_key"

[routes.owner]
harness = "main"
model = "gpt-5.6-terra"
effort = "high"
```

Valid credential references are:

- `credential-store`: normal `claude auth login` or `codex login` state.
- `env:NAME`: read the named environment variable.
- `secret:NAME`: read `[secrets].NAME` from `credentials.toml`.
- `file:PATH`: read a token from a file.
- `none`: no authentication.

Each connection receives an independent runtime secret variable, so routes can
use different relay keys without overwriting one another.

## Concurrency

```toml
[concurrency]
deck_workers = "auto"
probe_workers = "auto"
cpu_workers = "auto"
selection_workers = "auto"
```

Each value accepts `auto`, `all`, or a positive integer. `auto` caps model
concurrency at `harnesses.<name>.max_concurrency`; it also applies separate
probe and CPU limits. Relay burst limits are usually lower than the number of
long-lived owner processes, so calibrate with a pilot rather than assuming
one worker per deck is safe.

## Sources and Licensing

### Zenodo10K

```toml
[source]
type = "zenodo10k"
repo = "Forceless/Zenodo10K"
min_score = 50.0
scan = 0
```

The selector filters licenses, deduplicates, triages, verifies a complete
render, and freezes the winning rows in a manifest.

### Pinned Manifest

```toml
[source]
type = "manifest"
manifest = "/data/batch.json"
```

Rows require `name`, `url`, and `sha256`. Publishing also requires `license`
and should include `title`, `doi` or `record`, `source`, and `corpus`.

### Local Directory

```toml
[source]
type = "local"
path = "/data/decks"
manifest = "/data/decks/provenance.json"
```

The run copies the first N files in stable relative-path order into its frozen
input directory. The manifest is optional for generation and mandatory for
publishing. Its rows are joined by original filename, never list position.

## Execution and Storage

```toml
[execution]
executor = "local"       # or "hf-jobs"
work_root = "./runs"
detach = true
timeout_minutes = 90
max_turns = 260
min_free_disk_gib = 20
wps = "auto"             # auto, on, off

[storage]
type = "local"           # use hf for HF Jobs/checkpoint sharing
root = "./runs"
results_repo = ""
checkpoint_minutes = 10
```

`wps = "auto"` uses WPS when `wpp` is installed and records `--no-wps`
otherwise. `on` makes a missing WPS installation a doctor failure.
`doctor` refuses a launch host below `min_free_disk_gib`; 20 GiB is the safe
default for real batches. A small packaging-only CI smoke may lower it
explicitly, but production runs should size it for deck count and source size.

## Publishing

Publishing defaults to disabled and has no default namespace.

```toml
[publish]
enabled = false
rollout_repo = "owner/rollout"
rollout_checkout = ""
assets_repo = "owner/task-assets"
assets_private = true
registry = "evaluation_examples/task_assets/pptxgym-ids.json"
task_class_dir = "evaluation_examples/task_class"
task_assets_dir = "evaluation_examples/task_assets"
task_lists = [
  "evaluation_examples/test_pptxgym.json",
  "evaluation_examples/test_cua_scaling.json",
]
series = "110"
series_first = 1100001
series_last = 1109999
push = true
aws_verify = false
aws_osworld = ""
aws_uv = ""
aws_workers = 4
hf_workers = 4
aws_attempts = 3
aws_instance_type = ""
aws_region = ""
```

New asset datasets are private by default. Set `assets_private = false` only
when the task materials are intentionally public. Existing dataset visibility
is never changed by this setting.

`rollout_checkout` can point to an existing clean clone and avoids creating a
per-run clone. Otherwise `rollout_repo` is cloned with temporary credential
handling; tokens are not written into the remote URL.

## Hugging Face Jobs

```toml
[executors.hf-jobs]
flavor = "cpu-performance"
timeout = "8h"
repo = "owner/pptxgym"
revision = "main"
osworld_repo = "owner/osworld"
```

The results dataset and source repository must already exist and be writable by
the submitted credentials. See [HF Jobs](hf-jobs.md).
