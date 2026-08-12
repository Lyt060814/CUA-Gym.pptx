#!/usr/bin/env bash
# Answer a running job: publish a reply into the results dataset.
#
#   frontend/reply.sh <run> fixed     <signature> <commit> "why"
#   frontend/reply.sh <run> wontfix   <signature> -        "why"
#   frontend/reply.sh <run> not-ours  <signature> -        "why"
#   frontend/reply.sh <run> stop      deck0007    -        "why"
#
# The run polls for this while it has decks it could not unblock itself. A
# `fixed` reply names the commit to check out; everything else is applied as it
# stands.
#
# `pptxgym.orchestration.mailbox.publish` validates before anything is written, so a
# malformed reply is caught here rather than in a container an hour later —
# and `commit` is checked to be a commit id, because it reaches `git checkout`
# inside a process holding a GH_TOKEN.
set -euo pipefail
RUN="${1:?usage: reply.sh <run-id> <verdict> <signature|deck> <commit|-> [note]}"
VERDICT="${2:?}"
WHO="${3:?}"
COMMIT="${4:--}"
NOTE="${5:-}"
REPO="${PPTXGYM_RESULTS_REPO:-Lytttttt/pptxgym-runs}"
cd "$(dirname "${BASH_SOURCE[0]}")/.."

[ "$COMMIT" = "-" ] && COMMIT=""

python3 - "$RUN" "$VERDICT" "$WHO" "$COMMIT" "$NOTE" <<'PY'
import sys, os
from pathlib import Path
from pptxgym.orchestration import mailbox as mb

run, verdict, who, commit, note = sys.argv[1:6]
key = "decks" if who.startswith("deck") else "signature"
entry = {key: [who] if key == "decks" else who,
         "verdict": verdict, "commit": commit, "note": note}
out = Path("/tmp/reply.json")
mb.publish(out, [entry], run=run)          # raises BadReply before writing
print(out.read_text())

from huggingface_hub import HfApi
HfApi(token=os.environ["HF_TOKEN"]).upload_file(
    path_or_fileobj=str(out), repo_id=os.environ["PPTXGYM_RESULTS_REPO"],
    repo_type="dataset", path_in_repo=f"{run}/reply.json")
print(f"published to {os.environ['PPTXGYM_RESULTS_REPO']} under {run}/")
PY
