# Hugging Face Jobs

HF Jobs is the recommended disposable executor when a persistent Linux server
is unavailable. Local execution remains the default.

## Configure

```toml
[execution]
executor = "hf-jobs"
work_root = "./runs"

[storage]
type = "hf"
results_repo = "owner/pptxgym-runs"

[executors.hf-jobs]
flavor = "cpu-performance"
timeout = "8h"
repo = "owner/pptxgym"
revision = "main"
osworld_repo = "owner/osworld"
```

Authenticate locally with `hf auth login` and `gh auth login`. Native Claude
routes also need `CLAUDE_CODE_OAUTH_TOKEN` for transfer into the disposable
container. Native Codex routes transfer the local Codex credential store.
Relay routes use the configured secret references instead.

Run `pptxgym doctor` before submission.

## Launch

```bash
pptxgym run --mode fast --count 50 --workers 10 --detach
pptxgym run-status runs/<name>
pptxgym logs runs/<name> --follow
```

The job:

1. installs the pinned office/CLI runtime;
2. selects decks or fetches the frozen manifest;
3. runs foreman with the configured routes and worker pools;
4. uploads alternating verified checkpoints;
5. writes calibration data and a final archive;
6. publishes only when enabled.

## Resume and Publish

```bash
pptxgym resume runs/<name>
pptxgym publish runs/<name>
```

Resume selects the newest valid archive by Hub commit time. `publish` submits a
publish-only restore job and performs no model calls.

## Constraints

- `source.type = "local"` is not available to a remote job. Upload a manifest
  and its source URLs or run locally.
- A local `source.manifest` is validated and uploaded to the results dataset
  automatically. A non-local value is interpreted as an existing path in that
  dataset.
- The pipeline and results repositories must exist before submission.
- The source revision should be a commit SHA for reproducible production runs.
- HF repository commit-rate limits apply to checkpoints and asset uploads.
- WPS round-trip verification is currently recorded as unavailable in this
  container path; deterministic gates and URL verification still run.

Development history and the measurements behind this executor live in
[`history/hf-jobs-development.md`](history/hf-jobs-development.md).
