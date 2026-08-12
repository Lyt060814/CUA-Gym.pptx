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
REQUIRED_SECRETS=(GH_TOKEN HF_TOKEN)
NEEDS_CLAUDE=1
if [ -n "${PPTXGYM_PUBLISH_ONLY:-}" ]; then
    NEEDS_CLAUDE=0
elif [ -n "${PPTXGYM_NEEDS_CLAUDE_OAUTH:-}" ]; then
    NEEDS_CLAUDE="$PPTXGYM_NEEDS_CLAUDE_OAUTH"
elif [ "${PPTXGYM_PROBE_ENGINE:-claude}" = "codex" ] \
     && [[ "${PPTXGYM_ENGINE_SPLIT:-}" =~ ^codex=[0-9]+$ ]]; then
    # Both the deck owner and the independent witness are Codex.  A Claude
    # account at its weekly limit is irrelevant and must not be a bootstrap
    # dependency for this lane.
    NEEDS_CLAUDE=0
fi
[ "$NEEDS_CLAUDE" -eq 0 ] || REQUIRED_SECRETS+=(CLAUDE_CODE_OAUTH_TOKEN)
for v in "${REQUIRED_SECRETS[@]}"; do
    [ -n "$(eval echo "\${$v:-}")" ] || { echo "missing secret: $v"; exit 1; }
done

REPO="${PPTXGYM_REPO:?set PPTXGYM_REPO to the pipeline GitHub repository}"
COMMIT="${PPTXGYM_COMMIT:-main}"
RESULTS="${PPTXGYM_RESULTS_REPO:?set PPTXGYM_RESULTS_REPO to a writable dataset}"
WORKERS="${PPTXGYM_WORKERS:-10}"
say() { printf '\n### %s  [%s]\n' "$*" "$(date -u +%H:%M:%S)"; }

# Keep the token out of clone URLs and .git/config. Git invokes this helper
# for both the private source clone and the eventual rollout push.
git config --global credential.helper \
    '!f() { echo username=x-access-token; echo password="$GH_TOKEN"; }; f'

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

git clone --quiet "https://github.com/${REPO}.git" /srv/pptxgym || exit 1
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

# Publishing a run that already finished is its own job, and making it share
# the full path costs real money: the foreman would re-verify thirty decks and
# hand the ones that legitimately parked back to an orchestrator, paying again
# for an answer already on file. With this set, the run restores its resume
# point and goes straight to publish.
if [ -n "${PPTXGYM_PUBLISH_ONLY:-}" ]; then
    [ -n "${PPTXGYM_RESUME_FROM:-}" ] || {
        echo "PPTXGYM_PUBLISH_ONLY needs PPTXGYM_RESUME_FROM — there is"
        echo "nothing in this container to publish otherwise"; exit 1; }
    echo "    publish only — no selection, no foreman, no collect"
fi

