#!/usr/bin/env bash
# Does the runtime actually work, or does it merely contain the right binaries?
#
# Run this immediately after `bootstrap.sh`, before anything expensive.  Each
# check is one thing the pipeline does, in the order that a failure would stop
# it, so the first red line names the stage that would have died.
#
# The WPS round trip is last and is the one that matters: it is the only step
# that drives a GUI application on a virtual display, and it is the only part
# of the runtime we have never seen work anywhere but a developer machine.

set -uo pipefail

PPTX="${1:-}"
fails=0
check() {
    local name="$1"; shift
    if "$@" >/tmp/smoke-$$.out 2>&1; then
        printf 'ok    %s\n' "$name"
    else
        printf 'FAIL  %s\n' "$name"
        sed 's/^/        /' /tmp/smoke-$$.out | tail -20
        fails=$((fails + 1))
    fi
}

# A previous run's output left in place would let a broken `soffice` pass on
# the strength of a stale PDF, which is the one way a smoke test can be worse
# than no smoke test.
rm -rf /tmp/smoke-conv

check "python imports"        python3 -c "import pptx, lxml, PIL, pandas, huggingface_hub"
check "pptxgym imports"       python3 -c "import pptxgym.pipeline, pptxgym.comparators, pptxgym.emit"
check "soffice converts"      bash -c 'soffice --headless --convert-to pdf --outdir /tmp/smoke-conv "$0" && test -s /tmp/smoke-conv/*.pdf' "$PPTX"
# `pg-*.png`, not `pg-1.png`: pdftoppm zero-pads the page number to the width
# of the page count, so a one-page deck gives `pg-1.png` and a ten-page deck
# gives `pg-01.png`.  Asserting the unpadded name failed here on a real deck.
check "pdftoppm rasterises"   bash -c 'pdftoppm -png -r 50 -f 1 -l 1 /tmp/smoke-conv/*.pdf /tmp/smoke-conv/pg && compgen -G "/tmp/smoke-conv/pg-*.png" >/dev/null'
check "Xvfb starts"           bash -c 'Xvfb :99 -screen 0 1280x1024x24 & p=$!; sleep 3; DISPLAY=:99 xdotool getdisplaygeometry; r=$?; kill $p 2>/dev/null; exit $r'

# `claude --version` proves the binary; only a real prompt proves the
# credentials, and that is the thing most likely to be wrong in a container.
#
# The credential is `CLAUDE_CODE_OAUTH_TOKEN`, from `claude setup-token` on a
# machine where someone can log in interactively — a container cannot, and on
# HF Jobs there is no `exec` to log in through either.  It draws on the
# subscription rather than billing an API key.
[ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] || printf 'warn  CLAUDE_CODE_OAUTH_TOKEN unset — the next check is testing something else\n'
check "claude authenticates"  bash -c 'timeout 120 claude -p "reply with the single word: ok" 2>&1 | grep -qi ok'

# The real one.  `--json` so the verdict is machine-readable, and the deck is
# the caller's so this can run against whatever the job was going to process
# anyway.  Under 64 MB of /dev/shm this is where it would show.
if [ -n "$PPTX" ]; then
    check "WPS round trip"    python3 -m pptxgym.wps_roundtrip --json --workers 1 "$PPTX"
else
    printf 'skip  WPS round trip (no .pptx given)\n'
fi

rm -f /tmp/smoke-$$.out
printf '\n%s\n' "$([ "$fails" -eq 0 ] && echo 'runtime is usable' || echo "$fails check(s) failed")"
exit $((fails > 0))
