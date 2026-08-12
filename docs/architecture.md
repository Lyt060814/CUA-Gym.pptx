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