if [ -z "${PPTXGYM_PUBLISH_ONLY:-}" ]; then

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
        --workers "${PPTXGYM_SELECTION_WORKERS:-8}" \
        ${PPTXGYM_FOCUS:+--focus "$PPTXGYM_FOCUS"} \
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
        case "$name" in
            ""|*/*|*\\*|.|..) echo "    INVALID MANIFEST NAME: $name"; exit 1 ;;
        esac
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
    jq 'map(select(.focus != null) | {key:.name, value:.focus}) | from_entries' \
        "$MANIFEST" > /srv/decks/focus.json
    # 80% of the manifest, floor 1: a batch that lost a couple of decks to a
    # flaky CDN is still the batch; one that lost half measures the CDN.
    [ "$FETCHED" -gt 0 ] && [ $((FETCHED * 100 / WANTED)) -ge 80 ] \
        || { echo "too few decks fetched to be the batch that was asked for"; exit 1; }
fi

fi   # end: skipped entirely under PPTXGYM_PUBLISH_ONLY

say "shipping results out as they happen"
# The disk is ephemeral: state and logs leave every two minutes, a resume
# point every ten. Unchanged from brun.sh except the shipped file list, which
# now includes each deck's REVIEW.md and foreman.json — the two artefacts the
# orchestrator architecture is judged by.
cat > /tmp/ship.py <<'PY'
import io, json, os, subprocess, tarfile, time, pathlib
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
            def add_tail(src, arcname, limit=131072):
                """Small live diagnostics, not another multi-GB resume slot."""
                try:
                    data = src.read_bytes()[-limit:]
                    stat = src.stat()
                except OSError:
                    return
                info = tarfile.TarInfo(arcname)
                info.size = len(data)
                info.mtime = int(stat.st_mtime)
                t.addfile(info, io.BytesIO(data))

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
                          "probe.json", "REVIEW.md", "foreman.json",
                          "attacks.json"):
                    if (d / f).exists():
                        t.add(d / f)
                diagnostics = [(d / "solvable.jsonl", "current"),
                               (d / "solvable.stderr.log", "current"),
                               (d / "crash-stage.log", "current")]
                retries = sorted((d / "retries").glob("solvable-try-*"),
                                 key=lambda p: p.stat().st_mtime)
                if retries:
                    diagnostics.extend((p, retries[-1].name)
                                       for p in retries[-1].glob("solvable*"))
                for src, label in diagnostics:
                    add_tail(src, f"{d}/diagnostics/{label}-{src.name}.tail")
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
                    names = t.getnames()
            except Exception as e:                          # noqa: BLE001
                print("  ship: resume tar unreadable, not uploading:",
                      type(e).__name__, e, flush=True)
                continue
            n = len(names)
            # Readable is not the same as useful. This loop starts before the
            # restore, so on a resumed run its first tick packs an empty work
            # tree — a perfectly valid archive that overwrites a real resume
            # point with nothing. It happened: `final.tar.gz` and one slot
            # went to zero bytes on a publish-only run, and only the second
            # slot still held the thirty decks.
            if not any(x.endswith("/state.json") for x in names):
                print("  ship: work tree holds no deck yet, not overwriting "
                      f"slot {slot}", flush=True)
                continue
            api.upload_file(path_or_fileobj=out, repo_id=repo,
                            repo_type="dataset",
                            path_in_repo=f"{run}/resume-{slot}.tar.gz")
            print(f"  ship: resume point {slot} ({n} entries)", flush=True)
    except Exception as e:                                  # noqa: BLE001
        print("  ship:", type(e).__name__, str(e)[:120], flush=True)
PY
export PPTXGYM_RESULTS_REPO="$RESULTS"
if [ -z "${PPTXGYM_PUBLISH_ONLY:-}" ]; then
    python3 /tmp/ship.py >/tmp/ship.log 2>&1 &
    SHIP_PID=$!
else
    echo "    publish only — periodic checkpoints disabled"
fi

# The platform stops a job with SIGTERM and escalates to SIGKILL if the
# shell ignores it — which is how two runs died leaving a resume point up
# to ten minutes stale. Catching it buys one last checkpoint at the moment
# of death rather than whenever the loop last happened to fire.
on_term() {
    echo ""
    echo "### SIGTERM — checkpointing before the platform takes the machine"
    if [ -n "${PPTXGYM_PUBLISH_ONLY:-}" ]; then
        echo "  publish-only work is restored, not newly generated — no checkpoint"
        exit 143
    fi
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
    names = t.getnames()
if any(x.endswith("/state.json") for x in names):
    api.upload_file(path_or_fileobj=out, repo_id=repo, repo_type="dataset",
                    path_in_repo=f"{run}/resume-a.tar.gz")
    print(f"  checkpointed {len(names)} entries at SIGTERM", flush=True)
else:
    print("  nothing to checkpoint — not overwriting the resume point",
          flush=True)
PY2
    exit 143
}
trap on_term TERM INT
echo "    -> hf.co/datasets/$RESULTS  under $PPTXGYM_RUN"

say "resume point"
if [ -n "${PPTXGYM_RESUME_FROM:-}" ]; then
    # Newest slot first. The fixed order resume-a, resume-b, final once
    # picked a stale mid-run checkpoint over a fresher final and re-ran ten
    # finished decks. The commit date on each archive says which is current;
    # if the API won't say, fall back to the written order.
    order=$(python3 - <<'PY0'
import datetime, os
from huggingface_hub import HfApi
slots = ["resume-a", "resume-b", "resume", "final"]
run = os.environ["PPTXGYM_RESUME_FROM"]
try:
    infos = HfApi().get_paths_info(os.environ["PPTXGYM_RESULTS_REPO"],
                                   [f"{run}/{s}.tar.gz" for s in slots],
                                   repo_type="dataset", expand=True)
    epoch = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)
    dated = {i.path: i.last_commit.date for i in infos
             if getattr(i, "last_commit", None)}
    slots.sort(key=lambda s: dated.get(f"{run}/{s}.tar.gz", epoch),
               reverse=True)
except Exception:
    pass
print(" ".join(slots))
PY0
    ) || order="resume-a resume-b resume final"
    echo "    slot order, newest first: ${order}"
    for what in ${order}; do
        url="https://huggingface.co/datasets/${RESULTS}/resolve/main/${PPTXGYM_RESUME_FROM}/${what}.tar.gz"
        if curl -fsSL --max-time "${PPTXGYM_RESUME_TIMEOUT:-900}" \
                -H "Authorization: Bearer ${HF_TOKEN}" \
                "$url" -o /tmp/resume.tar.gz \
           && tar tzf /tmp/resume.tar.gz >/dev/null 2>&1; then
            # listed before it is trusted: a truncated archive extracts
            # *partially* and leaves a work tree that looks resumable and
            # is not
            tar xzf /tmp/resume.tar.gz -C . && \
            [ -n "$(ls -d work/deck*/state.json 2>/dev/null | head -1)" ] && {
                echo "    restored ${what}.tar.gz from ${PPTXGYM_RESUME_FROM}"
                RESTORED=1
                break
            }
            echo "    ${what}.tar.gz holds no deck — trying the next slot"
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

