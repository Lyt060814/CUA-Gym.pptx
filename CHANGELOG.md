# Changelog

## 0.9.0

- Added proprietary release metadata, packaged agent/operator guides, and a
  wheel-level release smoke test.
- Added `pptxgym guide` and safe relay credential references through
  `pptxgym setup --api-key-ref` while retaining the legacy setup interface.
- Fixed managed `publish --dry-run` so it renders the publication plan without
  uploading assets, changing the registry, writing a ledger, or committing.
- Made newly created managed asset datasets private by default, with an
  explicit `publish.assets_private = false` opt-out. Existing visibility is
  not changed.
- Added managed `setup`, `doctor`, `run`, `resume`, `run-status`, `logs`,
  `publish`, `verify`, and `harness` commands.
- Added versioned TOML configuration with separate credentials, stage-specific
  Claude/Codex routing, relay endpoints, and conservative automatic workers.
- Added local-first and optional HF Jobs executors with reproducible snapshots,
  checkpoint recovery, and publish-only retries.
- Made source, storage, rollout schema, asset repository, task lists, and ID
  series configurable.
- Packaged agent manuals and skills so installed wheels do not depend on a
  source checkout's `.claude/` directory.
- Added the focused scaling mode for animation, equation, native chart, and
  effects coverage.
- Added an agent-first quick start with guided setup, run, resume, publishing,
  credential, and confirmation rules shared by Codex and Claude Code.
- Organized the Python package into command, management, core, Office,
  evaluation, task, orchestration, and delivery domains. The `pptxgym` console
  command is unchanged; pre-1.0 flat Python imports and `python -m
  pptxgym.<tool>` aliases were removed in favor of canonical domain paths.
- Moved design, operator, validation, and project-maintenance documents out of
  the repository root and documented the supported package boundaries.
