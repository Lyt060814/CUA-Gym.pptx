# Architecture

Each deck has one owner. The owner chooses and repeats stage verbs; Python owns
state, validation, fingerprints, rendering, scoring, attacks, and packaging.
Stages communicate only through files, so every run can resume after process or
machine loss.

```text
ingest -> inspect -> propose -> recipe -> degrade -> materialise
       -> reconcile -> solvable -> score -> harden -> package
```

`fast` allows the owner to write proposal, recipe, and reconciliation records,
but only the `adopt` command can stamp them and it runs the same checker as a
specialist. The sealed solvability probe is never adoptable.

`full` assigns all four judgement stages to independent specialist sessions.

`focused` uses the fast ownership model. Deck selection assigns one advanced
feature family, the prompt must declare it, and the recipe checker requires the
corresponding native operator. Soft mentions of animation, equations, charts,
or effects are insufficient.

The harness layer is provider-neutral at the pipeline boundary. Agent specs
resolve to Claude or Codex CLI commands only at process launch. Packaged agent
manuals and skills are passed explicitly, so an installed wheel does not depend
on a source checkout's `.claude/` discovery behavior.

Publishing is a separate transaction over two stores: assets in a dataset and
task code/registry in Git. Assets must be fetch-verified before task files are
written. Registry checksum mapping makes retries idempotent.

## Package Layout

The package is organized by ownership boundary rather than pipeline order:

| Package | Owns |
| --- | --- |
| `pptxgym/commands` | CLI parsing and operator-facing commands |
| `pptxgym/management` | Config, setup, launch, resume, and verification |
| `pptxgym/core` | Deck state, stage contracts, fingerprints, and gates |
| `pptxgym/office` | PPTX/OOXML inspection, rendering, and deterministic edits |
| `pptxgym/evaluation` | Inventory, comparators, consistency, and attacks |
| `pptxgym/tasks` | Reference assets, evaluator emission, and generated tests |
| `pptxgym/orchestration` | Harness routing, owners, specialists, and monitoring |
| `pptxgym/delivery` | Corpus selection, publication, and VM smoke checks |

Dependencies flow toward deterministic lower layers. Office code does not
launch models or publish; evaluation code does not own run scheduling; delivery
does not decide task quality. `core.pipeline` is the integration boundary that
connects stage contracts to these domains.

`pptxgym/` itself contains no implementation modules. Library code imports the
domain path, for example `pptxgym.evaluation.comparators`; operators normally
use the `pptxgym` console command. Low-level module entry points use their full
domain path, such as `python -m pptxgym.delivery.corpus`.

## Internal Boundaries

The historical entry modules remain compatibility facades, while independent
mechanisms live behind them:

- `core.pipeline` integrates deck state and stages; ingestion, run logging, and
  code fingerprints are separate modules.
- `delivery.publish` owns the transaction; dataset I/O, Git, registry layout,
  and attribution are separate publication modules.
- `evaluation.attacks` owns attack execution and registration; attack records,
  deterministic OOXML mutations, and reporting are separate modules.
- `evaluation.comparators` owns operator comparators and score policy; shape
  matching and plan construction are separate modules.
- `orchestration.agent` owns harness execution; prompts and the mechanical
  solvability rubric are separate modules.
- Shared package and shape traversal code lives under `office.ooxml`; WPS
  process identity and receipt handling live in `office.wps_process`.

Some facade modules remain larger than ordinary modules because their remaining
functions share one registry or state contract. In particular, splitting the
comparator registry from score gates would create a circular dependency or a
second implementation of scored facets. Further splits should follow tested
semantic boundaries rather than line count. Compatibility facades must retain
their historical names, and generated evaluators must remain self-contained.

## Repository Layout

- `configs/`: safe example configurations; never credentials.
- `docs/design/`: current design rationale and measured constraints.
- `docs/reference/`: operator and maintenance references.
- `docs/validation/`: reproducible validation evidence.
- `docs/history/`: investigations retained for provenance, not current setup.
- `image/`: server/bootstrap and HF Jobs execution scripts.
- `pptxgym/resources/`: manuals, skills, and executor scripts packaged in the wheel.
- `tests/`: unit, integration, packaging, and compatibility tests.

Runtime data belongs under the configured run root. Corpus downloads, run
checkpoints, generated task assets, and local shortlists are not repository
source and must remain ignored.
