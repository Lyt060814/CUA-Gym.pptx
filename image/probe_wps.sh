#!/usr/bin/env bash
# The one question a container answers and this machine cannot: does WPS start
# under Xvfb with no user profile, no desktop session, and 64 MB of /dev/shm?
#
# Deliberately self-contained — no repo, no credentials, no corpus. It builds
# its own deck and drives WPS the way `wps_roundtrip.py` does (open, wait for
# the title, Ctrl+S, wait for the bytes to change). If this passes, the rest
# of the runtime is `apt-get` and the full `bootstrap.sh` is worth its build
# time. If it fails, it fails in about six minutes for a few cents.
set -uo pipefail
export DEBIAN_FRONTEND=noninteractive
say() { printf '\n### %s\n' "$*"; }

say "environment before we touch anything"
df -h /dev/shm /tmp | sed 's/^/    /'
nproc | sed 's/^/    cpus: /'
id | sed 's/^/    /'

# WPS's postinst calls six external commands that its own dependency list does
# not declare.  On a desktop system they are always there; in a minimal
# container they are not, and dpkg fails to configure the package -- which
# leaves `wpp` on disk but half-installed.  Read straight out of
# /var/lib/dpkg/info/wps-office.postinst on a working machine rather than
# discovered one HF Jobs round trip at a time:
#   hexdump              bsdextrautils
#   xdg-icon-resource    xdg-utils
#   update-mime-database shared-mime-info
#   update-desktop-database  desktop-file-utils
#   fc-cache             fontconfig
#   ldconfig             libc-bin (always present)
# The library set a GUI office suite needs that a minimal image does not have.
# `libxslt1.1` is not a guess: the container's own dlopen said
#   libwppmain.so failed, error: libxslt.so.1: cannot open shared object file
# and libxslt is in neither the package's Depends nor its Recommends.  The
# rest is the usual Qt/X11 runtime surface, added in one go rather than one
# five-minute job per library.
#
# Note what is NOT here: libcrypto.so.1.1, libQtCore.so.4 and most of the
# `ldd ... not found` list.  This machine, where WPS works, is missing exactly
# the same ones -- they resolve through the wrapper's RPATH or are never
# loaded.  A naive `ldd` over office6 reports 70 unresolved libraries on a
# working install, so that list is noise and the dlopen message is signal.
say "apt"
apt-get update -qq
apt-get install -y --no-install-recommends \
    ca-certificates wget xvfb xdotool x11-utils procps psmisc xkb-data x11-xkb-utils xfonts-base bsdextrautils xdg-utils shared-mime-info desktop-file-utils fontconfig \
    libxslt1.1 libsdl2-2.0-0 libasound2 libcurl4 \
    libgl1 libegl1 libglu1-mesa libsm6 libxrender1 libxext6 libxcb1 \
    libxkbcommon-x11-0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 \
    libxcb-randr0 libxcb-render-util0 libxcb-shape0 libxcb-xinerama0 \
    libxcomposite1 libxdamage1 libxrandr2 libxi6 libxtst6 libnss3 \
    libatk1.0-0 libpango-1.0-0 libcairo2 libfreetype6 libfontconfig1 \
    libcups2 libglib2.0-0 libbz2-1.0 dbus-x11 \
    python3 python3-pip fonts-liberation fonts-dejavu-core >/dev/null 2>&1
echo "    ok"

say "WPS 11.1.0.11723 (319 MB)"
wget -q -O /tmp/wps.deb \
  https://wdl1.pcfg.cache.wpscdn.com/wpsdl/wpsoffice/download/linux/11723/wps-office_11.1.0.11723.XA_amd64.deb \
  || { echo "    FETCH FAILED"; exit 1; }
ls -l /tmp/wps.deb | sed 's/^/    /'
# WITH recommends, unlike everything else here.  A proprietary GUI package
# puts the libraries it needs-but-does-not-strictly-require in Recommends,
# and `wpp` exiting in one second with an empty log is what that looks like.
apt-get install -y /tmp/wps.deb >/tmp/wpsinstall.log 2>&1 \
  || { echo "    INSTALL FAILED"; tail -30 /tmp/wpsinstall.log | sed 's/^/    /'; exit 1; }
command -v wpp | sed 's/^/    wpp at /' || { echo "    NO wpp BINARY"; exit 1; }

say "seed the EULA key"
mkdir -p /root/.config/Kingsoft
# Read off a working install.  `SystemCheck\DoNotReport` is the one that
# matters here: the container showed a modal "System Check" window that took
# focus, which is precisely the dialog `wps_roundtrip._settle_dialogs` was
# written to close.
cat > /root/.config/Kingsoft/Office.conf <<'CONF'
[6.0]
common\AcceptedEULA=true
common\SystemCheck\DoNotReport=true
wpp\Application%20Settings\ShowStartUpTaskPane=0
wpp\Application%20Settings\ShowTipDialogCount=0
CONF
cat /root/.config/Kingsoft/Office.conf | sed 's/^/    /'

