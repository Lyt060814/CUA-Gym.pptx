#!/usr/bin/env bash
# Watch a running job and never stop watching.
#
#   frontend/watchd.sh <job-id> [state-file] [alert-file]
#
# Its predecessor exited the moment it found something, because a background
# command only notifies the foreman when it *ends* and there was no other way
# to be woken. That made the watcher blind for exactly as long as the fix took,
# and a second finding arriving during the first repair was simply lost.
#
# So the two jobs are split. This one polls and never leaves: every finding is
# appended to the alert file and the loop goes straight back to watching.
# `wait-alert.sh` is the one that ends, and ending is all it does — see there.
#
# Every external call is still wrapped in `timeout`. A fetch that times out is
# reported, not swallowed: "I could not see" and "I looked and all is well" are
# different statements and the reader has to be able to tell them apart.
set -uo pipefail
JOB="${1:?usage: watchd.sh <job-id> [state-file] [alert-file]}"
STATE="${2:-/tmp/watch-$JOB.json}"
ALERTS="${3:-/tmp/alerts-$JOB.log}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BEAT="${ALERTS%.log}.beat"

# Depend on nothing the caller happened to have set. `python3 -m pptxgym.
# supervise` needs the repo on the path, and `hf` lives in ~/.local/bin, which
# a shell that did not read a profile does not have — a watcher that dies of
# `command not found` looks exactly like a watcher with nothing to report.
cd "$REPO_DIR" || exit 1
export PATH="$HOME/.local/bin:$PATH"

FETCH_TIMEOUT=120
POLL=45

touch "$ALERTS"            # appended to, never truncated
blind=0

note() { printf '%s\n' "$*" >> "$ALERTS"; }

# Proof of life, rewritten every poll. `pgrep` says a process exists, which is
# not the same claim: run 14 went twenty minutes unwatched behind a check that
# had only ever proved the daemon lived three seconds. Anyone asking "is this
# watching" reads the age of this file and gets a number.
beat() { date -u +%s > "$BEAT"; }
beat

while :; do
    beat
    started=$(date +%s)

    state=$(timeout 60 hf jobs inspect "$JOB" 2>/dev/null \
            | grep -oE '"stage":[[:space:]]*"[A-Z_]+"' | head -1 \
            | grep -oE '[A-Z_]+' | tail -1)

    if timeout "$FETCH_TIMEOUT" hf jobs logs "$JOB" > "/tmp/watch-$JOB.log" 2>/dev/null; then
        blind=0
        age=$(( $(date +%s) - started ))
        # `--ack` marks what it reports as dealt with, so the next poll reports
        # what happens *next*. Without it this loop would re-append the same
        # finding every 45 seconds and the alert file would be useless.
        out=$(timeout 120 python3 -m pptxgym.orchestration.supervise "/tmp/watch-$JOB.log" \
                      --state "$STATE" --age "$age" --ack 2>&1)
        rc=$?
        if [ "$rc" = 2 ]; then
            note "=== $(date -u +%H:%M:%SZ) job $JOB wants a human"
            note "$out"
            note "=== end"
        fi
    else
        blind=$(( blind + 1 ))
        if [ "$blind" -ge 3 ]; then
            # A broken watcher must say so. It must not also stop: the job is
            # still running and the API usually comes back.
            note "=== $(date -u +%H:%M:%SZ) blind for $blind polls — this is the"\
"watcher failing to fetch, not the run failing. Still watching."
            note "=== end"
            blind=0
        fi
    fi

    if [ -n "$state" ] && [ "$state" != "RUNNING" ]; then
        note "=== $(date -u +%H:%M:%SZ) job $JOB ended: $state"
        note "=== end"
        exit 0
    fi
    sleep "$POLL"
done
