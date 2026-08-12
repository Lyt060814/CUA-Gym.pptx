# Agent Operating Guide

This installed copy accompanies the `pptxgym` package. For a source checkout,
the repository-root `AGENTS.md` is authoritative. The complete operator
interview is packaged at
`resources/skills/pptxgym-operator/SKILL.md`.

## Operating Contract

1. Inspect the host, existing configuration, run snapshots, source, and
   destinations before asking questions or changing files.
2. Never read, print, summarize, or commit credential values. Use native
   login, `env:NAME`, `file:PATH`, or `secret:NAME` references.
3. Configure and run `pptxgym doctor` before launching. A model smoke is
   billable and requires explicit approval.
4. Ask for deck count, `fast`/`full`/`focused` mode, harness, worker limits,
   executor, monitoring, and publication choices that are not already known.
5. Show the effective setup and exact billable command before launch.
6. Resume an interrupted run from its frozen `run.toml`; do not reselect its
   decks or restart completed stages.
7. Publish only after repeating the rollout repository, asset dataset,
   registry/ID series, task lists, push policy, and optional VM smoke choice.
8. Run only one publisher per registry. Completion requires remote asset
   fetch verification, task/list/registry commit, push, and `pptxgym verify`.
9. Do not weaken solvability, attacks, attribution, leak, or fetch gates to
   improve yield.

Start with `pptxgym setup`, `pptxgym doctor`, and `pptxgym harness list`.
Normal operation uses `pptxgym run`, `resume`, `run-status`, `logs`, `publish`,
and `verify`; stage commands are for diagnosis and targeted recovery.
