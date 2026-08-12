# Changelog

## 0.9.0

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
