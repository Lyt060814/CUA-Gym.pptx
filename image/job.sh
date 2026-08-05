#!/usr/bin/env bash
# What an HF Jobs run actually executes.
#
#   hf jobs run --flavor cpu-performance --timeout 6h \
#       --secrets GH_TOKEN --secrets CLAUDE_CODE_OAUTH_TOKEN --secrets HF_TOKEN \
#       -e PPTXGYM_COMMIT=<sha> \
#       ubuntu:22.04 bash -c "$(cat image/job.sh)"
#
# The commit is pinned by environment rather than defaulted to `main`, because
# "which code produced this task" has to be answerable afterwards, and a job
# that silently tracked a moving branch cannot answer it. Everything else this
# pipeline emits carries its commit; the runner should not be the exception.
set -uo pipefail

REPO="${PPTXGYM_REPO:-Lyt060814/cua-gym-pptx}"
COMMIT="${PPTXGYM_COMMIT:-main}"
say() { printf '\n### %s\n' "$*"; }

say "enough to fetch the runtime with"
# A bare ubuntu:22.04 has neither curl nor wget nor git nor a CA bundle, so
# the script that installs everything cannot itself be downloaded. These four
# packages are the bootstrap's bootstrap; everything else belongs in
# bootstrap.sh, where it is versioned with the code.
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq && apt-get install -y -qq --no-install-recommends \
    ca-certificates curl git >/dev/null || { echo "cannot install curl/git"; exit 1; }

say "runtime"
# bootstrap.sh is fetched from the same commit as the code, not baked into an
# image: a runtime that can drift from the code it runs is a class of failure
# nobody debugs successfully.
curl -fsSL -H "Authorization: token ${GH_TOKEN}" \
    "https://raw.githubusercontent.com/${REPO}/${COMMIT}/image/bootstrap.sh" \
    -o /tmp/bootstrap.sh || { echo "could not fetch bootstrap.sh"; exit 1; }
bash /tmp/bootstrap.sh || { echo "bootstrap failed"; exit 1; }

say "code at ${COMMIT}"
git clone --quiet "https://${GH_TOKEN}@github.com/${REPO}.git" /work/pptxgym \
    || { echo "clone failed"; exit 1; }
cd /work/pptxgym || exit 1
git checkout --quiet "$COMMIT" || { echo "no such commit: $COMMIT"; exit 1; }
git log -1 --format='    %h %s' | sed 's/^/    /'
python3 -m pip install --quiet -e . 2>/dev/null || python3 -m pip install --quiet -e . --break-system-packages

say "a deck to smoke against"
python3 - <<'PY'
from pptx import Presentation
from pptx.util import Inches, Pt
p = Presentation()
for i in range(3):
    s = p.slides.add_slide(p.slide_layouts[5])
    s.shapes.title.text = f"slide {i+1}"
    tb = s.shapes.add_textbox(Inches(1), Inches(2), Inches(6), Inches(2))
    tb.text_frame.text = "the quick brown fox " * 3
p.save("/tmp/smoke.pptx")
print("    3 slides -> /tmp/smoke.pptx")
PY

say "smoke"
# The real instrument. Its last check is `wps_roundtrip.py`, which knows that
# WPS writes nothing for an unmodified document and dirties the notes pane
# first -- the thing the standalone probe kept getting wrong.
bash image/smoke.sh /tmp/smoke.pptx
rc=$?

say "verdict: $([ $rc -eq 0 ] && echo 'the runtime is usable on HF Jobs' || echo 'see the FAIL lines above')"
exit $rc