say "a deck to open"
# `python3 -m pip`, not `pip`, and errors NOT swallowed: the first run of
# this probe hid a pip failure behind `>/dev/null 2>&1` and then blamed WPS
# for a missing file.  Ubuntu 22.04 ships pip 22, which predates
# --break-system-packages, so passing it is itself the error.
python3 -m pip install --quiet python-pptx || { echo "    PIP FAILED"; exit 1; }
python3 - <<'PY'
from pptx import Presentation
from pptx.util import Inches
p = Presentation()
s = p.slides.add_slide(p.slide_layouts[5])
s.shapes.title.text = "probe"
s.shapes.add_textbox(Inches(1), Inches(2), Inches(4), Inches(1)).text_frame.text = "hello"
p.save("/tmp/probe.pptx")
print("    wrote /tmp/probe.pptx")
PY

say "Xvfb"
Xvfb :99 -screen 0 1280x1024x24 >/tmp/xvfb.log 2>&1 &
XPID=$!
for i in $(seq 1 30); do DISPLAY=:99 xdotool getdisplaygeometry >/dev/null 2>&1 && break; sleep 1; done
DISPLAY=:99 xdotool getdisplaygeometry | sed 's/^/    geometry: /' || { echo "    XVFB NEVER CAME UP"; cat /tmp/xvfb.log; exit 1; }

say "open the deck in WPS"
BEFORE=$(md5sum /tmp/probe.pptx | cut -d' ' -f1)
DISPLAY=:99 wpp /tmp/probe.pptx >/tmp/wpp.log 2>&1 &
WPID=$!
WIN=""
for i in $(seq 1 90); do
    WIN=$(DISPLAY=:99 xdotool search --name "probe" 2>/dev/null | head -1)
    [ -n "$WIN" ] && break
    kill -0 $WPID 2>/dev/null || { echo "    WPS EXITED after ${i}s"; tail -40 /tmp/wpp.log | sed 's/^/    /'; break; }
    sleep 1