if [ -z "${PPTXGYM_PUBLISH_ONLY:-}" ]; then

say "foreman — one orchestrator per deck"
# The probe takes the kernel mask where the container gives one and the deny
# rules where it does not, and says which everywhere it matters. --no-wps is
# a recorded gap, not a quiet weakening (HFJOBS.md). Models are pinned by the
# foreman's own lanes — orchestrator on opus+high, specialists per ASSIGN —
# because the one cold run that left a model floating measured the container's
# default instead of the pipeline.
#
# PPTXGYM_PROFILE=fast trades the three specialists the owner could do itself
# for wall clock, keeping the sealed probe and every measurement. Pair it with
# PPTXGYM_MODEL / PPTXGYM_EFFORT: Sonnet 5 is $2/$10 per MTok against Opus 5's
# $5/$25 and sits a latency class faster, which is the lever that actually
# moves how many decks fit in a rate-limit window.
export PPTXGYM_PROBE_BARRIER=best
# HF containers run the pipeline as root.  When a Codex probe cannot get an
# unshare mount namespace, it drops permanently to uid 65534; these directory
# modes then make the answer key physically untraversable while root-owned
# orchestrators continue normally.  The probe also gets a private HOME and no
# GH/HF credentials.  This is an OS boundary, not a prompt convention.
mkdir -p work
chmod 700 work /srv/decks
export PPTXGYM_PROBE_UID_BARRIER=1
FOREMAN_DECK_ARGS=()
if [ -n "${PPTXGYM_RUN_DECKS:-}" ]; then
    read -r -a RUN_DECKS <<< "${PPTXGYM_RUN_DECKS}"
    for d in "${RUN_DECKS[@]}"; do
        [[ "$d" =~ ^deck[0-9]{4}$ ]] || {
            echo "invalid deck id in PPTXGYM_RUN_DECKS: $d"; exit 1; }
        [ -f "work/$d/state.json" ] || {
            echo "PPTXGYM_RUN_DECKS asks for $d, but the restored archive does not contain it"
            exit 1
        }
    done
    FOREMAN_DECK_ARGS=(--deck "${RUN_DECKS[@]}")
    echo "    targeted resume: ${#RUN_DECKS[@]} deck(s); all others remain untouched"
