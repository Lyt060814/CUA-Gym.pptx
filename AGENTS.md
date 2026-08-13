# Agent Operating Guide

This repository is `CUA-Gym.pptx`; the Python package and CLI are named
`pptxgym`. Use the managed CLI for normal setup and operation. Individual
stage commands exist for diagnosis and targeted recovery, not as the default
user experience.

When the request is to configure, run, resume, verify, or publish this system,
follow [`.agents/skills/pptxgym-operator/SKILL.md`](.agents/skills/pptxgym-operator/SKILL.md).
It provides the setup/run interview and command sequence; this file remains
the authoritative safety and credential contract.

## What This System Does

`pptxgym` turns real `.pptx` decks into validated computer-use tasks:

```text
select -> inspect -> propose -> recipe -> degrade -> materialise
       -> reconcile -> solvable -> score -> harden -> package -> publish
```

Model judgement is limited to proposal, recipe, reconciliation, and the sealed
solvability probe. Damage, scoring, attacks, packaging, publishing, and state
fingerprints are deterministic. Each deck owns file-backed state under
`work/deckNNNN/`, so an interrupted run should be resumed, not restarted.

## Supported Choices

Modes:

- `fast`: one owner authors proposal, recipe, and reconciliation; the probe is
  independent. This is the normal scaling mode.
- `full`: separate specialists author each judgement stage. Use when review
  independence matters more than cost.
- `focused`: fast ownership and similar token usage, plus balanced selection
  and hard recipe constraints for animation, equation, native chart, and
  effects tasks.

Harnesses:

- `claude`: drive agents through Claude Code.
- `codex`: drive agents through Codex CLI.

Executors:

- `local`: default; run on the current Linux server.
- `hf-jobs`: optional remote executor with checkpoint archives and resume.

Harness and mode are independent. Routes may mix harnesses and may assign a
different model, effort, endpoint, connection, and credential to owner,
proposal, recipe, reconcile, and probe.

## Interaction Contract

When a user asks for help setting up or running this repository:

1. Inspect first. Do not ask for facts that can be read safely from the host,
   repository, existing config, or run snapshots.
2. State what is already known and what remains undecided.
3. Ask only the missing decisions, in groups of at most four related questions.
   Explain the practical consequence of each choice.
4. Restate the resulting plan, including destinations and any billable or
   destructive action, before executing it.
5. Configure and validate setup before launching a generation run.
6. Never publish unless the user explicitly chose publishing and the exact
   rollout and asset destinations are known.

Do not turn discovery into an interrogation. For example, if a valid config
already names Codex, a local source, ten workers, and publishing disabled, ask
whether those choices should be kept instead of asking for all four again.

## Safe Discovery

Start with read-only checks:

```bash
git status --short
uv run pptxgym --help
uv run pptxgym harness list --config <config>     # when config exists
uv run pptxgym doctor --config <config>           # no model call
```

Also inspect:

- `~/.config/pptxgym/config.toml`, if present;
- installed `claude`, `codex`, `soffice`, `pdftoppm`, `wpp`, `hf`, and `gh`;
- available disk and CPU;
- source directory or manifest accessibility;
- existing `runs/*/run.toml` and `run.json` before proposing a new run;
- git remotes and target repository state before publishing.

Do not read, print, summarize, or commit credential file contents during
discovery. Authentication-status commands must have both stdout and stderr
suppressed; report only available/unavailable. Some CLIs include masked token
fragments or account identifiers even in status output. For example:

```bash
if codex login status >/dev/null 2>&1; then
  echo "Codex authentication available"
else
  echo "Codex authentication unavailable"
fi
```

## Setup Interview

Resolve these decisions before setup. Ask only those not already answered.

| Area | Required decision |
| --- | --- |
| Goal | Configure only, configure and pilot, resume, or publish an existing run |
| Harness | Claude or Codex; native login or relay/custom endpoint |
| Models | Keep defaults or override owner/proposal/recipe/reconcile/probe model and effort |
| Executor | Local server or HF Jobs |
| Source | Zenodo10K, pinned manifest, or local deck directory |
| Storage | Local run directory or HF results dataset/checkpoints |
| Publishing | Disabled, or exact rollout repo/checkout plus exact asset dataset |
| Validation | WPS policy and whether to make a real billable harness smoke call |

Conditional details:

