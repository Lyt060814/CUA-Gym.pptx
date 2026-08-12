# Operations

## Run State

A managed local run contains:

- `run.toml`: frozen configuration and resolved workers.
- `run.json`: executor status and submitted job IDs.
- `events.jsonl`: append-only control-plane events.
- `runner.log`: local detached process output.
- `decks/`: frozen source files and selection manifest.
- `work/deckNNNN/`: content-addressed stage state and evidence.
- `publish.json`: publish verification and commit result.

## Monitor

```bash
pptxgym run-status runs/<name>
pptxgym logs runs/<name> --follow
```

For HF Jobs these commands call `hf jobs inspect` and `hf jobs logs` using the
recorded job ID. A run may record several job IDs after resumes or a separate
publish-only retry.

## Resume

```bash
pptxgym resume runs/<name> --detach
```

Local resume passes no source files to foreman. It reads the existing work
tree, recovers archived successful stage outputs, re-verifies shipped decks,
and only starts an owner when work remains.

HF resume restores the newest valid `resume-a`, `resume-b`, or final archive,
then runs the same state-aware foreman. Alternating checkpoints protect against
a corrupt or empty latest upload.

## Publish and Retry

```bash
pptxgym publish runs/<name>
```

Local runs publish directly. HF runs submit a publish-only restore job: no
selection, foreman, or model calls. Publishing is idempotent by source checksum
and registry state.

The publish gate:

1. selects only mechanically shipped tasks;
2. refuses answer-key leakage;
3. allocates IDs in the configured series;
4. uploads assets in bounded commits;
5. fetch-verifies every declared file;
6. writes task code, attribution, metadata, and tests;
7. refreshes every configured task list;
8. commits and optionally pushes the rollout checkout.

Run one publisher per registry. Another contributor moving the Git branch is
handled by pull/rebase/retry; two simultaneous registry allocators are not.

## Verify

```bash
pptxgym verify runs/<name>
```

This checks local shipped evidence and the recorded publish verification. The
publisher itself performs the authoritative remote byte-fetch check before it
writes task files.

## Failure Triage

- Sustained `429`: lower `deck_workers` and `probe_workers`; resume the same
  run after the rate window clears.
- Harness auth failure: run `pptxgym harness test`, refresh the native login or
  credential reference, then resume.
- Platform termination: resume from persistent local work or the newest HF
  archive. Do not create a new run name for the same manifest.
- Publish failure: fix target auth or branch state, then run `pptxgym publish`.
  It does not spend model tokens.
- Low disk: remove selection scratch and completed run staging directories only
  after preserving the run work/archive needed for recovery.

## Stage-Level Recovery

Managed commands are the normal interface. For a targeted repair, stage verbs
remain available:

```bash
pptxgym --work runs/<name>/work status --all
pptxgym --work runs/<name>/work blocked
pptxgym --work runs/<name>/work recipe --deck deck0007 --force
```

Stage state includes fingerprints of every input. Editing an upstream artefact
makes dependent success records stale and forces the appropriate tail to run
again.
