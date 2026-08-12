#!/usr/bin/env bash
set -euo pipefail

repo=$(cd "$(dirname "$0")/.." && pwd)
cd "$repo"

shopt -s nullglob
wheels=(dist/pptxgym-*.whl)
if [ "${#wheels[@]}" -ne 1 ]; then
  printf 'expected exactly one wheel under dist/, found %s\n' "${#wheels[@]}" >&2
  exit 2
fi

root=$(mktemp -d "${TMPDIR:-/tmp}/pptxgym-release-smoke.XXXXXX")
uv venv "$root/venv" >/dev/null
uv pip install --python "$root/venv/bin/python" "${wheels[0]}" >/dev/null

mkdir -p "$root/home" "$root/bin" "$root/decks" "$root/runs"
printf 'release smoke input\n' > "$root/decks/sample.pptx"
for command in git soffice pdftoppm codex; do
  printf '#!/bin/sh\nexit 0\n' > "$root/bin/$command"
  chmod 755 "$root/bin/$command"
done

export HOME="$root/home"
export PATH="$root/bin:$PATH"
export PPTXGYM_RELEASE_SMOKE_KEY="release-smoke-placeholder"
cli="$root/venv/bin/pptxgym"

"$cli" setup --harness codex --executor local \
  --source-type local --source-path "$root/decks" \
  --work-root "$root/runs" --base-url https://relay.example/v1 \
  --api-key-ref env:PPTXGYM_RELEASE_SMOKE_KEY \
  --non-interactive --force
"$cli" doctor
"$cli" harness list
"$cli" guide agent | grep -q 'Resume an interrupted run'
"$cli" guide operator | grep -q 'Never ask for a token in chat'
"$cli" run --mode fast --count 1 --name rc --dry-run --no-detach
test -s "$root/runs/rc/run.toml"

"$root/venv/bin/python" - <<'PY'
from importlib.resources import files

root = files("pptxgym") / "resources"
required = (
    root / "guides" / "AGENTS.md",
    root / "skills" / "pptxgym-operator" / "SKILL.md",
    root / "skills" / "pptxgym-operator" / "agents" / "openai.yaml",
    root / "executors" / "crun.sh",
)
missing = [str(path) for path in required if not path.is_file()]
if missing:
    raise SystemExit("missing packaged resources: " + ", ".join(missing))
PY

printf 'release smoke passed: %s\n' "$root"