- A relay needs base URL, wire API, and a credential reference.
- HF Jobs needs a writable results dataset, pipeline repository and immutable
  revision, flavor, timeout, and suitable HF/GitHub authentication.
- Publishing needs registry path, ID series/range, task lists, push policy, and
  optional AWS smoke settings.
- A local source may generate without provenance, but publishing requires a
  filename-keyed provenance manifest with redistributable licensing.

## Credential Rules

Never ask the user to paste a token into chat. Never put a literal secret in
`config.toml`, `run.toml`, a command shown in chat, a git remote URL, or a
commit.

Use one of these mechanisms:

- native `claude auth login`, `codex login`, `hf auth login`, or `gh auth login`;
- `env:NAME` in config, with the user setting the environment privately;
- `file:PATH` in config;
- `secret:NAME` backed by mode-`0600` `credentials.toml`.

If interactive authentication requires a browser or secret entry, ask the
user to perform that step in their terminal, then verify only that it works.
`pptxgym doctor --smoke` makes a real, potentially billable pipeline harness
call, so run it only after explicit consent. In status reports, distinguish
pipeline harness calls from the outer coding agent that is performing setup;
do not claim that no model call occurred when only the pipeline remained idle.

## Setup Procedure

1. Install prerequisites with `uv sync --extra dev --extra corpus`. On a bare
   Ubuntu host, explain what `image/bootstrap.sh` installs before requesting
   permission to run it with root privileges.
2. Create the base config with `pptxgym setup`; use `--non-interactive` only
   after all answers are known.
3. Apply stage routes, worker limits, source, storage, executor, and publish
   settings in TOML. Keep credentials separate.
4. Run `pptxgym doctor` without `--smoke` and fix every failed prerequisite.
5. Run `pptxgym harness list`, then `pptxgym harness test` or doctor smoke only
   when the user approved the billable call.
6. Show the user the effective routes, executor, source, storage, publish
   targets, and worker ceilings. Do not launch a batch unless requested.

Examples live in `configs/`; the complete schema is documented in
`docs/configuration.md`.

## Run Interview

Before a new run, resolve:

| Area | Required decision |
| --- | --- |
| Scope | Deck count and optional run name |
| Mode | `fast`, `full`, or `focused` |
| Capacity | Deck, probe, CPU, and selection workers |
| Execution | Local or HF Jobs, foreground or detached |
| Publication | Generate only, or publish after successful packaging |
| Monitoring | Expected completion window and how status will be reported |

Recommend a 3-10 deck pilot for a new harness, endpoint, model, or focused
recipe family. Do not claim a concurrency value is safe without measurements;
relay burst limits can be lower than the number of long-lived deck owners.

Before execution, show the exact command and a compact run brief. Then use:

```bash
uv run pptxgym run --mode <mode> --count <N> --workers <workers> --detach
uv run pptxgym run-status runs/<name>
uv run pptxgym logs runs/<name> --follow
```

Monitor for sustained 429s, authentication failures, platform termination,
disk pressure, parked decks, and publish errors. Prefer a detached run plus an
external/background monitor over keeping an interactive agent turn alive.

## Resume and Publish

For an interrupted run, inspect its snapshot and resume the same run:

```bash
uv run pptxgym resume runs/<name> --detach
```

Do not create a fresh run over the same decks merely because the executor
stopped. Stage fingerprints and checkpoints exist to preserve completed work.

Publishing is opt-in and serialized per ID registry:

```bash
uv run pptxgym publish runs/<name>
uv run pptxgym verify runs/<name>
```

Before publish, repeat the exact rollout repository/checkout, asset dataset,
ID series, task lists, push policy, and AWS verification choice. Publishing
does not require model calls and may be retried separately. A successful
completion means asset upload, remote fetch verification, task/list/registry
commit, push when configured, and `pptxgym verify` all agree.

## Boundaries

- Do not change deterministic gates to raise yield without evidence.
- Do not bypass solvability, attack, provenance, attribution, or fetch checks.
- Do not edit generated task IDs by hand; use the registry allocator.
- Do not run two publishers against one registry concurrently.
- Do not overwrite an existing config, run, remote, or user change without
  explicit confirmation.
- Do not restart completed stages; use resume and the recorded fingerprints.
- Do not expose answer keys or make result repositories public without an
  explicit user decision.

For architecture and repository layout read `docs/architecture.md`; for
operational recovery read `docs/operations.md`; for HF execution read
`docs/hf-jobs.md`.
