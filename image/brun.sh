#!/usr/bin/env bash
# The B run: ten fresh decks, cold, on HF Jobs.
#
#   hf jobs run --flavor cpu-performance --timeout 6h \
#       --secrets GH_TOKEN --secrets CLAUDE_CODE_OAUTH_TOKEN --secrets HF_TOKEN \
#       -e PPTXGYM_COMMIT=<sha> ubuntu:22.04 bash -c "$(cat image/brun.sh)"
#
# What this measures that nothing so far has: the yield of the current
# pipeline. Every fix in it was validated against the ten pilot decks, which
# is fitting to the sample — and one of those fixes (the surviving-twin
# exception to the unearnable rule) was *discovered* from the sample, not
# merely confirmed by it. Ten decks nobody has looked at, on frozen code, is
# the first honest number.
set -uo pipefail

REPO="${PPTXGYM_REPO:-Lyt060814/cua-gym-pptx}"
COMMIT="${PPTXGYM_COMMIT:-main}"
RESULTS="${PPTXGYM_RESULTS_REPO:-Lytttttt/pptxgym-runs}"
say() { printf '\n### %s  [%s]\n' "$*" "$(date -u +%H:%M:%S)"; }

# Every external command this script needs, checked before anything expensive.
# A run has already been lost to `curl: command not found` discovered after a
# 319 MB install, and the failure came ten minutes in rather than at once.
need() {
    local missing=""
    for c in "$@"; do command -v "$c" >/dev/null 2>&1 || missing="$missing $c"; done
    [ -z "$missing" ] || { echo "missing commands:$missing"; return 1; }
}

say "bootstrap's bootstrap"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq && apt-get install -y -qq --no-install-recommends \
    ca-certificates curl git jq >/dev/null || { echo "cannot install curl/git/jq"; exit 1; }
need curl git jq || exit 1

say "runtime at ${COMMIT}"
curl -fsSL -H "Authorization: token ${GH_TOKEN}" \
    "https://raw.githubusercontent.com/${REPO}/${COMMIT}/image/bootstrap.sh" \
    -o /tmp/bootstrap.sh || { echo "could not fetch bootstrap.sh"; exit 1; }
bash /tmp/bootstrap.sh || { echo "bootstrap failed"; exit 1; }

git clone --quiet "https://${GH_TOKEN}@github.com/${REPO}.git" /srv/pptxgym || exit 1
cd /srv/pptxgym || exit 1
git checkout --quiet "$COMMIT" || { echo "no such commit: $COMMIT"; exit 1; }
git log -1 --format='    %h %s'
PIPFLAGS=""
python3 -m pip install --help 2>/dev/null | grep -q -- --break-system-packages \
    && PIPFLAGS="--break-system-packages"
python3 -m pip install --quiet $PIPFLAGS -e . || { echo "pip install failed"; exit 1; }
python3 -m pip install --quiet $PIPFLAGS huggingface_hub >/dev/null 2>&1
need soffice pdftoppm Xvfb xdotool wpp claude || exit 1

say "the ten decks"
# From a private dataset rather than from Zenodo, and the reason is worth
# keeping: the corpus crawler **normalises** what it stores, so the copies the
# pipeline actually processes are not byte-identical to the files on Zenodo.
# The first attempt pinned the normalised hash and fetched the raw file, six of
# ten failed the check, and the run refused to proceed — correctly, and ten
# minutes of install time cheaper than finding out at the end.
#
# The pilot decks were normalised too, so this is also what keeps the two runs
# comparable: a yield that changed because the inputs changed would tell us
# nothing about the pipeline.
#
# All ten CC-BY-4.0, verified at the Zenodo API rather than taken from the
# corpus manifest's licence column, and attributed in corpus/ATTRIBUTION.md
# beside them. Pinned by sha256, because a run against unknown bytes measures
# nothing.
mkdir -p /srv/decks
FETCHED=0
while read -r name url sha; do
    curl -fsSL --max-time 300 --retry 6 --retry-delay 10 --retry-all-errors \
        -H "Authorization: Bearer ${HF_TOKEN}" \
        "$url" -o "/srv/decks/$name" || { echo "    FETCH FAILED $name"; continue; }
    got=$(sha256sum "/srv/decks/$name" | cut -d' ' -f1)
    if [ "$got" != "$sha" ]; then
        echo "    SHA MISMATCH $name (got $got)"; rm -f "/srv/decks/$name"; continue
    fi
    FETCHED=$((FETCHED + 1))
    printf '    ok %s\n' "$name"
done < <(jq -r '.[] | "\(.name) \(.url) \(.sha256)"' image/brun-fetch.json)
echo "    $FETCHED/10 decks"
[ "$FETCHED" -ge 8 ] || { echo "too few decks to call this a ten-deck run"; exit 1; }

