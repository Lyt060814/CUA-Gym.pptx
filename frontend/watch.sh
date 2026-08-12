#!/usr/bin/env bash
# Poll a running HF job and judge it, without being able to hang doing either.
#
#   frontend/watch.sh <job-id> [minutes] [state-file]
#
# Every external call is wrapped in `timeout`. That is the whole reason this
# file exists as a rewrite rather than a patch: its predecessor called
# `hf jobs inspect` bare, and when the API stopped answering the watcher stopped
# with it — two of them sat frozen for four and a half hours against a job that
# had already finished, printing nothing, indistinguishable from a healthy one.
#
# A fetch that times out is *reported*, not swallowed. "I could not see" and
# "I looked and all is well" are different statements and the caller has to be
# able to tell them apart.
set -uo pipefail
JOB="${1:?usage: watch.sh <job-id> [minutes] [state-file]}"
MINUTES="${2:-20}"
STATE="${3:-/tmp/watch-$JOB.json}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

FETCH_TIMEOUT=120      # a log fetch that takes longer than this is not working
POLL=45

blind=0
for _ in $(seq 1 $(( MINUTES * 60 / POLL ))); do
    started=$(date +%s)

    state=$(timeout 60 hf jobs inspect "$JOB" 2>/dev/null \
            | grep -oE '"stage":[[:space:]]*"[A-Z_]+"' | head -1 \
            | grep -oE '[A-Z_]+' | tail -1)

    if timeout "$FETCH_TIMEOUT" hf jobs logs "$JOB" > "/tmp/watch-$JOB.log" 2>/dev/null; then
        blind=0
        age=$(( $(date +%s) - started ))
        # `--ack` because this loop *reports and exits* on a stop-level
        # alert: without it, restarting after dealing with one re-reports the
        # same thing and exits again, and the run goes unwatched from the
        # first finding onward — which is the moment it most needs watching.
        python3 -m pptxgym.orchestration.supervise "/tmp/watch-$JOB.log" \
                --state "$STATE" --age "$age" --ack 2>&1
        rc=$?
        printf '\njob %s: %s\n' "$JOB" "${state:-unreachable}"
        if [ "$rc" = 2 ]; then
            printf '=== something wants a human; stopping so it gets one\n'
            exit 2
        fi
        if [ -n "$state" ] && [ "$state" != "RUNNING" ]; then
            printf '=== job ended: %s\n' "$state"
            exit 0
        fi
    else
        blind=$(( blind + 1 ))
        printf '=== could not fetch logs (attempt %d) — this is the watcher '\
'failing, not the run\n' "$blind"
        # Three blind polls in a row is a broken watcher, and a broken watcher
        # must say so and stop rather than keep printing nothing.
        if [ "$blind" -ge 3 ]; then
            printf '=== blind for %d polls; giving up so somebody looks\n' "$blind"
            exit 3
        fi
    fi
    sleep "$POLL"
done

printf '=== %s minutes, still running, nothing wanting a human\n' "$MINUTES"
python3 -m pptxgym.orchestration.supervise "/tmp/watch-$JOB.log" --state "$STATE" 2>&1 | tail -20
