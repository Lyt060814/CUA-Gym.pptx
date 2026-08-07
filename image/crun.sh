#!/usr/bin/env bash
# One job, whole loop: select decks from the corpus, run one orchestrator
# per deck, collect — no byte routes through a development machine, because
# the corpus, the results and the job all live on the hub.
#
#   hf jobs run --detach --flavor cpu-performance --timeout 8h \
#       --secrets GH_TOKEN --secrets CLAUDE_CODE_OAUTH_TOKEN --secrets HF_TOKEN \
#       -e PPTXGYM_COMMIT=<sha> [-e PPTXGYM_SELECT=30] \
#       [-e PPTXGYM_FETCH=corpus/<batch>/<batch>-fetch.json] \
#       [-e PPTXGYM_RESUME_FROM=<run>] \
#       ubuntu:22.04 bash -c "$(cat image/crun.sh)"
#
# Default is autoselect: the funnel (licence, dedup, probe, triage, fonts,
# render) runs here and picks the top PPTXGYM_SELECT decks; the manifest it
# writes back makes any batch re-runnable bit-for-bit via PPTXGYM_FETCH.
# The bar: >=90% of selected decks shipped, ~1 hour per deck.
#
# What is gone from brun.sh, on purpose: the reply/mail channel and the
# multi-round fix loop. The orchestrator architecture has no mid-run code
# channel — a parked deck stays parked with its REVIEW.md, and re-running
# after a fix costs nothing (state.json keeps every finished stage).
set -uo pipefail

# Fail on the first line with the missing secret's *name*, not twenty lines
# in with "unbound variable": the first 30-deck launch died exactly that way
# because `--secrets GH_TOKEN` silently passes nothing when the submitting
# shell has no GH_TOKEN exported.
for v in GH_TOKEN HF_TOKEN CLAUDE_CODE_OAUTH_TOKEN; do
    [ -n "$(eval echo "\${$v:-}")" ] || { echo "missing secret: $v"; exit 1; }
done

REPO="${PPTXGYM_REPO:-Lyt060814/cua-gym-pptx}"
COMMIT="${PPTXGYM_COMMIT:-main}"
RESULTS="${PPTXGYM_RESULTS_REPO:-Lytttttt/pptxgym-runs}"
WORKERS="${PPTXGYM_WORKERS:-10}"
say() { printf '\n### %s  [%s]\n' "$*" "$(date -u +%H:%M:%S)"; }

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
need soffice pdftoppm claude || exit 1
# Not `fc-list | grep -q`: under `pipefail`, grep -q exits at the first match
# and fc-list dies of SIGPIPE, so the pipeline reports failure on success and
# every run warned that the font it had just installed was missing.
fc-list > /tmp/fonts.txt 2>/dev/null || true
grep -qi carlito /tmp/fonts.txt \
    || echo "    WARNING: carlito absent — Calibri decks will reflow"

