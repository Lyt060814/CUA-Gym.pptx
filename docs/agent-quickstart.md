# Agent Quick Start

Use this path when a coding agent, rather than the operator, will prepare and
run `pptxgym`. The repository-level operating contract is [`AGENTS.md`](../AGENTS.md),
and the discoverable operator workflow is
[`pptxgym-operator`](../.agents/skills/pptxgym-operator/SKILL.md).

## Configure This Repository

Open the repository in a coding agent and send:

```text
Read AGENTS.md in full and help me set up pptxgym.

First inspect this machine, the repository, any existing pptxgym config, and
available authentication without reading or printing secret values. Tell me
what is already configured. Then ask me only the missing setup decisions in
small groups: harness and model routes, local or HF execution, deck source,
storage, concurrency limits, and whether/where publishing should be enabled.

Do not ask me to paste tokens into chat. Use native login, an environment
variable, a protected file, or credentials.toml. Before changing anything,
show me the proposed effective configuration and any billable or destructive
steps. After I confirm, perform setup, run the non-billable doctor checks, fix
what can be fixed safely, and report anything I must do manually. Do not launch
a task-generation run until I ask for one.
```

The agent should finish with:

- a validated `config.toml` and separate credential references;
- a clear harness/model/effort routing table;
- confirmed source, storage, executor, and publish destinations;
- `pptxgym doctor` passing, apart from explicitly accepted optional checks;
- no generation or publication performed implicitly.

## Run a Batch

For a configured checkout, send:

```text
Read AGENTS.md and help me run a pptxgym batch.

Inspect the current config and existing run snapshots first. Ask only what is
not already determined: deck count, fast/full/focused mode, worker limits, run
name, executor, detach/monitoring preference, and whether successful tasks
should be published. Explain the cost, capacity, and recovery implications of
the choices. Recommend a small pilot if this harness, endpoint, model, or
focused task family has not been calibrated.

Before launch, show me one compact run brief and the exact command. Launch only
after I confirm. Monitor errors and rate limits without restarting completed
work. If the executor stops, resume the same run. If publishing is enabled,
repeat the exact rollout and asset targets before publishing, serialize access
to the registry, and verify both destinations at the end.
```

## Resume or Publish Existing Work

Use:

```text
Read AGENTS.md. Inspect the run at <run-path> and determine whether it should
be resumed, published, or only verified. Preserve all completed stage state.
Explain the evidence and proposed command before acting. Do not make model
calls during a publish-only retry, and do not publish until you have repeated
the exact destinations and I have confirmed them.
```

## What the Agent Should Ask

Questions are conditional, not a fixed form. A new local Claude setup with
Zenodo and no publishing may need only harness, source, and worker decisions.
An HF relay run with publication also needs endpoint/auth reference, results
dataset, pipeline revision, rollout repository, asset dataset, ID range, task
lists, and optional VM verification.

The agent should ask two to four related questions at a time and should reuse
valid existing settings. It should never ask again for values it can safely
discover.