say "shipping results out as they happen"
# The disk is ephemeral: a job that dies at minute 90 takes everything with
# it. So the run log and the state of every deck leave *during* the run, not
# at the end. Every two minutes, which is cheap next to what it protects.
cat > /tmp/ship.py <<'PY'
import os, subprocess, tarfile, time, pathlib
from huggingface_hub import HfApi
api, repo = HfApi(), os.environ["PPTXGYM_RESULTS_REPO"]
run = os.environ.get("PPTXGYM_RUN", "brun")
try:
    api.create_repo(repo, repo_type="dataset", private=True, exist_ok=True)
except Exception as e:
    print("  ship: cannot create repo:", e, flush=True)
while True:
    time.sleep(120)
    try:
        out = pathlib.Path("/tmp/state.tar.gz")
        with tarfile.open(out, "w:gz") as t:
            for p in ("work/runs", "work/state.json"):
                if os.path.exists(p):
                    t.add(p)
            for d in sorted(pathlib.Path("work").glob("deck*")):
                for f in ("state.json", "task.json", "solvability.json",
                          "plan.json", "delta.json", "proposal.json"):
                    if (d / f).exists():
                        t.add(d / f)
        api.upload_file(path_or_fileobj=str(out), repo_id=repo,
                        repo_type="dataset",
                        path_in_repo=f"{run}/state.tar.gz")
        subprocess.run(["bash", "-c",
                        "tail -c 400000 /tmp/brun.log > /tmp/brun.tail"],
                       check=False)
        if os.path.exists("/tmp/brun.tail"):
            api.upload_file(path_or_fileobj="/tmp/brun.tail", repo_id=repo,
                            repo_type="dataset", path_in_repo=f"{run}/log.txt")
    except Exception as e:                                  # noqa: BLE001
        print("  ship:", type(e).__name__, str(e)[:120], flush=True)
PY
export PPTXGYM_RESULTS_REPO="$RESULTS"
export PPTXGYM_RUN="brun-$(date -u +%Y%m%dT%H%M%SZ)"
python3 /tmp/ship.py >/tmp/ship.log 2>&1 &
echo "    -> hf.co/datasets/$RESULTS  under $PPTXGYM_RUN"

say "ingest"
python3 -m pptxgym.cli ingest /srv/decks/*.pptx 2>&1 | tail -20

say "run — eleven stages, ten decks, no WPS round trip"
# --no-wps is a recorded gap, not a quiet weakening: the round trip does not
# work in a container and eight explanations for that were wrong. What it
# costs (the gt_roundtrip attack, the tolerance measurement) is CPU-only work
# of ~12 s a deck and is done on the development machine instead. See
# HFJOBS.md.
export PPTXGYM_WPS_TRACE=1
# Ten decks, ten agent workers: no deck ever queues for a slot, which is the
# ceiling worth having for a batch this size — the help says agent stages are
# ~85% of the wall clock, so everything else is rounding.
#
# cpu-workers is set rather than defaulted: the default is cores/4 and `nproc`
# reports 64 inside a container that has 32 vCPU, so the default would be 16
# by accident rather than by decision. 16 is also what 32 real vCPU wants for
# soffice renders.
#
# api-retries above the default of 3 because ten-wide is more likely to meet
# the account rate limit than the eight-wide pilot did, and a deck that failed
# for want of API capacity is not a bad deck — it is a deck to try again.
# --model opus, pinned.
#
# The first cold run left it unset and every stage ran on sonnet, because
# `claude -p` with no flag takes the CLI's default and the container's default
# is not this machine's. The pilot run was mostly opus. So the two runs used
# different models and the yield comparison measured that, not the pipeline:
# four decks came back "the agent wrote nothing", degradations were proposed
# with no recipe step to implement them, repairs changed nothing, and 0 of 10
# reached `packaged`.
#
# The lesson is narrower than "pin the model". A run whose whole purpose is to
# freeze one variable had frozen the code by commit and the corpus by sha256,
# and left the most expensive variable in the system floating.
python3 -m pptxgym.cli run --workers 10 --cpu-workers 16 --attack-workers 8 \
    --api-retries 5 --model opus --no-wps 2>&1 | tee -a /tmp/brun.log

say "where it got to"
python3 -m pptxgym.cli status 2>&1 | tee -a /tmp/brun.log

say "final upload"
python3 - <<'PY'
import os, tarfile, pathlib
from huggingface_hub import HfApi
api, repo = HfApi(), os.environ["PPTXGYM_RESULTS_REPO"]
run = os.environ["PPTXGYM_RUN"]
# Everything this time, including the emitted tasks and the agent logs, which
# is what makes the run auditable after the machine is gone.
with tarfile.open("/tmp/final.tar.gz", "w:gz") as t:
    for p in ("work",):
        if os.path.exists(p):
            t.add(p)
api.upload_file(path_or_fileobj="/tmp/final.tar.gz", repo_id=repo,
                repo_type="dataset", path_in_repo=f"{run}/final.tar.gz")
print("uploaded", os.path.getsize("/tmp/final.tar.gz") // 1048576, "MB")
PY

say "done"