say "authenticating this machine"
# Machine setup, done once here — before any agent exists — the same way a
# developer's laptop is set up, and for the same reason.
#
# The pipeline runs agents three processes deep: foreman spawns an
# orchestrator, the orchestrator's Bash tool runs a CLI verb, the verb spawns
# a sealed specialist. Claude Code deliberately keeps its OAuth token out of
# the environment it hands to Bash, so on a container whose only credential
# is that environment variable, the third process is always "Not logged in":
# both Jobs runs lost every sealed `solvable` to it, and the orchestrators
# ran the probe themselves, behind a weaker barrier and on a briefing they
# had written. That is the one piece of testimony in this pipeline that is
# supposed to be uncontaminable.
#
# So the token goes where a logged-in machine keeps it, and the env var is
# dropped afterwards: every nested `claude` then authenticates from the
# credential store like any other process on a set-up machine, and no agent
# ever needs the secret passed to it.
if [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
    mkdir -p "$HOME/.claude"
    umask 077
    printf '{"claudeAiOauth":{"accessToken":"%s","scopes":["user:inference"]}}\n' \
        "$CLAUDE_CODE_OAUTH_TOKEN" > "$HOME/.claude/.credentials.json"
    chmod 600 "$HOME/.claude/.credentials.json"
    if claude --version >/dev/null 2>&1 && \
       echo 'reply with the single word: ok' | \
       env -u CLAUDE_CODE_OAUTH_TOKEN claude -p --max-turns 1 >/dev/null 2>&1; then
        echo "    credential store works without the env var — nested agents will authenticate"
        unset CLAUDE_CODE_OAUTH_TOKEN
    else
        echo "    WARNING: credential store did not authenticate; keeping the env"
        echo "             var, so sealed specialist verbs may fail and the probe"
        echo "             will fall back to the weaker barrier (recorded as a caveat)"
    fi
fi

# Codex lane. Two ways to authenticate it, either optional — a run with
# neither is simply all-claude:
#   PPTXGYM_CODEX_BASE_URL + RELAY_API_KEY   an OpenAI-compatible relay:
#       codex runs on a custom provider over the Responses wire, api-key
#       only, no login of any kind (the muse-spark route).
#   CODEX_AUTH_B64                           base64 of a logged-in machine's
#       ~/.codex/auth.json, for a real ChatGPT-plan login.
if [ -n "${PPTXGYM_CODEX_BASE_URL:-}" ]; then
    mkdir -p "$HOME/.codex"
    cat > "$HOME/.codex/config.toml" <<TOML
model_provider = "relay"
model = "${PPTXGYM_CODEX_MODEL:-muse-spark-1.1}"

[model_providers.relay]
name = "pptxgym relay"
base_url = "${PPTXGYM_CODEX_BASE_URL}"
env_key = "RELAY_API_KEY"
wire_api = "responses"
TOML
    if [ -n "${RELAY_API_KEY:-}" ] && command -v codex >/dev/null 2>&1 && \
       (cd /tmp && codex exec --skip-git-repo-check \
           "Reply with the single word: ok" >/dev/null 2>&1); then
        echo "    codex relay works (${PPTXGYM_CODEX_BASE_URL}) — codex-lane decks will run"
    else
        echo "    WARNING: codex relay smoke failed; codex-lane decks will park"
    fi
elif [ -n "${CODEX_AUTH_B64:-}" ]; then
    mkdir -p "$HOME/.codex"
    umask 077
    echo "$CODEX_AUTH_B64" | base64 -d > "$HOME/.codex/auth.json" 2>/dev/null \
        && chmod 600 "$HOME/.codex/auth.json"
    if command -v codex >/dev/null 2>&1 && codex login status >/dev/null 2>&1; then
        echo "    codex credential store works — codex-lane decks will authenticate"
    else
        echo "    WARNING: codex login status failed; codex-lane decks will park"
    fi
    unset CODEX_AUTH_B64
fi

say "the decks"
# Two ways in, one loop end to end on this machine — no laptop in the path:
#
#   default            `corpus autoselect` runs the whole funnel here —
#                      licence, dedup, Range-probe, download, triage, font
#                      coverage, blank-render — picks the top PPTXGYM_SELECT
#                      decks and pushes manifest + scoring pool back to the
#                      results dataset. The pool makes it incremental: only
#                      rows nobody has scored yet cost anything.
#   PPTXGYM_FETCH=...  a manifest from a previous selection, pinned by
#                      sha256 against the *source* URLs — the rerun path,
#                      so a measurement can be repeated on known bytes.
mkdir -p /srv/decks /srv/scan
export PPTXGYM_RUN="${PPTXGYM_RUN:-crun-$(date -u +%Y%m%dT%H%M%SZ)}"
if [ -z "${PPTXGYM_FETCH:-}" ]; then
    SELECT_N="${PPTXGYM_SELECT:-$WORKERS}"
    python3 -m pptxgym.corpus autoselect --n "$SELECT_N" \
        --name "$PPTXGYM_RUN" --dest /srv/decks --scratch /srv/scan \
        --repo "$RESULTS" ${PPTXGYM_SCAN:+--scan "$PPTXGYM_SCAN"} \
        2>&1 | tee -a /tmp/crun.log \
        || { echo "autoselect failed"; exit 1; }
    GOT=$(ls /srv/decks/*.pptx 2>/dev/null | wc -l)
    [ "$GOT" -gt 0 ] || { echo "selection produced no decks"; exit 1; }
    echo "    $GOT decks selected -> /srv/decks"
else
    if curl -fsSL --max-time 120 -H "Authorization: Bearer ${HF_TOKEN}" \
        "https://huggingface.co/datasets/${RESULTS}/resolve/main/${PPTXGYM_FETCH}" \
        -o /tmp/fetch.json; then
        MANIFEST=/tmp/fetch.json
        echo "    manifest: ${PPTXGYM_FETCH} ($(jq length /tmp/fetch.json) decks)"
    else
        echo "cannot fetch manifest ${PPTXGYM_FETCH}"; exit 1
    fi
    WANTED=$(jq length "$MANIFEST")
    FETCHED=0
    # name last and greedy: real deck filenames contain spaces
    while read -r sha url name; do
        curl -fsSL --max-time 300 --retry 6 --retry-delay 10 --retry-all-errors \
            -H "Authorization: Bearer ${HF_TOKEN}" \
            "$url" -o "/srv/decks/$name" || { echo "    FETCH FAILED $name"; continue; }
        got=$(sha256sum "/srv/decks/$name" | cut -d' ' -f1)
        if [ "$got" != "$sha" ]; then
            echo "    SHA MISMATCH $name (got $got)"; rm -f "/srv/decks/$name"; continue
        fi
        FETCHED=$((FETCHED + 1))
        printf '    ok %s\n' "$name"
    done < <(jq -r '.[] | "\(.sha256) \(.url) \(.name)"' "$MANIFEST")
    echo "    $FETCHED/$WANTED decks"
    # 80% of the manifest, floor 1: a batch that lost a couple of decks to a
    # flaky CDN is still the batch; one that lost half measures the CDN.
    [ "$FETCHED" -gt 0 ] && [ $((FETCHED * 100 / WANTED)) -ge 80 ] \
        || { echo "too few decks fetched to be the batch that was asked for"; exit 1; }
fi

say "shipping results out as they happen"
# The disk is ephemeral: state and logs leave every two minutes, a resume
# point every ten. Unchanged from brun.sh except the shipped file list, which
# now includes each deck's REVIEW.md and foreman.json — the two artefacts the
# orchestrator architecture is judged by.
cat > /tmp/ship.py <<'PY'
import json, os, subprocess, tarfile, time, pathlib
from huggingface_hub import HfApi
api, repo = HfApi(), os.environ["PPTXGYM_RESULTS_REPO"]
run = os.environ.get("PPTXGYM_RUN", "crun")
last_full = 0.0
sent: set = set()
try:
    api.create_repo(repo, repo_type="dataset", private=True, exist_ok=True)
except Exception as e:
    print("  ship: cannot create repo:", e, flush=True)
while True:
    time.sleep(120)
    try:
        out = pathlib.Path("/tmp/state.tar.gz")
        with tarfile.open(out, "w:gz") as t:
            for p in ("work/runs",):
                if os.path.exists(p):
                    t.add(p)
            for d in sorted(pathlib.Path("work").glob("deck*")):
                # recipe.json belongs here: it is the only file that can
                # replay a degradation, and without it a deck whose big
                # archive is lost cannot be rebuilt from the small one —
                # twenty finished decks died that way.
                for f in ("state.json", "task.json", "solvability.json",
                          "plan.json", "delta.json", "proposal.json",
                          "recipe.json", "recipe.jsonl", "bundle.json",
                          "REVIEW.md", "foreman.json", "attacks.json"):
                    if (d / f).exists():
                        t.add(d / f)
        api.upload_file(path_or_fileobj=str(out), repo_id=repo,
                        repo_type="dataset",
                        path_in_repo=f"{run}/state.tar.gz")
        subprocess.run(["bash", "-c",
                        "tail -c 400000 /tmp/crun.log > /tmp/crun.tail"],
                       check=False)
        if os.path.exists("/tmp/crun.tail"):
            api.upload_file(path_or_fileobj="/tmp/crun.tail", repo_id=repo,
                            repo_type="dataset", path_in_repo=f"{run}/log.txt")
        # The deliverable leaves the machine the moment it exists. A task
        # bundle used to live only inside the ten-minute archive, so a job
        # killed between checkpoints took every finished task with it. A
        # bundle is a few MB and is uploaded once, when its deck ships.
        for d in sorted(pathlib.Path("work").glob("deck*")):
            try:
                rec = json.loads((d / "foreman.json").read_text())
            except (OSError, ValueError):
                continue
            if rec.get("outcome") != "shipped" or d.name in sent:
                continue
            tid = rec.get("task")
            src = pathlib.Path("work/emitted") / f"task_{tid}" if tid else None
            if not src or not src.exists():
                continue
            try:
                tarball = f"/tmp/{d.name}-bundle.tar.gz"
                with tarfile.open(tarball, "w:gz") as bt:
                    bt.add(src, arcname=f"task_{tid}")
                api.upload_file(path_or_fileobj=tarball, repo_id=repo,
                                repo_type="dataset",
                                path_in_repo=f"{run}/bundles/{d.name}.tar.gz")
                sent.add(d.name)
                print(f"  ship: bundle {d.name} (task_{tid})", flush=True)
            except Exception as e:                              # noqa: BLE001
                print("  ship: bundle", d.name, type(e).__name__,
                      str(e)[:80], flush=True)

        now = time.time()
        if now - last_full > 600:
            last_full = now
            # Two slots, alternating, each verified before it leaves.
            # One slot was enough to lose a night: a job re-using the same
            # run name overwrote a good 20-deck resume point with its own
            # from-scratch tree, and the restore found a corrupt archive
            # ("trailing garbage") with no older copy to fall back to.
            slot = "a" if int(last_full) % 2 == 0 else "b"
            out = f"/tmp/resume-{slot}.tar.gz"
            with tarfile.open(out, "w:gz") as t:
                if os.path.exists("work"):
                    t.add("work")
            try:                       # a tar that cannot be listed is not
                with tarfile.open(out) as t:   # a resume point
                    n = len(t.getnames())
            except Exception as e:                          # noqa: BLE001
                print("  ship: resume tar unreadable, not uploading:",
                      type(e).__name__, e, flush=True)
                continue
            api.upload_file(path_or_fileobj=out, repo_id=repo,
                            repo_type="dataset",
                            path_in_repo=f"{run}/resume-{slot}.tar.gz")
            print(f"  ship: resume point {slot} ({n} entries)", flush=True)
    except Exception as e:                                  # noqa: BLE001
        print("  ship:", type(e).__name__, str(e)[:120], flush=True)
PY
export PPTXGYM_RESULTS_REPO="$RESULTS"
python3 /tmp/ship.py >/tmp/ship.log 2>&1 &
SHIP_PID=$!

# The platform stops a job with SIGTERM and escalates to SIGKILL if the
# shell ignores it — which is how two runs died leaving a resume point up
# to ten minutes stale. Catching it buys one last checkpoint at the moment
# of death rather than whenever the loop last happened to fire.
on_term() {
    echo ""
    echo "### SIGTERM — checkpointing before the platform takes the machine"
    python3 - <<'PY2' || true
import os, tarfile
from huggingface_hub import HfApi
api, repo = HfApi(), os.environ["PPTXGYM_RESULTS_REPO"]
run = os.environ["PPTXGYM_RUN"]
out = "/tmp/resume-term.tar.gz"
with tarfile.open(out, "w:gz") as t:
    if os.path.exists("work"):
        t.add("work")
with tarfile.open(out) as t:
    n = len(t.getnames())
api.upload_file(path_or_fileobj=out, repo_id=repo, repo_type="dataset",
                path_in_repo=f"{run}/resume-a.tar.gz")
print(f"  checkpointed {n} entries at SIGTERM", flush=True)
PY2
    exit 143
}
trap on_term TERM INT
echo "    -> hf.co/datasets/$RESULTS  under $PPTXGYM_RUN"

say "resume point"
if [ -n "${PPTXGYM_RESUME_FROM:-}" ]; then
    for what in resume-a resume-b resume final; do
        url="https://huggingface.co/datasets/${RESULTS}/resolve/main/${PPTXGYM_RESUME_FROM}/${what}.tar.gz"
        if curl -fsSL --max-time 900 -H "Authorization: Bearer ${HF_TOKEN}" \
                "$url" -o /tmp/resume.tar.gz \
           && tar tzf /tmp/resume.tar.gz >/dev/null 2>&1; then
            # listed before it is trusted: a truncated archive extracts
            # *partially* and leaves a work tree that looks resumable and
            # is not
            tar xzf /tmp/resume.tar.gz -C . && {
                echo "    restored ${what}.tar.gz from ${PPTXGYM_RESUME_FROM}"
                RESTORED=1
                break
            }
        fi
        echo "    no usable ${what}.tar.gz in ${PPTXGYM_RESUME_FROM}"
    done
else
    echo "    (none — cold run)"
fi
# A resume that silently becomes a cold run is the most expensive failure
# this script can have: tonight it re-registered all 30 decks and started
# re-deriving 20 finished tasks on a fresh rate-limit window.
if [ -n "${PPTXGYM_RESUME_FROM:-}" ] && [ -z "${RESTORED:-}" ] \
   && [ -z "${PPTXGYM_ALLOW_COLD:-}" ]; then
    echo "asked to resume ${PPTXGYM_RESUME_FROM} and found no usable archive."
    echo "Refusing to burn a window re-doing finished work. Set"
    echo "PPTXGYM_ALLOW_COLD=1 to start over deliberately."
    exit 1
fi

# Both jobs that died mid-run (137 and 143) left no resource data at all,
# so "it was probably memory" stayed a guess. A sampler costs nothing and
# settles it: peak RSS and free memory every two minutes, in the log that
# ships out every two minutes.
( while true; do
    printf '    [mem %s] %s | top: %s\n' "$(date -u +%H:%M:%S)" \
      "$(free -g | awk '/^Mem:/{print "used "$3"G/"$2"G avail "$7"G"}')" \
      "$(ps -eo rss,comm --sort=-rss | awk 'NR>1 && NR<5 {printf "%s(%.1fG) ", $2, $1/1048576}')"
    sleep 120
  done ) >> /tmp/crun.log 2>&1 &

say "foreman — one orchestrator per deck"
# The probe takes the kernel mask where the container gives one and the deny
# rules where it does not, and says which everywhere it matters. --no-wps is
# a recorded gap, not a quiet weakening (HFJOBS.md). Models are pinned by the
# foreman's own lanes — orchestrator on opus+high, specialists per ASSIGN —
# because the one cold run that left a model floating measured the container's
# default instead of the pipeline.
export PPTXGYM_PROBE_BARRIER=best
python3 -m pptxgym.foreman /srv/decks/*.pptx --work work \
    --workers "$WORKERS" --no-wps \
    ${PPTXGYM_ENGINE_SPLIT:+--engine-split "$PPTXGYM_ENGINE_SPLIT"} \
    ${PPTXGYM_CODEX_MODEL:+--codex-model "$PPTXGYM_CODEX_MODEL"} \
    ${PPTXGYM_CODEX_WORKERS:+--codex-workers "$PPTXGYM_CODEX_WORKERS"} \
    ${PPTXGYM_EXTRA_FLAGS:-} \
    2>&1 | tee -a /tmp/crun.log

say "where it got to"
python3 -m pptxgym.cli --work work status 2>&1 | tee -a /tmp/crun.log

say "the numbers this run was asked for"
# Yield, wall clock and tokens, per deck and total — the three figures the
# 90%/1-hour bar is checked against, computed from the artefacts rather than
# promised by the log.
python3 - <<'PY' 2>&1 | tee -a /tmp/crun.log
import json, pathlib
decks = sorted(pathlib.Path("work").glob("deck*"))
shipped = parked = 0
total_min = total_tok = total_cost = 0.0
for d in decks:
    try:
        rec = json.loads((d / "foreman.json").read_text())
    except OSError:
        print(f"  {d.name}: no foreman record"); continue
    mins = rec.get("minutes", 0.0)
    tok = cost = 0.0
    for log in d.glob("*.jsonl"):
        try:
            for line in log.read_text().splitlines():
                if '"type": "result"' not in line and '"type":"result"' not in line:
                    continue
                r = json.loads(line)
                u = r.get("usage") or {}
                tok += (u.get("output_tokens") or 0)
                cost += (r.get("total_cost_usd") or 0)
        except (OSError, ValueError):
            pass
    total_min += mins; total_tok += tok; total_cost += cost
    if rec.get("outcome") == "shipped":
        shipped += 1
    else:
        parked += 1
    print(f"  {d.name}: {rec.get('outcome'):8s} {mins:6.1f} min  "
          f"{tok/1000:7.1f}k out-tokens  ${cost:.2f}  "
          f"{str(rec.get('why') or '')[:60]}")
print(f"\n  yield {shipped}/{len(decks)}  "
      f"avg {total_min/max(len(decks),1):.0f} min/deck  "
      f"{total_tok/1000:.0f}k out-tokens  ${total_cost:.2f} total")
PY

say "publish — shipped decks leave as tasks"
# End to end by standing instruction (2026-08-08): materials go to the HF
# dataset, each task's .py and the id registry are committed and pushed to
# the rollout repository, all from this job. Two safety properties:
#   - only decks whose foreman record says "shipped" are even considered,
#     and publish re-checks provenance itself;
#   - one publishing job at a time — the id registry is a git file, and two
#     concurrent pushes would race the allocation.
# With AWS credentials in the job's secrets the publish runs `--aws-verify`:
# each task's own setup() on a real VM, before its .py is committed — the
# strongest check there is. Without them it falls back to the URL check and
# says so; the smoke can then be run locally against the published tasks.
SHIPPED=$(python3 - <<'PY'
import json, pathlib
n = 0
for d in pathlib.Path("work").glob("deck*"):
    try:
        if json.loads((d / "foreman.json").read_text()).get("outcome") == "shipped":
            n += 1
    except (OSError, ValueError):
        pass
print(n)
PY
)
if [ "${SHIPPED:-0}" -gt 0 ] && [ -z "${PPTXGYM_NO_PUBLISH:-}" ]; then
    SMOKE=""
    if [ -n "${AWS_ACCESS_KEY_ID:-}" ] && [ -n "${AWS_SECRET_ACCESS_KEY:-}" ]; then
        OSWORLD_REPO="${PPTXGYM_OSWORLD_REPO:-yuanmengqi/OSWorld-V2}"
        curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1
        if git clone --quiet --depth 1 \
            "https://${GH_TOKEN}@github.com/${OSWORLD_REPO}.git" /srv/osworld \
            && [ -x "$HOME/.local/bin/uv" ]; then
            SMOKE="--aws-verify --osworld /srv/osworld --uv $HOME/.local/bin/uv"
            echo "    VM check armed: each task's setup() runs on a real instance"
        else
            echo "    WARNING: could not arm the VM check (no OSWorld checkout"
            echo "             or no uv) — publishing with the URL check only"
        fi
    else
        echo "    no AWS credentials — publishing with the URL check only;"
        echo "    the smoke can be run locally against the published tasks"
    fi
    ROLLOUT_REPO="${PPTXGYM_ROLLOUT_REPO:-yuanmengqi/osworld2.0-rollout}"
    if git clone --quiet --depth 1 \
        "https://${GH_TOKEN}@github.com/${ROLLOUT_REPO}.git" /srv/rollout; then
        python3 -m pptxgym.publish --work work --rollout /srv/rollout --push \
            $SMOKE 2>&1 | tee -a /tmp/crun.log \
            || echo "    PUBLISH FAILED — artefacts are in the final tar; publish can be re-run from them"
    else
        echo "    cannot clone ${ROLLOUT_REPO} — skipping publish, artefacts in the final tar"
    fi
else
    echo "    nothing shipped (or PPTXGYM_NO_PUBLISH set) — skipping publish"
fi

say "final upload"
python3 - <<'PY'
import os, tarfile
from huggingface_hub import HfApi
api, repo = HfApi(), os.environ["PPTXGYM_RESULTS_REPO"]
run = os.environ["PPTXGYM_RUN"]
with tarfile.open("/tmp/final.tar.gz", "w:gz") as t:
    if os.path.exists("work"):
        t.add("work")
api.upload_file(path_or_fileobj="/tmp/final.tar.gz", repo_id=repo,
                repo_type="dataset", path_in_repo=f"{run}/final.tar.gz")
print("uploaded", os.path.getsize("/tmp/final.tar.gz") // 1048576, "MB")
PY

say "done"
