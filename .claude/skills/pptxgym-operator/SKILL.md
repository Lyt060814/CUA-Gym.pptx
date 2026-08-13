---
name: pptxgym-operator
description: Configure, launch, monitor, resume, verify, and publish CUA-Gym.pptx/pptxgym task-generation runs. Use when a user asks to set up this repository, choose Claude or Codex harness routes, configure a relay, select local or HF Jobs execution, scale a batch, recover a stopped run, or publish generated tasks.
---

# Operate CUA-Gym.pptx

Read `AGENTS.md` before acting. It is the safety and credential contract; this
skill is the interaction workflow. Use the managed `pptxgym` commands for
normal operation. Use individual stage commands only for diagnosis or targeted
recovery.

## Discover First

Inspect without model calls or writes:

1. `git status --short`, installed CLI tools, disk, CPU, and office tools.
2. Existing config at `~/.config/pptxgym/config.toml` or the user-supplied path.
3. Existing `runs/*/run.toml` and `run.json` before proposing a new run.
4. Source path or manifest accessibility and configured Git/HF destinations.
5. Authentication availability, but never read or print credential contents.
   Suppress stdout and stderr from every auth-status command and report only
   available/unavailable; status output may contain masked credential or
   account identifiers.

Report what is already determined. Do not ask the user to repeat discoverable
facts.

## Choose the Operation

Classify the request as one of:

- setup only;
- configure and run a pilot or batch;
- resume an existing run;
- publish or verify completed work.

Do not turn a resume into a new run. Do not make model calls during
publish-only recovery.

## Setup Interview

Ask only missing decisions, no more than four related questions at once:

- harness: Claude or Codex, native login or relay/custom endpoint;
- stage routes: default models/efforts or owner, proposal, recipe, reconcile,
  and probe overrides;
- mode: `fast`, `full`, or `focused`;
- executor: local server by default, or HF Jobs;
- source: Zenodo10K, pinned manifest, or local deck directory;
- storage: local run root or HF results dataset;
- publishing: disabled, or exact rollout checkout/repo and asset dataset;
- capacity: deck, probe, CPU, and selection worker limits.

Explain practical consequences. `focused` uses fast ownership but enforces an
assigned advanced feature family. Harness and mode are independent.

Never ask for a token in chat. Use native login, `env:NAME`, `file:PATH`, or a
mode-0600 `credentials.toml` `secret:NAME` reference. If interactive login is
needed, ask the user to perform it privately and then verify only success.

## Prepare Setup

1. Show the effective choices, publish destinations, and any billable or
   destructive action before changing files.
2. Install with `uv sync --extra dev --extra corpus`. Explain and request
   permission before a root-level bootstrap.
3. Create or update config through `pptxgym setup` and TOML, preserving secrets
   separately.
4. Run `pptxgym doctor` without `--smoke`; fix required failures.
5. Show `pptxgym harness list`. Make a real pipeline harness smoke call only
   after explicit consent because it is billable. Do not describe the outer
   setup agent itself as "no model call"; report that no *pipeline harness*
   call was made.
6. Do not launch a batch until requested.

For schema details read `docs/configuration.md`. For HF execution read
`docs/hf-jobs.md`.

## Run Interview

Resolve deck count, mode, worker pools, run name, executor, detached/foreground
execution, monitoring, and whether publication follows packaging. Recommend a
3-10 deck pilot for any uncalibrated harness, endpoint, model, or focused
family. Do not claim concurrency is safe without evidence.

Show one compact run brief and the exact command, then launch after approval:

```bash
uv run pptxgym run --mode <mode> --count <N> --workers <workers> --detach
uv run pptxgym run-status runs/<name>
uv run pptxgym logs runs/<name> --follow
```

For a detached run, install an external/background monitor. Watch sustained
429s, authentication errors, platform exits, disk pressure, parked decks,
probe failures, and publish errors. A monitor must surface terminal/error state;
an interactive chat turn is not a reliable monitor.

## Resume

Inspect the run snapshot and checkpoint evidence, then resume the same run:

```bash
uv run pptxgym resume runs/<name> --detach
```

Completed stages are file-backed and fingerprinted. Confirm resume is using the
same run/decks; do not silently reselect or restart finished stages.

## Publish

Before publishing, repeat the exact rollout repository/checkout, assets
dataset, registry and ID series, task lists, push policy, and optional AWS smoke
choice. Only one publisher may update a registry at a time.

```bash
uv run pptxgym publish runs/<name>
uv run pptxgym verify runs/<name>
```

Completion requires uploaded assets, remote fetch verification, task files,
attribution, registry/list updates, successful Git push when configured, and a
clean `verify` result. Report both destinations and counts.
