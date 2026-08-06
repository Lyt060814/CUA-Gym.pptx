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
git clone --quiet "https://${GH_TOKEN}@github.com/${REPO}.git" /srv/pptxgym \
    || { echo "clone failed"; exit 1; }
cd /srv/pptxgym || exit 1
git checkout --quiet "$COMMIT" || { echo "no such commit: $COMMIT"; exit 1; }
git log -1 --format='    %h %s' | sed 's/^/    /'
# Ask pip what it supports rather than trying the modern form and falling
# back; the fallback ran the *wrong* way round and printed a usage error.
PIPFLAGS=""
python3 -m pip install --help 2>/dev/null | grep -q -- --break-system-packages \
    && PIPFLAGS="--break-system-packages"
python3 -m pip install --quiet $PIPFLAGS -e . || { echo "pip install -e . failed"; exit 1; }

say "a deck to smoke against"
# A real deck, fetched from its own source rather than generated here or
# vendored into this repo.
#
# Generated was wrong: a three-slide python-pptx deck round-trips in 252.9 s
# against 12.0 s for this one, twenty-one times slower and near enough the
# job deadline that a slower machine reads as broken.  A synthetic deck is not
# a small version of a real deck, and an instrument that is itself pathological
# cannot measure anything.
#
# Vendored was also wrong: it is somebody's work, and the right place to get
# it is where they put it.  Zenodo record 4312972, CC-BY-4.0, open access --
# verified against the Zenodo API rather than taken from the corpus manifest,
# because that manifest's licence column is a secondary claim.
#
#   Determining the FAIRness of UK Catalysis Hub Data
#   https://doi.org/10.5281/zenodo.4312972   CC-BY-4.0
DECK_URL="https://zenodo.org/records/4312972/files/PosterUKCH_C_2020d.pptx?download=1"
DECK_SHA="25f5434a8af0df1fe3d36d91e1ec4c8468d2b049882325f6e66d2f920092cf7e"
# Retries because Zenodo answered a container with 504 on the first attempt
# and this machine with 200 — a public archive is allowed to be busy, and a
# smoke test that fails on somebody else's bad minute tells us nothing about
# our runtime.
curl -fsSL --max-time 180 --retry 6 --retry-delay 10 --retry-all-errors \
    "$DECK_URL" -o /tmp/testdeck.pptx \
    || { echo "could not fetch the test deck from Zenodo after 6 tries"; exit 1; }
GOT=$(sha256sum /tmp/testdeck.pptx | cut -d' ' -f1)
if [ "$GOT" != "$DECK_SHA" ]; then
    echo "    test deck sha256 $GOT, expected $DECK_SHA"
    echo "    refusing: a smoke test against unknown bytes measures nothing"
    exit 1
fi
ls -l /tmp/testdeck.pptx | sed 's/^/    /'

say "eyes on the display"
# Four container runs were spent proposing explanations and testing them one
# ten-minute job at a time. The thing that finally identified the dialog was
# dumping the window tree — i.e. looking. So look properly: PPTXGYM_WPS_TRACE
# names which condition was slow and whether the document ever went modified,
# and a watcher keeps the last frame of the display the round trip is using,
# which comes back as base64 so it can actually be viewed.
export PPTXGYM_WPS_TRACE=1
apt-get install -y -qq --no-install-recommends imagemagick x11-apps >/dev/null 2>&1
mkdir -p /tmp/frames
( while true; do
    for d in 99 100 101; do
      DISPLAY=:$d import -window root -resize 900x /tmp/frames/last-$d.png 2>/dev/null && break
    done
    sleep 15
  done ) &
WATCHER=$!

say "smoke"
# The real instrument. Its last check is `wps_roundtrip.py`, which knows that
# WPS writes nothing for an unmodified document and dirties the notes pane
# first -- the thing the standalone probe kept getting wrong.
bash image/smoke.sh /tmp/testdeck.pptx
rc=$?
kill $WATCHER 2>/dev/null

if [ $rc -ne 0 ]; then
    say "the last frame of the display, base64 (decode and look at it)"
    F=$(ls -t /tmp/frames/*.png 2>/dev/null | head -1)
    if [ -n "$F" ]; then
        convert "$F" -colors 64 -depth 8 /tmp/frames/small.png 2>/dev/null || cp "$F" /tmp/frames/small.png
        echo "BEGIN_PNG_BASE64 $(stat -c%s /tmp/frames/small.png) bytes"
        base64 -w 200 /tmp/frames/small.png
        echo "END_PNG_BASE64"
    else
        echo "    no frame was captured"
    fi
fi

say "verdict: $([ $rc -eq 0 ] && echo 'the runtime is usable on HF Jobs' || echo 'see the FAIL lines above')"
exit $rc
