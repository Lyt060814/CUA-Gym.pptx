#!/usr/bin/env bash
# Block until the watcher has something to say, then end so the foreman wakes.
#
#   frontend/wait-alert.sh <alert-file> [offset] [max-minutes]
#
# The half of the split that ends. `watchd.sh` never stops watching; this reads
# the file it appends to, from `offset` bytes in, and returns as soon as there
# is more. Everything that arrived while the foreman was busy is still in the
# file, so nothing is missed by not being there to see it land — which is the
# whole point of separating the two.
#
# Prints the new text, then the new offset on the last line as
# `=== offset <N>`, so the next call resumes exactly where this one stopped.
#
# Exit 2: there is something to read.   0: the watched job ended.
#      3: nothing happened within max-minutes — say so rather than look busy.
set -uo pipefail
ALERTS="${1:?usage: wait-alert.sh <alert-file> [offset] [max-minutes]}"
OFFSET="${2:-0}"
MAX_MIN="${3:-30}"

touch "$ALERTS"
deadline=$(( $(date +%s) + MAX_MIN * 60 ))

while :; do
    size=$(stat -c %s "$ALERTS" 2>/dev/null || echo 0)
    if [ "$size" -gt "$OFFSET" ]; then
        tail -c "+$(( OFFSET + 1 ))" "$ALERTS"
        printf '=== offset %s\n' "$size"
        grep -q "ended:" <<< "$(tail -c "+$(( OFFSET + 1 ))" "$ALERTS")" \
            && exit 0
        exit 2
    fi
    if [ "$(date +%s)" -ge "$deadline" ]; then
        printf 'nothing from the watcher in %s minutes; it is still running '\
'(alert file %s, offset %s)\n' "$MAX_MIN" "$ALERTS" "$OFFSET"
        exit 3
    fi
    sleep 20
done