fi
env -u GH_TOKEN -u HF_TOKEN -u HUGGING_FACE_HUB_TOKEN \
    -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY -u AWS_SESSION_TOKEN \
    python3 -m pptxgym.foreman /srv/decks/*.pptx --work work \
    --workers "$WORKERS" --no-wps \
    --cpu-workers "${PPTXGYM_CPU_WORKERS:-8}" \
    --timeout "${PPTXGYM_TIMEOUT_MINUTES:-90}" \
    --max-turns "${PPTXGYM_MAX_TURNS:-260}" \
    ${PPTXGYM_PROFILE:+--profile "$PPTXGYM_PROFILE"} \
    ${PPTXGYM_MODEL:+--model "$PPTXGYM_MODEL"} \
    ${PPTXGYM_EFFORT:+--effort "$PPTXGYM_EFFORT"} \
    ${PPTXGYM_SPECIALIST_MODEL:+--specialist-model "$PPTXGYM_SPECIALIST_MODEL"} \
    ${PPTXGYM_SPECIALIST_EFFORT:+--specialist-effort "$PPTXGYM_SPECIALIST_EFFORT"} \
    ${PPTXGYM_ENGINE_SPLIT:+--engine-split "$PPTXGYM_ENGINE_SPLIT"} \
    ${PPTXGYM_CODEX_MODEL:+--codex-model "$PPTXGYM_CODEX_MODEL"} \
    ${PPTXGYM_CODEX_WORKERS:+--codex-workers "$PPTXGYM_CODEX_WORKERS"} \
    ${PPTXGYM_PROBE_ENGINE:+--probe-engine "$PPTXGYM_PROBE_ENGINE"} \
    ${PPTXGYM_PROBE_MODEL:+--probe-model "$PPTXGYM_PROBE_MODEL"} \
    ${PPTXGYM_PROBE_EFFORT:+--probe-effort "$PPTXGYM_PROBE_EFFORT"} \
    "${FOREMAN_DECK_ARGS[@]}" \
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

say "per stage — where the time and the tokens actually went"
# The per-deck totals above answer "can we afford this"; this answers "what
# do we cut next", which is a different question and needs the breakdown.
# Durations come from each stage's own `duration_ms` where it recorded one,
# tokens from `observe.deck_tokens`, which dedups by session_id across the
# live log, `retries/` and `attempts/` — a deck that spent ten solvability
# attempts is counted as ten, not one.
python3 - <<'PY' 2>&1 | tee -a /tmp/crun.log
import collections, json, pathlib
from pptxgym import observe
from pptxgym.pipeline import STAGES

work = pathlib.Path("work")
# `orchestrator` is not a stage and it is the biggest spender in the run, so
# a table keyed on STAGES alone leaves the main cost off the page — the first
# calibration reported 178k of 358k tokens for exactly that reason.
LINES = list(STAGES) + ["orchestrator"]
rows, per_deck = {}, {}
proposal = {key: collections.Counter() for key in
            ("difficulty", "size_band", "reach", "reasoning", "interaction",
             "hard_basis")}
for d in sorted(work.glob("deck*")):
    try:
        st = json.loads((d / "state.json").read_text())
    except (OSError, ValueError):
        continue
    tok = observe.deck_tokens(d)
    per_deck[d.name] = {"stages": {}, "tokens": tok}
    for task in (st.get("proposed") or {}).get("detail") or []:
        for key in ("difficulty", "size_band", "hard_basis"):
            if task.get(key):
                proposal[key][task[key]] += 1
        for key in ("reach", "reasoning", "interaction"):
            proposal[key].update(task.get(key) or {})
    for stage in LINES:
        rec = st.get(stage) or {}
        ms = rec.get("duration_ms")
        acc = tok.get(stage) or {}
        r = rows.setdefault(stage, {"n": 0, "ms": 0, "out": 0, "cost": 0.0,
                                    "sessions": 0, "turns": 0, "recorded": 0,
                                    "adopted": 0})
        if rec.get("status"):
            r["recorded"] += 1
        if rec.get("adopted"):
            r["adopted"] += 1
        if ms:
            r["n"] += 1
            r["ms"] += ms
        r["out"] += acc.get("output", 0)
        r["cost"] += acc.get("cost_usd", 0.0)
        r["sessions"] += acc.get("sessions", 0)
        r["turns"] += acc.get("turns", 0)
        # every stage the deck actually reached, whether or not it happened to
        # record a duration: an adopted stage has neither a clock nor a
        # session, and reporting it as absent is how `adopted` came back empty
        # on a run where every deck adopted three stages
        if rec or acc:
            per_deck[d.name]["stages"][stage] = {
                "status": rec.get("status"), "ms": ms,
                "out_tokens": acc.get("output", 0),
                "cost_usd": round(acc.get("cost_usd", 0.0), 4),
                "sessions": acc.get("sessions", 0),
                "adopted": bool(rec.get("adopted"))}

print(f"  {'stage':14s} {'decks':>5s} {'timed':>5s} {'median s':>9s} "
      f"{'total s':>8s} {'out tok':>9s} {'sess':>5s} {'turns':>6s} "
      f"{'adopted':>7s} {'cost':>8s}")
for stage in LINES:
    r = rows.get(stage)
    if not r or not (r["recorded"] or r["out"]):
        continue
    med = r["ms"] / r["n"] / 1000 if r["n"] else 0.0
    print(f"  {stage:14s} {r['recorded']:5d} {r['n']:5d} {med:9.1f} "
          f"{r['ms']/1000:8.0f} {r['out']:9d} {r['sessions']:5d} "
          f"{r['turns']:6d} {r['adopted']:7d} ${r['cost']:7.2f}")

proposal = {key: dict(value) for key, value in proposal.items()}
if proposal["difficulty"]:
    print("\n  proposal difficulty — why the bands were earned")
    for key, value in proposal.items():
        print(f"  {key:12s} " + "  ".join(
            f"{name}={count}" for name, count in sorted(value.items())))

out = pathlib.Path("calibration.json")
out.write_text(json.dumps({"per_stage": rows, "per_deck": per_deck,
                           "proposal": proposal},
                          indent=1))
print(f"\n  written to {out}")
PY

python3 - <<'PY' 2>&1 | tee -a /tmp/crun.log
# beside the state tar, because this is the artefact the calibration is for
import os, pathlib
from huggingface_hub import HfApi
p = pathlib.Path("calibration.json")
if p.exists():
    HfApi().upload_file(
        path_or_fileobj=str(p), repo_type="dataset",
        repo_id=os.environ["PPTXGYM_RESULTS_REPO"],
        path_in_repo=f"{os.environ['PPTXGYM_RUN']}/calibration.json")
    print("  calibration.json uploaded")
PY

fi   # end: foreman and collect, skipped under PPTXGYM_PUBLISH_ONLY

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
PUBLISH_ARGS=()
if [ -n "${PPTXGYM_PUBLISH_DECKS:-}" ]; then
    read -r -a PUBLISH_DECKS <<< "${PPTXGYM_PUBLISH_DECKS}"
    for d in "${PUBLISH_DECKS[@]}"; do
        [[ "$d" =~ ^deck[0-9]{4}$ ]] || {
            echo "invalid deck id in PPTXGYM_PUBLISH_DECKS: $d"; exit 1; }
    done
    PUBLISH_ARGS+=(--deck "${PUBLISH_DECKS[@]}")
fi
if [ -n "${PPTXGYM_RECOVER_PACKAGED:-}" ]; then
    [ "${#PUBLISH_ARGS[@]}" -gt 0 ] || {
        echo "PPTXGYM_RECOVER_PACKAGED requires PPTXGYM_PUBLISH_DECKS"; exit 1; }
    PUBLISH_ARGS+=(--recover-packaged)
fi
if { [ "${SHIPPED:-0}" -gt 0 ] || [ -n "${PPTXGYM_RECOVER_PACKAGED:-}" ]; } \
   && [ -z "${PPTXGYM_NO_PUBLISH:-}" ]; then
    SMOKE=""
    if [ -n "${PPTXGYM_AWS_VERIFY:-}" ] \
       && [ -n "${AWS_ACCESS_KEY_ID:-}" ] \
       && [ -n "${AWS_SECRET_ACCESS_KEY:-}" ]; then
        OSWORLD_REPO="${PPTXGYM_OSWORLD_REPO:?set PPTXGYM_OSWORLD_REPO for AWS verification}"
        curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1
        if git clone --quiet --depth 1 \
            "https://github.com/${OSWORLD_REPO}.git" /srv/osworld \
            && [ -x "$HOME/.local/bin/uv" ]; then
            SMOKE="--aws-verify --osworld /srv/osworld --uv $HOME/.local/bin/uv"
            SMOKE="$SMOKE --aws-workers ${PPTXGYM_AWS_WORKERS:-4}"
            SMOKE="$SMOKE --hf-workers ${PPTXGYM_HF_WORKERS:-4}"
            SMOKE="$SMOKE --aws-attempts ${PPTXGYM_AWS_ATTEMPTS:-3}"
            [ -z "${PPTXGYM_AWS_INSTANCE_TYPE:-}" ] || \
                SMOKE="$SMOKE --aws-instance-type $PPTXGYM_AWS_INSTANCE_TYPE"
            [ -z "${PPTXGYM_AWS_REGION:-}" ] || \
                SMOKE="$SMOKE --aws-region $PPTXGYM_AWS_REGION"
            echo "    VM check armed: each task's setup() runs on a real instance"
        else
            echo "    WARNING: could not arm the VM check (no OSWorld checkout"
            echo "             or no uv) — publishing with the URL check only"
        fi
    elif [ -n "${PPTXGYM_AWS_VERIFY:-}" ]; then
        echo "    no AWS credentials — publishing with the URL check only;"
        echo "    the smoke can be run locally against the published tasks"
    else
        echo "    AWS smoke disabled — publishing with the URL check"
    fi
    # Attribution is a licence condition, not a nicety, and every fact it needs
    # is already in the manifest: url, sha256, the Zenodo DOI and the licence
    # the depositor chose. The manifest path never wrote them into the decks,
    # so a run that fetched from a manifest reached publish with 24 CC-BY decks
    # and no source record for any of them. `meta.json` remembers which file it
    # ingested; that is the join.
    # Both roads produce a manifest: the fetch path downloads the one it was
    # pinned to, the autoselect path wrote its own next to the decks. Gating
    # this on PPTXGYM_FETCH alone let a 40-task autoselect run publish with
    # no ATTRIBUTION.md on any of them.
    if [ -n "${PPTXGYM_FETCH:-}" ]; then
        curl -fsSL -H "Authorization: Bearer ${HF_TOKEN}" \
            "https://huggingface.co/datasets/${RESULTS}/resolve/main/${PPTXGYM_FETCH}" \
            -o /tmp/prov-manifest.json 2>/dev/null
    else
        cp "/srv/decks/${PPTXGYM_RUN}-fetch.json" /tmp/prov-manifest.json 2>/dev/null
    fi
    if [ -s /tmp/prov-manifest.json ]; then
        python3 - <<'PY' 2>&1 | tee -a /tmp/crun.log
import json, pathlib
man = {d["name"]: d for d in json.load(open("/tmp/prov-manifest.json"))}
wrote = missing = 0
for d in sorted(pathlib.Path("work").glob("deck*")):
    f = d / "provenance.json"
    if f.exists():
        continue
    try:
        origin = pathlib.Path(json.loads((d / "meta.json").read_text())["origin"])
    except (OSError, ValueError, KeyError):
        continue
    row = man.get(origin.name)
    if not row:
        missing += 1
        continue
    # `doi` and `title` are the keys publish reads — writing the manifest's
    # own field names produced thirty provenance files that counted for
    # nothing, and seventeen CC-BY tasks published with no ATTRIBUTION.md.
    stem = origin.stem
    title = stem.split("-", 1)[1] if "-" in stem else stem
    f.write_text(json.dumps({"title": title.replace("_", " "),
                             "doi": row["record"],
                             "source": row["name"], "url": row["url"],
                             "sha256": row["sha256"],
                             "license": row["license"],
                             "corpus": "Zenodo10K"}, indent=1))
    wrote += 1
print(f"    provenance: wrote {wrote}, unmatched {missing}")
PY
    fi
    ROLLOUT_REPO="${PPTXGYM_ROLLOUT_REPO:?set PPTXGYM_ROLLOUT_REPO when publishing}"
    ASSETS_REPO="${PPTXGYM_ASSETS_REPO:?set PPTXGYM_ASSETS_REPO when publishing}"
    if git clone --quiet --depth 1 \
        "https://github.com/${ROLLOUT_REPO}.git" /srv/rollout; then
        # A container has no git identity, and the whole publish dies at the
        # commit — after the materials are already on the hub, which leaves
        # the two halves disagreeing. Set it before anything is staged.
        git -C /srv/rollout config user.name "pptxgym"
        git -C /srv/rollout config user.email "pptxgym@users.noreply.github.com"
        LAYOUT_ARGS=(
            --task-class-dir "${PPTXGYM_TASK_CLASS_DIR:-evaluation_examples/task_class}"
            --task-assets-dir "${PPTXGYM_TASK_ASSETS_DIR:-evaluation_examples/task_assets}"
            --registry "${PPTXGYM_REGISTRY:-evaluation_examples/task_assets/pptxgym-ids.json}"
            --series "${PPTXGYM_SERIES:-110}"
            --series-first "${PPTXGYM_SERIES_FIRST:-1100001}"
            --series-last "${PPTXGYM_SERIES_LAST:-1109999}"
        )
        if [ -n "${PPTXGYM_TASK_LISTS_JSON:-}" ]; then
            while IFS= read -r task_list; do
                [ -z "$task_list" ] || LAYOUT_ARGS+=(--task-list "$task_list")
            done < <(printf '%s' "$PPTXGYM_TASK_LISTS_JSON" | jq -r '.[]')
        fi
        python3 -m pptxgym.publish --work work --rollout /srv/rollout \
            --repo "$ASSETS_REPO" "${LAYOUT_ARGS[@]}" \
            "${PUBLISH_ARGS[@]}" --push \
            $SMOKE 2>&1 | tee -a /tmp/crun.log \
            || echo "    PUBLISH FAILED — artefacts are in the final tar; publish can be re-run from them"
    else
        echo "    cannot clone ${ROLLOUT_REPO} — skipping publish, artefacts in the final tar"
    fi
else
    echo "    nothing shipped (or PPTXGYM_NO_PUBLISH set) — skipping publish"
fi

say "final upload"
if [ -n "${PPTXGYM_SKIP_FINAL_UPLOAD:-}" ]; then
    echo "    skipped by PPTXGYM_SKIP_FINAL_UPLOAD"
else
python3 - <<'PY'
import os, tarfile
from huggingface_hub import HfApi
api, repo = HfApi(), os.environ["PPTXGYM_RESULTS_REPO"]
run = os.environ["PPTXGYM_RUN"]
with tarfile.open("/tmp/final.tar.gz", "w:gz") as t:
    if os.path.exists("work"):
        t.add("work")
with tarfile.open("/tmp/final.tar.gz") as t:
    names = t.getnames()
# The archive every other recovery reads from, so it gets the same rule as the
# resume slots: a run that finished holding nothing must not overwrite a run
# that finished holding thirty decks. A publish-only job did exactly that.
if any(x.endswith("/state.json") for x in names):
    api.upload_file(path_or_fileobj="/tmp/final.tar.gz", repo_id=repo,
                    repo_type="dataset", path_in_repo=f"{run}/final.tar.gz")
    print("uploaded", os.path.getsize("/tmp/final.tar.gz") // 1048576, "MB")
else:
    print("work tree holds no deck — leaving the previous final.tar.gz alone")
PY
fi

say "done"