done
if [ -z "$WIN" ]; then
    echo "    NO WINDOW — this is the failure we came to find"
    say "every window on the display (a dialog we have not seen would show here)"
    DISPLAY=:99 xdotool search --name "." 2>/dev/null | while read -r w; do
        printf '    %s  %s\n' "$w" "$(DISPLAY=:99 xdotool getwindowname "$w" 2>/dev/null)"
    done
    echo "    --- wpp.log (empty means it said nothing, which is itself the clue) ---"
    tail -40 /tmp/wpp.log | sed 's/^/    log: /'

    say "run it in the foreground for an exit code"
    DISPLAY=:99 timeout 30 /opt/kingsoft/wps-office/office6/wpp /tmp/probe.pptx 2>&1 | tail -20 | sed 's/^/    /'
    echo "    exit: ${PIPESTATUS[0]}"

    say "shared libraries office6 cannot resolve"
    MISSING=$(for f in /opt/kingsoft/wps-office/office6/wpp /opt/kingsoft/wps-office/office6/*.so*; do
        ldd "$f" 2>/dev/null | awk '/not found/ {print $1}'
    done | sort -u)
    if [ -n "$MISSING" ]; then
        echo "$MISSING" | sed 's/^/    MISSING /'
        say "which packages would supply them"
        for so in $MISSING; do
            printf '    %-32s %s\n' "$so" "$(apt-file search --package-only "/$so" 2>/dev/null | head -3 | tr '\n' ' ')"
        done
    else
        echo "    none — every library resolves, so this is not a linking failure"
    fi
    kill $WPID $XPID 2>/dev/null
    exit 1
fi
echo "    window $WIN: $(DISPLAY=:99 xdotool getwindowname "$WIN")"

say "the window tree — is there a notes pane at all?"
# Four candidate click points along the bottom of a maximised window all
# failed to dirty the document while keys demonstrably arrived. The remaining
# explanation is that the notes pane is not displayed, and a click cannot find
# a pane that is not there. xwininfo lists the child windows and their
# geometry, which answers it directly instead of by another guess.
apt-get install -y -qq --no-install-recommends x11-utils >/dev/null 2>&1
DISPLAY=:99 xwininfo -root -tree 2>/dev/null | head -60 | sed 's/^/    /'

say "geometry — does the window fill the screen the click coordinates assume?"
# `wps_roundtrip` clicks NOTES_XY = (500, 1143) on a SCREEN of 1920x1200, which
# is only the notes pane if the window is maximised.  There is no window
# manager here to maximise it, and a click that misses the notes pane types
# into whatever it hits -- or into nothing, which is what "the title never
# showed it as modified" looks like from the outside.
DISPLAY=:99 xdotool getdisplaygeometry | sed 's/^/    screen: /'
DISPLAY=:99 xdotool getwindowgeometry "$WIN" | sed 's/^/    /'

say "does a keystroke reach the document at all?"
# Type into the window and read the title back.  If the title never gains its
# ` * `, keys are not arriving -- independent of where the notes pane is.
# Does this X server have a keymap at all?  Without one Xvfb accepts key
# events and delivers nothing.
DISPLAY=:99 setxkbmap -query 2>&1 | sed 's/^/    xkb: /' || echo "    xkb: setxkbmap failed"
DISPLAY=:99 xmodmap -pke 2>/dev/null | wc -l | sed 's/^/    keycodes mapped: /'

# An unambiguous test.  F5 starts the slideshow, which opens a window -- a
# result that does not depend on where focus is or what is editable, unlike
# typing into a document view and reading the title back.  A window appearing
# proves keys arrive; nothing appearing proves they do not.
WINS_BEFORE=$(DISPLAY=:99 xdotool search --name "." 2>/dev/null | wc -l)
DISPLAY=:99 xdotool windowactivate --sync "$WIN" 2>/dev/null
DISPLAY=:99 xdotool key --window "$WIN" F5
sleep 8
WINS_AFTER=$(DISPLAY=:99 xdotool search --name "." 2>/dev/null | wc -l)
printf '    windows before F5: %s   after: %s  -> keys %s\n' \
    "$WINS_BEFORE" "$WINS_AFTER" \
    "$([ "$WINS_AFTER" -gt "$WINS_BEFORE" ] && echo ARRIVE || echo 'DO NOT ARRIVE')"
DISPLAY=:99 xdotool key --window "$WIN" Escape 2>/dev/null
sleep 2

BEFORE_TITLE=$(DISPLAY=:99 xdotool getwindowname "$WIN")
DISPLAY=:99 xdotool windowactivate --sync "$WIN" 2>/dev/null
DISPLAY=:99 xdotool key --window "$WIN" --clearmodifiers ctrl+a
sleep 1
DISPLAY=:99 xdotool type --window "$WIN" --delay 120 "ZZ"
sleep 3
printf '    title before: %s\n    title after : %s\n' \
    "$BEFORE_TITLE" "$(DISPLAY=:99 xdotool getwindowname "$WIN")"

say "close whatever else is on the display first"
for round in 1 2 3; do
    DISPLAY=:99 xdotool search --name "System Check|WPS Office|Tip|Prompt" 2>/dev/null | while read -r w; do
        [ "$w" = "$WIN" ] && continue
        DISPLAY=:99 xdotool windowclose "$w" 2>/dev/null
    done
    sleep 2
done

say "which way of typing reaches the document?"
# Five explanations have been proposed for "the notes edit did not reach the
# document" and four were wrong, each costing a ten-minute job. So stop
# proposing and enumerate: click the notes pane, try one input method, read
# the title back, and say plainly which ones work. The title gains a ` * `
# when the document is modified; that is the only ground truth there is.
#
# The notes pane sits at the bottom of a maximised window; NOTES_XY is
# (500, 1143) on 1920x1200, so the same fractions here.
NX=$(( 1280 * 500 / 1920 ))
NY=$(( 1024 * 1143 / 1200 ))
echo "    notes pane at ${NX},${NY} on this 1280x1024 screen"

try() {
    local label="$1"; shift
    local before after
    before=$(DISPLAY=:99 xdotool getwindowname "$WIN")
    DISPLAY=:99 xdotool mousemove "$NX" "$NY" 2>/dev/null
    DISPLAY=:99 xdotool click 1 2>/dev/null
    sleep 2
    "$@" >/dev/null 2>&1
    sleep 4
    after=$(DISPLAY=:99 xdotool getwindowname "$WIN")
    if [ "$before" != "$after" ]; then
        printf '    %-42s DIRTIED  (%s)\n' "$label" "$after"
        # undo so the next method starts from a clean document
        DISPLAY=:99 xdotool key --clearmodifiers ctrl+z >/dev/null 2>&1
        sleep 2
        return 0
    fi
    printf '    %-42s no change\n' "$label"
    return 1
}

try "type (no --window)"            env DISPLAY=:99 xdotool type --delay 120 ZZ
try "type --window"                 env DISPLAY=:99 xdotool type --window "$WIN" --delay 120 ZZ
try "key --window Z Z"              env DISPLAY=:99 xdotool key --window "$WIN" Z Z
try "windowfocus then type"         bash -c "DISPLAY=:99 xdotool windowfocus $WIN; DISPLAY=:99 xdotool type --delay 120 ZZ"
try "windowactivate --sync + type"  bash -c "DISPLAY=:99 xdotool windowactivate --sync $WIN; DISPLAY=:99 xdotool type --delay 120 ZZ"
try "key (no --window) Z Z"         env DISPLAY=:99 xdotool key --clearmodifiers Z Z
try "double-click then type"        bash -c "DISPLAY=:99 xdotool click --repeat 2 1; DISPLAY=:99 xdotool type --delay 120 ZZ"

say "input focus, for the record"
DISPLAY=:99 xdotool getwindowfocus 2>&1 | sed 's/^/    getwindowfocus: /'
echo "    document window: $WIN"

say "what the display held at the end"
DISPLAY=:99 xdotool search --name "." 2>/dev/null | while read -r w; do
    printf '    %s  %s\n' "$w" "$(DISPLAY=:99 xdotool getwindowname "$w" 2>/dev/null)"
done

kill $WPID $XPID 2>/dev/null
